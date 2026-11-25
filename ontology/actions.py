#!/usr/bin/env python3
"""阶段五：操作型本体动作引擎（Operational Ontology Action Engine）。

实现 schema.yaml 中定义的 3 个受控业务动作：
  1. EscalateOverdueOrder       — 针对逾期未发货订单发起销售预警与仓储催办工单
  2. FixProductCategory         — 将异常分类商品重新绑定到合法叶子分类（写入治理覆盖层）
  3. ReconcileCustomerIdentity  — 人工审核并固化跨系统客户映射（写入 L3 Crosswalk）

设计原则（参考 Palantir Foundry / AIP Action 设计）：
  - 所有写操作必须经由对象类型与显式前置条件守卫（Precondition Guard），禁止裸改库；
  - 支持 dry_run 预演模式，方便 AI Agent 或审批流先验证影响范围再提交；
  - 永不篡改原始源系统 CSV，所有治理变更落入版本化覆盖表，所有执行记录落入追加式审计账本。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT_DIR / "data" / "ontology" / "ontology.db"
DEFAULT_LEDGER_PATH = ROOT_DIR / "data" / "ontology" / "action_ledger.jsonl"
DEFAULT_CROSSWALK_PATH = ROOT_DIR / "ontology" / "crosswalk" / "customer_crosswalk.yaml"
DEFAULT_OVERRIDES_PATH = ROOT_DIR / "ontology" / "overrides" / "product_category_overrides.yaml"
DEFAULT_SCHEMA_PATH = ROOT_DIR / "ontology" / "schema.yaml"
DEFAULT_GRAPH_PATH = ROOT_DIR / "data" / "ontology" / "ontology_graph.json"


class ActionError(Exception):
    """动作执行基类异常。"""


class PreconditionError(ActionError):
    """对象不满足本体前置条件或安全守卫规则时抛出。"""


class ActionEngine:
    """受控操作型本体动作执行引擎。"""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        ledger_path: Path | str = DEFAULT_LEDGER_PATH,
        crosswalk_path: Path | str = DEFAULT_CROSSWALK_PATH,
        overrides_path: Path | str = DEFAULT_OVERRIDES_PATH,
        schema_path: Path | str = DEFAULT_SCHEMA_PATH,
        graph_path: Path | str = DEFAULT_GRAPH_PATH,
    ) -> None:
        self.db_path = Path(db_path)
        self.ledger_path = Path(ledger_path)
        self.crosswalk_path = Path(crosswalk_path)
        self.overrides_path = Path(overrides_path)
        self.schema_path = Path(schema_path)
        self.graph_path = Path(graph_path)

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(f"本体库不存在：{self.db_path}")
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def read_ledger(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries

    def _append_ledger(self, record: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _next_action_id(self) -> str:
        existing = self.read_ledger()
        return f"ACT-{len(existing) + 1:04d}"

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _get_as_of_date(self, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT value FROM metadata WHERE key = 'as_of_date'").fetchone()
        return row["value"] if row else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # Action 1: SalesOrder.EscalateOverdueOrder
    # ------------------------------------------------------------------

    def escalate_overdue_order(
        self,
        order_no: str,
        reason: str,
        priority: str = "P1",
        actor: str = "ontology-agent",
        dry_run: bool = False,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        """针对逾期未有效发货订单发起销售预警与 WMS 催办工单。"""
        if priority not in {"P1", "P2"}:
            raise PreconditionError(f"非法优先级 {priority!r}，仅允许 'P1' 或 'P2'")
        if not reason or not reason.strip():
            raise PreconditionError("催办原因 reason 不能为空")

        conn = self._connect()
        try:
            as_of = self._get_as_of_date(conn)
            order = conn.execute(
                """
                SELECT o.*, c.canonical_name, c.tier, c.is_vip,
                       s.owner_id, s.owner_name, s.team
                FROM sales_orders o
                JOIN customers c ON o.placed_by_customer_id = c.customer_id
                LEFT JOIN sales_owners s ON c.owned_by_owner_id = s.owner_id
                WHERE o.order_no = ?
                """,
                (order_no,),
            ).fetchone()
        finally:
            conn.close()

        if order is None:
            raise PreconditionError(f"目标订单不存在：{order_no}")

        # 强守卫：必须满足 schema.yaml 前置条件 SalesOrder.is_overdue_unshipped == true
        if not order["is_overdue_unshipped"]:
            raise PreconditionError(
                f"拒绝催办 {order_no}：不满足前置条件 is_overdue_unshipped==true "
                f"（erp_status={order['erp_status']}, wms_fulfillment={order['fulfillment_status']}, "
                f"promised_date={order['promised_date']}, actual_ship_date={order['actual_ship_date']}）"
            )

        # 幂等防重守卫
        if not allow_duplicate:
            for entry in self.read_ledger():
                if (
                    entry.get("action_name") == "EscalateOverdueOrder"
                    and entry.get("target_object_id") == order_no
                    and entry.get("status") == "COMMITTED"
                ):
                    raise PreconditionError(
                        f"订单 {order_no} 已存在已提交催办记录 {entry['action_id']} "
                        f"（工单 {entry['side_effects']['wms_ticket_id']}），请勿重复催办"
                    )

        action_id = self._next_action_id()
        ticket_id = f"ESC-{as_of.replace('-', '')}-{action_id.split('-')[1]}"
        notify_target = (
            f"{order['owner_name']} ({order['owner_id']} / {order['team']})"
            if order["owner_id"]
            else "sales-ops-unassigned (无归属销售，转销售运营兜底)"
        )

        record = {
            "action_id": action_id,
            "action_name": "EscalateOverdueOrder",
            "status": "DRY_RUN_OK" if dry_run else "COMMITTED",
            "executed_at": self._now_iso(),
            "as_of_date": as_of,
            "actor": actor,
            "target_object_type": "SalesOrder",
            "target_object_id": order_no,
            "parameters": {
                "reason": reason.strip(),
                "priority": priority,
            },
            "precondition_snapshot": {
                "is_overdue_unshipped": True,
                "customer_name": order["canonical_name"],
                "customer_tier": order["tier"],
                "promised_date": order["promised_date"],
                "fulfillment_status": order["fulfillment_status"],
                "total_amount": round(float(order["total_amount"]), 2),
            },
            "side_effects": {
                "wms_ticket_id": ticket_id,
                "notify_sales_owner": notify_target,
                "dispatch_instruction": f"[{priority}] 订单 {order_no}（{order['canonical_name']}）已逾期，请优先安排出库",
            },
        }

        if not dry_run:
            self._append_ledger(record)
        return record

    # ------------------------------------------------------------------
    # Action 2: Product.FixProductCategory
    # ------------------------------------------------------------------

    def fix_product_category(
        self,
        sku: str,
        target_leaf_category_code: str,
        reviewer: str,
        reason: str,
        dry_run: bool = False,
        rebuild: bool = False,
    ) -> dict[str, Any]:
        """将存在分类异常的 SKU 重新绑定到合法的二级叶子分类（写入治理覆盖层）。"""
        if not reviewer or not reviewer.strip():
            raise PreconditionError("审核人 reviewer 不能为空")
        if not reason or not reason.strip():
            raise PreconditionError("修复理由 reason 不能为空")

        conn = self._connect()
        try:
            as_of = self._get_as_of_date(conn)
            product = conn.execute(
                "SELECT * FROM products WHERE sku = ?",
                (sku,),
            ).fetchone()
            target_cat = conn.execute(
                "SELECT * FROM product_categories WHERE category_code = ?",
                (target_leaf_category_code,),
            ).fetchone()
        finally:
            conn.close()

        if product is None:
            raise PreconditionError(f"目标产品不存在：{sku}")
        if product["category_status"] == "VALID":
            raise PreconditionError(
                f"拒绝修复 {sku}：当前分类状态已为 VALID（{product['belongs_to_category_code']}），不满足前置条件 category_status != VALID"
            )
        if target_cat is None:
            raise PreconditionError(
                f"目标分类码 {target_leaf_category_code!r} 不在标准分类码表 ProductCategory 中"
            )
        if not target_cat["is_leaf"]:
            raise PreconditionError(
                f"拒绝绑定到非叶子节点 {target_leaf_category_code}（{target_cat['category_name']}），产品必须归属到叶子分类"
            )

        action_id = self._next_action_id()
        override_entry = {
            "sku": sku,
            "target_leaf_category_code": target_leaf_category_code,
            "target_category_name": target_cat["category_name"],
            "reviewer": reviewer.strip(),
            "reviewed_at": as_of,
            "reason": reason.strip(),
        }

        record = {
            "action_id": action_id,
            "action_name": "FixProductCategory",
            "status": "DRY_RUN_OK" if dry_run else "COMMITTED",
            "executed_at": self._now_iso(),
            "as_of_date": as_of,
            "actor": reviewer.strip(),
            "target_object_type": "Product",
            "target_object_id": sku,
            "parameters": {
                "target_leaf_category_code": target_leaf_category_code,
                "reason": reason.strip(),
            },
            "precondition_snapshot": {
                "product_name": product["product_name"],
                "previous_category_status": product["category_status"],
                "raw_category_code": product["raw_category_code"],
                "raw_category_name": product["raw_category_name"],
                "target_category_name": target_cat["category_name"],
                "target_root_category": target_cat["root_category_name"],
            },
            "side_effects": {
                "overrides_file": str(self.overrides_path),
                "rebuilt_ontology": False,
            },
        }

        if not dry_run:
            self._upsert_product_override(override_entry)
            if rebuild:
                self._rebuild_ontology()
                record["side_effects"]["rebuilt_ontology"] = True
            self._append_ledger(record)

        return record

    def _upsert_product_override(self, entry: dict[str, Any]) -> None:
        self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
        if self.overrides_path.exists():
            data = yaml.safe_load(self.overrides_path.read_text(encoding="utf-8")) or {}
        else:
            data = {"version": "1.0", "overrides": []}
        overrides: list[dict[str, Any]] = [
            o for o in data.get("overrides", []) if o.get("sku") != entry["sku"]
        ]
        overrides.append(entry)
        data["version"] = data.get("version", "1.0")
        data["overrides"] = overrides
        self.overrides_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Action 3: Customer.ReconcileCustomerIdentity
    # ------------------------------------------------------------------

    def reconcile_customer_identity(
        self,
        crm_account_id: str,
        erp_customer_no: str,
        reviewer: str,
        rationale: str,
        dry_run: bool = False,
        rebuild: bool = False,
    ) -> dict[str, Any]:
        """人工审核并固化一条 CRM 账号与 ERP 客户的跨系统映射（写入 L3 Crosswalk）。"""
        if not reviewer or not reviewer.strip():
            raise PreconditionError("审核人 reviewer 不能为空")
        if not rationale or not rationale.strip():
            raise PreconditionError("合并依据 rationale 不能为空")

        conn = self._connect()
        try:
            as_of = self._get_as_of_date(conn)
            crm_cust = conn.execute(
                "SELECT * FROM customers WHERE crm_account_id = ?",
                (crm_account_id,),
            ).fetchone()
            erp_cust = conn.execute(
                "SELECT * FROM customers WHERE erp_customer_no = ?",
                (erp_customer_no,),
            ).fetchone()
        finally:
            conn.close()

        if crm_cust is None:
            raise PreconditionError(f"未找到 CRM 账号对应的客户对象：{crm_account_id}")
        if erp_cust is None:
            raise PreconditionError(f"未找到 ERP 编号对应的客户对象：{erp_customer_no}")

        if crm_cust["customer_id"] == erp_cust["customer_id"]:
            raise PreconditionError(
                f"{crm_account_id} 与 {erp_customer_no} 已经归属到同一个客户实体 {crm_cust['customer_id']}，无需重复对齐"
            )
        if crm_cust["erp_customer_no"] is not None:
            raise PreconditionError(
                f"CRM 账号 {crm_account_id} 已绑定到另一个 ERP 客户 {crm_cust['erp_customer_no']}，禁止强制改绑"
            )
        if erp_cust["crm_account_id"] is not None:
            raise PreconditionError(
                f"ERP 客户 {erp_customer_no} 已绑定到另一个 CRM 账号 {erp_cust['crm_account_id']}，禁止强制改绑"
            )

        # 硬安全守卫 1：两个法人实体若信用代码均非空且不同，绝对禁止错并
        if (
            crm_cust["credit_code"]
            and erp_cust["credit_code"]
            and crm_cust["credit_code"] != erp_cust["credit_code"]
        ):
            raise PreconditionError(
                f"信用代码冲突，拒绝合并不同法人主体："
                f"{crm_account_id}({crm_cust['canonical_name']}, {crm_cust['credit_code']}) != "
                f"{erp_customer_no}({erp_cust['canonical_name']}, {erp_cust['credit_code']})"
            )

        # 硬安全守卫 2：主体类型必须一致（禁止把公司合并到个人）
        if crm_cust["party_type"] != erp_cust["party_type"]:
            raise PreconditionError(
                f"主体类型冲突，拒绝合并：{crm_account_id}({crm_cust['party_type']}) != "
                f"{erp_customer_no}({erp_cust['party_type']})"
            )

        action_id = self._next_action_id()
        mapping_entry = {
            "crm_account_id": crm_account_id,
            "erp_customer_no": erp_customer_no,
            "reviewer": reviewer.strip(),
            "reviewed_at": as_of,
            "rationale": rationale.strip(),
        }

        record = {
            "action_id": action_id,
            "action_name": "ReconcileCustomerIdentity",
            "status": "DRY_RUN_OK" if dry_run else "COMMITTED",
            "executed_at": self._now_iso(),
            "as_of_date": as_of,
            "actor": reviewer.strip(),
            "target_object_type": "Customer",
            "target_object_id": f"{crm_account_id}<->{erp_customer_no}",
            "parameters": {
                "crm_account_id": crm_account_id,
                "erp_customer_no": erp_customer_no,
                "rationale": rationale.strip(),
            },
            "precondition_snapshot": {
                "crm_customer_id": crm_cust["customer_id"],
                "crm_name": crm_cust["canonical_name"],
                "erp_customer_id": erp_cust["customer_id"],
                "erp_name": erp_cust["canonical_name"],
                "party_type": crm_cust["party_type"],
            },
            "side_effects": {
                "crosswalk_file": str(self.crosswalk_path),
                "rebuilt_ontology": False,
            },
        }

        if not dry_run:
            self._append_crosswalk(mapping_entry)
            if rebuild:
                self._rebuild_ontology()
                record["side_effects"]["rebuilt_ontology"] = True
            self._append_ledger(record)

        return record

    def _append_crosswalk(self, entry: dict[str, Any]) -> None:
        self.crosswalk_path.parent.mkdir(parents=True, exist_ok=True)
        if self.crosswalk_path.exists():
            data = yaml.safe_load(self.crosswalk_path.read_text(encoding="utf-8")) or {}
        else:
            data = {"version": "1.0", "mappings": []}
        mappings: list[dict[str, Any]] = list(data.get("mappings", []))
        mappings.append(entry)
        data["version"] = data.get("version", "1.0")
        data["mappings"] = mappings
        self.crosswalk_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _rebuild_ontology(self) -> None:
        from scripts.build_ontology import build_ontology

        build_ontology(
            schema_path=self.schema_path,
            crosswalk_path=self.crosswalk_path,
            overrides_path=self.overrides_path,
            db_path=self.db_path,
            graph_path=self.graph_path,
            quiet=True,
        )
