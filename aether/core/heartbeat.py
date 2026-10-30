"""心跳拼装器 — 纯代码模板

发送规则（架构评审结论）：
    空闲时              每 2 秒（紧随一帧图像之后）
    用户语音回合进行中   不发（避免打断 ER2 生成）
    ER2 正在生成中       推迟到本轮结束
    长动作执行中         每 3~5 秒，仅携带 [STATUS]，指令改为「仅确认」
    大脑 wake_er2=true  立即发（事件唤醒，不等周期）
    执行失败            立即发 [STATUS] 失败详情
"""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..contracts.facts import WorldFacts
from ..contracts.observation import BrainOutput
from .facts_renderer import render_facts

logger = logging.getLogger(__name__)

IDLE_INTERVAL_S = 2.0
BUSY_INTERVAL_S = 4.0

_INSTRUCTION_NORMAL = (
    "若正在执行计划且无新事件：调用 ack。\n"
    "若有值得回应的事件：编排计划，台词优先用建议台词。\n"
    "禁止与执行中计划冲突的动作。"
)
_INSTRUCTION_ACK_ONLY = "当前有计划正在执行，仅确认（调用 ack），不要下发新计划。"


class Gate(str, Enum):
    """当前是否允许发心跳，以及为什么"""
    ALLOW = "allow"
    USER_SPEAKING = "user_speaking"
    ER2_GENERATING = "er2_generating"
    TOO_SOON = "too_soon"
    NO_CHANGE = "no_change"


@dataclass(frozen=True, slots=True)
class HeartbeatDecision:
    gate: Gate
    text: Optional[str] = None

    @property
    def should_send(self) -> bool:
        return self.gate is Gate.ALLOW and bool(self.text)


class HeartbeatAssembler:
    """把事实 + 观察 + 建议拼装为一条心跳文本"""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._last_sent_at = 0.0
        self._last_digest = ""
        self._last_woken_output_id: Optional[int] = None

    def status_only(self, facts: WorldFacts, note: str) -> str:
        """执行进度 / 失败详情的即时状态注入"""
        return (
            f"[STATUS #{next(self._counter)}]\n"
            f"{note}\n"
            f"—事实—\n{render_facts(facts)}"
        )

    def assemble(
        self,
        facts: WorldFacts,
        brain: Optional[BrainOutput],
        *,
        user_speaking: bool = False,
        er2_generating: bool = False,
        force: bool = False,
        now: Optional[float] = None,
    ) -> HeartbeatDecision:
        now = time.monotonic() if now is None else now

        if user_speaking:
            return HeartbeatDecision(Gate.USER_SPEAKING)
        if er2_generating:
            return HeartbeatDecision(Gate.ER2_GENERATING)

        brain_wake = bool(
            brain is not None
            and brain.wake_er2
            and brain.output_id != self._last_woken_output_id
        )
        wake = force or brain_wake
        interval = BUSY_INTERVAL_S if facts.active_plan is not None else IDLE_INTERVAL_S
        if not wake and (now - self._last_sent_at) < interval:
            return HeartbeatDecision(Gate.TOO_SOON)

        text = self._compose(facts, brain)
        digest = _digest(text)
        # 无变化且非事件唤醒时不重复打扰 ER2
        if not wake and digest == self._last_digest:
            return HeartbeatDecision(Gate.NO_CHANGE)

        self._last_sent_at = now
        self._last_digest = digest
        if brain_wake and brain is not None:
            self._last_woken_output_id = brain.output_id
        return HeartbeatDecision(Gate.ALLOW, text)

    # ── 内部 ───────────────────────────────────────────────

    def _compose(self, facts: WorldFacts, brain: Optional[BrainOutput]) -> str:
        blocks = [
            f"[HEARTBEAT #{next(self._counter)}]",
            "—事实(代码)—",
            render_facts(facts),
        ]

        if brain and brain.observations:
            blocks.append("—观察(大脑)—")
            blocks.extend(f"· {ob.render()}" for ob in brain.observations)

        if brain and (brain.suggested_reply or brain.suggested_intent):
            blocks.append("—建议(大脑)—")
            if brain.suggested_reply:
                blocks.append(f"建议台词: {brain.suggested_reply}")
            if brain.suggested_intent:
                blocks.append(f"建议意图: {brain.suggested_intent}")

        blocks.append("—指令(固定)—")
        blocks.append(
            _INSTRUCTION_ACK_ONLY if facts.active_plan is not None
            else _INSTRUCTION_NORMAL
        )
        return "\n".join(blocks)


def _digest(text: str) -> str:
    """忽略首行编号后的内容指纹"""
    body = text.split("\n", 1)[1] if "\n" in text else text
    return str(hash(body))
