# 源系统数据字典与埋点问题清单

本目录的数据由 [generate_source_data.py](../../scripts/generate_source_data.py) 确定性生成（`SEED = 20261123`，`AS_OF = 2026-11-23`）。

---

## 1. 目录与表结构

### CRM（销售客户系统）
- `crm/owners.csv`：`owner_id, owner_name, team`（4 条）
- `crm/accounts.csv`：`account_id, account_name, account_type, customer_level, owner_id, city, credit_code`（12 条）

### ERP（订单与主数据系统）
- `erp/customer_type_codes.csv`：`type_code, type_name`（`10` 企业、`20` 个体工商户、`30` 个人）
- `erp/order_status_codes.csv`：`status_code, status_name`（`A` 待发货、`B` 部分发货、`C` 已发货、`D` 已完成、`X` 已取消）
- `erp/category_codes.csv`：`category_code, category_name, parent_code`（两级分类树：办公设备 / 耗材）
- `erp/customers.csv`：`customer_no, customer_name, type_code, city, credit_code`（12 条）
- `erp/products.csv`：`sku, product_name, category_code, category_name, unit, list_price`（15 条）
- `erp/sales_orders.csv`：`order_no, customer_no, order_date, promised_date, status_code`（152 条）
- `erp/order_items.csv`：`order_no, line_no, sku, qty, unit_price, amount`

### WMS（仓储发货系统）
- `wms/shipments.csv`：`shipment_id, order_no, ship_date, status, carrier`（129 条）

---

## 2. 故意埋入的企业数据问题清单

### A. 实体解析与跨系统客户对齐
| 类型 | 涉及记录 | 问题说明 |
| :--- | :--- | :--- |
| 简称 vs 全称 | `ACC-1001` (`云帆科技`) ↔ `C0001` (`杭州云帆科技有限公司`) 等 | CRM 多用简称，ERP 用工商全称 |
| 信用代码缺失 | `ACC-1006` (`星辰设计`) ↔ `C0006` (`星辰创意设计有限公司`) | CRM 未录信用代码，需靠名称/规则或人工映射表对齐 |
| 名称相近防误并 | `ACC-1008` (`新云帆网络`) vs `ACC-1001` (`云帆科技`) | 字面高度相似但为不同法人，信用代码不同 |
| 企业更名断链 | `ACC-1009` (`博思咨询`) ↔ `C0009` (`远东管理咨询有限公司`) | 名称完全对不上，但两系统共享信用代码 `91110105MA01B0099J`；且 CRM 缺等级和负责人 |
| 单边存在（仅 ERP） | `C0011` (`嘉禾物流有限公司`)、`C0012` (`赵六`) | 有历史订单但 CRM 未建档 |
| 单边存在（仅 CRM） | `ACC-1011` (`启明医疗`)、`ACC-1012` (`李四`) | 有销售跟进记录但从未下单 |

### B. 术语与语义口径不统一
| 类型 | 涉及记录 | 问题说明 |
| :--- | :--- | :--- |
| 客户等级大小写/同义词 | `crm/accounts.csv` | 混用 `VIP` / `vip` / `大客户` / `普通` / `一般客户` / `普通客户` / `潜在客户` / 空值 |
| 客户类型表示法不同 | CRM `account_type` vs ERP `type_code` | CRM 为中文（`公司`/`个体户`/`个人`），ERP 为码值（`10`/`20`/`30`） |
| “已发货”口径冲突 | `SO-20261106-903` 及部分随机订单 | WMS 的 `待出库` 不等于有效发货；ERP 状态 `A`/`B` 存在同步滞后（共 5 张单 ERP 仍显示未完全发货但 WMS 已出库/签收） |

### C. 产品分类主数据异常
| SKU | 商品名 | 问题类型 |
| :--- | :--- | :--- |
| `P-1012` | 标签打印机 BQ-50 | 分类码与分类名均为空（未维护） |
| `P-1013` | 碎纸机 SZ-300 | 分类码 `0201`（硒鼓）与分类名 `打印机` 相互矛盾 |
| `P-1014` | 扫描仪 SM-1200 | 分类码 `0104` 不在 `category_codes.csv` 码表中 |
| `P-1015` | 装订机 BD-60 | 直接挂在一级非叶子节点 `01` 上且分类名为空 |

### D. 订单与发货单边界埋点（`9xx` 系列）
| 订单号 / 发货单号 | 设计目的 |
| :--- | :--- |
| `SO-20261103-901`、`SO-20261110-902`、`SO-20261106-903`、`SO-20261112-904` | **CQ1 精确预期结果（4 张）**：VIP、近 30 天下单、逾期、未取消、WMS 无有效发货（其中 903 在 WMS 为“待出库”） |
| `SO-20261105-905` | **双陷阱干扰项**：ERP 状态滞留在 `A`，但 WMS 已出库且订单号缺连字符 `SO20261105905` |
| `SO-20261108-906` | VIP 已取消订单（`X`），不应计入逾期 |
| `SO-20261118-907` | VIP 未发货但承诺日为 `2026-11-28`，尚未逾期 |
| `SO-20261010-908` | VIP 逾期未发货，但下单日超过近 30 天窗口 |
| `SO-20261109-909` | CRM 等级为 `大客户`（非 `VIP`）的逾期未发货订单，用于检验语义边界 |
| `SO-20261111-910` | VIP 订单缺少 `promised_date`（承诺交付日为空） |
| `SO-20261114-911` | 普通个人客户逾期未发货 |
| `SO-20261015-912` | 下单日期格式异常：`2026/10/15`（标准为 `YYYYMMDD`） |
| `SO-20261018-913` | 第 1 行金额带千分位逗号：`"12,800.00"` |
| `DN-20261118-901` | WMS 孤立发货单，引用不存在的订单号 `SO-20261118-777` |
| 若干随机 WMS 记录 | 每 7 条中有 1 条订单号去掉了连字符 `-` |
