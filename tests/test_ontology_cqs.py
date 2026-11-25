#!/usr/bin/env python3
"""阶段四自动化验收测试：全面校验 Ontology 约束、CQ1-CQ4 及全部人为埋点边界。"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.client import OntologyClient
from scripts.run_cq_report import run_naive_cq1_experiments

DB_PATH = ROOT_DIR / "data" / "ontology" / "ontology.db"
GRAPH_PATH = ROOT_DIR / "data" / "ontology" / "ontology_graph.json"


class TestOntologyVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = OntologyClient(DB_PATH)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    # ------------------------------------------------------------------
    # 0. 存储完整性与图谱一致性校验
    # ------------------------------------------------------------------

    def test_sqlite_foreign_keys_and_graph_consistency(self) -> None:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(fk_errors, [], f"SQLite 外键校验失败：{fk_errors}")

            graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
            table_map = {
                "SalesOwner": "sales_owners",
                "Customer": "customers",
                "ProductCategory": "product_categories",
                "Product": "products",
                "SalesOrder": "sales_orders",
                "OrderLine": "order_lines",
                "Shipment": "shipments",
                "DataQualityIssue": "data_quality_issues",
            }
            for obj_type, table_name in table_map.items():
                db_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                self.assertEqual(
                    db_count,
                    len(graph["objects"][obj_type]),
                    f"SQLite 表 {table_name} 与 JSON 图谱 {obj_type} 数量不一致",
                )
            link_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            self.assertEqual(link_count, len(graph["links"]))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 1. CQ1：近 30 天 VIP 逾期未发货 + 全部边界陷阱验证
    # ------------------------------------------------------------------

    def test_cq1_exact_hits_and_owners(self) -> None:
        hits = self.client.query_cq1_vip_overdue_unshipped()
        by_no = {r["order_no"]: r for r in hits}

        expected_nos = {
            "SO-20261103-901",
            "SO-20261106-903",
            "SO-20261110-902",
            "SO-20261112-904",
        }
        self.assertEqual(set(by_no.keys()), expected_nos)

        # 903：WMS 仅为“待出库”，仍属于未有效发货
        self.assertEqual(by_no["SO-20261106-903"]["fulfillment_status"], "AWAITING_DISPATCH")
        self.assertEqual(by_no["SO-20261106-903"]["owner_name"], "王磊")

        # 904：L3 人工映射表对齐后的 VIP 客户星辰创意设计有限公司 -> 周敏 / 华东二组
        self.assertEqual(by_no["SO-20261112-904"]["canonical_name"], "星辰创意设计有限公司")
        self.assertEqual(by_no["SO-20261112-904"]["owner_name"], "周敏")
        self.assertEqual(by_no["SO-20261112-904"]["team"], "华东二组")
        self.assertEqual(by_no["SO-20261112-904"]["resolution_method"], "MANUAL_CROSSWALK")

    def test_cq1_excludes_all_planted_traps_with_clear_reasons(self) -> None:
        traps = {
            "SO-20261105-905": "WMS 已有效发货",    # 双陷阱：ERP 滞留 A + WMS 单号无连字符
            "SO-20261108-906": "订单已取消",         # 已取消
            "SO-20261118-907": "尚未逾期",           # 承诺日在未来
            "SO-20261010-908": "早于近 30 天窗口",   # 超过 30 天
            "SO-20261109-909": "tier=KEY_ACCOUNT",  # 大客户 != VIP
            "SO-20261111-910": "缺少承诺交付日",     # 缺承诺日
            "SO-20261114-911": "tier=STANDARD",     # 普通个人客户
        }
        for order_no, expected_substr in traps.items():
            exp = self.client.explain_cq1_decision(order_no)
            self.assertFalse(exp["matched_cq1"], f"{order_no} 不应命中 CQ1")
            joined = " | ".join(exp["reasons"])
            self.assertIn(expected_substr, joined, f"{order_no} 排除原因不符合预期：{joined}")

    def test_naive_csv_queries_fail_where_ontology_succeeds(self) -> None:
        naive = run_naive_cq1_experiments()
        onto_hits = sorted(r["order_no"] for r in self.client.query_cq1_vip_overdue_unshipped())
        self.assertEqual(naive["naive_by_name"], [])
        self.assertNotEqual(naive["naive_by_credit_and_raw_wms"], onto_hits)
        self.assertIn("SO-20261105-905", naive["naive_by_credit_and_raw_wms"])  # 误报
        self.assertNotIn("SO-20261106-903", naive["naive_by_credit_and_raw_wms"])  # 漏报
        self.assertNotIn("SO-20261112-904", naive["naive_by_credit_and_raw_wms"])  # 漏报

    # ------------------------------------------------------------------
    # 2. CQ2：客户 360° 全景视图与实体解析校验
    # ------------------------------------------------------------------

    def test_cq2_customer_360_entity_resolution_cases(self) -> None:
        rows = self.client.query_cq2_customer_360()
        self.assertEqual(len(rows), 14)

        by_erp = {r["erp_customer_no"]: r for r in rows if r["erp_customer_no"]}
        by_crm = {r["crm_account_id"]: r for r in rows if r["crm_account_id"]}

        # 更名企业通过信用代码自动拉通
        renamed = by_erp["C0009"]
        self.assertEqual(renamed["crm_account_id"], "ACC-1009")
        self.assertEqual(renamed["canonical_name"], "远东管理咨询有限公司")
        self.assertEqual(renamed["aliases"], ["博思咨询"])
        self.assertEqual(renamed["resolution_method"], "CREDIT_CODE")

        # 名称高度相似的企业严格隔离
        self.assertNotEqual(by_erp["C0001"]["customer_id"], by_erp["C0008"]["customer_id"])
        self.assertEqual(by_erp["C0001"]["tier"], "VIP")
        self.assertEqual(by_erp["C0008"]["tier"], "STANDARD")

        # 单边系统客户正确保留
        self.assertEqual(by_erp["C0011"]["source_presence"], "ERP_ONLY")
        self.assertGreater(by_erp["C0011"]["valid_order_count"], 0)
        self.assertEqual(by_crm["ACC-1011"]["source_presence"], "CRM_ONLY")
        self.assertEqual(by_crm["ACC-1011"]["valid_order_count"], 0)

    # ------------------------------------------------------------------
    # 3. CQ3：产品分类树传递闭包聚合与金额守恒校验
    # ------------------------------------------------------------------

    def test_cq3_category_sales_reconciliation_and_anomalies(self) -> None:
        cq3 = self.client.query_cq3_category_sales_and_audit()
        self.assertTrue(cq3["reconciliation"]["is_balanced"])
        self.assertGreater(cq3["reconciliation"]["valid_orders_total"], 0)

        by_root = {c["root_category_code"]: c for c in cq3["root_category_summary"]}
        self.assertEqual(set(by_root.keys()), {"01", "02", "UNCLASSIFIED"})
        # 01 办公设备包含 P-1015 的非叶子上卷金额
        self.assertGreater(by_root["01"]["valid_leaf_amount"], 0)
        self.assertGreater(by_root["01"]["non_leaf_amount"], 0)
        self.assertEqual(by_root["02"]["non_leaf_amount"], 0.0)
        self.assertGreater(by_root["UNCLASSIFIED"]["anomalous_amount"], 0)

        anomalies = {p["sku"]: p for p in cq3["anomalous_products"]}
        self.assertEqual(
            {sku: p["category_status"] for sku, p in anomalies.items()},
            {
                "P-1012": "MISSING",
                "P-1013": "CONFLICT",
                "P-1014": "UNKNOWN_CODE",
                "P-1015": "NON_LEAF",
            },
        )
        self.assertEqual(anomalies["P-1015"]["inferred_root_category"], "办公设备")
        self.assertIsNone(anomalies["P-1013"]["inferred_root_category"])

    # ------------------------------------------------------------------
    # 4. CQ4：数据一致性审计（12 条规则全量覆盖）与图邻域验证
    # ------------------------------------------------------------------

    def test_cq4_all_12_dq_rules_and_key_evidence(self) -> None:
        cq4 = self.client.query_cq4_data_quality_audit()
        by_rule = {r["rule_code"]: r["issue_count"] for r in cq4["by_rule"]}

        expected_counts = {
            "DQ-001": 5,
            "DQ-002": 1,
            "DQ-003": 1,
            "DQ-004": 18,
            "DQ-005": 1,
            "DQ-006": 1,
            "DQ-007": 1,
            "DQ-008": 1,
            "DQ-009": 1,
            "DQ-010": 1,
            "DQ-011": 3,
            "DQ-012": 4,
        }
        self.assertEqual(by_rule, expected_counts)
        self.assertEqual(cq4["total_issues"], 38)

        high_targets = {
            (h["rule_code"], h["target_object_id"])
            for h in cq4["high_severity_details"]
        }
        self.assertIn(("DQ-001", "SO-20261105-905"), high_targets)
        self.assertIn(("DQ-002", "DN-20261118-901"), high_targets)
        self.assertIn(("DQ-003", "SO-20261111-910"), high_targets)
        self.assertIn(("DQ-007", "P-1012"), high_targets)
        self.assertIn(("DQ-008", "P-1013"), high_targets)
        self.assertIn(("DQ-009", "P-1014"), high_targets)

    def test_graph_neighborhood_drilldown(self) -> None:
        nb = self.client.get_object_neighborhood("SalesOrder", "SO-20261105-905")
        out_edges = {(e["link_type"], e["neighbor_type"], e["neighbor_id"]) for e in nb["outgoing"]}
        in_edges = {(e["link_type"], e["neighbor_type"], e["neighbor_id"]) for e in nb["incoming"]}

        self.assertIn(("placed_by", "Customer", "CUST-0001"), out_edges)
        self.assertIn(("part_of_order", "OrderLine", "SO-20261105-905#1"), in_edges)
        self.assertIn(("fulfills", "Shipment", "DN-20261120-001"), in_edges)
        self.assertEqual([i["rule_code"] for i in nb["data_quality_issues"]], ["DQ-001"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
