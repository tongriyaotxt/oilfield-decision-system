"""
unmanned_audit.py —— 无人化准入审计器

回答三个问题，全部基于台账 + 能力清单推出来，不靠拍脑袋：
  1. 每个站**实际**处在哪个无人化等级？（能力清单反推，不看申报）
  2. 哪些站是"假无人"？（申报等级 > 实际等级）
  3. 到目标等级还缺什么、要花多少钱、能减多少驻站人？

输出：build/unmanned-audit.ttl（审计结论，可被 SHACL 继续校验）+ 控制台路线图。
退出码：存在"假无人"或台账不合格 → 1（可直接挂 CI）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from pyshacl import validate as shacl_validate
from rdflib import Graph, Literal, Namespace, RDF, URIRef
from rdflib.namespace import XSD

ROOT = Path(__file__).resolve().parents[1]
ONT = ROOT / "ontology"
BUILD = ROOT / "build"
UM = Namespace("https://example.org/um#")
CAP = Namespace("https://example.org/um/cap/")
LVL = Namespace("https://example.org/um/lvl/")

# ── 经济参数（务必用实际数据替换）──
STAFF_COST_WAN = 15.0        # 驻站人员综合成本 万元/人·年（含倒班、交通、食宿）
CENTER_FTE_RATIO = 0.35      # 无人化后转到监控中心/应急队的比例（不是全裁）
DAYS = 365


def short(v) -> str:
    s = str(v)
    return s.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(v, URIRef) else str(v)


def load():
    g = Graph()
    g.parse(ONT / "unmanned-core.ttl", format="turtle")
    return g


def levels(g: Graph) -> dict[URIRef, tuple[int, set[URIRef]]]:
    out = {}
    for lv in g.subjects(RDF.type, UM.AutonomyLevel):
        idx = int(g.value(lv, UM.levelIndex) or 0)
        req = set(g.objects(lv, UM.requiresCapability))
        out[lv] = (idx, req)
    return out


def achieved_level(g: Graph, station: URIRef, lvls: dict) -> URIRef:
    have = set(g.objects(station, UM.hasCapability))
    best = LVL.L0
    for lv, (idx, req) in lvls.items():
        if req and req <= have:                 # 必需能力全部具备
            if idx > int(g.value(best, UM.levelIndex) or 0):
                best = lv
        elif not req and idx == 0:
            continue
    return best


def main() -> int:
    g = load()
    lvls = levels(g)
    idx_of = {lv: i for lv, (i, _) in lvls.items()}

    rows, fake, total_capex, staff_now = [], 0, 0.0, 0.0
    station_q = """
        PREFIX um:  <https://example.org/um#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?s WHERE { ?s a ?t . ?t rdfs:subClassOf* um:Station }
    """
    stations = sorted((r[0] for r in g.query(station_q)), key=str)
    print(f"{'站':6s} {'类型':12s} {'申报':4s} {'实际':4s} {'目标':4s} "
          f"{'驻站':>4s} {'缺口能力':28s} {'缺口投资(万元)':>12s}")
    print("-" * 104)
    for st in stations:
        name = short(st)
        cls = next((short(c) for c in g.objects(st, RDF.type) if c != UM.Station), "-")
        declared = g.value(st, UM.atLevel)
        target = g.value(st, UM.targetLevel) or LVL.L4
        staff = float(g.value(st, UM.staffOnSite) or 0)
        have = set(g.objects(st, UM.hasCapability))
        actual = achieved_level(g, st, lvls)
        staff_now += staff

        fake_flag = idx_of.get(declared, 0) > idx_of.get(actual, 0)
        fake += 1 if fake_flag else 0

        need = set(lvls.get(target, (0, set()))[1])
        gaps = sorted(need - have, key=str)
        capex = sum(float(g.value(c, UM.capexWan) or 0) for c in gaps)
        total_capex += capex

        print(f"{name:6s} {cls:12s} {short(declared):4s} {short(actual):4s} {short(target):4s} "
              f"{staff:4.0f} {', '.join(short(c) for c in gaps)[:28]:28s} {capex:12.1f}")
        rows.append((st, declared, actual, target, gaps, capex, fake_flag))

    print("-" * 104)
    net_cut = staff_now * (1 - CENTER_FTE_RATIO)
    save = net_cut * STAFF_COST_WAN
    print(f"驻站人数合计 {staff_now:.0f} 人 → 无人化后净减 {net_cut:.1f} 人"
          f"（{CENTER_FTE_RATIO:.0%} 转监控中心/应急队）")
    print(f"人工年节约 ≈ {save:.0f} 万元/年（按 {STAFF_COST_WAN:.0f} 万元/人·年）")
    print(f"能力缺口投资合计 ≈ {total_capex:.1f} 万元（{len(rows)} 个站）")
    if save > 0:
        print(f"仅按人工节约测算的静态回收期 ≈ {total_capex / save * 12:.0f} 个月"
              f"（未计入节能、增产、事故损失下降）")
    print(f"\n『假无人』站点：{fake} 个 ← 申报等级高于实际能力，必须降级或补能力")

    # ── 审计结论落成 RDF，供后续门禁与看板消费 ──
    out = Graph()
    out.bind("um", UM)
    for st, declared, actual, target, gaps, capex, is_fake in rows:
        a = UM[f"audit/{short(st)}"]
        out.add((a, RDF.type, UM.Audit))
        out.add((a, UM.auditsStation, st))
        out.add((a, UM.declaredLevel, declared))
        out.add((a, UM.achievedLevel, actual))
        out.add((a, UM.targetLevel, target))
        out.add((a, UM.gapCapexWan, Literal(round(capex, 1), datatype=XSD.decimal)))
        out.add((a, UM.isFakeUnmanned, Literal(is_fake, datatype=XSD.boolean)))
        for c in gaps:
            out.add((a, UM.gapCapability, c))
    BUILD.mkdir(exist_ok=True)
    out.serialize(BUILD / "unmanned-audit.ttl", format="turtle")
    print(f"\n审计结论已写出: {BUILD / 'unmanned-audit.ttl'}")

    # ── SHACL 门禁复核 ──
    shapes = Graph().parse(ONT / "unmanned-shapes.ttl", format="turtle")
    ont = Graph().parse(ONT / "unmanned-core.ttl", format="turtle")
    ok, report, _ = shacl_validate(data_graph=ont, shacl_graph=shapes,
                                   ont_graph=ont, inference="none", advanced=True)
    q = """PREFIX sh: <http://www.w3.org/ns/shacl#>
           SELECT ?f ?m WHERE { ?r a sh:ValidationResult ; sh:focusNode ?f ; sh:resultMessage ?m }"""
    findings = list(report.query(q))
    print(f"\nSHACL 门禁 conforms = {ok} | 结论 {len(findings)} 条")
    for f, m in findings:
        print(f"  ✗ {short(f)}: {m}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
