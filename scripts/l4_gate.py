"""
l4_gate.py —— L4 无人化自治准入闸门（含故障注入仿真）

判定一条铁律：
    申报 L4，必须在**异常工况下也无人**。
    所以要看四维：能力齐 / 自愈策略覆盖齐 / 实测证据达标 / 授权与兜底到位。

故障注入仿真做什么：
    对 8 类关键异常逐一注入，检查"检测时延 + 动作时延 ≤ 允许回到安全态的时间"，
    并且所需能力与自愈策略都已就位。任一项不通过 → 该类异常在 L4 下仍会把人叫到现场。

输出：build/l4-report.ttl + 控制台记分卡。退出码：有站申报 L4 却不达标 → 1。
"""
from __future__ import annotations

import math
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
AN = Namespace("https://example.org/um/anomaly/")
ACT = Namespace("https://example.org/um/action/")
LVL = Namespace("https://example.org/um/lvl/")

# 动作执行时延（min），工程参数，落地时用实际调试数据替换
ACTION_MIN = {"A1": 1.0, "A2": 12.0, "A3": 6.0, "A4": 0.8,
              "A5": 0.8, "A6": 25.0, "A7": 3.0, "A8": 45.0}

THRESHOLDS = dict(days=30, self_heal=0.80, false_action=0.01, emergency_min=30)


def short(v) -> str:
    s = str(v)
    return s.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(v, URIRef) else str(v)


def load() -> Graph:
    g = Graph()
    for f in ("unmanned-core.ttl", "l4-core.ttl"):
        g.parse(ONT / f, format="turtle")
    return g


def stations(g: Graph) -> list[URIRef]:
    q = """
        PREFIX um: <https://example.org/um#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?s WHERE { ?s a ?t . ?t rdfs:subClassOf* um:Station }
    """
    return sorted((r[0] for r in g.query(q)), key=str)


def inject(g: Graph, st: URIRef) -> list[dict]:
    """故障注入：逐异常检验自愈可行性。"""
    have = set(g.objects(st, UM.hasCapability))
    covered = set(g.objects(st, UM.coversAnomaly))
    results = []
    for a in g.subjects(RDF.type, UM.Anomaly):
        name = short(a)
        if not bool(g.value(a, UM.criticalAnomaly)):
            continue
        need = set(g.objects(a, UM.requiresCapabilityForSelfHeal))
        miss_caps = sorted(need - have, key=str)
        has_strategy = a in covered
        detect = float(g.value(a, UM.detectMinutes) or 1)
        limit = float(g.value(a, UM.mustReachSafeStateIn) or 60)
        t = detect * 1.2 + ACTION_MIN.get(name, 10.0)
        ok = (not miss_caps) and has_strategy and (t <= limit)
        reason = ("能力缺失:" + ",".join(short(c) for c in miss_caps) if miss_caps
                  else "无自愈策略（未配置动作库）" if not has_strategy
                  else "动作链路超时" if t > limit else "")
        results.append(dict(anomaly=name, ok=ok, minutes=round(t, 1), limit=limit,
                            need=[short(c) for c in need],
                            reason=reason, actions=[short(x) for x in g.objects(a, UM.requiresAction)]))
    return sorted(results, key=lambda r: r["anomaly"])


def gate(g: Graph, st: URIRef) -> dict:
    declared = g.value(st, UM.atLevel)
    declared_idx = int(g.value(declared, UM.levelIndex) or 0) if declared else 0
    have = set(g.objects(st, UM.hasCapability))
    need = set(g.objects(LVL.L4, UM.requiresCapability))
    miss_caps = sorted(need - have, key=str)

    sim = inject(g, st)
    passed = [r for r in sim if r["ok"]]
    failed = [r for r in sim if not r["ok"]]

    raw_days = g.value(st, UM.autonomyDays)
    evaluated = raw_days is not None
    days = int(raw_days or 0)
    rate = float(g.value(st, UM.selfHealRate) or 0)
    fa = float(g.value(st, UM.falseActionRate) or 0)
    dp = int(g.value(st, UM.drillsPassed) or 0)
    dt = int(g.value(st, UM.drillsTotal) or 0)
    rev = bool(g.value(st, UM.procedureRevised))
    center = bool(g.value(st, UM.centerConnected))
    emin = int(g.value(st, UM.emergencyMinutes) or 0)

    unmet = []
    if evaluated:
        if days < THRESHOLDS["days"]:
            unmet.append(f"无干预{days}d<{THRESHOLDS['days']}d")
        if rate < THRESHOLDS["self_heal"]:
            unmet.append(f"自愈率{rate:.2f}<{THRESHOLDS['self_heal']}")
        if fa > THRESHOLDS["false_action"]:
            unmet.append(f"误动作{fa:.3f}>{THRESHOLDS['false_action']}")
        if dt and dp < dt:
            unmet.append(f"演练{dp}/{dt}<{dt}/{dt}")
    if not rev:
        unmet.append("规程未修订")
    if not center:
        unmet.append("未接入监控中心")
    if emin and emin > THRESHOLDS["emergency_min"]:
        unmet.append(f"应急{emin}min>{THRESHOLDS['emergency_min']}min")

    ok = evaluated and (not miss_caps) and (not failed) and (not unmet)

    capex = sum(float(g.value(c, UM.capexWan) or 0) for c in miss_caps)
    build_months = 2.5 * len(miss_caps) + 1.5 * len(failed)
    proof_months = (max(0, THRESHOLDS["days"] - days) / 30
                    + 0.5 * max(0, dt - dp)
                    + (2 if not rev else 0) + (1 if not center else 0))
    months = math.ceil(max(build_months, proof_months)) if not ok else 0

    return dict(station=st, declared=declared, declared_idx=declared_idx, ok=ok,
                evaluated=evaluated, sim=sim, passed=len(passed), total=len(sim),
                failed=failed, miss_caps=miss_caps, unmet=unmet, capex=capex,
                months=months, days=days, rate=rate, fa=fa, drills=f"{dp}/{dt}",
                rev=rev, center=center, emin=emin)


def main() -> int:
    g = load()
    rows = [gate(g, st) for st in stations(g)]

    print(f"{'站':5s} {'申报':4s} {'自愈覆盖':>8s} {'无干预':>6s} {'自愈率':>6s} "
          f"{'误动':>6s} {'演练':>5s} {'应急':>5s} {'结论':>6s} {'缺口(万)':>8s} {'工期(月)':>7s}")
    print("-" * 96)
    for r in rows:
        verdict = "✅ PASS" if r["ok"] else ("— 未评估" if not r["evaluated"] else "❌ FAIL")
        drill = r["drills"] if r["evaluated"] else "-"
        print(f"{short(r['station']):5s} {short(r['declared']):4s} "
              f"{r['passed']}/{r['total']:<6} "
              f"{(str(r['days']) + 'd') if r['evaluated'] else '-':>6s} "
              f"{r['rate']:6.2f} {r['fa']:6.3f} {drill:>5s} "
              f"{(str(r['emin']) + 'm') if r['emin'] else '-':>5s} "
              f"{verdict:>8s} {r['capex']:8.1f} "
              f"{(str(r['months']) if r['evaluated'] else '待评估'):>7s}")
    print("-" * 96)

    # 明细：挑第一个不通过的站，展示"哪类异常会把人数叫回现场"
    bad = next((r for r in rows if not r["ok"]), None)
    if bad:
        print(f"\n【明细】{short(bad['station'])} 的自愈短板（这就是 L4 过不了的真实原因）：")
        for item in bad["failed"]:
            print(f"  ✗ {item['anomaly']}  需 {item['minutes']}min / 限 {item['limit']}min"
                  f"  动作链 {'→'.join(item['actions'])}  原因 {item['reason']}")
    good = next((r for r in rows if r["ok"]), None)
    print(f"\n【明细】{short(good['station']) if good else '-'} 通过项"
          f"（示范：自愈链路完整的站长什么样）：")
    if good:
        for item in good["sim"]:
            print(f"  ✓ {item['anomaly']}  {item['minutes']}min / 限 {item['limit']}min"
                  f"  {'→'.join(item['actions'])}")

    # 写报告
    out = Graph()
    out.bind("um", UM)
    for r in rows:
        node = UM[f"l4report/{short(r['station'])}"]
        out.add((node, RDF.type, UM.L4Report))
        out.add((node, UM.auditsStation, r["station"]))
        out.add((node, UM.l4Passed, Literal(r["ok"], datatype=XSD.boolean)))
        out.add((node, UM.selfHealCoverage, Literal(f"{r['passed']}/{r['total']}")))
        out.add((node, UM.gapCapexWan, Literal(round(r["capex"], 1), datatype=XSD.decimal)))
        out.add((node, UM.estimatedMonths, Literal(r["months"], datatype=XSD.integer)))
        for c in r["miss_caps"]:
            out.add((node, UM.gapCapability, c))
        for item in r["failed"]:
            out.add((node, UM.uncoveredAnomaly, AN[item["anomaly"]]))
    BUILD.mkdir(exist_ok=True)
    out.serialize(BUILD / "l4-report.ttl", format="turtle")
    print(f"\nL4 评估报告已写出: {BUILD / 'l4-report.ttl'}")

    # SHACL 复核（L4 申报站点的证据与授权）
    shapes = Graph().parse(ONT / "l4-shapes.ttl", format="turtle")
    core = Graph()
    for f in ("unmanned-core.ttl", "l4-core.ttl"):
        core.parse(ONT / f, format="turtle")
    ok, report, _ = shacl_validate(data_graph=core, shacl_graph=shapes,
                                   ont_graph=core, inference="none", advanced=True)
    q = """PREFIX sh: <http://www.w3.org/ns/shacl#>
           SELECT ?f ?m WHERE { ?r a sh:ValidationResult ; sh:focusNode ?f ; sh:resultMessage ?m }"""
    finds = list(report.query(q))
    print(f"\nL4 门禁 conforms = {ok} | 结论 {len(finds)} 条")
    for f, m in finds:
        print(f"  ✗ {short(f)}: {m}")

    # 台账里没有人能"凭空"达到 L4：申报 L4 的站必须全部通过
    declared_l4 = [r for r in rows if r["declared_idx"] >= 4]
    bad_l4 = [r for r in declared_l4 if not r["ok"]]
    if declared_l4:
        print(f"\n申报 L4 的站 {len(declared_l4)} 个，其中不达标 {len(bad_l4)} 个"
              f"（{'、'.join(short(r['station']) for r in bad_l4) or '无'}）")
    return 0 if not bad_l4 else 1


if __name__ == "__main__":
    sys.exit(main())
