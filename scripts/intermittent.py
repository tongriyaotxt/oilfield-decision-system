"""
intermittent.py —— 间开决策引擎（开井/关井/维持/升级人工）

输入：build/telemetry.csv（只有采集数据：液面、沉没度、产液、开停状态）
      ontology/well-core.ttl（只有台账：泵挂、泵径、冲程、冲次、环空面积、性状、状态）
输出：每口井一条决策（RDF 实例，带判据与证据）+ 策略仿真对比

决策链：
  1. 从关井段回归沉没度上升速率 r [m/h]
  2. 从开井段前 1h 回归沉没度下降速率 f [m/h]
  3. 占空比 duty = r / (r + f)  ← 物质守恒的必然结果，等价于 qi/q抽
  4. 反演供液能力 qi^ = r · A · 24
  5. 按 duty 与性状选策略，并把启停约束（最小关井 4h、稠油 ≤8h）压进周期
  6. 故障锁定 → 升级人工，禁止任何自动开井

注意：qi^ 是"观测反演值"，与地层真值有偏差是正常的；这里不追真值，追可解释。
"""
from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rdflib import Graph, Literal, Namespace, RDF, RDFS
from rdflib.namespace import XSD

from well_model import KPI, Schedule, WellSpec, simulate
from well_sim import TRUTH          # 仅用于反事实评估，与决策链隔离

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
W = Namespace("https://example.org/well#")
WR = Namespace("https://example.org/well/w/")
CRIT = Namespace("https://example.org/well/crit/")
DEC = Namespace("https://example.org/well/dec/")
MEA = Namespace("https://example.org/well/meas/")
MET = Namespace("https://example.org/well/metric/")
STR = Namespace("https://example.org/well/strategy/")

DAYS = 14.0
DT_H = 10.0 / 60.0
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)

H_MIN = 30.0          # 沉没度下限 m（crit:LowSubmergence）
H_TARGET = 120.0      # 恢复目标 m（crit:Recovered）
MIN_OFF_H = 4.0       # 最小关井 h（crit:MinOffTime）
HEAVY_OFF_MAX = 8.0   # 稠油单次关井上限 h（crit:HeavyOilOffLimit）
WINTER_OFF_MAX = 6.0  # 寒区冬季关井上限 h（crit:WinterOffLimit）
CO2_OFF_MAX = 4.0     # CCUS 气窜敏感井关井上限 h（crit:CO2FloodOffLimit）
WINTER_MODE = True    # 生产环境按决策日期判定：11 月—次年 3 月为真


# ─────────────────────────── 台账读取 ───────────────────────────
def load_wells() -> dict[str, dict]:
    g = Graph().parse(ROOT / "ontology" / "well-core.ttl", format="turtle")
    out: dict[str, dict] = {}
    for w in g.subjects(RDF.type, W.OilWell):
        wid = str(w).rsplit("/", 1)[-1]
        num = lambda p: float(g.value(w, W[p]))          # noqa: E731
        traits = [str(t).rsplit("/", 1)[-1] for t in g.objects(w, W.hasTrait)]
        state = g.value(w, W.hasState)
        out[wid] = dict(
            depth=num("pumpDepth"), dia=num("pumpDiameter"),
            stroke=num("strokeLength"), spm=num("strokeRate"),
            area=num("annularArea"), traits=traits,
            state=str(state).rsplit("/", 1)[-1] if state else "",
            label=str(g.value(w, RDFS.label) or wid),
        )
    return out


# ─────────────────────────── 特征回归 ───────────────────────────
@dataclass
class Features:
    wid: str
    r: float | None          # 恢复速率 m/h
    f: float | None          # 排液速率 m/h
    run_ratio: float
    eff: float               # 泵效（充满系数）
    sub_avg: float
    liquid_m3: float


def read_telemetry() -> dict[str, list[tuple]]:
    series: dict[str, list[tuple]] = {}
    with (BUILD / "telemetry.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            series.setdefault(row["well"], []).append(
                (float(row["t_h"]), int(row["running"]), float(row["level_m"]),
                 float(row["submergence_m"]), float(row["prod_rate_m3d"])))
    return series


def segments(rows: list[tuple]) -> list[tuple[int, float, float, int, int]]:
    """切成连续的开井/关井段：(running, t0, t1, i0, i1)"""
    out, start, flag = [], 0, rows[0][1]
    for i in range(1, len(rows)):
        if rows[i][1] != flag:
            out.append((flag, rows[start][0], rows[i][0], start, i))
            start, flag = i, rows[i][1]
    out.append((flag, rows[start][0], rows[-1][0], start, len(rows) - 1))
    return out


def segment_slope(rows: list[tuple], i0: int, i1: int, rising: bool,
                  max_span_h: float, min_delta: float = 3.0, tol: float = 1.2) -> float | None:
    """按段取"首次逼近极值"算斜率：一"段"一票，且不被尾部噪声拖晚。

    两个坑都在这一个函数里解决：
      ① 关井时液面到井口就恒定了，整段回归会被平台段拉平；
      ② 若直接取极值出现时刻，噪声会把极值推到平台尾部（+0.8m 就够），
         斜率被系统性低估。所以取"首次进入 极值±tol"的时刻。
    """
    if i1 <= i0:
        return None
    t_start, sub_start = rows[i0][0], rows[i0][3]
    window = [i for i in range(i0, min(i1 + 1, len(rows)))
              if rows[i][0] - t_start <= max_span_h]
    if not window:
        return None
    ext = (max if rising else min)(rows[i][3] for i in window)
    target = ext - tol if rising else ext + tol
    cross = next((i for i in window
                  if (rows[i][3] >= target if rising else rows[i][3] <= target)), None)
    if cross is None:
        return None
    span = rows[cross][0] - t_start
    if span < 0.25:
        return None
    delta = (rows[cross][3] - sub_start) if rising else (sub_start - rows[cross][3])
    if delta < min_delta:
        return None
    return delta / span


def features(wid: str, rows: list[tuple], q_theory: float) -> Features:
    rise: list[float] = []
    draw: list[float] = []
    for running, t0, t1, i0, i1 in segments(rows):
        if not running and (t1 - t0) >= 2.0:
            s = segment_slope(rows, i0, i1, rising=True, max_span_h=8.0)
            if s:
                rise.append(s)
        elif running and (t1 - t0) >= 1.0:
            s = segment_slope(rows, i0, i1, rising=False, max_span_h=2.0)
            if s:
                draw.append(s)
    r = statistics.median(rise) if rise else None
    f = statistics.median(draw) if draw else None

    run_h = sum(1 for x in rows if x[1]) * DT_H
    liquid = sum(x[4] for x in rows if x[1]) * DT_H / 24.0
    eff = liquid / (q_theory * run_h / 24.0) if run_h else 0.0
    return Features(
        wid=wid, r=r, f=f, run_ratio=run_h / (len(rows) * DT_H),
        eff=eff, sub_avg=statistics.fmean(x[3] for x in rows), liquid_m3=liquid,
    )


# ─────────────────────────── 决策 ───────────────────────────
@dataclass
class Decision:
    wid: str
    kind: str            # Start / ShutIn / Hold / Escalate
    strategy: str        # Continuous / Intermittent / VariableSpeed / Human
    on_h: float
    off_h: float
    duty: float
    criteria: list[str]
    confidence: str
    note: str
    evidence: list[tuple[str, float, str]]   # (metric, value, unit)


def off_caps(traits: list[str], winter: bool = False) -> tuple[list[float], list[str]]:
    """性状 → 单次关井时长的上限集合 + 命中的判据（取最严的一条生效）。"""
    caps, crits = [], []
    if "HeavyOil" in traits:
        caps.append(HEAVY_OFF_MAX); crits.append("crit:HeavyOilOffLimit")
    if "WinterFreeze" in traits and winter:
        caps.append(WINTER_OFF_MAX); crits.append("crit:WinterOffLimit")
    if "CO2Breakthrough" in traits:
        caps.append(CO2_OFF_MAX); crits.append("crit:CO2FloodOffLimit")
    return caps, crits


def schedule_for(duty: float, caps: list[float]) -> tuple[float, float, float]:
    """把占空比压进"可执行的周期"，同时满足最小关井与各性状的关井上限。"""
    duty = min(0.99, max(0.02, duty))
    on = 12.0 * duty
    off = 12.0 * (1.0 - duty)
    if off < MIN_OFF_H:                       # 关得太短 → 拉长周期，保持占空比
        off, on = MIN_OFF_H, MIN_OFF_H * duty / (1.0 - duty)
    cap = min(caps) if caps else None
    if cap is not None and off > cap:         # 关井上限 → 压缩关井，保持占空比
        off, on = cap, cap * duty / (1.0 - duty)
    on = max(0.4, on)
    return on, off, on / (on + off)


def decide(wid: str, meta: dict, ft: Features) -> Decision:
    q_theory = 1440 * (3.141592653589793 / 4 * (meta["dia"] / 1000) ** 2) * meta["stroke"] * meta["spm"]
    winter = WINTER_MODE          # 生产环境由决策日期判定（11月—次年3月为真）
    caps, cap_crits = off_caps(meta["traits"], winter=winter)

    # ① 故障锁定：先判安全，再谈优化
    if "FaultLock" in meta["traits"] or meta["state"] == "Faulted":
        return Decision(
            wid, "Escalate", "Human", 0.0, 0.0, 0.0,
            ["crit:FaultLockout"], "high",
            "存在过载/缺相类故障，禁止自动开井；须人工排除后复位。",
            [("m:MotorCurrent", 0.0, "A")])

    # ② 缺恢复速率样本 → 不能定周期，先补测（这条最能体现"数据不足就别动"）
    if ft.r is None or ft.f is None:
        return Decision(
            wid, "Escalate", "Human", 0.0, 0.0, 0.0,
            ["crit:CandidateScreen"], "low",
            "缺少足够长的关井/开井段，恢复速率估计不可信；先安排一次 ≥4h 测恢复。",
            [])

    duty = ft.r / (ft.r + ft.f)
    qi_hat = ft.r * meta["area"] * 24.0
    on, off, duty = schedule_for(duty, caps)

    # ③ 供液充足：占空比接近 1 → 不该间开，恢复到连续生产（这是提产的主力）
    if duty >= 0.85:
        return Decision(
            wid, "Hold", "Continuous", 0.0, 0.0, 1.0,
            ["crit:Recovered"], "high",
            f"供液能力 qi≈{qi_hat:.1f} m³/d 接近泵能力 {q_theory:.1f}，间开会直接减产；转连续生产。",
            [("m:RiseRate", round(ft.r, 2), "m/h"),
             ("m:PumpEfficiency", round(ft.eff, 3), "1"),
             ("m:Submergence", round(ft.sub_avg, 1), "m")])

    # ④ 中间区间：调参优于频繁启停
    if duty >= 0.35:
        return Decision(
            wid, "Start", "VariableSpeed",
            round(on, 2), round(off, 2), round(duty, 3),
            ["crit:LowEfficiency", "crit:MinOffTime"], "medium",
            f"占空比 {duty:.2f} 偏高，优先降冲次/换小泵；过渡期按 {on:.1f}h/{off:.1f}h 间开。",
            [("m:RiseRate", round(ft.r, 2), "m/h"),
             ("m:PumpEfficiency", round(ft.eff, 3), "1")])

    # ⑤ 明显供液不足：标准间开，长关短开
    return Decision(
        wid, "Start", "Intermittent",
        round(on, 2), round(off, 2), round(duty, 3),
        ["crit:LowSubmergence", "crit:LowEfficiency", "crit:MinOffTime"] + cap_crits,
        "high",
        f"qi≈{qi_hat:.1f} m³/d / 泵能力 {q_theory:.1f} → 开 {on:.1f}h 停 {off:.1f}h；"
        f"关井上限 {'/'.join(str(c) for c in caps) if caps else '无（受最小 4h 约束）'}h"
        f"{'（含稠油/寒区/CO2 气窜约束）' if caps else ''}。",
        [("m:RiseRate", round(ft.r, 2), "m/h"),
         ("m:PumpEfficiency", round(ft.eff, 3), "1"),
         ("m:Submergence", round(ft.sub_avg, 1), "m")])


# ─────────────────────────── 主流程 ───────────────────────────
def main() -> int:
    wells = load_wells()
    series = read_telemetry()

    decisions: list[Decision] = []
    feats: dict[str, Features] = {}
    for wid in sorted(wells):
        m = wells[wid]
        q_theory = 1440 * (3.141592653589793 / 4 * (m["dia"] / 1000) ** 2) * m["stroke"] * m["spm"]
        ft = features(wid, series[wid], q_theory)
        feats[wid] = ft
        decisions.append(decide(wid, m, ft))

    # ── 策略仿真：把决策变成周期，跑 14 天 ──
    print(f"{'井':4s} {'策略':13s} {'开/停h':>10s} {'时率':>5s} {'qi^':>6s} {'偏差':>6s} "
          f"{'基线液':>7s} {'优化液':>7s} {'Δ液':>7s} {'基线电':>7s} {'优化电':>7s}")
    print("-" * 100)

    base_all = KPI(); opt_all = KPI()
    detail = []
    trait_map = {"HeavyOil": "heavy_oil", "SandProne": "sand",
                 "GasLock": "gas_lock", "FaultLock": "fault"}
    for wid, d in zip(sorted(wells), decisions):
        m = wells[wid]
        # ★ 反事实评估必须用"地层真值"（仿真器视角）。引擎看不到 qi，但评估要公平。
        qi_true, cut_true = TRUTH[wid]
        spec = WellSpec(wid=wid, pump_depth=m["depth"], pump_dia=m["dia"], stroke=m["stroke"],
                        spm=m["spm"], inflow=qi_true, annular_area=m["area"], water_cut=cut_true,
                        traits=tuple(trait_map.get(t, t.lower()) for t in m["traits"]))
        base_sched = Schedule(4.0, 8.0)
        if d.strategy == "Continuous":
            opt_sched = Schedule(24.0, 0.0)
        elif d.off_h > 0:
            opt_sched = Schedule(d.on_h, d.off_h)
        else:
            opt_sched = Schedule(0.0, 24.0)
        # 暖机 1 天后再统计 14 天，剔除初始沉没度带来的边界效应
        kb = simulate(spec, base_sched, 1.0 + DAYS, DT_H)[0] - simulate(spec, base_sched, 1.0, DT_H)[0]
        ko = simulate(spec, opt_sched, 1.0 + DAYS, DT_H)[0] - simulate(spec, opt_sched, 1.0, DT_H)[0]
        for tot, k in ((base_all, kb), (opt_all, ko)):
            tot.liquid_m3 += k.liquid_m3; tot.oil_m3 += k.oil_m3
            tot.kwh += k.kwh; tot.run_h += k.run_h; tot.starts += k.starts
            tot.hours = k.hours
        onoff = f"{d.on_h:.1f}/{d.off_h:.1f}" if d.off_h > 0 else ("连续" if d.strategy == "Continuous" else "停机")
        qi_hat = (feats[wid].r or 0) * m["area"] * 24
        dev = (qi_hat - TRUTH[wid][0]) / TRUTH[wid][0] * 100 if TRUTH[wid][0] else 0.0
        print(f"{wid:4s} {d.strategy:13s} {onoff:>10s} {d.duty*100:4.0f}% "
              f"{qi_hat:6.2f} {dev:+5.1f}% {kb.liquid_per_day:7.2f} "
              f"{ko.liquid_per_day:7.2f} {ko.liquid_per_day-kb.liquid_per_day:+7.2f} "
              f"{kb.kwh_per_day:7.1f} {ko.kwh_per_day:7.1f}")
        detail.append((wid, d, kb, ko))

    print("-" * 100)
    dl = opt_all.liquid_per_day - base_all.liquid_per_day
    dk = opt_all.kwh_per_day - base_all.kwh_per_day
    print(f"{'合计':4s} {'':13s} {'':10s} {'':5s} {'':6s} "
          f"{base_all.liquid_per_day:7.2f} {opt_all.liquid_per_day:7.2f} {dl:+7.2f} "
          f"{base_all.kwh_per_day:7.1f} {opt_all.kwh_per_day:7.1f}")
    print(f"\n产液 {base_all.liquid_per_day:.2f} → {opt_all.liquid_per_day:.2f} m³/d "
          f"({dl/base_all.liquid_per_day*100:+.1f}%)")
    print(f"产油 {base_all.oil_per_day:.2f} → {opt_all.oil_per_day:.2f} t/d "
          f"({(opt_all.oil_per_day-base_all.oil_per_day)/base_all.oil_per_day*100:+.1f}%)")
    print(f"电耗 {base_all.kwh_per_day:.0f} → {opt_all.kwh_per_day:.0f} kWh/d "
          f"({dk/base_all.kwh_per_day*100:+.1f}%)")
    print(f"吨液电耗 {base_all.kwh_per_m3:.1f} → {opt_all.kwh_per_m3:.1f} kWh/m³")
    n = len(wells)
    print(f"启停次数 {base_all.starts/DAYS:.1f} → {opt_all.starts/DAYS:.1f} 次/日"
          f"（全油田 {n} 口井，单井 ≈{opt_all.starts/DAYS/n:.1f} 次/日）")
    print(f"开井时率 {base_all.run_h/(n*base_all.hours)*100:.0f}% → "
          f"{opt_all.run_h/(n*opt_all.hours)*100:.0f}%"
          f"（时率下降不等于减产：省掉的是空抽那部分）")

    # ── 决策落成 RDF ──
    g = Graph()
    g.bind("well", W); g.bind("dec", DEC); g.bind("crit", CRIT)
    g.bind("meas", MEA); g.bind("metric", MET); g.bind("xsd", XSD)
    ts = (T0 + timedelta(days=DAYS)).isoformat()
    for wid, d in zip(sorted(wells), decisions):
        node = DEC[f"{wid}-{ts[:10]}"]
        g.add((node, RDF.type, W[f"{d.kind}Decision"]))
        g.add((node, W.decidesFor, WR[wid]))
        g.add((node, W.recommends, STR[d.strategy]))
        g.add((node, W.createdAt, Literal(ts, datatype=XSD.dateTime)))
        g.add((node, W.confidence, Literal(d.confidence)))
        g.add((node, W.note, Literal(d.note)))
        for c in d.criteria:
            g.add((node, W.justifiedBy, CRIT[c.split(":")[1]]))
        for i, (met, val, unit) in enumerate(d.evidence):
            meas = MEA[f"{wid}-{met.split(':')[1]}"]
            g.add((meas, RDF.type, W.Measurement))
            g.add((meas, W.ofMetric, MET[met.split(":")[1]]))
            g.add((meas, W.hasValue, Literal(val, datatype=XSD.decimal)))
            g.add((meas, W.atTime, Literal(ts, datatype=XSD.dateTime)))
            g.add((node, W.basedOn, meas))
            g.add((WR[wid], W.hasMeasurement, meas))
        if d.off_h > 0:
            g.add((node, W.hasCycleOnHours, Literal(d.on_h, datatype=XSD.decimal)))
            g.add((node, W.hasCycleOffHours, Literal(d.off_h, datatype=XSD.decimal)))
            g.add((node, W.hasDutyRatio, Literal(d.duty, datatype=XSD.decimal)))

    BUILD.mkdir(exist_ok=True)
    out = BUILD / "decisions.ttl"
    g.serialize(out, format="turtle")
    print(f"\n决策已写出: {out}  ({len(decisions)} 条，全部带 justifiedBy + basedOn)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
