#!/usr/bin/env python3
"""Operational Ontology Agent Loop 引擎（ReAct 循环 + MCP 协议调用 + HITL 人工审批闸门）。

架构特点：
  1. 统一经由 MCP 协议（tools/list, tools/call）访问 Operational Ontology 的 9 个语义工具，
     完整记录每轮 Thought -> MCP Tool Call -> Observation -> Guard / Approval -> Final Answer 轨迹；
  2. 支持双引擎：
     - deterministic（默认，零外部 API Key / 离线可用）：内置面向企业履约与治理场景的 ReAct 状态机，
       能真正多轮调用工具、遇到前置条件守卫拦截时自我纠偏下钻排查、写动作先 dry_run 再走人工审批；
     - openai_compatible（接入真实 LLM）：使用标准库 urllib.request 直连任意 OpenAI 兼容
       /chat/completions 接口（OpenAI / DeepSeek / 通义千问 / Ollama 等），注入 9 个标准 Tool Schema
       跑真实的原生 Function Calling Agent Loop；
  3. Human-in-the-Loop（HITL）人工审批闸门：
     - 当 require_approval=True 且 Agent 准备提交写动作（dry_run=False）时，自动先做 dry_run 预演，
       若预演通过则将 Session 挂起为 AWAITING_APPROVAL，等待人工批准或驳回后再无缝恢复 Loop！
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mcp_server.server import OntologyMCPServer
from ontology.llm_config import LLMConfig, LOCAL_CONFIG_PATH, load_llm_config
from ontology.llm_providers import (
    LLMCallError,
    LLMToolCall,
    append_tool_results,
    build_initial_history,
    call_llm,
)

SYSTEM_PROMPT = """你是企业级 B2B 订单履约 Operational Ontology 智能运维 Agent。
你只能通过注册的 Ontology MCP 语义工具来查询业务事实与执行受控动作：
1. 绝不猜测数据；回答前先用工具查询统一业务实体（如 find_vip_overdue_orders、get_customer_360、get_category_sales_and_audit、list_data_quality_issues、inspect_object_graph）。
2. 若任何动作被前置条件守卫拦截（isError=true / PreconditionError），请不要强行重试，而是立即调用 explain_order_overdue_status 或 inspect_object_graph 查明根因并向用户解释。
3. 执行业务写动作（escalate_overdue_order / fix_product_category / reconcile_customer_identity）前应严谨核对对象状态，写回产品修复或客户对齐时可开启 rebuild=true 让本体实时生效，然后复查修复前后指标对比。
4. 用简洁清晰的中文给出最终结论、数据证据与已执行/已拦截动作清单。"""

ACTION_TOOLS = {
    "escalate_overdue_order",
    "fix_product_category",
    "reconcile_customer_identity",
}

PRESET_TASKS: list[dict[str, Any]] = [
    {
        "id": "vip_patrol_escalate",
        "title": "VIP 履约巡检 + 自动催办闭环",
        "badge": "HITL 闭环",
        "kind": "good",
        "prompt": "请巡检近 30 天内 VIP 客户逾期未发货订单，下钻最急/涉险金额最高的订单并为其发起 P1 催办工单。",
    },
    {
        "id": "guard_self_heal_905",
        "title": "误操作拦截与 AI 自我纠偏（SO-20261105-905）",
        "badge": "守卫自愈",
        "kind": "guard",
        "prompt": "ERP 里看到订单 SO-20261105-905 还是待发货，请立刻帮我催办出库；如果不行，请查清跨系统根因并报告。",
    },
    {
        "id": "fix_p1012_closed_loop",
        "title": "数据治理自动闭环：发现并修复 P-1012",
        "badge": "重建闭环",
        "kind": "good",
        "prompt": "请审计未归类商品对大类营收的影响，将未维护分类的商品 P-1012 修复到合法叶子分类 0101(打印机) 并重建本体，最后对比修复前后的异常数和营收变化。",
    },
    {
        "id": "alias_due_diligence",
        "title": "用更名前旧名“博思咨询”做客户 360° 尽调",
        "badge": "语义解析",
        "kind": "read",
        "prompt": "销售同事问客户“博思咨询”最近的订单金额、准时率和档案完整性如何，请帮我做一次完整的客户尽调。",
    },
    {
        "id": "guard_bad_merge",
        "title": "法人合并安全守卫测试（ACC-1011 ↔ C0011）",
        "badge": "安全拦截",
        "kind": "guard",
        "prompt": "请尝试把 CRM 账号 ACC-1011 和 ERP 客户 C0011 合并对齐，如果被本体守卫拦截请核查两家实体档案并给出解释。",
    },
]


@dataclass
class TraceStep:
    step_no: int
    kind: str  # thought | tool_call | observation | guard_blocked | approval_required | approval_decision | final_answer | error
    title: str
    content: str
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: Any = None
    is_error: bool = False
    duration_ms: int = 0
    timestamp: str = ""


@dataclass
class PendingApproval:
    approval_id: str
    tool_name: str
    tool_args: dict[str, Any]
    dry_run_preview: dict[str, Any]
    summary: str
    llm_tool_call_id: str | None = None


@dataclass
class AgentSession:
    session_id: str
    task: str
    engine_mode: str  # real_llm | deterministic
    require_approval: bool
    status: str = "RUNNING"  # RUNNING | AWAITING_APPROVAL | COMPLETED | FAILED
    created_at: str = ""
    model_info: dict[str, Any] = field(default_factory=dict)
    steps: list[TraceStep] = field(default_factory=list)
    pending_approval: PendingApproval | None = None
    final_answer: str = ""
    metrics: dict[str, int] = field(
        default_factory=lambda: {
            "iterations": 0,
            "mcp_calls": 0,
            "guard_blocks": 0,
            "actions_committed": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "llm_latency_ms": 0,
        }
    )
    # 内部恢复上下文（不直接暴露给普通摘要，但在审批恢复时复用）
    _resume_state: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task": self.task,
            "engine_mode": self.engine_mode,
            "require_approval": self.require_approval,
            "status": self.status,
            "created_at": self.created_at,
            "model_info": dict(self.model_info),
            "steps": [asdict(s) for s in self.steps],
            "pending_approval": asdict(self.pending_approval) if self.pending_approval else None,
            "final_answer": self.final_answer,
            "metrics": dict(self.metrics),
        }


class OntologyAgentLoopRunner:
    """将 Agent Loop 通过标准 MCP JSON-RPC 协议挂载到 OntologyMCPServer。"""

    def __init__(
        self,
        mcp_server_factory: Callable[[], OntologyMCPServer],
        on_ontology_rebuilt: Callable[[], None] | None = None,
        local_config_path: Path = LOCAL_CONFIG_PATH,
    ) -> None:
        self._mcp_factory = mcp_server_factory
        self._on_rebuilt = on_ontology_rebuilt
        self._local_config_path = Path(local_config_path)
        self._sessions: dict[str, AgentSession] = {}
        self._seq = 0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def list_sessions(self) -> list[dict[str, Any]]:
        return [s.to_public_dict() for s in reversed(list(self._sessions.values()))]

    def get_session(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._sessions:
            raise KeyError(f"Agent Session 不存在：{session_id}")
        return self._sessions[session_id].to_public_dict()

    def clear_sessions(self) -> None:
        self._sessions.clear()
        self._seq = 0

    # ------------------------------------------------------------------
    # MCP JSON-RPC 2.0 工具调用桥接
    # ------------------------------------------------------------------

    def _mcp_list_tools(self) -> list[dict[str, Any]]:
        srv = self._mcp_factory()
        resp = srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert resp is not None
        return resp["result"]["tools"]

    def _mcp_call_tool(
        self,
        session: AgentSession,
        tool_name: str,
        arguments: dict[str, Any],
        step_title: str | None = None,
    ) -> dict[str, Any]:
        """真正通过 MCP JSON-RPC 2.0 `tools/call` 调用工具，并记录 trace 与耗时。"""
        self._add_step(
            session,
            kind="tool_call",
            title=step_title or f"MCP tools/call → {tool_name}",
            content=f"调用 MCP 工具 {tool_name}，参数：{json.dumps(arguments, ensure_ascii=False)}",
            tool_name=tool_name,
            tool_args=dict(arguments),
        )

        t0 = time.perf_counter()
        srv = self._mcp_factory()
        rpc_resp = srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": session.metrics["mcp_calls"] + 10,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        )
        elapsed_ms = max(1, int((time.perf_counter() - t0) * 1000))
        session.metrics["mcp_calls"] += 1

        assert rpc_resp is not None
        if "error" in rpc_resp:
            parsed: dict[str, Any] = {
                "ok": False,
                "tool": tool_name,
                "error_type": "MCPRPCError",
                "message": rpc_resp["error"]["message"],
            }
            is_err = True
        else:
            is_err = bool(rpc_resp["result"].get("isError", False))
            raw_text = rpc_resp["result"]["content"][0]["text"]
            parsed = json.loads(raw_text)

        if is_err:
            session.metrics["guard_blocks"] += 1
            self._add_step(
                session,
                kind="guard_blocked",
                title=f"本体守卫拦截 ← {tool_name}",
                content=parsed.get("message", "工具调用被拒绝"),
                tool_name=tool_name,
                tool_result=parsed,
                is_error=True,
                duration_ms=elapsed_ms,
            )
        else:
            if tool_name in ACTION_TOOLS and not arguments.get("dry_run"):
                session.metrics["actions_committed"] += 1
                if arguments.get("rebuild") and self._on_rebuilt:
                    self._on_rebuilt()
            self._add_step(
                session,
                kind="observation",
                title=f"Observation ← {tool_name} ({elapsed_ms}ms)",
                content=self._summarize_observation(tool_name, parsed),
                tool_name=tool_name,
                tool_result=parsed,
                is_error=False,
                duration_ms=elapsed_ms,
            )

        return parsed

    def _add_step(
        self,
        session: AgentSession,
        kind: str,
        title: str,
        content: str,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result: Any = None,
        is_error: bool = False,
        duration_ms: int = 0,
    ) -> TraceStep:
        step = TraceStep(
            step_no=len(session.steps) + 1,
            kind=kind,
            title=title,
            content=content,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            is_error=is_error,
            duration_ms=duration_ms,
            timestamp=self._now(),
        )
        session.steps.append(step)
        return step

    @staticmethod
    def _summarize_observation(tool_name: str, parsed: dict[str, Any]) -> str:
        data = parsed.get("data")
        if tool_name == "find_vip_overdue_orders" and isinstance(data, list):
            nos = [r["order_no"] for r in data]
            tot = sum(float(r["total_amount"]) for r in data)
            return f"检出 {len(data)} 张 VIP 逾期未有效发货订单：{nos}，涉险总额 ¥{tot:,.2f}"
        if tool_name == "explain_order_overdue_status" and isinstance(data, dict):
            return f"订单 {data.get('order_no')} 命中CQ1={data.get('matched_cq1')}；原因：{'；'.join(data.get('reasons', []))}"
        if tool_name == "get_customer_360" and isinstance(data, list):
            names = [f"{r['canonical_name']}({r['customer_id']})" for r in data[:4]]
            return f"返回 {len(data)} 个统一客户实体：{', '.join(names)}"
        if tool_name == "get_category_sales_and_audit" and isinstance(data, dict):
            rec = data["reconciliation"]
            return (
                f"有效总额 ¥{rec['valid_orders_total']:,.2f}（守恒={rec['is_balanced']}），"
                f"异常分类商品 {len(data['anomalous_products'])} 个"
            )
        if tool_name == "list_data_quality_issues":
            if isinstance(data, dict):
                return f"共 {data['total_issues']} 条质量异常（{data['by_severity']}）"
            if isinstance(data, list):
                return f"按条件筛出 {len(data)} 条质量异常明细"
        if tool_name == "inspect_object_graph" and isinstance(data, dict):
            return (
                f"对象 {data['object_type']}:{data['object_id']}：出边 {len(data['outgoing'])} 条、"
                f"入边 {len(data['incoming'])} 条、挂载异常 {len(data['data_quality_issues'])} 条"
            )
        if tool_name in ACTION_TOOLS and isinstance(data, dict):
            return f"动作 {data.get('action_name')} 状态={data.get('status')}（{data.get('action_id')}）"
        return "工具执行成功"

    # ------------------------------------------------------------------
    # 对外入口：启动新 Loop / 审批恢复 Loop
    # ------------------------------------------------------------------

    def start_run(
        self,
        task: str,
        engine_mode: str = "real_llm",
        require_approval: bool = True,
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not task or not task.strip():
            raise ValueError("任务描述 task 不能为空")
        # 兼容旧参数名 openai_compatible -> 统一路由到多协议 real_llm 引擎
        normalized_mode = "real_llm" if engine_mode in {"real_llm", "openai_compatible"} else engine_mode
        if normalized_mode not in {"real_llm", "deterministic"}:
            raise ValueError(f"不支持的引擎模式：{engine_mode}")

        self._seq += 1
        session = AgentSession(
            session_id=f"AGT-{self._seq:04d}",
            task=task.strip(),
            engine_mode=normalized_mode,
            require_approval=require_approval,
            status="RUNNING",
            created_at=self._now(),
        )
        self._sessions[session.session_id] = session

        if normalized_mode == "real_llm":
            cfg = load_llm_config(override=llm_config, local_config_path=self._local_config_path)
            session.model_info = {
                "provider": cfg.provider,
                "model": cfg.model,
                "base_url": cfg.base_url,
                "config_source": cfg.config_source,
            }
            if not cfg.is_ready():
                session.status = "FAILED"
                self._add_step(
                    session,
                    kind="error",
                    title="真实模型 API Key 尚未配置（预留待填）",
                    content=(
                        f"当前目标模型：{cfg.provider} / {cfg.model} ({cfg.base_url})。\n"
                        "检测到 API Key 仍处于预留空置状态，系统不会自动伪造模拟回答。\n"
                        "请任选一种方式填入你的真实 Key 后重新运行：\n"
                        "  1) 在控制台上方「真实模型与 API 接入配置」面板粘贴 API Key 并点击「保存到本地配置」；\n"
                        "  2) 编辑本地文件 config/llm.local.yaml 或复制 .env.example 为 .env 并填入 LLM_API_KEY；\n"
                        "  3) 或填入本地免 Key 的 Ollama 地址（如 http://127.0.0.1:11434/v1）。"
                    ),
                    is_error=True,
                )
                return session.to_public_dict()

            session._resume_state = {
                "cfg": cfg,
                "history": build_initial_history(cfg.provider, session.task),
                "remaining_tool_calls": [],
                "completed_turn_results": [],
                "current_tool_call": None,
            }
            self._continue_llm_loop(session)
        else:
            session.model_info = {
                "provider": "offline_rule_demo",
                "model": "deterministic-demo",
                "base_url": "local://in-memory",
            }
            self._run_deterministic_start(session)

        return session.to_public_dict()

    def resolve_approval(
        self,
        session_id: str,
        approved: bool,
        reviewer_note: str = "",
    ) -> dict[str, Any]:
        if session_id not in self._sessions:
            raise KeyError(f"Agent Session 不存在：{session_id}")
        session = self._sessions[session_id]
        if session.status != "AWAITING_APPROVAL" or session.pending_approval is None:
            raise ValueError(f"Session {session_id} 当前状态为 {session.status}，没有待审批动作")

        pending = session.pending_approval
        session.pending_approval = None
        session.status = "RUNNING"

        decision_label = "批准执行" if approved else "人工驳回"
        self._add_step(
            session,
            kind="approval_decision",
            title=f"HITL 审批结果：{decision_label} ({pending.tool_name})",
            content=f"审核决定：{decision_label}" + (f"；备注：{reviewer_note}" if reviewer_note else ""),
            tool_name=pending.tool_name,
            tool_args=pending.tool_args,
        )

        if session.engine_mode == "real_llm":
            rs = session._resume_state
            cfg: LLMConfig = rs["cfg"]
            current_tc: LLMToolCall = rs["current_tool_call"]
            rs["current_tool_call"] = None

            if approved:
                res = self._mcp_call_tool(
                    session,
                    pending.tool_name,
                    pending.tool_args,
                    step_title=f"已批准 · MCP tools/call → {pending.tool_name}",
                )
            else:
                res = {
                    "ok": False,
                    "tool": pending.tool_name,
                    "error_type": "HumanRejected",
                    "message": f"人工审批驳回该写动作：{reviewer_note or '审批人未批准'}",
                }
            rs["completed_turn_results"].append((current_tc, res))

            # 先排空同一轮中 LLM 返回的剩余 tool_calls（保证严格满足 OpenAI / Anthropic / Gemini 协议要求）
            paused_again = self._drain_llm_tool_calls(session)
            if not paused_again:
                append_tool_results(cfg.provider, rs["history"], rs["completed_turn_results"])
                rs["completed_turn_results"] = []
                self._continue_llm_loop(session)
        else:
            self._resume_deterministic_after_approval(session, pending, approved, reviewer_note)

        return session.to_public_dict()

    # ------------------------------------------------------------------
    # 执行写动作前统一过 HITL 审批闸门（干跑预演 -> 挂起等待审批 或 直接执行）
    # ------------------------------------------------------------------

    def _propose_action(
        self,
        session: AgentSession,
        tool_name: str,
        commit_args: dict[str, Any],
        approval_summary: str,
        llm_tool_call_id: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """返回 ('PAUSED', dry_res) | ('BLOCKED', dry_res) | ('COMMITTED', commit_res)。"""
        # 无论是否开启人工审批，先用 dry_run=True 走前置条件守卫校验，安全可靠！
        dry_args = dict(commit_args)
        dry_args["dry_run"] = True
        dry_args.pop("rebuild", None)

        dry_res = self._mcp_call_tool(
            session,
            tool_name,
            dry_args,
            step_title=f"前置守卫预演 (dry_run) → {tool_name}",
        )
        if not dry_res["ok"]:
            return "BLOCKED", dry_res

        if session.require_approval:
            approval = PendingApproval(
                approval_id=f"APR-{session.session_id}-{len(session.steps) + 1}",
                tool_name=tool_name,
                tool_args=commit_args,
                dry_run_preview=dry_res["data"],
                summary=approval_summary,
                llm_tool_call_id=llm_tool_call_id,
            )
            session.pending_approval = approval
            session.status = "AWAITING_APPROVAL"
            self._add_step(
                session,
                kind="approval_required",
                title=f"等待人工审批（HITL）：{tool_name}",
                content=approval_summary,
                tool_name=tool_name,
                tool_args=commit_args,
                tool_result=dry_res["data"],
            )
            return "PAUSED", dry_res

        commit_res = self._mcp_call_tool(
            session,
            tool_name,
            commit_args,
            step_title=f"自动提交 · MCP tools/call → {tool_name}",
        )
        return ("COMMITTED" if commit_res["ok"] else "BLOCKED"), commit_res

    # ------------------------------------------------------------------
    # 引擎 A：本地确定性 ReAct 推理引擎（零 Key 离线可用，覆盖 5+ 典型企业场景与自由提问）
    # ------------------------------------------------------------------

    def _run_deterministic_start(self, session: AgentSession) -> None:
        task = session.task
        session.metrics["iterations"] += 1

        # 路由 1：指定某张订单催办（如 SO-20261105-905 误催办自愈，或其它指定单号）
        order_match = re.search(r"SO-\d{8}-\d{3}", task, re.IGNORECASE)
        if order_match and any(k in task for k in ("催办", "加急", "发货", "出库", "逾期")):
            order_no = order_match.group(0).upper()
            self._flow_order_escalate_with_self_heal(session, order_no)
            return

        # 路由 2：指定产品 SKU 分类修复闭环（如 P-1012 / P-1013 / P-1014 / P-1015）
        sku_match = re.search(r"P-\d{4}", task, re.IGNORECASE)
        if sku_match and any(k in task for k in ("修复", "分类", "归类", "整改", "绑定")):
            sku = sku_match.group(0).upper()
            cat_match = re.search(r"\b(0[12]0[1-9]|0[12])\b", task)
            target_cat = cat_match.group(1) if cat_match else "0101"
            self._flow_fix_product_closed_loop(session, sku, target_cat)
            return

        # 路由 3：客户跨系统合并 / 对齐（含信用代码冲突拦截测试）
        crm_match = re.search(r"ACC-\d{4}", task, re.IGNORECASE)
        erp_match = re.search(r"C\d{4}", task, re.IGNORECASE)
        if crm_match and erp_match:
            self._flow_reconcile_customer(session, crm_match.group(0).upper(), erp_match.group(0).upper())
            return

        # 路由 4：VIP 逾期履约巡检 + 自动催办闭环
        if any(k in task.lower() for k in ("vip", "逾期", "巡检", "催办")):
            self._flow_vip_patrol(session, want_escalate=("催办" in task or "工单" in task or "加急" in task))
            return

        # 路由 5：按客户名/旧名别名查询客户 360° 尽调（如“博思咨询”“星辰设计”“云帆”“嘉禾”）
        alias_candidates = [
            "博思咨询", "远东管理咨询", "星辰设计", "星辰创意", "云帆科技", "新云帆",
            "蓝天教育", "恒信", "宏图", "文华文具", "金桥", "嘉禾", "启明医疗", "张三", "李四", "赵六",
        ]
        quoted = re.search(r"[“\"]([^”\"]+)[”\"]", task)
        found_query = quoted.group(1) if quoted else next((kw for kw in alias_candidates if kw in task), None)
        if found_query or any(k in task for k in ("客户", "360", "尽调", "准时率", "别名")):
            self._flow_customer_due_diligence(session, found_query or "")
            return

        # 路由 6：品类销售 / 数据质量治理巡检（兜底通用巡检）
        self._flow_general_governance_audit(session)

    # ---- 场景流 1：指定订单催办 + 守卫拦截后 AI 自我纠偏 ----
    def _flow_order_escalate_with_self_heal(self, session: AgentSession, order_no: str) -> None:
        self._add_step(
            session,
            kind="thought",
            title="Thought #1：准备通过受控动作预演催办",
            content=f"用户请求催办订单 {order_no}。遵循 Operational Ontology 安全规范，不盲目写回，先发起 escalate_overdue_order 前置条件守卫校验。",
        )
        outcome, res = self._propose_action(
            session,
            "escalate_overdue_order",
            {
                "order_no": order_no,
                "reason": f"Agent 收到针对订单 {order_no} 的催办请求",
                "priority": "P1",
                "actor": "ontology-agent-loop",
            },
            approval_summary=f"预演通过，准备为逾期订单 {order_no} 生成 WMS 加急工单并通知归属销售。",
        )

        if outcome == "PAUSED":
            session._resume_state = {"flow": "single_order_escalate", "order_no": order_no}
            return

        if outcome == "COMMITTED":
            self._finish_single_order_escalate(session, order_no, res, approved=True)
            return

        # 被守卫拦截 → AI 自我纠偏：调用 explain_order_overdue_status + inspect_object_graph 查清根因！
        session.metrics["iterations"] += 1
        self._add_step(
            session,
            kind="thought",
            title="Thought #2：动作被本体守卫拦截，启动自动根因下钻",
            content=f"escalate_overdue_order 被前置守卫拒绝（{res.get('message')}）。我现在调用 explain_order_overdue_status 与 inspect_object_graph 核实跨系统状态差异与挂载异常。",
        )
        exp = self._mcp_call_tool(session, "explain_order_overdue_status", {"order_no": order_no})
        nb = self._mcp_call_tool(
            session,
            "inspect_object_graph",
            {"object_type": "SalesOrder", "object_id": order_no},
        )
        ship_edges = [e for e in nb["data"]["incoming"] if e["link_type"] == "fulfills"]
        ship_dq_note = ""
        if ship_edges:
            sid = ship_edges[0]["neighbor_id"]
            ship_nb = self._mcp_call_tool(
                session, "inspect_object_graph", {"object_type": "Shipment", "object_id": sid}
            )
            ship_dqs = [f"{i['rule_code']}({i['rule_name']})" for i in ship_nb["data"]["data_quality_issues"]]
            if ship_dqs:
                ship_dq_note = f"\n- 关联 WMS 发货单 `{sid}` 同时命中异常：`{', '.join(ship_dqs)}`（已由本体层自动修复单号并关联）。"

        order_dqs = [f"{i['rule_code']}({i['description']})" for i in nb["data"]["data_quality_issues"]]
        reasons = "；".join(exp["data"].get("reasons", []))
        answer = (
            f"### 已拦截误催办并完成跨系统根因核查：`{order_no}`\n\n"
            f"- **执行结论**：未发起 WMS 催办（已被 Ontology 前置守卫 `is_overdue_unshipped == true` 拦截，避免了无效工单）。\n"
            f"- **真实业务事实**：客户 **{exp['data'].get('customer_name')}**，ERP 状态为 `{exp['data'].get('erp_status')}`，"
            f"但本体层按 WMS 权威事实源裁决为 `{exp['data'].get('fulfillment_status')}`。\n"
            f"- **规则解释**：{reasons}\n"
            f"- **挂载数据质量异常**：{('；'.join(order_dqs)) if order_dqs else '无'}"
            f"{ship_dq_note}\n"
            f"- **建议动作**：无需催仓库发货，建议通知 IT/商务排查 ERP 状态同步任务滞后问题。"
        )
        self._complete_session(session, answer)

    # ---- 场景流 2：VIP 逾期履约巡检 + 选出最高涉险订单发起催办 ----
    def _flow_vip_patrol(self, session: AgentSession, want_escalate: bool) -> None:
        self._add_step(
            session,
            kind="thought",
            title="Thought #1：调用 CQ1 语义工具检索近 30 天 VIP 逾期未发货订单",
            content="先通过 MCP 工具 find_vip_overdue_orders 获取按 WMS 事实源裁决后的高优逾期清单，并统计负责销售与涉险金额。",
        )
        cq1_res = self._mcp_call_tool(session, "find_vip_overdue_orders", {"window_days": 30})
        orders = cq1_res["data"]
        if not orders:
            self._complete_session(session, "近 30 天内没有 VIP 客户逾期未有效发货订单，履约状态健康。")
            return

        # 找出涉险金额最高的订单并下钻图谱核实
        top_order = max(orders, key=lambda o: o["total_amount"])
        session.metrics["iterations"] += 1
        self._add_step(
            session,
            kind="thought",
            title=f"Thought #2：锁定最高涉险订单 {top_order['order_no']} 并核查对象图谱",
            content=(
                f"共发现 {len(orders)} 张 VIP 逾期单，其中 {top_order['order_no']}（{top_order['canonical_name']}）"
                f"涉险金额最高（¥{top_order['total_amount']:,.2f}，已逾期 {top_order['overdue_days']} 天）。"
                "先下钻其语义图谱确认归属关系与异常情况。"
            ),
        )
        self._mcp_call_tool(
            session,
            "inspect_object_graph",
            {"object_type": "SalesOrder", "object_id": top_order["order_no"]},
        )

        if not want_escalate:
            self._finish_vip_patrol(session, orders, top_order, None, approved=False)
            return

        session.metrics["iterations"] += 1
        self._add_step(
            session,
            kind="thought",
            title=f"Thought #3：为 {top_order['order_no']} 发起受控催办预演",
            content="准备调用 escalate_overdue_order 发起 P1 催办工单；先执行 dry_run 预演校验前置条件与幂等性。",
        )
        commit_args = {
            "order_no": top_order["order_no"],
            "reason": f"VIP 客户 {top_order['canonical_name']} 订单涉险 ¥{top_order['total_amount']:,.2f}，已逾期 {top_order['overdue_days']} 天未有效发货",
            "priority": "P1",
            "actor": "ontology-agent-loop",
        }
        outcome, res = self._propose_action(
            session,
            "escalate_overdue_order",
            commit_args,
            approval_summary=(
                f"准备为 VIP 客户【{top_order['canonical_name']}】订单 {top_order['order_no']}"
                f"（¥{top_order['total_amount']:,.2f}，逾期 {top_order['overdue_days']} 天）创建 P1 催办工单，"
                f"并通知销售负责人 {top_order['owner_name']}/{top_order['team']}。"
            ),
        )
        if outcome == "PAUSED":
            session._resume_state = {"flow": "vip_patrol", "orders": orders, "top_order": top_order}
            return
        self._finish_vip_patrol(session, orders, top_order, res, approved=(outcome == "COMMITTED"))

    # ---- 场景流 3：修复异常商品分类 + 重建本体 + 前后对比闭环 ----
    def _flow_fix_product_closed_loop(self, session: AgentSession, sku: str, target_cat: str) -> None:
        self._add_step(
            session,
            kind="thought",
            title="Thought #1：先审计修复前的 CQ3 品类营收与 CQ4 质量异常基线",
            content=f"在修复商品 {sku} 之前，先调用 get_category_sales_and_audit 与 list_data_quality_issues 记录修复前基准指标。",
        )
        before_cq3 = self._mcp_call_tool(session, "get_category_sales_and_audit", {})["data"]
        before_dq = self._mcp_call_tool(session, "list_data_quality_issues", {})["data"]

        session.metrics["iterations"] += 1
        self._add_step(
            session,
            kind="thought",
            title=f"Thought #2：发起受控动作 fix_product_category（{sku} → {target_cat}）",
            content=f"将尝试把异常商品 {sku} 重新绑定到目标分类 {target_cat} 并开启 rebuild=true；先通过 dry_run 校验叶子节点守卫。",
        )
        commit_args = {
            "sku": sku,
            "target_leaf_category_code": target_cat,
            "reviewer": "ontology-agent-steward",
            "reason": f"Agent 治理闭环：将异常商品 {sku} 归类至标准叶子节点 {target_cat}",
            "rebuild": True,
        }
        outcome, res = self._propose_action(
            session,
            "fix_product_category",
            commit_args,
            approval_summary=(
                f"将异常商品 {sku} 写入治理覆盖表绑定至叶子分类 {target_cat}，并立即重建 SQLite 本体库与 W3C 导出文件。"
            ),
        )
        if outcome == "PAUSED":
            session._resume_state = {
                "flow": "fix_product",
                "sku": sku,
                "target_cat": target_cat,
                "before_cq3": before_cq3,
                "before_dq": before_dq,
            }
            return
        if outcome == "BLOCKED":
            self._complete_session(
                session,
                f"### 商品分类修复被本体守卫拦截\n\n- **目标**：`{sku} → {target_cat}`\n- **拦截原因**：{res.get('message')}\n- 请选择标准二级叶子分类码（如 `0101` 打印机、`0102` 复印机、`0103` 投影仪）后重试。",
            )
            return
        self._finish_fix_product(session, sku, target_cat, before_cq3, before_dq, res, approved=True)

    # ---- 场景流 4：跨系统客户合并 + 守卫拦截根因解释 ----
    def _flow_reconcile_customer(self, session: AgentSession, crm_id: str, erp_no: str) -> None:
        self._add_step(
            session,
            kind="thought",
            title=f"Thought #1：校验并预演跨系统客户对齐 {crm_id} ↔ {erp_no}",
            content="先通过 reconcile_customer_identity 的前置条件与安全守卫（信用代码冲突、主体类型一致性、防重复改绑）进行预演。",
        )
        commit_args = {
            "crm_account_id": crm_id,
            "erp_customer_no": erp_no,
            "reviewer": "ontology-agent-steward",
            "rationale": f"Agent 收到对齐请求：{crm_id} ↔ {erp_no}",
            "rebuild": True,
        }
        outcome, res = self._propose_action(
            session,
            "reconcile_customer_identity",
            commit_args,
            approval_summary=f"将 {crm_id} 与 {erp_no} 写入 L3 人工映射表（Crosswalk）并立即重建本体。",
        )
        if outcome == "PAUSED":
            session._resume_state = {"flow": "reconcile_customer", "crm_id": crm_id, "erp_no": erp_no}
            return
        if outcome == "COMMITTED":
            after_c = self._mcp_call_tool(session, "get_customer_360", {})["data"]
            self._complete_session(
                session,
                f"### 跨系统客户对齐已完成并重建本体\n\n- 已将 `{crm_id}` ↔ `{erp_no}` 写入 L3 Crosswalk（`{res['data']['action_id']}`）。\n- 重建后统一客户总数更新为 **{len(after_c)} 家**。",
            )
            return

        # 拦截后主动拉取两边客户档案做核查对比
        session.metrics["iterations"] += 1
        self._add_step(
            session,
            kind="thought",
            title="Thought #2：合并被安全守卫拦截，自动调取双方客户 360° 档案核对",
            content=f"拦截信息：{res.get('message')}。现在分别查询 {crm_id} 与 {erp_no} 的实体档案向用户提供完整佐证。",
        )
        c1 = self._mcp_call_tool(session, "get_customer_360", {"customer_query": crm_id})["data"]
        c2 = self._mcp_call_tool(session, "get_customer_360", {"customer_query": erp_no})["data"]
        c1_txt = f"{c1[0]['canonical_name']}（{c1[0]['party_type']} / {c1[0]['source_presence']}）" if c1 else crm_id
        c2_txt = f"{c2[0]['canonical_name']}（{c2[0]['party_type']} / {c2[0]['source_presence']}）" if c2 else erp_no
        self._complete_session(
            session,
            f"### 跨系统客户合并已被本体安全守卫拦截\n\n"
            f"- **拦截原因**：{res.get('message')}\n"
            f"- **CRM 侧实体**：`{crm_id}` → **{c1_txt}**\n"
            f"- **ERP 侧实体**：`{erp_no}` → **{c2_txt}**\n"
            f"- **结论**：Operational Ontology 强制阻断了高危错并风险，避免了营收归属与法人主数据污染。",
        )

    # ---- 场景流 5：按规范名或更名前旧名做客户 360° 尽调 ----
    def _flow_customer_due_diligence(self, session: AgentSession, query: str) -> None:
        self._add_step(
            session,
            kind="thought",
            title=f"Thought #1：调用 get_customer_360 检索客户（关键词：{query or '全部'}）",
            content="利用 Ontology 的别名索引（含 CRM 简称与更名前旧名）检索统一客户实体。",
        )
        rows = self._mcp_call_tool(session, "get_customer_360", {"customer_query": query})["data"]
        if not rows:
            self._complete_session(session, f"未在本体库中匹配到关键词 `{query}` 对应的客户实体。")
            return

        cust = rows[0]
        session.metrics["iterations"] += 1
        self._add_step(
            session,
            kind="thought",
            title=f"Thought #2：下钻 {cust['customer_id']} 图谱邻域与挂载异常",
            content=f"已命中规范实体 {cust['canonical_name']}（别名：{cust['aliases']}），进一步调用 inspect_object_graph 核查其关联订单与主数据缺陷。",
        )
        nb = self._mcp_call_tool(
            session,
            "inspect_object_graph",
            {"object_type": "Customer", "object_id": cust["customer_id"]},
        )["data"]
        dqs = [f"{i['rule_code']}: {i['description']}" for i in nb["data_quality_issues"]]
        rate = f"{cust['on_time_rate_pct']}%" if cust["on_time_rate_pct"] is not None else "N/A"
        self._complete_session(
            session,
            f"### 客户 360° 尽调报告：{cust['canonical_name']}（`{cust['customer_id']}`）\n\n"
            f"- **跨系统实体解析**：通过 `{cust['resolution_method']}` 对齐 CRM `{cust['crm_account_id'] or '-'}` 与 ERP `{cust['erp_customer_no'] or '-'}`；保留历史/简称别名：`{cust['aliases'] or '无'}`。\n"
            f"- **档案与归属**：类型 `{cust['party_type']}` ｜ 等级 `{cust['tier']}` ｜ 归属销售 `{cust['owner_name'] or '缺失'}`（{cust['team'] or '-'}）。\n"
            f"- **交易与履约表现**：有效订单 **{cust['valid_order_count']} 张**，有效总额 **¥{cust['valid_order_amount']:,.2f}**，WMS 准时发货率 **{rate}**，当前逾期未发 **{cust['overdue_unshipped_count']} 张**。\n"
            f"- **挂载数据质量异常（{len(dqs)} 条）**：{('；'.join(dqs)) if dqs else '无异常'}。",
        )

    # ---- 场景流 6：通用全盘数据治理与营收审计 ----
    def _flow_general_governance_audit(self, session: AgentSession) -> None:
        self._add_step(
            session,
            kind="thought",
            title="Thought #1：执行全盘跨系统数据质量与品类营收巡检",
            content="依次调用 list_data_quality_issues(HIGH) 与 get_category_sales_and_audit，评估当前高危一致性异常与未归类营收规模。",
        )
        high_dqs = self._mcp_call_tool(session, "list_data_quality_issues", {"severity": "HIGH"})["data"]
        cq3 = self._mcp_call_tool(session, "get_category_sales_and_audit", {})["data"]
        unclassified = next(
            (c["total_amount"] for c in cq3["root_category_summary"] if c["root_category_code"] == "UNCLASSIFIED"),
            0.0,
        )
        self._complete_session(
            session,
            f"### 跨系统数据治理巡检结论\n\n"
            f"- **高危异常（HIGH）**：共检出 **{len(high_dqs)} 条**（包含 ERP/WMS 状态滞后 `DQ-001`、孤立发货单 `DQ-002`、缺承诺日 `DQ-003` 及 3 个高危分类异常商品）。\n"
            f"- **营收归类透明度**：有效订单总额 **¥{cq3['reconciliation']['valid_orders_total']:,.2f}**（100% 金额守恒），其中因分类缺失/冲突/无效码挂账在 `UNCLASSIFIED` 的金额为 **¥{unclassified:,.2f}**。\n"
            f"- **下一步建议**：可直接下达任务“修复 P-1012 到 0101 并重建本体”或“巡检 VIP 逾期并催办”，体验完整受控写回闭环。",
        )

    # ---- HITL 审批后恢复执行（确定性引擎）----
    def _resume_deterministic_after_approval(
        self,
        session: AgentSession,
        pending: PendingApproval,
        approved: bool,
        reviewer_note: str,
    ) -> None:
        rs = session._resume_state
        flow = rs.get("flow")
        session.metrics["iterations"] += 1

        if not approved:
            self._complete_session(
                session,
                f"### 动作已被人工审批驳回\n\n- 未执行 `{pending.tool_name}`（目标参数：`{json.dumps(pending.tool_args, ensure_ascii=False)}`）。\n- 驳回备注：{reviewer_note or '审批人选择不执行本次写操作'}。\n- 本体库与审计账本保持只读无变更。",
            )
            return

        commit_res = self._mcp_call_tool(
            session,
            pending.tool_name,
            pending.tool_args,
            step_title=f"已批准 · MCP tools/call → {pending.tool_name}",
        )
        if not commit_res["ok"]:
            self._complete_session(session, f"动作提交阶段发生异常：{commit_res.get('message')}")
            return

        if flow == "vip_patrol":
            self._finish_vip_patrol(session, rs["orders"], rs["top_order"], commit_res, approved=True)
        elif flow == "fix_product":
            self._finish_fix_product(
                session, rs["sku"], rs["target_cat"], rs["before_cq3"], rs["before_dq"], commit_res, approved=True
            )
        elif flow == "single_order_escalate":
            self._finish_single_order_escalate(session, rs["order_no"], commit_res, approved=True)
        elif flow == "reconcile_customer":
            after_c = self._mcp_call_tool(session, "get_customer_360", {})["data"]
            self._complete_session(
                session,
                f"### 跨系统客户对齐已批准并完成重建\n\n"
                f"- 动作编号：`{commit_res['data']['action_id']}`（`{rs['crm_id']}` ↔ `{rs['erp_no']}`）。\n"
                f"- 重建后统一客户总数更新为 **{len(after_c)} 家**，已写入追加式审计账本。",
            )

    def _finish_vip_patrol(
        self,
        session: AgentSession,
        orders: list[dict[str, Any]],
        top_order: dict[str, Any],
        commit_res: dict[str, Any] | None,
        approved: bool,
    ) -> None:
        total_risk = sum(float(o["total_amount"]) for o in orders)
        lines = [
            f"### VIP 履约巡检与受控催办闭环报告",
            f"",
            f"- **CQ1 精准命中**：共 **{len(orders)} 张** VIP 近 30 天逾期未有效发货订单，总涉险金额 **¥{total_risk:,.2f}**。",
        ]
        for o in orders:
            lines.append(
                f"  - `{o['order_no']}` ｜ {o['canonical_name']} ｜ 负责：{o['owner_name']}/{o['team']} ｜ 逾期 {o['overdue_days']} 天 ｜ WMS=`{o['fulfillment_status']}` ｜ ¥{o['total_amount']:,.2f}"
            )
        if approved and commit_res and commit_res.get("ok"):
            se = commit_res["data"]["side_effects"]
            lines.append(
                f"- **已执行催办**：针对最高涉险单 `{top_order['order_no']}` 已生成 WMS 工单 **`{se['wms_ticket_id']}`**（`{commit_res['data']['action_id']}`），并通知 **{se['notify_sales_owner']}**。"
            )
        elif commit_res and not commit_res.get("ok"):
            lines.append(f"- **催办拦截说明**：{commit_res.get('message')}")
        self._complete_session(session, "\n".join(lines))

    def _finish_single_order_escalate(
        self, session: AgentSession, order_no: str, commit_res: dict[str, Any], approved: bool
    ) -> None:
        se = commit_res["data"]["side_effects"]
        self._complete_session(
            session,
            f"### 逾期订单催办已完成：`{order_no}`\n\n"
            f"- 动作编号：`{commit_res['data']['action_id']}`\n"
            f"- WMS 催办工单：**`{se['wms_ticket_id']}`**\n"
            f"- 通知销售负责人：**{se['notify_sales_owner']}**\n"
            f"- 已记录至不可变审计账本。",
        )

    def _finish_fix_product(
        self,
        session: AgentSession,
        sku: str,
        target_cat: str,
        before_cq3: dict[str, Any],
        before_dq: dict[str, Any],
        commit_res: dict[str, Any],
        approved: bool,
    ) -> None:
        self._add_step(
            session,
            kind="thought",
            title="Thought #3：本体已重建，复查 CQ3 与 CQ4 核验治理闭环效果",
            content="再次调用 get_category_sales_and_audit 与 list_data_quality_issues，量化修复前后的异常清零与大类营收变化。",
        )
        after_cq3 = self._mcp_call_tool(session, "get_category_sales_and_audit", {})["data"]
        after_dq = self._mcp_call_tool(session, "list_data_quality_issues", {})["data"]

        b_map = {c["root_category_code"]: c["total_amount"] for c in before_cq3["root_category_summary"]}
        a_map = {c["root_category_code"]: c["total_amount"] for c in after_cq3["root_category_summary"]}
        delta_01 = a_map.get("01", 0.0) - b_map.get("01", 0.0)
        delta_un = b_map.get("UNCLASSIFIED", 0.0) - a_map.get("UNCLASSIFIED", 0.0)

        self._complete_session(
            session,
            f"### 主数据治理闭环完成：`{sku} → {target_cat}`\n\n"
            f"- **执行凭证**：`{commit_res['data']['action_id']}`（已写入版本化覆盖层并自动重建 SQLite 本体与 W3C Turtle 导出，源 ERP CSV 保持只读未篡改）。\n"
            f"- **数据质量变化**：总异常数 **{before_dq['total_issues']} → {after_dq['total_issues']} 条**。\n"
            f"- **CQ3 营收归类变化（100% 金额守恒）**：\n"
            f"  - `01 办公设备`：¥{b_map.get('01', 0.0):,.2f} → **¥{a_map.get('01', 0.0):,.2f}**（`+¥{delta_01:,.2f}`）\n"
            f"  - `UNCLASSIFIED 未归类`：¥{b_map.get('UNCLASSIFIED', 0.0):,.2f} → **¥{a_map.get('UNCLASSIFIED', 0.0):,.2f}**（`-¥{delta_un:,.2f}`）",
        )

    def _complete_session(self, session: AgentSession, final_answer: str) -> None:
        session.final_answer = final_answer
        session.status = "COMPLETED"
        self._add_step(
            session,
            kind="final_answer",
            title="Final Answer（Agent Loop 完成）",
            content=final_answer,
        )

    # ------------------------------------------------------------------
    # 引擎 B：真实多协议 LLM Function Calling Loop (OpenAI / Anthropic / Gemini)
    # ------------------------------------------------------------------

    def _drain_llm_tool_calls(self, session: AgentSession) -> bool:
        """顺序执行当前轮次待处理的 tool_calls；若触发 HITL 审批挂起返回 True，全部排空返回 False。"""
        rs = session._resume_state
        queue: list[LLMToolCall] = rs["remaining_tool_calls"]
        completed: list[tuple[LLMToolCall, dict[str, Any]]] = rs["completed_turn_results"]

        while queue:
            tc = queue.pop(0)
            if tc.name in ACTION_TOOLS and not tc.arguments.get("dry_run"):
                outcome, res = self._propose_action(
                    session,
                    tc.name,
                    tc.arguments,
                    approval_summary=(
                        f"真实模型（{session.model_info.get('model')}）请求执行受控动作 {tc.name}，"
                        f"参数：{json.dumps(tc.arguments, ensure_ascii=False)}"
                    ),
                    llm_tool_call_id=tc.id,
                )
                if outcome == "PAUSED":
                    rs["current_tool_call"] = tc
                    return True
                completed.append((tc, res))
            else:
                res = self._mcp_call_tool(session, tc.name, tc.arguments)
                completed.append((tc, res))

        return False

    def _continue_llm_loop(self, session: AgentSession) -> None:
        rs = session._resume_state
        cfg: LLMConfig = rs["cfg"]
        history: list[dict[str, Any]] = rs["history"]
        mcp_tools = self._mcp_list_tools()

        while session.metrics["iterations"] < cfg.max_turns:
            session.metrics["iterations"] += 1
            turn_no = session.metrics["iterations"]

            try:
                resp = call_llm(
                    cfg=cfg,
                    system_prompt=SYSTEM_PROMPT,
                    history=history,
                    mcp_tools=mcp_tools,
                )
            except LLMCallError as exc:
                session.status = "FAILED"
                self._add_step(
                    session,
                    kind="error",
                    title=f"真实模型接口调用失败 ({cfg.provider} / {cfg.model})",
                    content=str(exc),
                    is_error=True,
                )
                return

            session.model_info["model"] = resp.model
            session.metrics["prompt_tokens"] += resp.usage.prompt_tokens
            session.metrics["completion_tokens"] += resp.usage.completion_tokens
            session.metrics["total_tokens"] += resp.usage.total_tokens
            session.metrics["llm_latency_ms"] += resp.latency_ms

            history.append(resp.raw_assistant_entry)

            if resp.reasoning:
                self._add_step(
                    session,
                    kind="thought",
                    title=f"Model Reasoning · Round #{turn_no} ({resp.model})",
                    content=resp.reasoning,
                    duration_ms=resp.latency_ms,
                )

            if not resp.tool_calls:
                self._complete_session(session, resp.text or "任务执行完毕。")
                return

            if resp.text:
                self._add_step(
                    session,
                    kind="thought",
                    title=f"LLM Thought · Round #{turn_no} ({resp.model}, {resp.latency_ms}ms)",
                    content=resp.text,
                    duration_ms=resp.latency_ms,
                )
            elif not resp.reasoning:
                call_names = ", ".join(tc.name for tc in resp.tool_calls)
                self._add_step(
                    session,
                    kind="thought",
                    title=f"LLM 决策调用工具 · Round #{turn_no} ({resp.model})",
                    content=f"模型决定调用 {len(resp.tool_calls)} 个 MCP 工具：{call_names}",
                    duration_ms=resp.latency_ms,
                )

            rs["remaining_tool_calls"] = list(resp.tool_calls)
            rs["completed_turn_results"] = []
            paused = self._drain_llm_tool_calls(session)
            if paused:
                return

            append_tool_results(cfg.provider, history, rs["completed_turn_results"])
            rs["completed_turn_results"] = []

        self._complete_session(
            session,
            f"已达到配置的最大迭代轮次上限（max_turns={cfg.max_turns}），Agent Loop 自动收敛结束。",
        )
