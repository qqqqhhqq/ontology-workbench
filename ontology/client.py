#!/usr/bin/env python3
"""Ontology 统一语义查询与图遍历客户端（基于 data/ontology/ontology.db）。

提供面向业务概念的查询 API（而非裸源表拼接），直接回答 CQ1-CQ4，
同时提供对象详情与语义边邻域查询，供阶段四验收与阶段五 Action / AI Agent 复用。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ontology" / "ontology.db"


class OntologyClient:
    """面向本体的只读语义查询客户端。"""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"本体库不存在：{self.db_path}，请先运行 python3 scripts/build_ontology.py"
            )
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "OntologyClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 元数据与基础对象/图查询
    # ------------------------------------------------------------------

    def get_metadata(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM metadata").fetchall()
        return {r["key"]: r["value"] for r in rows}

    @property
    def as_of_date(self) -> date:
        return date.fromisoformat(self.get_metadata()["as_of_date"])

    def get_object_neighborhood(self, object_type: str, object_id: str) -> dict[str, Any]:
        """基于显式 links 边表查询任意本体对象的出边与入边，支持图谱下钻。"""
        outgoing = [
            dict(r)
            for r in self._conn.execute(
                """
                SELECT link_type, to_type AS neighbor_type, to_id AS neighbor_id
                FROM links
                WHERE from_type = ? AND from_id = ?
                ORDER BY link_type, to_id
                """,
                (object_type, object_id),
            ).fetchall()
        ]
        incoming = [
            dict(r)
            for r in self._conn.execute(
                """
                SELECT link_type, from_type AS neighbor_type, from_id AS neighbor_id
                FROM links
                WHERE to_type = ? AND to_id = ?
                ORDER BY link_type, from_id
                """,
                (object_type, object_id),
            ).fetchall()
        ]
        issues = [
            dict(r)
            for r in self._conn.execute(
                """
                SELECT issue_id, rule_code, rule_name, severity, description, raw_evidence
                FROM data_quality_issues
                WHERE target_object_type = ? AND target_object_id = ?
                ORDER BY issue_id
                """,
                (object_type, object_id),
            ).fetchall()
        ]
        return {
            "object_type": object_type,
            "object_id": object_id,
            "outgoing": outgoing,
            "incoming": incoming,
            "data_quality_issues": issues,
        }

    # ------------------------------------------------------------------
    # CQ1：高价值客户履约预警
    # ------------------------------------------------------------------

    def query_cq1_vip_overdue_unshipped(self, window_days: int = 30) -> list[dict[str, Any]]:
        """CQ1：近 window_days 天内下单的 VIP 客户逾期未有效发货订单及负责销售。"""
        as_of = self.as_of_date
        window_start = (as_of - timedelta(days=window_days - 1)).isoformat()

        rows = self._conn.execute(
            """
            SELECT
                o.order_no,
                c.customer_id,
                c.canonical_name,
                c.aliases_json,
                c.tier,
                c.resolution_method,
                s.owner_id,
                s.owner_name,
                s.team,
                o.order_date,
                o.promised_date,
                o.erp_status,
                o.fulfillment_status,
                o.total_amount
            FROM sales_orders o
            JOIN customers c ON o.placed_by_customer_id = c.customer_id
            LEFT JOIN sales_owners s ON c.owned_by_owner_id = s.owner_id
            WHERE c.is_vip = 1
              AND o.order_date >= ?
              AND o.is_overdue_unshipped = 1
            ORDER BY o.promised_date ASC, o.order_no ASC
            """,
            (window_start,),
        ).fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            promised = date.fromisoformat(r["promised_date"])
            results.append(
                {
                    "order_no": r["order_no"],
                    "customer_id": r["customer_id"],
                    "canonical_name": r["canonical_name"],
                    "aliases": json.loads(r["aliases_json"]),
                    "tier": r["tier"],
                    "resolution_method": r["resolution_method"],
                    "owner_id": r["owner_id"],
                    "owner_name": r["owner_name"],
                    "team": r["team"],
                    "order_date": r["order_date"],
                    "promised_date": r["promised_date"],
                    "overdue_days": (as_of - promised).days,
                    "erp_status": r["erp_status"],
                    "fulfillment_status": r["fulfillment_status"],
                    "total_amount": round(float(r["total_amount"]), 2),
                }
            )
        return results

    def explain_cq1_decision(self, order_no: str, window_days: int = 30) -> dict[str, Any]:
        """解释任意一张订单为何命中或未命中 CQ1（用于边界案例审计与可解释性）。"""
        as_of = self.as_of_date
        window_start = as_of - timedelta(days=window_days - 1)

        row = self._conn.execute(
            """
            SELECT o.*, c.canonical_name, c.tier, c.is_vip
            FROM sales_orders o
            JOIN customers c ON o.placed_by_customer_id = c.customer_id
            WHERE o.order_no = ?
            """,
            (order_no,),
        ).fetchone()
        if row is None:
            return {"order_no": order_no, "exists": False, "matched_cq1": False, "reasons": ["订单不存在"]}

        reasons: list[str] = []
        if not row["is_vip"]:
            reasons.append(f"非 VIP 客户（tier={row['tier']}）")
        if date.fromisoformat(row["order_date"]) < window_start:
            reasons.append(f"下单日 {row['order_date']} 早于近 {window_days} 天窗口起点 {window_start.isoformat()}")
        if row["is_cancelled"]:
            reasons.append("订单已取消（CANCELLED）")
        if row["promised_date"] is None:
            reasons.append("缺少承诺交付日（DQ-003 异常，无法判定逾期）")
        elif date.fromisoformat(row["promised_date"]) >= as_of:
            reasons.append(f"承诺日 {row['promised_date']} 未早于快照日 {as_of.isoformat()}（尚未逾期）")
        if row["is_effectively_shipped"]:
            reasons.append(
                f"WMS 已有效发货（fulfillment_status={row['fulfillment_status']}，"
                f"actual_ship_date={row['actual_ship_date']}；ERP={row['erp_status']}）"
            )

        return {
            "order_no": order_no,
            "customer_name": row["canonical_name"],
            "tier": row["tier"],
            "order_date": row["order_date"],
            "promised_date": row["promised_date"],
            "erp_status": row["erp_status"],
            "fulfillment_status": row["fulfillment_status"],
            "matched_cq1": len(reasons) == 0,
            "reasons": reasons if reasons else ["全部条件命中（VIP + 近30天 + 未取消 + 已过承诺日 + WMS无有效发货）"],
        }

    # ------------------------------------------------------------------
    # CQ2：跨系统客户 360° 全景视图
    # ------------------------------------------------------------------

    def query_cq2_customer_360(self) -> list[dict[str, Any]]:
        """CQ2：以真实世界 Customer 为统一锚点，汇总跨三系统档案、销售与履约指标。"""
        rows = self._conn.execute(
            """
            SELECT
                c.customer_id,
                c.canonical_name,
                c.aliases_json,
                c.party_type,
                c.tier,
                c.source_presence,
                c.resolution_method,
                c.crm_account_id,
                c.erp_customer_no,
                s.owner_name,
                s.team,
                COALESCE(SUM(CASE WHEN o.is_cancelled = 0 THEN 1 ELSE 0 END), 0) AS valid_order_count,
                COALESCE(SUM(CASE WHEN o.is_cancelled = 1 THEN 1 ELSE 0 END), 0) AS cancelled_order_count,
                COALESCE(SUM(CASE WHEN o.is_cancelled = 0 THEN o.total_amount ELSE 0 END), 0.0) AS valid_order_amount,
                COALESCE(SUM(CASE WHEN o.is_on_time IS NOT NULL THEN 1 ELSE 0 END), 0) AS evaluated_shipment_count,
                COALESCE(SUM(CASE WHEN o.is_on_time = 1 THEN 1 ELSE 0 END), 0) AS on_time_count,
                COALESCE(SUM(CASE WHEN o.is_overdue_unshipped = 1 THEN 1 ELSE 0 END), 0) AS overdue_unshipped_count
            FROM customers c
            LEFT JOIN sales_owners s ON c.owned_by_owner_id = s.owner_id
            LEFT JOIN sales_orders o ON o.placed_by_customer_id = c.customer_id
            GROUP BY c.customer_id
            ORDER BY valid_order_amount DESC, c.customer_id ASC
            """
        ).fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            evaluated = int(r["evaluated_shipment_count"])
            on_time = int(r["on_time_count"])
            on_time_rate = round(on_time / evaluated * 100, 1) if evaluated > 0 else None
            results.append(
                {
                    "customer_id": r["customer_id"],
                    "canonical_name": r["canonical_name"],
                    "aliases": json.loads(r["aliases_json"]),
                    "party_type": r["party_type"],
                    "tier": r["tier"],
                    "source_presence": r["source_presence"],
                    "resolution_method": r["resolution_method"],
                    "crm_account_id": r["crm_account_id"],
                    "erp_customer_no": r["erp_customer_no"],
                    "owner_name": r["owner_name"],
                    "team": r["team"],
                    "valid_order_count": int(r["valid_order_count"]),
                    "cancelled_order_count": int(r["cancelled_order_count"]),
                    "valid_order_amount": round(float(r["valid_order_amount"]), 2),
                    "evaluated_shipment_count": evaluated,
                    "on_time_count": on_time,
                    "on_time_rate_pct": on_time_rate,
                    "overdue_unshipped_count": int(r["overdue_unshipped_count"]),
                }
            )
        return results

    # ------------------------------------------------------------------
    # CQ3：按产品大类聚合销售与分类合规审计
    # ------------------------------------------------------------------

    def query_cq3_category_sales_and_audit(self) -> dict[str, Any]:
        """CQ3：沿分类树上卷统计一级大类有效销售额，并暴露全部异常分类商品。"""
        cat_rows = self._conn.execute(
            """
            SELECT
                COALESCE(pc.root_category_code, 'UNCLASSIFIED') AS root_category_code,
                COALESCE(pc.root_category_name, '未归类/异常待核') AS root_category_name,
                SUM(CASE WHEN p.category_status = 'VALID' THEN ol.line_amount ELSE 0 END) AS valid_leaf_amount,
                SUM(CASE WHEN p.category_status = 'NON_LEAF' THEN ol.line_amount ELSE 0 END) AS non_leaf_amount,
                SUM(CASE WHEN p.category_status NOT IN ('VALID', 'NON_LEAF') THEN ol.line_amount ELSE 0 END) AS anomalous_amount,
                SUM(ol.line_amount) AS total_amount,
                COUNT(DISTINCT ol.order_no) AS order_count,
                COUNT(ol.order_line_id) AS line_count
            FROM order_lines ol
            JOIN sales_orders o ON ol.order_no = o.order_no
            JOIN products p ON ol.sku = p.sku
            LEFT JOIN product_categories pc ON p.belongs_to_category_code = pc.category_code
            WHERE o.is_cancelled = 0
            GROUP BY COALESCE(pc.root_category_code, 'UNCLASSIFIED'),
                     COALESCE(pc.root_category_name, '未归类/异常待核')
            ORDER BY root_category_code ASC
            """
        ).fetchall()

        anomalous_products = self._conn.execute(
            """
            SELECT
                p.sku,
                p.product_name,
                p.category_status,
                p.raw_category_code,
                p.raw_category_name,
                p.belongs_to_category_code,
                pc.root_category_name AS inferred_root_category,
                COALESCE(SUM(CASE WHEN o.is_cancelled = 0 THEN ol.line_amount ELSE 0 END), 0.0) AS valid_sales_amount,
                COALESCE(SUM(CASE WHEN o.is_cancelled = 0 THEN 1 ELSE 0 END), 0) AS valid_line_count
            FROM products p
            LEFT JOIN product_categories pc ON p.belongs_to_category_code = pc.category_code
            LEFT JOIN order_lines ol ON ol.sku = p.sku
            LEFT JOIN sales_orders o ON ol.order_no = o.order_no
            WHERE p.category_status != 'VALID'
            GROUP BY p.sku
            ORDER BY p.sku ASC
            """
        ).fetchall()

        total_valid_orders_amount = self._conn.execute(
            "SELECT COALESCE(SUM(total_amount), 0.0) FROM sales_orders WHERE is_cancelled = 0"
        ).fetchone()[0]

        categories_list = [
            {
                "root_category_code": r["root_category_code"],
                "root_category_name": r["root_category_name"],
                "valid_leaf_amount": round(float(r["valid_leaf_amount"]), 2),
                "non_leaf_amount": round(float(r["non_leaf_amount"]), 2),
                "anomalous_amount": round(float(r["anomalous_amount"]), 2),
                "total_amount": round(float(r["total_amount"]), 2),
                "order_count": int(r["order_count"]),
                "line_count": int(r["line_count"]),
            }
            for r in cat_rows
        ]

        reconciled_sum = sum(Decimal(f"{c['total_amount']:.2f}") for c in categories_list)
        expected_sum = Decimal(f"{float(total_valid_orders_amount):.2f}")

        return {
            "root_category_summary": categories_list,
            "anomalous_products": [
                {
                    "sku": r["sku"],
                    "product_name": r["product_name"],
                    "category_status": r["category_status"],
                    "raw_category_code": r["raw_category_code"],
                    "raw_category_name": r["raw_category_name"],
                    "belongs_to_category_code": r["belongs_to_category_code"],
                    "inferred_root_category": r["inferred_root_category"],
                    "valid_sales_amount": round(float(r["valid_sales_amount"]), 2),
                    "valid_line_count": int(r["valid_line_count"]),
                }
                for r in anomalous_products
            ],
            "reconciliation": {
                "category_sum": float(reconciled_sum),
                "valid_orders_total": float(expected_sum),
                "is_balanced": reconciled_sum == expected_sum,
            },
        }

    # ------------------------------------------------------------------
    # CQ4：跨系统数据一致性审计
    # ------------------------------------------------------------------

    def query_cq4_data_quality_audit(self) -> dict[str, Any]:
        """CQ4：按严重级别与规则码统计全部 DataQualityIssue，并返回 HIGH 级异常明细。"""
        by_severity = [
            dict(r)
            for r in self._conn.execute(
                """
                SELECT severity, COUNT(*) AS issue_count
                FROM data_quality_issues
                GROUP BY severity
                ORDER BY CASE severity WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END
                """
            ).fetchall()
        ]
        by_rule = [
            dict(r)
            for r in self._conn.execute(
                """
                SELECT rule_code, rule_name, severity, COUNT(*) AS issue_count
                FROM data_quality_issues
                GROUP BY rule_code, rule_name, severity
                ORDER BY rule_code ASC
                """
            ).fetchall()
        ]
        high_issues = [
            dict(r)
            for r in self._conn.execute(
                """
                SELECT issue_id, rule_code, rule_name, target_object_type,
                       target_object_id, source_system, description, raw_evidence
                FROM data_quality_issues
                WHERE severity = 'HIGH'
                ORDER BY rule_code ASC, target_object_id ASC
                """
            ).fetchall()
        ]
        total = sum(int(r["issue_count"]) for r in by_severity)
        return {
            "total_issues": total,
            "by_severity": by_severity,
            "by_rule": by_rule,
            "high_severity_details": high_issues,
        }
