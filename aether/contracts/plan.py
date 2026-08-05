"""动作计划契约 — ER2（小脑）下发给调度器的编排结构

设计要点（源自架构评审）：
    序号 seq 只解决「先后」，不解决「能否并行」。
    因此每个动作声明其占用的身体通道（Channel），调度器据此加锁。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_plan_counter = itertools.count(1)


class Channel(str, Enum):
    """身体资源通道 — 互斥锁的粒度"""
    SPEECH = "speech"
    FACE = "face"
    UPPER_BODY = "upper_body"
    LOWER_BODY = "lower_body"
    MOVEMENT = "movement"


class ActionKind(str, Enum):
    """调度器可执行的原子动作类型"""
    SPEAK = "speak"                    # TTS → 虚拟麦
    EXPRESSION = "expression"          # 表情 OSC 参数
    PLAY_ACTION = "play_action"        # 预录动作 ActionID
    GENERATE_MOTION = "generate_motion"  # DART 动态生成
    MOVE = "move"                      # 移动 OSC 输入轴
    WAIT = "wait"                      # 显式停顿


#: 动作类型 → 占用通道。DART 全身与预录全身动画都占据整个躯体。
CHANNEL_USAGE: dict[ActionKind, frozenset[Channel]] = {
    ActionKind.SPEAK: frozenset({Channel.SPEECH}),
    ActionKind.EXPRESSION: frozenset({Channel.FACE}),
    ActionKind.PLAY_ACTION: frozenset({Channel.UPPER_BODY}),
    ActionKind.GENERATE_MOTION: frozenset({Channel.UPPER_BODY}),
    ActionKind.MOVE: frozenset({Channel.MOVEMENT}),
    ActionKind.WAIT: frozenset(),
}

#: 需要整个躯体的作用域（由 args.scope / args.full_body 触发）
FULL_BODY_CHANNELS = frozenset({
    Channel.UPPER_BODY, Channel.LOWER_BODY, Channel.MOVEMENT,
})


@dataclass(frozen=True, slots=True)
class PlanStep:
    """计划中的一步"""
    seq: int
    action: ActionKind
    args: dict[str, Any] = field(default_factory=dict)
    parallel: bool = False   # 与前一步并发（前提是通道不冲突）

    def channels(self) -> frozenset[Channel]:
        """本步实际占用的通道"""
        scope = str(self.args.get("scope", "")).lower()
        if self.action is ActionKind.GENERATE_MOTION and scope == "full_body":
            return FULL_BODY_CHANNELS
        if self.action is ActionKind.PLAY_ACTION and self.args.get("full_body"):
            return FULL_BODY_CHANNELS
        return CHANNEL_USAGE.get(self.action, frozenset())


def estimated_step_seconds(step: PlanStep) -> float:
    """返回计划预算使用的保守时长估计，并拒绝无效时间参数。"""
    for key in ("duration_s", "duration", "timeout"):
        if key not in step.args:
            continue
        try:
            value = float(step.args[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第{step.seq}步 {key} 不是有效数字") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"第{step.seq}步 {key} 必须是有限非负数")

    if "duration_s" in step.args:
        return float(step.args["duration_s"])
    if "duration" in step.args:
        return float(step.args["duration"])
    if step.action is ActionKind.SPEAK:
        text = str(step.args.get("text", ""))
        return max(0.4, len(text) / 8.0)
    if step.action is ActionKind.WAIT:
        return 0.5
    if step.action in (ActionKind.EXPRESSION, ActionKind.PLAY_ACTION):
        return 2.0
    return 2.0


def estimated_plan_seconds(plan: "Plan") -> float:
    """按调度语义估算墙钟时长；parallel 仅在通道不冲突时取并发最大值。"""
    total = 0.0
    group: list[PlanStep] = []
    occupied: frozenset[Channel] = frozenset()

    def flush() -> float:
        return max((estimated_step_seconds(item) for item in group), default=0.0)

    for step in plan.steps:
        channels = step.channels()
        if not group:
            group = [step]
            occupied = channels
        elif step.parallel and not (occupied & channels):
            group.append(step)
            occupied = occupied | channels
        else:
            total += flush()
            group = [step]
            occupied = channels
    return total + flush()


@dataclass(frozen=True, slots=True)
class Plan:
    """一个动作计划 —— 约束：总时长不超过 5 秒，长行为拆成多个计划滚动执行"""
    steps: tuple[PlanStep, ...]
    plan_id: str = ""
    source: str = "cerebellum"

    def __post_init__(self) -> None:
        if not self.plan_id:
            object.__setattr__(self, "plan_id", f"p{next(_plan_counter)}")
        object.__setattr__(
            self, "steps", tuple(sorted(self.steps, key=lambda s: s.seq))
        )


class StepStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class StepResult:
    seq: int
    action: ActionKind
    status: StepStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PlanResult:
    """计划执行结果 —— 同时回流 ER2（function_response）与 StateStore"""
    plan_id: str
    status: StepStatus
    steps: tuple[StepResult, ...] = ()
    detail: str = ""

    def summary(self) -> str:
        if self.status is StepStatus.SUCCESS:
            return f"{self.plan_id} 全部完成"
        failed = [s for s in self.steps if s.status is StepStatus.FAILED]
        if failed:
            first = failed[0]
            return f"{self.plan_id} 第{first.seq}步失败: {first.detail or '未知原因'}"
        return f"{self.plan_id} {self.status.value}: {self.detail}"
