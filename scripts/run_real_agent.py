#!/usr/bin/env python3
"""命令行真实 Agent + 真实大模型调试工具（直连 MCP + Ontology 守卫 + 终端 HITL 审批）。

配置来源（优先级由高到低）：
  1. 命令行参数 --provider / --base-url / --model / --api-key
  2. 环境变量或 .env 文件（LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY）
  3. config/llm.local.yaml（已加入 .gitignore）

常用示例：
  # 1) 查看当前配置状态（安全掩码显示）
  python3 scripts/run_real_agent.py --show-config

  # 2) 测试真实模型连通性
  python3 scripts/run_real_agent.py --ping

  # 3) 用真实模型运行完整 Agent Loop（默认 sandbox 沙箱 + 终端 y/n 审批写动作）
  python3 scripts/run_real_agent.py "请巡检近30天VIP逾期订单，并查清 SO-20261105-905 为何不能催办"

  # 4) 临时覆盖模型参数并免审批自动演练
  python3 scripts/run_real_agent.py --no-approval --model deepseek-chat "修复 P-1012 到 0101 并重建本体"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.llm_config import load_llm_config
from ontology.llm_providers import ping_llm
from scripts.serve_console import ConsoleContext


def _print_new_steps(steps: list[dict[str, Any]], printed_count: int) -> int:
    for s in steps[printed_count:]:
        tag = s["kind"].upper()
        dur = f" ({s['duration_ms']}ms)" if s.get("duration_ms") else ""
        print(f"\n[#{s['step_no']} {tag}]{dur} {s['title']}")
        print(f"  {s['content']}")
        if s.get("tool_args"):
            print(f"  args: {json.dumps(s['tool_args'], ensure_ascii=False)}")
    return len(steps)


def main() -> None:
    parser = argparse.ArgumentParser(description="真实 LLM + Operational Ontology Agent Loop CLI")
    parser.add_argument("task", nargs="?", help="自然语言业务任务")
    parser.add_argument("--mode", choices=["sandbox", "live"], default="sandbox")
    parser.add_argument("--provider", choices=["openai_compatible", "anthropic", "gemini"], default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--no-approval", action="store_true", help="关闭 HITL 人工审批，自动执行通过守卫的写动作")
    parser.add_argument("--show-config", action="store_true", help="查看当前合并后的模型配置（掩码显示密钥）")
    parser.add_argument("--ping", action="store_true", help="向当前配置的真实模型发探活请求")
    args = parser.parse_args()

    override = {
        k: v
        for k, v in {
            "provider": args.provider,
            "base_url": args.base_url,
            "model": args.model,
            "api_key": args.api_key,
        }.items()
        if v is not None
    }
    cfg = load_llm_config(override=override)

    if args.show_config:
        print(json.dumps(cfg.to_public_dict(), ensure_ascii=False, indent=2))
        return

    if args.ping:
        res = ping_llm(cfg)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(0 if res.get("ok") else 1)

    if not args.task:
        parser.error("请提供自然语言任务，或使用 --show-config / --ping")

    ctx = ConsoleContext(mode=args.mode)
    try:
        print(f"=== 启动真实 Agent Loop ({cfg.provider} / {cfg.model} @ {cfg.base_url} | 模式={args.mode}) ===")
        sess = ctx.agent_runner.start_run(
            task=args.task,
            engine_mode="real_llm",
            require_approval=not args.no_approval,
            llm_config=override,
        )
        printed = _print_new_steps(sess["steps"], 0)

        while sess["status"] == "AWAITING_APPROVAL" and sess.get("pending_approval"):
            ap = sess["pending_approval"]
            print("\n" + "=" * 70)
            print(f"[HITL 审批闸门] 动作：{ap['tool_name']}")
            print(f"摘要：{ap['summary']}")
            print(f"Dry Run 预演快照：{json.dumps(ap['dry_run_preview'], ensure_ascii=False, indent=2)}")
            ans = input("是否批准执行该受控动作？[y/N]: ").strip().lower()
            approved = ans in {"y", "yes"}
            note = input("可选审批备注（直接回车跳过）: ").strip()
            sess = ctx.agent_runner.resolve_approval(
                session_id=sess["session_id"],
                approved=approved,
                reviewer_note=note,
            )
            printed = _print_new_steps(sess["steps"], printed)

        m = sess["metrics"]
        print("\n" + "=" * 70)
        print(
            f"Session {sess['session_id']} 结束：status={sess['status']} | "
            f"轮次={m['iterations']} | MCP调用={m['mcp_calls']} | 守卫拦截={m['guard_blocks']} | "
            f"已提交动作={m['actions_committed']} | Tokens={m['total_tokens']} "
            f"(in={m['prompt_tokens']}, out={m['completion_tokens']}) | LLM耗时={m['llm_latency_ms']}ms"
        )
    finally:
        ctx.close()


if __name__ == "__main__":
    main()
