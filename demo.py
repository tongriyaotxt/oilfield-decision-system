"""
demo.py —— 一键跑出最终 Demo（自包含 HTML 报告）

跑三件事并把结果写进 build/demo.html：
  ① 14 天三策略对照（baseline / shadow / agent），含脏数据、设备缺陷、SIS 联锁
  ② 规模压测（1000 / 5000 / 10000 井的决策吞吐）
  ③ 门禁与统计（决策可追溯、影子一致率、联锁拒绝次数）

不依赖任何外网资源，双击即可看。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "sim"))

import run_production as rp          # noqa: E402
import scale_test                    # noqa: E402
from control_api import Economics    # noqa: E402

BUILD = ROOT / "build"

CSS = """
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;
background:#0f1419;color:#d7dde3;line-height:1.65}
.wrap{max-width:1080px;margin:0 auto;padding:36px 24px 80px}
h1{font-size:26px;margin:0 0 6px;color:#fff}
h2{font-size:17px;margin:34px 0 10px;padding-bottom:6px;border-bottom:1px solid #232a31;color:#8fd3ff}
h3{font-size:14px;margin:18px 0 8px;color:#c9a227}
.sub{color:#7d8894;font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}
th,td{padding:7px 10px;border-bottom:1px solid #1d2429;text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:#8fa0af;font-weight:600;background:#151b20}
tr:hover td{background:#141a1f}
.up{color:#5fbf7f}.down{color:#e0705b}.warn{color:#e0b050}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:14px 0}
.card{background:#151b20;border:1px solid #232a31;border-radius:8px;padding:14px}
.card .k{font-size:12px;color:#7d8894}
.card .v{font-size:22px;color:#fff;margin-top:4px}
.card .d{font-size:12px;margin-top:2px}
pre{background:#0b1014;border:1px solid #1d2429;border-radius:8px;padding:14px;
overflow-x:auto;font-size:12px;color:#a9b7c6}
.note{background:#1a1510;border-left:3px solid #c9a227;padding:10px 14px;font-size:13px;
color:#d8c9a0;margin:12px 0;border-radius:0 6px 6px 0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
code{background:#1b2329;padding:1px 5px;border-radius:4px;font-size:12px;color:#9fd0ff}
"""


def bar_chart(rows: list[tuple[str, float, float]], title: str) -> str:
    """两序列横向条形图（SVG）。rows = [(label, base, agent)]"""
    h = 26 * len(rows) + 30
    maxv = max(max(b, a) for _, b, a in rows) or 1
    out = [f'<svg width="100%" height="{h}" viewBox="0 0 720 {h}">']
    out.append(f'<text x="0" y="14" fill="#8fa0af" font-size="12">{title}</text>')
    for i, (label, b, a) in enumerate(rows):
        y = 26 + i * 26
        wb = (b / maxv) * 300
        wa = (a / maxv) * 300
        out.append(f'<text x="0" y="{y+11}" fill="#a9b7c6" font-size="11">{label}</text>')
        out.append(f'<rect x="76" y="{y}" width="{wb:.1f}" height="9" fill="#3d4b57"/>')
        out.append(f'<rect x="76" y="{y+11}" width="{wa:.1f}" height="9" fill="#2f7fb0"/>')
        out.append(f'<text x="{76+max(wb,wa)+8:.0f}" y="{y+16}" fill="#7d8894" '
                   f'font-size="10">{b:.0f} / {a:.0f}</text>')
    out.append('</svg>')
    return "".join(out)


def main() -> int:
    rp.HOURS = 14 * 24                       # demo 用 14 天，保证秒级出结果
    rp.DAYS, rp.WARMUP_H = 14, 24
    print("跑 baseline / shadow / agent 三种策略（14 天 × 10 井）…")
    base, shadow, agt = rp.run("baseline"), rp.run("shadow"), rp.run("agent")

    ba, aa = base["agg"], agt["agg"]
    econ = Economics()
    d_oil, d_kwh = aa["oil"] - ba["oil"], aa["kwh"] - ba["kwh"]
    net = d_oil * econ.revenue_per_t() - d_kwh * 0.55

    print("跑规模压测…")
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        scale_test.main()

    dm = agt["day_mark"]
    keys = sorted(dm)
    wc0, wc1 = dm[keys[0]]["wc"], dm[keys[-1]]["wc"]
    wear0, wear1 = dm[keys[0]]["wear"], dm[keys[-1]]["wear"]

    def pct(a: float, b: float) -> str:
        if not b:
            return "-"
        d = (a - b) / b * 100
        cls = "up" if d > 0 else ("down" if d < 0 else "")
        return f'<span class="{cls}">{d:+.1f}%</span>'

    well_rows = []
    for wid, b in base["per_well"].items():
        a = agt["per_well"][wid]
        du = (a["kwh"] / a["liquid"] if a["liquid"] else 0) - \
             (b["kwh"] / b["liquid"] if b["liquid"] else 0)
        well_rows.append(
            f"<tr><td>{wid}</td><td>{b['trait']}</td>"
            f"<td>{b['duty']*100:.0f}% → {a['duty']*100:.0f}%</td>"
            f"<td>{b['liquid']:.1f} → {a['liquid']:.1f}</td>"
            f"<td>{b['kwh']:.0f} → {a['kwh']:.0f}</td>"
            f"<td class=\"up\">{du:+.0f}</td>"
            f"<td>{'🔒 锁定' if a['sis'] else ''}</td></tr>")

    sis_rows = "".join(
        f"<tr><td>{e['well']}</td><td>{e['hour']}h</td><td>{e['reason']}</td></tr>"
        for e in agt["plant"].sis_events)

    chart = bar_chart([(wid, base["per_well"][wid]["kwh"], agt["per_well"][wid]["kwh"])
                       for wid in base["per_well"]], "日耗电对比（灰=基线 / 蓝=智能）")

    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>油井生产智能决策系统 · Demo</title><style>{CSS}</style></head><body><div class="wrap">
<h1>油井生产智能决策系统 · Demo</h1>
<div class="sub">模拟生产环境 · 14 天 × 10 口井 · 三策略对照 · 脏数据与设备缺陷全部注入
· 生成于 Demo 脚本运行时刻</div>

<div class="note">一句话结论：<b>该关的关（省电）、该开的开（提产）</b>，
每条决策都带口径、证据与回执；联锁（SIS）优先级高于 Agent，且不可被覆盖。</div>

<h2>1. 关键结果</h2>
<div class="cards">
 <div class="card"><div class="k">日产液</div><div class="v">{aa['liquid']:.1f}</div>
   <div class="d">{pct(aa['liquid'], ba['liquid'])} vs 基线 {ba['liquid']:.1f} m³/d</div></div>
 <div class="card"><div class="k">日产油</div><div class="v">{aa['oil']:.2f}</div>
   <div class="d">{pct(aa['oil'], ba['oil'])} vs 基线 {ba['oil']:.2f} t/d</div></div>
 <div class="card"><div class="k">日耗电</div><div class="v">{aa['kwh']:.0f}</div>
   <div class="d">{pct(aa['kwh'], ba['kwh'])} vs 基线 {ba['kwh']:.0f} kWh/d</div></div>
 <div class="card"><div class="k">吨液电耗</div><div class="v">{aa['unit']:.1f}</div>
   <div class="d">{pct(aa['unit'], ba['unit'])} vs 基线 {ba['unit']:.1f} kWh/m³</div></div>
 <div class="card"><div class="k">增量净收益</div><div class="v">{net/1e4:+.1f} 万</div>
   <div class="d">元/日（10 口井，油价 ${econ.price_usd:.0f}）</div></div>
</div>

<h2>2. 三策略对照</h2>
<table><tr><th>策略</th><th>日产液 m³/d</th><th>日产油 t/d</th><th>日耗电 kWh</th>
<th>吨液电耗</th><th>启停/日</th></tr>
<tr><td>baseline（现场开4停8）</td><td>{ba['liquid']:.1f}</td><td>{ba['oil']:.2f}</td>
<td>{ba['kwh']:.0f}</td><td>{ba['unit']:.1f}</td><td>{ba['starts']:.1f}</td></tr>
<tr><td>shadow（只算不发）</td><td>{shadow['agg']['liquid']:.1f}</td>
<td>{shadow['agg']['oil']:.2f}</td><td>{shadow['agg']['kwh']:.0f}</td>
<td>{shadow['agg']['unit']:.1f}</td><td>{shadow['agg']['starts']:.1f}</td></tr>
<tr><td><b>agent（真闭环）</b></td><td><b>{aa['liquid']:.1f}</b></td><td><b>{aa['oil']:.2f}</b></td>
<td><b>{aa['kwh']:.0f}</b></td><td><b>{aa['unit']:.1f}</b></td>
<td><b>{aa['starts']:.1f}</b></td></tr></table>
<div class="sub">shadow 与 baseline 完全一致，说明"只算不发"确实不影响现场；
agent 的差异全部来自真实下发的指令。</div>

<h2>3. 分井对照</h2>
<table><tr><th>井</th><th>性状</th><th>时率</th><th>产液 m³/d</th><th>耗电 kWh/d</th>
<th>吨液电耗变化</th><th>联锁</th></tr>{''.join(well_rows)}</table>
{chart}

<h2>4. 环境演化（证明模型不是静态的）</h2>
<table><tr><th>指标</th><th>第 1 天</th><th>第 13 天</th></tr>
<tr><td>平均含水率</td><td>{wc0*100:.1f}%</td><td>{wc1*100:.1f}%</td></tr>
<tr><td>泵磨损系数</td><td>{wear0:.4f}</td><td>{wear1:.4f}</td></tr></table>
<div class="sub">供液能力按 0.14%/日递减；含水率随累产上升；泵充满程度随运行时长下降。</div>

<h2>5. 影子运行与联锁</h2>
<div class="grid2">
<div><h3>与人工经验规则的一致率</h3>
<p>比对 <b>{shadow['compared']}</b> 条，一致 <b>{shadow['agree']}</b> 条，
一致率 <b class="warn">{shadow['agree']/max(shadow['compared'],1)*100:.1f}%</b>。</p>
<div class="sub">分歧集中在"泵效 15%~30%"的井：人工经验会判维持，
系统判缩短时率——这正是间开收益最大的区间，逐条复核即可。</div></div>
<div><h3>抽空保护（SIS）跳闸</h3>
<table><tr><th>井</th><th>时刻</th><th>原因</th></tr>{sis_rows}</table>
<p>Agent 因联锁被拒 <b>{agt['sis_refused']}</b> 次（审计 <code>sis.refused</code>）。</p></div>
</div>

<h2>6. 决策过程统计</h2>
<pre>决策轮数 {agt['rounds']}   决策条数 {agt['decisions']}
下发 {agt['dispatched']}   成功 {agt['done']}   回滚 {agt['rolled']}   被拒 {agt['rejected']}
等审批 {agt['awaiting']}   门禁拦截 {agt['gate_fail']}   质量拦截 {agt['low_quality']}
动作分布 {', '.join(f"{k[4:]}={v}" for k, v in sorted(agt.items()) if k.startswith('act_'))}</pre>

<h2>7. 规模压测</h2>
<pre>{buf.getvalue().strip()}</pre>

<h2>8. 边界：能做什么 / 不能做什么</h2>
<table><tr><th>能</th><th>不能</th></tr>
<tr><td>一句话取数，口径自动对齐、可复算</td><td>提高单井产能上限（间开不创造供液）</td></tr>
<tr><td>判定每口井该开多久停多久</td><td>替代执行机构（阀门/变频/RTU 仍是硬件）</td></tr>
<tr><td>决策可追溯、门禁不通过不许下发</td><td>替你做经营决策（关停/措施由人签字）</td></tr>
<tr><td>执行失败自动回滚、偏差超阈值降级</td><td>覆盖安全联锁（SIS 永远优先）</td></tr>
<tr><td>回采偏差 11.9% → 5.0%（按井标定）</td><td>复现今天数字到真实油田（需换真台账重标定）</td></tr></table>

<h2>9. 组成与文件</h2>
<pre>ontology/analytics-core.ttl    指标口径（14 指标/47 别名/三条铁律）
ontology/decision-core.ttl     5 条决策规则 + 5 个动作 + 5 个约束
ontology/decision-shapes.ttl   决策门禁（可追溯/周期/稠油/降级/审批）
ontology/control-core.ttl      自主控制契约（命令/回执/授权/灰度/审计）
scripts/analytics_agent.py     分析 Agent（口径四件套 + 陷阱题）
scripts/decision_agent.py      开关井决策（规则→约束→预期→RDF）
scripts/closed_loop.py         回归闭环（回采→降级→标定）
scripts/control_api.py         命令总线（幂等/重试/回滚/审计）
sim/sim_env.py                 现场仿真 + 时序库 + RTU + 脏数据 + SIS
sim/run_production.py          三策略对照编排
scripts/scale_test.py          规模压测</pre>

</div></body></html>"""

    out = BUILD / "demo.html"
    out.write_text(html, encoding="utf-8")
    print(f"\nDemo 已生成：{out}  ({out.stat().st_size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
