#!/usr/bin/env python3
"""三大扩展标准集成测试：MCP Server (stdio) + 交互式控制台 API + W3C (OWL/SHACL/RDF/SPARQL)。"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.client import OntologyClient
from ontology.w3c.rdf_store import RDFTripleStore
from scripts.serve_console import create_console_server


class TestW3CStandards(unittest.TestCase):
    def test_w3c_turtle_parsing_shacl_and_cq_reconciliation(self) -> None:
        w3c_dir = ROOT_DIR / "data" / "w3c"
        tbox = w3c_dir / "ontology_tbox.ttl"
        shacl = w3c_dir / "shapes_shacl.ttl"
        abox = w3c_dir / "instances_abox.ttl"

        tbox_store = RDFTripleStore()
        self.assertGreater(tbox_store.parse_file(tbox), 400)

        shacl_store = RDFTripleStore()
        self.assertGreater(shacl_store.parse_file(shacl), 90)

        full_store = RDFTripleStore()
        full_store.parse_file(tbox)
        self.assertGreater(full_store.parse_file(abox), 7000)

        # SHACL 验证结果必须与高/中/低质量规则精确一致
        report = full_store.validate_shacl_rules()
        self.assertFalse(report["conforms"])
        self.assertEqual(report["by_severity"]["sh:Violation"], 10)
        self.assertEqual(report["by_severity"]["sh:Warning"], 19)
        self.assertEqual(report["by_severity"]["sh:Info"], 4)

        # RDF 三元组计算的 CQ1 & CQ3 必须与 SQLite OntologyClient 100% 一致
        with OntologyClient() as client:
            sql_cq1 = [r["order_no"] for r in client.query_cq1_vip_overdue_unshipped()]
            sql_cq3 = {
                c["root_category_code"]: c["total_amount"]
                for c in client.query_cq3_category_sales_and_audit()["root_category_summary"]
            }

        rdf_cq1 = [r["order_no"] for r in full_store.verify_cq1_on_rdf()]
        rdf_cq3 = full_store.verify_cq3_on_rdf()
        self.assertEqual(rdf_cq1, sql_cq1)
        self.assertEqual(rdf_cq3, sql_cq3)

    def test_sparql_query_files_present_and_well_formed(self) -> None:
        qdir = ROOT_DIR / "ontology" / "w3c" / "queries"
        for fname in [
            "cq1_vip_overdue.rq",
            "cq2_customer_360.rq",
            "cq3_category_sales.rq",
            "cq4_dq_audit.rq",
        ]:
            text = (qdir / fname).read_text(encoding="utf-8")
            self.assertIn("PREFIX b2b:", text)
            self.assertIn("SELECT ", text)
        self.assertIn("b2b:parentCategory*", (qdir / "cq3_category_sales.rq").read_text(encoding="utf-8"))


class TestMCPServerStdio(unittest.TestCase):
    def test_mcp_full_stdio_lifecycle(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_server.server", "--mode", "sandbox"],
            cwd=str(ROOT_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None

        def rpc(method: str, params: dict | None = None, req_id: int | None = 1) -> dict | None:
            msg: dict = {"jsonrpc": "2.0", "method": method}
            if req_id is not None:
                msg["id"] = req_id
            if params is not None:
                msg["params"] = params
            proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            if req_id is None:
                return None
            line = proc.stdout.readline()
            self.assertTrue(line, f"MCP Server 未返回响应（method={method}）")
            return json.loads(line)

        try:
            # 1. initialize + initialized notification + ping
            init_resp = rpc(
                "initialize",
                {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
                req_id=1,
            )
            assert init_resp is not None
            self.assertEqual(init_resp["result"]["serverInfo"]["name"], "b2b-fulfillment-ontology")
            self.assertIn("tools", init_resp["result"]["capabilities"])
            rpc("notifications/initialized", req_id=None)
            self.assertEqual(rpc("ping", req_id=2)["result"], {})  # type: ignore[index]

            # 2. tools/list
            tools_resp = rpc("tools/list", req_id=3)
            assert tools_resp is not None
            tools = tools_resp["result"]["tools"]
            self.assertEqual(len(tools), 9)
            tool_by_name = {t["name"]: t for t in tools}
            self.assertTrue(tool_by_name["find_vip_overdue_orders"]["annotations"]["readOnlyHint"])
            self.assertFalse(tool_by_name["escalate_overdue_order"]["annotations"]["readOnlyHint"])
            self.assertIn("inputSchema", tool_by_name["escalate_overdue_order"])

            # 3. tools/call (read CQ1)
            cq1_resp = rpc(
                "tools/call",
                {"name": "find_vip_overdue_orders", "arguments": {"window_days": 30}},
                req_id=4,
            )
            assert cq1_resp is not None
            self.assertFalse(cq1_resp["result"]["isError"])
            payload = json.loads(cq1_resp["result"]["content"][0]["text"])
            self.assertEqual(len(payload["data"]), 4)

            # 4. tools/call (guard rejection on SO-20261105-905)
            bad_resp = rpc(
                "tools/call",
                {
                    "name": "escalate_overdue_order",
                    "arguments": {"order_no": "SO-20261105-905", "reason": "误催办测试"},
                },
                req_id=5,
            )
            assert bad_resp is not None
            self.assertTrue(bad_resp["result"]["isError"])

            # 5. tools/call (valid escalate in sandbox)
            ok_resp = rpc(
                "tools/call",
                {
                    "name": "escalate_overdue_order",
                    "arguments": {"order_no": "SO-20261112-904", "reason": "VIP逾期催办"},
                },
                req_id=6,
            )
            assert ok_resp is not None
            self.assertFalse(ok_resp["result"]["isError"])

            # 6. resources/list & resources/read
            res_list = rpc("resources/list", req_id=7)
            assert res_list is not None
            self.assertEqual(len(res_list["result"]["resources"]), 8)
            tbox_read = rpc("resources/read", {"uri": "ontology://w3c/tbox"}, req_id=8)
            assert tbox_read is not None
            self.assertIn("owl:Ontology", tbox_read["result"]["contents"][0]["text"])

            # 7. prompts/list & prompts/get
            prompts = rpc("prompts/list", req_id=9)
            assert prompts is not None
            self.assertEqual(len(prompts["result"]["prompts"]), 2)
            p_get = rpc("prompts/get", {"name": "vip_fulfillment_patrol", "arguments": {"window_days": 30}}, req_id=10)
            assert p_get is not None
            self.assertIn("find_vip_overdue_orders", p_get["result"]["messages"][0]["content"]["text"])
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()


class TestInteractiveConsoleAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.ctx = create_console_server(host="127.0.0.1", port=0, mode="sandbox")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.ctx.close()

    def _get(self, path: str) -> tuple[int, bytes]:
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.read()

    def _post_json(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_static_assets_and_read_endpoints(self) -> None:
        for asset in ["/", "/styles.css", "/app.js"]:
            status, body = self._get(asset)
            self.assertEqual(status, 200)
            self.assertGreater(len(body), 500)

        _, raw_ov = self._get("/api/overview")
        ov = json.loads(raw_ov.decode("utf-8"))
        self.assertEqual(ov["mode"], "sandbox")
        self.assertEqual(len(ov["cq1"]), 4)
        self.assertEqual(ov["cq4"]["total_issues"], 38)

        for preset in ["core", "vip_overdue", "customer_er", "product_tree", "dq_conflicts"]:
            _, raw_g = self._get(f"/api/graph?preset={preset}")
            g = json.loads(raw_g.decode("utf-8"))
            self.assertGreater(g["node_count"], 5)
            self.assertGreater(g["edge_count"], 4)

        _, raw_node = self._get("/api/node?type=SalesOrder&id=SO-20261105-905")
        node = json.loads(raw_node.decode("utf-8"))
        self.assertEqual(node["properties"]["fulfillment_status"], "IN_TRANSIT")
        self.assertFalse(node["cq1_explain"]["matched_cq1"])

        _, raw_alias = self._get("/api/customers?q=" + urllib.parse.quote("博思咨询"))
        alias = json.loads(raw_alias.decode("utf-8"))
        self.assertEqual(alias["data"][0]["canonical_name"], "远东管理咨询有限公司")

        _, raw_w3c = self._get("/api/w3c/preview?asset=cq3")
        self.assertIn("b2b:parentCategory*", json.loads(raw_w3c.decode("utf-8"))["content"])

    def test_console_actions_rebuild_and_sandbox_reset(self) -> None:
        # 守卫拦截
        bad = self._post_json(
            "/api/actions/escalate_overdue_order",
            {"order_no": "SO-20261105-905", "reason": "误催办"},
        )
        self.assertFalse(bad["ok"])

        # 合法修复 + 重建本体
        fix = self._post_json(
            "/api/actions/fix_product_category",
            {
                "sku": "P-1012",
                "target_leaf_category_code": "0101",
                "reviewer": "console-tester",
                "reason": "修复到打印机叶子分类",
                "rebuild": True,
            },
        )
        self.assertTrue(fix["ok"])
        self.assertTrue(fix["data"]["side_effects"]["rebuilt_ontology"])

        _, raw_ov = self._get("/api/overview")
        ov_after = json.loads(raw_ov.decode("utf-8"))
        self.assertEqual(ov_after["cq4"]["total_issues"], 37)
        self.assertEqual(len(ov_after["ledger"]), 1)

        # 一键重置沙箱恢复到 38 条异常与 0 条账本
        reset = self._post_json("/api/sandbox/reset")
        self.assertTrue(reset["ok"])
        _, raw_ov_reset = self._get("/api/overview")
        ov_reset = json.loads(raw_ov_reset.decode("utf-8"))
        self.assertEqual(ov_reset["cq4"]["total_issues"], 38)
        self.assertEqual(len(ov_reset["ledger"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
