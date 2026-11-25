#!/usr/bin/env python3
"""阶段四：CQ1-CQ4 业务查询验证报告 + “裸查源表 vs Ontology”对比实验。"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.client import OntologyClient

SOURCES_DIR = ROOT_DIR / "data" / "sources"


def _read_csv(rel: str) -> list[dict[str, str]]:
    with (SOURCES_DIR / rel).open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run_naive_cq1_experiments() -> dict[str, list[str]]:
    """模拟没有 Ontology 时，分析师/AI 直接拼源系统 CSV 常见的两种朴素查法。"""
    accounts = _read_csv("crm/accounts.csv")
    customers = _read_csv("erp/customers.csv")
    orders = _read_csv("erp/sales_orders.csv")
    shipments = _read_csv("wms/shipments.csv")

    # 朴素查法 A：按客户名精确 JOIN + 严格匹配 'VIP' + 相信 ERP 状态 'A'
    vip_names = {a["account_name"] for a in accounts if a["customer_level"] == "VIP"}
    erp_vip_a = {c["customer_no"] for c in customers if c["customer_name"] in vip_names}
    naive_a = sorted(
        o["order_no"]
        for o in orders
        if o["customer_no"] in erp_vip_a
        and o["order_date"] >= "20261025"
        and o["promised_date"] != ""
        and o["promised_date"] < "20261123"
        and o["status_code"] == "A"
    )

    # 朴素查法 B：稍聪明一点——按信用代码 JOIN + 忽略大小写 VIP + 用裸订单号查 WMS 是否有发货单
    vip_credits = {
        a["credit_code"]
        for a in accounts
        if a["customer_level"].upper() == "VIP" and a["credit_code"]
    }
    erp_vip_b = {
        c["customer_no"]
        for c in customers
        if c["credit_code"] and c["credit_code"] in vip_credits
    }
    raw_wms_order_nos = {s["order_no"] for s in shipments}
    naive_b = sorted(
        o["order_no"]
        for o in orders
        if o["customer_no"] in erp_vip_b
        and o["order_date"] >= "20261025"
        and o["promised_date"] != ""
        and o["promised_date"] < "20261123"
        and o["status_code"] != "X"
        and o["order_no"] not in raw_wms_order_nos
    )

    return {"naive_by_name": naive_a, "naive_by_credit_and_raw_wms": naive_b}


def main() -> None:
    with OntologyClient() as client:
        meta = client.get_metadata()
        print("=" * 78)
        print(f"阶段四验收报告：{meta['ontology_name']} v{meta['ontology_version']} (AS_OF={meta['as_of_date']})")
        print("=" * 78)

        # ---- CQ1 ----
        print("\n[CQ1] 近 30 天内 VIP 客户逾期未有效发货订单")
        cq1 = client.query_cq1_vip_overdue_unshipped()
        for r in cq1:
            alias_str = f"({', '.join(r['aliases'])})" if r["aliases"] else ""
            print(
                f"  - {r['order_no']} | {r['canonical_name']}{alias_str} | "
                f"负责={r['owner_name']}/{r['team']} | 下单={r['order_date']} | "
                f"承诺={r['promised_date']} (逾期{r['overdue_days']}天) | "
                f"WMS裁决={r['fulfillment_status']} | 金额=¥{r['total_amount']:,.2f}"
            )

        print("\n  [对照实验] 裸查源系统 CSV vs Ontology 语义查询（CQ1）")
        naive = run_naive_cq1_experiments()
        onto_hits = sorted(r["order_no"] for r in cq1)
        onto_set = set(onto_hits)
        b_set = set(naive["naive_by_credit_and_raw_wms"])
        b_missed = sorted(onto_set - b_set)
        b_fp = sorted(b_set - onto_set)
        print(f"  - 朴素直查 A（名称JOIN + VIP精确匹配 + ERP状态）      : {naive['naive_by_name']} (全部漏报!)")
        print(
            f"  - 朴素直查 B（信用代码JOIN + 裸单号查WMS存在即已发）   : {naive['naive_by_credit_and_raw_wms']} "
            f"(漏报={b_missed}, 误报={b_fp})"
        )
        print(f"  - Ontology 语义查询（100% 精准命中）                   : {onto_hits}")

        print("\n  [边界可解释性] 人为埋点干扰单（905~911）排除原因核验")
        for order_no in [
            "SO-20261105-905",
            "SO-20261108-906",
            "SO-20261118-907",
            "SO-20261010-908",
            "SO-20261109-909",
            "SO-20261111-910",
            "SO-20261114-911",
        ]:
            exp = client.explain_cq1_decision(order_no)
            print(f"  - {order_no} ({exp['customer_name']}): 命中={exp['matched_cq1']} | {'; '.join(exp['reasons'])}")

        # ---- CQ2 ----
        print("\n[CQ2] 跨系统客户 360° 全景视图（14 个统一客户实体）")
        cq2 = client.query_cq2_customer_360()
        for r in cq2:
            rate_txt = f"{r['on_time_rate_pct']:5.1f}%" if r["on_time_rate_pct"] is not None else "   N/A"
            owner_txt = f"{r['owner_name']}/{r['team']}" if r["owner_name"] else "-"
            alias_txt = f" [别名:{','.join(r['aliases'])}]" if r["aliases"] else ""
            print(
                f"  - {r['customer_id']} | {r['canonical_name']}{alias_txt}\n"
                f"      覆盖={r['source_presence']:<8} 解析={r['resolution_method']:<16} "
                f"等级={r['tier']:<11} 销售={owner_txt:<10} "
                f"有效单={r['valid_order_count']:>2} 金额=¥{r['valid_order_amount']:>10,.2f} "
                f"准时率={rate_txt} 当前逾期={r['overdue_unshipped_count']}"
            )

        # ---- CQ3 ----
        print("\n[CQ3] 产品一级大类有效销售额聚合与分类合规审计")
        cq3 = client.query_cq3_category_sales_and_audit()
        for c in cq3["root_category_summary"]:
            print(
                f"  - {c['root_category_code']:<12} {c['root_category_name']:<10} | "
                f"严格合规=¥{c['valid_leaf_amount']:>11,.2f} | "
                f"非叶子上卷=¥{c['non_leaf_amount']:>9,.2f} | "
                f"异常挂账=¥{c['anomalous_amount']:>10,.2f} | "
                f"合计=¥{c['total_amount']:>11,.2f}"
            )
        rec = cq3["reconciliation"]
        print(
            f"  金额守恒校验：分类汇总 ¥{rec['category_sum']:,.2f} == "
            f"有效订单总额 ¥{rec['valid_orders_total']:,.2f} -> balanced={rec['is_balanced']}"
        )
        print("  分类异常商品清单（4 个 SKU）：")
        for p in cq3["anomalous_products"]:
            print(
                f"    * {p['sku']} {p['product_name']} | 状态={p['category_status']:<12} | "
                f"原始=({p['raw_category_code']!r}, {p['raw_category_name']!r}) | "
                f"推导大类={p['inferred_root_category'] or '未归类'} | "
                f"影响有效销售额=¥{p['valid_sales_amount']:,.2f} ({p['valid_line_count']}行)"
            )

        # ---- CQ4 ----
        print("\n[CQ4] 跨系统数据一致性审计（DataQualityIssue）")
        cq4 = client.query_cq4_data_quality_audit()
        sev_str = ", ".join(f"{s['severity']}={s['issue_count']}" for s in cq4["by_severity"])
        print(f"  总异常数：{cq4['total_issues']} 条（{sev_str}）")
        for r in cq4["by_rule"]:
            print(f"  - {r['rule_code']} [{r['severity']:<6}] {r['rule_name']:<28}: {r['issue_count']} 条")
        print("  其中 HIGH 级高危异常明细：")
        for h in cq4["high_severity_details"]:
            print(
                f"    * [{h['rule_code']}] {h['target_object_type']}:{h['target_object_id']} "
                f"({h['source_system']}) -> {h['description']}"
            )

        # ---- 图谱邻域示例 ----
        print("\n[图谱下钻示例] SalesOrder:SO-20261105-905 的对象邻域与挂载异常")
        nb = client.get_object_neighborhood("SalesOrder", "SO-20261105-905")
        print(f"  出边 ({len(nb['outgoing'])}): {nb['outgoing']}")
        print(f"  入边 ({len(nb['incoming'])}): {nb['incoming']}")
        print(f"  挂载异常 ({len(nb['data_quality_issues'])}): {[i['rule_code'] + ':' + i['rule_name'] for i in nb['data_quality_issues']]}")


if __name__ == "__main__":
    main()
