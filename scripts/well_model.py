"""
well_model.py —— 单井抽汲/恢复的极简物理模型

只保留做"开/关"决策必需的几条守恒关系，避免把仿真写成油藏数值模拟：

1) 泵的理论排量      Qth = 1440 · (π/4·D²) · S · n        [m³/d]
2) 井筒物质守恒      A · dL/dt = 产液率 − 供液率
   A  = 油套环形空间截面积 [m²]，L = 动液面深度 [m]（从井口往下量）
   沉没度 H = 泵挂深度 − 动液面深度，必须 ≥ 0（泵不能被"露出"）
3) 泵的抽汲能力上限  Qth（吸入条件满足时），吸入不足时产液率退化到供液率 qi
4) 关井恢复          dL/dt = −qi/A  →  沉没度按 qi/A 上升
   开井排液          dL/dt = +(q抽 − qi)/A → 沉没度按 (q抽−qi)/A 下降

由 2) 可以直接得到一个非常关键的结论（后面决策引擎就用它）：

    维持沉没度在窗口内所需的开井时率  duty = qi / q抽 = r / (r + f)

   r = 关井时沉没度上升速率 [m/h]，f = 开井时沉没度下降速率 [m/h]。
   两者都能从遥测数据里直接回归出来，**不需要知道地层参数**。
   这就是"仅用采集数据决定开关"的物理依据。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class WellSpec:
    wid: str
    pump_depth: float          # 泵挂深度 m
    pump_dia: float            # 泵径 mm
    stroke: float              # 冲程 m
    spm: float                 # 冲次 1/min
    inflow: float              # 供液能力 qi m³/d
    annular_area: float        # 油套环空截面积 m²
    water_cut: float           # 含水率 0~1
    traits: tuple[str, ...] = ()
    motor_kw: float = 22.0
    load_factor: float = 0.80  # 电机平均负载系数（用于电耗估算）

    @property
    def q_theory(self) -> float:
        plunger = math.pi / 4 * (self.pump_dia / 1000) ** 2
        return 1440.0 * plunger * self.stroke * self.spm

    @property
    def is_faulted(self) -> bool:
        return "fault" in self.traits

    @property
    def is_heavy_oil(self) -> bool:
        return "heavy_oil" in self.traits


@dataclass
class WellState:
    level: float = 0.0          # 动液面深度 m
    liquid_m3: float = 0.0
    oil_m3: float = 0.0
    kwh: float = 0.0
    run_h: float = 0.0
    starts: int = 0
    last_running: bool = False
    prod_now: float = 0.0       # m³/d 瞬时

    def submergence(self, spec: WellSpec) -> float:
        return max(0.0, spec.pump_depth - self.level)


def init_state(spec: WellSpec, start_submergence: float = 60.0) -> WellState:
    return WellState(level=max(0.0, spec.pump_depth - start_submergence))


def step(spec: WellSpec, st: WellState, running: bool, dt_h: float) -> None:
    """推进一步。dt_h 单位小时；产液/电耗按小时累积。"""
    sub = st.submergence(spec)

    if spec.is_faulted:
        running = False

    if running:
        # 吸入条件满足 → 满排量；沉没度过低 → 泵充满不足，产液退化为供液率
        prod_rate = spec.q_theory if sub > 2.0 else spec.inflow
        st.run_h += dt_h
        if not st.last_running:
            st.starts += 1
            st.kwh += spec.motor_kw * spec.load_factor * 0.1   # 启动冲击（等值 6min 额定）
        st.kwh += spec.motor_kw * spec.load_factor * dt_h
    else:
        prod_rate = 0.0

    # 液面：产液多于供液则液面下降（深度变大），关井则恢复
    d_level = (prod_rate - spec.inflow) / spec.annular_area / 24.0 * dt_h
    st.level = min(spec.pump_depth, max(0.0, st.level + d_level))

    liquid = prod_rate * dt_h / 24.0
    st.liquid_m3 += liquid
    st.oil_m3 += liquid * (1.0 - spec.water_cut)
    st.prod_now = prod_rate
    st.last_running = running


@dataclass
class Schedule:
    """开井/关井节律：on_h 小时开、off_h 小时停，循环。off_h=0 表示连续生产。"""
    on_h: float
    off_h: float

    @property
    def duty(self) -> float:
        total = self.on_h + self.off_h
        return self.on_h / total if total > 0 else 1.0

    def running_at(self, t_h: float) -> bool:
        if self.off_h <= 0:
            return True
        period = self.on_h + self.off_h
        return (t_h % period) < self.on_h


@dataclass
class KPI:
    liquid_m3: float = 0.0
    oil_m3: float = 0.0
    kwh: float = 0.0
    run_h: float = 0.0
    starts: int = 0
    hours: float = 0.0
    eff_sum: float = 0.0
    eff_n: int = 0

    def add(self, spec: WellSpec, st0: WellState, st1: WellState, hours: float) -> None:
        self.liquid_m3 += st1.liquid_m3 - st0.liquid_m3
        self.oil_m3 += st1.oil_m3 - st0.oil_m3
        self.kwh += st1.kwh - st0.kwh
        self.run_h += st1.run_h - st0.run_h
        self.starts += st1.starts - st0.starts
        self.hours += hours

    @property
    def liquid_per_day(self) -> float:
        return self.liquid_m3 / self.hours * 24 if self.hours else 0.0

    @property
    def oil_per_day(self) -> float:
        return self.oil_m3 / self.hours * 24 if self.hours else 0.0

    @property
    def kwh_per_day(self) -> float:
        return self.kwh / self.hours * 24 if self.hours else 0.0

    @property
    def kwh_per_m3(self) -> float:
        return self.kwh / self.liquid_m3 if self.liquid_m3 else 0.0

    @property
    def runtime_ratio(self) -> float:
        return self.run_h / self.hours if self.hours else 0.0

    def __sub__(self, other: "KPI") -> "KPI":
        """剔除暖机期：simulate(1天) 与 simulate(1+N天) 相减，得到稳态 N 天。"""
        return KPI(
            liquid_m3=self.liquid_m3 - other.liquid_m3,
            oil_m3=self.oil_m3 - other.oil_m3,
            kwh=self.kwh - other.kwh,
            run_h=self.run_h - other.run_h,
            starts=self.starts - other.starts,
            hours=self.hours - other.hours,
        )


def simulate(spec: WellSpec, schedule: Schedule, days: float,
             dt_h: float = 10.0 / 60.0, telemetry: list | None = None,
             seed_level: float = 60.0) -> tuple[KPI, WellState]:
    """跑一段时间。telemetry 传入空列表则逐点记录采集数据。"""
    st = init_state(spec, seed_level)
    kpi = KPI()
    steps = int(days * 24 / dt_h)
    for i in range(steps):
        t_h = i * dt_h
        running = schedule.running_at(t_h)
        before = WellState(**{k: getattr(st, k) for k in st.__dataclass_fields__})
        step(spec, st, running, dt_h)
        kpi.add(spec, before, st, dt_h)
        if telemetry is not None:
            telemetry.append((
                spec.wid, round(t_h, 3), int(running),
                round(st.level, 2), round(st.submergence(spec), 2),
                round(st.prod_now, 3), round(st.run_h, 3),
            ))
    return kpi, st
