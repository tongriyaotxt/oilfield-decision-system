"""
scale_test.py —— 规模压测：决策层的吞吐能撑到多少口井

测的是"每 12h 一轮，对全油田算一遍决策"的成本：
  特征聚合（SQLite）→ 规则判定 → 回执 RDF 构建。门禁（SHACL）单独计时，因为它最贵。
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import decision_agent as da      # noqa: E402

BUILD = ROOT / "build"


def make_recs(n: int) -> list[dict]:
    rnd = random.Random(3)
    out = []
    for i in range(n):
        qth = rnd.choice([12, 18, 26, 45, 60])
        out.append(dict(well=f"W{i:05d}", status="连续", trait="常规", days=24,
                        coverage=1.0, eff=rnd.uniform(0.05, 0.95),
                        duty=rnd.uniform(0.05, 1.0), unit_power=rnd.uniform(10, 120),
                        oil_pd=rnd.uniform(0.01, 2.0), liquid_pd=rnd.uniform(0.2, 20),
                        liquid_sum=rnd.uniform(5, 600), q_theory=qth,
                        water_cut=rnd.uniform(0.6, 0.95)))
    return out


def main() -> int:
    rules, _ = da.load_rules()
    print(f"{'井数':>7s} {'决策耗时':>10s} {'吞吐(条/秒)':>12s} {'单轮内存RDF':>12s}")
    print("-" * 48)
    results = []
    for n in (1000, 5000, 10000):
        recs = make_recs(n)
        t0 = time.perf_counter()
        decisions = [(r, da.decide(r, rules, 10)) for r in recs]
        dt = time.perf_counter() - t0
        g = da.to_rdf(decisions, "2026-10-01", "2026-10-02")
        results.append((n, dt, n / dt, len(g)))
        print(f"{n:7d} {dt*1000:9.0f}ms {n/dt:11.0f} {'':s} {len(g):10d} 三元组")
    print("-" * 48)
    print("口径：纯 CPU 单进程（Python）；门禁 SHACL 是 O(决策数) 的额外开销，")
    print("      生产上建议只对'动作非 Hold'的决策跑门禁（本轮约占 40%）。")
    for n, dt, thr, triples in results:
        print(f"  {n} 井 → 每 12h 一轮 {dt*1000:.0f}ms；按 2 轮/日算，日成本 {(dt*2):.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
