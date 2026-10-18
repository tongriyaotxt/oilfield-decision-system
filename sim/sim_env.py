"""
sim_env.py —— 模拟生产环境（现场侧）

三件套 + 四个"真实现象"，把之前"不能验证"的东西补上：
  ① Plant     井筒/泵/电机物理模型，并叠加：
              · 产量自然递减（供液能力按小时衰减）
              · 含水率随累产上升
              · 泵磨损（运行越久，充满程度越低）
              · **抽空保护联锁（SIS）**：连续干抽 6h → 强制停机并锁定，远程指令一律拒绝
  ② Historian 时序库（SQLite），带真实脏数据：漂移/缺口/死值/时钟偏移
  ③ RTU       执行器：阀卡在运行位、通信超时、SIS 锁定拒动

边界：SIS 是独立回路，Agent 只能"看"到它的结果，不能覆盖。这是本文件刻意保留的硬约束。
"""
from __future__ import annotations

import random
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from well_model import Schedule, WellSpec, init_state, step  # noqa: E402

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
DT_H = 10.0 / 60.0
STEPS_PER_HOUR = 6

DECLINE_PER_H = 0.99994         # 供液能力每小时衰减（≈ 每天 0.14%，30 天约 4%）
WC_RISE_PER_DAY = 0.0008        # 含水率日上升
WEAR_PER_100H = 0.998           # 每运行 100h 泵充满程度下降 0.2%
DRY_HOURS_TO_TRIP = 6           # 连续干抽 6h → SIS 跳闸


# ═══════════════════════════ ② 时序库 ═══════════════════════════
class Historian:
    DDL = """
    CREATE TABLE telemetry (
        ts TEXT, well TEXT,
        liquid_m3d REAL, water_cut REAL, oil_t REAL,
        runtime_h REAL, kwh REAL, submergence REAL,
        quality TEXT, ts_ingest TEXT
    );
    CREATE INDEX ix_t ON telemetry (well, ts);
    """

    def __init__(self, path: Path):
        if path.exists():
            path.unlink()
        self.con = sqlite3.connect(path)
        self.con.executescript(self.DDL)

    def ingest(self, rows: list[tuple]) -> None:
        self.con.executemany("INSERT INTO telemetry VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        self.con.commit()

    def fetch(self, well: str, start: str, end: str, good_only: bool = True) -> list[tuple]:
        q = ("SELECT ts,liquid_m3d,water_cut,oil_t,runtime_h,kwh,submergence,quality "
             "FROM telemetry WHERE well=? AND ts>=? AND ts<?")
        if good_only:
            q += " AND quality='good'"
        return self.con.execute(q + " ORDER BY ts", (well, start, end)).fetchall()

    def coverage(self, well: str, start: str, end: str, hours: int) -> float:
        n = self.con.execute(
            "SELECT COUNT(*) FROM telemetry WHERE well=? AND ts>=? AND ts<? AND quality='good'",
            (well, start, end)).fetchone()[0]
        return n / max(hours, 1)

    def anomalies(self, well: str) -> dict[str, int]:
        return {q: n for q, n in self.con.execute(
            "SELECT quality, COUNT(*) FROM telemetry WHERE well=? GROUP BY quality", (well,)
        ).fetchall() if q != "good"}


# ═══════════════════════════ ① 现场 ═══════════════════════════
@dataclass
class SimWell:
    spec: WellSpec
    state: object
    schedule: Schedule
    trait: str = "常规"
    status: str = "连续"
    wc0: float = 0.8
    wear: float = 1.0
    dry_hours: int = 0
    sis_locked: bool = False
    valve_stuck_open: bool = False
    total_run_h: float = 0.0


class Plant:
    def __init__(self, wells: list[SimWell]):
        self.wells = {w.spec.wid: w for w in wells}
        self.sis_events: list[dict] = []

    def run_hour(self, hour_index: int, base: datetime) -> list[tuple]:
        out = []
        for wid, w in self.wells.items():
            running = (w.schedule.running_at(hour_index) or w.valve_stuck_open) \
                and not w.sis_locked
            liq0, oil0, kwh0 = w.state.liquid_m3, w.state.oil_m3, w.state.kwh
            run_h = 0.0
            for _ in range(STEPS_PER_HOUR):
                step(w.spec, w.state, running, DT_H)
                if running:
                    run_h += DT_H
            d_liq = w.state.liquid_m3 - liq0
            d_oil = w.state.oil_m3 - oil0
            d_kwh = w.state.kwh - kwh0

            # 泵磨损：实际举升按 wear 折减（状态量与上报量同时折减，保持账实一致）
            if w.wear < 0.999:
                lost_liq = d_liq * (1 - w.wear)
                lost_oil = d_oil * (1 - w.wear)
                w.state.liquid_m3 -= lost_liq
                w.state.oil_m3 -= lost_oil
                d_liq, d_oil = d_liq * w.wear, d_oil * w.wear

            ts = (base + timedelta(hours=hour_index)).isoformat()
            out.append((ts, wid, round(d_liq * 24, 3), round(w.spec.water_cut, 4),
                        round(d_oil, 4), round(run_h, 3), round(d_kwh, 3),
                        round(w.state.submergence(w.spec), 2), "good",
                        datetime.now(timezone.utc).isoformat()))

            # ── 演化：递减 / 含水 / 磨损 / 抽空保护 ──
            w.total_run_h += run_h
            w.wear = max(0.75, w.wear * (1 - (1 - WEAR_PER_100H) * run_h / 100.0))
            w.spec.inflow = max(0.05, w.spec.inflow * DECLINE_PER_H)
            w.spec.water_cut = min(0.98, w.spec.water_cut + WC_RISE_PER_DAY / 24.0)

            dry = (running and w.state.submergence(w.spec) <= 0.5
                   and (d_liq * 24) < 0.25 * w.spec.q_theory)
            w.dry_hours = w.dry_hours + 1 if dry else 0
            if w.dry_hours >= DRY_HOURS_TO_TRIP and not w.sis_locked:
                w.sis_locked = True
                w.valve_stuck_open = False
                w.schedule = Schedule(0.0, 24.0)
                self.sis_events.append(dict(well=wid, hour=hour_index,
                                            reason="抽空保护：连续干抽 ≥6h"))
        return out

    def apply(self, wid: str, sched: Schedule) -> None:
        self.wells[wid].schedule = sched


# ═══════════════════════════ ③ 执行器 ═══════════════════════════
class RTU:
    def __init__(self, plant: Plant, stuck_open: set[str], timeout_wells: set[str],
                 valve_seq: float = 3.0):
        self.plant = plant
        self.stuck_open = stuck_open
        self.timeout = timeout_wells
        self.valve_seq_s = valve_seq

    def execute(self, cmd, attempt: int = 0):        # noqa: ANN001
        from control_api import Receipt
        w = self.plant.wells[cmd.well]
        lat = int(self.valve_seq_s * 1000 / 6)
        if w.sis_locked:                              # 联锁锁定：远程一律拒绝
            return Receipt(cmd.request_id, "Rejected", 200, attempt,
                           "SIS 联锁锁定，禁止远程操作（需现场复位）")
        if cmd.well in self.timeout and attempt == 0:
            return Receipt(cmd.request_id, "Failed", lat, 0, "通信超时（可重试）")
        if cmd.well in self.stuck_open:
            return Receipt(cmd.request_id, "Failed", 5000, attempt,
                           "阀卡在运行位：指令未生效，井仍在运行")
        self.plant.apply(cmd.well, Schedule(cmd.on_h, cmd.off_h) if cmd.off_h > 0
                         else Schedule(24.0, 0.0))
        return Receipt(cmd.request_id, "Done", lat, attempt, "")


# ═══════════════════════════ 井组 ═══════════════════════════
def build_plant(seed: int = 11) -> Plant:
    rnd = random.Random(seed)
    fleet = [
        ("A-01", 3.0, 15.0, "常规"), ("A-02", 2.0, 14.0, "常规"),
        ("A-03", 8.0, 3.2, "常规"), ("A-04", 1.2, 16.0, "常规"),
        ("B-01", 18.0, 1.1, "常规"), ("B-02", 22.0, 1.2, "常规"),
        ("B-03", 4.0, 12.0, "常规"), ("C-01", 1.5, 14.0, "稠油"),
        ("C-02", 5.0, 3.0, "常规"), ("C-03", 0.8, 18.0, "常规"),
    ]
    wells = []
    for i, (wid, qi, qmult, trait) in enumerate(fleet):
        plunger = 3.141592653589793 / 4 * (0.044 ** 2)
        qth = qi * qmult
        spm = max(1.0, qth / (1440 * plunger * 3.0))
        spec = WellSpec(wid=wid, pump_depth=1200.0, pump_dia=44.0, stroke=3.0, spm=spm,
                        inflow=qi, annular_area=0.012 + 0.001 * (i % 3),
                        water_cut=0.80 + 0.01 * i, traits=(),
                        motor_kw=22.0, load_factor=0.68 + 0.02 * (i % 5))
        wells.append(SimWell(spec=spec, state=init_state(spec, 60.0),
                             schedule=Schedule(4.0, 8.0), trait=trait,
                             wc0=spec.water_cut))
    return Plant(wells)


# ═══════════════════════════ 脏数据 ═══════════════════════════
def pollute(rows: list[tuple], hour: int) -> list[tuple]:
    out = []
    for ts, wid, liq, wc, oil, run_h, kwh, sub, quality, ing in rows:
        if wid == "A-03" and 48 <= hour < 96:
            sub, quality = round(sub * 1.3, 2), "drift"
        if wid == "A-04" and 40 <= hour < 52:
            continue                                   # 数据缺口 12h
        if wid == "B-03" and 60 <= hour < 72:
            liq, quality = 0.0, "stuck"
        if wid == "C-01":
            ts = (datetime.fromisoformat(ts) + timedelta(minutes=7)).isoformat()
        out.append((ts, wid, liq, wc, oil, run_h, kwh, sub, quality, ing))
    return out
