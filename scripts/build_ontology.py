#!/usr/bin/env python3
"""阶段三：语义映射与实体解析引擎（Schema-Driven Ontology Builder）。

读取：
  - ontology/schema.yaml                       本体规范（受控词表、约束定义、快照基准日）
  - ontology/crosswalk/customer_crosswalk.yaml L3 人工确认客户身份映射表
  - data/sources/{crm,erp,wms}/*.csv           三套异构源系统原始数据

执行：
  1. 词表归一化（CRM/ERP/WMS 枚举值映射到本体受控词表）
  2. 三级客户实体解析（L1 信用代码 -> L2 名称+类型+城市 -> L3 人工 Crosswalk -> 单边实体）
  3. 树形产品分类传递闭包推理与主数据合规校验
  4. 订单/明细/发货单格式清洗、订单号标准化与 WMS 权威履约状态裁决
  5. 自动执行 12 条数据质量规则（DQ-001 ~ DQ-012），产出 DataQualityIssue
  6. 基于 schema.yaml 校验实例枚举合法性，并物化输出 SQLite + JSON 图谱

产出：
  - data/ontology/ontology.db          关系+边表混合本体库（支持 SQL 与图遍历）
  - data/ontology/ontology_graph.json  完整本体实例与语义关系快照
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT_DIR / "ontology" / "schema.yaml"
CROSSWALK_PATH = ROOT_DIR / "ontology" / "crosswalk" / "customer_crosswalk.yaml"
OVERRIDES_PATH = ROOT_DIR / "ontology" / "overrides" / "product_category_overrides.yaml"
SOURCES_DIR = ROOT_DIR / "data" / "sources"
OUT_DIR = ROOT_DIR / "data" / "ontology"
DB_PATH = OUT_DIR / "ontology.db"
GRAPH_PATH = OUT_DIR / "ontology_graph.json"

STANDARD_ERP_DATE_RE = re.compile(r"^\d{8}$")
COMPACT_ORDER_NO_RE = re.compile(r"^SO(\d{8})(\d{3})$")
PURE_DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?$")


# ---------------------------------------------------------------------------
# 1. 本体规范加载器（Single Source of Truth）
# ---------------------------------------------------------------------------


class OntologySchema:
    """从 ontology/schema.yaml 加载词表映射、约束元数据与全局参数。"""

    def __init__(self, path: Path) -> None:
        raw_bytes = path.read_bytes()
        self.sha256 = hashlib.sha256(raw_bytes).hexdigest()[:16]
        self.raw: dict[str, Any] = yaml.safe_load(raw_bytes.decode("utf-8"))

        meta = self.raw["ontology"]
        self.ontology_id: str = meta["id"]
        self.name: str = meta["name"]
        self.version: str = meta["version"]
        self.namespace: str = meta["namespace"]
        self.as_of_date: date = date.fromisoformat(meta["as_of_date"])

        # 构建源值 -> 标准枚举值查表
        self.crm_party_type_map = self._build_source_map("PartyType", "crm")
        self.erp_party_type_map = self._build_source_map("PartyType", "erp")
        self.crm_tier_map = self._build_source_map("CustomerTier", "crm")
        self.wms_shipment_status_map = self._build_source_map("ShipmentStatus", "wms")

        self.erp_order_status_map: dict[str, str] = {}
        for enum_key, spec in self.raw["enums"]["ErpOrderStatus"]["values"].items():
            self.erp_order_status_map[spec["erp_code"]] = enum_key

        self.effective_shipment_statuses: set[str] = {
            enum_key
            for enum_key, spec in self.raw["enums"]["ShipmentStatus"]["values"].items()
            if spec.get("is_effective_shipment")
        }

        self.enum_allowed: dict[str, set[str]] = {
            enum_name: set(enum_def["values"].keys())
            for enum_name, enum_def in self.raw["enums"].items()
        }

        self.constraints: dict[str, dict[str, Any]] = {
            c["code"]: c for c in self.raw["constraints"]
        }

    def _build_source_map(self, enum_name: str, source_key: str) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for enum_key, spec in self.raw["enums"][enum_name]["values"].items():
            for src_val in spec.get("source_mappings", {}).get(source_key, []):
                mapping[src_val] = enum_key
        return mapping

    def assert_enum(self, enum_name: str, value: str) -> str:
        if value not in self.enum_allowed[enum_name]:
            raise ValueError(f"非法枚举值 {enum_name}.{value!r}，允许集合：{sorted(self.enum_allowed[enum_name])}")
        return value


# ---------------------------------------------------------------------------
# 2. 本体实例数据结构（Objects & Links）
# ---------------------------------------------------------------------------


@dataclass
class SalesOwner:
    owner_id: str
    owner_name: str
    team: str


@dataclass
class Customer:
    customer_id: str
    canonical_name: str
    aliases: list[str]
    party_type: str
    tier: str
    credit_code: str | None
    city: str | None
    crm_account_id: str | None
    erp_customer_no: str | None
    owned_by_owner_id: str | None
    source_presence: str
    resolution_method: str
    is_vip: bool


@dataclass
class ProductCategory:
    category_code: str
    category_name: str
    parent_category_code: str | None
    level: int
    is_leaf: bool
    root_category_code: str
    root_category_name: str


@dataclass
class Product:
    sku: str
    product_name: str
    unit: str
    list_price: str
    raw_category_code: str
    raw_category_name: str
    belongs_to_category_code: str | None
    category_status: str


@dataclass
class SalesOrder:
    order_no: str
    placed_by_customer_id: str
    order_date: str
    promised_date: str | None
    erp_status: str
    total_amount: str
    is_cancelled: bool
    fulfillment_status: str
    actual_ship_date: str | None
    is_effectively_shipped: bool
    is_overdue_unshipped: bool
    is_on_time: bool | None


@dataclass
class OrderLine:
    order_line_id: str
    order_no: str
    line_no: int
    sku: str
    quantity: int
    unit_price: str
    line_amount: str


@dataclass
class Shipment:
    shipment_id: str
    raw_order_no: str
    normalized_order_no: str
    fulfills_order_no: str | None
    ship_date: str
    shipment_status: str
    carrier: str
    is_effective: bool
    is_dangling: bool


@dataclass
class DataQualityIssue:
    issue_id: str
    rule_code: str
    rule_name: str
    severity: str
    target_object_type: str
    target_object_id: str
    source_system: str
    description: str
    raw_evidence: str


@dataclass(frozen=True)
class SemanticLink:
    link_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str


@dataclass
class OntologyStore:
    sales_owners: list[SalesOwner] = field(default_factory=list)
    customers: list[Customer] = field(default_factory=list)
    product_categories: list[ProductCategory] = field(default_factory=list)
    products: list[Product] = field(default_factory=list)
    sales_orders: list[SalesOrder] = field(default_factory=list)
    order_lines: list[OrderLine] = field(default_factory=list)
    shipments: list[Shipment] = field(default_factory=list)
    data_quality_issues: list[DataQualityIssue] = field(default_factory=list)
    links: list[SemanticLink] = field(default_factory=list)

    def add_link(
        self, link_type: str, from_type: str, from_id: str, to_type: str, to_id: str
    ) -> None:
        self.links.append(SemanticLink(link_type, from_type, from_id, to_type, to_id))


# ---------------------------------------------------------------------------
# 3. 辅助读取与清洗工具
# ---------------------------------------------------------------------------


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return [
            {k: v.strip() for k, v in row.items()}
            for row in csv.DictReader(f)
        ]


def load_crosswalk(path: Path) -> dict[str, str]:
    """返回 {crm_account_id: erp_customer_no} 人工确认映射字典。"""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    pairs: dict[str, str] = {}
    for entry in data.get("mappings", []):
        crm_id = entry["crm_account_id"].strip()
        erp_no = entry["erp_customer_no"].strip()
        pairs[crm_id] = erp_no
    return pairs


def load_product_category_overrides(path: Path) -> dict[str, str]:
    """返回 {sku: target_leaf_category_code} 产品分类人工治理覆盖字典。"""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    overrides: dict[str, str] = {}
    for entry in data.get("overrides", []):
        sku = entry["sku"].strip()
        target_code = entry["target_leaf_category_code"].strip()
        overrides[sku] = target_code
    return overrides


def parse_erp_date(raw: str) -> tuple[date | None, bool]:
    """解析 ERP 日期，返回 (date对象或None, 是否偏离标准YYYYMMDD格式)。"""
    if not raw:
        return None, False
    if STANDARD_ERP_DATE_RE.match(raw):
        return datetime.strptime(raw, "%Y%m%d").date(), False
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date(), True
        except ValueError:
            continue
    raise ValueError(f"无法解析日期格式：{raw!r}")


def parse_decimal_amount(raw: str) -> tuple[Decimal, bool]:
    """清洗金额字符串，返回 (Decimal金额, 是否存在格式异常)。"""
    if PURE_DECIMAL_RE.match(raw):
        return Decimal(raw).quantize(Decimal("0.01")), False
    cleaned = raw.replace(",", "").replace("¥", "").replace("￥", "").strip()
    return Decimal(cleaned).quantize(Decimal("0.01")), True


def normalize_order_no(raw: str) -> tuple[str, bool]:
    """标准化订单号格式，返回 (标准单号, 是否发生了格式修复)。"""
    m = COMPACT_ORDER_NO_RE.match(raw)
    if m:
        return f"SO-{m.group(1)}-{m.group(2)}", True
    return raw, False


# ---------------------------------------------------------------------------
# 4. 实体解析与语义映射流水线
# ---------------------------------------------------------------------------


class IssueCollector:
    def __init__(self, schema: OntologySchema, store: OntologyStore) -> None:
        self.schema = schema
        self.store = store
        self._seq = 0

    def record(
        self,
        rule_code: str,
        target_object_type: str,
        target_object_id: str,
        source_system: str,
        raw_evidence: str,
        detail: str | None = None,
    ) -> None:
        self._seq += 1
        rule = self.schema.constraints[rule_code]
        desc = rule["description"] if detail is None else f"{rule['description']}（{detail}）"
        self.store.data_quality_issues.append(
            DataQualityIssue(
                issue_id=f"DQ-{self._seq:04d}",
                rule_code=rule_code,
                rule_name=rule["name"],
                severity=self.schema.assert_enum("Severity", rule["severity"]),
                target_object_type=target_object_type,
                target_object_id=target_object_id,
                source_system=source_system,
                description=desc,
                raw_evidence=raw_evidence,
            )
        )


def build_sales_owners(store: OntologyStore) -> dict[str, SalesOwner]:
    owners_by_id: dict[str, SalesOwner] = {}
    for row in read_csv_rows(SOURCES_DIR / "crm" / "owners.csv"):
        owner = SalesOwner(
            owner_id=row["owner_id"],
            owner_name=row["owner_name"],
            team=row["team"],
        )
        store.sales_owners.append(owner)
        owners_by_id[owner.owner_id] = owner
    return owners_by_id


def resolve_customers(
    schema: OntologySchema,
    store: OntologyStore,
    issues: IssueCollector,
    owners_by_id: dict[str, SalesOwner],
    crosswalk: dict[str, str],
) -> dict[str, Customer]:
    """三级客户实体解析：L1 信用代码 -> L2 名称+类型+城市 -> L3 人工 Crosswalk -> 单边实体。"""
    crm_rows = read_csv_rows(SOURCES_DIR / "crm" / "accounts.csv")
    erp_rows = read_csv_rows(SOURCES_DIR / "erp" / "customers.csv")

    crm_by_id = {r["account_id"]: r for r in crm_rows}
    erp_by_no = {r["customer_no"]: r for r in erp_rows}

    matched_crm: set[str] = set()
    matched_erp: set[str] = set()
    # 元素为 (crm_row | None, erp_row | None, resolution_method)
    resolved_pairs: list[tuple[dict[str, str] | None, dict[str, str] | None, str]] = []

    # ---- L1: 统一社会信用代码精确对齐 ----
    erp_by_credit = {
        r["credit_code"]: r for r in erp_rows if r["credit_code"]
    }
    for crm in crm_rows:
        code = crm["credit_code"]
        if code and code in erp_by_credit:
            erp = erp_by_credit[code]
            matched_crm.add(crm["account_id"])
            matched_erp.add(erp["customer_no"])
            resolved_pairs.append((crm, erp, "CREDIT_CODE"))

    # ---- L2: 名称 + 标准主体类型 + 城市精确对齐 ----
    erp_by_exact_key: dict[tuple[str, str, str], dict[str, str]] = {}
    for erp in erp_rows:
        if erp["customer_no"] in matched_erp:
            continue
        ptype = schema.erp_party_type_map[erp["type_code"]]
        erp_by_exact_key[(erp["customer_name"], ptype, erp["city"])] = erp

    for crm in crm_rows:
        if crm["account_id"] in matched_crm:
            continue
        ptype = schema.crm_party_type_map[crm["account_type"]]
        key = (crm["account_name"], ptype, crm["city"])
        if key in erp_by_exact_key:
            erp = erp_by_exact_key[key]
            matched_crm.add(crm["account_id"])
            matched_erp.add(erp["customer_no"])
            resolved_pairs.append((crm, erp, "EXACT_NAME"))

    # ---- L3: 受控人工映射表（Crosswalk）----
    for crm_id, erp_no in crosswalk.items():
        if crm_id in matched_crm or erp_no in matched_erp:
            continue
        if crm_id not in crm_by_id or erp_no not in erp_by_no:
            raise ValueError(f"Crosswalk 映射引用了不存在的源记录：{crm_id} <-> {erp_no}")
        matched_crm.add(crm_id)
        matched_erp.add(erp_no)
        resolved_pairs.append((crm_by_id[crm_id], erp_by_no[erp_no], "MANUAL_CROSSWALK"))

    # ---- 剩余单边系统记录 ----
    for erp in erp_rows:
        if erp["customer_no"] not in matched_erp:
            resolved_pairs.append((None, erp, "SINGLE_SOURCE"))
    for crm in crm_rows:
        if crm["account_id"] not in matched_crm:
            resolved_pairs.append((crm, None, "SINGLE_SOURCE"))

    # 确定性排序：先按 ERP 编号，再按 CRM 账号 ID，保证 CUST-xxxx 稳定可复现
    resolved_pairs.sort(
        key=lambda pair: (
            0 if pair[1] else 1,
            pair[1]["customer_no"] if pair[1] else "",
            pair[0]["account_id"] if pair[0] else "",
        )
    )

    customers_by_erp_no: dict[str, Customer] = {}
    for seq, (crm, erp, method) in enumerate(resolved_pairs, start=1):
        customer_id = f"CUST-{seq:04d}"

        if crm and erp:
            presence = "BOTH"
            canonical_name = erp["customer_name"]
            aliases = [crm["account_name"]] if crm["account_name"] != canonical_name else []
            party_type = schema.erp_party_type_map[erp["type_code"]]
            tier = schema.crm_tier_map[crm["customer_level"]]
            credit_code = erp["credit_code"] or crm["credit_code"] or None
            city = erp["city"] or crm["city"] or None
            owner_id = crm["owner_id"] or None
        elif erp:
            presence = "ERP_ONLY"
            canonical_name = erp["customer_name"]
            aliases = []
            party_type = schema.erp_party_type_map[erp["type_code"]]
            tier = "UNKNOWN"
            credit_code = erp["credit_code"] or None
            city = erp["city"] or None
            owner_id = None
        else:
            assert crm is not None
            presence = "CRM_ONLY"
            canonical_name = crm["account_name"]
            aliases = []
            party_type = schema.crm_party_type_map[crm["account_type"]]
            tier = schema.crm_tier_map[crm["customer_level"]]
            credit_code = crm["credit_code"] or None
            city = crm["city"] or None
            owner_id = crm["owner_id"] or None

        # 严格校验枚举
        schema.assert_enum("PartyType", party_type)
        schema.assert_enum("CustomerTier", tier)
        schema.assert_enum("SourcePresence", presence)
        schema.assert_enum("ResolutionMethod", method)

        if owner_id and owner_id not in owners_by_id:
            raise ValueError(f"未知销售负责人：{owner_id}")

        customer = Customer(
            customer_id=customer_id,
            canonical_name=canonical_name,
            aliases=aliases,
            party_type=party_type,
            tier=tier,
            credit_code=credit_code,
            city=city,
            crm_account_id=crm["account_id"] if crm else None,
            erp_customer_no=erp["customer_no"] if erp else None,
            owned_by_owner_id=owner_id,
            source_presence=presence,
            resolution_method=method,
            is_vip=(tier == "VIP"),
        )
        store.customers.append(customer)

        if owner_id:
            store.add_link("owned_by", "Customer", customer_id, "SalesOwner", owner_id)
        if customer.erp_customer_no:
            customers_by_erp_no[customer.erp_customer_no] = customer

        # ---- 审计规则 DQ-011 & DQ-012 ----
        if crm:
            missing_fields: list[str] = []
            if tier == "UNKNOWN":
                missing_fields.append("customer_level")
            if owner_id is None:
                missing_fields.append("owner_id")
            if party_type != "INDIVIDUAL" and not crm["credit_code"]:
                missing_fields.append("credit_code")
            if missing_fields:
                issues.record(
                    rule_code="DQ-011",
                    target_object_type="Customer",
                    target_object_id=customer_id,
                    source_system="CRM",
                    raw_evidence=f"account_id={crm['account_id']}, missing={missing_fields}",
                    detail=f"缺失字段：{', '.join(missing_fields)}",
                )

        if presence in {"CRM_ONLY", "ERP_ONLY"}:
            src = "CRM" if presence == "CRM_ONLY" else "ERP"
            src_id = customer.crm_account_id or customer.erp_customer_no or ""
            issues.record(
                rule_code="DQ-012",
                target_object_type="Customer",
                target_object_id=customer_id,
                source_system=src,
                raw_evidence=f"source_presence={presence}, source_id={src_id}, name={canonical_name}",
                detail=f"{presence}：{canonical_name}",
            )

    return customers_by_erp_no


def build_product_categories(store: OntologyStore) -> dict[str, ProductCategory]:
    raw_rows = read_csv_rows(SOURCES_DIR / "erp" / "category_codes.csv")
    by_code = {r["category_code"]: r for r in raw_rows}
    has_children = {r["parent_code"] for r in raw_rows if r["parent_code"]}

    def resolve_lineage(code: str) -> tuple[int, str, str]:
        depth = 1
        cur = code
        visited: set[str] = set()
        while by_code[cur]["parent_code"]:
            if cur in visited:
                raise ValueError(f"分类树存在循环引用：{cur}")
            visited.add(cur)
            cur = by_code[cur]["parent_code"]
            depth += 1
        return depth, cur, by_code[cur]["category_name"]

    categories: dict[str, ProductCategory] = {}
    for row in raw_rows:
        code = row["category_code"]
        parent = row["parent_code"] or None
        level, root_code, root_name = resolve_lineage(code)
        cat = ProductCategory(
            category_code=code,
            category_name=row["category_name"],
            parent_category_code=parent,
            level=level,
            is_leaf=(code not in has_children),
            root_category_code=root_code,
            root_category_name=root_name,
        )
        store.product_categories.append(cat)
        categories[code] = cat
        if parent:
            store.add_link("parent_category", "ProductCategory", code, "ProductCategory", parent)

    return categories


def build_products(
    schema: OntologySchema,
    store: OntologyStore,
    issues: IssueCollector,
    categories: dict[str, ProductCategory],
    category_overrides: dict[str, str] | None = None,
) -> dict[str, Product]:
    overrides = category_overrides or {}
    products_by_sku: dict[str, Product] = {}
    for row in read_csv_rows(SOURCES_DIR / "erp" / "products.csv"):
        sku = row["sku"]
        raw_code = row["category_code"]
        raw_name = row["category_name"]
        list_price, _ = parse_decimal_amount(row["list_price"])

        belongs_to: str | None = None
        if sku in overrides:
            override_code = overrides[sku]
            if override_code not in categories or not categories[override_code].is_leaf:
                raise ValueError(
                    f"产品分类覆盖表配置非法：sku={sku}, target_leaf_category_code={override_code!r} 不是合法叶子分类"
                )
            cat_status = "VALID"
            belongs_to = override_code
        elif not raw_code and not raw_name:
            cat_status = "MISSING"
            issues.record(
                rule_code="DQ-007",
                target_object_type="Product",
                target_object_id=sku,
                source_system="ERP",
                raw_evidence=f"sku={sku}, product_name={row['product_name']}, category_code='', category_name=''",
            )
        elif raw_code not in categories:
            cat_status = "UNKNOWN_CODE"
            issues.record(
                rule_code="DQ-009",
                target_object_type="Product",
                target_object_id=sku,
                source_system="ERP",
                raw_evidence=f"sku={sku}, category_code={raw_code!r}, category_name={raw_name!r}",
                detail=f"未知分类码 {raw_code}",
            )
        else:
            std_cat = categories[raw_code]
            if raw_name and raw_name != std_cat.category_name:
                cat_status = "CONFLICT"
                issues.record(
                    rule_code="DQ-008",
                    target_object_type="Product",
                    target_object_id=sku,
                    source_system="ERP",
                    raw_evidence=(
                        f"sku={sku}, category_code={raw_code}({std_cat.category_name}), "
                        f"raw_category_name={raw_name!r}"
                    ),
                    detail=f"码 {raw_code} 对应“{std_cat.category_name}”，但商品写着“{raw_name}”",
                )
            elif not std_cat.is_leaf:
                cat_status = "NON_LEAF"
                belongs_to = std_cat.category_code
                issues.record(
                    rule_code="DQ-010",
                    target_object_type="Product",
                    target_object_id=sku,
                    source_system="ERP",
                    raw_evidence=(
                        f"sku={sku}, category_code={raw_code}({std_cat.category_name}), "
                        f"level={std_cat.level}, is_leaf=False"
                    ),
                    detail=f"直接挂在一级节点 {raw_code}({std_cat.category_name})",
                )
            else:
                cat_status = "VALID"
                belongs_to = std_cat.category_code

        schema.assert_enum("CategoryQualityStatus", cat_status)
        product = Product(
            sku=sku,
            product_name=row["product_name"],
            unit=row["unit"],
            list_price=f"{list_price:.2f}",
            raw_category_code=raw_code,
            raw_category_name=raw_name,
            belongs_to_category_code=belongs_to,
            category_status=cat_status,
        )
        store.products.append(product)
        products_by_sku[sku] = product
        if belongs_to:
            store.add_link("belongs_to_category", "Product", sku, "ProductCategory", belongs_to)

    return products_by_sku


def build_orders_and_shipments(
    schema: OntologySchema,
    store: OntologyStore,
    issues: IssueCollector,
    customers_by_erp_no: dict[str, Customer],
    products_by_sku: dict[str, Product],
) -> None:
    raw_orders = read_csv_rows(SOURCES_DIR / "erp" / "sales_orders.csv")
    raw_items = read_csv_rows(SOURCES_DIR / "erp" / "order_items.csv")
    raw_shipments = read_csv_rows(SOURCES_DIR / "wms" / "shipments.csv")

    known_order_nos = {r["order_no"] for r in raw_orders}

    # ---- 1. 明细行（OrderLine）清洗与汇总 ----
    order_totals: dict[str, Decimal] = {no: Decimal("0.00") for no in known_order_nos}
    for row in raw_items:
        order_no = row["order_no"]
        line_no = int(row["line_no"])
        line_id = f"{order_no}#{line_no}"
        sku = row["sku"]
        if sku not in products_by_sku:
            raise ValueError(f"订单明细引用了未知 SKU：{sku}")

        unit_price, _ = parse_decimal_amount(row["unit_price"])
        line_amount, odd_amount = parse_decimal_amount(row["amount"])
        if odd_amount:
            issues.record(
                rule_code="DQ-006",
                target_object_type="OrderLine",
                target_object_id=line_id,
                source_system="ERP",
                raw_evidence=f"order_no={order_no}, line_no={line_no}, raw_amount={row['amount']!r}",
                detail=f"原始金额 {row['amount']!r} 已清洗为 {line_amount:.2f}",
            )

        line = OrderLine(
            order_line_id=line_id,
            order_no=order_no,
            line_no=line_no,
            sku=sku,
            quantity=int(row["qty"]),
            unit_price=f"{unit_price:.2f}",
            line_amount=f"{line_amount:.2f}",
        )
        store.order_lines.append(line)
        order_totals[order_no] += line_amount

        store.add_link("part_of_order", "OrderLine", line_id, "SalesOrder", order_no)
        store.add_link("for_product", "OrderLine", line_id, "Product", sku)

    # ---- 2. 发货单（Shipment）清洗、单号标准化与链接 ----
    shipments_by_order: dict[str, list[Shipment]] = {no: [] for no in known_order_nos}
    for row in raw_shipments:
        shipment_id = row["shipment_id"]
        raw_order_no = row["order_no"]
        norm_order_no, was_repaired = normalize_order_no(raw_order_no)
        if was_repaired:
            issues.record(
                rule_code="DQ-004",
                target_object_type="Shipment",
                target_object_id=shipment_id,
                source_system="WMS",
                raw_evidence=f"shipment_id={shipment_id}, raw_order_no={raw_order_no!r} -> {norm_order_no!r}",
            )

        ship_status = schema.assert_enum(
            "ShipmentStatus", schema.wms_shipment_status_map[row["status"]]
        )
        is_effective = ship_status in schema.effective_shipment_statuses
        fulfills = norm_order_no if norm_order_no in known_order_nos else None
        is_dangling = fulfills is None

        shipment = Shipment(
            shipment_id=shipment_id,
            raw_order_no=raw_order_no,
            normalized_order_no=norm_order_no,
            fulfills_order_no=fulfills,
            ship_date=date.fromisoformat(row["ship_date"]).isoformat(),
            shipment_status=ship_status,
            carrier=row["carrier"],
            is_effective=is_effective,
            is_dangling=is_dangling,
        )
        store.shipments.append(shipment)

        if fulfills:
            shipments_by_order[fulfills].append(shipment)
            store.add_link("fulfills", "Shipment", shipment_id, "SalesOrder", fulfills)
        else:
            issues.record(
                rule_code="DQ-002",
                target_object_type="Shipment",
                target_object_id=shipment_id,
                source_system="WMS",
                raw_evidence=f"shipment_id={shipment_id}, order_no={norm_order_no}, status={row['status']}",
                detail=f"引用不存在的订单 {norm_order_no}",
            )

    # ---- 3. 销售订单（SalesOrder）构建、WMS 权威履约裁决与跨系统审计 ----
    for row in raw_orders:
        order_no = row["order_no"]
        customer = customers_by_erp_no[row["customer_no"]]

        order_dt, odd_order_dt = parse_erp_date(row["order_date"])
        assert order_dt is not None
        if odd_order_dt:
            issues.record(
                rule_code="DQ-005",
                target_object_type="SalesOrder",
                target_object_id=order_no,
                source_system="ERP",
                raw_evidence=f"order_no={order_no}, raw_order_date={row['order_date']!r}",
                detail=f"原始日期 {row['order_date']!r} 已清洗为 {order_dt.isoformat()}",
            )

        promised_dt, odd_promised_dt = parse_erp_date(row["promised_date"])
        if odd_promised_dt:
            issues.record(
                rule_code="DQ-005",
                target_object_type="SalesOrder",
                target_object_id=order_no,
                source_system="ERP",
                raw_evidence=f"order_no={order_no}, raw_promised_date={row['promised_date']!r}",
            )

        erp_status = schema.assert_enum(
            "ErpOrderStatus", schema.erp_order_status_map[row["status_code"]]
        )
        is_cancelled = erp_status == "CANCELLED"

        if not is_cancelled and promised_dt is None:
            issues.record(
                rule_code="DQ-003",
                target_object_type="SalesOrder",
                target_object_id=order_no,
                source_system="ERP",
                raw_evidence=f"order_no={order_no}, customer={customer.canonical_name}, promised_date=''",
            )

        # 基于 WMS 裁决真实履约状态
        linked_shipments = shipments_by_order[order_no]
        statuses = {s.shipment_status for s in linked_shipments}
        if is_cancelled:
            fulfillment_status = "CANCELLED"
        elif "DELIVERED" in statuses:
            fulfillment_status = "DELIVERED"
        elif "IN_TRANSIT" in statuses:
            fulfillment_status = "IN_TRANSIT"
        elif "AWAITING_DISPATCH" in statuses:
            fulfillment_status = "AWAITING_DISPATCH"
        else:
            fulfillment_status = "UNSHIPPED"
        schema.assert_enum("FulfillmentStatus", fulfillment_status)

        effective_ship_dates = sorted(
            date.fromisoformat(s.ship_date) for s in linked_shipments if s.is_effective
        )
        actual_ship_dt = effective_ship_dates[0] if effective_ship_dates else None
        is_effectively_shipped = fulfillment_status in {"IN_TRANSIT", "DELIVERED"}
        is_overdue_unshipped = (
            (not is_cancelled)
            and (promised_dt is not None)
            and (promised_dt < schema.as_of_date)
            and (not is_effectively_shipped)
        )
        is_on_time: bool | None = (
            (actual_ship_dt <= promised_dt)
            if (actual_ship_dt is not None and promised_dt is not None)
            else None
        )

        order = SalesOrder(
            order_no=order_no,
            placed_by_customer_id=customer.customer_id,
            order_date=order_dt.isoformat(),
            promised_date=promised_dt.isoformat() if promised_dt else None,
            erp_status=erp_status,
            total_amount=f"{order_totals[order_no]:.2f}",
            is_cancelled=is_cancelled,
            fulfillment_status=fulfillment_status,
            actual_ship_date=actual_ship_dt.isoformat() if actual_ship_dt else None,
            is_effectively_shipped=is_effectively_shipped,
            is_overdue_unshipped=is_overdue_unshipped,
            is_on_time=is_on_time,
        )
        store.sales_orders.append(order)
        store.add_link("placed_by", "SalesOrder", order_no, "Customer", customer.customer_id)

        # ---- 跨系统一致性审计 DQ-001：ERP 状态滞后于 WMS ----
        if is_effectively_shipped and erp_status in {"PENDING_SHIPMENT", "PARTIALLY_SHIPPED"}:
            ship_summary = ", ".join(f"{s.shipment_id}({s.shipment_status})" for s in linked_shipments)
            issues.record(
                rule_code="DQ-001",
                target_object_type="SalesOrder",
                target_object_id=order_no,
                source_system="ERP+WMS",
                raw_evidence=f"order_no={order_no}, erp_status={erp_status}, wms={ship_summary}",
                detail=f"ERP={erp_status}，但 WMS={fulfillment_status}",
            )


# ---------------------------------------------------------------------------
# 5. 物化输出：SQLite 本体库 + JSON 语义图谱
# ---------------------------------------------------------------------------


def write_sqlite(db_path: Path, schema: OntologySchema, store: OntologyStore) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE sales_owners (
                owner_id TEXT PRIMARY KEY,
                owner_name TEXT NOT NULL,
                team TEXT NOT NULL
            );

            CREATE TABLE customers (
                customer_id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                aliases_json TEXT NOT NULL,
                party_type TEXT NOT NULL,
                tier TEXT NOT NULL,
                credit_code TEXT,
                city TEXT,
                crm_account_id TEXT,
                erp_customer_no TEXT,
                owned_by_owner_id TEXT,
                source_presence TEXT NOT NULL,
                resolution_method TEXT NOT NULL,
                is_vip INTEGER NOT NULL,
                FOREIGN KEY (owned_by_owner_id) REFERENCES sales_owners(owner_id)
            );

            CREATE TABLE product_categories (
                category_code TEXT PRIMARY KEY,
                category_name TEXT NOT NULL,
                parent_category_code TEXT,
                level INTEGER NOT NULL,
                is_leaf INTEGER NOT NULL,
                root_category_code TEXT NOT NULL,
                root_category_name TEXT NOT NULL,
                FOREIGN KEY (parent_category_code) REFERENCES product_categories(category_code)
            );

            CREATE TABLE products (
                sku TEXT PRIMARY KEY,
                product_name TEXT NOT NULL,
                unit TEXT NOT NULL,
                list_price REAL NOT NULL,
                raw_category_code TEXT NOT NULL,
                raw_category_name TEXT NOT NULL,
                belongs_to_category_code TEXT,
                category_status TEXT NOT NULL,
                FOREIGN KEY (belongs_to_category_code) REFERENCES product_categories(category_code)
            );

            CREATE TABLE sales_orders (
                order_no TEXT PRIMARY KEY,
                placed_by_customer_id TEXT NOT NULL,
                order_date TEXT NOT NULL,
                promised_date TEXT,
                erp_status TEXT NOT NULL,
                total_amount REAL NOT NULL,
                is_cancelled INTEGER NOT NULL,
                fulfillment_status TEXT NOT NULL,
                actual_ship_date TEXT,
                is_effectively_shipped INTEGER NOT NULL,
                is_overdue_unshipped INTEGER NOT NULL,
                is_on_time INTEGER,
                FOREIGN KEY (placed_by_customer_id) REFERENCES customers(customer_id)
            );

            CREATE TABLE order_lines (
                order_line_id TEXT PRIMARY KEY,
                order_no TEXT NOT NULL,
                line_no INTEGER NOT NULL,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price REAL NOT NULL,
                line_amount REAL NOT NULL,
                FOREIGN KEY (order_no) REFERENCES sales_orders(order_no),
                FOREIGN KEY (sku) REFERENCES products(sku)
            );

            CREATE TABLE shipments (
                shipment_id TEXT PRIMARY KEY,
                raw_order_no TEXT NOT NULL,
                normalized_order_no TEXT NOT NULL,
                fulfills_order_no TEXT,
                ship_date TEXT NOT NULL,
                shipment_status TEXT NOT NULL,
                carrier TEXT NOT NULL,
                is_effective INTEGER NOT NULL,
                is_dangling INTEGER NOT NULL,
                FOREIGN KEY (fulfills_order_no) REFERENCES sales_orders(order_no)
            );

            CREATE TABLE data_quality_issues (
                issue_id TEXT PRIMARY KEY,
                rule_code TEXT NOT NULL,
                rule_name TEXT NOT NULL,
                severity TEXT NOT NULL,
                target_object_type TEXT NOT NULL,
                target_object_id TEXT NOT NULL,
                source_system TEXT NOT NULL,
                description TEXT NOT NULL,
                raw_evidence TEXT NOT NULL
            );

            CREATE TABLE links (
                link_type TEXT NOT NULL,
                from_type TEXT NOT NULL,
                from_id TEXT NOT NULL,
                to_type TEXT NOT NULL,
                to_id TEXT NOT NULL,
                PRIMARY KEY (link_type, from_type, from_id, to_type, to_id)
            );

            CREATE INDEX idx_orders_customer ON sales_orders(placed_by_customer_id);
            CREATE INDEX idx_orders_overdue ON sales_orders(is_overdue_unshipped, order_date);
            CREATE INDEX idx_lines_order ON order_lines(order_no);
            CREATE INDEX idx_lines_sku ON order_lines(sku);
            CREATE INDEX idx_shipments_order ON shipments(fulfills_order_no);
            CREATE INDEX idx_dq_rule ON data_quality_issues(rule_code, severity);
            CREATE INDEX idx_links_from ON links(from_type, from_id);
            CREATE INDEX idx_links_to ON links(to_type, to_id);
            """
        )

        cur.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("ontology_id", schema.ontology_id),
                ("ontology_name", schema.name),
                ("ontology_version", schema.version),
                ("namespace", schema.namespace),
                ("as_of_date", schema.as_of_date.isoformat()),
                ("schema_sha256", schema.sha256),
            ],
        )

        cur.executemany(
            "INSERT INTO sales_owners VALUES (?, ?, ?)",
            [(o.owner_id, o.owner_name, o.team) for o in store.sales_owners],
        )

        cur.executemany(
            "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    c.customer_id, c.canonical_name, json.dumps(c.aliases, ensure_ascii=False),
                    c.party_type, c.tier, c.credit_code, c.city, c.crm_account_id,
                    c.erp_customer_no, c.owned_by_owner_id, c.source_presence,
                    c.resolution_method, int(c.is_vip),
                )
                for c in store.customers
            ],
        )

        cur.executemany(
            "INSERT INTO product_categories VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    pc.category_code, pc.category_name, pc.parent_category_code,
                    pc.level, int(pc.is_leaf), pc.root_category_code, pc.root_category_name,
                )
                for pc in store.product_categories
            ],
        )

        cur.executemany(
            "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    p.sku, p.product_name, p.unit, float(p.list_price),
                    p.raw_category_code, p.raw_category_name,
                    p.belongs_to_category_code, p.category_status,
                )
                for p in store.products
            ],
        )

        cur.executemany(
            "INSERT INTO sales_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    o.order_no, o.placed_by_customer_id, o.order_date, o.promised_date,
                    o.erp_status, float(o.total_amount), int(o.is_cancelled),
                    o.fulfillment_status, o.actual_ship_date, int(o.is_effectively_shipped),
                    int(o.is_overdue_unshipped),
                    None if o.is_on_time is None else int(o.is_on_time),
                )
                for o in store.sales_orders
            ],
        )

        cur.executemany(
            "INSERT INTO order_lines VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    ol.order_line_id, ol.order_no, ol.line_no, ol.sku,
                    ol.quantity, float(ol.unit_price), float(ol.line_amount),
                )
                for ol in store.order_lines
            ],
        )

        cur.executemany(
            "INSERT INTO shipments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    s.shipment_id, s.raw_order_no, s.normalized_order_no,
                    s.fulfills_order_no, s.ship_date, s.shipment_status,
                    s.carrier, int(s.is_effective), int(s.is_dangling),
                )
                for s in store.shipments
            ],
        )

        cur.executemany(
            "INSERT INTO data_quality_issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    dq.issue_id, dq.rule_code, dq.rule_name, dq.severity,
                    dq.target_object_type, dq.target_object_id, dq.source_system,
                    dq.description, dq.raw_evidence,
                )
                for dq in store.data_quality_issues
            ],
        )

        cur.executemany(
            "INSERT INTO links VALUES (?, ?, ?, ?, ?)",
            [(lk.link_type, lk.from_type, lk.from_id, lk.to_type, lk.to_id) for lk in store.links],
        )

        conn.commit()
    finally:
        conn.close()


def write_graph_json(graph_path: Path, schema: OntologySchema, store: OntologyStore) -> None:
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {
            "ontology_id": schema.ontology_id,
            "ontology_name": schema.name,
            "version": schema.version,
            "namespace": schema.namespace,
            "as_of_date": schema.as_of_date.isoformat(),
            "schema_sha256": schema.sha256,
        },
        "summary": {
            "object_counts": {
                "SalesOwner": len(store.sales_owners),
                "Customer": len(store.customers),
                "ProductCategory": len(store.product_categories),
                "Product": len(store.products),
                "SalesOrder": len(store.sales_orders),
                "OrderLine": len(store.order_lines),
                "Shipment": len(store.shipments),
                "DataQualityIssue": len(store.data_quality_issues),
            },
            "link_counts": dict(sorted(Counter(lk.link_type for lk in store.links).items())),
            "customer_resolution_counts": dict(
                sorted(Counter(c.resolution_method for c in store.customers).items())
            ),
            "dq_issue_counts": dict(
                sorted(Counter(dq.rule_code for dq in store.data_quality_issues).items())
            ),
        },
        "objects": {
            "SalesOwner": [asdict(x) for x in store.sales_owners],
            "Customer": [asdict(x) for x in store.customers],
            "ProductCategory": [asdict(x) for x in store.product_categories],
            "Product": [asdict(x) for x in store.products],
            "SalesOrder": [asdict(x) for x in store.sales_orders],
            "OrderLine": [asdict(x) for x in store.order_lines],
            "Shipment": [asdict(x) for x in store.shipments],
            "DataQualityIssue": [asdict(x) for x in store.data_quality_issues],
        },
        "links": [asdict(lk) for lk in store.links],
    }
    graph_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 6. 构建摘要报告
# ---------------------------------------------------------------------------


def print_summary(schema: OntologySchema, store: OntologyStore) -> None:
    print(f"=== 本体物化完成：{schema.name} v{schema.version} (AS_OF={schema.as_of_date}) ===")
    print(f"输出数据库：{DB_PATH.relative_to(ROOT_DIR)}")
    print(f"输出图谱：  {GRAPH_PATH.relative_to(ROOT_DIR)}")
    print()

    print("[1] 对象实例统计")
    for label, count in [
        ("SalesOwner", len(store.sales_owners)),
        ("Customer", len(store.customers)),
        ("ProductCategory", len(store.product_categories)),
        ("Product", len(store.products)),
        ("SalesOrder", len(store.sales_orders)),
        ("OrderLine", len(store.order_lines)),
        ("Shipment", len(store.shipments)),
        ("DataQualityIssue", len(store.data_quality_issues)),
    ]:
        print(f"  - {label:<18}: {count}")

    print("\n[2] 客户实体解析结果（12 CRM + 12 ERP -> 14 真实参与方）")
    for c in store.customers:
        alias_txt = f" (别名: {', '.join(c.aliases)})" if c.aliases else ""
        print(
            f"  - {c.customer_id} | {c.canonical_name}{alias_txt} | "
            f"{c.source_presence:<8} | {c.resolution_method:<16} | "
            f"tier={c.tier:<11} | CRM={c.crm_account_id or '-':<8} | ERP={c.erp_customer_no or '-'}"
        )

    print("\n[3] 语义关系（Links）统计")
    for link_type, cnt in sorted(Counter(lk.link_type for lk in store.links).items()):
        print(f"  - {link_type:<22}: {cnt}")

    print("\n[4] 数据质量规则命中汇总（DQ-001 ~ DQ-012）")
    dq_counter = Counter(dq.rule_code for dq in store.data_quality_issues)
    for code, rule in schema.constraints.items():
        print(f"  - {code} [{rule['severity']:<6}] {rule['name']:<28}: {dq_counter.get(code, 0)} 条")


def build_ontology(
    schema_path: Path = SCHEMA_PATH,
    crosswalk_path: Path = CROSSWALK_PATH,
    overrides_path: Path = OVERRIDES_PATH,
    db_path: Path = DB_PATH,
    graph_path: Path = GRAPH_PATH,
    quiet: bool = False,
) -> tuple[OntologySchema, OntologyStore]:
    """执行完整的本体物化流水线，支持传入自定义覆盖表与输出路径。"""
    schema = OntologySchema(schema_path)
    crosswalk = load_crosswalk(crosswalk_path)
    category_overrides = load_product_category_overrides(overrides_path)
    store = OntologyStore()
    issues = IssueCollector(schema, store)

    owners_by_id = build_sales_owners(store)
    customers_by_erp_no = resolve_customers(schema, store, issues, owners_by_id, crosswalk)
    categories = build_product_categories(store)
    products_by_sku = build_products(
        schema, store, issues, categories, category_overrides=category_overrides
    )
    build_orders_and_shipments(schema, store, issues, customers_by_erp_no, products_by_sku)

    write_sqlite(db_path, schema, store)
    write_graph_json(graph_path, schema, store)
    if not quiet:
        print_summary(schema, store)
    return schema, store


def main() -> None:
    build_ontology()


if __name__ == "__main__":
    main()
