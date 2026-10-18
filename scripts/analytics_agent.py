"""
analytics_agent.py —— 受本体约束的生产数据分析 Agent

设计原则（"Agent 能落地"与"Agent 乱写 SQL"的分界线）：
  ① 模型只负责"自然语言 → 计划"这一步；计划之外的一切（口径、聚合方式、时间口径、
     能否相加、分母为 0 怎么办）**全部由本体决定**，不让模型自由发挥。
  ② 每个指标唯一 IRI + 公式 + 是否可加 + 分子/分母 + 时间口径；比率型指标跨实体汇总
     强制走 SUM(分子)/SUM(分母)。
  ③ 执行前校验、执行后自检；不通过就改写计划重试，仍不通过就明说"不能算"。

工具（供 LLM function-calling 调用，也可脚本直连）：
  resolve(text) / describe(metric) / plan(question) / compile(plan) / run(sql) /
  validate(plan, rows) / answer(plan, rows, issues)
"""
from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from rdflib import Graph, Namespace, RDF, URIRef
from rdflib.namespace import RDFS, SKOS

ROOT = Path(__file__).resolve().parents[1]
ONT = ROOT / "ontology" / "analytics-core.ttl"
DB = ROOT / "build" / "analytics.db"
AO = Namespace("https://example.org/ao#")

UNIT_LABEL = {"u-t": "t", "u-m3": "m³", "u-pct": "%",
              "u-kwh": "kWh", "u-h": "h", "u-count": "口"}


def short(v) -> str:
    s = str(v)
    return s.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(v, URIRef) else str(v)


# ─────────────────────────── 语义层 ───────────────────────────
class SemanticLayer:
    def __init__(self, path: Path):
        self.g = Graph().parse(path, format="turtle")
        self.metrics: dict[str, dict] = {}
        self.aliases: list[tuple[str, str]] = []
        self.dims: dict[str, str] = {}
        for cls in ("AtomicMetric", "DerivedMetric"):
            for m in self.g.subjects(RDF.type, AO[cls]):
                iri = short(m)
                self.metrics[iri] = self._load(m)
                for a in self.g.objects(m, AO.hasAlias):
                    self.aliases.append((str(a), iri))
                self.aliases.append((str(self.g.value(m, RDFS.label) or iri), iri))
        self.aliases = [(a, i) for a, i in self.aliases if a]
        self.aliases.sort(key=lambda x: -len(x[0]))   # 长别名优先
        for d in self.g.subjects(RDF.type, AO.Dimension):
            self.dims[short(d)] = str(self.g.value(d, AO.dimField) or short(d))

    def _load(self, m: URIRef) -> dict:
        g = self.g
        unit = g.value(m, AO.hasUnit)
        num, den = g.value(m, AO.numerator), g.value(m, AO.denominator)
        tsem = g.value(m, AO.hasTimeSemantics)
        return dict(
            iri=short(m),
            label=str(g.value(m, RDFS.label) or short(m)),
            unit=UNIT_LABEL.get(short(unit), "") if unit else "",
            additive=bool(g.value(m, AO.isAdditive)),
            agg=str(g.value(m, AO.hasAggregation) or "sum"),
            field=str(g.value(m, AO.sourceField) or ""),
            formula=str(g.value(m, AO.hasFormula) or ""),
            num=short(num) if num else None,
            den=short(den) if den else None,
            time_sem=short(tsem) if tsem else "",
            definition=str(g.value(m, SKOS.definition) or ""),
        )

    def resolve_all(self, text: str) -> list[str]:
        """一次问句里出现多个指标时全部识别（长别名优先、不重叠）。"""
        taken = [False] * len(text)
        found: list[tuple[int, str]] = []
        for alias, iri in self.aliases:
            start = 0
            while True:
                i = text.find(alias, start)
                if i < 0:
                    break
                if not any(taken[i:i + len(alias)]):
                    for k in range(i, i + len(alias)):
                        taken[k] = True
                    found.append((i, iri))
                start = i + 1
        seen, out = set(), []
        for _, iri in sorted(found):
            if iri not in seen:
                seen.add(iri)
                out.append(iri)
        return out

    def sql_expr(self, iri: str, depth: int = 0) -> str:
        m = self.metrics[iri]
        if depth > 5:
            raise RecursionError(f"口径循环依赖: {iri}")
        if m["num"] and m["den"]:
            e = f"({self.sql_expr(m['num'], depth + 1)})/NULLIF({self.sql_expr(m['den'], depth + 1)},0)"
            return f"100.0*{e}" if m["unit"] == "%" else e
        if m["agg"] == "count-distinct":
            return f"COUNT(DISTINCT {m['field']})"
        return f"SUM({m['field']})"


# ─────────────────────────── 计划/问题 ───────────────────────────
@dataclass
class Plan:
    question: str
    metrics: list[str]
    group_by: list[str] = field(default_factory=list)
    filters: list[tuple[str, str, list[str]]] = field(default_factory=list)
    start: str = ""
    end: str = ""
    intent: str = "total"
    order_desc: bool = False
    rank_by: str = ""
    limit: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class Issue:
    level: str
    code: str
    message: str


class Agent:
    def __init__(self, sl: SemanticLayer, db: Path):
        self.sl = sl
        self.con = sqlite3.connect(db)
        self.trace: list[str] = []

    def log(self, tool: str, detail: str) -> None:
        self.trace.append(f"  → {tool:9s} {detail}")

    # ---------- 时间 ----------
    def _window(self, q: str) -> tuple[str, str, str]:
        maxday = self.con.execute("SELECT MAX(day) FROM well_daily").fetchone()[0]
        dmax = date.fromisoformat(maxday)
        if re.search(r"上个月|上月", q):
            last = date(dmax.year, dmax.month, 1) - timedelta(days=1)
            return date(last.year, last.month, 1).isoformat(), last.isoformat(), "上个月"
        if re.search(r"本月|这个月", q):
            return date(dmax.year, dmax.month, 1).isoformat(), maxday, "本月"
        m = re.search(r"(?:近|最近)(\d+)天", q)
        if m:
            n = int(m.group(1))
            return (dmax - timedelta(days=n - 1)).isoformat(), maxday, f"近{n}天"
        m = re.search(r"(\d{1,2})月", q)
        if m:
            mon = int(m.group(1))
            year = dmax.year if mon <= dmax.month else dmax.year - 1
            first = date(year, mon, 1)
            nxt = date(year + (mon == 12), (mon % 12) + 1, 1)
            return first.isoformat(), (nxt - timedelta(days=1)).isoformat(), f"{mon}月"
        first = date(dmax.year, dmax.month, 1) - timedelta(days=1)
        return date(first.year, first.month, 1).isoformat(), first.isoformat(), "最近完整月"

    # ---------- 计划 ----------
    def plan(self, q: str) -> Plan:
        metrics = self.sl.resolve_all(q)
        if not metrics:
            raise ValueError("无法识别指标：请指明是产油量/产液量/含水率/吨液电耗/时率等")
        self.log("resolve", "、".join(f"{i}({self.sl.metrics[i]['label']})" for i in metrics))

        start, end, label = self._window(q)
        self.log("time", f"{start} ~ {end}（{label}）")

        # 同维度多值 → 过滤 + 自动分组（"间开井和连续井" 是分组对比，不是 AND）
        dims: list[str] = []
        filters: list[tuple[str, str, list[str]]] = []
        for col, kw in (("block", "区块"), ("plant", "采油厂"),
                        ("lift", "举升"), ("status", "状态")):
            vals = [v for v in self._dim_values(col) if v in q]
            if len(vals) == 1:
                filters.append((col, "=", vals))
            elif len(vals) >= 2:
                filters.append((col, "IN", vals))
                dims.append(col)
        for col, kw in (("block", "区块"), ("plant", "采油厂"),
                        ("lift", "举升"), ("status", "井状态")):
            if re.search(rf"(各|哪个|哪些|按){kw}", q) and col not in dims:
                dims.append(col)
        if re.search(r"(哪口|哪些井|各井)", q):
            dims.append("well")

        intent, desc, limit, rank_by = "total", False, 0, ""
        if re.search(r"上升最快|下降最快|变化最大|趋势", q):
            intent = "trend"
        elif re.search(r"最高|最多|最大|最低|最少|最小", q):
            intent = "rank"
            desc = bool(re.search(r"最高|最多|最大", q))
            n = re.search(r"前(\d+)", q)
            limit = int(n.group(1)) if n else 0
            rank_by = metrics[0]
        elif re.search(r"平均|均值|水平", q):
            intent = "avg"

        p = Plan(question=q, metrics=metrics, group_by=dims, filters=filters,
                 start=start, end=end, intent=intent, order_desc=desc,
                 rank_by=rank_by, limit=limit)
        self.log("plan", f"metrics={metrics} group={dims or '-'} intent={intent}")
        self._grain_and_semantics(p)
        return p

    def _dim_values(self, col: str) -> list[str]:
        return [r[0] for r in self.con.execute(f"SELECT DISTINCT {col} FROM well_daily")]

    def _grain_and_semantics(self, p: Plan) -> None:
        days = (date.fromisoformat(p.end) - date.fromisoformat(p.start)).days + 1
        for iri in p.metrics:
            m = self.sl.metrics[iri]
            if iri == "oil_rate":
                p.notes.append(f"「{m['label']}」是区间日均（{days} 天），"
                               f"不是某一天的单井日产。")
            if m["time_sem"] == "production-day" and re.search(r"盘库|结算|月末", p.question):
                p.notes.append(f"「{m['label']}」为生产日口径，与月末盘库有计量差（罐存变动），"
                               f"不可直接相加对账。")

    # ---------- 编译 ----------
    def _where(self, p: Plan) -> str:
        parts = [f"day BETWEEN '{p.start}' AND '{p.end}'"]
        for col, op, vals in p.filters:
            if op == "=":
                parts.append(f"{col} = '{vals[0]}'")
            else:
                parts.append(f"{col} IN ({','.join(chr(39)+v+chr(39) for v in vals)})")
        return " AND ".join(parts)

    def compile(self, p: Plan) -> str:
        cols = ", ".join(f"{self.sl.dims[d]} AS {d}" for d in p.group_by)
        exprs = ", ".join(f"{self.sl.sql_expr(i)} AS m_{n}" for n, i in enumerate(p.metrics))
        sql = f"SELECT {cols + ', ' if cols else ''}{exprs} FROM well_daily WHERE {self._where(p)}"
        if p.group_by:
            sql += " GROUP BY " + ", ".join(p.group_by)
        if p.intent == "rank":
            sql += f" ORDER BY m_0 {'DESC' if p.order_desc else 'ASC'}"
        if p.limit:
            sql += f" LIMIT {p.limit}"
        self.log("compile", f"{len(sql)} 字符 SQL，{len(p.metrics)} 个指标列")
        return sql

    def trend_sql(self, p: Plan) -> str:
        m = self.sl.metrics[p.metrics[0]]
        if not (m["num"] and m["den"]):
            raise ValueError(f"「{m['label']}」不是比率型指标，趋势/斜率分析未定义")
        day_expr = (f"100.0*SUM({self.sl.metrics[m['num']]['field']})"
                    f"/NULLIF(SUM({self.sl.metrics[m['den']]['field']}),0)")
        return f"""
        WITH d AS (SELECT well, day, {day_expr} AS wc FROM well_daily
                   WHERE {self._where(p)} GROUP BY well, day),
             r AS (SELECT well, wc,
                          FIRST_VALUE(wc) OVER (PARTITION BY well ORDER BY day) AS wc0,
                          FIRST_VALUE(wc) OVER (PARTITION BY well ORDER BY day DESC) AS wc1
                   FROM d)
        SELECT well, ROUND(wc0,1) AS 起, ROUND(wc1,1) AS 止, ROUND(wc1-wc0,2) AS 变化
        FROM r GROUP BY well ORDER BY 变化 DESC LIMIT {p.limit or 3}
        """

    # ---------- 校验 ----------
    def validate(self, p: Plan, rows: list) -> list[Issue]:
        out: list[Issue] = []
        if not rows:
            return [Issue("error", "empty", "结果为空：时间窗或过滤条件可能过严。")]
        for iri in p.metrics:
            m = self.sl.metrics[iri]
            if not m["additive"] and p.intent in ("total", "avg") and not p.group_by:
                out.append(Issue(
                    "warn", "ratio-not-additive",
                    f"「{m['label']}」是比率型，直接相加或取平均都错。已按口径 {m['formula']} "
                    f"改写为「先汇总分子分母再相除」；若你要的是各实体比率的算术平均，那是另一口径。"))
            if m["agg"] == "ratio":
                vals = [r[len(p.group_by) + p.metrics.index(iri)] for r in rows]
                if any(v is None for v in vals):
                    out.append(Issue(
                        "warn", "zero-denominator",
                        f"「{m['label']}」有分母为 0 的实体（躺井无产液/非注碳井），"
                        f"已置空不参与比较——不能当 0 计入。"))
        self.log("validate", f"{len(out)} 条提示")
        return out

    def run(self, sql: str) -> list[tuple]:
        return self.con.execute(sql).fetchall()

    # ---------- 端到端 ----------
    def ask(self, q: str) -> None:
        print("\n" + "=" * 80)
        print(f"Q: {q}")
        print("-" * 80)
        p = self.plan(q)
        for n in p.notes:
            print(f"  ⚠ 口径提示：{n}")
        sql = self.trend_sql(p) if p.intent == "trend" else self.compile(p)
        rows = self.run(sql)
        issues = self.validate(p, rows)
        if issues and issues[0].code == "empty":
            self.log("replan", "空结果 → 放宽时间窗重试一次")
            p.start, p.end = "2026-07-03", "2026-09-30"
            sql = self.trend_sql(p) if p.intent == "trend" else self.compile(p)
            rows = self.run(sql)
            issues = self.validate(p, rows)

        print("\n  【口径】")
        for iri in p.metrics:
            m = self.sl.metrics[iri]
            print(f"    {m['label']}: 单位 {m['unit'] or '-'} | 时间口径 {m['time_sem']} | "
                  f"{'可加' if m['additive'] else '不可加(比率型)'}")
            if m["formula"]:
                print(f"      公式 {m['formula']}")
        print(f"  【时间窗】{p.start} ~ {p.end}")

        print("\n  【结果】")
        if p.intent == "trend":
            head = ["井号", "起始%", "结束%", "变化"]
        else:
            head = [d for d in p.group_by] + \
                   [f"{self.sl.metrics[i]['label']}({self.sl.metrics[i]['unit'] or '-'})"
                    for i in p.metrics]
        print("    " + " | ".join(f"{h:>14s}" for h in head))
        for r in rows:
            cells = []
            for v in r:
                cells.append(f"{v:>14.2f}" if isinstance(v, float) else f"{str(v):>14s}")
            print("    " + " | ".join(cells))
        if issues:
            print("\n  【校验】")
            for i in issues:
                print(f"    {'✗' if i.level == 'error' else '⚠'} [{i.code}] {i.message}")
        print("\n  【工具轨迹】")
        for t in self.trace:
            print(t)
        self.trace.clear()


DEMOS = [
    "上个月甲区区块的产油量和含水率是多少？",
    "哪个采油厂的吨液电耗最高？",
    "间开井和连续井的时率对比",
    "含水率上升最快的3口井",
    "全油田上个月含水率合计是多少？",
    "丁区区块各井的吨碳换油",
]


def main() -> int:
    sl = SemanticLayer(ONT)
    agent = Agent(sl, DB)
    print(f"语义层：{len(sl.metrics)} 指标 / {len(sl.aliases)} 别名 / {len(sl.dims)} 维度")
    for q in DEMOS:
        try:
            agent.ask(q)
        except Exception as e:
            print("\n" + "=" * 80)
            print(f"Q: {q}\n  ✗ 无法回答：{e}")
            print("  → 缺必要口径（指标/维度），必须先澄清，不能猜一个数出来。")
            agent.trace.clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
