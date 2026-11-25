# 阶段五：操作型动作（Operational Actions）与 AI Agent 工具层

前面四个阶段完成了“只读语义层”（能看清业务真相、回答 CQ1–CQ4、暴露 38 条数据异常）。本阶段完成最后一块拼图——把静态本体升级为**可安全执行业务动作、可直接被 AI Agent / MCP 调用的操作型本体（Operational Ontology）**。

---

## 1. 本阶段核心产物

| 文件 | 角色 | 说明 |
| :--- | :--- | :--- |
| [actions.py](../ontology/actions.py) | 受控动作执行引擎 | 实现 `EscalateOverdueOrder`、`FixProductCategory`、`ReconcileCustomerIdentity` 三个动作，内置前置条件守卫、`dry_run` 预演、幂等防重与追加式审计账本 |
| [product_category_overrides.yaml](../ontology/overrides/product_category_overrides.yaml) | 产品分类治理覆盖层 | 不篡改原始 ERP CSV，以版本化覆盖表承接 `FixProductCategory` 的治理修复 |
| [agent_tools.py](../ontology/agent_tools.py) | AI Agent 语义工具层 | 封装 **6 个只读语义工具 + 3 个受控动作工具**，导出标准 JSON Schema（兼容 Function Calling / MCP），统一返回 `{ok, tool, data/error}` 结构化信封 |
| [demo_agent_workflow.py](../scripts/demo_agent_workflow.py) | 端到端闭环演示 | 在隔离临时沙箱内演示“履约巡检 → 防误催办 → 合法催办 → 别名查客户 → 主数据修复与重建闭环”，不污染基线数据 |
| [test_ontology_actions.py](../tests/test_ontology_actions.py) | 阶段五自动化测试 | 覆盖全部 3 个动作的守卫拦截、幂等性、真实写回与重建前后指标对账 |

---

## 2. 为什么 AI Agent 需要“操作型本体”，而不是直接给它 SQL 写权限？

```
                  [ AI Agent / 业务前端 / MCP Client ]
                                   │
             只暴露 9 个语义工具（6 Read + 3 Guarded Action）
                                   ▼
   ┌──────────────────────────────────────────────────────────────┐
   │             Operational Ontology（本体语义与动作层）            │
   │  • 读：统一实体、别名检索、WMS 事实源裁决、图谱邻域下钻             │
   │  • 写：前置条件守卫 + 幂等防重 + dry_run 预演 + 追加式审计账本      │
   └───────────────────┬──────────────────────────┬───────────────┘
                       ▼                          ▼
        [ 治理覆盖层（Crosswalk/Overrides） ]   [ 不可变审计账本 ]
                       │                          (action_ledger.jsonl)
                       ▼ 重新物化（build_ontology.py）
             [ 源系统 CSV 保持只读不可变（CRM / ERP / WMS） ]
```

在企业里直接让 LLM 写 SQL 查/改底层库，会同时面临**读不准**（阶段四对照实验已证明朴素直查准确率仅 50%）和**写不安全**（误催办已发货单、错并不同法人、改坏主数据）两大风险。操作型本体用四道防线解决这一问题：

1. **前置条件守卫（Precondition Guard）**：
   - `EscalateOverdueOrder` 强制校验 `SalesOrder.is_overdue_unshipped == true`——即使 ERP 还写着 `A`（如 `SO-20261105-905`），由于本体层已按 WMS 裁决为 `IN_TRANSIT`，误催办会被直接拦截。
   - `FixProductCategory` 强制校验目标分类必须是标准码表中真实存在的**叶子节点**（拦截挂到一级大类 `01` 或未知码 `0104`）。
   - `ReconcileCustomerIdentity` 强制校验**未绑定冲突、信用代码不冲突、主体类型一致**（绝不允许把 `启明医疗` 与 `嘉禾物流` 或把企业与个人错并）。
2. **幂等防重与 `dry_run` 预演**：
   - 同一订单重复发起 `EscalateOverdueOrder` 会被审计账本拦截；支持先 `dry_run=True` 预览“将通知哪位销售、生成什么工单”，确认后再提交。
3. **源系统不可变 + 治理覆盖层（Governance Overlay）**：
   - 人工或 Agent 审核通过的主数据修复写入受版本管控的 `customer_crosswalk.yaml` 与 `product_category_overrides.yaml`，原始源表保持完整血缘可追溯。
4. **可观测的闭环重建**：
   - 沙箱演示中，通过 `FixProductCategory` 将未分类商品 `P-1012`（标签打印机）修复到 `0101`（打印机）并触发重建后：
     - 数据质量异常总数：**38 → 37**（`DQ-007` 清零）；
     - `01 办公设备` 营收：**¥1,959,142.90 → ¥2,013,560.05（+¥54,417.15）**；
     - `UNCLASSIFIED` 挂账金额：**¥414,185.30 → ¥359,768.15（-¥54,417.15）**，金额继续 100% 守恒。

---

## 3. 复现命令

```bash
# 运行阶段五隔离沙箱端到端演示
python3 scripts/demo_agent_workflow.py

# 运行全项目（阶段四 + 阶段五）共 12 组自动化测试
python3 -m unittest discover -s tests -v
```
