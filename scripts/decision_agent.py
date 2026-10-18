"""
decision_agent.py —— 开/关井决策 Agent（建在分析语义层之上）

与"分析问答"共用同一套口径，区别只在于最后一步：
  分析：指标 → 结论；
  决策：指标 → 命中规则 → 受约束裁剪 → 可执行周期 + 预期效果 + 证据落库。

决策链（每一步都可复算）：
  1) 取窗口内每井特征：时率、泵效、吨液电耗、日产油、累计产液
  2) 按规则优先级逐条判定（规则读自 decision-core.ttl，改阈值不改代码）
  3) 命中"缩短时率"时：目标时率 = 实际日产液 / 理论排量（产量不变，砍掉空抽）
  4) 用约束裁剪成可执行周期：最小关井 4h、稠油 ≤8h、冬季 ≤6h、单日启停 ≤3
  5) 算预期：产液保持率、日节电
  6) 写 RDF（规则+证据+周期），过 SHACL 门禁；不合格就整批打回
单向安全：只有"缩短时率/关井/维持/升级人工"允许自动；"提高时率/复产"必须人工审批。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from pyshacl import validate as shacl_validate
from rdflib import Graph, Literal, Namespace, RDF, URIRef
from rdflib.namespace import RDFS, XSD

ROOT = Path(__file__).resolve().parents[1]
ONT = ROOT / "ontology"
DB = ROOT / "build" / "analytics.db"
AO = Namespace("https://example.org/ao#")
ACT = Namespace("https://example.org/ao/act/")
RULE = Namespace("https://example.org/ao/rule/")
MET = Namespace("https://example.org/ao/metric/")

WINDOW_DAYS = 7
KW = 22.0 * 0.8          # 抽油机平均有功（kW）
MIN_OFF = 4.0
MAX_STARTS = 3
HEAVY_OFF = 8.0
WINTER_OFF = 6.0

METRIC_KEY = {"liquid_m3": "liquid_sum", "oil_rate": "oil_pd",
              "pump_eff": "eff", "supply_ratio": "supply_ratio"}
OP = {"lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
      "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b}


def short(v) -> str:
    s = str(v)
    return s.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(v, URIRef) else str(v)


# ─────────────────────────── 本体：规则与约束 ───────────────────────────
def load_rules() -> tuple[list[dict], dict]:
    g = Graph()
    g.parse(ONT / "analytics-core.ttl", format="turtle")
    g.parse(ONT / "decision-core.ttl", format="turtle")
    rules = []
    for r in g.subjects(RDF.type, AO.DecisionRule):
        if str(g.value(r, AO.enabled)).lower() != "true":
            continue
        rules.append(dict(
            iri=short(r),
            id=str(g.value(r, AO.ruleId)),
            prio=int(g.value(r, AO.priority)),
            metric=short(g.value(r, AO.onMetric)),
            op=str(g.value(r, AO.operator)),
            thr=float(g.value(r, AO.threshold)),
            action=short(g.value(r, AO.takesAction)),
            rationale=str(g.value(r, AO.rationale) or ""),
            label=str(g.value(r, RDFS.label) or ""),
        ))
    cons = {}
    for c in g.subjects(RDF.type, AO.Constraint):
        cons[short(c)] = float(g.value(c, AO.constraintValue))
    rules.sort(key=lambda r: r["prio"])
    return rules, cons


# ─────────────────────────── 特征 ───────────────────────────
FEAT_SQL = """
SELECT d.well,
       (SELECT status FROM well_daily s WHERE s.well = d.well
        ORDER BY day DESC LIMIT 1) AS status,
       MAX(d.block) AS block, MAX(m.trait) AS trait,
       SUM(d.runtime_h)/SUM(24.0)                       AS duty,
       SUM(d.liquid_m3)/NULLIF(SUM(d.q_theory*d.runtime_h/24.0),0) AS eff,
       SUM(d.kwh)/NULLIF(SUM(d.liquid_m3),0)            AS unit_power,
       SUM(d.liquid_m3)/COUNT(DISTINCT d.day)           AS liquid_pd,
       SUM(d.oil_t)/COUNT(DISTINCT d.day)               AS oil_pd,
       SUM(d.liquid_m3)                                 AS liquid_sum,
       SUM(d.kwh)                                       AS kwh_sum,
       MAX(d.q_theory)                                  AS q_theory,
       COUNT(DISTINCT d.day)                            AS days
FROM well_daily d JOIN well_meta m ON m.well = d.well
WHERE d.day BETWEEN ? AND ?
GROUP BY d.well
ORDER BY d.well
"""


def features(con: sqlite3.Connection, start: str, end: str) -> list[dict]:
    cols = ("well", "status", "block", "trait", "duty", "eff", "unit_power",
            "liquid_pd", "oil_pd", "liquid_sum", "kwh_sum", "q_theory", "days")
    out = []
    for row in con.execute(FEAT_SQL, (start, end)):
        out.append({k: (row[i] if row[i] is not None else 0.0) for i, k in enumerate(cols)})
    return out


# ─────────────────────────── 决策 ───────────────────────────
def cycle(duty: float, trait: str, month: int) -> tuple[float, float, float, list[str]]:
    """把目标时率压成可执行周期，并返回命中的约束说明。"""
    notes = []
    duty = min(0.98, max(0.02, duty))
    if duty >= 0.70:
        # 时率很高时再切"开/停"周期没有意义（会排出 30h 开、4h 停这种荒谬制度），
        # 正确做法是连续生产；此时若需节能，应走降冲次/换小泵。
        return 24.0, 0.0, 1.0, ["目标时率≥70% → 按连续生产执行（节能靠降冲次，不靠启停）"]
    on, off = 12.0 * duty, 12.0 * (1 - duty)
    if off < MIN_OFF:
        off, on = MIN_OFF, MIN_OFF * duty / (1 - duty)
        notes.append(f"关井不足 {MIN_OFF:g}h → 拉长周期保时率")
    cap = None
    if trait == "稠油":
        cap = HEAVY_OFF
        notes.append(f"稠油单次关井 ≤{HEAVY_OFF:g}h")
    if month in (11, 12, 1, 2, 3):
        cap = min(cap or 99, WINTER_OFF)
        notes.append(f"寒区冬季关井 ≤{WINTER_OFF:g}h")
    if cap and off > cap:
        off, on = cap, cap * duty / (1 - duty)
        notes.append(f"按上限压缩关井至 {cap:g}h")
    # 启停次数约束：24h / 周期 ≤ 3 次 → 周期 ≥ 8h（否则一天启停五六次，电机先坏）
    if 24.0 / (on + off) > MAX_STARTS:
        period = 24.0 / MAX_STARTS
        on, off = period * duty, period * (1 - duty)
        if off < MIN_OFF:                     # 时率偏高时仍保住最小关井
            off = MIN_OFF
            on = MIN_OFF * duty / (1 - duty)
        notes.append(f"启停≤{MAX_STARTS:g}次/日 → 周期拉长到 {on + off:.1f}h")
    on = max(0.4, on)
    return round(on, 2), round(off, 2), round(on / (on + off), 3), notes


def decide(rec: dict, rules: list[dict], month: int) -> dict:
    # 供液比 = 供液能力/泵能力，**与时率无关**（泵效 = 供液比 / 时率，拿泵效当判据会自激振荡）
    rec["supply_ratio"] = (rec.get("liquid_pd", 0.0) / rec["q_theory"]) if rec.get("q_theory") else 0.0
    fired = None
    for r in rules:
        v = rec.get(METRIC_KEY.get(r["metric"], ""), 0.0) or 0.0
        if OP[r["op"]](v, r["thr"]):
            fired = r
            break
    if fired is None:                       # 无规则命中 → 保守维持
        fired = dict(iri="default", id="R0", label="无规则命中，保守维持",
                     action="Hold", rationale="各项指标落在规则空档内。")

    qth = rec["q_theory"] or 1.0
    duty_now = rec["duty"]
    liq = rec["liquid_pd"]
    act = fired["action"]
    d: dict = dict(action=act, rule=fired, notes=[], duty_target=duty_now,
                   on=0.0, off=0.0, keep=1.0, saved=0.0, approval=False,
                   trait=rec["trait"])

    if act == "ShutIn":
        # 目标时率 = 1.2 × 供液比（= qi/q泵），1.2 是沉没度余量
        target = min(0.95, max(0.05, 1.2 * rec["supply_ratio"]))
        if target >= duty_now * 0.9:        # 已在最优附近，别折腾
            d.update(action="Hold", notes=["时率已接近最优，无需调整"])
        else:
            on, off, duty, notes = cycle(target, rec["trait"], month)
            saved = max(0.0, (duty_now - duty) * KW * 24)
            d.update(duty_target=duty, on=on, off=off, keep=1.0,
                     saved=round(saved, 1),
                     notes=notes + ["供液受限：产量由供液能力决定，压缩时率不降产"])
    elif act == "Start":
        target = min(0.98, max(duty_now, rec["eff"]))
        if duty_now >= 0.95:                # 已连续生产，无需再提
            d.update(action="Hold", duty_target=duty_now,
                     notes=["已接近连续生产，无需提高时率"])
        else:
            on, off, duty, notes = cycle(target, rec["trait"], month)
            d.update(duty_target=duty, on=on, off=off, keep=1.0,
                     saved=0.0, approval=True,
                     notes=notes + ["提高时率需人工审批（单向安全）"])
    elif act == "Escalate":
        d.update(keep=1.0, notes=["系统不自动开井，转人工评估复产条件"])
    elif act == "Stop":
        d.update(keep=0.0, notes=["建议进入关停/措施评估，属经营决策"])

    d["confidence"] = ("high" if abs(rec["eff"]) > 0.05 and rec["days"] >= 5
                       else "medium" if rec["days"] >= 3 else "low")
    return d


# ─────────────────────────── RDF 与门禁 ───────────────────────────
def to_rdf(rows: list[tuple[dict, dict]], start: str, end: str) -> Graph:
    g = Graph()
    g.bind("ao", AO); g.bind("act", ACT); g.bind("rule", RULE); g.bind("metric", MET)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()
    for rec, d in rows:
        node = AO[f"decision/{rec['well']}"]
        g.add((node, RDF.type, AO.WellDecision))
        g.add((node, AO.decidesWell, AO[f"well/{rec['well']}"]))
        g.add((node, AO.hasAction, ACT[d["action"]]))
        g.add((node, AO.ruleFired, RULE[d["rule"]["iri"]]))
        g.add((node, AO.confidence, Literal(d["confidence"])))
        g.add((node, AO.decidedAt, Literal(now, datatype=XSD.dateTime)))
        g.add((node, AO.windowDays, Literal(rec["days"], datatype=XSD.integer)))
        g.add((node, AO.hasTrait, Literal(rec["trait"])))
        if d["action"] == "Start":
            g.add((node, AO.requiresApproval, Literal(d["approval"], datatype=XSD.boolean)))
        for key, metric in (("eff", "pump_eff"), ("duty", "duty_ratio"),
                            ("unit_power", "unit_power"), ("oil_pd", "oil_rate"),
                            ("liquid_sum", "liquid_m3")):
            ev = AO[f"evidence/{rec['well']}-{metric}"]
            g.add((ev, RDF.type, AO.MetricValue))
            g.add((ev, AO.ofMetric, MET[metric]))
            g.add((ev, AO.value, Literal(round(float(rec[key]), 4), datatype=XSD.decimal)))
            g.add((ev, AO.observedIn, Literal(f"{start}~{end}")))
            g.add((node, AO.hasEvidence, ev))
        if d["on"] or d["off"]:
            g.add((node, AO.targetDuty, Literal(d["duty_target"], datatype=XSD.decimal)))
            g.add((node, AO.onHours, Literal(d["on"], datatype=XSD.decimal)))
            g.add((node, AO.offHours, Literal(d["off"], datatype=XSD.decimal)))
        g.add((node, AO.expectedLiquidKeep, Literal(d["keep"], datatype=XSD.decimal)))
        g.add((node, AO.expectedKwhSaved, Literal(d["saved"], datatype=XSD.decimal)))
    return g


def main() -> int:
    rules, cons = load_rules()
    print("规则本体装载（按优先级）：")
    for r in rules:
        print(f"  {r['id']} p{r['prio']:<3} {r['metric']} {r['op']} {r['thr']:g}"
              f" → {r['action']:8s} | {r['label']}")
    print(f"约束：{', '.join(f'{k}={v:g}' for k, v in cons.items())}")

    con = sqlite3.connect(DB)
    maxday = con.execute("SELECT MAX(day) FROM well_daily").fetchone()[0]
    end = date.fromisoformat(maxday)
    start = date.fromordinal(end.toordinal() - WINDOW_DAYS + 1)
    print(f"\n决策窗口：{start} ~ {end}（{WINDOW_DAYS} 天）\n")

    recs = features(con, start.isoformat(), end.isoformat())
    rows = [(rec, decide(rec, rules, end.month)) for rec in recs]

    hdr = (f"{'井':8s} {'状态':4s} {'时率':>5s} {'泵效':>5s} {'吨液电耗':>7s} "
           f"{'日产油':>6s} {'规则':>4s} {'动作':>8s} {'目标时率':>7s} {'开/停h':>11s} "
           f"{'保持':>5s} {'节电':>6s}")
    print(hdr)
    print("-" * 108)
    for rec, d in rows:
        cycle_s = f"{d['on']:.1f}/{d['off']:.1f}" if d["off"] else "-"
        print(f"{rec['well']:8s} {rec['status']:4s} {rec['duty']*100:4.0f}% "
              f"{rec['eff']*100:4.0f}% {rec['unit_power']:7.1f} "
              f"{rec['oil_pd']:6.2f} {d['rule']['id']:>4s} {d['action']:>8s} "
              f"{d['duty_target']*100:6.0f}% {cycle_s:>11s} "
              f"{d['keep']*100:4.0f}% {d['saved']:6.1f}")
    print("-" * 108)

    by_action: dict[str, list] = {}
    for rec, d in rows:
        by_action.setdefault(d["action"], []).append((rec, d))
    saved = sum(d["saved"] for _, d in rows)
    liquid_keep = sum(rec["liquid_pd"] * d["keep"] for rec, d in rows)
    liquid_now = sum(rec["liquid_pd"] for rec, _ in rows)
    print("汇总：")
    for a, items in sorted(by_action.items(), key=lambda kv: -len(kv[1])):
        print(f"  {a:8s} {len(items):2d} 井  "
              f"{', '.join(rec['well'] for rec, _ in items[:6])}"
              f"{' …' if len(items) > 6 else ''}")
    print(f"  预期日节电 {saved:.0f} kWh | 产液保持 "
          f"{liquid_keep/liquid_now*100 if liquid_now else 0:.1f}%"
          f"（{liquid_now:.1f} → {liquid_keep:.1f} m³/d）")

    print("\n决策理由样例（前 3 条，可直接贴到告警/工单里）：")
    for rec, d in rows[:3]:
        print(f"  · {rec['well']}：泵效 {rec['eff']*100:.0f}% / 时率 {rec['duty']*100:.0f}% "
              f"→ 命中 {d['rule']['id']}「{d['rule']['label']}」，"
              f"动作 {d['action']}；{d['rule']['rationale']}")
        for n in d["notes"]:
            print(f"      约束：{n}")

    g = to_rdf(rows, start.isoformat(), end.isoformat())
    out = ROOT / "build" / "well-decisions.ttl"
    g.serialize(out, format="turtle")
    print(f"\n决策已写出：{out}（{len(rows)} 条，含规则、证据、周期）")

    shapes = Graph().parse(ONT / "decision-shapes.ttl", format="turtle")
    ont = Graph()
    for f in ("analytics-core.ttl", "decision-core.ttl"):
        ont.parse(ONT / f, format="turtle")
    ok, report, _ = shacl_validate(data_graph=g + ont, shacl_graph=shapes,
                                   ont_graph=ont, inference="none", advanced=True)
    q = """PREFIX sh: <http://www.w3.org/ns/shacl#>
           SELECT ?f ?m WHERE { ?r a sh:ValidationResult ; sh:focusNode ?f ; sh:resultMessage ?m }"""
    finds = list(report.query(q))
    print(f"决策门禁 conforms = {ok} | 结论 {len(finds)} 条")
    for f, m in finds[:8]:
        print(f"  ✗ {short(f)}: {m}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

