"""
report_ai.py —— 班报生成：事实表 → AI 写话术 → 数字核对（不通过就退回模板）

护栏（这是"AI 出报告"能进生产的关键）：
  ① 事实表里的每个数字都由确定性代码算出，AI 只负责组织语言；
  ② 报告里出现的**所有数字**必须能在事实表中找到（含四舍五入变体），否则拒收；
  ③ 报告里出现的**井号**必须在当日风险清单内，否则拒收；
  ④ 拒收后回退到模板报告，并保留拒收原因（可复盘）。

用法：
  python report_ai.py --emit-prompt        # 输出喂给模型的提示词
  python report_ai.py --check build/report-llm.md   # 核对模型输出
  python report_ai.py --template           # 生成模板版班报
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DB = BUILD / "analytics.db"


def facts() -> dict:
    con = sqlite3.connect(DB)
    maxday = con.execute("SELECT MAX(day) FROM well_daily").fetchone()[0]
    row = con.execute(
        """SELECT SUM(liquid_m3), SUM(oil_t), SUM(kwh),
                  SUM(liquid_m3)/(SELECT COUNT(DISTINCT well) FROM well_daily),
                  SUM(kwh)/NULLIF(SUM(liquid_m3),0)
           FROM well_daily WHERE day >= date(?, '-29 day')""", (maxday,)).fetchone()
    liq30, oil30, kwh30, liq_per_well, unit = row
    risks = json.loads((BUILD / "fault-risk.json").read_text(encoding="utf-8"))
    orders = json.loads((BUILD / "workorders.json").read_text(encoding="utf-8"))

    by_type: dict[str, int] = {}
    for r in risks:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    status: dict[str, int] = {}
    for o in orders:
        status[o["status"]] = status.get(o["status"], 0) + 1
    top = sorted([r for r in risks if r["severity"] != "low"],
                 key=lambda r: -r["score"])[:3]
    done = status.get("done", 0)
    return dict(
        date=maxday, window_days=30,
        wells=con.execute("SELECT COUNT(DISTINCT well) FROM well_daily").fetchone()[0],
        liquid_30d=round(liq30, 1), oil_30d=round(oil30, 1), kwh_30d=round(kwh30, 0),
        liquid_per_well=round(liq_per_well, 1),
        unit_power=round(unit, 1),
        risk_total=len(risks),
        risk_by_type=by_type,
        top_risks=[dict(well=r["well"], type=r["type"], score=r["score"],
                        days=r["days"], evidence=r["evidence"]) for r in top],
        orders_total=len(orders), orders_done=done,
        orders_escalated=status.get("escalated", 0), orders_rejected=status.get("rejected", 0),
        completion=round(done / max(len(orders), 1) * 100, 0),
        wells_in_risk=sorted({r["well"] for r in risks}),
    )


def build_prompt(f: dict) -> str:
    return f"""你是采油厂值班长的报告助手。请只根据下面的事实表写一份早班简报。

【硬性要求】
1. 180~250 字，中文，分三段：总体、重点井、今日优先动作。
2. **只能使用事实表中出现的数字**，不得自行推导、换算或编造；每个数字必须原样来自事实表。
3. 提到井号时只能用事实表 top_risks 里的井号。
4. 不写套话，不写"建议加强管理"这类空话。

【事实表】
日期 {f['date']}｜井数 {f['wells']}
近 30 天：产液 {f['liquid_30d']} m³、产油 {f['oil_30d']} t、耗电 {f['kwh_30d']} kWh
单井日均产液 {f['liquid_per_well']} m³｜吨液电耗 {f['unit_power']} kWh/m³
风险合计 {f['risk_total']} 条 ｜ 分类 {json.dumps(f['risk_by_type'], ensure_ascii=False)}
工单 {f['orders_total']} 条：完成 {f['orders_done']}、升级 {f['orders_escalated']}、
拒单 {f['orders_rejected']}｜完成率 {f['completion']}%
重点井：
{chr(10).join(f"- {r['well']}｜{r['type']}｜风险 {r['score']}｜{r['evidence']}" for r in f['top_risks'])}

只输出简报正文，不要标题、不要 Markdown。"""


def numbers_in(text: str) -> set[str]:
    out = set()
    for m in re.finditer(r"\d+(?:\.\d+)?", text):
        out.add(m.group())
    return out


def allowed_numbers(f: dict) -> set[str]:
    vals = [str(f["wells"]), str(f["liquid_30d"]), str(f["oil_30d"]), str(f["kwh_30d"]),
            str(f["liquid_per_well"]), str(f["unit_power"]), str(f["risk_total"]),
            str(f["orders_total"]), str(f["orders_done"]), str(f["orders_escalated"]),
            str(f["orders_rejected"]), str(f["completion"]), str(f["window_days"])]
    for v in f["risk_by_type"].values():
        vals.append(str(v))
    for r in f["top_risks"]:
        vals += [str(r["score"]), str(r["days"]) if r["days"] is not None else ""]
        vals += numbers_in(r["evidence"])            # 证据串里的数字也是"事实表内的"
    allowed: set[str] = set()
    for v in vals:
        if not v:
            continue
        allowed.add(v)
        try:
            x = float(v)
        except ValueError:
            continue
        allowed |= {str(int(x)), f"{x:.0f}", f"{x:.1f}", f"{x:.2f}", str(round(x))}
    return {a for a in allowed if a}


def check(text: str, f: dict) -> tuple[bool, list[str]]:
    problems = []
    allow = allowed_numbers(f)
    # 井号与日期先抹掉，避免把 "06" / "09" 误判成数字
    masked = text
    for wid in sorted(f["wells_in_risk"], key=len, reverse=True):
        masked = masked.replace(wid, "＃")
    masked = masked.replace(f["date"], "Ｄ")
    for n in numbers_in(masked):
        if n not in allow:
            problems.append(f"数字「{n}」不在事实表中")
    # 语义级核对：'N 口井' 必须等于事实井数（数字在允许集里也可能语义错）
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*口井", text):
        if m.group(1) not in (str(f["wells"]), f"{f['wells']:.0f}"):
            problems.append(f"「{m.group(1)} 口井」与事实井数 {f['wells']} 不符")
    for wid in re.findall(r"[甲区乙区丙区丁区]{2,3}-\d{2}", text):
        if wid not in f["wells_in_risk"]:
            problems.append(f"井号「{wid}」不在今日风险清单内")
    return (not problems), problems


def template(f: dict) -> str:
    t = f["top_risks"][0] if f["top_risks"] else None
    s = (f"截至 {f['date']}，全厂 {f['wells']} 口井近 30 天产液 {f['liquid_30d']} 立方米、"
         f"产油 {f['oil_30d']} 吨、耗电 {f['kwh_30d']} 千瓦时，"
         f"吨液电耗 {f['unit_power']} 千瓦时每立方米。"
         f"今日识别风险 {f['risk_total']} 条，工单 {f['orders_total']} 条、"
         f"完成 {f['orders_done']} 条、升级 {f['orders_escalated']} 条、"
         f"拒单 {f['orders_rejected']} 条，完成率 {f['completion']:.0f}%。")
    if t:
        s += (f"重点井 {t['well']}：{t['evidence']}。"
              + (f"其余重点井为 {f['top_risks'][1]['well']}、{f['top_risks'][2]['well']}。"
                 if len(f["top_risks"]) > 2 else ""))
        s += f"今日优先动作：先处置 {t['well']}，同步跟进升级工单。"
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-prompt", action="store_true")
    ap.add_argument("--facts", action="store_true")
    ap.add_argument("--template", action="store_true")
    ap.add_argument("--check", default="")
    args = ap.parse_args()

    f = facts()
    if args.facts:
        print(json.dumps(f, ensure_ascii=False, indent=2))
        return 0
    if args.emit_prompt:
        (BUILD / "report-prompt.txt").write_text(build_prompt(f), encoding="utf-8")
        print(build_prompt(f))
        return 0
    if args.template:
        text = template(f)
        (BUILD / "report-template.md").write_text(text, encoding="utf-8")
        ok, problems = check(text, f)
        print(text)
        print(f"\n[模板自检] 通过={ok} {problems if problems else ''}")
        return 0
    if args.check:
        text = Path(args.check).read_text(encoding="utf-8")
        ok, problems = check(text, f)
        print(f"[AI 报告核对] 通过={ok}")
        for p in problems:
            print(f"  ✗ {p}")
        if not ok:
            print("\n→ 拒收模型输出，回退模板报告：")
            print(template(f))
        return 0 if ok else 2
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
