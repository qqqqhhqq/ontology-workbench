#!/usr/bin/env python3
"""生成 demo 用的源系统数据：CRM、ERP、WMS 三套，各自落成一组 CSV。

数据全部虚构。随机种子与数据快照日固定，所以每次运行结果完全一致。

真实企业数据常见的问题被有意埋入数据中：
- 批量问题（如客户等级命名不统一、ERP 状态更新滞后、WMS 订单号缺连字符）直接体现在基础规则里；
- 精确控制的边界案例（如 VIP 逾期未发货、已取消、缺承诺日、格式异常、悬空发货单）集中在“人为埋点”一节。

完整的数据字典与问题清单见 data/sources/README.md。
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

# 数据快照日：demo 里所有“今天”“近 30 天”都以它为准，确保结果可复现。
AS_OF = date(2026, 11, 23)
ORDER_DATE_FROM = date(2026, 8, 1)
ORDER_DATE_TO = date(2026, 11, 22)
SEED = 20261123

CARRIERS = ["顺丰速运", "德邦快递", "自送"]

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "sources"


# ---------------------------------------------------------------------------
# 基础数据
# ---------------------------------------------------------------------------

OWNERS = [  # (负责人编号, 姓名, 团队)
    ("EMP-001", "王磊", "华东一组"),
    ("EMP-002", "陈静", "华东一组"),
    ("EMP-003", "刘洋", "华东二组"),
    ("EMP-004", "周敏", "华东二组"),
]


@dataclass
class Party:
    """一个真实世界的参与方（客户），以及它在 CRM、ERP 各自系统里的记录。"""

    city: str
    orders: int  # 随机生成多少张订单；0 表示只在 CRM 有账号、未下过单
    crm_id: str = ""
    crm_name: str = ""
    crm_type: str = ""
    crm_level: str = ""
    crm_owner: str = ""
    crm_credit_code: str = ""
    erp_no: str = ""
    erp_name: str = ""
    erp_type_code: str = ""
    erp_credit_code: str = ""

    @property
    def is_vip(self) -> bool:
        return self.crm_level.lower() == "vip"


PARTIES = [
    Party(
        city="杭州", orders=22,
        crm_id="ACC-1001", crm_name="云帆科技", crm_type="公司", crm_level="VIP",
        crm_owner="EMP-001", crm_credit_code="91330100MA27Y0011A",
        erp_no="C0001", erp_name="杭州云帆科技有限公司", erp_type_code="10",
        erp_credit_code="91330100MA27Y0011A",
    ),
    Party(
        city="上海", orders=16,
        crm_id="ACC-1002", crm_name="恒信律师事务所", crm_type="公司", crm_level="大客户",
        crm_owner="EMP-002", crm_credit_code="31310000MD0020022B",
        erp_no="C0002", erp_name="恒信律师事务所", erp_type_code="10",
        erp_credit_code="31310000MD0020022B",
    ),
    Party(
        city="南京", orders=20,
        crm_id="ACC-1003", crm_name="蓝天教育集团", crm_type="公司", crm_level="vip",
        crm_owner="EMP-001", crm_credit_code="91320100MA1N30033C",
        erp_no="C0003", erp_name="蓝天教育科技集团有限公司", erp_type_code="10",
        erp_credit_code="91320100MA1N30033C",
    ),
    Party(
        city="杭州", orders=5,
        crm_id="ACC-1004", crm_name="张三", crm_type="个人", crm_level="普通",
        crm_owner="EMP-003", crm_credit_code="",
        erp_no="C0004", erp_name="张三", erp_type_code="30",
        erp_credit_code="",
    ),
    Party(
        city="杭州", orders=8,
        crm_id="ACC-1005", crm_name="文华文具店", crm_type="个体户", crm_level="一般客户",
        crm_owner="EMP-003", crm_credit_code="92330106MA2850055E",
        erp_no="C0005", erp_name="杭州市西湖区文华文具店", erp_type_code="20",
        erp_credit_code="92330106MA2850055E",
    ),
    # CRM 里漏填了信用代码，但简称与 ERP 全称可对齐
    Party(
        city="苏州", orders=18,
        crm_id="ACC-1006", crm_name="星辰设计", crm_type="公司", crm_level="VIP",
        crm_owner="EMP-004", crm_credit_code="",
        erp_no="C0006", erp_name="星辰创意设计有限公司", erp_type_code="10",
        erp_credit_code="91320500MA1W60066F",
    ),
    Party(
        city="上海", orders=15,
        crm_id="ACC-1007", crm_name="宏图地产", crm_type="公司", crm_level="大客户",
        crm_owner="EMP-002", crm_credit_code="91310115MA1K70077G",
        erp_no="C0007", erp_name="宏图房地产有限公司", erp_type_code="10",
        erp_credit_code="91310115MA1K70077G",
    ),
    # 与“云帆科技”名称高度相似，但属于完全不同的法人实体
    Party(
        city="上海", orders=7,
        crm_id="ACC-1008", crm_name="新云帆网络", crm_type="公司", crm_level="普通",
        crm_owner="EMP-004", crm_credit_code="91310104MA1FR0088H",
        erp_no="C0008", erp_name="上海新云帆网络科技有限公司", erp_type_code="10",
        erp_credit_code="91310104MA1FR0088H",
    ),
    # 改过名：CRM 还是旧名“博思咨询”，ERP 已变更为“远东管理咨询有限公司”；
    # 靠名称无法对齐，但两系统共享同一统一社会信用代码；且 CRM 缺等级和负责人
    Party(
        city="北京", orders=9,
        crm_id="ACC-1009", crm_name="博思咨询", crm_type="公司", crm_level="",
        crm_owner="", crm_credit_code="91110105MA01B0099J",
        erp_no="C0009", erp_name="远东管理咨询有限公司", erp_type_code="10",
        erp_credit_code="91110105MA01B0099J",
    ),
    Party(
        city="杭州", orders=6,
        crm_id="ACC-1010", crm_name="金桥培训学校", crm_type="公司", crm_level="普通客户",
        crm_owner="EMP-003", crm_credit_code="91330104MA27A0100K",
        erp_no="C0010", erp_name="金桥职业培训学校", erp_type_code="10",
        erp_credit_code="91330104MA27A0100K",
    ),
    # 只在 ERP 里有：下过单，但 CRM 没有建立客户账号
    Party(
        city="上海", orders=10,
        erp_no="C0011", erp_name="嘉禾物流有限公司", erp_type_code="10",
        erp_credit_code="91310112MA1GB0111L",
    ),
    Party(
        city="杭州", orders=3,
        erp_no="C0012", erp_name="赵六", erp_type_code="30",
        erp_credit_code="",
    ),
    # 只在 CRM 里有：有销售跟进账号，但尚未下过单
    Party(
        city="杭州", orders=0,
        crm_id="ACC-1011", crm_name="启明医疗", crm_type="公司", crm_level="潜在客户",
        crm_owner="EMP-001", crm_credit_code="91330108MA28M0122M",
    ),
    Party(
        city="上海", orders=0,
        crm_id="ACC-1012", crm_name="李四", crm_type="个人", crm_level="",
        crm_owner="EMP-002", crm_credit_code="",
    ),
]

CUSTOMER_TYPE_CODES = [  # ERP 客户类型码表
    ("10", "企业"),
    ("20", "个体工商户"),
    ("30", "个人"),
]

ORDER_STATUS_CODES = [  # ERP 订单状态码表
    ("A", "待发货"),
    ("B", "部分发货"),
    ("C", "已发货"),
    ("D", "已完成"),
    ("X", "已取消"),
]

CATEGORY_CODES = [  # (分类码, 分类名称, 上级分类码)，ERP 产品分类码表
    ("01", "办公设备", ""),
    ("0101", "打印机", "01"),
    ("0102", "复印机", "01"),
    ("0103", "投影仪", "01"),
    ("02", "耗材", ""),
    ("0201", "硒鼓", "02"),
    ("0202", "墨盒", "02"),
    ("0203", "纸张", "02"),
]


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category_code: str
    category_name: str
    unit: str
    list_price: float

    @property
    def is_consumable(self) -> bool:
        return self.unit != "台"


PRODUCTS = [  # ERP 产品主数据
    Product("P-1001", "激光打印机 LJ-2200", "0101", "打印机", "台", 1299.00),
    Product("P-1002", "喷墨打印机 TJ-330", "0101", "打印机", "台", 899.00),
    Product("P-1003", "数码复合机 FH-360", "0102", "复印机", "台", 4599.00),
    Product("P-1004", "数码复印机 FM-810", "0102", "复印机", "台", 3299.00),
    Product("P-1005", "便携投影仪 TY-200", "0103", "投影仪", "台", 2799.00),
    Product("P-1006", "原装硒鼓 SD-88A", "0201", "硒鼓", "个", 499.00),
    Product("P-1007", "兼容硒鼓 SD-88A-C", "0201", "硒鼓", "个", 299.00),
    Product("P-1008", "彩色墨盒 MK-950", "0202", "墨盒", "个", 259.00),
    Product("P-1009", "黑色墨盒 MK-950K", "0202", "墨盒", "个", 199.00),
    Product("P-1010", "A4 复印纸（5包/箱）", "0203", "纸张", "箱", 145.00),
    Product("P-1011", "A3 复印纸（5包/箱）", "0203", "纸张", "箱", 215.00),
    # 下面四个为故意埋入的分类主数据问题
    Product("P-1012", "标签打印机 BQ-50", "", "", "台", 699.00),          # 分类未维护
    Product("P-1013", "碎纸机 SZ-300", "0201", "打印机", "台", 899.00),  # 分类码(硒鼓)与分类名冲突
    Product("P-1014", "扫描仪 SM-1200", "0104", "扫描仪", "台", 1599.00), # 分类码不在码表中
    Product("P-1015", "装订机 BD-60", "01", "", "台", 399.00),           # 挂在非叶子节点上且缺名称
]


# ---------------------------------------------------------------------------
# 订单：随机生成部分
# ---------------------------------------------------------------------------


@dataclass
class Order:
    customer_no: str
    order_date: date
    promised_date: date | None
    items: list[tuple[str, int, float]] = field(default_factory=list)  # (sku, 数量, 单价)
    order_no: str = ""
    status: str = ""  # ERP 订单状态码
    cancelled: bool = False
    ship_date: date | None = None  # None 表示 WMS 里没有该订单的发货单


def random_date(rng: random.Random, first: date, last: date) -> date:
    return first + timedelta(days=rng.randint(0, (last - first).days))


def shipment_status(ship_date: date) -> str:
    """发货单在 WMS 里的状态，由发货日与快照日的间隔决定。"""
    if ship_date == AS_OF:
        return "待出库"
    if ship_date <= AS_OF - timedelta(days=7):
        return "已签收"
    return "已出库"


def decide_shipment(order: Order, rng: random.Random, vip: bool) -> None:
    """决定随机订单是否发货。

    VIP 客户近 30 天内的逾期未发货订单全部留给“人为埋点”，
    以便 CQ1 的预期结果完全可控、清晰可验证。
    """
    assert order.promised_date is not None
    overdue_days = (AS_OF - order.promised_date).days
    if overdue_days > 10:
        p_shipped = 0.9
    elif overdue_days > 0:
        p_shipped = 0.6
    else:
        p_shipped = 0.35

    vip_recent_overdue = (
        vip
        and overdue_days > 0
        and order.order_date >= AS_OF - timedelta(days=29)
    )
    if vip_recent_overdue or rng.random() < p_shipped:
        earliest = order.order_date + timedelta(days=1)
        latest = AS_OF - timedelta(days=1) if vip_recent_overdue else AS_OF
        upper = min(latest, order.promised_date + timedelta(days=3))
        if upper < earliest:
            upper = earliest
        order.ship_date = random_date(rng, earliest, upper)


def erp_status(order: Order, rng: random.Random) -> str:
    """ERP 的订单状态：现实中常有同步延迟，部分已发货订单在 ERP 里仍停留在 A 或 B。"""
    if order.cancelled:
        return "X"
    if order.ship_date is None or shipment_status(order.ship_date) == "待出库":
        return "A"
    if order.ship_date <= AS_OF - timedelta(days=21):
        return "D"
    return rng.choice(["C", "C", "C", "A", "B"])


def random_items(rng: random.Random) -> list[tuple[str, int, float]]:
    chosen = rng.sample(PRODUCTS, k=rng.randint(1, 4))
    items = []
    for product in chosen:
        qty = rng.randint(20, 200) if product.is_consumable else rng.randint(1, 12)
        unit_price = round(product.list_price * rng.choice([1.0, 0.95, 0.9, 0.85, 0.8]), 2)
        items.append((product.sku, qty, unit_price))
    return items


def random_orders(rng: random.Random) -> list[Order]:
    orders = []
    for party in PARTIES:
        if not party.erp_no or party.orders <= 0:
            continue
        for _ in range(party.orders):
            order_date = random_date(rng, ORDER_DATE_FROM, ORDER_DATE_TO)
            order = Order(
                customer_no=party.erp_no,
                order_date=order_date,
                promised_date=order_date + timedelta(days=rng.randint(5, 15)),
            )
            order.cancelled = rng.random() < 0.03
            order.items = random_items(rng)
            if not order.cancelled:
                decide_shipment(order, rng, vip=party.is_vip)
            order.status = erp_status(order, rng)
            orders.append(order)

    orders.sort(key=lambda o: (o.order_date, o.customer_no))
    per_day: dict[date, int] = {}
    for order in orders:
        per_day[order.order_date] = per_day.get(order.order_date, 0) + 1
        order.order_no = f"SO-{order.order_date:%Y%m%d}-{per_day[order.order_date]:03d}"
    return orders


# ---------------------------------------------------------------------------
# 订单：人为埋点（单号 9xx）
# ---------------------------------------------------------------------------

# 个别字段的格式毛病，写出 CSV 时套用
ODD_ORDER_DATES = {"SO-20261015-912": "2026/10/15"}
ODD_AMOUNTS = {("SO-20261018-913", 1): "12,800.00"}
NO_DASH_ORDER_NOS = {"SO-20261105-905"}


def planted_orders() -> list[Order]:
    d = date
    return [
        # ---- CQ1 精确预期结果（4 张）：VIP 客户、近 30 天内下单、已过承诺日、WMS 无有效发货 ----
        Order(
            "C0001", d(2026, 11, 3), d(2026, 11, 10),
            [("P-1001", 2, 1299.00), ("P-1006", 10, 499.00)],
            order_no="SO-20261103-901", status="A",
        ),
        Order(
            "C0001", d(2026, 11, 10), d(2026, 11, 18),
            [("P-1010", 30, 145.00), ("P-1011", 10, 215.00)],
            order_no="SO-20261110-902", status="A",
        ),
        Order(
            "C0003", d(2026, 11, 6), d(2026, 11, 13),
            [("P-1005", 1, 2799.00)],
            order_no="SO-20261106-903", status="A", ship_date=AS_OF,  # WMS 仅为“待出库”，不算已发货
        ),
        Order(
            "C0006", d(2026, 11, 12), d(2026, 11, 19),
            [("P-1003", 2, 4599.00), ("P-1009", 20, 199.00)],
            order_no="SO-20261112-904", status="A",
        ),
        # ---- 典型边界 / 干扰案例 ----
        # 905: VIP 客户，ERP 状态滞留在 A(待发货)，但 WMS 实际已出库且单号缺连字符(SO20261105905)
        Order(
            "C0001", d(2026, 11, 5), d(2026, 11, 12),
            [("P-1002", 3, 899.00)],
            order_no="SO-20261105-905", status="A", ship_date=d(2026, 11, 20),
        ),
        # 906: VIP 客户，已取消（X），不应算逾期未发货
        Order(
            "C0003", d(2026, 11, 8), d(2026, 11, 15),
            [("P-1004", 1, 3299.00)],
            order_no="SO-20261108-906", status="X", cancelled=True,
        ),
        # 907: VIP 客户，未发货但承诺日在快照日之后（未逾期）
        Order(
            "C0006", d(2026, 11, 18), d(2026, 11, 28),
            [("P-1007", 50, 299.00)],
            order_no="SO-20261118-907", status="A",
        ),
        # 908: VIP 客户，逾期未发货，但下单日在近 30 天窗口之外
        Order(
            "C0001", d(2026, 10, 10), d(2026, 10, 20),
            [("P-1008", 40, 259.00)],
            order_no="SO-20261010-908", status="A",
        ),
        # 909: CRM 等级为“大客户”（非 VIP），近 30 天逾期未发货——用于检验 VIP 语义边界
        Order(
            "C0002", d(2026, 11, 9), d(2026, 11, 16),
            [("P-1001", 4, 1299.00)],
            order_no="SO-20261109-909", status="A",
        ),
        # 910: VIP 客户，缺少承诺交付日期（数据质量缺陷）
        Order(
            "C0001", d(2026, 11, 11), None,
            [("P-1006", 15, 499.00)],
            order_no="SO-20261111-910", status="A",
        ),
        # 911: 普通个人客户，近 30 天逾期未发货
        Order(
            "C0004", d(2026, 11, 14), d(2026, 11, 20),
            [("P-1010", 5, 145.00)],
            order_no="SO-20261114-911", status="A",
        ),
        # 912: 下单日期格式异常（2026/10/15，非标准 YYYYMMDD）
        Order(
            "C0005", d(2026, 10, 15), d(2026, 10, 25),
            [("P-1011", 20, 215.00)],
            order_no="SO-20261015-912", status="D", ship_date=d(2026, 10, 22),
        ),
        # 913: 订单明细金额格式异常（带千分位逗号 "12,800.00"）
        Order(
            "C0007", d(2026, 10, 18), d(2026, 10, 28),
            [("P-1004", 4, 3200.00)],
            order_no="SO-20261018-913", status="D", ship_date=d(2026, 10, 24),
        ),
    ]


# ---------------------------------------------------------------------------
# 写出 CSV 文件
# ---------------------------------------------------------------------------


def write_csv(path: Path, header: list[str], rows: Iterable[Iterable[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def erp_date(d: date | None) -> str:
    """ERP 默认用 8 位无分隔日期字符串（YYYYMMDD）。"""
    return "" if d is None else f"{d:%Y%m%d}"


def item_amount(order_no: str, line_no: int, qty: int, unit_price: float) -> str:
    default = f"{round(qty * unit_price, 2):.2f}"
    return ODD_AMOUNTS.get((order_no, line_no), default)


def write_crm() -> None:
    write_csv(
        OUT_DIR / "crm" / "owners.csv",
        ["owner_id", "owner_name", "team"],
        OWNERS,
    )
    account_rows = [
        (
            p.crm_id, p.crm_name, p.crm_type, p.crm_level,
            p.crm_owner, p.city, p.crm_credit_code,
        )
        for p in PARTIES
        if p.crm_id
    ]
    write_csv(
        OUT_DIR / "crm" / "accounts.csv",
        ["account_id", "account_name", "account_type", "customer_level", "owner_id", "city", "credit_code"],
        account_rows,
    )


def write_erp(orders: list[Order]) -> None:
    write_csv(
        OUT_DIR / "erp" / "customer_type_codes.csv",
        ["type_code", "type_name"],
        CUSTOMER_TYPE_CODES,
    )
    write_csv(
        OUT_DIR / "erp" / "order_status_codes.csv",
        ["status_code", "status_name"],
        ORDER_STATUS_CODES,
    )
    write_csv(
        OUT_DIR / "erp" / "category_codes.csv",
        ["category_code", "category_name", "parent_code"],
        CATEGORY_CODES,
    )

    customer_rows = [
        (p.erp_no, p.erp_name, p.erp_type_code, p.city, p.erp_credit_code)
        for p in PARTIES
        if p.erp_no
    ]
    write_csv(
        OUT_DIR / "erp" / "customers.csv",
        ["customer_no", "customer_name", "type_code", "city", "credit_code"],
        customer_rows,
    )

    product_rows = [
        (p.sku, p.name, p.category_code, p.category_name, p.unit, f"{p.list_price:.2f}")
        for p in PRODUCTS
    ]
    write_csv(
        OUT_DIR / "erp" / "products.csv",
        ["sku", "product_name", "category_code", "category_name", "unit", "list_price"],
        product_rows,
    )

    order_rows = []
    item_rows = []
    for o in orders:
        raw_order_date = ODD_ORDER_DATES.get(o.order_no, erp_date(o.order_date))
        order_rows.append((
            o.order_no, o.customer_no, raw_order_date, erp_date(o.promised_date), o.status,
        ))
        for line_no, (sku, qty, unit_price) in enumerate(o.items, start=1):
            item_rows.append((
                o.order_no, line_no, sku, qty, f"{unit_price:.2f}",
                item_amount(o.order_no, line_no, qty, unit_price),
            ))

    write_csv(
        OUT_DIR / "erp" / "sales_orders.csv",
        ["order_no", "customer_no", "order_date", "promised_date", "status_code"],
        order_rows,
    )
    write_csv(
        OUT_DIR / "erp" / "order_items.csv",
        ["order_no", "line_no", "sku", "qty", "unit_price", "amount"],
        item_rows,
    )


def write_wms(orders: list[Order], rng: random.Random) -> None:
    shipped = sorted(
        (o for o in orders if o.ship_date is not None),
        key=lambda o: (o.ship_date, o.order_no),  # type: ignore[arg-type]
    )
    rows = []
    per_day: dict[date, int] = {}
    for i, order in enumerate(shipped):
        assert order.ship_date is not None
        per_day[order.ship_date] = per_day.get(order.ship_date, 0) + 1
        order_no = order.order_no
        if order_no in NO_DASH_ORDER_NOS or i % 7 == 3:
            order_no = order_no.replace("-", "")
        rows.append((
            f"DN-{order.ship_date:%Y%m%d}-{per_day[order.ship_date]:03d}",
            order_no,
            f"{order.ship_date:%Y-%m-%d}",
            shipment_status(order.ship_date),
            rng.choice(CARRIERS),
        ))

    # 埋点：孤立发货单，引用了 ERP 中不存在的订单号
    rows.append(("DN-20261118-901", "SO-20261118-777", "2026-11-18", "已出库", "顺丰速运"))

    write_csv(
        OUT_DIR / "wms" / "shipments.csv",
        ["shipment_id", "order_no", "ship_date", "status", "carrier"],
        rows,
    )


# ---------------------------------------------------------------------------
# 自检摘要
# ---------------------------------------------------------------------------


def is_cq1_target(order: Order, vip_erp_nos: set[str]) -> bool:
    """CQ1: VIP 客户、近 30 天内下单、已过承诺日、未取消、WMS 无有效发货。"""
    if order.customer_no not in vip_erp_nos:
        return False
    if order.cancelled or order.promised_date is None:
        return False
    if order.order_date < AS_OF - timedelta(days=29) or order.promised_date >= AS_OF:
        return False
    return order.ship_date is None or shipment_status(order.ship_date) == "待出库"


def report(orders: list[Order]) -> None:
    vip_erp_nos = {p.erp_no for p in PARTIES if p.is_vip and p.erp_no}
    cq1_hits = [o for o in orders if is_cq1_target(o, vip_erp_nos)]
    lagging_erp = [
        o for o in orders
        if not o.cancelled
        and o.ship_date is not None
        and shipment_status(o.ship_date) != "待出库"
        and o.status in {"A", "B"}
    ]
    print(f"快照日期 (AS_OF): {AS_OF}")
    print(f"CRM 账号数: {sum(1 for p in PARTIES if p.crm_id)} | ERP 客户数: {sum(1 for p in PARTIES if p.erp_no)}")
    print(f"ERP 产品数: {len(PRODUCTS)} (含 4 个分类异常埋点)")
    print(f"ERP 订单总数: {len(orders)} (随机 {len(orders) - len(planted_orders())} + 埋点 {len(planted_orders())})")
    print(f"WMS 发货单数: {sum(1 for o in orders if o.ship_date is not None) + 1} (含 1 条孤立发货单)")
    print(f"ERP 状态滞后于 WMS 的订单数: {len(lagging_erp)}")
    print(f"CQ1 预期命中订单 ({len(cq1_hits)} 张): {[o.order_no for o in cq1_hits]}")


def main() -> None:
    rng = random.Random(SEED)
    orders = random_orders(rng) + planted_orders()
    orders.sort(key=lambda o: (o.order_date, o.order_no))

    write_crm()
    write_erp(orders)
    write_wms(orders, rng)
    report(orders)


if __name__ == "__main__":
    main()
