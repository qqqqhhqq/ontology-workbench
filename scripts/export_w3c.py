#!/usr/bin/env python3
"""导出并校验 W3C 标准文件（OWL 2 / RDFS Turtle、SHACL 1.0 Shapes、RDF ABox 与 SPARQL 对账）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ontology.w3c.exporter import export_all_w3c
from ontology.w3c.rdf_store import RDFTripleStore


def main() -> None:
    stats = export_all_w3c()
    tbox_path = Path(stats["tbox_path"])
    shacl_path = Path(stats["shacl_path"])
    abox_path = Path(stats["abox_path"])

    # 使用 Turtle 1.1 解析器逐文件严格解析校验
    tbox_store = RDFTripleStore()
    tbox_triples = tbox_store.parse_file(tbox_path)

    shacl_store = RDFTripleStore()
    shacl_triples = shacl_store.parse_file(shacl_path)

    full_store = RDFTripleStore()
    full_store.parse_file(tbox_path)
    abox_triples = full_store.parse_file(abox_path)

    shacl_report = full_store.validate_shacl_rules()
    cq1_rdf = full_store.verify_cq1_on_rdf()
    cq3_rdf = full_store.verify_cq3_on_rdf()

    print("=== W3C 语义网标准导出与校验完成 ===")
    print(f"  - OWL/RDFS TBox : {tbox_path.relative_to(ROOT_DIR)} ({tbox_triples} triples)")
    print(f"  - SHACL Shapes  : {shacl_path.relative_to(ROOT_DIR)} ({shacl_triples} triples)")
    print(f"  - RDF ABox      : {abox_path.relative_to(ROOT_DIR)} ({abox_triples} triples)")
    print(
        f"  - SHACL 校验结果: conforms={shacl_report['conforms']}, "
        f"total={shacl_report['total_results']}, by_severity={shacl_report['by_severity']}"
    )
    print(f"  - RDF 图谱验证 CQ1 ({len(cq1_rdf)} 张): {[r['order_no'] for r in cq1_rdf]}")
    print(f"  - RDF 传递闭包验证 CQ3: {cq3_rdf}")


if __name__ == "__main__":
    main()
