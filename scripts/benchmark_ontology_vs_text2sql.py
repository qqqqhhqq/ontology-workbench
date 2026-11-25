#!/usr/bin/env python3
"""使用当前配置的真实大模型（如 Qwen3.8），端到端实跑 Ontology vs Text-to-SQL 4 题同场对比评测。

产出：
  - 终端详细实测对比摘要
  - data/ontology/arena_benchmark_result.json（完整真实 SQL、MCP 调用、评分、Token 与耗时记录）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.agent_loop import OntologyAgentLoopRunner
from ontology.text2sql_arena import ARENA_BENCHMARKS, run_head_to_head_arena
from scripts.serve_console import ConsoleContext

RESULT_PATH = ROOT_DIR / "data" / "ontology" / "arena_benchmark_result.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Ontology vs Text-to-SQL 真实大模型对比评测")
    parser.add_argument("--only", default=None, help="仅重跑指定题目 ID（如 bench_action_905）并合并进历史结果")
    args = parser.parse_args()

    existing_by_id: dict[str, dict] = {}
    if args.only and RESULT_PATH.exists():
        old = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        for b in old.get("benchmarks", []):
            existing_by_id[b["benchmark_id"]] = b

    targets = [b for b in ARENA_BENCHMARKS if not args.only or b["id"] == args.only]
    results_by_id = dict(existing_by_id)
    print("=" * 80)
    print("实跑对比评测：Text-to-SQL（10张原始源表） vs Operational Ontology（9个MCP工具）")
    print("=" * 80)

    for idx, bench in enumerate(targets, start=1):
        print(f"\n[{idx}/{len(targets)}] {bench['title']} ({bench['tag']})")
        ctx = ConsoleContext(mode="sandbox")
        try:
            def runner_factory() -> OntologyAgentLoopRunner:
                return OntologyAgentLoopRunner(
                    mcp_server_factory=ctx.mcp_server,
                    local_config_path=ctx.llm_config_path,
                )

            res = run_head_to_head_arena(
                question=bench["question"],
                agent_runner_factory=runner_factory,
                benchmark_id=bench["id"],
            )
            results_by_id[bench["id"]] = res

            t2s = res["text_to_sql"]
            onto = res["ontology"]
            t_ev = t2s["evaluation"]
            o_ev = onto["evaluation"]

            print(
                f"  Text-to-SQL : 得分 {t_ev['passed']}/{t_ev['total']} ({t_ev['score_pct']}%) | "
                f"轮次={t2s['metrics']['iterations']} | SQL数={len(t2s['sql_queries'])} | "
                f"Tokens={t2s['metrics']['total_tokens']} | 耗时={t2s['metrics']['llm_latency_ms']}ms"
            )
            for d in t_ev["details"]:
                print(f"    {'✅' if d['passed'] else '❌'} {d['label']} -> {d['note']}")

            print(
                f"  Ontology    : 得分 {o_ev['passed']}/{o_ev['total']} ({o_ev['score_pct']}%) | "
                f"轮次={onto['metrics']['iterations']} | MCP调用={onto['metrics']['mcp_calls']} | "
                f"Tokens={onto['metrics']['total_tokens']} | 耗时={onto['metrics']['llm_latency_ms']}ms"
            )
            for d in o_ev["details"]:
                print(f"    {'✅' if d['passed'] else '❌'} {d['label']} -> {d['note']}")
        finally:
            ctx.close()

    results = [results_by_id[b["id"]] for b in ARENA_BENCHMARKS if b["id"] in results_by_id]

    # 汇总总分
    t_pass = sum(r["text_to_sql"]["evaluation"]["passed"] for r in results)
    t_tot = sum(r["text_to_sql"]["evaluation"]["total"] for r in results)
    o_pass = sum(r["ontology"]["evaluation"]["passed"] for r in results)
    o_tot = sum(r["ontology"]["evaluation"]["total"] for r in results)
    t_tok = sum(r["text_to_sql"]["metrics"]["total_tokens"] for r in results)
    o_tok = sum(r["ontology"]["metrics"]["total_tokens"] for r in results)
    t_ms = sum(r["text_to_sql"]["metrics"]["llm_latency_ms"] for r in results)
    o_ms = sum(r["ontology"]["metrics"]["llm_latency_ms"] for r in results)

    summary = {
        "model": results[0]["model"] if results else "",
        "text_to_sql_score": f"{t_pass}/{t_tot} ({round(t_pass / t_tot * 100)}%)",
        "ontology_score": f"{o_pass}/{o_tot} ({round(o_pass / o_tot * 100)}%)",
        "text_to_sql_total_tokens": t_tok,
        "ontology_total_tokens": o_tok,
        "text_to_sql_total_latency_ms": t_ms,
        "ontology_total_latency_ms": o_ms,
        "benchmarks": results,
    }
    RESULT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n" + "=" * 80)
    print(f"总计对比 ({summary['model']}):")
    print(f"  - Text-to-SQL : 命中 {summary['text_to_sql_score']} | 总Tokens={t_tok} | 总耗时={t_ms}ms")
    print(f"  - Ontology    : 命中 {summary['ontology_score']} | 总Tokens={o_tok} | 总耗时={o_ms}ms")
    print(f"完整实测 JSON 已保存：{RESULT_PATH.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
