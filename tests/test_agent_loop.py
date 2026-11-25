#!/usr/bin/env python3
"""Agent Loop 集成测试：覆盖 MCP 协议调用、守卫自愈、HITL 人工审批/驳回、重建闭环与 HTTP API。"""

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

from scripts.serve_console import ConsoleContext, create_console_server


class TestAgentLoopEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = ConsoleContext(mode="sandbox")

    def tearDown(self) -> None:
        self.ctx.close()

    def test_guard_self_heal_on_order_905(self) -> None:
        """误催办 SO-20261105-905 -> 前置守卫拦截 -> Agent 自动调用 explain + inspect 下钻查明根因。"""
        sess = self.ctx.agent_runner.start_run(
            task="ERP 里看到订单 SO-20261105-905 还是待发货，请立刻催办出库",
            engine_mode="deterministic",
            require_approval=True,
        )
        self.assertEqual(sess["status"], "COMPLETED")
        self.assertEqual(sess["metrics"]["guard_blocks"], 1)
        self.assertEqual(sess["metrics"]["actions_committed"], 0)
        self.assertGreaterEqual(sess["metrics"]["mcp_calls"], 3)
        self.assertIn("IN_TRANSIT", sess["final_answer"])
        self.assertIn("DQ-001", sess["final_answer"])
        self.assertEqual(self.ctx.toolkit().engine.read_ledger(), [])

    def test_vip_patrol_hitl_approve_and_reject(self) -> None:
        """VIP 履约巡检：先预演挂起 AWAITING_APPROVAL，批准后生成工单并落账；再次同任务可测驳回。"""
        # 1) 发起并挂起等待人工审批
        sess = self.ctx.agent_runner.start_run(
            task="巡检近 30 天 VIP 逾期订单并催办最高涉险订单",
            engine_mode="deterministic",
            require_approval=True,
        )
        self.assertEqual(sess["status"], "AWAITING_APPROVAL")
        self.assertIsNotNone(sess["pending_approval"])
        self.assertEqual(sess["pending_approval"]["tool_name"], "escalate_overdue_order")
        self.assertEqual(sess["pending_approval"]["tool_args"]["order_no"], "SO-20261112-904")
        # 挂起时未落账
        self.assertEqual(len(self.ctx.toolkit().engine.read_ledger()), 0)

        # 2) 人工批准并恢复执行
        resumed = self.ctx.agent_runner.resolve_approval(
            session_id=sess["session_id"], approved=True, reviewer_note="同意催办"
        )
        self.assertEqual(resumed["status"], "COMPLETED")
        self.assertEqual(resumed["metrics"]["actions_committed"], 1)
        self.assertIn("ESC-20261123-0001", resumed["final_answer"])
        self.assertEqual(len(self.ctx.toolkit().engine.read_ledger()), 1)

        # 3) 驳回分支：测另一张单 SO-20261103-901，挂起后驳回 -> 不落账
        s2 = self.ctx.agent_runner.start_run(
            task="请为订单 SO-20261103-901 发起催办",
            engine_mode="deterministic",
            require_approval=True,
        )
        self.assertEqual(s2["status"], "AWAITING_APPROVAL")
        s2_rej = self.ctx.agent_runner.resolve_approval(
            session_id=s2["session_id"], approved=False, reviewer_note="客户要求延后发"
        )
        self.assertEqual(s2_rej["status"], "COMPLETED")
        self.assertEqual(s2_rej["metrics"]["actions_committed"], 0)
        self.assertIn("驳回", s2_rej["final_answer"])
        self.assertEqual(len(self.ctx.toolkit().engine.read_ledger()), 1)

    def test_fix_product_p1012_hitl_closed_loop_rebuild(self) -> None:
        """修复 P-1012 -> 0101 闭环：HITL 批准后自动重建本体、异常数 38->37、01 办公设备 +54,417.15。"""
        sess = self.ctx.agent_runner.start_run(
            task="将未维护分类的商品 P-1012 修复到 0101 并重建本体",
            engine_mode="deterministic",
            require_approval=True,
        )
        self.assertEqual(sess["status"], "AWAITING_APPROVAL")
        self.assertEqual(sess["pending_approval"]["tool_name"], "fix_product_category")

        resumed = self.ctx.agent_runner.resolve_approval(sess["session_id"], approved=True)
        self.assertEqual(resumed["status"], "COMPLETED")
        self.assertEqual(resumed["metrics"]["actions_committed"], 1)
        self.assertIn("38 → 37", resumed["final_answer"])
        self.assertIn("+¥54,417.15", resumed["final_answer"])

        # 验证控制台总览与 W3C ABox 均已实时刷新
        ov = self.ctx.get_overview()
        self.assertEqual(ov["cq4"]["total_issues"], 37)

    def test_alias_due_diligence_and_bad_merge_guard(self) -> None:
        """测试旧名“博思咨询”尽调 + 信用代码冲突法人合并守卫拦截。"""
        s_alias = self.ctx.agent_runner.start_run(
            task="查一下客户“博思咨询”的 360 情况",
            engine_mode="deterministic",
        )
        self.assertEqual(s_alias["status"], "COMPLETED")
        self.assertIn("远东管理咨询有限公司", s_alias["final_answer"])
        self.assertIn("CREDIT_CODE", s_alias["final_answer"])

        s_merge = self.ctx.agent_runner.start_run(
            task="把 ACC-1011 和 C0011 合并对齐",
            engine_mode="deterministic",
        )
        self.assertEqual(s_merge["status"], "COMPLETED")
        self.assertEqual(s_merge["metrics"]["guard_blocks"], 1)
        self.assertIn("信用代码冲突", s_merge["final_answer"])


class TestAgentLoopHTTPAPI(unittest.TestCase):
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

    def _get_json(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post_json(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_agent_http_end_to_end_with_hitl_and_reset(self) -> None:
        presets = self._get_json("/api/agent/presets")
        self.assertEqual(len(presets["presets"]), 5)

        run_resp = self._post_json(
            "/api/agent/run",
            {
                "task": "修复 P-1012 到 0101 并重建本体",
                "engine_mode": "deterministic",
                "require_approval": True,
            },
        )
        self.assertTrue(run_resp["ok"])
        sess = run_resp["session"]
        self.assertEqual(sess["status"], "AWAITING_APPROVAL")

        approve_resp = self._post_json(
            "/api/agent/approve",
            {"session_id": sess["session_id"], "approved": True, "reviewer_note": "LGTM"},
        )
        self.assertTrue(approve_resp["ok"])
        self.assertEqual(approve_resp["session"]["status"], "COMPLETED")

        ov = self._get_json("/api/overview")
        self.assertEqual(ov["cq4"]["total_issues"], 37)

        # 重置沙箱同时清空 sessions 与恢复 38 条异常
        self._post_json("/api/sandbox/reset", {})
        ov2 = self._get_json("/api/overview")
        self.assertEqual(ov2["cq4"]["total_issues"], 38)
        self.assertEqual(self._get_json("/api/agent/sessions")["sessions"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
