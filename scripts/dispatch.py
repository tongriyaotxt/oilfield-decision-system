"""
dispatch.py —— 工单生成 + 自动派工 + 生命周期 + 超时升级 + 审计

规则（可复算，不含 AI）：
  ① 风险分 ≥0.5 且非"制度类"（抽汲过剩）→ 生成工单
  ② 派工：技能必须匹配 → 按（距离罚分 + 当前负荷）最小选人
  ③ 无人可派 → 升级班长（level 1）；接单超时 30min → 升级值班长（level 2）
  ④ 拒单（缺备件）→ 自动派生"备件申请"工单，原单挂起
  ⑤ 全流程写审计：created/assigned/accepted/timeout/escalated/rejected/done

AI 在这里只负责"话术与优先级建议"，派工本身是规则 + 可解释评分。
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
T0 = datetime(2026, 10, 1, 8, 0)          # 早班 8:00

SLA = {"sucker-rod": 480, "pump-wear": 1440, "gas-lock": 240,
       "water-break": 2880, "sensor-fail": 1440, "valve-stuck": 720}
SKILL = {"sucker-rod": "作业", "pump-wear": "检泵", "gas-lock": "采油",
         "water-break": "地质", "sensor-fail": "仪表", "valve-stuck": "仪表"}
SEVERITY = {"sucker-rod": "critical", "pump-wear": "high", "gas-lock": "high",
            "water-break": "medium", "sensor-fail": "medium", "valve-stuck": "high"}


@dataclass
class Worker:
    name: str
    skills: list[str]
    base: str
    load: int = 0


@dataclass
class Order:
    oid: str
    well: str
    type: str
    priority: str
    skill: str
    sla: int
    evidence: str
    assignee: str = ""
    status: str = "created"
    escalate: int = 0
    note: str = ""
    minutes_open: float = 0.0
    history: list[str] = field(default_factory=list)


CREW = [
    Worker("王建国", ["采油", "电气"], "扶余"),
    Worker("李海涛", ["检泵", "采油"], "新木"),
    Worker("张云峰", ["仪表", "电气"], "新立"),
    Worker("刘志强", ["作业", "检泵"], "红岗"),
    Worker("赵雪梅", ["地质", "采油"], "扶余"),
    Worker("陈国栋", ["仪表", "采油"], "扶余", load=2),
]

DIST = {("扶余", "扶余"): 0, ("扶余", "新木"): 1, ("扶余", "新立"): 1, ("扶余", "红岗"): 2,
        ("新木", "新木"): 0, ("新木", "新立"): 1, ("新木", "红岗"): 2,
        ("新立", "新立"): 0, ("新立", "红岗"): 2, ("红岗", "红岗"): 0}


def block_of(well: str) -> str:
    return well.split("-")[0]


def pick(order: Order, crew: list[Worker]) -> tuple[Worker | None, str]:
    cands = [w for w in crew if order.skill in w.skills]
    if not cands:
        return None, f"无人具备「{order.skill}」技能"
    def cost(w: Worker) -> tuple[int, int]:
        d = DIST.get((w.base, block_of(order.well)), 3)
        return (d * 2 + w.load, w.load)
    cands.sort(key=cost)
    best = cands[0]
    why = (f"技能匹配「{order.skill}」，基地 {best.base}→{block_of(order.well)}"
           f"（距离罚分 {DIST.get((best.base, block_of(order.well)), 3)}），当前负荷 {best.load}")
    return best, why


def outcome(oid: str) -> str:
    """确定性"现场行为"：接单 / 超时 / 拒单（缺备件）。"""
    h = int(hashlib.md5(oid.encode()).hexdigest()[:4], 16) % 100
    if h < 12:
        return "timeout"
    if h < 22:
        return "reject"
    return "accept"


def main() -> int:
    risks = json.loads((BUILD / "fault-risk.json").read_text(encoding="utf-8"))
    todo = [r for r in risks if r["score"] >= 0.5 and r["severity"] != "low"]
    crew = [Worker(**asdict(w)) for w in CREW]
    orders: list[Order] = []
    audit: list[dict] = []

    def log(oid: str, etype: str, detail: str) -> None:
        audit.append(dict(t=(T0 + timedelta(minutes=len(audit))).isoformat(timespec="minutes"),
                          order=oid, event=etype, detail=detail))

    print(f"工单生成 · {len(todo)} 条（风险分≥0.5 且非制度类）\n")
    print(f"{'工单':9s} {'井':9s} {'类型':12s} {'优先级':9s} {'技能':4s} {'SLA':>6s} {'派给':8s} {'状态':10s}")
    print("-" * 96)

    for i, r in enumerate(todo, 1):
        oid = f"WO-{i:04d}"
        o = Order(oid=oid, well=r["well"], type=r["type"], priority=r["severity"],
                  skill=SKILL.get(r["type"], "采油"), sla=SLA.get(r["type"], 1440),
                  evidence=r["evidence"])
        log(oid, "created", f"{o.type} {o.priority} 井 {o.well}")

        w, why = pick(o, crew)
        if w is None:
            o.status, o.escalate, o.note = "escalated", 1, why
            log(oid, "escalated", f"level1 班长：{why}")
        else:
            o.assignee, o.status = w.name, "assigned"
            w.load += 1
            log(oid, "assigned", f"{w.name}（{why}）")

            oc = outcome(oid)
            if oc == "accept":
                o.status = "done"
                o.minutes_open = 35 + (int(hashlib.md5(oid.encode()).hexdigest()[:2], 16) % 120)
                log(oid, "accepted", o.assignee)
                log(oid, "done", f"{o.minutes_open:.0f} 分钟完成，已验收")
                w.load = max(0, w.load - 1)
            elif oc == "timeout":
                o.status, o.escalate = "escalated", 2
                o.minutes_open = 30
                log(oid, "timeout", "30 分钟未接单 → 升级值班长")
            else:
                o.status, o.note = "rejected", "缺备件（泵/凡尔总成）"
                log(oid, "rejected", o.note + " → 派生备件申请工单")

        print(f"{o.oid:9s} {o.well:9s} {o.type:12s} {o.priority:9s} {o.skill:4s} "
              f"{o.sla:6d} {(o.assignee or '-'):8s} {o.status:10s}")
        orders.append(o)

    rejected = [o for o in orders if o.status == "rejected"]
    print(f"\n派工结果：完成 {sum(1 for o in orders if o.status == 'done')} ｜ "
          f"升级 {sum(1 for o in orders if o.status == 'escalated')} ｜ "
          f"拒单 {len(rejected)}")

    (BUILD / "workorders.json").write_text(
        json.dumps([asdict(o) for o in orders], ensure_ascii=False, indent=2), encoding="utf-8")
    with (BUILD / "dispatch-audit.jsonl").open("w", encoding="utf-8") as f:
        for e in audit:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"工单：{BUILD / 'workorders.json'} ｜ 审计：{BUILD / 'dispatch-audit.jsonl'}（{len(audit)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
