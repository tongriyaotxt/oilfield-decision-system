"""
build.py —— 加载 TBox+ABox，做 OWL 2 RL 物化，观察"推理出来的东西"。

为什么用 OWL 2 RL (owlrl) 而不是 DL 推理器 (HermiT/ELK)？
  HermiT/ELK 需要 JVM，且给出的是"分类结果"而非可查询的三元组；
  RL 物化把蕴含写成显式三元组，便于落库（GraphDB/Neptune/Fuseki 都是这条路线）。
  代价：RL 是 OWL 2 的表达力子集 —— 例如 minCardinality 不能用来做分类（见文档）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from owlrl import DeductiveClosure, OWLRL_Semantics
from rdflib import Graph, Namespace, RDF, OWL, URIRef

ROOT = Path(__file__).resolve().parents[1]
ONT = ROOT / "ontology"
BUILD = ROOT / "build"
IND = Namespace("https://example.org/ind#")
EX = Namespace("https://example.org/")

META = {RDF.type, OWL.sameAs, OWL.differentFrom}


def load(*names: str) -> Graph:
    g = Graph()
    for n in names:
        g.parse(ONT / n, format="turtle")
    return g


def typed(g: Graph) -> set[tuple[str, str]]:
    """(个体, 类) 对，只保留本体的命名类。"""
    out = set()
    for s, o in g.subject_objects(RDF.type):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and str(o).startswith(str(IND)):
            out.add((short(s), short(o)))
    return out


def short(u: URIRef) -> str:
    s = str(u)
    for base in ("https://example.org/ind/", "https://example.org/ind#"):
        if s.startswith(base):
            return s[len(base):]
    return s


def expand(g: Graph) -> Graph:
    DeductiveClosure(OWLRL_Semantics).expand(g)
    return g


def inconsistent(g: Graph) -> list[str]:
    """RL 里不一致的显式信号：某个体被推成 owl:Nothing。"""
    return sorted({short(s) for s in g.subjects(RDF.type, OWL.Nothing) if isinstance(s, URIRef)})


def run_queries(g: Graph) -> None:
    for q in sorted((ROOT / "queries").glob("*.rq")):
        rows = list(g.query(q.read_text(encoding="utf-8")))
        print(f"\n--- {q.name} ({len(rows)} rows) ---")
        for r in rows:
            print("   " + " | ".join(short(v) if isinstance(v, URIRef) else str(v) for v in r))


DEFINED = {"Assembly", "System", "Actuator", "SafetyEquipment",
           "SafetyRelevantPart", "DocumentedPart"}


def main() -> int:
    raw = load("ind-core.ttl", "ind-data.ttl")
    before_types = typed(raw)
    before_n = len(raw)
    before_haspart = len(set(raw.subject_objects(IND.hasPart)))
    before_partof = len(set(raw.subject_objects(IND.partOf)))

    g = expand(raw)          # 注意：owlrl 原地扩写，所以上面的计数必须先取

    new_types = typed(g) - before_types
    print("=" * 72)
    print("断言图 (TBox+ABox)      ", before_n, "三元组")
    print("OWL 2 RL 物化后         ", len(g), "三元组", f"(+{len(g) - before_n})")
    print("新增类型断言             ", len(new_types), "条")
    print("hasPart 边（断言 → 推理后）", before_haspart, "→",
          len(set(g.subject_objects(IND.hasPart))))
    print("partOf 边（断言 → 推理后）", before_partof, "→",
          len(set(g.subject_objects(IND.partOf))))
    print("locatedIn 边（属性链）   ", len(set(g.subject_objects(IND.locatedIn))))
    print("=" * 72)

    print("\n数据文件里从未写过、纯靠推理得到的【定义类】分类：")
    for cls in ("Assembly", "System", "Actuator", "SafetyEquipment",
                "SafetyRelevantPart", "DocumentedPart"):
        hits = sorted(s for s, c in new_types if c == cls)
        print(f"\n  {cls}  ({len(hits)})")
        for h in hits:
            print(f"     - {h}")

    run_queries(g)

    BUILD.mkdir(exist_ok=True)
    out = BUILD / "inferred.ttl"
    g.serialize(out, format="turtle")
    print(f"\n物化结果已写出: {out}")

    # ---- 一致性检查演示：RL 物化本身不判不一致，此处只为展示该事实 ----
    bad = Graph()
    bad.parse(ONT / "ind-core.ttl", format="turtle")
    bad.parse(ONT / "ind-inconsistent.ttl", format="turtle")
    expand(bad)
    hits = inconsistent(bad)
    print("\n" + "=" * 72)
    print("一致性检查 ind-inconsistent.ttl ->",
          f"owl:Nothing = {hits}" if hits else
          "owl:Nothing = 空（OWL 2 RL 只做正向蕴含，不判互斥冲突；见 scripts/consistency.py）")
    print("=" * 72)
    return 0



if __name__ == "__main__":
    sys.exit(main())
