"""
control_api.py —— 自主控制接口层（把决策变成可下发的命令）

这一层解决的不是"算法"，是"落地"：
  ① 数据源接口    DataSource      换 API/CSV/数据湖，只换适配器，决策不动
  ② 执行器接口    Actuator        生产环境接 OPC UA / MQTT；本仓库用内存模拟器（含故障注入）
  ③ 命令总线      CommandBus      幂等 request_id、deadline、重试、回执校验、失败回滚
  ④ 授权分级      Authorization   A 自动 / B 人工确认 / C 仅建议
  ⑤ 灰度          Canary          按井号哈希稳定抽样，先小范围跑
  ⑥ 审计          AuditLog        JSONL + RDF，命令/回执/审批/回滚全留痕
  ⑦ 指标注册      MetricsRegistry 未建模的指标**拒绝**，不许猜
  ⑧ 经济评价      Economics       经济极限由油价/成本算出，不再硬编码 0.1 t/d
  ⑨ 措施建议      Recommender     低于经济极限的井：给排序后的措施方案与预期区间（不承诺产量）

设计边界：本层不进入秒级实时环。命令带 deadline 与 expected_state，由边缘执行器兜住秒级动作，
本层负责"下达—回执—校验—回滚—留痕"。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from rdflib import Graph, Literal, Namespace, RDF
from rdflib.namespace import XSD

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_agent as da

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "build" / "analytics.db"
BUILD = ROOT / "build"
AO = da.AO
CTL = Namespace("https://example.org/ctl#")
AUTH = Namespace("https://example.org/ctl/auth/")
ST = Namespace("https://example.org/ctl/status/")
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


# ═══════════════════════════ ① 数据源接口 ═══════════════════════════
class DataSource(Protocol):
    def well_features(self, start: str, end: str) -> list[dict]: ...
    def water_cut(self, well: str, start: str, end: str) -> float: ...


class SQLiteSource:
    """生产上换成数仓/API/湖：只要实现这两个方法，上层不用改。"""

    def __init__(self, path: Path):
        self.con = sqlite3.connect(path)

    def well_features(self, start: str, end: str) -> list[dict]:
        return da.features(self.con, start, end)

    def water_cut(self, well: str, start: str, end: str) -> float:
        row = self.con.execute(
            """SELECT SUM(water_m3)/NULLIF(SUM(liquid_m3),0) FROM well_daily
               WHERE well=? AND day BETWEEN ? AND ?""", (well, start, end)).fetchone()
        return float(row[0] or 0.85)


# ═══════════════════════════ ② 执行器接口 ═══════════════════════════
class Actuator(Protocol):
    def execute(self, cmd: "Command") -> "Receipt": ...


@dataclass
class Command:
    request_id: str
    well: str
    action: str                       # ShutIn / Start / Hold / Stop / Escalate
    on_h: float = 0.0
    off_h: float = 0.0
    duty: float = 1.0
    deadline_s: int = 30
    expected_state: str = ""
    auth: str = "A"
    issued_by: str = "agent"


@dataclass
class Receipt:
    request_id: str
    status: str                       # Done / Rejected / Failed / AwaitingHuman / RolledBack
    latency_ms: int = 0
    retries: int = 0
    error: str = ""

class SimActuator:
    """模拟 RTU：确定性故障注入（按井+动作哈希），便于复现与回归。"""

    def __init__(self, timeout_rate: float = 0.07, fail_rate: float = 0.04,
                 reject_rate: float = 0.04):
        self.timeout_rate, self.fail_rate, self.reject_rate = timeout_rate, fail_rate, reject_rate

    def _bucket(self, cmd: Command) -> float:
        h = hashlib.md5(f"{cmd.well}|{cmd.action}".encode()).hexdigest()
        return int(h[:6], 16) % 100 / 100.0

    def execute(self, cmd: Command, attempt: int = 0) -> Receipt:
        b = self._bucket(cmd)
        latency = 40 + int(b * 100)
        if b < self.reject_rate:                      # 参数/设备拒绝，不可重试
            return Receipt(cmd.request_id, "Rejected", latency, 0, "设备拒绝：参数越界或阀位不可达")
        if b < self.reject_rate + self.fail_rate:     # 永久失败 → 需回滚
            return Receipt(cmd.request_id, "Failed", attempt, latency, "执行超时未到位")
        if b < self.reject_rate + self.fail_rate + self.timeout_rate:
            if attempt == 0:                          # 首次超时（可重试）
                return Receipt(cmd.request_id, "Failed", latency, 0, "首次超时（可重试）")
            return Receipt(cmd.request_id, "Done", latency, attempt, "")   # 重试成功
        return Receipt(cmd.request_id, "Done", latency, attempt, "")

    @staticmethod
    def verify(cmd: Command, receipt: Receipt) -> bool:
        """回执校验：不仅看指令成功，还要比对 expected_state（防'指令成功、设备没动'）。"""
        return receipt.status == "Done" and bool(cmd.expected_state)


# ═══════════════════════════ ③ 命令总线 ═══════════════════════════
class CommandBus:
    def __init__(self, actuator: Actuator, audit: "AuditLog",
                 max_retries: int = 2, rollback: bool = True):
        self.actuator = actuator
        self.audit = audit
        self.max_retries = max_retries
        self.rollback = rollback
        self.sent: dict[str, Receipt] = {}          # 幂等表

    def dispatch(self, cmd: Command) -> Receipt:
        if cmd.request_id in self.sent:              # 幂等：重复请求直接回放回执
            self.audit.log("command.idempotent_replay", cmd.request_id, cmd.well, cmd.action)
            return self.sent[cmd.request_id]

        if cmd.auth == "B":
            r = Receipt(cmd.request_id, "AwaitingHuman")
            self.sent[cmd.request_id] = r
            self.audit.log("command.awaiting_human", cmd.request_id, cmd.well, cmd.action)
            return r
        if cmd.auth == "C":
            r = Receipt(cmd.request_id, "AwaitingHuman")
            self.sent[cmd.request_id] = r
            self.audit.log("command.suggestion_only", cmd.request_id, cmd.well, cmd.action)
            return r

        receipt = self.actuator.execute(cmd)
        attempts = 0
        while receipt.status == "Failed" and attempts < self.max_retries \
                and "可重试" in receipt.error:
            attempts += 1
            self.audit.log("command.retry", cmd.request_id, cmd.well, f"第{attempts}次重试")
            receipt = self.actuator.execute(cmd, attempt=attempts)
        receipt.retries = max(receipt.retries, attempts)

        if receipt.status == "Done" and not SimActuator.verify(cmd, receipt):
            receipt.status = "Failed"
            receipt.error = "回执校验失败：expected_state 未达到"

        if receipt.status == "Failed" and self.rollback:
            self.audit.log("command.rollback", cmd.request_id, cmd.well,
                           f"失败({receipt.error}) → 恢复原制度")
            receipt.status = "RolledBack"

        self.sent[cmd.request_id] = receipt
        self.audit.log("command.result", cmd.request_id, cmd.well,
                       f"{cmd.action} → {receipt.status} ({receipt.latency_ms}ms)")
        return receipt

    def confirm(self, cmd: Command, operator: str) -> Receipt:
        """B/C 类命令的人工确认（生产上是审批接口，带工号与双人签）。"""
        self.audit.log("command.approved", cmd.request_id, cmd.well, f"by {operator}")
        self.sent.pop(cmd.request_id, None)          # 解除幂等缓存，允许审批后真正执行
        return self.dispatch(Command(**{**cmd.__dict__, "auth": "A"}))


# ═══════════════════════════ ④ 授权分级 ═══════════════════════════
class Authorization:
    LEVEL = {"ShutIn": "A", "Hold": "A", "Escalate": "A", "Start": "B", "Stop": "C"}
    NOTE = {"A": "系统自动（只向安全方向）", "B": "建议 + 人工确认",
            "C": "仅建议，经营决策"}

    @classmethod
    def level(cls, action: str) -> str:
        return cls.LEVEL.get(action, "C")


# ═══════════════════════════ ⑤ 灰度 ═══════════════════════════
class Canary:
    def __init__(self, percent: float = 0.40, salt: str = "2026Q4"):
        self.percent, self.salt = percent, salt

    def inside(self, well: str) -> bool:
        h = hashlib.md5(f"{well}|{self.salt}".encode()).hexdigest()
        return (int(h[:8], 16) % 1000) / 1000.0 < self.percent


# ═══════════════════════════ ⑥ 审计 ═══════════════════════════
class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.events: list[dict] = []

    def log(self, etype: str, rid: str, well: str, detail: str) -> None:
        self.events.append(dict(type=etype, request_id=rid, well=well, detail=detail,
                                at=NOW.isoformat()))

    def flush(self) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ═══════════════════════════ ⑦ 指标注册 ═══════════════════════════
class UnknownMetric(Exception):
    pass


class MetricsRegistry:
    """新指标必须先注册（单位/公式/可加性/时间口径），否则拒绝——不许猜。"""

    REQUIRED = ("unit", "formula", "additive", "grain", "time_sem")

    def __init__(self, known: dict[str, dict]):
        self.known = dict(known)

    def register(self, name: str, spec: dict) -> None:
        missing = [k for k in self.REQUIRED if k not in spec]
        if missing:
            raise ValueError(f"指标 {name} 缺少必填口径：{missing}")
        self.known[name] = spec

    def require(self, name: str) -> dict:
        if name not in self.known:
            raise UnknownMetric(
                f"指标「{name}」未建模。请先注册口径（单位/公式/可加性/时间口径），"
                f"本系统不会用猜测值回答。")
        return self.known[name]


# ═══════════════════════════ ⑧ 经济评价 ═══════════════════════════
@dataclass
class Economics:
    price_usd: float = 60.0
    fx: float = 7.2
    bbl_per_t: float = 7.33
    tax_rate: float = 0.12
    var_cost_per_t: float = 200.0
    fixed_cost_per_well_year: float = 80_000.0

    def revenue_per_t(self) -> float:
        return self.price_usd * self.fx * self.bbl_per_t

    def contribution_per_t(self) -> float:
        return self.revenue_per_t() * (1 - self.tax_rate) - self.var_cost_per_t

    def floor_t_per_day(self) -> float:
        return self.fixed_cost_per_well_year / 365.0 / self.contribution_per_t()


# ═══════════════════════════ ⑨ 措施建议 ═══════════════════════════
class Recommender:
    """解决"产量天花板"这一条：系统不创造供液，但能把措施排好序、给出预期区间与投入。"""

    def recommend(self, rec: dict) -> dict:
        eff, duty, oil = rec["eff"], rec["duty"], rec["oil_pd"]
        if oil < 0.02 and duty < 0.05:
            return dict(plan="长停/复产评估：先做测压与液面恢复，再定复产或核销",
                        uplift=(0.0, 0.3), cost=10, note="复产需作业，按恢复能力评估")
        if eff < 0.20 and duty < 0.30:
            return dict(plan="供液不足为主：优化间开制度 + 评估注水/补能", uplift=(0.0, 0.1),
                        cost=5, note="增产空间小，优先降耗")
        if eff < 0.20 and duty > 0.90:
            return dict(plan="抽汲过剩：降冲次/换小泵", uplift=(0.0, 0.0),
                        cost=15, note=f"预期降耗 30~50%，产液基本不变")
        if 0.20 <= eff < 0.70:
            return dict(plan="泵型不匹配：换泵或调参", uplift=(0.05, 0.2),
                        cost=20, note="泵效提升带动单耗下降")
        if eff >= 0.70 and oil < 0.2:
            return dict(plan="措施增产：酸化解堵 / 补孔 / 压裂（三选一，按邻井类比）",
                        uplift=(0.2, 0.8), cost=(30, 80), note="产量天花板受供液限制，需改造储层")
        return dict(plan="维持", uplift=(0.0, 0.0), cost=0, note="当前制度已合理")


# ═══════════════════════════ 主流程：一轮自主控制 ═══════════════════════════
def main() -> int:
    print("=" * 92)
    print("自主控制接口层 · 一轮完整链路（取数 → 经济评价 → 决策 → 授权 → 灰度 → 下发 → 回执 → 审计）")
    print("=" * 92)

    # ⑦ 指标注册：未建模必须拒绝
    rules, _ = da.load_rules()
    registry = MetricsRegistry({k: v for k, v in da.METRIC_KEY.items()} | {
        "pump_eff": {"unit": "%", "formula": "SUM(liquid)/SUM(qtheory*runtime/24)",
                     "additive": False, "grain": "well-day", "time_sem": "calendar-day"}})
    print("\n[⑦ 指标注册]")
    try:
        registry.require("oil_rate_weekly")
    except UnknownMetric as e:
        print(f"  ✗ 拒绝：{e}")
    print(f"  ✓ 已注册指标：{', '.join(sorted(registry.known))}")

    # ① 数据源
    src = SQLiteSource(DB)
    maxday = src.con.execute("SELECT MAX(day) FROM well_daily").fetchone()[0]
    end = da.date.fromisoformat(maxday)
    start = da.date.fromordinal(end.toordinal() - da.WINDOW_DAYS + 1)
    recs = src.well_features(start.isoformat(), end.isoformat())
    for r in recs:
        r["water_cut"] = src.water_cut(r["well"], start.isoformat(), end.isoformat())
    print(f"\n[① 数据源] {len(recs)} 口井，窗口 {start} ~ {end}")

    # ⑧ 经济评价
    econ = Economics()
    floor = econ.floor_t_per_day()
    print(f"\n[⑧ 经济评价] 油价 ${econ.price_usd:.0f}/bbl、汇率 {econ.fx}、操作成本 "
          f"{econ.fixed_cost_per_well_year/1e4:.0f} 万元/井·年")
    print(f"  边际贡献 {econ.contribution_per_t():.0f} 元/t → 经济极限 "
          f"{floor:.3f} t/d（原规则硬编码 0.1，现在随油价浮动）")
    print("  敏感性：" + " ｜ ".join(
        f"${p:.0f}→{Economics(price_usd=p).floor_t_per_day():.3f} t/d" for p in (40, 60, 80)))

    # 决策 + 授权分级
    decisions = [(rec, da.decide(rec, rules, end.month)) for rec in recs]
    print("\n[③ 决策 + ④ 授权分级]")
    for rec, d in decisions[:3]:
        lvl = Authorization.level(d["action"])
        print(f"  {rec['well']}: {d['action']:8s} → {lvl} 类（{Authorization.NOTE[lvl]}）")

    # ⑤ 灰度 + ③ 命令总线
    audit = AuditLog(BUILD / "control-audit.jsonl")
    bus = CommandBus(SimActuator(), audit, max_retries=2, rollback=True)
    canary = Canary(percent=0.40)
    receipts: list[tuple[Command, Receipt]] = []
    skipped = awaiting = 0
    for rec, d in decisions:
        if not canary.inside(rec["well"]):
            skipped += 1
            continue
        lvl = Authorization.level(d["action"])
        cmd = Command(
            request_id=f"{rec['well']}-{NOW:%Y%m%d}-{d['action']}",
            well=rec["well"], action=d["action"], on_h=d["on"], off_h=d["off"],
            duty=d["duty_target"], deadline_s=30,
            expected_state=f"duty={d['duty_target']:.2f}", auth=lvl,
        )
        r = bus.dispatch(cmd)
        receipts.append((cmd, r))
        await_s = r.status == "AwaitingHuman"
        awaiting += 1 if await_s else 0

    print(f"\n[⑤ 灰度] 40% 井在灰度内：下发 {len(receipts)} 条，灰度外跳过 {skipped} 条")
    print("\n[⑥ 下发 / 回执]")
    stats: dict[str, int] = {}
    for cmd, r in receipts:
        stats[r.status] = stats.get(r.status, 0) + 1
        extra = f" 重试{r.retries}次" if r.retries else ""
        print(f"  {cmd.well:8s} {cmd.action:8s} [{cmd.auth}类] → {r.status:12s} "
              f"{r.latency_ms:4d}ms{extra}"
              + (f" | {r.error}" if r.error else ""))
    print(f"  汇总：{stats}")

    # 人工确认演示（B 类）
    print("\n[④ 人工确认] B 类命令需审批：")
    for cmd, r in receipts:
        if r.status == "AwaitingHuman" and cmd.auth == "B":
            r2 = bus.confirm(cmd, operator="值班长-张工")
            print(f"  {cmd.well} {cmd.action} → 审批后 {r2.status}")
            break

    # ⑨ 措施建议：低于经济极限的井
    print(f"\n[⑨ 措施建议] 低于经济极限（<{floor:.3f} t/d）的井：")
    rec_engine = Recommender()
    below = [r for r in recs if r["oil_pd"] < floor]
    for r in below[:5]:
        adv = rec_engine.recommend(r)
        up = adv["uplift"]
        print(f"  {r['well']:8s} 产油{r['oil_pd']:.2f} t/d 泵效{r['eff']*100:3.0f}% → "
              f"{adv['plan']}｜预期增油 {up[0]:.1f}~{up[1]:.1f} t/d｜投入 {adv['cost']} 万元｜{adv['note']}")
    print(f"  共 {len(below)} 口井需要经营层决策（系统只排序+报价，不自动关停）")

    # 审计落盘 + RDF
    audit.flush()
    g = Graph(); g.bind("ctl", CTL); g.bind("status", ST)
    for cmd, r in receipts:
        node = CTL[f"command/{cmd.request_id}"]
        g.add((node, RDF.type, CTL.Command))
        g.add((node, CTL.requestId, Literal(cmd.request_id)))
        g.add((node, CTL.commandsWell, AO[f"well/{cmd.well}"]))
        g.add((node, CTL.commandAction, da.ACT[cmd.action]))
        g.add((node, CTL.authLevel, AUTH[cmd.auth]))
        g.add((node, CTL.deadlineSeconds, Literal(cmd.deadline_s, datatype=XSD.integer)))
        g.add((node, CTL.expectedState, Literal(cmd.expected_state)))
        rc = CTL[f"receipt/{cmd.request_id}"]
        g.add((rc, RDF.type, CTL.Receipt))
        g.add((rc, CTL.receiptOf, node))
        g.add((rc, CTL.status, ST[r.status]))
        g.add((rc, CTL.latencyMs, Literal(r.latency_ms, datatype=XSD.integer)))
        g.add((rc, CTL.retries, Literal(r.retries, datatype=XSD.integer)))
        if r.error:
            g.add((rc, CTL.errorMessage, Literal(r.error)))
    out = BUILD / "control-report.ttl"
    g.serialize(out, format="turtle")
    print(f"\n[⑥ 审计] {len(audit.events)} 条事件 → {BUILD / 'control-audit.jsonl'}")
    print(f"          回执图 {len(g)} 三元组 → {out}")

    # 结论：哪些"不能"被接口化解决了
    print("\n" + "=" * 92)
    print("原先的'不能'，现在怎么处理：")
    print("  · 不能替代执行机构   → Actuator 接口（生产接 OPC UA/MQTT）+ 回执校验 + 失败回滚")
    print("  · 不能做实时控制     → 命令带 deadline/expected_state，秒级动作交给边缘，本层只管下达与留痕")
    print("  · 不能处理未建模指标 → MetricsRegistry.require 直接拒绝，不用猜测值回答")
    print("  · 不能替经营决策     → 经济极限按油价/成本动态计算 + C 类只给建议与报价")
    print("  · 不能提高产能上限   → Recommender 给措施方案、预期区间与投入，供决策层排序（不承诺产量）")
    print("  · 不能复现真实数据   → DataSource 适配器化，换库只改一个类")
    print("  · 不能保证不跑飞     → 灰度 + 回滚 + 审计三件套")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
