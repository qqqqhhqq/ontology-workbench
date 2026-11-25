#!/usr/bin/env python3
"""阶段五自动化测试：覆盖 ActionEngine 守卫、治理闭环重建与 AI Agent 语义工具层。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.actions import ActionEngine, PreconditionError
from ontology.agent_tools import OntologyAgentToolkit


class TestOperationalActionsAndAgentTools(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ontology_test_")
        self.tmp_dir = Path(self._tmp.name)
        self.db_path = self.tmp_dir / "ontology.db"
        self.graph_path = self.tmp_dir / "ontology_graph.json"
        self.ledger_path = self.tmp_dir / "action_ledger.jsonl"
        self.crosswalk_path = self.tmp_dir / "customer_crosswalk.yaml"
        self.overrides_path = self.tmp_dir / "product_category_overrides.yaml"

        shutil.copy2(ROOT_DIR / "data" / "ontology" / "ontology.db", self.db_path)
        shutil.copy2(ROOT_DIR / "ontology" / "crosswalk" / "customer_crosswalk.yaml", self.crosswalk_path)
        shutil.copy2(ROOT_DIR / "ontology" / "overrides" / "product_category_overrides.yaml", self.overrides_path)

        self.engine = ActionEngine(
            db_path=self.db_path,
            ledger_path=self.ledger_path,
            crosswalk_path=self.crosswalk_path,
            overrides_path=self.overrides_path,
            graph_path=self.graph_path,
        )
        self.kit = OntologyAgentToolkit(
            db_path=self.db_path,
            ledger_path=self.ledger_path,
            crosswalk_path=self.crosswalk_path,
            overrides_path=self.overrides_path,
            graph_path=self.graph_path,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 1. EscalateOverdueOrder 前置守卫、dry_run、提交与幂等防重
    # ------------------------------------------------------------------

    def test_escalate_overdue_order_guards_and_commit(self) -> None:
        # 拒绝已有效发货的双陷阱单 SO-20261105-905
        with self.assertRaises(PreconditionError):
            self.engine.escalate_overdue_order("SO-20261105-905", reason="误催办测试")

        # 拒绝已取消订单 SO-20261108-906 与未逾期订单 SO-20261118-907
        with self.assertRaises(PreconditionError):
            self.engine.escalate_overdue_order("SO-20261108-906", reason="已取消")
        with self.assertRaises(PreconditionError):
            self.engine.escalate_overdue_order("SO-20261118-907", reason="未逾期")

        # dry_run 成功但不写入审计账本
        dry = self.engine.escalate_overdue_order(
            "SO-20261103-901", reason="VIP 逾期13天", priority="P1", dry_run=True
        )
        self.assertEqual(dry["status"], "DRY_RUN_OK")
        self.assertIn("王磊", dry["side_effects"]["notify_sales_owner"])
        self.assertEqual(self.engine.read_ledger(), [])

        # 正式提交成功并写审计账本
        committed = self.engine.escalate_overdue_order(
            "SO-20261103-901", reason="VIP 逾期13天", priority="P1"
        )
        self.assertEqual(committed["status"], "COMMITTED")
        self.assertEqual(len(self.engine.read_ledger()), 1)

        # 幂等防重拦截
        with self.assertRaises(PreconditionError):
            self.engine.escalate_overdue_order("SO-20261103-901", reason="重复催办")

    # ------------------------------------------------------------------
    # 2. FixProductCategory 守卫 + 写回覆盖层 + 重建本体闭环验证
    # ------------------------------------------------------------------

    def test_fix_product_category_closed_loop_rebuild(self) -> None:
        # 拒绝修改原本已合规的 P-1001
        with self.assertRaises(PreconditionError):
            self.engine.fix_product_category("P-1001", "0101", "steward", "不需要改")

        # 拒绝挂到非叶子节点 01 或未知码 0104
        with self.assertRaises(PreconditionError):
            self.engine.fix_product_category("P-1012", "01", "steward", "挂非叶子")
        with self.assertRaises(PreconditionError):
            self.engine.fix_product_category("P-1012", "0104", "steward", "挂未知码")

        before_cq3 = self.kit.call_tool("get_category_sales_and_audit")["data"]
        before_map = {c["root_category_code"]: c["total_amount"] for c in before_cq3["root_category_summary"]}

        # 合法修复 P-1012 -> 0101(打印机) 并重建本体
        res = self.engine.fix_product_category(
            sku="P-1012",
            target_leaf_category_code="0101",
            reviewer="mdm-steward",
            reason="确认属于打印机叶子分类",
            rebuild=True,
        )
        self.assertTrue(res["side_effects"]["rebuilt_ontology"])

        after_cq3 = self.kit.call_tool("get_category_sales_and_audit")["data"]
        after_map = {c["root_category_code"]: c["total_amount"] for c in after_cq3["root_category_summary"]}

        # 金额守恒且 P-1012 的 54,417.15 从 UNCLASSIFIED 精准转移到 01 办公设备
        self.assertTrue(after_cq3["reconciliation"]["is_balanced"])
        self.assertAlmostEqual(after_map["01"] - before_map["01"], 54417.15, places=2)
        self.assertAlmostEqual(before_map["UNCLASSIFIED"] - after_map["UNCLASSIFIED"], 54417.15, places=2)

        # DQ-007 彻底清零，总异常从 38 降至 37
        dq_after = self.kit.call_tool("list_data_quality_issues")["data"]
        self.assertEqual(dq_after["total_issues"], 37)
        dq007 = self.kit.call_tool("list_data_quality_issues", {"rule_code": "DQ-007"})["data"]
        self.assertEqual(dq007, [])

    # ------------------------------------------------------------------
    # 3. ReconcileCustomerIdentity 强安全守卫 + 合法合并闭环
    # ------------------------------------------------------------------

    def test_reconcile_customer_identity_guards_and_closed_loop(self) -> None:
        # 守卫 1：已绑定不同实体的禁止改绑（云帆科技 vs 新云帆网络）
        with self.assertRaises(PreconditionError):
            self.engine.reconcile_customer_identity("ACC-1001", "C0008", "steward", "错误改绑")

        # 守卫 2：两个单边法人信用代码冲突，严禁错并（启明医疗 vs 嘉禾物流）
        with self.assertRaises(PreconditionError) as ctx_credit:
            self.engine.reconcile_customer_identity("ACC-1011", "C0011", "steward", "信用代码冲突测试")
        self.assertIn("信用代码冲突", str(ctx_credit.exception))

        # 守卫 3：主体类型冲突，严禁把个人与企业合并（李四 vs 嘉禾物流）
        with self.assertRaises(PreconditionError) as ctx_type:
            self.engine.reconcile_customer_identity("ACC-1012", "C0011", "steward", "主体类型冲突测试")
        self.assertIn("主体类型冲突", str(ctx_type.exception))

        # 合法场景：在沙箱内确认两个无信用代码的单边个人记录（ACC-1012 & C0012）为同一人并重建
        res = self.engine.reconcile_customer_identity(
            crm_account_id="ACC-1012",
            erp_customer_no="C0012",
            reviewer="sales-steward",
            rationale="经核实个人客户手机与收货地址一致",
            rebuild=True,
        )
        self.assertTrue(res["side_effects"]["rebuilt_ontology"])

        # 重建后总客户数从 14 合并为 13，且 DQ-012 单边异常从 4 条降为 2 条
        customers_after = self.kit.call_tool("get_customer_360")["data"]
        self.assertEqual(len(customers_after), 13)
        dq012 = self.kit.call_tool("list_data_quality_issues", {"rule_code": "DQ-012"})["data"]
        self.assertEqual(len(dq012), 2)

    # ------------------------------------------------------------------
    # 4. AI Agent 语义工具集与 Schema 校验
    # ------------------------------------------------------------------

    def test_agent_toolkit_schemas_and_alias_search(self) -> None:
        schemas = self.kit.export_tool_schemas()
        self.assertEqual(len(schemas), 9)
        for s in schemas:
            self.assertIn("name", s)
            self.assertIn("description", s)
            self.assertIn("input_schema", s)

        # 通过更名前旧名“博思咨询”能直接查到“远东管理咨询有限公司”
        res = self.kit.call_tool("get_customer_360", {"customer_query": "博思咨询"})
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["data"]), 1)
        self.assertEqual(res["data"][0]["canonical_name"], "远东管理咨询有限公司")

        # 守卫失败时工具层返回结构化错误信封而非崩溃
        err = self.kit.call_tool(
            "escalate_overdue_order", {"order_no": "SO-20261105-905", "reason": "尝试误催办"}
        )
        self.assertFalse(err["ok"])
        self.assertEqual(err["error_type"], "PreconditionError")


if __name__ == "__main__":
    unittest.main(verbosity=2)
