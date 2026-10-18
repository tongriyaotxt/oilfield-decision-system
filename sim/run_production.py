"""
run_production.py —— 模拟生产环境上的三策略对照（30 天）

  ① baseline：现场现行制度（全井 开4h/停8h），系统只观测；
  ② shadow  ：系统决策**只算不下发**，与"人工经验规则"比对一致率（影子运行）；
  ③ agent   ：决策下发 → 门禁 → 命令 → RTU → 回执/回滚 → 审计（真闭环）。

环境侧叠加了四个真实现象：产量递减、含水上升、泵磨损、抽空保护联锁（SIS）。
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

from rdflib import Graph

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "sim"))

import decision_agent as da                                    # noqa: E402
from control_api import (AuditLog, Authorization, Command,      # noqa: E402
                         CommandBus, Economics)
from sim_env import T0, Historian, RTU, build_plant, pollute    # noqa: E402

BUILD = ROOT / "build"
DAYS = 30
HOURS = DAYS * 24
DECISION_EVERY = 12
WARMUP_H = 24


def features(hist, plant, t0, t1):
    out, hours = [], max(1, int((t1 - t0).total_seconds() // 3600))
    for wid, w in plant.wells.items():
        rows = hist.fetch(wid, t0.isoformat(), t1.isoformat(), good_only=True)
        cov = hist.coverage(wid, t0.isoformat(), t1.isoformat(), hours)
        if not rows:
            out.append(dict(well=wid, status=w.status, trait=w.trait, days=0, coverage=cov,
                            eff=0.0, duty=0.0, unit_power=0.0, oil_pd=0.0, liquid_pd=0.0,
                            liquid_sum=0.0, q_theory=w.spec.q_theory,
                            water_cut=w.spec.water_cut))
            continue
        n = len(rows)
        sum_kwh = sum(r[5] for r in rows)
        sum_run = sum(r[4] for r in rows)
        mean_liq = sum(r[1] for r in rows) / n          # m³/d（遥测本身是折算日率）
        oil_rate = sum(r[3] for r in rows) / n * 24      # oil_t 是每小时质量 → 折算日产量
        total_liq = mean_liq * hours / 24.0
        duty = sum_run / hours
        eff = mean_liq / (w.spec.q_theory * duty) if duty > 0.01 else 0.0
        out.append(dict(well=wid, status=w.status, trait=w.trait, days=n, coverage=cov,
                        eff=min(1.0, eff), duty=duty,
                        unit_power=(sum_kwh / total_liq) if total_liq > 0 else 0.0,
                        oil_pd=oil_rate, liquid_pd=mean_liq, liquid_sum=total_liq,
                        q_theory=w.spec.q_theory, water_cut=sum(r[2] for r in rows) / n))
    return out


def human_rule(rec: dict) -> str:
    """现场经验规则（用来算影子一致率）：凭泵效和产量拍方向。"""
    if rec["oil_pd"] < 0.15 or rec["eff"] < 0.15:
        return "ShutIn"
    if rec["eff"] > 0.85 and rec["duty"] < 0.90:
        return "Start"
    return "Hold"


DIRECTION = {"ShutIn": "down", "Stop": "down", "Start": "up",
             "Hold": "hold", "Escalate": "human"}


def agree(agent_action: str, rec: dict) -> bool:
    """一致率按"方向"比：降时率/关停 都算 down，避免标签差异掩盖真实分歧。"""
    return DIRECTION.get(agent_action, "?") == DIRECTION.get(human_rule(rec), "?")


def run(policy: str) -> dict:
    plant = build_plant()
    plant.wells["B-03"].valve_stuck_open = True          # 阀卡在运行位
    hist = Historian(BUILD / f"sim-{policy}.db")
    rtu = RTU(plant, stuck_open={"B-03"}, timeout_wells={"C-02"})
    audit = AuditLog(BUILD / f"audit-{policy}.jsonl")
    bus = CommandBus(rtu, audit, max_retries=2, rollback=True)
    rules, _ = da.load_rules()

    stats = dict(decisions=0, dispatched=0, done=0, rolled=0, rejected=0, awaiting=0,
                 gate_fail=0, rounds=0, low_quality=0, guard=0, sis_refused=0,
                 agree=0, compared=0)
    action_hist: dict[str, list[str]] = {}
    last_target: dict[str, float] = {}
    duty_h = {w: 0.0 for w in plant.wells}
    starts = {w: 0 for w in plant.wells}
    last_run: dict[str, bool] = {}
    snap: dict[str, tuple] = {}
    day_mark: dict[int, dict] = {}

    for hour in range(HOURS):
        now = T0 + timedelta(hours=hour)
        hist.ingest(pollute(plant.run_hour(hour, T0), hour))

        # ── 运行统计与快照（放在决策之前：影子模式的 continue 不会跳过它们）──
        for wid, w in plant.wells.items():
            prev = last_run.get(wid, False)
            for k in range(6):
                cur = w.schedule.running_at(hour + k / 6.0) or w.valve_stuck_open
                if w.sis_locked:
                    cur = False
                if hour >= WARMUP_H:
                    if cur:
                        duty_h[wid] += 1 / 6
                    if not prev and cur:
                        starts[wid] += 1
                prev = cur
            last_run[wid] = prev
        if hour == WARMUP_H:
            for wid, w in plant.wells.items():
                snap[wid] = (w.state.liquid_m3, w.state.oil_m3, w.state.kwh)
        if hour in (WARMUP_H, HOURS - 24):
            day_mark[hour] = dict(
                liquid=sum(w.state.liquid_m3 for w in plant.wells.values()),
                oil=sum(w.state.oil_m3 for w in plant.wells.values()),
                wc=sum(w.spec.water_cut for w in plant.wells.values()) / len(plant.wells),
                wear=sum(w.wear for w in plant.wells.values()) / len(plant.wells))

        if policy in ("shadow", "agent") and hour >= 12 and hour % DECISION_EVERY == 0:
            stats["rounds"] += 1
            t1, t0 = now, max(T0, now - timedelta(hours=24))
            decisions = []
            for rec in features(hist, plant, t0, t1):
                if rec["coverage"] < 0.90:
                    stats["low_quality"] += 1
                    audit.log("data.quality_block", f"{rec['well']}-{hour}", rec["well"],
                              f"覆盖率 {rec['coverage']*100:.0f}% <90%，拒绝自动决策")
                    continue
                d = da.decide(rec, rules, t1.month)
                decisions.append((rec, d))
                stats["decisions"] += 1
                stats["compared"] += 1
                stats["agree"] += 1 if agree(d["action"], rec) else 0
                stats[f"act_{d['action']}"] = stats.get(f"act_{d['action']}", 0) + 1

            if policy == "shadow":                       # 影子：只算不发
                continue

            g = da.to_rdf(decisions, t0.isoformat(), t1.isoformat())
            shapes = Graph().parse(ROOT / "ontology" / "decision-shapes.ttl", format="turtle")
            ont = Graph()
            for f in ("analytics-core.ttl", "decision-core.ttl"):
                ont.parse(ROOT / "ontology" / f, format="turtle")
            ok, _, _ = da.shacl_validate(data_graph=g + ont, shacl_graph=shapes,
                                         ont_graph=ont, inference="none", advanced=True)
            if not ok:
                stats["gate_fail"] += 1
                audit.log("gate.blocked", f"round-{hour}", "-", "门禁未通过，本轮不下发")
                continue

            for rec, d in decisions:
                if d["action"] in ("Hold", "Escalate"):
                    continue
                if d["action"] == "ShutIn" and rec["well"] in last_target:
                    floor = last_target[rec["well"]] * 0.8
                    if d["duty_target"] < floor:
                        d["on"], d["off"], d["duty_target"], _ = da.cycle(floor, rec["trait"], 10)
                        audit.log("guard.rate_limit", f"{rec['well']}-{hour}", rec["well"],
                                  f"目标时率限幅到 {floor*100:.0f}%")
                last_target[rec["well"]] = d["duty_target"]
                ah = action_hist.setdefault(rec["well"], [])
                if len(ah) >= 2 and ah[-1] != d["action"] and ah[-2] != d["action"]:
                    stats["guard"] += 1
                    audit.log("guard.oscillation", f"{rec['well']}-{hour}", rec["well"],
                              f"动作反复 {ah[-2:]} → {d['action']}，锁定转人工")
                    continue
                ah.append(d["action"])

                lvl = Authorization.level(d["action"])
                cmd = Command(request_id=f"{rec['well']}-{hour}-{d['action']}",
                              well=rec["well"], action=d["action"], on_h=d["on"],
                              off_h=d["off"], duty=d["duty_target"], deadline_s=30,
                              expected_state=f"duty={d['duty_target']:.2f}", auth=lvl)
                r = bus.dispatch(cmd)
                if r.status == "AwaitingHuman":
                    stats["awaiting"] += 1
                    if lvl == "C":
                        continue
                    r = bus.confirm(cmd, operator="值班长-张工")
                stats["dispatched"] += 1
                if r.status == "Done":
                    stats["done"] += 1
                elif r.status == "RolledBack":
                    stats["rolled"] += 1
                elif r.status == "Rejected":
                    stats["rejected"] += 1
                    if "SIS" in (r.error or ""):
                        stats["sis_refused"] += 1
                        audit.log("sis.refused", cmd.request_id, cmd.well, r.error)

        for wid, w in plant.wells.items():
            pass

    span = (HOURS - WARMUP_H) / 24.0
    per_well = {}
    for wid, w in plant.wells.items():
        l0, o0, k0 = snap[wid]
        per_well[wid] = dict(liquid=(w.state.liquid_m3 - l0) / span,
                             oil=(w.state.oil_m3 - o0) / span,
                             kwh=(w.state.kwh - k0) / span,
                             duty=duty_h[wid] / (HOURS - WARMUP_H),
                             starts=starts[wid] / span, trait=w.trait,
                             sis=w.sis_locked)
    agg = {k: sum(v[k] for v in per_well.values()) for k in ("liquid", "oil", "kwh", "starts")}
    agg["unit"] = agg["kwh"] / agg["liquid"] if agg["liquid"] else 0.0
    audit.flush()
    return dict(policy=policy, per_well=per_well, agg=agg, hist=hist, plant=plant,
                day_mark=day_mark, **stats)


def main() -> int:
    print("=" * 100)
    print(f"模拟生产环境 · {DAYS} 天 × 10 口井 · 三策略对照（同一天气、同一批设备缺陷、同一套脏数据）")
    print("=" * 100)
    base = run("baseline")
    shadow = run("shadow")
    agt = run("agent")

    print(f"\n{'策略':26s} {'日产液':>9s} {'日产油':>8s} {'日耗电':>9s} {'吨液电耗':>9s} {'启停/日':>8s}")
    print("-" * 100)
    for name, r in (("baseline（开4停8）", base), ("shadow（只算不发）", shadow),
                    ("agent（真闭环）", agt)):
        a = r["agg"]
        print(f"{name:26s} {a['liquid']:9.1f} {a['oil']:8.2f} {a['kwh']:9.0f} "
              f"{a['unit']:9.1f} {a['starts']:8.1f}")

    print("\n【分井对照（基线 → 智能）】")
    print(f"{'井':7s} {'性状':5s} {'时率':>11s} {'产液':>13s} {'电耗':>13s} {'吨液电耗变化':>12s} {'SIS':>4s}")
    print("-" * 100)
    for wid in base["per_well"]:
        b, a = base["per_well"][wid], agt["per_well"][wid]
        du = (a["kwh"] / a["liquid"] if a["liquid"] else 0) - \
             (b["kwh"] / b["liquid"] if b["liquid"] else 0)
        print(f"{wid:7s} {b['trait']:5s} {b['duty']*100:4.0f}%→{a['duty']*100:4.0f}% "
              f"{b['liquid']:6.1f}→{a['liquid']:6.1f} {b['kwh']:6.0f}→{a['kwh']:6.0f} "
              f"{du:+11.1f} {'锁定' if a['sis'] else '':>4s}")

    ba, aa = base["agg"], agt["agg"]
    econ = Economics()
    d_oil, d_kwh = aa["oil"] - ba["oil"], aa["kwh"] - ba["kwh"]
    gain, cost = d_oil * econ.revenue_per_t(), d_kwh * 0.55
    print("\n【增量经济性】（$%.0f/bbl、0.55 元/kWh）" % econ.price_usd)
    print(f"  增油 {d_oil:+.2f} t/d → {gain:+,.0f} 元/日 ｜ 增电 {d_kwh:+.0f} kWh/d → "
          f"{cost:+,.0f} 元/日 ｜ 净 {gain-cost:+,.0f} 元/日（10 口井）")

    dm = agt["day_mark"]
    first, last = dm[WARMUP_H], dm[HOURS - 24]
    print("\n【环境演化验证（第 1 天 vs 第 29 天，证明递减/含水/磨损真的在跑）】")
    print(f"  含水率 {first['wc']*100:.1f}% → {last['wc']*100:.1f}% ｜ "
          f"泵磨损系数 {first['wear']:.4f} → {last['wear']:.4f}")

    print("\n【影子运行：与人工经验规则的一致率】")
    print(f"  比对 {shadow['compared']} 条 ｜ 一致 {shadow['agree']} 条 ｜ "
          f"一致率 {shadow['agree']/max(shadow['compared'],1)*100:.1f}%")

    print("\n【联锁（SIS）：Agent 不可覆盖】")
    for e in agt["plant"].sis_events:
        print(f"  {e['well']} 第 {e['hour']}h 跳闸：{e['reason']}")
    print(f"  Agent 因联锁被拒 {agt['sis_refused']} 次（审计 sis.refused）")

    print("\n【过程统计】")
    print(f"  agent：决策 {agt['rounds']} 轮/{agt['decisions']} 条 ｜ 下发 {agt['dispatched']} ｜ "
          f"成功 {agt['done']} ｜ 回滚 {agt['rolled']} ｜ 被拒 {agt['rejected']} ｜ "
          f"等审批 {agt['awaiting']} ｜ 门禁拦截 {agt['gate_fail']} ｜ 质量拦截 {agt['low_quality']} ｜ "
          f"振荡锁定 {agt['guard']}")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
