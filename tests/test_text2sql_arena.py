#!/usr/bin/env python3
"""Ontology vs Text-to-SQL Arena 单元测试（只读 SQL 安全、脏数据陷阱复现、评分器与 API）。"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.text2sql_arena import (
    RawSQLiteSandbox,
    evaluate_answer_against_checks,
)
from scripts.serve_console import create_console_server


class TestRawSQLiteSandboxAndEvaluator(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = RawSQLiteSandbox()

    def tearDown(self) -> None:
        self.sb.close()

    def test_readonly_sql_security_guards(self) -> None:
        for bad_sql in [
            "DROP TABLE erp_sales_orders",
            "DELETE FROM crm_accounts",
            "UPDATE erp_sales_orders SET status_code='D'",
            "INSERT INTO crm_owners VALUES ('X','Y','Z')",
            "PRAGMA table_info(crm_accounts)",
            "SELECT 1; DROP TABLE crm_accounts",
            "",
        ]:
            res = self.sb.execute_readonly_sql(bad_sql)
            self.assertFalse(res["ok"], f"危险 SQL 应被拦截：{bad_sql}")

    def test_proves_silent_comma_amount_truncation_in_raw_sql(self) -> None:
        """实证：对原始表裸执行 SUM(CAST(amount AS REAL)) 会静默把 '12,800.00' 截断成 12.0，少算 ¥12,788！"""
        res = self.sb.execute_readonly_sql(
            """
            SELECT ROUND(SUM(CAST(oi.amount AS REAL)), 2) AS raw_sum
            FROM erp_order_items oi
            JOIN erp_sales_orders so ON so.order_no = oi.order_no
            WHERE so.status_code <> 'X'
            """
        )
        self.assertTrue(res["ok"])
        raw_sum = res["rows"][0]["raw_sum"]
        # 真实金额为 5872306.60，裸 SQL 静默少算 12788.00
        self.assertAlmostEqual(raw_sum, 5859518.60, places=2)
        self.assertAlmostEqual(5872306.60 - raw_sum, 12788.00, places=2)

    def test_evaluator_grades_cq1_correctly(self) -> None:
        good_answer = (
            "命中 4 张 VIP 逾期单：SO-20261103-901、SO-20261110-902、SO-20261106-903、SO-20261112-904。"
            "已排除 SO-20261105-905（WMS 已出库）与 SO-20261109-909（大客户非 VIP）。"
        )
        ev = evaluate_answer_against_checks("bench_cq1", good_answer, [], "ontology")
        self.assertEqual(ev["passed"], 6)
        self.assertEqual(ev["total"], 6)
        self.assertEqual(ev["score_pct"], 100)


class TestArenaHTTPAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.ctx = create_console_server(host="127.0.0.1", port=0, mode="sandbox")
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.ctx.close()

    def test_arena_benchmarks_endpoint(self) -> None:
        with urllib.request.urlopen(self.base + "/api/arena/benchmarks") as r:
            data = json.loads(r.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["benchmarks"]), 4)
        self.assertIn("CREATE TABLE wms_shipments", data["raw_ddl"])
        self.assertIsNotNone(data["cached_report"])
        self.assertEqual(data["cached_report"]["ontology_score"], "16/16 (100%)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
