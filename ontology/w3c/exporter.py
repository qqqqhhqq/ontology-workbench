#!/usr/bin/env python3
"""W3C 语义网标准导出器：从 ontology/schema.yaml 与 ontology.db 生成标准 Turtle 文件。

产出 3 份严格遵循 W3C 规范的 Turtle 1.1 文件，可直接导入 Apache Jena Fuseki、
GraphDB、Oxigraph、Blazegraph、Stardog 或 Python rdflib / pyshacl：
  1. data/w3c/ontology_tbox.ttl  — OWL 2 / RDFS 类、对象属性（含传递属性）、数据属性与 SKOS 词表
  2. data/w3c/shapes_shacl.ttl   — W3C SHACL 1.0 NodeShape / PropertyShape 与 SHACL-SPARQL 跨系统约束
  3. data/w3c/instances_abox.ttl — 完整 RDF 实例三元组图谱（ABox，含精确 XSD 字面量与语义边）
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_SCHEMA_PATH = ROOT_DIR / "ontology" / "schema.yaml"
DEFAULT_DB_PATH = ROOT_DIR / "data" / "ontology" / "ontology.db"
DEFAULT_W3C_DIR = ROOT_DIR / "data" / "w3c"

BASE_NS = "https://ontology.example.com/b2b-fulfillment#"
RES_NS = "https://ontology.example.com/b2b-fulfillment/resource/"

PREFIX_HEADER = f"""@prefix b2b:  <{BASE_NS}> .
@prefix res:  <{RES_NS}> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
"""

XSD_TYPE_MAP = {
    "string": "xsd:string",
    "integer": "xsd:integer",
    "decimal": "xsd:decimal",
    "date": "xsd:date",
    "boolean": "xsd:boolean",
    "list[string]": "xsd:string",
}


def ttl_str(value: str, lang: str | None = None) -> str:
    """严格按 Turtle 1.1 规范转义字符串字面量。"""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    if lang:
        return f'"{escaped}"@{lang}'
    return f'"{escaped}"'


def ttl_typed(value: str, xsd_type: str) -> str:
    return f"{ttl_str(value)}^^{xsd_type}"


def ttl_bool(value: bool) -> str:
    return "true" if value else "false"


def res_iri(object_type: str, object_id: str) -> str:
    """生成合法 RFC 3987 / Turtle 1.1 资源 IRI（对 # 等保留字符进行百分号编码）。"""
    safe_id = urllib.parse.quote(object_id, safe="-._~")
    return f"<{RES_NS}{object_type}/{safe_id}>"


def enum_iri(enum_name: str, enum_val: str) -> str:
    return f"b2b:{enum_name}_{enum_val}"


def camel_prop(name: str) -> str:
    """将 snake_case 转为标准 OWL/RDF lowerCamelCase 属性名。"""
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


# ---------------------------------------------------------------------------
# 1. TBox：OWL 2 + RDFS + SKOS 受控词表
# ---------------------------------------------------------------------------


def generate_tbox_ttl(schema: dict[str, Any]) -> str:
    meta = schema["ontology"]
    lines: list[str] = [
        PREFIX_HEADER.rstrip(),
        "",
        f"<{meta['namespace'].rstrip('#')}> a owl:Ontology ;",
        f"    dcterms:identifier {ttl_str(meta['id'])} ;",
        f"    rdfs:label {ttl_str(meta['name'], 'zh')} ;",
        f"    owl:versionInfo {ttl_str(meta['version'])} ;",
        f"    b2b:asOfDate {ttl_typed(meta['as_of_date'], 'xsd:date')} ;",
        f"    rdfs:comment {ttl_str('由 ontology/schema.yaml 自动生成的 W3C OWL 2 / RDFS 本体模式层（TBox）。', 'zh')} .",
        "",
        "# =============================================================================",
        "# 1. 本体类定义（OWL Classes）",
        "# =============================================================================",
    ]

    for cls_name, cls_def in schema["object_types"].items():
        lines.extend(
            [
                f"b2b:{cls_name} a owl:Class ;",
                f"    rdfs:label {ttl_str(cls_def['label'], 'zh')} ;",
                f"    rdfs:comment {ttl_str(cls_def['description'], 'zh')} .",
                "",
            ]
        )

    lines.extend(
        [
            "# =============================================================================",
            "# 2. 受控词表（SKOS ConceptScheme + OWL Enumerated Individuals）",
            "# =============================================================================",
        ]
    )
    for enum_name, enum_def in schema["enums"].items():
        desc = enum_def.get("description", enum_name)
        lines.extend(
            [
                f"b2b:{enum_name}Scheme a skos:ConceptScheme ;",
                f"    rdfs:label {ttl_str(enum_name)} ;",
                f"    rdfs:comment {ttl_str(desc, 'zh')} .",
                "",
            ]
        )
        for val_key, val_spec in enum_def["values"].items():
            label = val_spec.get("label", val_key)
            lines.extend(
                [
                    f"{enum_iri(enum_name, val_key)} a skos:Concept, owl:NamedIndividual ;",
                    f"    skos:inScheme b2b:{enum_name}Scheme ;",
                    f"    skos:notation {ttl_str(val_key)} ;",
                    f"    skos:prefLabel {ttl_str(label, 'zh')} .",
                    "",
                ]
            )

    lines.extend(
        [
            "# =============================================================================",
            "# 3. 语义对象属性（OWL ObjectProperties，含逆属性与传递闭包属性）",
            "# =============================================================================",
            "# 传递祖先分类属性：沿 parentCategory 自动推理任意层级的一级大类",
            "b2b:hasAncestorCategory a owl:ObjectProperty, owl:TransitiveProperty ;",
            "    rdfs:domain b2b:ProductCategory ;",
            "    rdfs:range b2b:ProductCategory ;",
            f"    rdfs:label {ttl_str('具有上级祖先分类（传递属性）', 'zh')} .",
            "",
        ]
    )

    for link_name, link_def in schema["link_types"].items():
        prop = camel_prop(link_name)
        stmts = [
            f"b2b:{prop} a owl:ObjectProperty",
            f"    rdfs:domain b2b:{link_def['from']}",
            f"    rdfs:range b2b:{link_def['to']}",
            f"    rdfs:comment {ttl_str(link_def['description'], 'zh')}",
        ]
        if link_name == "parent_category":
            stmts.append("    rdfs:subPropertyOf b2b:hasAncestorCategory")
        if "inverse" in link_def:
            inv_prop = camel_prop(link_def["inverse"])
            stmts.append(f"    owl:inverseOf b2b:{inv_prop}")
        lines.append(" ;\n".join(stmts) + " .")
        lines.append("")
        if "inverse" in link_def:
            inv_prop = camel_prop(link_def["inverse"])
            lines.extend(
                [
                    f"b2b:{inv_prop} a owl:ObjectProperty ;",
                    f"    rdfs:domain b2b:{link_def['to']} ;",
                    f"    rdfs:range b2b:{link_def['from']} ;",
                    f"    owl:inverseOf b2b:{prop} .",
                    "",
                ]
            )

    # 额外语义链接：审计对象与一级根分类推导属性
    lines.extend(
        [
            "b2b:auditsObject a owl:ObjectProperty ;",
            "    rdfs:domain b2b:DataQualityIssue ;",
            f"    rdfs:label {ttl_str('审计目标对象', 'zh')} .",
            "",
            "b2b:rootCategory a owl:ObjectProperty ;",
            "    rdfs:domain b2b:ProductCategory ;",
            "    rdfs:range b2b:ProductCategory ;",
            f"    rdfs:label {ttl_str('所属一级大类（传递闭包上卷结果）', 'zh')} .",
            "",
            "# =============================================================================",
            "# 4. 数据属性（OWL DatatypeProperties）",
            "# =============================================================================",
        ]
    )

    emitted_props: set[str] = set()
    for cls_name, cls_def in schema["object_types"].items():
        for prop_name, prop_def in cls_def["properties"].items():
            p_camel = camel_prop(prop_name)
            if p_camel in emitted_props:
                continue
            emitted_props.add(p_camel)
            raw_type = prop_def["type"]
            if raw_type.startswith("enum("):
                enum_name = raw_type[5:-1]
                lines.extend(
                    [
                        f"b2b:{p_camel} a owl:ObjectProperty ;",
                        f"    rdfs:range skos:Concept ;",
                        f"    rdfs:comment {ttl_str(f'受控词表属性（{enum_name}Scheme）', 'zh')} .",
                        "",
                    ]
                )
            else:
                xsd = XSD_TYPE_MAP.get(raw_type, "xsd:string")
                desc = prop_def.get("description", prop_name)
                lines.extend(
                    [
                        f"b2b:{p_camel} a owl:DatatypeProperty ;",
                        f"    rdfs:range {xsd} ;",
                        f"    rdfs:comment {ttl_str(desc, 'zh')} .",
                        "",
                    ]
                )

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 2. SHACL 1.0：NodeShape / PropertyShape + SHACL-SPARQL 跨系统业务约束
# ---------------------------------------------------------------------------


def generate_shacl_ttl(schema: dict[str, Any]) -> str:
    prefix_decl = f"""b2b:Prefixes a owl:Ontology ;
    sh:declare [
        sh:prefix "b2b" ;
        sh:namespace "{BASE_NS}"^^xsd:anyURI ;
    ] , [
        sh:prefix "xsd" ;
        sh:namespace "http://www.w3.org/2001/XMLSchema#"^^xsd:anyURI ;
    ] .
"""
    return f"""{PREFIX_HEADER.rstrip()}

# =============================================================================
# W3C SHACL 1.0 约束规范（对应 ontology/schema.yaml 中的核心结构与 DQ-001~DQ-012 规则）
# 可直接被 pyshacl、TopBraid SHACL、Apache Jena SHACL 或 GraphDB SHACL 引擎执行校验。
# =============================================================================

{prefix_decl}

# ---- 1. Customer 实体约束（含 DQ-011 / DQ-012）----
b2b:CustomerShape a sh:NodeShape ;
    sh:targetClass b2b:Customer ;
    sh:property [
        sh:path b2b:customerId ;
        sh:datatype xsd:string ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:pattern "^CUST-[0-9]{{4}}$" ;
    ] ;
    sh:property [
        sh:path b2b:canonicalName ;
        sh:datatype xsd:string ;
        sh:minCount 1 ;
    ] ;
    sh:property [
        sh:path b2b:tier ;
        sh:minCount 1 ;
        sh:in ( b2b:CustomerTier_VIP b2b:CustomerTier_KEY_ACCOUNT b2b:CustomerTier_STANDARD b2b:CustomerTier_PROSPECT b2b:CustomerTier_UNKNOWN ) ;
    ] ;
    sh:property [
        sh:path b2b:partyType ;
        sh:minCount 1 ;
        sh:in ( b2b:PartyType_ORGANIZATION b2b:PartyType_SOLE_PROPRIETOR b2b:PartyType_INDIVIDUAL ) ;
    ] ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:severity sh:Info ;
        sh:message "DQ-012: 客户仅在单一源系统建档（CRM_ONLY 或 ERP_ONLY）"@zh ;
        sh:prefixes b2b:Prefixes ;
        sh:select \"\"\"
            SELECT $this
            WHERE {{
                $this b2b:sourcePresence ?presence .
                FILTER (?presence IN (b2b:SourcePresence_CRM_ONLY, b2b:SourcePresence_ERP_ONLY))
            }}
        \"\"\" ;
    ] .

# ---- 2. Product 分类合规约束（DQ-007 / DQ-008 / DQ-009 / DQ-010）----
b2b:ProductShape a sh:NodeShape ;
    sh:targetClass b2b:Product ;
    sh:property [
        sh:path b2b:sku ;
        sh:datatype xsd:string ;
        sh:minCount 1 ;
        sh:pattern "^P-[0-9]{{4}}$" ;
    ] ;
    sh:property [
        sh:path b2b:belongsToCategory ;
        sh:class b2b:ProductCategory ;
        sh:minCount 1 ;
        sh:severity sh:Violation ;
        sh:message "DQ-007/008/009: 产品缺少可信的标准分类链接（未维护、码名冲突或未知分类码）"@zh ;
    ] ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:severity sh:Warning ;
        sh:message "DQ-010: 产品直接挂在非叶子节点分类上，缺少二级细类定义"@zh ;
        sh:prefixes b2b:Prefixes ;
        sh:select \"\"\"
            SELECT $this ?cat
            WHERE {{
                $this b2b:belongsToCategory ?cat .
                ?cat b2b:isLeaf false .
            }}
        \"\"\" ;
    ] .

# ---- 3. SalesOrder 跨系统履约与完整性约束（DQ-001 / DQ-003）----
b2b:SalesOrderShape a sh:NodeShape ;
    sh:targetClass b2b:SalesOrder ;
    sh:property [
        sh:path b2b:orderNo ;
        sh:datatype xsd:string ;
        sh:minCount 1 ;
        sh:pattern "^SO-[0-9]{{8}}-[0-9]{{3}}$" ;
    ] ;
    sh:property [
        sh:path b2b:placedBy ;
        sh:class b2b:Customer ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
    ] ;
    sh:property [
        sh:path b2b:orderDate ;
        sh:datatype xsd:date ;
        sh:minCount 1 ;
    ] ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:severity sh:Violation ;
        sh:message "DQ-003: 未取消订单缺少承诺交付日期（promisedDate）"@zh ;
        sh:prefixes b2b:Prefixes ;
        sh:select \"\"\"
            SELECT $this
            WHERE {{
                $this b2b:isCancelled false .
                FILTER NOT EXISTS {{ $this b2b:promisedDate ?d }}
            }}
        \"\"\" ;
    ] ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:severity sh:Violation ;
        sh:message "DQ-001: WMS 已实际出库或签收，但 ERP 订单状态仍滞留在待发货或部分发货"@zh ;
        sh:prefixes b2b:Prefixes ;
        sh:select \"\"\"
            SELECT $this ?erpStatus
            WHERE {{
                $this b2b:isEffectivelyShipped true ;
                      b2b:erpStatus ?erpStatus .
                FILTER (?erpStatus IN (b2b:ErpOrderStatus_PENDING_SHIPMENT, b2b:ErpOrderStatus_PARTIALLY_SHIPPED))
            }}
        \"\"\" ;
    ] .

# ---- 4. Shipment 关联完整性约束（DQ-002 / DQ-004）----
b2b:ShipmentShape a sh:NodeShape ;
    sh:targetClass b2b:Shipment ;
    sh:property [
        sh:path b2b:fulfills ;
        sh:class b2b:SalesOrder ;
        sh:minCount 1 ;
        sh:severity sh:Violation ;
        sh:message "DQ-002: WMS 发货单引用了 ERP 中不存在的订单（孤立发货单）"@zh ;
    ] ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:severity sh:Warning ;
        sh:message "DQ-004: WMS 发货单原始订单号格式不规范（丢失连字符）"@zh ;
        sh:prefixes b2b:Prefixes ;
        sh:select \"\"\"
            SELECT $this ?raw ?norm
            WHERE {{
                $this b2b:rawOrderNo ?raw ;
                      b2b:normalizedOrderNo ?norm .
                FILTER (?raw != ?norm)
            }}
        \"\"\" ;
    ] .
"""


# ---------------------------------------------------------------------------
# 3. ABox：RDF 1.1 Turtle 完整实例三元组导出
# ---------------------------------------------------------------------------


def generate_abox_ttl(db_path: Path) -> tuple[str, int]:
    """从 ontology.db 导出全部 8 类对象及语义边为标准 RDF 1.1 Turtle，返回 (ttl文本, 估算三元组数)。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    lines: list[str] = [PREFIX_HEADER.rstrip(), ""]
    triple_count = 0

    def emit_subject(iri: str, predicates: list[tuple[str, str]]) -> None:
        nonlocal triple_count
        if not predicates:
            return
        triple_count += len(predicates)
        body = " ;\n".join(f"    {p} {o}" for p, o in predicates)
        lines.append(f"{iri}\n{body} .")
        lines.append("")

    try:
        # 1. SalesOwner
        lines.append("# ---- SalesOwner ----")
        for r in conn.execute("SELECT * FROM sales_owners ORDER BY owner_id"):
            emit_subject(
                res_iri("SalesOwner", r["owner_id"]),
                [
                    ("a", "b2b:SalesOwner"),
                    ("rdfs:label", ttl_str(r["owner_name"], "zh")),
                    ("b2b:ownerId", ttl_str(r["owner_id"])),
                    ("b2b:ownerName", ttl_str(r["owner_name"])),
                    ("b2b:team", ttl_str(r["team"])),
                ],
            )

        # 2. Customer
        lines.append("# ---- Customer ----")
        for r in conn.execute("SELECT * FROM customers ORDER BY customer_id"):
            preds: list[tuple[str, str]] = [
                ("a", "b2b:Customer"),
                ("rdfs:label", ttl_str(r["canonical_name"], "zh")),
                ("b2b:customerId", ttl_str(r["customer_id"])),
                ("b2b:canonicalName", ttl_str(r["canonical_name"])),
                ("b2b:partyType", enum_iri("PartyType", r["party_type"])),
                ("b2b:tier", enum_iri("CustomerTier", r["tier"])),
                ("b2b:sourcePresence", enum_iri("SourcePresence", r["source_presence"])),
                ("b2b:resolutionMethod", enum_iri("ResolutionMethod", r["resolution_method"])),
                ("b2b:isVip", ttl_bool(bool(r["is_vip"]))),
            ]
            for alias in json.loads(r["aliases_json"]):
                preds.append(("skos:altLabel", ttl_str(alias, "zh")))
                preds.append(("b2b:aliases", ttl_str(alias)))
            if r["credit_code"]:
                preds.append(("b2b:creditCode", ttl_str(r["credit_code"])))
            if r["city"]:
                preds.append(("b2b:city", ttl_str(r["city"])))
            if r["crm_account_id"]:
                preds.append(("b2b:crmAccountId", ttl_str(r["crm_account_id"])))
            if r["erp_customer_no"]:
                preds.append(("b2b:erpCustomerNo", ttl_str(r["erp_customer_no"])))
            if r["owned_by_owner_id"]:
                preds.append(("b2b:ownedBy", res_iri("SalesOwner", r["owned_by_owner_id"])))
            emit_subject(res_iri("Customer", r["customer_id"]), preds)

        # 3. ProductCategory
        lines.append("# ---- ProductCategory ----")
        for r in conn.execute("SELECT * FROM product_categories ORDER BY category_code"):
            preds = [
                ("a", "b2b:ProductCategory"),
                ("rdfs:label", ttl_str(r["category_name"], "zh")),
                ("b2b:categoryCode", ttl_str(r["category_code"])),
                ("b2b:categoryName", ttl_str(r["category_name"])),
                ("b2b:level", ttl_typed(str(r["level"]), "xsd:integer")),
                ("b2b:isLeaf", ttl_bool(bool(r["is_leaf"]))),
                ("b2b:rootCategory", res_iri("ProductCategory", r["root_category_code"])),
            ]
            if r["parent_category_code"]:
                preds.append(("b2b:parentCategory", res_iri("ProductCategory", r["parent_category_code"])))
            emit_subject(res_iri("ProductCategory", r["category_code"]), preds)

        # 4. Product
        lines.append("# ---- Product ----")
        for r in conn.execute("SELECT * FROM products ORDER BY sku"):
            preds = [
                ("a", "b2b:Product"),
                ("rdfs:label", ttl_str(r["product_name"], "zh")),
                ("b2b:sku", ttl_str(r["sku"])),
                ("b2b:productName", ttl_str(r["product_name"])),
                ("b2b:unit", ttl_str(r["unit"])),
                ("b2b:listPrice", ttl_typed(f"{float(r['list_price']):.2f}", "xsd:decimal")),
                ("b2b:rawCategoryCode", ttl_str(r["raw_category_code"])),
                ("b2b:rawCategoryName", ttl_str(r["raw_category_name"])),
                ("b2b:categoryStatus", enum_iri("CategoryQualityStatus", r["category_status"])),
            ]
            if r["belongs_to_category_code"]:
                preds.append(("b2b:belongsToCategory", res_iri("ProductCategory", r["belongs_to_category_code"])))
            emit_subject(res_iri("Product", r["sku"]), preds)

        # 5. SalesOrder
        lines.append("# ---- SalesOrder ----")
        for r in conn.execute("SELECT * FROM sales_orders ORDER BY order_no"):
            preds = [
                ("a", "b2b:SalesOrder"),
                ("rdfs:label", ttl_str(r["order_no"])),
                ("b2b:orderNo", ttl_str(r["order_no"])),
                ("b2b:placedBy", res_iri("Customer", r["placed_by_customer_id"])),
                ("b2b:orderDate", ttl_typed(r["order_date"], "xsd:date")),
                ("b2b:erpStatus", enum_iri("ErpOrderStatus", r["erp_status"])),
                ("b2b:totalAmount", ttl_typed(f"{float(r['total_amount']):.2f}", "xsd:decimal")),
                ("b2b:isCancelled", ttl_bool(bool(r["is_cancelled"]))),
                ("b2b:fulfillmentStatus", enum_iri("FulfillmentStatus", r["fulfillment_status"])),
                ("b2b:isEffectivelyShipped", ttl_bool(bool(r["is_effectively_shipped"]))),
                ("b2b:isOverdueUnshipped", ttl_bool(bool(r["is_overdue_unshipped"]))),
            ]
            if r["promised_date"]:
                preds.append(("b2b:promisedDate", ttl_typed(r["promised_date"], "xsd:date")))
            if r["actual_ship_date"]:
                preds.append(("b2b:actualShipDate", ttl_typed(r["actual_ship_date"], "xsd:date")))
            if r["is_on_time"] is not None:
                preds.append(("b2b:isOnTime", ttl_bool(bool(r["is_on_time"]))))
            emit_subject(res_iri("SalesOrder", r["order_no"]), preds)

        # 6. OrderLine
        lines.append("# ---- OrderLine ----")
        for r in conn.execute("SELECT * FROM order_lines ORDER BY order_line_id"):
            emit_subject(
                res_iri("OrderLine", r["order_line_id"]),
                [
                    ("a", "b2b:OrderLine"),
                    ("b2b:orderLineId", ttl_str(r["order_line_id"])),
                    ("b2b:partOfOrder", res_iri("SalesOrder", r["order_no"])),
                    ("b2b:forProduct", res_iri("Product", r["sku"])),
                    ("b2b:lineNo", ttl_typed(str(r["line_no"]), "xsd:integer")),
                    ("b2b:quantity", ttl_typed(str(r["quantity"]), "xsd:integer")),
                    ("b2b:unitPrice", ttl_typed(f"{float(r['unit_price']):.2f}", "xsd:decimal")),
                    ("b2b:lineAmount", ttl_typed(f"{float(r['line_amount']):.2f}", "xsd:decimal")),
                ],
            )

        # 7. Shipment
        lines.append("# ---- Shipment ----")
        for r in conn.execute("SELECT * FROM shipments ORDER BY shipment_id"):
            preds = [
                ("a", "b2b:Shipment"),
                ("rdfs:label", ttl_str(r["shipment_id"])),
                ("b2b:shipmentId", ttl_str(r["shipment_id"])),
                ("b2b:rawOrderNo", ttl_str(r["raw_order_no"])),
                ("b2b:normalizedOrderNo", ttl_str(r["normalized_order_no"])),
                ("b2b:shipDate", ttl_typed(r["ship_date"], "xsd:date")),
                ("b2b:shipmentStatus", enum_iri("ShipmentStatus", r["shipment_status"])),
                ("b2b:carrier", ttl_str(r["carrier"])),
                ("b2b:isEffective", ttl_bool(bool(r["is_effective"]))),
                ("b2b:isDangling", ttl_bool(bool(r["is_dangling"]))),
            ]
            if r["fulfills_order_no"]:
                preds.append(("b2b:fulfills", res_iri("SalesOrder", r["fulfills_order_no"])))
            emit_subject(res_iri("Shipment", r["shipment_id"]), preds)

        # 8. DataQualityIssue
        lines.append("# ---- DataQualityIssue ----")
        for r in conn.execute("SELECT * FROM data_quality_issues ORDER BY issue_id"):
            emit_subject(
                res_iri("DataQualityIssue", r["issue_id"]),
                [
                    ("a", "b2b:DataQualityIssue"),
                    ("b2b:issueId", ttl_str(r["issue_id"])),
                    ("b2b:ruleCode", ttl_str(r["rule_code"])),
                    ("b2b:severity", enum_iri("Severity", r["severity"])),
                    ("b2b:targetObjectType", ttl_str(r["target_object_type"])),
                    ("b2b:targetObjectId", ttl_str(r["target_object_id"])),
                    ("b2b:auditsObject", res_iri(r["target_object_type"], r["target_object_id"])),
                    ("b2b:sourceSystem", ttl_str(r["source_system"])),
                    ("b2b:description", ttl_str(r["description"])),
                    ("b2b:rawEvidence", ttl_str(r["raw_evidence"])),
                ],
            )
    finally:
        conn.close()

    return "\n".join(lines).rstrip() + "\n", triple_count


def export_all_w3c(
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    db_path: Path = DEFAULT_DB_PATH,
    out_dir: Path = DEFAULT_W3C_DIR,
) -> dict[str, Any]:
    """一键导出 W3C TBox / SHACL / ABox 三份标准 Turtle 文件并返回统计信息。"""
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)

    tbox_ttl = generate_tbox_ttl(schema)
    shacl_ttl = generate_shacl_ttl(schema)
    abox_ttl, abox_triples = generate_abox_ttl(db_path)

    tbox_path = out_dir / "ontology_tbox.ttl"
    shacl_path = out_dir / "shapes_shacl.ttl"
    abox_path = out_dir / "instances_abox.ttl"

    tbox_path.write_text(tbox_ttl, encoding="utf-8")
    shacl_path.write_text(shacl_ttl, encoding="utf-8")
    abox_path.write_text(abox_ttl, encoding="utf-8")

    return {
        "tbox_path": str(tbox_path),
        "shacl_path": str(shacl_path),
        "abox_path": str(abox_path),
        "abox_triples": abox_triples,
    }
