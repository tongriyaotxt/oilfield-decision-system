"""
well_sim.py —— 现场基线仿真 + 遥测数据落盘

模拟"当前现场做法"：全油田一律执行人工经验的固定间开 开4h / 停8h，故障井停机。
产出 build/telemetry.csv（12 口井 × 14 天 × 10 分钟），这就是决策引擎唯一能看到的东西——
和真实油田一致：**引擎拿不到地层参数，只能看到液面、电流、产液和开停状态**。

井的几何参数从 ontology/well-core.ttl 读取（台账唯一来源），
供液能力 qi 与含水率只存在于仿真器里，绝不外泄给引擎。
"""
from __future__ import annotations

import csv
import random
from pathlib import Path

from rdflib import Graph, Namespace, RDF
from rdflib.namespace import XSD

from well_model import KPI, Schedule, WellSpec, simulate

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
W = Namespace("https://example.org/well#")
WR = Namespace("https://example.org/well/w/")
TR = Namespace("https://example.org/well/trait/")

DAYS = 14.0
DT_H = 10.0 / 60.0
BASELINE = Schedule(on_h=4.0, off_h=8.0)      # 现场一刀切：开 4 停 8

# 只有仿真器知道的地层真值（引擎不可见）
TRUTH = {
    "W01": (3.0, 0.85), "W02": (2.0, 0.90), "W03": (4.0, 0.80), "W04": (1.2, 0.92),
    "W05": (8.0, 0.75), "W06": (2.5, 0.88), "W07": (1.5, 0.93), "W08": (5.0, 0.86),
    "W09": (18.0, 0.60), "W10": (22.0, 0.55), "W11": (0.8, 0.95), "W12": (3.2, 0.87),
}


def load_specs() -> list[WellSpec]:
    g = Graph().parse(ROOT / "ontology" / "well-core.ttl", format="turtle")
    specs: list[WellSpec] = []
    for w in g.subjects(RDF.type, W.OilWell):
        wid = str(w).rsplit("/", 1)[-1]
        qi, cut = TRUTH[wid]
        traits = tuple(str(t).rsplit("/", 1)[-1].lower().replace("faultlock", "fault")
                       .replace("heavyoil", "heavy_oil").replace("sandprone", "sand")
                       for t in g.objects(w, W.hasTrait))
        num = lambda p: float(g.value(w, W[p]))  # noqa: E731
        specs.append(WellSpec(
            wid=wid,
            pump_depth=num("pumpDepth"), pump_dia=num("pumpDiameter"),
            stroke=num("strokeLength"), spm=num("strokeRate"),
            inflow=qi, annular_area=num("annularArea"), water_cut=cut,
            traits=traits,
        ))
    return sorted(specs, key=lambda s: s.wid)


def main() -> int:
    specs = load_specs()
    BUILD.mkdir(exist_ok=True)
    out = BUILD / "telemetry.csv"
    rows: list[tuple] = []
    total = KPI()

    print(f"{'井':5s} {'Q理论':>7s} {'供液':>6s} {'产液':>7s} {'产油':>7s} "
          f"{'电耗':>8s} {'吨液电耗':>8s} {'泵效':>6s} {'开井率':>6s}")
    print("-" * 78)
    for spec in specs:
        tel: list[tuple] = []
        kpi, st = simulate(spec, BASELINE, DAYS, DT_H, telemetry=tel)
        rows.extend(tel)
        eff = kpi.liquid_m3 / (spec.q_theory * kpi.run_h / 24.0) if kpi.run_h else 0.0
        print(f"{spec.wid:5s} {spec.q_theory:7.1f} {spec.inflow:6.1f} {kpi.liquid_per_day:7.2f} "
              f"{kpi.oil_per_day:7.2f} {kpi.kwh_per_day:8.1f} {kpi.kwh_per_m3:8.1f} "
              f"{eff*100:5.0f}% {kpi.runtime_ratio*100:5.0f}%")
        total.liquid_m3 += kpi.liquid_m3; total.oil_m3 += kpi.oil_m3
        total.kwh += kpi.kwh; total.run_h += kpi.run_h
        total.hours = kpi.hours

    print("-" * 78)
    print(f"{'合计':5s} {'':7s} {'':6s} {total.liquid_per_day:7.2f} {total.oil_per_day:7.2f} "
          f"{total.kwh_per_day:8.1f} {total.kwh_per_m3:8.1f}")

    with out.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["well", "t_h", "running", "level_m", "submergence_m",
                     "prod_rate_m3d", "run_h_cum"])
        # 现场数据一定有噪声：液面 ±0.8m（回声仪）、产液 ±3%（计量误差）
        rnd = random.Random(42)
        for wid, t_h, running, level, sub, prod, run_h in rows:
            wr.writerow([
                wid, t_h, running,
                round(level + rnd.uniform(-0.8, 0.8), 2),
                round(max(0.0, sub + rnd.uniform(-0.8, 0.8)), 2),
                round(max(0.0, prod * (1 + rnd.uniform(-0.03, 0.03))), 3),
                run_h,
            ])
    days = DAYS
    print(f"\n遥测已写出: {out}  ({len(rows)} 行, {len(specs)} 口井 × {days:.0f} 天 × 10min)")
    print("特性：基线策略下供液充足井(W09/W10)被误间开，产液被硬砍；"
          "供液不足井则有大量时间在空抽。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
