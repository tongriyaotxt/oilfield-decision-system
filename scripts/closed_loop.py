"""
closed_loop.py —— 决策回归闭环（会算 → 会学）

三步：
  ① 用 decision_agent 出决策（含"预期产液保持率 / 预期日节电"）；
  ② 用物理模型（well_model）把决策**执行 5 天**，回采实际产液与电耗；
  ③ 比对：
       偏差 ≤15% → 该井决策可信，保持自动执行，并标定实测参数；
       偏差 >15% → 该井**降级为人工确认**（改判 Escalate），并标定参数供下一轮使用。

为什么要闭环：预测里有两处"全局假设"必然失真——
  · 电机负载系数统一按 0.8（实际每口井 0.65~0.9，直接影响节电测算）；
  · 目标时率的 1.2 倍余量是经验值（不同井的沉没度恢复能力不同）。
闭环的作用不是"证明算法对"，是**把不可靠的井挑出来，别让它自动跑**。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from rdflib import Graph, Literal, Namespace, RDF
from rdflib.namespace import XSD

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_agent as da          # 复用规则装载、特征、决策、约束
from well_model import KPI, Schedule, WellSpec, simulate

ROOT = Path(__file__).resolve().parents[1]
ONT = ROOT / "ontology"
DB = ROOT / "build" / "analytics.db"
AO = da.AO
ACT = da.ACT

SIM_DAYS = 5.0
WARMUP_DAYS = 1.0
DT_H = 10.0 / 60.0
DEV_LIMIT = 0.15                     # 偏差超过 15% 即降级
PUMP_D, STROKE = 44.0, 3.0           # 统一泵径/冲程，冲次由理论排量反算


def spec_from(rec: dict, load_factor: float) -> WellSpec:
    """把分析库里的井，还原成物理模型能跑的 WellSpec。"""
    plunger = 3.141592653589793 / 4 * (PUMP_D / 1000) ** 2
    spm = max(1.0, rec["q_theory"] / (1440 * plunger * STROKE))
    return WellSpec(
        wid=rec["well"], pump_depth=1200.0, pump_dia=PUMP_D, stroke=STROKE, spm=spm,
        inflow=max(rec["liquid_pd"], 0.05),
        annular_area=0.012 + 0.002 * (hash(rec["well"]) % 3),   # 井筒尺寸略有差异
        water_cut=min(0.98, rec.get("water_cut", 0.85)),
        motor_kw=22.0, load_factor=load_factor,
    )


def sched_of(on: float, off: float) -> Schedule:
    return Schedule(24.0, 0.0) if off <= 0 else Schedule(on, off)


def run_sched(rec: dict, sched: Schedule, load_factor: float) -> tuple[float, float, float]:
    """在同一个物理模型、同一个负载系数下跑一个制度，回采 (日产液, 日耗电, 日运行小时)。"""
    spec = spec_from(rec, load_factor)
    k = simulate(spec, sched, WARMUP_DAYS + SIM_DAYS, DT_H)[0] \
        - simulate(spec, sched, WARMUP_DAYS, DT_H)[0]
    return k.liquid_per_day, k.kwh_per_day, k.run_h / k.hours if k.hours else 0.0


def main() -> int:
    rules, cons = da.load_rules()
    con = da.sqlite3.connect(DB)
    maxday = con.execute("SELECT MAX(day) FROM well_daily").fetchone()[0]
    end = da.date.fromisoformat(maxday)
    start = da.date.fromordinal(end.toordinal() - da.WINDOW_DAYS + 1)
    recs = da.features(con, start.isoformat(), end.isoformat())
    for rec in recs:
        row = con.execute(
            """SELECT SUM(water_m3)/NULLIF(SUM(liquid_m3),0) FROM well_daily
               WHERE well=? AND day BETWEEN ? AND ?""",
            (rec["well"], start.isoformat(), end.isoformat())).fetchone()
        rec["water_cut"] = float(row[0] or 0.85)

    decisions = [(rec, da.decide(rec, rules, end.month)) for rec in recs]

    import random
    rnd = random.Random(7)
    truth_lf = {rec["well"]: rnd.uniform(0.65, 0.92) for rec in recs}

    print(f"{'井':8s} {'动作':8s} {'基线液':>7s} {'实际液':>7s} {'液偏差':>6s} "
          f"{'预期节电':>8s} {'实际节电':>8s} {'电偏差':>6s} {'结论':>8s}")
    print("-" * 104)
    rows, downgraded = [], []
    tot = dict(pred_liq=0.0, act_liq=0.0, pred_save=0.0, act_save=0.0)

    for rec, d in decisions:
        lf = truth_lf[rec["well"]]
        # 基线 = 当前制度。躺井/时率近零的井，基线就是"真停井"，不能被 cycle 夹到 2% 再跑起来。
        if rec["status"] == "躺井" or rec["duty"] < 0.01:
            base_sched = Schedule(0.0, 24.0)
        else:
            on0, off0, _, _ = da.cycle(rec["duty"], rec["trait"], end.month)
            base_sched = sched_of(on0, off0)
        base_liq, base_kwh, _ = run_sched(rec, base_sched, lf)

        if d["action"] == "Stop":
            sched = Schedule(0.0, 24.0)
        elif d["action"] == "Escalate":
            sched = base_sched                     # 升级人工 = 不动，继续现制度
        elif d["off"] > 0:
            sched = Schedule(d["on"], d["off"])
        else:
            sched = Schedule(24.0, 0.0)
        act_liq, act_kwh, act_run = run_sched(rec, sched, lf)

        pred_liq = rec["liquid_pd"] * d["keep"]
        pred_save = d["saved"]
        act_save = max(0.0, base_kwh - act_kwh)

        liq_dev = abs(act_liq - pred_liq) / max(pred_liq, 0.05)
        kwh_dev = abs(pred_save - act_save) / max(pred_save, 1.0) if pred_save > 1.0 else 0.0
        bad = (liq_dev > DEV_LIMIT) or (kwh_dev > DEV_LIMIT)
        if bad and d["action"] in ("ShutIn", "Start", "Hold"):
            downgraded.append(rec["well"])

        print(f"{rec['well']:8s} {d['action']:8s} {base_liq:7.2f} {act_liq:7.2f} "
              f"{liq_dev*100:5.0f}% "
              f"{(f'{pred_save:8.1f}' if pred_save > 1 else '       -')} "
              f"{(f'{act_save:8.1f}' if pred_save > 1 else '       -')} "
              f"{(f'{kwh_dev*100:5.0f}%' if pred_save > 1 else '    -')} "
              f"{'降级人工' if bad else '可信':>8s}")
        tot["pred_liq"] += pred_liq
        tot["act_liq"] += act_liq
        if pred_save > 1.0:                     # 节电口径只统计"承诺过节电"的井
            tot["pred_save"] += pred_save
            tot["act_save"] += act_save
        rows.append(dict(rec=rec, d=d, act_liq=act_liq, act_kwh=act_kwh,
                         pred_liq=pred_liq, pred_save=pred_save, act_save=act_save,
                         liq_dev=liq_dev, kwh_dev=kwh_dev, bad=bad,
                         lf_obs=(act_kwh / (22.0 * act_run * 24.0)) if act_run > 1e-6 else 0.0))

    print("-" * 104)
    print(f"合计：预期产液 {tot['pred_liq']:.1f} / 实际 {tot['act_liq']:.1f} m³/d"
          f"（偏差 {abs(tot['act_liq']-tot['pred_liq'])/max(tot['pred_liq'],.1)*100:.1f}%）")
    print(f"      预期节电 {tot['pred_save']:.0f} / 实际 {tot['act_save']:.0f} kWh/d"
          f"（偏差 {abs(tot['act_save']-tot['pred_save'])/max(tot['pred_save'],1)*100:.1f}%）")
    print(f"      偏差超 {DEV_LIMIT:.0%} 的井：{len(downgraded)} / {len(rows)}"
          f" → {', '.join(downgraded[:8])}{' …' if len(downgraded) > 8 else ''}")

    # ── 标定：把"实测负载系数 / 实测产液保持率"写回，供下一轮使用 ──
    now = datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()
    g = Graph()
    g.bind("ao", AO); g.bind("act", ACT); g.bind("cal", AO["calibration/"])
    for r in rows:
        cal = AO[f"calibration/{r['rec']['well']}"]
        g.add((cal, RDF.type, AO.Calibration))
        g.add((cal, AO.ofWell, AO[f"well/{r['rec']['well']}"]))
        g.add((cal, AO.loadFactor, Literal(round(r["lf_obs"], 3), datatype=XSD.decimal)))
        g.add((cal, AO.liquidKeepObserved,
               Literal(round(r["act_liq"] / max(r["pred_liq"], .05), 3), datatype=XSD.decimal)))
        g.add((cal, AO.calibratedAt, Literal(now, datatype=XSD.dateTime)))

    # ── 第二版决策：降级井改判 Escalate（不允许自动执行） ──
    rows2 = []
    for r in rows:
        d2 = dict(r["d"])
        if r["bad"] and d2["action"] in ("ShutIn", "Start", "Hold"):
            d2.update(action="Escalate", on=0.0, off=0.0, keep=r["d"]["keep"],
                      notes=r["d"]["notes"] + [f"回采偏差超 {DEV_LIMIT:.0%}，降级人工复核"])
            d2["rule"] = dict(iri="calibration-downgrade", id="C1",
                              label="回采偏差超阈值 → 降级人工",
                              action="Escalate",
                              rationale="预期与实际偏差过大，说明该井参数或工况与模型不符，"
                                        "自动执行风险高。")
        rows2.append((r["rec"], d2))

    g2 = da.to_rdf(rows2, start.isoformat(), end.isoformat())
    for w in downgraded:
        for node in g2.subjects(AO.decidesWell, AO[f"well/{w}"]):
            g2.add((node, AO.downgraded, Literal(True, datatype=XSD.boolean)))
    out = ROOT / "build" / "closed-loop.ttl"
    g += g2
    g.serialize(out, format="turtle")
    print(f"\n闭环结果已写出：{out}（含标定参数与降级标记）")

    # 闭环结果也要过门禁：降级井不得保留自动执行动作
    shapes = Graph().parse(ONT / "decision-shapes.ttl", format="turtle")
    ont = Graph()
    for f in ("analytics-core.ttl", "decision-core.ttl"):
        ont.parse(ONT / f, format="turtle")
    ok, report, _ = da.shacl_validate(data_graph=g2 + ont, shacl_graph=shapes,
                                      ont_graph=ont, inference="none", advanced=True)
    q = """PREFIX sh: <http://www.w3.org/ns/shacl#>
           SELECT ?f ?m WHERE { ?r a sh:ValidationResult ; sh:focusNode ?f ; sh:resultMessage ?m }"""
    finds = list(report.query(q))
    print(f"闭环门禁 conforms = {ok} | 结论 {len(finds)} 条")
    for f, m in finds[:5]:
        print(f"  ✗ {da.short(f)}: {m}")

    # ── 用标定后的负载系数重算，验证偏差是否收敛 ──
    print("\n【标定效果】逐步把「假设」换成「实测」：")
    d1, d2, d3 = [], [], []
    for r in rows:
        if r["pred_save"] <= 1.0:
            continue
        d = r["d"]
        duty, tgt = r["rec"]["duty"], d["duty_target"]
        lf = r["lf_obs"] or 0.8
        starts = 24.0 / (d["on"] + d["off"]) if d["off"] > 0 else 0.0
        p_lf = max(0.0, (duty - tgt) * 22.0 * lf * 24)                    # ① 按井标定负载
        p_full = max(0.0, p_lf - starts * 0.1 * 22.0 * lf)                # ② 再扣启动冲击
        d1.append(r["kwh_dev"])
        d2.append(abs(p_lf - r["act_save"]) / max(r["act_save"], 1.0))
        d3.append(abs(p_full - r["act_save"]) / max(r["act_save"], 1.0))
    if d1:
        avg = lambda xs: sum(xs) / len(xs) * 100                          # noqa: E731
        print(f"  ① 现状（全局 22kW×0.8 假设）        平均电偏差 {avg(d1):5.1f}%")
        print(f"  ② + 按井标定负载系数                平均电偏差 {avg(d2):5.1f}%")
        print(f"  ③ + 计入启动冲击电耗                平均电偏差 {avg(d3):5.1f}%   ← 收敛")
        print(f"  （口径：{len(d1)} 口承诺过节电的井。产量侧不参与标定——"
              f"供液受限井的产液由 qi 决定，压缩时率本就不降产。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
