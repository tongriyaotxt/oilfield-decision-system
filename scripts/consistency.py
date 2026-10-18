"""
consistency.py —— 一致性检查的三层现实

第 1 层：OWL 2 RL 物化（owlrl）。实测：**不会**把互斥类冲突报出来。
        OWL 2 RL/RDF 规则表里 cax-dw 是"假"条件，owlrl 只做正向蕴含、不当裁判。
第 2 层：自己写 SPARQL 规则查互斥 / 函数属性冲突 —— CI 里最便宜可靠的做法。
第 3 层：SHACL（sh:not + sh:class）把互斥写成可执行约束，顺便出人话报告。

要真正的 DL 一致性判定（含基数、存在性、不可满足类），得挂 HermiT/ELK/Openllet（需 JVM）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from owlrl import DeductiveClosure, OWLRL_Semantics
from pyshacl import validate as shacl_validate
from rdflib import Graph, Namespace, OWL, RDF, URIRef

ROOT = Path(__file__).resolve().parents[1]
ONT = ROOT / "ontology"
IND = Namespace("https://example.org/ind#")

DISJOINT_RULE = """
PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?x ?c1 ?c2 WHERE {
  ?c1 owl:disjointWith ?c2 .
  ?x rdf:type/rdfs:subClassOf* ?c1 .
  ?x rdf:type/rdfs:subClassOf* ?c2 .
  FILTER (isIRI(?x))
}
"""

FUNCTIONAL_RULE = """
PREFIX owl: <http://www.w3.org/2002/07/owl#>
SELECT DISTINCT ?x ?p ?v1 ?v2 WHERE {
  ?p a owl:FunctionalProperty .
  ?x ?p ?v1 , ?v2 .
  FILTER (?v1 != ?v2)
}
"""


def short(v) -> str:
    s = str(v)
    return s.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(v, URIRef) else s


def main() -> int:
    g = Graph()
    g.parse(ONT / "ind-core.ttl", format="turtle")
    g.parse(ONT / "ind-inconsistent.ttl", format="turtle")

    DeductiveClosure(OWLRL_Semantics).expand(g)
    nothing = [s for s in g.subjects(RDF.type, OWL.Nothing) if isinstance(s, URIRef)]

    print("=" * 72)
    print("[1] OWL 2 RL 物化后的 owl:Nothing 断言:", nothing or "无（RL 不负责判不一致）")
    print("=" * 72)

    problems = 0
    print("[2] SPARQL 互斥类规则：")
    for x, c1, c2 in g.query(DISJOINT_RULE):
        problems += 1
        print(f"    ✗ {short(x)} 同时是 {short(c1)} 和 {short(c2)}（{short(c1)} ⊓ {short(c2)} = ⊥）")

    print("[2] SPARQL 函数属性冲突规则：")
    rows = list(g.query(FUNCTIONAL_RULE))
    for x, p, v1, v2 in rows:
        problems += 1
        print(f"    ✗ {short(x)} 的 {short(p)} 有两个不同取值: {v1} / {v2}")
    if not rows:
        print("    ✓ 无")

    print("=" * 72)
    shapes = Graph().parse(ONT / "ind-shapes.ttl", format="turtle")
    conforms, report, _ = shacl_validate(
        data_graph=g, shacl_graph=shapes, ont_graph=Graph().parse(ONT / "ind-core.ttl"),
        inference="none", advanced=True,
    )
    print("[3] SHACL 复核 conforms =", conforms)
    q = """
    PREFIX sh: <http://www.w3.org/ns/shacl#>
    SELECT ?focus ?msg WHERE {
      ?r a sh:ValidationResult ; sh:focusNode ?focus ; sh:resultMessage ?msg
    }"""
    for focus, msg in report.query(q):
        print(f"    ✗ {short(focus)}: {msg}")
    print("=" * 72)
    return 0 if (problems == 0 and conforms) else 1


if __name__ == "__main__":
    sys.exit(main())
