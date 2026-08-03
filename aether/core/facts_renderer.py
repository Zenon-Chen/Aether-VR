"""事实渲染 — 把结构化事实变成一段自然语言

同一份事实，一个来源，两个去处：
    → 大脑输入（作为其感知的一部分）
    → 心跳事实段（喂给 ER2）
"""

from __future__ import annotations

import time

from ..contracts.facts import WorldFacts
from ..contracts.plan import Channel

_CHANNEL_LABELS: dict[Channel, str] = {
    Channel.SPEECH: "语音",
    Channel.FACE: "面部",
    Channel.UPPER_BODY: "上半身",
    Channel.LOWER_BODY: "下半身",
    Channel.MOVEMENT: "移动",
}


def render_facts(facts: WorldFacts, now: float | None = None) -> str:
    """把事实快照渲染为供模型阅读的中文摘要"""
    now = time.monotonic() if now is None else now
    lines: list[str] = []

    if facts.emergency_stopped:
        lines.append("⚠ 已紧急停止，所有动作已中断。")

    lines.append(_render_body(facts, now))

    if facts.active_plan is not None:
        plan = facts.active_plan
        lines.append(
            f"计划{plan.plan_id}：共{plan.total}步，当前第{plan.step}步（{plan.status}）。"
        )
    else:
        lines.append("当前没有执行中的计划。")

    if facts.recent_results:
        lines.append("最近结果：" + "；".join(facts.recent_results) + "。")

    if facts.last_speech:
        lines.append(f"角色刚才说了：「{facts.last_speech}」")

    if facts.reflex_log:
        lines.append("反射记录：" + "；".join(facts.reflex_log) + "。")

    degraded = facts.services.degraded()
    lines.append(
        "所有服务正常。" if not degraded
        else "服务异常：" + "、".join(degraded) + "。"
    )

    return "\n".join(lines)


def _render_body(facts: WorldFacts, now: float) -> str:
    busy_parts: list[str] = []
    idle_labels: list[str] = []

    for ch in Channel:
        state = facts.channel(ch)
        label = _CHANNEL_LABELS[ch]
        if state.busy:
            remaining = state.remaining(now)
            holder = state.holder or "未知"
            suffix = f"，还剩{remaining:.1f}s" if remaining > 0 else ""
            busy_parts.append(f"{label}执行中（{holder}{suffix}）")
        else:
            idle_labels.append(label)

    if not busy_parts:
        return "身体：全部空闲。"
    tail = "，其余空闲。" if idle_labels else "。"
    return "身体：" + "，".join(busy_parts) + tail
