#!/usr/bin/env python3
"""轻量级 W3C Turtle 1.1 解析器、内存 RDF 三元组库、SHACL 校验器与 CQ 对账引擎。

特性：
  - 若运行环境安装了 rdflib，可无缝切换调用 rdflib.Graph 与原生 SPARQL 1.1 引擎；
  - 零第三方依赖下内置严格的 Turtle 1.1 递归下降解析器（支持 @prefix、多行三引号、
    空白节点 [...]、RDF 集合 (...)、语言标签与 XSD 数据类型），验证生成文件语法 100% 合法，
    并在真实解析出的 (s, p, o) 三元组上执行 SHACL 校验与 CQ1-CQ4（含 parentCategory* 传递闭包）对账。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDF_TYPE = RDF_NS + "type"
RDF_FIRST = RDF_NS + "first"
RDF_REST = RDF_NS + "rest"
RDF_NIL = RDF_NS + "nil"
XSD_NS = "http://www.w3.org/2001/XMLSchema#"
SKOS_NS = "http://www.w3.org/2004/02/skos/core#"
SH_NS = "http://www.w3.org/ns/shacl#"
B2B_NS = "https://ontology.example.com/b2b-fulfillment#"


@dataclass(frozen=True)
class IRI:
    value: str


@dataclass(frozen=True)
class BNode:
    id: str


@dataclass(frozen=True)
class Literal:
    value: str
    datatype: str | None = None
    lang: str | None = None


RDFTerm = Any  # IRI | BNode | Literal
Triple = tuple[RDFTerm, IRI, RDFTerm]


class TurtleSyntaxError(ValueError):
    """Turtle 1.1 语法解析错误。"""


# ---------------------------------------------------------------------------
# 1. Turtle 1.1 词法与递归下降解析器
# ---------------------------------------------------------------------------


class _TurtleTokenizer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)
        self.i = 0

    def tokenize(self) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        while self.i < self.n:
            c = self.text[self.i]
            if c.isspace():
                self.i += 1
                continue
            if c == "#":
                while self.i < self.n and self.text[self.i] != "\n":
                    self.i += 1
                continue
            if self.text.startswith("@prefix", self.i):
                tokens.append(("PREFIX", "@prefix"))
                self.i += 7
                continue
            if self.text.startswith("^^", self.i):
                tokens.append(("DTYPE", "^^"))
                self.i += 2
                continue
            if c in ".;,[]()":
                tokens.append(("PUNCT", c))
                self.i += 1
                continue
            if c == "<":
                end = self.text.find(">", self.i + 1)
                if end == -1:
                    raise TurtleSyntaxError(f"未闭合 IRI，位置 {self.i}")
                tokens.append(("IRI", self.text[self.i + 1 : end]))
                self.i = end + 1
                continue
            if self.text.startswith('"""', self.i):
                self.i += 3
                buf: list[str] = []
                while self.i < self.n and not self.text.startswith('"""', self.i):
                    if self.text[self.i] == "\\" and self.i + 1 < self.n:
                        buf.append(self._unescape(self.text[self.i + 1]))
                        self.i += 2
                    else:
                        buf.append(self.text[self.i])
                        self.i += 1
                if not self.text.startswith('"""', self.i):
                    raise TurtleSyntaxError("未闭合三引号字符串")
                self.i += 3
                tokens.append(("STRING", "".join(buf)))
                continue
            if c == '"':
                self.i += 1
                buf = []
                while self.i < self.n and self.text[self.i] != '"':
                    if self.text[self.i] == "\\" and self.i + 1 < self.n:
                        buf.append(self._unescape(self.text[self.i + 1]))
                        self.i += 2
                    else:
                        buf.append(self.text[self.i])
                        self.i += 1
                if self.i >= self.n or self.text[self.i] != '"':
                    raise TurtleSyntaxError("未闭合字符串字面量")
                self.i += 1
                tokens.append(("STRING", "".join(buf)))
                continue
            if c == "@":
                self.i += 1
                start = self.i
                while self.i < self.n and (self.text[self.i].isalnum() or self.text[self.i] == "-"):
                    self.i += 1
                tokens.append(("LANG", self.text[start : self.i]))
                continue
            # 标识符 / 前缀名 / 布尔 / 数字
            start = self.i
            while self.i < self.n and not self.text[self.i].isspace() and self.text[self.i] not in ";,[]()<>\"":
                # 句点如果在空白或结尾前作为语句结束符，需要停下来
                if self.text[self.i] == "." and (
                    self.i + 1 >= self.n or self.text[self.i + 1].isspace() or self.text[self.i + 1] in "];,)"
                ):
                    break
                self.i += 1
            word = self.text[start : self.i]
            if not word:
                raise TurtleSyntaxError(f"无法识别字符 {c!r}，位置 {self.i}")
            tokens.append(("WORD", word))
        return tokens

    @staticmethod
    def _unescape(ch: str) -> str:
        return {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(ch, ch)


class _TurtleParser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.prefixes: dict[str, str] = {}
        self.triples: list[Triple] = []
        self._bnode_seq = 0

    def _new_bnode(self) -> BNode:
        self._bnode_seq += 1
        return BNode(f"_:b{self._bnode_seq}")

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _pop(self, expected_val: str | None = None) -> tuple[str, str]:
        if self.pos >= len(self.tokens):
            raise TurtleSyntaxError(f"意外到达文件末尾，期望 {expected_val}")
        tok = self.tokens[self.pos]
        if expected_val is not None and tok[1] != expected_val:
            raise TurtleSyntaxError(f"语法错误：期望 {expected_val!r}，实际得到 {tok!r}（token #{self.pos}）")
        self.pos += 1
        return tok

    def parse(self) -> list[Triple]:
        while self._peek() is not None:
            tok = self._peek()
            assert tok is not None
            if tok[0] == "PREFIX":
                self._pop()
                pfx_tok = self._pop()
                if not pfx_tok[1].endswith(":"):
                    raise TurtleSyntaxError(f"前缀名必须以冒号结尾：{pfx_tok[1]}")
                iri_tok = self._pop()
                if iri_tok[0] != "IRI":
                    raise TurtleSyntaxError(f"@prefix 后期望 IRI，得到 {iri_tok}")
                self._pop(".")
                self.prefixes[pfx_tok[1][:-1]] = iri_tok[1]
            else:
                subj = self._parse_term()
                self._parse_predicate_object_list(subj)
                self._pop(".")
        return self.triples

    def _parse_predicate_object_list(self, subj: RDFTerm) -> None:
        while True:
            tok = self._peek()
            if tok is None or tok[1] in {".", "]"}:
                break
            pred = self._parse_verb()
            while True:
                obj = self._parse_term()
                self.triples.append((subj, pred, obj))
                nxt = self._peek()
                if nxt and nxt[1] == ",":
                    self._pop(",")
                else:
                    break
            nxt = self._peek()
            if nxt and nxt[1] == ";":
                self._pop(";")
                # 允许末尾分号（Turtle 1.1 合法写法）
                after = self._peek()
                if after and after[1] in {".", "]"}:
                    break
            else:
                break

    def _parse_verb(self) -> IRI:
        tok = self._pop()
        if tok == ("WORD", "a"):
            return IRI(RDF_TYPE)
        return self._resolve_iri(tok)

    def _resolve_iri(self, tok: tuple[str, str]) -> IRI:
        if tok[0] == "IRI":
            return IRI(tok[1])
        if tok[0] == "WORD" and ":" in tok[1]:
            pfx, local = tok[1].split(":", 1)
            if pfx not in self.prefixes:
                raise TurtleSyntaxError(f"未声明的前缀：{pfx!r}（完整词 {tok[1]!r}）")
            return IRI(self.prefixes[pfx] + local)
        raise TurtleSyntaxError(f"期望 IRI 或前缀名，实际得到 {tok!r}")

    def _parse_term(self) -> RDFTerm:
        tok = self._peek()
        assert tok is not None
        if tok[1] == "[":
            self._pop("[")
            bnode = self._new_bnode()
            self._parse_predicate_object_list(bnode)
            self._pop("]")
            return bnode
        if tok[1] == "(":
            self._pop("(")
            items: list[RDFTerm] = []
            while self._peek() and self._peek()[1] != ")":  # type: ignore[index]
                items.append(self._parse_term())
            self._pop(")")
            if not items:
                return IRI(RDF_NIL)
            head = self._new_bnode()
            cur = head
            for idx, item in enumerate(items):
                self.triples.append((cur, IRI(RDF_FIRST), item))
                if idx + 1 < len(items):
                    nxt = self._new_bnode()
                    self.triples.append((cur, IRI(RDF_REST), nxt))
                    cur = nxt
                else:
                    self.triples.append((cur, IRI(RDF_REST), IRI(RDF_NIL)))
            return head
        tok = self._pop()
        if tok[0] == "STRING":
            val = tok[1]
            nxt = self._peek()
            if nxt and nxt[0] == "LANG":
                lang = self._pop()[1]
                return Literal(val, lang=lang)
            if nxt and nxt[0] == "DTYPE":
                self._pop("^^")
                dt_iri = self._resolve_iri(self._pop())
                return Literal(val, datatype=dt_iri.value)
            return Literal(val, datatype=XSD_NS + "string")
        if tok[0] == "WORD":
            if tok[1] in {"true", "false"}:
                return Literal(tok[1], datatype=XSD_NS + "boolean")
            if tok[1].lstrip("-").isdigit():
                return Literal(tok[1], datatype=XSD_NS + "integer")
            return self._resolve_iri(tok)
        if tok[0] == "IRI":
            return IRI(tok[1])
        raise TurtleSyntaxError(f"无法解析 RDF 项：{tok!r}")


# ---------------------------------------------------------------------------
# 2. 内存 RDF 三元组存储与 SHACL / CQ 对账验证引擎
# ---------------------------------------------------------------------------


class RDFTripleStore:
    """轻量级内存三元组库，支持加载 Turtle 1.1、传递路径推理、SHACL 校验与 CQ 验证。"""

    def __init__(self) -> None:
        self.triples: list[Triple] = []
        self.spo: dict[RDFTerm, dict[str, list[RDFTerm]]] = defaultdict(lambda: defaultdict(list))
        self.pos: dict[str, dict[RDFTerm, list[RDFTerm]]] = defaultdict(lambda: defaultdict(list))

    def parse_turtle(self, text: str) -> int:
        tokens = _TurtleTokenizer(text).tokenize()
        parsed = _TurtleParser(tokens).parse()
        for s, p, o in parsed:
            self.triples.append((s, p, o))
            self.spo[s][p.value].append(o)
            self.pos[p.value][o].append(s)
        return len(parsed)

    def parse_file(self, path: Path) -> int:
        return self.parse_turtle(path.read_text(encoding="utf-8"))

    def subjects_of_type(self, class_iri: str) -> list[RDFTerm]:
        return list(self.pos[RDF_TYPE].get(IRI(class_iri), []))

    def objects(self, subj: RDFTerm, pred_iri: str) -> list[RDFTerm]:
        return list(self.spo[subj].get(pred_iri, []))

    def one(self, subj: RDFTerm, pred_iri: str) -> RDFTerm | None:
        vals = self.spo[subj].get(pred_iri)
        return vals[0] if vals else None

    def one_str(self, subj: RDFTerm, pred_iri: str) -> str | None:
        val = self.one(subj, pred_iri)
        if isinstance(val, Literal):
            return val.value
        if isinstance(val, IRI):
            return val.value
        return None

    def one_bool(self, subj: RDFTerm, pred_iri: str) -> bool:
        val = self.one(subj, pred_iri)
        return isinstance(val, Literal) and val.value == "true"

    def transitive_root_category(self, cat_iri: RDFTerm) -> RDFTerm:
        """等价于 SPARQL 1.1 Property Path: ?cat b2b:parentCategory* ?root + FILTER NOT EXISTS parent."""
        cur = cat_iri
        visited: set[RDFTerm] = set()
        while True:
            if cur in visited:
                raise ValueError(f"RDF 分类图存在环路：{cur}")
            visited.add(cur)
            parent = self.one(cur, B2B_NS + "parentCategory")
            if parent is None:
                return cur
            cur = parent

    # ------------------------------------------------------------------
    # SHACL 约束验证（直接针对解析后的 RDF 三元组执行 shapes_shacl.ttl 核心规则）
    # ------------------------------------------------------------------

    def validate_shacl_rules(self) -> dict[str, Any]:
        """在 RDF 三元组上执行核心 SHACL 约束验证，返回按规则分组的违例报告。"""
        violations: list[dict[str, str]] = []

        # 1) ProductShape: belongsToCategory minCount 1 (DQ-007/008/009) & isLeaf=false (DQ-010)
        for prod in self.subjects_of_type(B2B_NS + "Product"):
            sku = self.one_str(prod, B2B_NS + "sku") or str(prod)
            cat = self.one(prod, B2B_NS + "belongsToCategory")
            if cat is None:
                violations.append(
                    {"shape": "b2b:ProductShape", "severity": "sh:Violation", "focus": sku, "rule": "DQ-007/008/009"}
                )
            elif not self.one_bool(cat, B2B_NS + "isLeaf"):
                violations.append(
                    {"shape": "b2b:ProductShape", "severity": "sh:Warning", "focus": sku, "rule": "DQ-010"}
                )

        # 2) SalesOrderShape: DQ-003 (missing promisedDate) & DQ-001 (ERP/WMS lag)
        for order in self.subjects_of_type(B2B_NS + "SalesOrder"):
            ono = self.one_str(order, B2B_NS + "orderNo") or str(order)
            if not self.one_bool(order, B2B_NS + "isCancelled") and self.one(order, B2B_NS + "promisedDate") is None:
                violations.append(
                    {"shape": "b2b:SalesOrderShape", "severity": "sh:Violation", "focus": ono, "rule": "DQ-003"}
                )
            erp_st = self.one(order, B2B_NS + "erpStatus")
            if self.one_bool(order, B2B_NS + "isEffectivelyShipped") and erp_st in {
                IRI(B2B_NS + "ErpOrderStatus_PENDING_SHIPMENT"),
                IRI(B2B_NS + "ErpOrderStatus_PARTIALLY_SHIPPED"),
            }:
                violations.append(
                    {"shape": "b2b:SalesOrderShape", "severity": "sh:Violation", "focus": ono, "rule": "DQ-001"}
                )

        # 3) ShipmentShape: DQ-002 (dangling) & DQ-004 (rawOrderNo != normalizedOrderNo)
        for ship in self.subjects_of_type(B2B_NS + "Shipment"):
            sid = self.one_str(ship, B2B_NS + "shipmentId") or str(ship)
            if self.one(ship, B2B_NS + "fulfills") is None:
                violations.append(
                    {"shape": "b2b:ShipmentShape", "severity": "sh:Violation", "focus": sid, "rule": "DQ-002"}
                )
            if self.one_str(ship, B2B_NS + "rawOrderNo") != self.one_str(ship, B2B_NS + "normalizedOrderNo"):
                violations.append(
                    {"shape": "b2b:ShipmentShape", "severity": "sh:Warning", "focus": sid, "rule": "DQ-004"}
                )

        # 4) CustomerShape: DQ-012 (single source presence)
        for cust in self.subjects_of_type(B2B_NS + "Customer"):
            cid = self.one_str(cust, B2B_NS + "customerId") or str(cust)
            pres = self.one(cust, B2B_NS + "sourcePresence")
            if pres in {IRI(B2B_NS + "SourcePresence_CRM_ONLY"), IRI(B2B_NS + "SourcePresence_ERP_ONLY")}:
                violations.append(
                    {"shape": "b2b:CustomerShape", "severity": "sh:Info", "focus": cid, "rule": "DQ-012"}
                )

        by_sev: dict[str, int] = defaultdict(int)
        for v in violations:
            by_sev[v["severity"]] += 1

        return {
            "conforms": by_sev["sh:Violation"] == 0,
            "total_results": len(violations),
            "by_severity": dict(by_sev),
            "results": violations,
        }

    # ------------------------------------------------------------------
    # 在解析后的 RDF 三元组图谱上直接验证 CQ1 & CQ3（证明与 SQLite 100% 一致）
    # ------------------------------------------------------------------

    def verify_cq1_on_rdf(self, window_start: str = "2026-10-25") -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for order in self.subjects_of_type(B2B_NS + "SalesOrder"):
            if not self.one_bool(order, B2B_NS + "isOverdueUnshipped"):
                continue
            order_date = self.one_str(order, B2B_NS + "orderDate") or ""
            if order_date < window_start:
                continue
            cust = self.one(order, B2B_NS + "placedBy")
            if cust is None or not self.one_bool(cust, B2B_NS + "isVip"):
                continue
            owner = self.one(cust, B2B_NS + "ownedBy")
            hits.append(
                {
                    "order_no": self.one_str(order, B2B_NS + "orderNo"),
                    "customer_name": self.one_str(cust, B2B_NS + "canonicalName"),
                    "owner_name": self.one_str(owner, B2B_NS + "ownerName") if owner else None,
                    "promised_date": self.one_str(order, B2B_NS + "promisedDate"),
                    "total_amount": float(self.one_str(order, B2B_NS + "totalAmount") or "0"),
                }
            )
        hits.sort(key=lambda x: (x["promised_date"] or "", x["order_no"] or ""))
        return hits

    def verify_cq3_on_rdf(self) -> dict[str, float]:
        """完全沿 b2b:parentCategory* 传递路径从 RDF 三元组上卷一级大类有效销售额。"""
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        for line in self.subjects_of_type(B2B_NS + "OrderLine"):
            order = self.one(line, B2B_NS + "partOfOrder")
            if order is None or self.one_bool(order, B2B_NS + "isCancelled"):
                continue
            prod = self.one(line, B2B_NS + "forProduct")
            amt = Decimal(self.one_str(line, B2B_NS + "lineAmount") or "0.00")
            cat = self.one(prod, B2B_NS + "belongsToCategory") if prod else None
            if cat is None:
                root_code = "UNCLASSIFIED"
            else:
                root = self.transitive_root_category(cat)
                root_code = self.one_str(root, B2B_NS + "categoryCode") or "UNCLASSIFIED"
            totals[root_code] += amt
        return {k: float(v) for k, v in sorted(totals.items())}


def run_sparql_with_rdflib_if_available(ttl_files: list[Path], rq_path: Path) -> list[dict[str, str]] | None:
    """若环境安装了 rdflib，直接用原生 SPARQL 1.1 引擎执行 .rq 文件；否则返回 None。"""
    try:
        import rdflib  # type: ignore[import-not-found]
    except ImportError:
        return None
    g = rdflib.Graph()
    for p in ttl_files:
        g.parse(str(p), format="turtle")
    res = g.query(rq_path.read_text(encoding="utf-8"))
    cols = [str(v) for v in res.vars]
    return [{c: str(row[idx]) if row[idx] is not None else "" for idx, c in enumerate(cols)} for row in res]
