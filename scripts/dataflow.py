"""
dataflow.py —— 数据流与控制流的时延/容量核算

回答两个工程问题（不是画图，是算账）：
  1) 控制流的时延预算，能不能满足 L4 八类异常的时间窗？
     —— 逐段拆解：采样周期 + 边缘处理 + 策略匹配 + 执行机构行程 + 二次确认
  2) 数据流的带宽与存储，能不能落得起？
     —— 逐路拆解：单井/单站日均量、万井规模、边缘缓存 30 天、中心 3 年

时延门槛直接读 ontology/l4-core.ttl（detectMinutes / mustReachSafeStateIn），
保证"文档里的时间窗"和"算出来的时延"用的是同一份数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

from rdflib import Graph, Namespace, RDF, URIRef

ROOT = Path(__file__).resolve().parents[1]
ONT = ROOT / "ontology"
UM = Namespace("https://example.org/um#")
AN = Namespace("https://example.org/um/anomaly/")

# ── 控制流时延分解（秒）：[环节, 耗时, 说明] ───────────────────────────────
# 铁律：安全联锁(SIS)在最内环，硬接线/独立逻辑，**不经平台、不经网络**
PATH = {
    "A1": [("UPS 状态检测", 6, "市电失电由 UPS 干接点/ voltage relay 检出"),
           ("安全停机 + 非必要负荷切除", 4, "边缘控制器逐路切负荷"),
           ("自动重启", 30, "市电恢复后软启动，非安全关键")],
    "A2": [("心跳超时判定", 180, "3 个心跳周期，避免抖动误判"),
           ("本地保守策略接管", 5, "边缘已在运行，无需切换"),
           ("北斗短报文告警", 60, "含排队与发送窗口")],
    "A3": [("阀位/流量偏差检测", 120, "偏差持续 2 个采样周期才判卡涩"),
           ("旁通阀行程（气动）", 5, "气动执行机构 2–5s；电动阀需 10–30s"),
           ("降级配产下发", 20, "边缘本地闭环")],
    "A4": [("压力变送器采样", 1, "1s 周期"),
           ("SIS 联锁逻辑", 0.2, "独立控制器，硬接线，不经 SCADA"),
           ("切断阀行程（气动）", 3, "全行程 3s"),
           ("泄压/放空", 2, "放空阀联开")],
    "A5": [("可燃气体/H2S 采样", 1, "1s 周期"),
           ("SIS 联锁 + 切断", 3.2, "同一联锁回路"),
           ("放空 + 声光", 3, "与切断同步"),
           ("派单", 5, "告警经中心自动生成工单")],
    "A6": [("多参数交叉校验窗口", 600, "10 分钟一致性窗口，抗单点漂移"),
           ("剔除 + 保守降级", 5, "切换备用传感器/回退到保守参数")],
    "A7": [("周界探测", 1, "红外/雷达"),
           ("视频 AI 复核", 2, "本地 GPU 推理，不上云"),
           ("声光驱离 + 派单", 3, "驱离与告警同步")],
    "A8": [("功图/气液比采集", 600, "功图 10 分钟一张"),
           ("特征计算 + 气窜判据", 60, "边缘轻量模型"),
           ("降载 + 注入联动", 30, "远程调参与注入端协同"),
           ("二次确认（避免误降载）", 60, "再取一张功图比对")],
}

# ── 数据流：单井/单站的数据源（压缩后字节/样本）───────────────────────────
# (名称, 采样间隔 s, 每样本压缩字节, 归属, 备注)
STREAMS_WELL = [
    ("电参(电流/电压/有功)", 60, 16 * 6, "井", "本地 1s 计算，1min 上送均值"),
    ("功图(载荷/位移)", 600, 1600, "井", "每冲次 200 点 × 2 通道"),
    ("套压/油压/回压", 60, 16 * 3, "井", "联锁与气窜判据"),
    ("井口温度", 60, 16, "井", "寒区防冻"),
    ("启停/阀位(事件)", 0, 120, "井", "事件驱动，日均约 8 条"),
    ("动液面(按需)", 86400, 32, "井", "稀疏测恢复，不追求全覆盖"),
]
STREAMS_STATION = [
    ("流量/液位", 60, 16 * 4, "站", "计量与配产"),
    ("气体探测(H2S/LEL)", 1, 16, "站", "SIS 输入，本地闭环"),
    ("阀位/泵状态", 60, 16 * 8, "站", "流程切换"),
    ("电能质量", 60, 16 * 4, "站", "与新能源协同"),
    ("视频事件(截图)", 0, 200_000, "站", "日均 3 张，仅在告警时上传"),
    ("边缘自愈日志", 60, 200, "站", "策略命中/动作/验证结果"),
]


def en(s: float) -> str:
    """秒数转人类可读的时长（保留一位小数，避免 4.08333min 这种读数）。"""
    if s < 60:
        return f"{s:g}s"
    if s < 3600:
        m = s / 60
        return f"{m:.0f}min" if abs(m - round(m)) < 0.05 else f"{m:.1f}min"
    h = s / 3600
    return f"{h:.0f}h" if abs(h - round(h)) < 0.05 else f"{h:.1f}h"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def latency_table() -> int:
    g = Graph()
    for f in ("unmanned-core.ttl", "l4-core.ttl"):
        g.parse(ONT / f, format="turtle")

    print("=" * 92)
    print("一、控制流时延预算 vs L4 时间窗（读自 l4-core.ttl）")
    print("=" * 92)
    print(f"{'异常':4s} {'分解':52s} {'合计':>8s} {'时间窗':>8s} {'余量':>8s} {'结论':>6s}")
    print("-" * 92)
    bad = 0
    for a, segs in PATH.items():
        node = AN[a]
        limit_min = float(g.value(node, UM.mustReachSafeStateIn) or 0)
        detect_min = float(g.value(node, UM.detectMinutes) or 0)
        total = sum(s for _, s, _ in segs)
        limit = limit_min * 60
        margin = (limit - total) / limit * 100
        ok = total <= limit
        bad += 0 if ok else 1
        # 最内环必须是 SIS：这里标注检测段是否受 SIS 覆盖
        detail = " → ".join(f"{n}({en(s)})" for n, s, _ in segs)
        shown = detail if len(detail) <= 54 else detail[:53] + "…"
        print(f"{a:4s} {shown:54s} {en(total):>8s} {en(limit):>8s} "
              f"{margin:5.0f}% {'✅' if ok else '❌':>6s}")
        # 台账的 detectMinutes 与分解的第一段作一致性提示
        if detect_min and abs(detect_min * 60 - segs[0][1]) / (detect_min * 60) > 2:
            print(f"     ⚠ 台账 detectMinutes={detect_min:g}min 与实测检测段 {en(segs[0][1])} 不一致，需对齐")
    print("-" * 92)
    print("备注：A4/A5 的切断必须由 SIS 完成（独立控制器 + 硬接线），"
          "若走 SCADA 路径需额外 0.5–2s 且受网络影响。")
    return bad


def volume_table() -> None:
    print()
    print("=" * 92)
    print("二、数据流容量（压缩后口径，用于选通信与存储）")
    print("=" * 92)

    def daily(streams) -> float:
        total = 0.0
        for name, interval, size, *_ in streams:
            n = 8 if interval == 0 else 86400 / interval      # 事件类按日均 8 条
            total += n * size
        return total

    well = daily(STREAMS_WELL)
    station = daily(STREAMS_STATION)
    print(f"{'数据源':28s} {'间隔':>8s} {'单样本':>9s} {'日均(单对象)':>13s}")
    print("-" * 92)
    for name, interval, size, *_ in STREAMS_WELL + STREAMS_STATION:
        per = "事件" if interval == 0 else en(interval)
        print(f"{name:28s} {per:>8s} {human_bytes(size):>9s} "
              f"{human_bytes(size * (8 if interval == 0 else 86400 / interval)):>13s}")
    print("-" * 92)
    print(f"单井日均 {human_bytes(well)} ｜ 单站日均（含视频事件） {human_bytes(station)}")

    for wells, stations in ((1000, 60), (10000, 400), (30000, 900)):
        d = wells * well + stations * station
        print(f"  {wells:>5d} 井 + {stations:>3d} 站 → 日增 {human_bytes(d)}/日，"
              f"年增 {human_bytes(d * 365)}，3 年 {human_bytes(d * 365 * 3)}")
    print()
    avg_bps = well * 8 / 86400
    peak_bps = 1600 * 8 / 600
    print(f"带宽（单井）：均值 {avg_bps:.0f} bit/s ≈ {avg_bps/1000:.2f} kbps；"
          f"功图突发 {peak_bps:.0f} bit/s ≈ {peak_bps/1000:.2f} kbps"
          f" → 4G/NB-IoT 绰绰有余，瓶颈在**可靠性与时延**，不在带宽")
    print(f"边缘缓存（断网 30 天，20 井/站）：{human_bytes((well * 20 + station) * 30)}"
          f" → 一张 64 GB 工业级 SD 卡即可，无需上云即可自治")


def main() -> int:
    bad = latency_table()
    volume_table()
    print()
    print("=" * 92)
    print("结论：控制流时延全部在时间窗内 ✓" if not bad else f"有 {bad} 类异常超出时间窗 ✗")
    print("=" * 92)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
