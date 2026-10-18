"""
shift_demo.py —— 早班闭环演示：故障预判 → 工单派工 → AI 班报 → 数字核对 → 归档

产物：build/shift-report.md（可直接贴到值班群/交接班记录）
依赖：fault_prediction / dispatch / report_ai 三步的产物，本脚本会先跑一遍。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
sys.path.insert(0, str(ROOT / "scripts"))

import dispatch as dp            # noqa: E402
import fault_predict as fp       # noqa: E402
import report_ai as ra           # noqa: E402


def run() -> int:
    print("① 故障预判…")
    fp.main()
    print("\n② 工单与派工…")
    dp.main()

    facts = ra.facts()
    template = ra.template(facts)
    ok_t, _ = ra.check(template, facts)

    llm_path = BUILD / "report-llm.md"
    if not llm_path.exists():                     # 冷启动时用仓库内的真实模型输出样本
        alt = ROOT / "samples" / "report-llm.md"
        if alt.exists():
            llm_path = alt
    ai_text, ai_ok, ai_problems = "", False, []
    if llm_path.exists():
        ai_text = llm_path.read_text(encoding="utf-8-sig").strip()
        ai_ok, ai_problems = ra.check(ai_text, facts)
    chosen = ai_text if (ai_text and ai_ok) else template

    risks = json.loads((BUILD / "fault-risk.json").read_text(encoding="utf-8"))
    orders = json.loads((BUILD / "workorders.json").read_text(encoding="utf-8"))
    audit = [json.loads(l) for l in
             (BUILD / "dispatch-audit.jsonl").read_text(encoding="utf-8").splitlines() if l]

    md = [f"# 早班生产简报（{facts['date']}）", ""]
    md.append("## 一、总体")
    md.append(chosen)
    md.append("")
    md.append("## 二、故障预判（风险分 ≥0.5 需处置）")
    md.append("| 井 | 类型 | 等级 | 风险分 | 预计触线 | 处置技能 | 证据 |")
    md.append("|---|---|---|---|---|---|---|")
    for r in sorted([x for x in risks if x["score"] >= 0.5 and x["severity"] != "low"],
                    key=lambda x: -x["score"]):
        d = "立即" if r["days"] == 0 else (f"{r['days']:.0f} 天" if r["days"] else "-")
        md.append(f"| {r['well']} | {r['type']} | {r['severity']} | {r['score']} | {d} "
                  f"| {r['skill']} | {r['evidence']} |")
    md.append("")
    md.append("## 三、工单与派工")
    md.append("| 工单 | 井 | 类型 | 优先级 | 技能 | SLA(min) | 派给 | 状态 |")
    md.append("|---|---|---|---|---|---|---|---|")
    for o in orders:
        md.append(f"| {o['oid']} | {o['well']} | {o['type']} | {o['priority']} | {o['skill']} "
                  f"| {o['sla']} | {o['assignee'] or '-'} | {o['status']} |")
    by = {}
    for o in orders:
        by[o["status"]] = by.get(o["status"], 0) + 1
    md.append("")
    md.append(f"派工汇总：{' ｜ '.join(f'{k} {v}' for k, v in by.items())}")
    md.append("")
    md.append("## 四、AI 班报与核对")
    if ai_text:
        md.append(f"- 模型输出：**{'通过核对' if ai_ok else '未通过核对，已回退模板'}**")
        if ai_problems:
            md += [f"  - {p}" for p in ai_problems]
        md.append("")
        md.append("> " + ai_text.replace("\n", "\n> ") if ai_ok else "> " + template)
    else:
        md.append("- 未提供模型输出，使用模板报告（模板自检："
                  f"{'通过' if ok_t else '未通过'}）")
    md.append("")
    md.append("## 五、审计（最近 10 条）")
    md.append("```")
    for e in audit[-10:]:
        md.append(f"{e['t']}  {e['order']}  {e['event']:9s} {e['detail']}")
    md.append("```")

    out = BUILD / "shift-report.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\n③ 班报已生成：{out}")
    print(f"   AI 报告核对：{'通过' if ai_ok else ('未提供' if not ai_text else '拒收并回退模板')}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
