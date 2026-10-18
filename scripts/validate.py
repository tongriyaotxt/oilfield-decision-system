"""
validate.py —— SHACL 闭世界校验，跑在"物化后的图"上。

流水线顺序很重要：OWL 物化 → SHACL 校验。
SHACL 用 rdf:type 选目标节点；只有在物化图上跑，SafetyRelevantPart、
Assembly 这类"推理出来的类型"才能命中目标，约束才覆盖到该覆盖的实例。
"""
from __future__ import annotations

import sys
from pathlib import Path

from owlrl import DeductiveClosure, OWLRL_Semantics
from pyshacl import validate as shacl_validate
from rdflib import Graph, URIRef

ROOT = Path(__file__).resolve().parents[1]
ONT = ROOT / "ontology"


def short(v) -> str:
    s = str(v)
    return s.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(v, URIRef) else s


def main() -> int:
    data = Graph()
    for n in ("ind-core.ttl", "ind-data.ttl"):
        data.parse(ONT / n, format="turtle")
    DeductiveClosure(OWLRL_Semantics).expand(data)     # 先物化

    shapes = Graph().parse(ONT / "ind-shapes.ttl", format="turtle")
    ont = Graph().parse(ONT / "ind-core.ttl", format="turtle")

    conforms, report_graph, _ = shacl_validate(
        data_graph=data,
        shacl_graph=shapes,
        ont_graph=ont,
        inference="none",      # 已在上面做过蕴含，无需重复
        advanced=True,         # 允许 sh:sparql 约束
    )

    q = """
    PREFIX sh: <http://www.w3.org/ns/shacl#>
    SELECT ?focus ?path ?msg WHERE {
      ?r a sh:ValidationResult ;
         sh:focusNode ?focus ;
         sh:resultMessage ?msg .
      OPTIONAL { ?r sh:resultPath ?path }
    } ORDER BY ?focus
    """
    rows = list(report_graph.query(q))
    print("=" * 72)
    print("SHACL conforms:", conforms, f"| 违规 {len(rows)} 条")
    print("=" * 72)
    for focus, path, msg in rows:
        p = f" [{short(path)}]" if path else ""
        print(f"  ✗ {short(focus)}{p}: {msg}")
    return 0 if conforms else 1


if __name__ == "__main__":
    sys.exit(main())
