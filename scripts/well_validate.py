"""
well_validate.py —— 决策合规门禁

两遍校验：
  1) 对引擎真实产出的 decisions.ttl 校验 → 应为 conforms = True；
  2) 人为注入两条坏决策（给故障井下发开井、稠油井关 14h），
     证明门禁不是摆设，能真拦住。
退出码：真实产出不合格才返回 1。
"""
from __future__ import annotations

import sys
from pathlib import Path

from pyshacl import validate as shacl_validate
from rdflib import Graph, Literal, Namespace, RDF, URIRef

ROOT = Path(__file__).resolve().parents[1]
ONT = ROOT / "ontology"
BUILD = ROOT / "build"
W = Namespace("https://example.org/well#")
WR = Namespace("https://example.org/well/w/")
CRIT = Namespace("https://example.org/well/crit/")
DEC = Namespace("https://example.org/well/dec/")
TR = Namespace("https://example.org/well/trait/")


def short(v) -> str:
    s = str(v)
    return s.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(v, URIRef) else s


def run(data: Graph, shapes: Graph, ont: Graph) -> tuple[bool, list[tuple]]:
    ok, report, _ = shacl_validate(data_graph=data, shacl_graph=shapes,
                                   ont_graph=ont, inference="none", advanced=True)
    q = """PREFIX sh: <http://www.w3.org/ns/shacl#>
           SELECT ?f ?path ?msg WHERE {
             ?r a sh:ValidationResult ; sh:focusNode ?f ; sh:resultMessage ?msg .
             OPTIONAL { ?r sh:resultPath ?path }
           } ORDER BY ?f"""
    return ok, [(f, p, m) for f, p, m in report.query(q)]


def main() -> int:
    ont = Graph().parse(ONT / "well-core.ttl", format="turtle")
    shapes = Graph().parse(ONT / "well-shapes.ttl", format="turtle")
    data = Graph().parse(BUILD / "decisions.ttl", format="turtle")
    data = data + ont

    ok, rows = run(data, shapes, ont)
    print("=" * 74)
    print("[1] 引擎真实产出 build/decisions.ttl → conforms =", ok, f"| 违规 {len(rows)}")
    print("=" * 74)
    for f, p, m in rows:
        print(f"  ✗ {short(f)} [{short(p) if p else '-'}]: {m}")

    # 注入坏决策
    bad_src = Graph().parse(BUILD / "decisions.ttl", format="turtle")
    a = DEC["BAD-faulted-well-start"]
    bad_src.add((a, RDF.type, W.StartDecision))
    bad_src.add((a, W.decidesFor, WR["W12"]))                    # 故障锁定井
    bad_src.add((a, W.justifiedBy, CRIT["Recovered"]))
    bad_src.add((a, W.hasCycleOnHours, Literal(12.0)))
    bad_src.add((a, W.hasCycleOffHours, Literal(4.0)))
    bad_src.add((a, W.hasDutyRatio, Literal(0.75)))

    b = DEC["BAD-heavy-long-off"]
    bad_src.add((b, RDF.type, W.StartDecision))
    bad_src.add((b, W.decidesFor, WR["W07"]))                    # 稠油井
    bad_src.add((b, W.justifiedBy, CRIT["LowEfficiency"]))
    bad_src.add((b, W.hasCycleOnHours, Literal(0.5)))
    bad_src.add((b, W.hasCycleOffHours, Literal(14.0)))          # 超过 8h 上限
    bad_src.add((b, W.hasDutyRatio, Literal(0.03)))
    # 故意不给 basedOn / createdAt / confidence，用来触发可追溯性约束

    # ── 寒区特有场景：冬季冻结风险 + CO2 气窜敏感井，关 10h ──
    jl = WR["W99-Jilin"]
    bad_src.add((jl, RDF.type, W.OilWell))
    bad_src.add((jl, W.hasTrait, TR["WinterFreeze"]))
    bad_src.add((jl, W.hasTrait, TR["CO2Breakthrough"]))
    c = DEC["BAD-jilin-winter-co2"]
    bad_src.add((c, RDF.type, W.StartDecision))
    bad_src.add((c, W.decidesFor, jl))
    bad_src.add((c, W.justifiedBy, CRIT["LowSubmergence"]))
    bad_src.add((c, W.hasCycleOnHours, Literal(2.0)))
    bad_src.add((c, W.hasCycleOffHours, Literal(10.0)))          # 冬季 6h 上限 / CO2 4h 上限
    bad_src.add((c, W.hasDutyRatio, Literal(0.167)))

    ok_bad, rows_bad = run(bad_src + ont, shapes, ont)
    print()
    print("=" * 74)
    print("[2] 注入 3 条坏决策 → conforms =", ok_bad, f"| 拦下 {len(rows_bad)} 条")
    print("=" * 74)
    for f, p, m in rows_bad:
        print(f"  ✗ {short(f)} [{short(p) if p else '-'}]: {m}")

    if not ok_bad:
        print("\n门禁有效：故障井开井、稠油长关、寒区冬季长关、CO2 气窜井长关、无证据决策全部被拦。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
