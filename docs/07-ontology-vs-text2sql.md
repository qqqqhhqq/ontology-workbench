# 实测对比报告：Operational Ontology vs Text-to-SQL（真实 `Qwen3.8-27B` 同题评测）

本文档基于本项目真实的 CRM / ERP / WMS 三套异构企业源数据（快照日 `2026-11-23`），使用**完全相同的真实大模型 `Qwen3.8-27B`**（通过企业网关 `/single-endpoint/v1/chat/completions`），在两条架构链路上同场运行 4 道典型企业业务题：

- **Text-to-SQL 链路**：将 10 张原始源系统 CSV 原样装入只读 SQLite，提供完整 10 表 DDL 与 `execute_readonly_sql` 工具，允许模型自主多轮探表、查码表、编写 SQL（`max_turns = 8`）。
- **Operational Ontology 链路**：通过标准 MCP JSON-RPC 2.0 暴露 9 个语义工具（6 读 + 3 前置守卫动作），底层复用 Schema 驱动物化的统一实体、受控词表、WMS 事实源裁决与质量审计。

完整实测原始 JSON（含全部 43 条真实生成的 SQL、MCP 调用轨迹、Token 与延迟记录）保存在 [arena_benchmark_result.json](../data/ontology/arena_benchmark_result.json)。

---

## 1. 真实 `Qwen3.8-27B` 四题量化总分对比

| 评测题目 | 考察核心难点 | Text-to-SQL（10张裸表） | Operational Ontology（MCP） | 关键差距 |
| :--- | :--- | :--- | :--- | :--- |
| **CQ1 · VIP 逾期未发货预警** | 跨 3 系统对齐、缺信用代码、`待出库≠已发`、无连字符单号 | **2/6 (33%)**<br>8 轮 · 14 条 SQL<br>27,185 Tokens · 13.1s | **6/6 (100%)**<br>2 轮 · 1 次 MCP<br>4,701 Tokens · 5.3s | 准确率 **+67%**<br>省 **82.7%** Token |
| **CQ2 · “博思咨询”更名 360°** | CRM 旧名 ↔ ERP 更名后全称（信用代码关联） | **3/3 (100%)**<br>7 轮 · 8 条 SQL<br>14,729 Tokens · 11.6s | **3/3 (100%)**<br>2 轮 · 1 次 MCP<br>3,837 Tokens · 3.7s | 虽答对但多花 **3.8x Token**、慢 **3.1x** |
| **CQ3 · 品类大类营收与审计** | 树形分类上卷、码名冲突、千分位逗号金额 `'12,800.00'` | **0/4 (0%)**<br>8 轮 · 12 条 SQL<br>20,935 Tokens · 14.0s | **4/4 (100%)**<br>2 轮 · 1 次 MCP<br>4,929 Tokens · 7.3s | 裸 SQL **静默少算 ¥12,788**，省 **76.5%** Token |
| **Action · 催办 `SO-20261105-905` 与预演** | 双陷阱单误催办拦截 + 受控写动作预演闭环 | **0/3 (0%)**<br>7 轮 · 9 条 SQL<br>26,955 Tokens · 16.2s | **3/3 (100%)**<br>3 轮 · 3 次 MCP<br>8,343 Tokens · 7.8s | 裸 SQL 查不到无连字符发货单**误判未发**，且无法执行受控动作 |
| **四题合计** | **16 项 Ground Truth 硬验收点** | **5/16 (31%)**<br>30 轮 · 43 条 SQL<br>**89,804 Tokens · 54.9s** | **16/16 (100%)**<br>9 轮 · 6 次 MCP<br>**21,810 Tokens · 24.1s** | **准确率 +69%**<br>**省 75.7% Token（便宜 4.1x）**<br>**提速 2.3x** |

---

## 2. 真实 `Qwen3.8-27B` 在 Text-to-SQL 上踩中的 4 个典型“企业暗坑”

以下全部摘自本次真实跑出的 SQL 原文，而非假设：

### 暗坑一：静默财务失真（不报错、不警告，悄悄少算 ¥12,788）
在 **CQ3** 第 2 轮，`Qwen3.8` 写出了一条看似毫无毛病的标准聚合 SQL：
```sql
SELECT COUNT(*) AS total_items, SUM(CAST(amount AS REAL)) AS total_amount
FROM erp_order_items oi
JOIN erp_sales_orders so ON so.order_no = oi.order_no
WHERE so.status_code <> 'X'
```
- **SQLite 返回**：`5,859,518.6`（真实金额为 **`5,872,306.60`**）
- **根因**：明细表里 `SO-20261018-913` 的金额带有千分位逗号 `"12,800.00"`，`CAST('12,800.00' AS REAL)` 在 SQLite 中静默截断为 **`12.0`**，直接蒸发了 **¥12,788.00** 且零报错！
- **Ontology 表现**：入库清洗层已做 `Decimal` 标准化并留痕 `DQ-006`，模型 1 次 MCP 调用即得一分不差的 `¥5,872,306.60`（100% 金额守恒）。

### 暗坑二：跨系统隐性实体断链（直接丢掉整个 VIP 客户）
在 **CQ1** 第 8 轮，`Qwen3.8` 苦苦探表 8 轮、连写 14 条 SQL 后，写出了：
```sql
JOIN crm_accounts a ON a.credit_code = c.credit_code AND a.credit_code <> ''
```
- **后果**：CRM 里漏填信用代码的 VIP 客户 **星辰设计（`ACC-1006` ↔ `C0006`）** 被直接过滤掉，导致其逾期单 `SO-20261112-904`（涉险 ¥13,178，全场金额最高！）彻底漏报。
- **Ontology 表现**：通过受版本管控的 L3 人工映射表（`customer_crosswalk.yaml`）永久固化身份关系，任何模型/报表都不会再丢单。

### 暗坑三：关联键格式毛病引发“一本正经的误判”
在 **Action 题（`SO-20261105-905`）** 与 **CQ1** 中，Text-to-SQL 写的是 `WHERE s.order_no = o.order_no`。
- **后果**：由于 WMS 表中每 7 条就有 1 条单号丢失连字符（`SO20261105905`），Text-to-SQL 查 `wms_shipments` 得到 **0 行**，于是**错误断言 `SO-20261105-905` 和 `SO-20261028-001` 从未发货**！
- **Ontology 表现**：在物化层统一标准化订单号并保留 `DQ-004` 审计记录，精准识别 `DN-20261120-001` 已出库（`IN_TRANSIT`），并由前置守卫直接阻断误催办。

### 暗坑四：探索性盲试导致 Token 爆炸与网关 429 限流
- 为了摸清 10 张裸表的字段值域、码表含义、日期格式（`2026/10/15` vs `20261122`）和等级大小写（`VIP` vs `vip`），Text-to-SQL 在 4 道题中共发起了 **30 轮 LLM 推理、执行 43 条 SQL、消耗 89,804 Tokens**（在连续评测中甚至一度打爆了企业网关的 `HTTP 429 Too Many Requests` 限流！）。
- 而 Ontology 仅用了 **9 轮推理、6 次 MCP 工具调用、21,810 Tokens**（**省 75.7% Token**），且每一轮回答的口径完全确定可复现。

---

## 3. 架构选型建议：什么时候用 Text-to-SQL，什么时候必须上 Ontology？

两者并非绝对替代，而是**分层协作**：

| 业务场景 | 推荐方案 | 原因 |
| :--- | :--- | :--- |
| 单一干净数仓/宽表上的**临时探索性 BI 分析**（如临时按城市/日期交叉切片） | **Text-to-SQL** | 建模前期投入低，灵活性极高，适合一次性探索问题 |
| **跨异构业务系统**（CRM + ERP + WMS）的日常运营问答与核心 KPI | **Operational Ontology** | 避免每次提问靠 LLM 临场猜 JOIN 与口径，消除静默错数与高额 Token 浪费 |
| 需要 **AI Agent 真正执行写回/审批/工单/治理动作** | **必须用 Operational Ontology** | Text-to-SQL 是只读范式，放开写 SQL 极度危险；Ontology 提供前置守卫、`dry_run`、HITL 审批与不可变审计账本 |
| 最佳实践（现代企业 AIP 架构） | **Ontology 打底 + 受控 SQL 补充** | 用 Ontology 沉淀核心业务对象、实体对齐、权威口径与受控动作；在此之上对干净的本体视图层（如 `ontology.db`）按需提供只读自由切片能力 |

---

## 4. 在线体验同场 PK

打开本地控制台 http://127.0.0.1:8765 → 点击顶部 **「Ontology vs SQL」** 标签页，即可：
1. 直接浏览上述 4 道 `Qwen3.8-27B` 真实评测的完整 SQL 原文、返回样本与 MCP 轨迹；
2. 点击任意题目上的 **「⚡ 现场 PK」** 或输入自定义中文问题，实时观战同一台真实模型在两条架构上的同题对决！
