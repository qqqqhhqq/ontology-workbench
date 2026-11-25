# 阶段三：语义映射与实体解析（Ontology Materialization）

本阶段将阶段二设计的本体规范（[schema.yaml](../ontology/schema.yaml)）转化为可重复运行的构建流水线，把 CRM / ERP / WMS 三套异构源数据清洗、对齐、推导并物化为统一的本体实例层。

---

## 1. 本阶段核心产物

| 文件 | 角色 | 说明 |
| :--- | :--- | :--- |
| [customer_crosswalk.yaml](../ontology/crosswalk/customer_crosswalk.yaml) | L3 人工映射表 | 受版本管控的跨系统客户身份确认清单（含审核人、时间与依据） |
| [build_ontology.py](../scripts/build_ontology.py) | 本体构建引擎 | 直接读取 `schema.yaml` 驱动词表映射、实体解析、派生计算与 12 条质量规则 |
| [ontology.db](../data/ontology/ontology.db) | SQLite 混合本体库 | 包含 8 张对象表 + 1 张显式语义边表（`links`） + `metadata` 表，已通过外键完整性校验 |
| [ontology_graph.json](../data/ontology/ontology_graph.json) | 语义图谱 JSON 快照 | 完整的 `objects`、`links` 与 `summary` 结构化导出，便于前端可视化或图引擎加载 |

---

## 2. 为什么强调“Schema 驱动”（Schema-Driven）？

在企业工程中，**绝不应该在 YAML 文档里写一套规则、又在 Python 代码里硬编码另一套映射表**。`build_ontology.py` 直接以 [schema.yaml](../ontology/schema.yaml) 为单一事实源（Single Source of Truth）：

1. **受控词表自动加载**：`PartyType`、`CustomerTier`、`ErpOrderStatus`、`ShipmentStatus` 的源值映射以及“哪些状态属于有效发货”，均在启动时从 `schema.yaml` 动态解析。
2. **快照基准日统一**：`as_of_date = 2026-11-23` 直接读取自 `schema.yaml`。
3. **实例枚举强校验**：每个对象物化前都会执行 `assert_enum`，杜绝脏字符串混入本体层。
4. **约束元数据复用**：`DQ-001` ~ `DQ-012` 的规则名、严重级别和中文描述均直接引用 `schema.yaml`。

---

## 3. 核心技术难点与实现细节

### 3.1 三级客户实体解析（12 CRM + 12 ERP → 14 真实参与方）

解析引擎严格按置信度从高到低执行，**拒绝黑盒模糊字符串猜合并**：

| 层级 | 匹配策略 | 本批命中数 | 典型案例 |
| :--- | :--- | :---: | :--- |
| **L1** | 统一社会信用代码精确一致（`CREDIT_CODE`） | 8 | 自动拉通更名企业 `ACC-1009(博思咨询)` ↔ `C0009(远东管理咨询有限公司)`；同时严格隔离名称相似的 `云帆科技` 与 `新云帆网络` |
| **L2** | 名称 + 标准主体类型 + 城市精确一致（`EXACT_NAME`） | 1 | 无信用代码的个人客户 `ACC-1004(张三)` ↔ `C0004(张三)` |
| **L3** | 受控人工映射表（`MANUAL_CROSSWALK`） | 1 | CRM 漏填信用代码的 `ACC-1006(星辰设计)` ↔ `C0006(星辰创意设计有限公司)` |
| **单边** | 单系统存在，原样保留并标记 `source_presence`（`SINGLE_SOURCE`） | 4 | `ERP_ONLY`（嘉禾物流、赵六）2 家；`CRM_ONLY`（启明医疗、李四）2 家 |

### 3.2 产品分类树传递闭包与异常分流
- `ProductCategory` 沿 `parent_category` 向上遍历并检测环路，为 8 个节点计算出 `level`、`is_leaf` 及一级大类 `root_category`。
- 15 个商品 SKU 中：
  - **11 个 `VALID`**：合法挂载到二级叶子节点。
  - **1 个 `NON_LEAF`（`P-1015`）**：挂在一级节点 `01 办公设备`，保留 `belongs_to_category` 链接以便按大类统计，同时触发 `DQ-010`。
  - **3 个不可信（`P-1012` MISSING、`P-1013` CONFLICT、`P-1014` UNKNOWN_CODE）**：故意不建立分类链接，防止错误归类污染大类营收报表，分别触发 `DQ-007/008/009`。

### 3.3 跨系统订单号修复与 WMS 权威裁决
- **订单号修复**：WMS 中有 **18 条**发货单丢失了连字符（如 `SO20261105905` → `SO-20261105-905`），标准化后全部准确挂载到对应 `SalesOrder`，同时留痕 `DQ-004`。
- **双陷阱验证（`SO-20261105-905`）**：该订单在 ERP 仍停留在 `PENDING_SHIPMENT`，且 WMS 订单号缺连字符；本体层修复单号并以 WMS 裁决 `fulfillment_status = IN_TRANSIT`，因此**不会被误报为逾期未发货**，同时被自动捕获到 `DQ-001`（ERP 滞后）与 `DQ-004`。
- **孤立发货单**：`DN-20261118-901` 引用不存在的 `SO-20261118-777`，保留对象并将 `is_dangling = true`，挂载 `DQ-002`。

---

## 4. 本次物化结果摘要

运行 `python3 scripts/build_ontology.py` 的实际产出：
- **对象实例**：4 名销售、14 个统一客户、8 个分类节点、15 个产品、152 张销售订单、361 条明细行、129 张发货单、38 条数据质量异常。
- **语义边（Links）**：共 **1,031 条**显式语义关系（`placed_by` 152、`part_of_order` 361、`for_product` 361、`fulfills` 128、`belongs_to_category` 12、`owned_by` 11、`parent_category` 6）。
- **质量审计（38 条）**：`HIGH` 10 条（`DQ-001`×5、`DQ-002`×1、`DQ-003`×1、`DQ-007/008/009` 各 1）、`MEDIUM` 21 条（`DQ-004`×18、`DQ-005`×1、`DQ-006`×1、`DQ-010`×1）、`LOW` 7 条（`DQ-011`×3、`DQ-012`×4）。
- **SQLite 外键校验**：`PRAGMA foreign_key_check` 返回 **0 错误**。
