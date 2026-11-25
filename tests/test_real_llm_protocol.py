#!/usr/bin/env python3
"""真实 LLM HTTP 协议与配置管理集成测试（本地真实 HTTP OpenAI & Anthropic 协议桩）。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.llm_config import load_llm_config, save_llm_config
from ontology.llm_providers import ping_llm
from scripts.serve_console import ConsoleContext, create_console_server


def _start_mock_llm_http_server() -> tuple[ThreadingHTTPServer, dict[str, Any]]:
    """启动本地真实 HTTP 协议服务器，验证 /v1/chat/completions 与 /v1/messages 真实请求载荷。"""
    state: dict[str, Any] = {"openai_requests": [], "anthropic_requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _reply(self, code: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8"))

            if self.path == "/v1/chat/completions":
                auth = self.headers.get("Authorization", "")
                state["openai_requests"].append({"auth": auth, "body": body})
                if auth != "Bearer sk-test-real-9999":
                    self._reply(401, {"error": {"message": "Invalid API Key"}})
                    return

                # 探活请求（无 tools）
                if not body.get("tools"):
                    self._reply(
                        200,
                        {
                            "model": body["model"],
                            "choices": [{"message": {"role": "assistant", "content": "PONG"}}],
                            "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
                        },
                    )
                    return

                # Round 1: 同时返回 2 个 tool_calls（1 个读 + 1 个写），测试多工具并发 + HITL 中断恢复！
                tool_msgs = [m for m in body["messages"] if m.get("role") == "tool"]
                if not tool_msgs:
                    self._reply(
                        200,
                        {
                            "model": body["model"],
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "reasoning_content": "先查 CQ1 核实逾期单，同时准备为最高金额单 SO-20261112-904 发起催办。",
                                        "content": "",
                                        "tool_calls": [
                                            {
                                                "id": "call_cq1_001",
                                                "type": "function",
                                                "function": {
                                                    "name": "find_vip_overdue_orders",
                                                    "arguments": json.dumps({"window_days": 30}),
                                                },
                                            },
                                            {
                                                "id": "call_esc_002",
                                                "type": "function",
                                                "function": {
                                                    "name": "escalate_overdue_order",
                                                    "arguments": json.dumps(
                                                        {
                                                            "order_no": "SO-20261112-904",
                                                            "reason": "真实模型测试催办",
                                                            "priority": "P1",
                                                        }
                                                    ),
                                                },
                                            },
                                        ],
                                    }
                                }
                            ],
                            "usage": {"prompt_tokens": 320, "completion_tokens": 68, "total_tokens": 388},
                        },
                    )
                    return

                # Round 2: 验证两个 tool_call_id 都按 OpenAI 规范完整回传
                self._reply(
                    200,
                    {
                        "model": body["model"],
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": f"真实模型已完成闭环：收到 {len(tool_msgs)} 条工具回报，已生成工单 ESC-20261123-0001。",
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 510, "completion_tokens": 42, "total_tokens": 552},
                    },
                )
                return

            if self.path == "/v1/messages":
                api_key = self.headers.get("x-api-key", "")
                ver = self.headers.get("anthropic-version", "")
                state["anthropic_requests"].append({"api_key": api_key, "version": ver, "body": body})
                if api_key != "sk-ant-test-8888":
                    self._reply(401, {"error": {"message": "Invalid Anthropic Key"}})
                    return

                if not body.get("tools"):
                    self._reply(
                        200,
                        {
                            "model": body["model"],
                            "content": [{"type": "text", "text": "PONG"}],
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        },
                    )
                    return

                has_tool_res = any(
                    isinstance(m.get("content"), list)
                    and any(b.get("type") == "tool_result" for b in m["content"])
                    for m in body["messages"]
                )
                if not has_tool_res:
                    self._reply(
                        200,
                        {
                            "model": body["model"],
                            "content": [
                                {"type": "thinking", "thinking": "先通过别名检索博思咨询的 360 档案"},
                                {
                                    "type": "tool_use",
                                    "id": "toolu_001",
                                    "name": "get_customer_360",
                                    "input": {"customer_query": "博思咨询"},
                                },
                            ],
                            "usage": {"input_tokens": 280, "output_tokens": 45},
                        },
                    )
                else:
                    self._reply(
                        200,
                        {
                            "model": body["model"],
                            "content": [{"type": "text", "text": "Anthropic 模型确认：博思咨询已更名为远东管理咨询有限公司。"}],
                            "usage": {"input_tokens": 420, "output_tokens": 30},
                        },
                    )
                return

            self._reply(404, {"error": f"Unknown path {self.path}"})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


class TestLLMConfigAndReservedKey(unittest.TestCase):
    def test_reserved_empty_key_fails_cleanly_without_faking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "llm.local.yaml"
            save_llm_config(
                {
                    "provider": "openai_compatible",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini",
                    "clear_api_key": True,
                },
                local_config_path=cfg_path,
            )
            cfg = load_llm_config(local_config_path=cfg_path, dotenv_path=Path(tmp) / ".env")
            self.assertFalse(cfg.is_ready())
            self.assertEqual(cfg.api_key, "")

            # real_llm 模式下绝不伪造模拟答案，清晰返回 FAILED 配置指引
            ctx = ConsoleContext(mode="sandbox", llm_config_path=cfg_path)
            try:
                sess = ctx.agent_runner.start_run(task="巡检 VIP 逾期订单", engine_mode="real_llm")
                self.assertEqual(sess["status"], "FAILED")
                self.assertIn("预留", sess["steps"][0]["title"])
            finally:
                ctx.close()

    def test_save_mask_preserve_and_clear_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "llm.local.yaml"
            saved = save_llm_config(
                {
                    "provider": "openai_compatible",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "api_key": "sk-test-secret-9999",
                },
                local_config_path=cfg_path,
            )
            pub = saved.to_public_dict()
            self.assertTrue(pub["has_api_key"])
            self.assertTrue(pub["is_ready"])
            self.assertEqual(pub["api_key_masked"], "sk-t****9999")
            self.assertNotIn("secret", json.dumps(pub))

            # 更新模型名时未传 api_key，应自动保留原密钥
            updated = save_llm_config({"model": "deepseek-reasoner"}, local_config_path=cfg_path)
            self.assertEqual(updated.model, "deepseek-reasoner")
            self.assertEqual(updated.api_key, "sk-test-secret-9999")

            # 显式清除密钥
            cleared = save_llm_config({"clear_api_key": True}, local_config_path=cfg_path)
            self.assertEqual(cleared.api_key, "")
            self.assertFalse(cleared.is_ready())


class TestRealHTTPLLMLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mock_srv, cls.mock_state = _start_mock_llm_http_server()
        cls.mock_base = f"http://127.0.0.1:{cls.mock_srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mock_srv.shutdown()
        cls.mock_srv.server_close()

    def test_openai_compatible_multi_tool_call_with_hitl_resume_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "llm.local.yaml"
            save_llm_config(
                {
                    "provider": "openai_compatible",
                    "base_url": f"{self.mock_base}/v1",
                    "model": "deepseek-reasoner",
                    "api_key": "sk-test-real-9999",
                },
                local_config_path=cfg_path,
            )

            # 1) 连通性探活
            ping_res = ping_llm(load_llm_config(local_config_path=cfg_path, dotenv_path=Path(tmp) / ".env"))
            self.assertTrue(ping_res["ok"])
            self.assertEqual(ping_res["reply"], "PONG")

            # 2) 多工具并发 + HITL 挂起与恢复完整闭环
            ctx = ConsoleContext(mode="sandbox", llm_config_path=cfg_path)
            try:
                sess = ctx.agent_runner.start_run(
                    task="查 VIP 逾期并催办 SO-20261112-904",
                    engine_mode="real_llm",
                    require_approval=True,
                )
                self.assertEqual(sess["status"], "AWAITING_APPROVAL")
                self.assertEqual(sess["pending_approval"]["tool_name"], "escalate_overdue_order")
                self.assertEqual(sess["metrics"]["total_tokens"], 388)
                # 批准继续
                resumed = ctx.agent_runner.resolve_approval(sess["session_id"], approved=True, reviewer_note="OK")
                self.assertEqual(resumed["status"], "COMPLETED")
                self.assertEqual(resumed["metrics"]["actions_committed"], 1)
                self.assertEqual(resumed["metrics"]["total_tokens"], 388 + 552)
                self.assertIn("ESC-20261123-0001", resumed["final_answer"])

                # 校验发送给 OpenAI 端点的第 2 轮请求包含全部 9 个工具且完整回传了 2 个 tool_call_id
                last_req = self.mock_state["openai_requests"][-1]["body"]
                self.assertEqual(len(last_req["tools"]), 9)
                tool_ids = [m["tool_call_id"] for m in last_req["messages"] if m.get("role") == "tool"]
                self.assertEqual(tool_ids, ["call_cq1_001", "call_esc_002"])
            finally:
                ctx.close()

    def test_anthropic_messages_protocol_and_console_config_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "llm.local.yaml"
            console_srv, console_ctx = create_console_server(
                host="127.0.0.1", port=0, mode="sandbox", llm_config_path=cfg_path
            )
            base = f"http://127.0.0.1:{console_srv.server_address[1]}"
            threading.Thread(target=console_srv.serve_forever, daemon=True).start()
            try:
                def post(path: str, payload: dict) -> dict:
                    req = urllib.request.Request(
                        base + path,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req) as r:
                        return json.loads(r.read().decode("utf-8"))

                # 通过控制台 API 保存 Anthropic 配置并校验掩码
                saved = post(
                    "/api/agent/config",
                    {
                        "provider": "anthropic",
                        "base_url": self.mock_base,
                        "model": "claude-sonnet-4-20250514",
                        "api_key": "sk-ant-test-8888",
                    },
                )
                self.assertTrue(saved["ok"])
                self.assertEqual(saved["config"]["api_key_masked"], "sk-a****8888")

                # 通过控制台 API 测试连通性
                ping = post("/api/agent/ping", {})
                self.assertTrue(ping["ok"])
                self.assertEqual(ping["reply"], "PONG")

                # 通过控制台 API 跑真实 Anthropic Function Calling Loop
                run = post(
                    "/api/agent/run",
                    {"task": "查博思咨询", "engine_mode": "real_llm", "require_approval": True},
                )
                self.assertTrue(run["ok"])
                self.assertEqual(run["session"]["status"], "COMPLETED")
                self.assertIn("远东管理咨询有限公司", run["session"]["final_answer"])
                self.assertEqual(run["session"]["metrics"]["total_tokens"], (280 + 45) + (420 + 30))
            finally:
                console_srv.shutdown()
                console_srv.server_close()
                console_ctx.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
