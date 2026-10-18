"""
llm_planner.py —— AI 接入点①：自然语言 → 计划（Plan）

这是整套系统里**唯一**应该交给大模型的地方。
模型只输出一个结构化 Plan（JSON），之后的编译、执行、校验、门禁全部是确定性代码：
    模型负责"听懂人话"，不负责"算数"，也不负责"决定口径"。

三道护栏：
  1) 指标必须命中已注册口径（否则 PlanRejected，不回退到猜测）
  2) 维度/意图/日期必须符合枚举与格式
  3) 生成 SQL 前不再给模型任何自由度（SQL 由本体编译）

用法：
  python llm_planner.py --print-prompt                  # 打印喂给模型的提示词
  python llm_planner.py --plan-json '{...}' --question "..."   # 用模型产出的计划执行
  python llm_planner.py --question "..."                # 无模型时用规则解析（对照）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analytics_agent import Agent, SemanticLayer, DB, ONT, Plan   # noqa: E402
import decision_agent as da                                   # noqa: E402

ALLOWED_DIMS = {"block", "plant", "lift", "status", "well", "day", "month"}
ALLOWED_INTENT = {"total", "avg", "rank", "trend"}


class PlanRejected(Exception):
    """模型给了不合格的计划：拒绝执行，并把原因回给调用方（可让模型重试一次）。"""


def catalog(sl: SemanticLayer) -> str:
    lines = []
    for k, m in sorted(sl.metrics.items()):
        lines.append(f"- {k} | {m['label']} | 单位 {m['unit'] or '-'} | "
                     f"{'可加' if m['additive'] else '比率型不可加'} | "
                     f"时间口径 {m['time_sem']} | 公式 {m['formula'] or '源字段聚合'}")
    return "\n".join(lines)


def build_prompt(question: str, sl: SemanticLayer) -> str:
    return f"""你是油田生产数据分析的意图解析器。只做一件事：把用户问题解析成 JSON 计划。

可用指标（只能用列表里的 id，不得自造）：
{catalog(sl)}

可用维度：block(区块) / plant(采油厂) / lift(举升方式) / status(井状态) / well(井号) / day / month
意图 intent：
  total=区间汇总；avg=日均；rank=排序取前几；trend=按井看变化趋势

输出严格 JSON，不要解释、不要代码块：
{{"metrics":["指标id"],"group_by":["维度"],"filters":[{{"dim":"维度","op":"="或"IN","values":["值"]}}],
 "start":"YYYY-MM-DD","end":"YYYY-MM-DD","intent":"total|avg|rank|trend","order":"DESC|ASC","limit":0}}

规则：
- 问"合计/总计"但指标是比率型 → intent 用 total（后续由系统按 SUM(分子)/SUM(分母) 处理）
- 问"哪个X最高/最低" → intent=rank，order=DESC/ASC
- 问"上升最快/趋势" → intent=trend
- 时间窗：数据截止 2026-09-30；"上个月"=2026-08-01~2026-08-31
- 拿不准就少给字段，不要瞎编维度值

用户问题：{question}
"""


def parse_plan(obj: dict, sl: SemanticLayer) -> da.Plan:
    metrics = obj.get("metrics") or []
    if not isinstance(metrics, list) or not metrics:
        raise PlanRejected("缺少 metrics")
    for m in metrics:
        if m not in sl.metrics:
            raise PlanRejected(f"指标「{m}」未注册。可选：{', '.join(sorted(sl.metrics))[:200]}")
    dims = obj.get("group_by") or []
    for d in dims:
        if d not in ALLOWED_DIMS:
            raise PlanRejected(f"维度「{d}」不合法，可选：{sorted(ALLOWED_DIMS)}")
    intent = obj.get("intent", "total")
    if intent not in ALLOWED_INTENT:
        raise PlanRejected(f"intent「{intent}」不合法")
    for k in ("start", "end"):
        v = obj.get(k, "")
        if not (len(v) == 10 and v[4] == "-" and v[7] == "-"):
            raise PlanRejected(f"{k} 必须是 YYYY-MM-DD，收到「{v}」")
    filters = []
    for f in obj.get("filters") or []:
        d, op, vals = f.get("dim"), f.get("op", "="), f.get("values") or []
        if d not in ALLOWED_DIMS:
            raise PlanRejected(f"过滤维度「{d}」不合法")
        if op not in ("=", "IN"):
            raise PlanRejected(f"op「{op}」只支持 = 或 IN")
        if not vals:
            raise PlanRejected("filters.values 不能为空")
        filters.append((d, op, list(vals)))
    return Plan(question=obj.get("_question", "(from llm)"), metrics=list(metrics),
                   group_by=list(dims), filters=filters,
                   start=obj["start"], end=obj["end"], intent=intent,
                   order_desc=str(obj.get("order", "")).upper() != "ASC",
                   limit=int(obj.get("limit") or 0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", default="")
    ap.add_argument("--plan-json", default="")
    ap.add_argument("--plan-file", default="", help="从文件读模型输出的 JSON（推荐，避免 shell 转义）")
    ap.add_argument("--print-prompt", action="store_true")
    args = ap.parse_args()

    sl = SemanticLayer(ONT)
    if args.print_prompt:
        print(build_prompt(args.question or "上个月各采油厂的吨液电耗是多少？", sl))
        return 0

    agent = Agent(sl, DB)
    raw = args.plan_json
    if args.plan_file:
        raw = Path(args.plan_file).read_text(encoding="utf-8-sig")   # 容忍 BOM
    if raw:
        try:
            obj = json.loads(raw)
            obj.setdefault("_question", args.question)
            plan = parse_plan(obj, sl)
        except PlanRejected as e:
            print(f"  ✗ 计划被拒绝：{e}")
            print("  → 拒绝执行，不用猜测值回答（这是护栏，不是故障）")
            return 2
        print(f"\n[LLM 计划] metrics={plan.metrics} group={plan.group_by} "
              f"filters={plan.filters} {plan.start}~{plan.end} intent={plan.intent}")
        agent.log("llm.plan", "计划通过本体校验")
    else:
        plan = agent.plan(args.question)

    sql = agent.trend_sql(plan) if plan.intent == "trend" else agent.compile(plan)
    rows = agent.run(sql)
    issues = agent.validate(plan, rows)
    for iri in plan.metrics:
        m = sl.metrics[iri]
        print(f"  【口径】{m['label']} | 单位 {m['unit'] or '-'} | {m['time_sem']} | "
              f"{'可加' if m['additive'] else '比率型'}"
              + (f" | {m['formula']}" if m["formula"] else ""))
    print("  【结果】")
    for r in rows[:8]:
        print("    " + " | ".join(f"{v:.2f}" if isinstance(v, float) else str(v) for v in r))
    for i in issues:
        print(f"  {'✗' if i.level == 'error' else '⚠'} [{i.code}] {i.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
