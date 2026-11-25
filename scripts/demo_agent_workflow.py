#!/usr/bin/env python3
"""阶段五端到端演示：操作型本体（Operational Ontology）与 AI Agent 受控闭环。

本演示在隔离的临时沙箱中执行全部写操作与本体重建，不污染基线数据文件，可反复运行。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.agent_tools import OntologyAgentToolkit


def main() -> None:
    schemas = OntologyAgentToolkit.export_tool_schemas()
    read_tools = [t["name"] for t in schemas if t["mode"] == "read"]
    action_tools = [t["name"] for t in schemas if t["mode"] == "action"]

    print("=" * 78)
    print("阶段五演示：Operational Ontology + AI Agent 语义工具闭环")
    print("=" * 78)
    print(f"已导出标准工具 Schema 共 {len(schemas)} 个：")
    print(f"  - 只读语义查询 ({len(read_tools)}): {', '.join(read_tools)}")
    print(f"  - 受控业务动作 ({len(action_tools)}): {', '.join(action_tools)}")

    with tempfile.TemporaryDirectory(prefix="ontology_demo_") as tmp:
        tmp_dir = Path(tmp)
        db_copy = tmp_dir / "ontology.db"
        graph_copy = tmp_dir / "ontology_graph.json"
        ledger_copy = tmp_dir / "action_ledger.jsonl"
        crosswalk_copy = tmp_dir / "customer_crosswalk.yaml"
        overrides_copy = tmp_dir / "product_category_overrides.yaml"

        shutil.copy2(ROOT_DIR / "data" / "ontology" / "ontology.db", db_copy)
        shutil.copy2(ROOT_DIR / "ontology" / "crosswalk" / "customer_crosswalk.yaml", crosswalk_copy)
        shutil.copy2(ROOT_DIR / "ontology" / "overrides" / "product_category_overrides.yaml", overrides_copy)

        kit = OntologyAgentToolkit(
            db_path=db_copy,
            ledger_path=ledger_copy,
            crosswalk_path=crosswalk_copy,
            overrides_path=overrides_copy,
            graph_path=graph_copy,
        )

        # ---- 场景 1：VIP 履约巡检 + 防误催办守卫 + 合法催办 + 幂等防重 ----
        print("\n[场景 1] AI Agent 履约巡检与受控催办闭环")
        res_cq1 = kit.call_tool("find_vip_overdue_orders", {"window_days": 30})
        orders = res_cq1["data"]
        print(f"  1) 调用 find_vip_overdue_orders -> 发现 {len(orders)} 张 VIP 逾期未发订单")

        # 模拟 AI 误尝试催办 ERP 显示 A 但 WMS 已出库的 SO-20261105-905
        res_bad = kit.call_tool(
            "escalate_overdue_order",
            {"order_no": "SO-20261105-905", "reason": "ERP显示待发货，尝试催办", "priority": "P1"},
        )
        print(f"  2) 误催办 SO-20261105-905 -> ok={res_bad['ok']} | 守卫拦截：{res_bad['message']}")

        # 对真正逾期的 SO-20261112-904（星辰创意设计有限公司）先 dry_run 再正式提交
        res_dry = kit.call_tool(
            "escalate_overdue_order",
            {
                "order_no": "SO-20261112-904",
                "reason": "VIP客户订单已逾期4天且WMS无发货单，申请加急出库",
                "priority": "P1",
                "dry_run": True,
            },
        )
        print(
            f"  3) dry_run 预演 SO-20261112-904 -> status={res_dry['data']['status']} | "
            f"将通知={res_dry['data']['side_effects']['notify_sales_owner']}"
        )

        res_commit = kit.call_tool(
            "escalate_overdue_order",
            {
                "order_no": "SO-20261112-904",
                "reason": "VIP客户订单已逾期4天且WMS无发货单，申请加急出库",
                "priority": "P1",
            },
        )
        print(
            f"  4) 正式提交催办 SO-20261112-904 -> action_id={res_commit['data']['action_id']} | "
            f"工单={res_commit['data']['side_effects']['wms_ticket_id']} | "
            f"已通知={res_commit['data']['side_effects']['notify_sales_owner']}"
        )

        # 重复催办拦截
        res_dup = kit.call_tool(
            "escalate_overdue_order",
            {"order_no": "SO-20261112-904", "reason": "再次尝试催办"},
        )
        print(f"  5) 重复催办 SO-20261112-904 -> ok={res_dup['ok']} | 幂等拦截：{res_dup['message']}")

        # ---- 场景 2：用更名前旧名检索客户 360° ----
        print("\n[场景 2] 销售用旧名“博思咨询”询问 AI 客户全景")
        res_alias = kit.call_tool("get_customer_360", {"customer_query": "博思咨询"})
        c = res_alias["data"][0]
        print(
            f"  命中 {c['customer_id']} | 规范名={c['canonical_name']} | 别名={c['aliases']} | "
            f"对齐方式={c['resolution_method']} | 有效单={c['valid_order_count']} | "
            f"金额=¥{c['valid_order_amount']:,.2f} | 准时率={c['on_time_rate_pct']}%"
        )

        # ---- 场景 3：主数据治理闭环（守卫拦截 + 合法修复 + 重建本体 + 指标前后对比）----
        print("\n[场景 3] 主数据治理闭环（异常发现 -> 守卫拦截 -> 修复写回 -> 重建本体验证）")
        # 3a) 拦截把不同信用代码的单边法人强行合并
        res_bad_merge = kit.call_tool(
            "reconcile_customer_identity",
            {
                "crm_account_id": "ACC-1011",
                "erp_customer_no": "C0011",
                "reviewer": "ops-admin",
                "rationale": "误把启明医疗和嘉禾物流配对",
            },
        )
        print(f"  1) 尝试合并不同法人 ACC-1011 & C0011 -> ok={res_bad_merge['ok']} | 拦截：{res_bad_merge['message']}")

        # 3b) 拦截把 P-1012 挂到非叶子节点 01
        res_bad_cat = kit.call_tool(
            "fix_product_category",
            {
                "sku": "P-1012",
                "target_leaf_category_code": "01",
                "reviewer": "mdm-steward",
                "reason": "尝试挂到一级大类",
            },
        )
        print(f"  2) 尝试将 P-1012 挂到非叶子节点 01 -> ok={res_bad_cat['ok']} | 拦截：{res_bad_cat['message']}")

        # 3c) 记录修复前 CQ3 & CQ4 指标
        before_cq3 = {
            r["root_category_code"]: r["total_amount"]
            for r in kit.call_tool("get_category_sales_and_audit")["data"]["root_category_summary"]
        }
        before_dq_total = kit.call_tool("list_data_quality_issues")["data"]["total_issues"]

        # 3d) 合法修复 P-1012（标签打印机 BQ-50）-> 0101（打印机，合法叶子节点），并自动重建本体
        res_fix = kit.call_tool(
            "fix_product_category",
            {
                "sku": "P-1012",
                "target_leaf_category_code": "0101",
                "reviewer": "mdm-steward",
                "reason": "经商品经理核实，标签打印机 BQ-50 属于二级叶子分类 0101(打印机)",
                "rebuild": True,
            },
        )
        after_cq3 = {
            r["root_category_code"]: r["total_amount"]
            for r in kit.call_tool("get_category_sales_and_audit")["data"]["root_category_summary"]
        }
        after_dq_total = kit.call_tool("list_data_quality_issues")["data"]["total_issues"]
        dq007_after = kit.call_tool("list_data_quality_issues", {"rule_code": "DQ-007"})["data"]

        print(
            f"  3) 合法修复 P-1012 -> 0101(打印机) 并重建本体 -> action_id={res_fix['data']['action_id']}, "
            f"rebuilt={res_fix['data']['side_effects']['rebuilt_ontology']}"
        )
        print(f"     * 数据质量异常总数：{before_dq_total} -> {after_dq_total}（DQ-007 剩余 {len(dq007_after)} 条，已清零！）")
        print(
            f"     * 01 办公设备营收：¥{before_cq3['01']:,.2f} -> ¥{after_cq3['01']:,.2f} "
            f"(+¥{after_cq3['01'] - before_cq3['01']:,.2f})"
        )
        print(
            f"     * 未归类挂账金额：¥{before_cq3['UNCLASSIFIED']:,.2f} -> ¥{after_cq3['UNCLASSIFIED']:,.2f} "
            f"(-¥{before_cq3['UNCLASSIFIED'] - after_cq3['UNCLASSIFIED']:,.2f})"
        )

        # ---- 审计账本汇总 ----
        ledger = kit.engine.read_ledger()
        print(f"\n[审计账本] 本次沙箱共记录 {len(ledger)} 条不可变动作事件：")
        for entry in ledger:
            print(
                f"  - {entry['action_id']} | {entry['action_name']:<26} | "
                f"target={entry['target_object_type']}:{entry['target_object_id']} | "
                f"actor={entry['actor']} | status={entry['status']}"
            )


if __name__ == "__main__":
    main()
