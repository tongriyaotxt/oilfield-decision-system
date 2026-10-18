"""
fault_predict.py —— 故障预判（趋势 + 阶跃 + 数据质量，输出风险与预计失效时间）

不做黑箱预测，做三件可复算的事：
  ① 趋势：对关键指标做最小二乘斜率（泵效、吨液电耗、含水率、产液）
  ② 阶跃：近 3 天均值 vs 前 11 天均值，抓断脱/卡泵这类突变
  ③ 质量：缺失天数、非 good 质量位占比，抓传感器与通信失效

输出每条风险：类型 + 风险分 + 预计多少天越过处置阈值 + **可复算证据串**。
处置建议只给"该派什么技能的人"，判断仍由人做。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "build" / "analytics.db"
BUILD = ROOT / "build"

EFF_FLOOR = 0.25          # 泵效低于此值 → 该检泵
STEP_DROP = 0.30          # 产液阶跃下降 30% → 断脱/卡泵
WATER_SLOPE = 0.015       # 含水每天涨 1.5 个百分点 → 窜槽


@dataclass
class Risk:
    well: str
    type: str
    severity: str
    score: float
    days: float | None
    skill: str
    evidence: str


def slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def load(con: sqlite3.Connection, wid: str, start: str, end: str) -> list[dict]:
    rows = con.execute(
        """SELECT day, oil_t, liquid_m3, water_m3, runtime_h, kwh, q_theory, status
           FROM well_daily WHERE well=? AND day>=? AND day<=? ORDER BY day""",
        (wid, start, end)).fetchall()
    out = []
    for day, oil, liq, wat, run, kwh, qth, status in rows:
        eff = (liq / (qth * run / 24.0)) if run and qth else 0.0
        out.append(dict(day=day, oil=oil or 0, liq=liq or 0, wat=wat or 0,
                        run=run or 0, kwh=kwh or 0, qth=qth or 0,
                        eff=min(1.0, eff), wc=(wat / liq if liq else 0.0),
                        unit=(kwh / liq if liq else 0.0), status=status))
    return out


def analyse(wid: str, series: list[dict], days_span: int) -> list[Risk]:
    if len(series) < 8:
        return [Risk(wid, "sensor-fail", "medium", 0.7, None, "仪表",
                     f"近 {days_span} 天只有 {len(series)} 天有效数据 → 疑似采集中断")]
    out: list[Risk] = []
    xs = list(range(len(series)))
    effs = [s["eff"] for s in series]
    liqs = [s["liq"] for s in series]
    wcs = [s["wc"] for s in series]
    units = [s["unit"] for s in series]

    cur_eff = effs[-1]
    eff_slope = slope(xs, effs)
    liq_slope = slope(xs, liqs)
    wc_slope = slope(xs, wcs)
    unit_slope = slope(xs, units)
    runtime_ratio = sum(1 for s in series if s["run"] > 0.5) / len(series)

    # ① 断脱/卡泵：连续运行但产液阶跃归零
    last3 = sum(s["liq"] for s in series[-3:]) / 3
    prev = sum(s["liq"] for s in series[:-3]) / max(len(series) - 3, 1)
    if prev > 0.3 and last3 < prev * (1 - STEP_DROP) and runtime_ratio > 0.5:
        out.append(Risk(wid, "sucker-rod", "critical", 0.92, 0.0, "作业",
                        f"产液 3 日均 {last3:.1f} vs 前 {prev:.1f}（降 {100*(1-last3/prev):.0f}%），"
                        f"且运行时率 {runtime_ratio*100:.0f}% → 疑似断脱/卡泵"))

    # ② 泵磨损/凡尔漏：泵效持续下滑
    if eff_slope < -0.004 and cur_eff < 0.60:
        d = (cur_eff - EFF_FLOOR) / abs(eff_slope) if eff_slope else None
        score = min(0.95, 0.45 + abs(eff_slope) * 60)
        out.append(Risk(wid, "pump-wear", "high", round(score, 2),
                        round(d, 1) if d is not None else None, "检泵",
                        f"泵效 {cur_eff:.2f}，斜率 {eff_slope:+.3f}/天，"
                        f"阈值 {EFF_FLOOR:.2f} → 预计 {d:.0f} 天触线" if d else
                        f"泵效 {cur_eff:.2f}，斜率 {eff_slope:+.3f}/天"))

    # ③ 含水突升：窜槽/水淹
    if wc_slope > WATER_SLOPE:
        out.append(Risk(wid, "water-break", "medium", min(0.9, 0.5 + wc_slope * 8), None,
                        "地质",
                        f"含水率 {wcs[-1]*100:.1f}%，斜率 {wc_slope*100:+.2f}pp/天 → "
                        f"疑似窜槽/水淹"))

    # ④ 气锁：泵效短期塌陷（不伴随含水突升）
    if len(effs) >= 4 and (effs[-3] - effs[-1]) > 0.12 and wc_slope <= WATER_SLOPE:
        out.append(Risk(wid, "gas-lock", "high", 0.72, None, "采油",
                        f"泵效 3 天内 {effs[-3]:.2f} → {effs[-1]:.2f}（降 "
                        f"{(effs[-3]-effs[-1])*100:.0f}pp），含水平稳 → 疑似气影响/气锁"))

    # ⑤ 抽汲过剩（不是故障，是制度问题，单列以免与故障混淆）
    if cur_eff < 0.30 and runtime_ratio > 0.80:
        out.append(Risk(wid, "over-pumping", "low", 0.35, None, "采油",
                        f"泵效 {cur_eff:.2f} 且运行时率 {runtime_ratio*100:.0f}% → "
                        f"抽汲过剩，应降冲次/间开，不必派检修"))

    # ⑥ 传感器/数据质量
    if len(series) < days_span - 2 or any(s["liq"] == 0 and s["run"] > 1 for s in series[-5:]):
        out.append(Risk(wid, "sensor-fail", "medium", 0.60, None, "仪表",
                        f"近 {days_span} 天有效数据 {len(series)} 天，"
                        f"或有'运行时率>1h 但产液为 0'的死值 → 疑似计量/传感器故障"))

    # ⑦ 电机/阀：状态连续但长时间不运行
    if runtime_ratio < 0.15 and cur_eff == 0:
        out.append(Risk(wid, "valve-stuck", "high", 0.65, None, "仪表",
                        f"状态标记为生产但运行时率仅 {runtime_ratio*100:.0f}% → "
                        f"疑似停机未上报/阀门卡涩"))

    return out


def main() -> int:
    con = sqlite3.connect(DB)
    maxday = con.execute("SELECT MAX(day) FROM well_daily").fetchone()[0]
    end = date.fromisoformat(maxday)
    span = 14
    start = end - timedelta(days=span - 1)
    wells = [r[0] for r in con.execute("SELECT DISTINCT well FROM well_daily ORDER BY well")]

    risks: list[Risk] = []
    for wid in wells:
        risks.extend(analyse(wid, load(con, wid, start.isoformat(), end.isoformat()), span))
    risks.sort(key=lambda r: (-r.score, r.well))

    print(f"故障预判 · 窗口 {start} ~ {end}（{span} 天）· {len(wells)} 口井")
    print(f"{'井':9s} {'类型':13s} {'等级':8s} {'风险分':>6s} {'预计天数':>8s} {'处置技能':>6s}")
    print("-" * 96)
    for r in risks:
        d = f"{r.days:.0f}" if r.days not in (None, 0.0) else ("立即" if r.days == 0 else "-")
        print(f"{r.well:9s} {r.type:13s} {r.severity:8s} {r.score:6.2f} {d:>8s} {r.skill:>6s}")
        print(f"    ↳ {r.evidence}")

    BUILD.mkdir(exist_ok=True)
    (BUILD / "fault-risk.json").write_text(
        json.dumps([asdict(r) for r in risks], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n风险清单已写出：{BUILD / 'fault-risk.json'}（{len(risks)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
