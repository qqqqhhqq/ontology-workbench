#!/usr/bin/env python3
"""阶段五：面向 AI Agent / MCP 的 Ontology 语义工具层。

将底层的 OntologyClient（读）与 ActionEngine（写）封装为标准化的 JSON Schema 工具集：
  - LLM 无需理解 10 张脏源表结构，无需手写易错的多表 JOIN SQL；
  - 所有查询遵循统一业务语义与事实源裁决；
  - 所有写操作强制经过 ActionEngine 前置条件守卫、支持 dry_run 预演、并自动写入审计账本。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ontology.actions import (
    ActionEngine,
    ActionError,
    DEFAULT_CROSSWALK_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_GRAPH_PATH,
    DEFAULT_LEDGER_PATH,
    DEFAULT_OVERRIDES_PATH,
    DEFAULT_SCHEMA_PATH,
)
from ontology.client import OntologyClient


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "find_vip_overdue_orders",
        "mode": "read",
        "description": "查询近 N 天内下单的 VIP 客户逾期未有效发货订单（已按 WMS 事实源裁决，自动关联负责销售与团队）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "window_days": {
                    "type": "integer",
                    "default": 30,
                    "description": "回看下单时间窗口天数，默认 30 天",
                }
            },
        },
    },
    {
        "name": "explain_order_overdue_status",
        "mode": "read",
        "description": "解释任意一张销售订单为何命中或未命中“近 30 天 VIP 逾期未发货”规则，返回业务可解释证据。",
        "input_schema": {
            "type": "object",
            "required": ["order_no"],
            "properties": {
                "order_no": {"type": "string", "description": "标准订单号，如 SO-20261105-905"},
                "window_days": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "get_customer_360",
        "mode": "read",
        "description": "查询跨 CRM/ERP/WMS 的客户 360° 全景视图。支持按客户规范全称、简称或更名前旧名（别名）、客户ID检索。",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_query": {
                    "type": "string",
                    "description": "可选检索词（支持规范名、别名如“博思咨询”“星辰设计”、CUST/ACC/C 编号）；为空返回全部客户",
                }
            },
        },
    },
    {
        "name": "get_category_sales_and_audit",
        "mode": "read",
        "description": "沿产品分类树上卷汇总一级大类（办公设备 vs 耗材）有效销售额，并列出全部异常分类商品及影响金额。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_data_quality_issues",
        "mode": "read",
        "description": "查询跨系统数据质量与一致性异常（DQ-001 ~ DQ-012），支持按严重级别或规则码过滤。",
        "input_schema": {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "enum": ["HIGH", "MEDIUM", "LOW"],
                    "description": "按严重级别过滤",
                },
                "rule_code": {
                    "type": "string",
                    "description": "按规则码过滤，如 DQ-001",
                },
            },
        },
    },
    {
        "name": "inspect_object_graph",
        "mode": "read",
        "description": "查询任意本体对象在知识图谱中的全部出边、入边关系及挂载的数据质量异常。",
        "input_schema": {
            "type": "object",
            "required": ["object_type", "object_id"],
            "properties": {
                "object_type": {
                    "type": "string",
                    "enum": [
                        "Customer",
                        "SalesOwner",
                        "Product",
                        "ProductCategory",
                        "SalesOrder",
                        "OrderLine",
                        "Shipment",
                    ],
                },
                "object_id": {"type": "string"},
            },
        },
    },
    {
        "name": "escalate_overdue_order",
        "mode": "action",
        "description": "【受控动作】针对已逾期未有效发货订单发起销售预警与 WMS 加急催办工单。强制校验 is_overdue_unshipped==true 与幂等防重。",
        "input_schema": {
            "type": "object",
            "required": ["order_no", "reason"],
            "properties": {
                "order_no": {"type": "string"},
                "reason": {"type": "string"},
                "priority": {"type": "string", "enum": ["P1", "P2"], "default": "P1"},
                "actor": {"type": "string", "default": "ai-ops-agent"},
                "dry_run": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "fix_product_category",
        "mode": "action",
        "description": "【受控动作】将存在分类异常的产品重新绑定到合法的叶子分类节点，写入治理覆盖层并可选立即重建本体。",
        "input_schema": {
            "type": "object",
            "required": ["sku", "target_leaf_category_code", "reviewer", "reason"],
            "properties": {
                "sku": {"type": "string"},
                "target_leaf_category_code": {"type": "string"},
                "reviewer": {"type": "string"},
                "reason": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
                "rebuild": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "reconcile_customer_identity",
        "mode": "action",
        "description": "【受控动作】人工审核并固化跨系统客户映射（写入 L3 Crosswalk）。强制拦截图谱已绑定冲突、不同信用代码法人与主体类型冲突。",
        "input_schema": {
            "type": "object",
            "required": ["crm_account_id", "erp_customer_no", "reviewer", "rationale"],
            "properties": {
                "crm_account_id": {"type": "string"},
                "erp_customer_no": {"type": "string"},
                "reviewer": {"type": "string"},
                "rationale": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
                "rebuild": {"type": "boolean", "default": False},
            },
        },
    },
]


class OntologyAgentToolkit:
    """AI Agent 统一工具调度器：屏蔽物理存储，强制走本体语义与动作守卫。"""

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
        self.engine = ActionEngine(
            db_path=self.db_path,
            ledger_path=ledger_path,
            crosswalk_path=crosswalk_path,
            overrides_path=overrides_path,
            schema_path=schema_path,
            graph_path=graph_path,
        )

    @staticmethod
    def export_tool_schemas() -> list[dict[str, Any]]:
        """导出可供 LLM / MCP Server 直接注册的标准 Tool Schema 列表。"""
        return TOOL_SCHEMAS

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """统一工具调用入口，始终返回结构化信封 {ok, tool, data/error}。"""
        args = arguments or {}
        try:
            data = self._dispatch(name, args)
            return {"ok": True, "tool": name, "data": data}
        except ActionError as exc:
            return {
                "ok": False,
                "tool": name,
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        except Exception as exc:
            return {
                "ok": False,
                "tool": name,
                "error_type": type(exc).__name__,
                "message": str(exc),
            }

    def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name == "find_vip_overdue_orders":
            with OntologyClient(self.db_path) as client:
                return client.query_cq1_vip_overdue_unshipped(
                    window_days=int(args.get("window_days", 30))
                )

        if name == "explain_order_overdue_status":
            with OntologyClient(self.db_path) as client:
                return client.explain_cq1_decision(
                    order_no=args["order_no"],
                    window_days=int(args.get("window_days", 30)),
                )

        if name == "get_customer_360":
            query = (args.get("customer_query") or "").strip().lower()
            with OntologyClient(self.db_path) as client:
                rows = client.query_cq2_customer_360()
            if not query:
                return rows
            matched = []
            for r in rows:
                haystack = " ".join(
                    [
                        r["customer_id"],
                        r["canonical_name"],
                        " ".join(r["aliases"]),
                        r["crm_account_id"] or "",
                        r["erp_customer_no"] or "",
                    ]
                ).lower()
                if query in haystack:
                    matched.append(r)
            return matched

        if name == "get_category_sales_and_audit":
            with OntologyClient(self.db_path) as client:
                return client.query_cq3_category_sales_and_audit()

        if name == "list_data_quality_issues":
            severity = args.get("severity")
            rule_code = args.get("rule_code")
            with OntologyClient(self.db_path) as client:
                # 若未指定过滤条件，返回审计总览；指定过滤条件则返回明细列表
                if not severity and not rule_code:
                    return client.query_cq4_data_quality_audit()
                sql = "SELECT * FROM data_quality_issues WHERE 1=1"
                params: list[Any] = []
                if severity:
                    sql += " AND severity = ?"
                    params.append(severity)
                if rule_code:
                    sql += " AND rule_code = ?"
                    params.append(rule_code)
                sql += " ORDER BY rule_code ASC, target_object_id ASC"
                return [dict(r) for r in client._conn.execute(sql, params).fetchall()]

        if name == "inspect_object_graph":
            with OntologyClient(self.db_path) as client:
                return client.get_object_neighborhood(
                    object_type=args["object_type"],
                    object_id=args["object_id"],
                )

        if name == "escalate_overdue_order":
            return self.engine.escalate_overdue_order(
                order_no=args["order_no"],
                reason=args["reason"],
                priority=args.get("priority", "P1"),
                actor=args.get("actor", "ai-ops-agent"),
                dry_run=bool(args.get("dry_run", False)),
                allow_duplicate=bool(args.get("allow_duplicate", False)),
            )

        if name == "fix_product_category":
            return self.engine.fix_product_category(
                sku=args["sku"],
                target_leaf_category_code=args["target_leaf_category_code"],
                reviewer=args["reviewer"],
                reason=args["reason"],
                dry_run=bool(args.get("dry_run", False)),
                rebuild=bool(args.get("rebuild", False)),
            )

        if name == "reconcile_customer_identity":
            return self.engine.reconcile_customer_identity(
                crm_account_id=args["crm_account_id"],
                erp_customer_no=args["erp_customer_no"],
                reviewer=args["reviewer"],
                rationale=args["rationale"],
                dry_run=bool(args.get("dry_run", False)),
                rebuild=bool(args.get("rebuild", False)),
            )

        raise ValueError(f"未注册的 Ontology 工具：{name}")
