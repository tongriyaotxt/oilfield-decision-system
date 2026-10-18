"""
build_analytics_db.py —— 造一个"像油田"的分析库（SQLite）

不是为了仿真生产，是为了给分析 Agent 提供**真实结构的数据**：
  · 24 口井 / 4 区块 / 3 采油厂 / 3 种举升方式 / 3 种井状态
  · 90 天单井日数据：产油、产液、产水、运行时长、耗电、注碳
  · 刻意埋进分析里最常见的四个坑：
      ① 躺井（runtime=0，吨液电耗分母为 0）
      ② 间开井（时率 10%~30%，直接算"平均日产油"会把水平拉低）
      ③ 含水率缓慢上升（趋势题必须按井先出序列）
      ④ 注碳只发生在部分井（吨碳换油分母为 0）
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DB = BUILD / "analytics.db"

DAYS = 90
END = date(2026, 9, 30)

BLOCKS = {
    "扶余": ("第一采油厂", 8),
    "新木": ("第一采油厂", 6),
    "新立": ("第二采油厂", 6),
    "红岗": ("第三采油厂", 4),
}
LIFTS = ("抽油机", "抽油机", "抽油机", "电泵", "螺杆泵")


def main() -> int:
    BUILD.mkdir(exist_ok=True)
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""CREATE TABLE well_meta (
        well TEXT PRIMARY KEY, block TEXT, plant TEXT, lift TEXT, trait TEXT,
        q_theory REAL, pump_depth REAL, commission_date TEXT)""")
    cur.execute("""CREATE TABLE well_daily (
        day TEXT, well TEXT, block TEXT, plant TEXT, lift TEXT, status TEXT,
        oil_t REAL, liquid_m3 REAL, water_m3 REAL, runtime_h REAL, kwh REAL,
        co2_inj_t REAL, q_theory REAL)""")

    rnd = random.Random(20261001)
    wells = []
    for block, (plant, n) in BLOCKS.items():
        for i in range(1, n + 1):
            wid = f"{block}-{i:02d}"
            lift = rnd.choice(LIFTS)
            # 供液能力悬殊：低渗低产井占多数
            base = rnd.choice([1.5, 2.5, 3.5, 5.0, 8.0, 12.0, 22.0])
            wc0 = rnd.uniform(0.55, 0.93)
            status = rnd.choices(["连续", "间开", "躺井"], weights=[0.55, 0.35, 0.10])[0]
            # 约 1/3 的井泵型与供液匹配（泵效 70%+，属"不该间开"的井）
            qth = round(base * (rnd.uniform(1.0, 1.4) if rnd.random() < 0.35
                                else rnd.uniform(3.0, 8.0)), 1)
            wells.append(dict(well=wid, block=block, plant=plant, lift=lift,
                              base=base, wc0=wc0, status=status, qth=qth,
                              depth=rnd.choice([900, 1100, 1250, 1400, 1600]),
                              trait=("稠油" if (block == "红岗" and rnd.random() < 0.5) else "常规"),
                              ccus=(block == "红岗" and rnd.random() < 0.6)))
            cur.execute("INSERT INTO well_meta VALUES (?,?,?,?,?,?,?,?)",
                        (wid, block, plant, lift, wells[-1]["trait"], qth,
                         wells[-1]["depth"], "2015-06-01"))

    for d in range(DAYS):
        day = END - timedelta(days=DAYS - 1 - d)
        for w in wells:
            status = w["status"]
            # 躺井偶尔复产/复发，制造真实波动
            if status == "躺井" and rnd.random() < 0.03:
                status = "连续"
            runtime = {"连续": 24.0, "间开": rnd.uniform(2.0, 7.0), "躺井": 0.0}[status]
            if runtime > 0:
                liquid = w["base"] * runtime / 24 * rnd.uniform(0.85, 1.15)
            else:
                liquid = 0.0
            wc = min(0.985, w["wc0"] + d * rnd.uniform(0.0002, 0.0016))
            water = liquid * wc
            oil = (liquid - water) * 0.85
            kwh = 22 * 0.8 * runtime * rnd.uniform(0.92, 1.08)
            co2 = round(oil * rnd.uniform(1.5, 3.0), 2) if (w["ccus"] and runtime > 0) else 0.0
            cur.execute("INSERT INTO well_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (day.isoformat(), w["well"], w["block"], w["plant"], w["lift"],
                         status, round(oil, 2), round(liquid, 2), round(water, 2),
                         round(runtime, 2), round(kwh, 2), co2, w["qth"]))

    con.commit()
    n_rows = cur.execute("SELECT COUNT(*) FROM well_daily").fetchone()[0]
    rng = cur.execute("SELECT MIN(day), MAX(day) FROM well_daily").fetchone()
    oil = cur.execute("SELECT ROUND(SUM(oil_t),1) FROM well_daily").fetchone()[0]
    con.close()
    print(f"分析库已生成: {DB}")
    print(f"  井 {len(wells)} 口 | 日数据 {n_rows} 行 | 区间 {rng[0]} ~ {rng[1]}")
    print(f"  区间产油合计 {oil} t | 埋坑：躺井(runtime=0) / 间开井 / 含水上升 / 注碳仅部分井")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
