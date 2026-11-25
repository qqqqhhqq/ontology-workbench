#!/usr/bin/env python3
"""标准 Model Context Protocol (MCP) Server — JSON-RPC 2.0 over stdio。

严格遵循 MCP 协议规范（支持 2024-11-05 / 2025-03-26）：
  - Transport : 逐行 JSON-RPC 2.0（stdin 读取请求，stdout 写响应，所有日志走 stderr）
  - Tools     : 暴露 OntologyAgentToolkit 的全部 9 个语义工具（6 读 + 3 受控写，含 MCP annotations）
  - Resources : 暴露 ontology://schema、summary、cq/1..4 及 W3C TBox / SHACL 文件
  - Prompts   : 暴露 vip_fulfillment_patrol 与 data_governance_audit 两个标准工作流模板

运行方式：
  python3 -m mcp_server.server --mode sandbox   # 默认：隔离沙箱模式（可放心演练写动作）
  python3 -m mcp_server.server --mode live      # 直连工作区本体与治理覆盖表
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, TextIO

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.agent_tools import OntologyAgentToolkit
from ontology.client import OntologyClient

SERVER_NAME = "b2b-fulfillment-ontology"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

RESOURCES_CATALOG: list[dict[str, str]] = [
    {
        "uri": "ontology://schema",
        "name": "Ontology Schema (YAML)",
        "description": "完整的本体规范（受控词表、8 个对象类型、7 类关系、12 条约束、3 个受控动作）",
        "mimeType": "text/yaml",
    },
    {
        "uri": "ontology://summary",
        "name": "Ontology Materialization Summary",
        "description": "本体对象数量、语义边分布、实体解析与质量规则统计摘要",
        "mimeType": "application/json",
    },
    {
        "uri": "ontology://cq/1",
        "name": "CQ1: VIP Overdue Unshipped Orders",
        "description": "近 30 天内 VIP 客户逾期未有效发货订单（WMS 事实源裁决）",
        "mimeType": "application/json",
    },
    {
        "uri": "ontology://cq/2",
        "name": "CQ2: Customer 360 View",
        "description": "跨 CRM/ERP/WMS 的 14 个统一客户全景视图",
        "mimeType": "application/json",
    },
    {
        "uri": "ontology://cq/3",
        "name": "CQ3: Category Sales & Classification Audit",
        "description": "按一级大类传递闭包聚合的有效销售额与异常商品清单",
        "mimeType": "application/json",
    },
    {
        "uri": "ontology://cq/4",
        "name": "CQ4: Data Quality Audit Report",
        "description": "全量 DQ-001 ~ DQ-012 跨系统一致性与质量异常报告",
        "mimeType": "application/json",
    },
    {
        "uri": "ontology://w3c/tbox",
        "name": "W3C OWL 2 / RDFS TBox (Turtle)",
        "description": "标准 OWL 2 / RDFS 本体模式层 Turtle 文件",
        "mimeType": "text/turtle",
    },
    {
        "uri": "ontology://w3c/shacl",
        "name": "W3C SHACL 1.0 Constraints (Turtle)",
        "description": "标准 SHACL 1.0 NodeShape 与 SHACL-SPARQL 业务约束文件",
        "mimeType": "text/turtle",
    },
]

PROMPTS_CATALOG: list[dict[str, Any]] = [
    {
        "name": "vip_fulfillment_patrol",
        "description": "执行 VIP 客户履约风险巡检：调用 CQ1 识别逾期未发订单，核验销售归属，先 dry_run 预演再发起受控催办。",
        "arguments": [
            {
                "name": "window_days",
                "description": "回看下单时间窗口天数（默认 30）",
                "required": False,
            }
        ],
    },
    {
        "name": "data_governance_audit",
        "description": "执行跨系统数据治理巡检：检索 HIGH 级质量异常，分析影响金额与对象邻域，给出合规的修复动作建议。",
        "arguments": [],
    },
]


class OntologyMCPServer:
    """无状态协议层 + 可隔离沙箱的 Ontology MCP Server。"""

    def __init__(
        self,
        mode: str = "sandbox",
        db_path: Path | None = None,
        ledger_path: Path | None = None,
        crosswalk_path: Path | None = None,
        overrides_path: Path | None = None,
        graph_path: Path | None = None,
    ) -> None:
        self.mode = mode
        self._sandbox_tmp: tempfile.TemporaryDirectory[str] | None = None

        if mode == "sandbox" and db_path is None:
            self._sandbox_tmp = tempfile.TemporaryDirectory(prefix="ontology_mcp_")
            tmp_dir = Path(self._sandbox_tmp.name)
            self.db_path = tmp_dir / "ontology.db"
            self.graph_path = tmp_dir / "ontology_graph.json"
            self.ledger_path = tmp_dir / "action_ledger.jsonl"
            self.crosswalk_path = tmp_dir / "customer_crosswalk.yaml"
            self.overrides_path = tmp_dir / "product_category_overrides.yaml"

            shutil.copy2(ROOT_DIR / "data" / "ontology" / "ontology.db", self.db_path)
            shutil.copy2(ROOT_DIR / "data" / "ontology" / "ontology_graph.json", self.graph_path)
            shutil.copy2(ROOT_DIR / "ontology" / "crosswalk" / "customer_crosswalk.yaml", self.crosswalk_path)
            shutil.copy2(ROOT_DIR / "ontology" / "overrides" / "product_category_overrides.yaml", self.overrides_path)
        else:
            self.db_path = db_path or (ROOT_DIR / "data" / "ontology" / "ontology.db")
            self.graph_path = graph_path or (ROOT_DIR / "data" / "ontology" / "ontology_graph.json")
            self.ledger_path = ledger_path or (ROOT_DIR / "data" / "ontology" / "action_ledger.jsonl")
            self.crosswalk_path = crosswalk_path or (ROOT_DIR / "ontology" / "crosswalk" / "customer_crosswalk.yaml")
            self.overrides_path = overrides_path or (
                ROOT_DIR / "ontology" / "overrides" / "product_category_overrides.yaml"
            )

        self.toolkit = OntologyAgentToolkit(
            db_path=self.db_path,
            ledger_path=self.ledger_path,
            crosswalk_path=self.crosswalk_path,
            overrides_path=self.overrides_path,
            graph_path=self.graph_path,
        )

    def close(self) -> None:
        if self._sandbox_tmp is not None:
            self._sandbox_tmp.cleanup()
            self._sandbox_tmp = None

    # ------------------------------------------------------------------
    # JSON-RPC 2.0 请求分发
    # ------------------------------------------------------------------

    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        # Notification（无 id）按 JSON-RPC 2.0 规范不返回响应
        if req_id is None:
            return None

        try:
            if method == "initialize":
                client_ver = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
                result: Any = {
                    "protocolVersion": client_ver,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                    "instructions": (
                        f"B2B 订单履约 Operational Ontology MCP Server（当前模式：{self.mode}）。"
                        "请优先通过语义工具查询统一业务实体；执行写动作前建议先使用 dry_run=true 预演。"
                    ),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._mcp_tools()}
            elif method == "tools/call":
                result = self._handle_tool_call(params)
            elif method == "resources/list":
                result = {"resources": RESOURCES_CATALOG}
            elif method == "resources/read":
                result = self._handle_resource_read(params)
            elif method == "prompts/list":
                result = {"prompts": PROMPTS_CATALOG}
            elif method == "prompts/get":
                result = self._handle_prompt_get(params)
            else:
                return self._error(req_id, -32601, f"Method not found: {method}")

            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except KeyError as exc:
            return self._error(req_id, -32602, f"Missing required parameter: {exc}")
        except ValueError as exc:
            return self._error(req_id, -32602, str(exc))
        except Exception as exc:
            return self._error(req_id, -32603, f"Internal error: {exc}")

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def _mcp_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for spec in self.toolkit.export_tool_schemas():
            is_read = spec["mode"] == "read"
            tools.append(
                {
                    "name": spec["name"],
                    "description": spec["description"],
                    "inputSchema": spec["input_schema"],
                    "annotations": {
                        "readOnlyHint": is_read,
                        "destructiveHint": False,
                        "idempotentHint": is_read,
                        "openWorldHint": False,
                    },
                }
            )
        return tools

    def _handle_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params["name"]
        arguments = params.get("arguments") or {}
        res = self.toolkit.call_tool(name, arguments)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(res, ensure_ascii=False, indent=2),
                }
            ],
            "isError": not res["ok"],
        }

    def _handle_resource_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params["uri"]
        if uri == "ontology://schema":
            text = (ROOT_DIR / "ontology" / "schema.yaml").read_text(encoding="utf-8")
            mime = "text/yaml"
        elif uri == "ontology://summary":
            graph = json.loads(self.graph_path.read_text(encoding="utf-8"))
            text = json.dumps(
                {"metadata": graph["metadata"], "summary": graph["summary"], "mode": self.mode},
                ensure_ascii=False,
                indent=2,
            )
            mime = "application/json"
        elif uri == "ontology://cq/1":
            with OntologyClient(self.db_path) as c:
                text = json.dumps(c.query_cq1_vip_overdue_unshipped(), ensure_ascii=False, indent=2)
            mime = "application/json"
        elif uri == "ontology://cq/2":
            with OntologyClient(self.db_path) as c:
                text = json.dumps(c.query_cq2_customer_360(), ensure_ascii=False, indent=2)
            mime = "application/json"
        elif uri == "ontology://cq/3":
            with OntologyClient(self.db_path) as c:
                text = json.dumps(c.query_cq3_category_sales_and_audit(), ensure_ascii=False, indent=2)
            mime = "application/json"
        elif uri == "ontology://cq/4":
            with OntologyClient(self.db_path) as c:
                text = json.dumps(c.query_cq4_data_quality_audit(), ensure_ascii=False, indent=2)
            mime = "application/json"
        elif uri == "ontology://w3c/tbox":
            text = (ROOT_DIR / "data" / "w3c" / "ontology_tbox.ttl").read_text(encoding="utf-8")
            mime = "text/turtle"
        elif uri == "ontology://w3c/shacl":
            text = (ROOT_DIR / "data" / "w3c" / "shapes_shacl.ttl").read_text(encoding="utf-8")
            mime = "text/turtle"
        else:
            raise ValueError(f"Unknown resource URI: {uri}")

        return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}

    def _handle_prompt_get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params["name"]
        args = params.get("arguments") or {}
        if name == "vip_fulfillment_patrol":
            days = args.get("window_days", 30)
            return {
                "description": "VIP 客户履约风险巡检与受控催办流程",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": (
                                f"请对近 {days} 天内下单的 VIP 客户执行履约巡检：\n"
                                "1. 调用 find_vip_overdue_orders 获取逾期未有效发货订单；\n"
                                "2. 汇总受影响客户、负责销售及涉险总额；\n"
                                "3. 针对最紧急的订单，先调用 escalate_overdue_order(dry_run=true) 预演催办，"
                                "向我展示将通知的销售人员与工单号后再决定是否正式提交。"
                            ),
                        },
                    }
                ],
            }
        if name == "data_governance_audit":
            return {
                "description": "跨系统数据质量巡检与治理修复建议",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": (
                                "请执行跨系统数据一致性审计：\n"
                                "1. 调用 list_data_quality_issues(severity='HIGH') 检索全部高危异常；\n"
                                "2. 调用 get_category_sales_and_audit 查看未归类商品影响金额；\n"
                                "3. 给出按业务影响排序的治理建议，并在获得确认后使用 fix_product_category "
                                "或 reconcile_customer_identity（先 dry_run=true）落地修复。"
                            ),
                        },
                    }
                ],
            }
        raise ValueError(f"Unknown prompt: {name}")

    # ------------------------------------------------------------------
    # stdio 主循环
    # ------------------------------------------------------------------

    def serve_stdio(self, inp: TextIO = sys.stdin, out: TextIO = sys.stdout) -> None:
        sys.stderr.write(
            f"[{SERVER_NAME}] MCP Server started over stdio (mode={self.mode}, db={self.db_path})\n"
        )
        sys.stderr.flush()
        try:
            for raw_line in inp:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    resp = self._error(None, -32700, f"Parse error: {exc}")
                    out.write(json.dumps(resp, ensure_ascii=False) + "\n")
                    out.flush()
                    continue

                resp = self.handle_message(msg)
                if resp is not None:
                    out.write(json.dumps(resp, ensure_ascii=False) + "\n")
                    out.flush()
        finally:
            self.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="B2B 订单履约 Operational Ontology MCP Server")
    parser.add_argument(
        "--mode",
        choices=["sandbox", "live"],
        default="sandbox",
        help="sandbox: 隔离沙箱副本（默认）；live: 直连工作区本体库与治理覆盖表",
    )
    args = parser.parse_args()
    server = OntologyMCPServer(mode=args.mode)
    server.serve_stdio()


if __name__ == "__main__":
    main()
