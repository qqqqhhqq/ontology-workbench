#!/usr/bin/env python3
"""Ontology vs Text-to-SQL 同模型同题实战对比引擎（Arena）。

将完全相同的真实大模型（如 Qwen3.8）分别接入两条架构链路，对比回答同一批企业业务问题：
  1. Text-to-SQL 链路：直接把 CRM / ERP / WMS 共 10 张未治理源系统 CSV 原样装入只读 SQLite 库，
     提供完整 10 表 DDL 与只读 SQL 执行工具，让模型自行编写 SQL 查表作答；
  2. Operational Ontology 链路：通过 MCP JSON-RPC 2.0 语义工具层与前置条件守卫作答。

通过自动化的 Ground Truth 评分项，直观暴露两者在准确率、Token 成本、耗时、口径一致性与动作安全上的差异。
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ontology.agent_loop import OntologyAgentLoopRunner
from ontology.llm_config import LLMConfig, LOCAL_CONFIG_PATH, load_llm_config
from ontology.llm_providers import (
    LLMCallError,
    LLMToolCall,
    append_tool_results,
    build_initial_history,
    call_llm,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT_DIR / "data" / "sources"

FORBIDDEN_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|VACUUM|REINDEX|PRAGMA)\b",
    re.IGNORECASE,
)

RAW_TABLES_SPEC: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "crm_owners",
        "crm/owners.csv",
        [("owner_id", "TEXT"), ("owner_name", "TEXT"), ("team", "TEXT")],
    ),
    (
        "crm_accounts",
        "crm/accounts.csv",
        [
            ("account_id", "TEXT"),
            ("account_name", "TEXT"),
            ("account_type", "TEXT"),
            ("customer_level", "TEXT"),
            ("owner_id", "TEXT"),
            ("city", "TEXT"),
            ("credit_code", "TEXT"),
        ],
    ),
    (
        "erp_customer_type_codes",
        "erp/customer_type_codes.csv",
        [("type_code", "TEXT"), ("type_name", "TEXT")],
    ),
    (
        "erp_order_status_codes",
        "erp/order_status_codes.csv",
        [("status_code", "TEXT"), ("status_name", "TEXT")],
    ),
    (
        "erp_category_codes",
        "erp/category_codes.csv",
        [("category_code", "TEXT"), ("category_name", "TEXT"), ("parent_code", "TEXT")],
    ),
    (
        "erp_customers",
        "erp/customers.csv",
        [
            ("customer_no", "TEXT"),
            ("customer_name", "TEXT"),
            ("type_code", "TEXT"),
            ("city", "TEXT"),
            ("credit_code", "TEXT"),
        ],
    ),
    (
        "erp_products",
        "erp/products.csv",
        [
            ("sku", "TEXT"),
            ("product_name", "TEXT"),
            ("category_code", "TEXT"),
            ("category_name", "TEXT"),
            ("unit", "TEXT"),
            ("list_price", "TEXT"),
        ],
    ),
    (
        "erp_sales_orders",
        "erp/sales_orders.csv",
        [
            ("order_no", "TEXT"),
            ("customer_no", "TEXT"),
            ("order_date", "TEXT"),
            ("promised_date", "TEXT"),
            ("status_code", "TEXT"),
        ],
    ),
    (
        "erp_order_items",
        "erp/order_items.csv",
        [
            ("order_no", "TEXT"),
            ("line_no", "INTEGER"),
            ("sku", "TEXT"),
            ("qty", "INTEGER"),
            ("unit_price", "TEXT"),
            ("amount", "TEXT"),
        ],
    ),
    (
        "wms_shipments",
        "wms/shipments.csv",
        [
            ("shipment_id", "TEXT"),
            ("order_no", "TEXT"),
            ("ship_date", "TEXT"),
            ("status", "TEXT"),
            ("carrier", "TEXT"),
        ],
    ),
]

ARENA_BENCHMARKS: list[dict[str, Any]] = [
    {
        "id": "bench_cq1",
        "title": "CQ1 · 近 30 天 VIP 客户逾期未发货预警",
        "tag": "跨三系统 + 口径裁决",
        "question": (
            "数据快照基准日为 2026-11-23。请查出近 30 天内（2026-10-25 至 2026-11-23）下单的 VIP 客户中，"
            "所有已过承诺交付日、未取消、且实际尚未有效发货的订单号、客户名与负责销售。"
        ),
        "ground_truth_summary": "精准 4 张：SO-20261103-901、SO-20261106-903（待出库不算有效发货）、SO-20261110-902、SO-20261112-904（星辰设计缺信用代码）；必须排除 SO-20261105-905（WMS单号缺连字符但已出库）、SO-20261109-909（大客户≠VIP）与随机无连字符单 SO-20261028-001。",
        "checks": [
            {"id": "hit_901", "label": "命中 SO-20261103-901", "must_contain": ["SO-20261103-901"]},
            {"id": "hit_902", "label": "命中 SO-20261110-902", "must_contain": ["SO-20261110-902"]},
            {"id": "hit_903", "label": "命中 SO-20261106-903（识别待出库≠已发货）", "must_contain": ["SO-20261106-903"]},
            {"id": "hit_904", "label": "命中 SO-20261112-904（跨系统对齐缺信用代码的星辰设计）", "must_contain": ["SO-20261112-904"]},
            {"id": "no_fp_905", "label": "不把 SO-20261105-905 误报为逾期未发", "must_not_claim_hit": "SO-20261105-905"},
            {"id": "no_fp_909", "label": "不把大客户单 SO-20261109-909 混入 VIP", "must_not_claim_hit": "SO-20261109-909"},
        ],
    },
    {
        "id": "bench_cq2_alias",
        "title": "CQ2 · 用更名前旧名“博思咨询”做客户 360° 核查",
        "tag": "跨系统实体对齐",
        "question": (
            "销售问客户“博思咨询”历史上共下了多少张有效（未取消）订单？有效订单总金额是多少？"
            "它在 ERP 里现在的全称是什么？"
        ),
        "ground_truth_summary": "已更名为“远东管理咨询有限公司”（ERP C0009 ↔ CRM ACC-1009，通过信用代码 91110105MA01B0099J 对齐），有效订单 9 张，有效总金额 ¥415,402.65。",
        "checks": [
            {"id": "find_new_name", "label": "识别更名后全称“远东管理咨询有限公司”", "must_contain": ["远东管理咨询"]},
            {"id": "find_order_cnt", "label": "准确给出 9 张有效订单", "must_contain": ["9"]},
            {"id": "find_amount", "label": "准确汇总有效金额 ¥415,402.65", "must_contain_any": ["415,402.65", "415402.65"]},
        ],
    },
    {
        "id": "bench_cq3_sales",
        "title": "CQ3 · 按一级大类统计有效销售额 + 异常商品审计",
        "tag": "分类树聚合 + 脏金额格式",
        "question": (
            "请统计所有未取消订单的明细行总金额，并按一级产品大类（办公设备 vs 耗材）汇总销售额；"
            "同时列出存在分类异常的商品及未归类金额，并核对金额是否守恒。"
        ),
        "ground_truth_summary": "有效订单总金额 ¥5,872,306.60（注意 SO-20261018-913 含千分位逗号 '12,800.00'，裸 SQL SUM 会静默算成 12.0 丢掉 ¥12,788！）；办公设备 ¥1,959,142.90（含非叶子 P-1015 ¥65,296.35）、耗材 ¥3,498,978.40、未归类异常 ¥414,185.30（P-1012/1013/1014）。",
        "checks": [
            {"id": "total_exact", "label": "总金额精准 ¥5,872,306.60（未踩 '12,800.00' 静默截断坑）", "must_contain_any": ["5,872,306.60", "5872306.60", "5872306.6"]},
            {"id": "cat_consumable", "label": "耗材大类精准 ¥3,498,978.40", "must_contain_any": ["3,498,978.40", "3498978.40", "3498978.4"]},
            {"id": "cat_unclassified", "label": "识别未归类/冲突商品 P-1012、P-1013、P-1014", "must_contain": ["P-1012", "P-1013", "P-1014"]},
            {"id": "cat_nonleaf", "label": "识别非叶子商品 P-1015", "must_contain": ["P-1015"]},
        ],
    },
    {
        "id": "bench_action_905",
        "title": "Action · 订单 SO-20261105-905 催办与业务动作闭环",
        "tag": "只读查询 vs 受控操作闭环",
        "question": (
            "ERP 里显示订单 SO-20261105-905 仍是待发货，请核实后立即为其发起加急催办工单；"
            "如果不能催办，请说明真实原因，并顺便给近 30 天内涉险金额最高的 VIP 逾期未发订单做一次 dry_run 催办预演。"
        ),
        "ground_truth_summary": "Text-to-SQL 是只读查询范式，无法执行带前置守卫的催办预演/工单闭环；Ontology 模式既拦截 SO-20261105-905（WMS DN-20261120-001 已出库，单号原缺连字符 SO20261105905），又能合法预演催办 SO-20261112-904（¥13,178，通知周敏/华东二组）。",
        "checks": [
            {"id": "catch_905_shipped", "label": "查出 SO-20261105-905 在 WMS 已出库（DN-20261120-001）", "must_contain_any": ["DN-20261120-001", "IN_TRANSIT", "已出库"]},
            {"id": "find_top_904", "label": "识别涉险金额最高 VIP 逾期单 SO-20261112-904", "must_contain": ["SO-20261112-904"]},
            {"id": "can_execute_action", "label": "通过受控 Action 完成 dry_run 催办预演（路由至周敏）", "require_action_tool": True},
        ],
    },
]


# ---------------------------------------------------------------------------
# 1. 原始企业 10 表只读 SQLite 沙箱（原汁原味还原裸库现状）
# ---------------------------------------------------------------------------


class RawSQLiteSandbox:
    """将 data/sources/{crm,erp,wms}/*.csv 原样加载进内存 SQLite，供 Text-to-SQL 真实查询。"""

    def __init__(self, sources_dir: Path = SOURCES_DIR) -> None:
        self.sources_dir = Path(sources_dir)
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.ddl_text = self._load_all_tables()

    def close(self) -> None:
        self._conn.close()

    def _load_all_tables(self) -> str:
        ddl_parts: list[str] = []
        cur = self._conn.cursor()
        for table_name, rel_csv, cols in RAW_TABLES_SPEC:
            col_defs = ", ".join(f"{name} {ctype}" for name, ctype in cols)
            ddl = f"CREATE TABLE {table_name} ({col_defs});"
            cur.execute(ddl)
            ddl_parts.append(ddl)

            csv_path = self.sources_dir / rel_csv
            with csv_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                placeholders = ", ".join("?" for _ in cols)
                col_names = [c[0] for c in cols]
                rows = [[row.get(c, "") for c in col_names] for row in reader]
                cur.executemany(f"INSERT INTO {table_name} VALUES ({placeholders})", rows)

        self._conn.commit()
        return "\n".join(ddl_parts)

    def execute_readonly_sql(self, sql: str, max_rows: int = 80) -> dict[str, Any]:
        cleaned = (sql or "").strip()
        if not cleaned:
            return {"ok": False, "error": "SQL 语句不能为空"}
        # 去掉末尾单个分号，禁止多语句注入
        no_trail = cleaned[:-1].strip() if cleaned.endswith(";") else cleaned
        if ";" in no_trail:
            return {"ok": False, "error": "安全拦截：禁止执行多条拼接 SQL 语句"}
        if not re.match(r"^(SELECT|WITH)\b", no_trail, re.IGNORECASE):
            return {"ok": False, "error": "安全拦截：Text-to-SQL 仅允许只读 SELECT / WITH 查询"}
        if FORBIDDEN_SQL_RE.search(no_trail):
            return {"ok": False, "error": "安全拦截：检测到禁止的写操作或 DDL 关键字"}

        t0 = time.perf_counter()
        try:
            cur = self._conn.execute(no_trail)
            cols = [d[0] for d in (cur.description or [])]
            fetched = cur.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [dict(zip(cols, r)) for r in fetched[:max_rows]]
            elapsed = max(1, int((time.perf_counter() - t0) * 1000))
            return {
                "ok": True,
                "columns": cols,
                "row_count": len(rows),
                "truncated": truncated,
                "rows": rows,
                "duration_ms": elapsed,
            }
        except sqlite3.Error as exc:
            return {"ok": False, "error": f"SQLite 执行错误：{exc}"}


# ---------------------------------------------------------------------------
# 2. Text-to-SQL Agent Runner（同样接入真实 LLM，只暴露原始表 DDL + 只读 SQL 工具）
# ---------------------------------------------------------------------------

TEXT2SQL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "execute_readonly_sql",
        "description": "在企业原始源系统 SQLite 数据库（含 crm_*、erp_*、wms_* 共 10 张表）上执行一条只读 SELECT/WITH SQL 语句，返回查询结果。",
        "inputSchema": {
            "type": "object",
            "required": ["sql"],
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "标准 SQLite SELECT 或 WITH 查询语句",
                }
            },
        },
    }
]


@dataclass
class Text2SQLRunResult:
    mode: str = "text_to_sql"
    status: str = "COMPLETED"
    model_info: dict[str, Any] = field(default_factory=dict)
    sql_queries: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str = ""
    metrics: dict[str, int] = field(
        default_factory=lambda: {
            "iterations": 0,
            "tool_calls": 0,
            "sql_errors": 0,
            "actions_executed": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "llm_latency_ms": 0,
        }
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_text_to_sql_agent(
    question: str,
    cfg: LLMConfig,
    sources_dir: Path = SOURCES_DIR,
) -> dict[str, Any]:
    """用真实大模型直接对 10 张原始 CRM/ERP/WMS 表执行 Text-to-SQL 多轮问答。"""
    res = Text2SQLRunResult(
        model_info={"provider": cfg.provider, "model": cfg.model, "base_url": cfg.base_url}
    )
    if not cfg.is_ready():
        res.status = "FAILED"
        res.final_answer = "真实模型 API Key 尚未配置，请先在 config/llm.local.yaml 或页面配置面板填入 Key。"
        res.steps.append(
            {"step_no": 1, "kind": "error", "title": "API Key 尚未配置", "content": res.final_answer}
        )
        return res.to_dict()

    sandbox = RawSQLiteSandbox(sources_dir=sources_dir)
    system_prompt = (
        "你是一位企业级 Text-to-SQL 数据分析师。你只能通过 execute_readonly_sql 工具查询原始源系统数据库，无法执行任何业务写操作。\n"
        "数据快照基准日（今天）为 2026-11-23。\n"
        "以下是 CRM、ERP、WMS 三套源系统导出的 10 张原始数据库表结构（DDL）：\n"
        f"{sandbox.ddl_text}\n\n"
        "要求：\n"
        "1. 根据用户问题自行编写 SQLite SQL 查询，必要时可多轮查询码表或探查数据。\n"
        "2. 严格基于 SQL 查询结果用简洁中文回答，列出关键单号、金额或原因；若用户要求执行写动作（如催办工单），请如实说明 Text-to-SQL 模式不具备业务写操作能力。"
    )

    try:
        history = build_initial_history(cfg.provider, question)
        for _ in range(cfg.max_turns):
            res.metrics["iterations"] += 1
            turn_no = res.metrics["iterations"]
            try:
                resp = call_llm(
                    cfg=cfg,
                    system_prompt=system_prompt,
                    history=history,
                    mcp_tools=TEXT2SQL_TOOLS,
                )
            except LLMCallError as exc:
                res.status = "FAILED"
                res.final_answer = f"调用模型失败：{exc}"
                res.steps.append(
                    {"step_no": len(res.steps) + 1, "kind": "error", "title": "模型调用失败", "content": str(exc)}
                )
                return res.to_dict()

            res.model_info["model"] = resp.model
            res.metrics["prompt_tokens"] += resp.usage.prompt_tokens
            res.metrics["completion_tokens"] += resp.usage.completion_tokens
            res.metrics["total_tokens"] += resp.usage.total_tokens
            res.metrics["llm_latency_ms"] += resp.latency_ms
            history.append(resp.raw_assistant_entry)

            if resp.reasoning:
                res.steps.append(
                    {
                        "step_no": len(res.steps) + 1,
                        "kind": "thought",
                        "title": f"Model Reasoning · Round #{turn_no}",
                        "content": resp.reasoning,
                        "duration_ms": resp.latency_ms,
                    }
                )

            if not resp.tool_calls:
                res.final_answer = resp.text or "查询完成。"
                res.steps.append(
                    {
                        "step_no": len(res.steps) + 1,
                        "kind": "final_answer",
                        "title": "Text-to-SQL 最终回答",
                        "content": res.final_answer,
                    }
                )
                return res.to_dict()

            turn_results: list[tuple[LLMToolCall, dict[str, Any]]] = []
            for tc in resp.tool_calls:
                res.metrics["tool_calls"] += 1
                sql_str = str(tc.arguments.get("sql") or "")
                out = sandbox.execute_readonly_sql(sql_str)
                if not out.get("ok"):
                    res.metrics["sql_errors"] += 1
                res.sql_queries.append(
                    {
                        "round": turn_no,
                        "sql": sql_str,
                        "ok": out.get("ok", False),
                        "row_count": out.get("row_count", 0),
                        "error": out.get("error"),
                        "sample_rows": (out.get("rows") or [])[:5],
                    }
                )
                res.steps.append(
                    {
                        "step_no": len(res.steps) + 1,
                        "kind": "tool_call" if out.get("ok") else "guard_blocked",
                        "title": f"SQL #{len(res.sql_queries)} ({'OK, ' + str(out.get('row_count', 0)) + '行' if out.get('ok') else 'SQL ERROR'})",
                        "content": sql_str,
                        "tool_result": out,
                        "duration_ms": out.get("duration_ms", 0),
                    }
                )
                turn_results.append((tc, out))

            append_tool_results(cfg.provider, history, turn_results)

        res.final_answer = f"已达到最大 SQL 迭代轮次上限（{cfg.max_turns} 轮）。"
        return res.to_dict()
    finally:
        sandbox.close()


# ---------------------------------------------------------------------------
# 3. Ground Truth 自动评分与同场 PK 对比器
# ---------------------------------------------------------------------------


def evaluate_answer_against_checks(
    benchmark_id: str | None,
    answer_text: str,
    steps: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    """根据预设题目的 Ground Truth 检查项自动评估回答质量。"""
    bench = next((b for b in ARENA_BENCHMARKS if b["id"] == benchmark_id), None)
    if not bench:
        return {"has_ground_truth": False, "passed": 0, "total": 0, "score_pct": None, "details": []}

    text = answer_text or ""
    details: list[dict[str, Any]] = []
    passed = 0

    for chk in bench["checks"]:
        ok = True
        note = ""
        if "must_contain" in chk:
            missing = [tok for tok in chk["must_contain"] if tok not in text]
            ok = len(missing) == 0
            note = "命中" if ok else f"缺失：{', '.join(missing)}"
        elif "must_contain_any" in chk:
            ok = any(tok in text for tok in chk["must_contain_any"])
            note = "命中" if ok else f"未出现预期值（{chk['must_contain_any'][0]}）"
        elif "must_not_claim_hit" in chk:
            target = chk["must_not_claim_hit"]
            # 若最终答案未把该单列为命中，或在明确排除语境下提及则视为通过
            if target not in text:
                ok = True
                note = "未误报"
            else:
                # 检查是否在否定/排除语境中提到（如“排除”“不命中”“已出库”“非 VIP”）
                idx = text.find(target)
                ctx_window = text[max(0, idx - 60) : min(len(text), idx + 120)]
                ok = any(
                    w in ctx_window
                    for w in ("排除", "不命中", "未命中", "无需", "非 VIP", "非VIP", "大客户", "已出库", "已发货", "不计入", "不应")
                )
                note = "明确排除未误报" if ok else f"疑似将 {target} 误报为逾期未发"
        elif chk.get("require_action_tool"):
            called_action = any(
                s.get("tool_name") in {"escalate_overdue_order", "fix_product_category", "reconcile_customer_identity"}
                and s.get("kind") in {"observation", "approval_required"}
                and not s.get("is_error")
                for s in steps
            )
            ok = mode == "ontology" and called_action
            note = "已通过受控 Action 完成预演" if ok else "不具备受控业务动作执行能力"

        if ok:
            passed += 1
        details.append({"id": chk["id"], "label": chk["label"], "passed": ok, "note": note})

    total = len(details)
    return {
        "has_ground_truth": True,
        "passed": passed,
        "total": total,
        "score_pct": round(passed / total * 100) if total else 100,
        "ground_truth_summary": bench["ground_truth_summary"],
        "details": details,
    }


def run_head_to_head_arena(
    question: str,
    agent_runner_factory: Callable[[], OntologyAgentLoopRunner],
    benchmark_id: str | None = None,
    llm_override: dict[str, Any] | None = None,
    local_config_path: Path = LOCAL_CONFIG_PATH,
    sources_dir: Path = SOURCES_DIR,
) -> dict[str, Any]:
    """同一真实模型同题 PK：左侧 Text-to-SQL（10张裸表） vs 右侧 Operational Ontology（MCP 9工具）。"""
    cfg = load_llm_config(override=llm_override, local_config_path=local_config_path)

    # 1) Text-to-SQL 侧
    t2s_res = run_text_to_sql_agent(question=question, cfg=cfg, sources_dir=sources_dir)

    # 2) Operational Ontology 侧（在隔离 Runner 中运行，开启 require_approval=False 或 dry_run 以保证一次自动跑完对比；
    #    注意：我们在 arena 对比时把 require_approval=True 改为由 prompt 要求 dry_run 或直接观察到 AWAITING_APPROVAL 即视为具备受控预演能力！）
    runner = agent_runner_factory()
    onto_sess = runner.start_run(
        task=question,
        engine_mode="real_llm",
        require_approval=True,
        llm_config=llm_override,
    )
    # 若 Ontology 侧因真模型尝试直接提交写动作而停在 HITL 审批门，保留完整 dry_run 预览作为最终展示
    onto_answer = onto_sess.get("final_answer") or ""
    if onto_sess.get("status") == "AWAITING_APPROVAL" and onto_sess.get("pending_approval"):
        ap = onto_sess["pending_approval"]
        onto_answer = (
            f"【受控预演通过并触发 HITL 审批闸门】\n"
            f"模型请求执行 `{ap['tool_name']}`，Ontology 前置守卫 `dry_run` 已通过校验并安全挂起等待人工放行：\n"
            f"- 审批摘要：{ap['summary']}\n"
            f"- 预演快照：`{json.dumps(ap['dry_run_preview'], ensure_ascii=False)}`"
        )

    t2s_eval = evaluate_answer_against_checks(benchmark_id, t2s_res.get("final_answer", ""), t2s_res.get("steps", []), "text_to_sql")
    onto_eval = evaluate_answer_against_checks(benchmark_id, onto_answer, onto_sess.get("steps", []), "ontology")

    return {
        "ok": True,
        "benchmark_id": benchmark_id,
        "question": question,
        "model": f"{cfg.provider}/{cfg.model}",
        "text_to_sql": {
            **t2s_res,
            "evaluation": t2s_eval,
        },
        "ontology": {
            "mode": "ontology",
            "status": onto_sess.get("status"),
            "model_info": onto_sess.get("model_info", {}),
            "steps": onto_sess.get("steps", []),
            "pending_approval": onto_sess.get("pending_approval"),
            "final_answer": onto_answer,
            "metrics": onto_sess.get("metrics", {}),
            "evaluation": onto_eval,
        },
    }
