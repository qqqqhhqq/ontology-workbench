#!/usr/bin/env python3
"""交互式 Ontology 可视化图谱与受控动作控制台服务端（零外部依赖）。

特性：
  - 默认运行在隔离沙箱模式（--mode sandbox）：自动复制本体库与覆盖表到临时沙箱，
    支持在页面上安全试跑催办、修复分类、重建本体与一键重置，永不污染基线数据；
  - 复用 OntologyAgentToolkit / OntologyClient / W3C 导出文件，统一业务口径；
  - 静态页面位于 web/，接口位于 /api/*。

运行方式：
  python3 scripts/serve_console.py                  # 默认 http://127.0.0.1:8765 (sandbox)
  python3 scripts/serve_console.py --port 8765 --mode live
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from mcp_server.server import OntologyMCPServer
from ontology.agent_loop import OntologyAgentLoopRunner, PRESET_TASKS
from ontology.agent_tools import OntologyAgentToolkit
from ontology.client import OntologyClient
from ontology.llm_config import LOCAL_CONFIG_PATH, load_llm_config, save_llm_config
from ontology.llm_providers import ping_llm
from ontology.text2sql_arena import ARENA_BENCHMARKS, RawSQLiteSandbox, run_head_to_head_arena
from ontology.w3c.exporter import export_all_w3c

WEB_DIR = ROOT_DIR / "web"

TABLE_BY_TYPE: dict[str, tuple[str, str]] = {
    "SalesOwner": ("sales_owners", "owner_id"),
    "Customer": ("customers", "customer_id"),
    "ProductCategory": ("product_categories", "category_code"),
    "Product": ("products", "sku"),
    "SalesOrder": ("sales_orders", "order_no"),
    "OrderLine": ("order_lines", "order_line_id"),
    "Shipment": ("shipments", "shipment_id"),
    "DataQualityIssue": ("data_quality_issues", "issue_id"),
}


class ConsoleContext:
    """管理控制台后端运行状态（沙箱复制 / 重置 / 线程锁 / Toolkit）。"""

    def __init__(
        self,
        mode: str = "sandbox",
        llm_config_path: Path = LOCAL_CONFIG_PATH,
    ) -> None:
        self.mode = mode
        self.llm_config_path = Path(llm_config_path)
        self.lock = threading.Lock()
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self.db_path: Path
        self.graph_path: Path
        self.ledger_path: Path
        self.crosswalk_path: Path
        self.overrides_path: Path
        self.w3c_dir: Path
        self._init_paths()
        self.agent_runner = OntologyAgentLoopRunner(
            mcp_server_factory=self.mcp_server,
            on_ontology_rebuilt=lambda: export_all_w3c(db_path=self.db_path, out_dir=self.w3c_dir),
            local_config_path=self.llm_config_path,
        )

    def _init_paths(self) -> None:
        if self.mode == "sandbox":
            if self._tmp is not None:
                self._tmp.cleanup()
            self._tmp = tempfile.TemporaryDirectory(prefix="ontology_console_")
            tmp_dir = Path(self._tmp.name)
            self.db_path = tmp_dir / "ontology.db"
            self.graph_path = tmp_dir / "ontology_graph.json"
            self.ledger_path = tmp_dir / "action_ledger.jsonl"
            self.crosswalk_path = tmp_dir / "customer_crosswalk.yaml"
            self.overrides_path = tmp_dir / "product_category_overrides.yaml"
            self.w3c_dir = tmp_dir / "w3c"
            self._copy_baseline_into_sandbox()
        else:
            self.db_path = ROOT_DIR / "data" / "ontology" / "ontology.db"
            self.graph_path = ROOT_DIR / "data" / "ontology" / "ontology_graph.json"
            self.ledger_path = ROOT_DIR / "data" / "ontology" / "action_ledger.jsonl"
            self.crosswalk_path = ROOT_DIR / "ontology" / "crosswalk" / "customer_crosswalk.yaml"
            self.overrides_path = ROOT_DIR / "ontology" / "overrides" / "product_category_overrides.yaml"
            self.w3c_dir = ROOT_DIR / "data" / "w3c"

    def _copy_baseline_into_sandbox(self) -> None:
        shutil.copy2(ROOT_DIR / "data" / "ontology" / "ontology.db", self.db_path)
        shutil.copy2(ROOT_DIR / "data" / "ontology" / "ontology_graph.json", self.graph_path)
        shutil.copy2(ROOT_DIR / "ontology" / "crosswalk" / "customer_crosswalk.yaml", self.crosswalk_path)
        shutil.copy2(ROOT_DIR / "ontology" / "overrides" / "product_category_overrides.yaml", self.overrides_path)
        if self.ledger_path.exists():
            self.ledger_path.unlink()
        if (ROOT_DIR / "data" / "w3c").exists():
            shutil.copytree(ROOT_DIR / "data" / "w3c", self.w3c_dir, dirs_exist_ok=True)
        else:
            export_all_w3c(db_path=self.db_path, out_dir=self.w3c_dir)

    def reset_sandbox(self) -> dict[str, Any]:
        if self.mode != "sandbox":
            return {"ok": False, "message": "当前为 live 模式，不支持一键重置沙箱"}
        self._copy_baseline_into_sandbox()
        self.agent_runner.clear_sessions()
        return {"ok": True, "message": "沙箱已恢复到初始基线快照（AS_OF=2026-11-23）"}

    def mcp_server(self) -> OntologyMCPServer:
        return OntologyMCPServer(
            mode=self.mode,
            db_path=self.db_path,
            ledger_path=self.ledger_path,
            crosswalk_path=self.crosswalk_path,
            overrides_path=self.overrides_path,
            graph_path=self.graph_path,
        )

    def toolkit(self) -> OntologyAgentToolkit:
        return OntologyAgentToolkit(
            db_path=self.db_path,
            ledger_path=self.ledger_path,
            crosswalk_path=self.crosswalk_path,
            overrides_path=self.overrides_path,
            graph_path=self.graph_path,
        )

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    # ------------------------------------------------------------------
    # 数据查询构造器
    # ------------------------------------------------------------------

    def get_overview(self) -> dict[str, Any]:
        kit = self.toolkit()
        graph = json.loads(self.graph_path.read_text(encoding="utf-8"))
        with OntologyClient(self.db_path) as client:
            cq1 = client.query_cq1_vip_overdue_unshipped()
            cq2 = client.query_cq2_customer_360()
            cq3 = client.query_cq3_category_sales_and_audit()
            cq4 = client.query_cq4_data_quality_audit()
        ledger = kit.engine.read_ledger()
        return {
            "mode": self.mode,
            "metadata": graph["metadata"],
            "summary": graph["summary"],
            "cq1": cq1,
            "cq2": cq2,
            "cq3": cq3,
            "cq4": cq4,
            "ledger": ledger,
            "mcp_tools": kit.export_tool_schemas(),
        }

    def get_graph_view(self, preset: str = "core") -> dict[str, Any]:
        """构造适合交互式 Canvas 力导向布局的节点与边集合。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            # 统计每个对象挂载的 DQ 异常数与最高级别
            dq_meta: dict[tuple[str, str], dict[str, Any]] = {}
            sev_rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
            for r in conn.execute(
                "SELECT target_object_type, target_object_id, severity, rule_code FROM data_quality_issues"
            ):
                key = (r["target_object_type"], r["target_object_id"])
                info = dq_meta.setdefault(key, {"count": 0, "max_severity": None, "rules": []})
                info["count"] += 1
                info["rules"].append(r["rule_code"])
                cur = info["max_severity"]
                if cur is None or sev_rank[r["severity"]] > sev_rank[cur]:
                    info["max_severity"] = r["severity"]

            nodes: dict[str, dict[str, Any]] = {}

            def add_node(obj_type: str, obj_id: str, label: str, subtitle: str, badge: str = "") -> str:
                uid = f"{obj_type}:{obj_id}"
                if uid not in nodes:
                    dq = dq_meta.get((obj_type, obj_id), {"count": 0, "max_severity": None, "rules": []})
                    nodes[uid] = {
                        "uid": uid,
                        "type": obj_type,
                        "id": obj_id,
                        "label": label,
                        "subtitle": subtitle,
                        "badge": badge,
                        "dq_count": dq["count"],
                        "dq_severity": dq["max_severity"],
                        "dq_rules": dq["rules"],
                    }
                return uid

            def load_owners() -> None:
                for r in conn.execute("SELECT * FROM sales_owners"):
                    add_node("SalesOwner", r["owner_id"], r["owner_name"], r["team"], r["owner_id"])

            def load_customers(ids: set[str] | None = None) -> None:
                for r in conn.execute("SELECT * FROM customers"):
                    if ids is not None and r["customer_id"] not in ids:
                        continue
                    aliases = json.loads(r["aliases_json"])
                    short = aliases[0] if aliases else r["canonical_name"]
                    add_node(
                        "Customer",
                        r["customer_id"],
                        short,
                        f"{r['canonical_name']} · {r['tier']} · {r['resolution_method']}",
                        r["tier"],
                    )

            def load_categories() -> None:
                for r in conn.execute("SELECT * FROM product_categories"):
                    add_node(
                        "ProductCategory",
                        r["category_code"],
                        r["category_name"],
                        f"L{r['level']} · {'叶子' if r['is_leaf'] else '一级大类'}",
                        r["category_code"],
                    )

            def load_products() -> None:
                for r in conn.execute("SELECT * FROM products"):
                    add_node(
                        "Product",
                        r["sku"],
                        r["product_name"].split()[0],
                        f"{r['sku']} · {r['category_status']}",
                        r["category_status"],
                    )

            def load_orders(order_nos: set[str] | None = None) -> set[str]:
                included: set[str] = set()
                for r in conn.execute("SELECT * FROM sales_orders"):
                    if order_nos is not None and r["order_no"] not in order_nos:
                        continue
                    included.add(r["order_no"])
                    add_node(
                        "SalesOrder",
                        r["order_no"],
                        r["order_no"],
                        f"ERP={r['erp_status']} / WMS={r['fulfillment_status']} · ¥{float(r['total_amount']):,.0f}",
                        "OVERDUE" if r["is_overdue_unshipped"] else r["fulfillment_status"],
                    )
                return included

            def load_shipments(for_orders: set[str] | None = None, include_dangling: bool = True) -> None:
                for r in conn.execute("SELECT * FROM shipments"):
                    if r["is_dangling"]:
                        if include_dangling:
                            add_node(
                                "Shipment",
                                r["shipment_id"],
                                r["shipment_id"],
                                f"孤立发货单 -> {r['normalized_order_no']} ({r['shipment_status']})",
                                "DANGLING",
                            )
                        continue
                    if for_orders is not None and r["fulfills_order_no"] not in for_orders:
                        continue
                    add_node(
                        "Shipment",
                        r["shipment_id"],
                        r["shipment_id"],
                        f"{r['shipment_status']} · {r['carrier']} ({r['ship_date']})",
                        r["shipment_status"],
                    )

            if preset == "vip_overdue":
                # CQ1 4 张真单 + 双陷阱单 905 + 缺承诺日 910 + 大客户 909
                focus_orders = {
                    r["order_no"]
                    for r in conn.execute(
                        """
                        SELECT o.order_no FROM sales_orders o
                        JOIN customers c ON o.placed_by_customer_id = c.customer_id
                        WHERE (c.is_vip = 1 AND o.order_date >= '2026-10-25' AND o.is_overdue_unshipped = 1)
                           OR o.order_no IN ('SO-20261105-905', 'SO-20261108-906', 'SO-20261109-909', 'SO-20261111-910')
                        """
                    )
                }
                cust_ids = {
                    r["placed_by_customer_id"]
                    for r in conn.execute(
                        f"SELECT DISTINCT placed_by_customer_id FROM sales_orders WHERE order_no IN ({','.join('?' * len(focus_orders))})",
                        tuple(focus_orders),
                    )
                }
                load_owners()
                load_customers(cust_ids)
                loaded_orders = load_orders(focus_orders)
                load_shipments(loaded_orders, include_dangling=False)

            elif preset == "customer_er":
                load_owners()
                load_customers()

            elif preset == "product_tree":
                load_categories()
                load_products()

            elif preset == "dq_conflicts":
                # 全部有 DQ 异常的节点 + 关键关联节点
                dq_orders = {
                    r["target_object_id"]
                    for r in conn.execute(
                        "SELECT target_object_id FROM data_quality_issues WHERE target_object_type = 'SalesOrder'"
                    )
                }
                dq_ship_orders = {
                    r["fulfills_order_no"]
                    for r in conn.execute(
                        """
                        SELECT s.fulfills_order_no FROM shipments s
                        JOIN data_quality_issues dq ON dq.target_object_type='Shipment' AND dq.target_object_id=s.shipment_id
                        WHERE s.fulfills_order_no IS NOT NULL
                        LIMIT 6
                        """
                    )
                }
                focus_orders = dq_orders | dq_ship_orders
                load_owners()
                load_customers()
                load_categories()
                load_products()
                loaded_orders = load_orders(focus_orders)
                load_shipments(loaded_orders, include_dangling=True)

            elif preset == "all_orders":
                load_owners()
                load_customers()
                load_categories()
                load_products()
                loaded_orders = load_orders()
                load_shipments(loaded_orders, include_dangling=True)

            else:  # preset == "core"
                focus_orders = {
                    r["order_no"]
                    for r in conn.execute(
                        """
                        SELECT order_no FROM sales_orders
                        WHERE order_no LIKE '%-9__'
                           OR is_overdue_unshipped = 1
                           OR order_no IN (
                               SELECT target_object_id FROM data_quality_issues WHERE target_object_type = 'SalesOrder'
                           )
                        """
                    )
                }
                load_owners()
                load_customers()
                load_categories()
                load_products()
                loaded_orders = load_orders(focus_orders)
                load_shipments(loaded_orders, include_dangling=True)

            # 只提取两端都在当前节点集合中的显式语义边（跳过 OrderLine 避免视觉噪音）
            edges: list[dict[str, str]] = []
            for r in conn.execute(
                "SELECT link_type, from_type, from_id, to_type, to_id FROM links WHERE from_type != 'OrderLine'"
            ):
                src = f"{r['from_type']}:{r['from_id']}"
                dst = f"{r['to_type']}:{r['to_id']}"
                if src in nodes and dst in nodes:
                    edges.append(
                        {
                            "source": src,
                            "target": dst,
                            "link_type": r["link_type"],
                        }
                    )

            return {
                "preset": preset,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "nodes": list(nodes.values()),
                "edges": edges,
            }
        finally:
            conn.close()

    def get_node_detail(self, obj_type: str, obj_id: str) -> dict[str, Any]:
        if obj_type not in TABLE_BY_TYPE:
            raise ValueError(f"未知对象类型：{obj_type}")
        table, pk = TABLE_BY_TYPE[obj_type]
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(f"SELECT * FROM {table} WHERE {pk} = ?", (obj_id,)).fetchone()
            if row is None:
                raise KeyError(f"对象不存在：{obj_type}:{obj_id}")
            props = dict(row)
        finally:
            conn.close()

        with OntologyClient(self.db_path) as client:
            nb = client.get_object_neighborhood(obj_type, obj_id)
            cq1_explain = client.explain_cq1_decision(obj_id) if obj_type == "SalesOrder" else None

        return {
            "object_type": obj_type,
            "object_id": obj_id,
            "properties": props,
            "neighborhood": nb,
            "cq1_explain": cq1_explain,
        }

    def get_w3c_asset(self, asset: str) -> dict[str, Any]:
        if not (self.w3c_dir / "ontology_tbox.ttl").exists():
            export_all_w3c(db_path=self.db_path, out_dir=self.w3c_dir)

        mapping = {
            "tbox": (self.w3c_dir / "ontology_tbox.ttl", "W3C OWL 2 / RDFS TBox (ontology_tbox.ttl)", "turtle"),
            "shacl": (self.w3c_dir / "shapes_shacl.ttl", "W3C SHACL 1.0 Constraints (shapes_shacl.ttl)", "turtle"),
            "abox_sample": (self.w3c_dir / "instances_abox.ttl", "W3C RDF 1.1 ABox 节选 (前 180 行)", "turtle"),
            "cq1": (ROOT_DIR / "ontology" / "w3c" / "queries" / "cq1_vip_overdue.rq", "SPARQL 1.1 - CQ1 VIP 逾期预警", "sparql"),
            "cq2": (ROOT_DIR / "ontology" / "w3c" / "queries" / "cq2_customer_360.rq", "SPARQL 1.1 - CQ2 客户 360°", "sparql"),
            "cq3": (ROOT_DIR / "ontology" / "w3c" / "queries" / "cq3_category_sales.rq", "SPARQL 1.1 - CQ3 分类树传递闭包聚合", "sparql"),
            "cq4": (ROOT_DIR / "ontology" / "w3c" / "queries" / "cq4_dq_audit.rq", "SPARQL 1.1 - CQ4 数据质量审计", "sparql"),
            "mcp_config": (ROOT_DIR / "mcp_server" / "mcp_config.example.json", "MCP Client 标准挂载配置 (stdio JSON-RPC 2.0)", "json"),
        }
        if asset not in mapping:
            raise ValueError(f"不支持的资源标识：{asset}")
        path, title, lang = mapping[asset]
        text = path.read_text(encoding="utf-8")
        if asset == "abox_sample":
            all_lines = text.splitlines()
            text = "\n".join(all_lines[:180]) + f"\n\n# ...（共 {len(all_lines)} 行，完整文件见 data/w3c/instances_abox.ttl）\n"
        return {"asset": asset, "title": title, "language": lang, "content": text}


def create_console_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    mode: str = "sandbox",
    llm_config_path: Path = LOCAL_CONFIG_PATH,
) -> tuple[ThreadingHTTPServer, ConsoleContext]:
    ctx = ConsoleContext(mode=mode, llm_config_path=llm_config_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # 保持终端清爽，仅在需要时排查
            pass

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "File not found")
                return
            mime, _ = mimetypes.guess_type(str(path))
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            path = parsed.path

            try:
                if path == "/api/overview":
                    with ctx.lock:
                        self._send_json(200, ctx.get_overview())
                    return
                if path == "/api/graph":
                    preset = (qs.get("preset") or ["core"])[0]
                    with ctx.lock:
                        self._send_json(200, ctx.get_graph_view(preset=preset))
                    return
                if path == "/api/node":
                    obj_type = (qs.get("type") or [""])[0]
                    obj_id = (qs.get("id") or [""])[0]
                    with ctx.lock:
                        self._send_json(200, ctx.get_node_detail(obj_type, obj_id))
                    return
                if path == "/api/customers":
                    q = (qs.get("q") or [""])[0]
                    with ctx.lock:
                        res = ctx.toolkit().call_tool("get_customer_360", {"customer_query": q})
                        self._send_json(200, res)
                    return
                if path == "/api/dq":
                    args: dict[str, Any] = {}
                    if qs.get("severity") and qs["severity"][0]:
                        args["severity"] = qs["severity"][0]
                    if qs.get("rule_code") and qs["rule_code"][0]:
                        args["rule_code"] = qs["rule_code"][0]
                    with ctx.lock:
                        res = ctx.toolkit().call_tool("list_data_quality_issues", args)
                        self._send_json(200, res)
                    return
                if path == "/api/ledger":
                    with ctx.lock:
                        self._send_json(200, {"entries": ctx.toolkit().engine.read_ledger()})
                    return
                if path == "/api/w3c/preview":
                    asset = (qs.get("asset") or ["tbox"])[0]
                    with ctx.lock:
                        self._send_json(200, ctx.get_w3c_asset(asset))
                    return
                if path == "/api/agent/presets":
                    with ctx.lock:
                        self._send_json(
                            200,
                            {
                                "presets": PRESET_TASKS,
                                "sessions": ctx.agent_runner.list_sessions(),
                                "llm_config": load_llm_config(local_config_path=ctx.llm_config_path).to_public_dict(),
                            },
                        )
                    return
                if path == "/api/agent/config":
                    with ctx.lock:
                        self._send_json(
                            200,
                            {
                                "ok": True,
                                "config": load_llm_config(local_config_path=ctx.llm_config_path).to_public_dict(),
                            },
                        )
                    return
                if path == "/api/agent/sessions":
                    with ctx.lock:
                        self._send_json(200, {"sessions": ctx.agent_runner.list_sessions()})
                    return
                if path == "/api/arena/benchmarks":
                    sandbox = RawSQLiteSandbox()
                    try:
                        ddl = sandbox.ddl_text
                    finally:
                        sandbox.close()
                    cached = None
                    cache_file = ROOT_DIR / "data" / "ontology" / "arena_benchmark_result.json"
                    if cache_file.exists():
                        try:
                            cached = json.loads(cache_file.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            cached = None
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "benchmarks": ARENA_BENCHMARKS,
                            "raw_ddl": ddl,
                            "cached_report": cached,
                        },
                    )
                    return

                # 静态资源路由
                if path in {"/", ""}:
                    self._send_file(WEB_DIR / "index.html")
                    return
                rel = path.lstrip("/")
                target = (WEB_DIR / rel).resolve()
                if not str(target).startswith(str(WEB_DIR.resolve())):
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                self._send_file(target)
            except KeyError as exc:
                self._send_json(404, {"ok": False, "message": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "message": str(exc)})
            except Exception as exc:
                self._send_json(500, {"ok": False, "message": str(exc)})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw_body.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                self._send_json(400, {"ok": False, "message": f"Invalid JSON: {exc}"})
                return

            try:
                if path == "/api/sandbox/reset":
                    with ctx.lock:
                        self._send_json(200, ctx.reset_sandbox())
                    return
                if path == "/api/agent/config":
                    with ctx.lock:
                        saved = save_llm_config(body, local_config_path=ctx.llm_config_path)
                        self._send_json(200, {"ok": True, "config": saved.to_public_dict()})
                    return
                if path == "/api/agent/ping":
                    with ctx.lock:
                        cfg = load_llm_config(override=body, local_config_path=ctx.llm_config_path)
                    # 网络探活在锁外执行，避免阻塞其它接口
                    self._send_json(200, ping_llm(cfg))
                    return
                if path == "/api/agent/run":
                    with ctx.lock:
                        session = ctx.agent_runner.start_run(
                            task=str(body.get("task") or ""),
                            engine_mode=str(body.get("engine_mode") or "real_llm"),
                            require_approval=bool(body.get("require_approval", True)),
                            llm_config=body.get("llm_config") or {},
                        )
                        self._send_json(200, {"ok": True, "session": session})
                    return
                if path == "/api/agent/approve":
                    with ctx.lock:
                        session = ctx.agent_runner.resolve_approval(
                            session_id=str(body.get("session_id") or ""),
                            approved=bool(body.get("approved", False)),
                            reviewer_note=str(body.get("reviewer_note") or ""),
                        )
                        self._send_json(200, {"ok": True, "session": session})
                    return
                if path == "/api/arena/run":
                    question = str(body.get("question") or "").strip()
                    if not question:
                        self._send_json(400, {"ok": False, "message": "问题 question 不能为空"})
                        return
                    benchmark_id = body.get("benchmark_id")
                    llm_override = body.get("llm_config") or {}
                    # 在独立沙箱副本上执行同题 PK，不阻塞主控制台锁、不污染当前沙箱状态
                    iso_ctx = ConsoleContext(mode="sandbox", llm_config_path=ctx.llm_config_path)
                    try:
                        def runner_factory() -> OntologyAgentLoopRunner:
                            return OntologyAgentLoopRunner(
                                mcp_server_factory=iso_ctx.mcp_server,
                                local_config_path=iso_ctx.llm_config_path,
                            )

                        result = run_head_to_head_arena(
                            question=question,
                            agent_runner_factory=runner_factory,
                            benchmark_id=benchmark_id,
                            llm_override=llm_override,
                            local_config_path=ctx.llm_config_path,
                        )
                        self._send_json(200, result)
                    finally:
                        iso_ctx.close()
                    return
                if path.startswith("/api/actions/"):
                    tool_name = path.split("/api/actions/", 1)[1]
                    if tool_name not in {
                        "escalate_overdue_order",
                        "fix_product_category",
                        "reconcile_customer_identity",
                    }:
                        self._send_json(404, {"ok": False, "message": f"未知动作：{tool_name}"})
                        return
                    with ctx.lock:
                        res = ctx.toolkit().call_tool(tool_name, body)
                        if res["ok"] and body.get("rebuild") and not body.get("dry_run"):
                            export_all_w3c(db_path=ctx.db_path, out_dir=ctx.w3c_dir)
                        self._send_json(200, res)
                    return
                self._send_json(404, {"ok": False, "message": f"Unknown endpoint: {path}"})
            except KeyError as exc:
                self._send_json(404, {"ok": False, "message": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "message": str(exc)})
            except Exception as exc:
                self._send_json(500, {"ok": False, "message": str(exc)})

    server = ThreadingHTTPServer((host, port), Handler)
    return server, ctx


def main() -> None:
    parser = argparse.ArgumentParser(description="交互式 Operational Ontology 图谱与控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", choices=["sandbox", "live"], default="sandbox")
    args = parser.parse_args()

    server, ctx = create_console_server(host=args.host, port=args.port, mode=args.mode)
    url = f"http://{args.host}:{args.port}"
    print(f"Operational Ontology 控制台已启动：{url} （模式：{args.mode}）")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务...")
    finally:
        server.server_close()
        ctx.close()


if __name__ == "__main__":
    main()
