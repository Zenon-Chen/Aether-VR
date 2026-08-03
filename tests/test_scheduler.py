"""调度器：序号 + 通道锁"""

import asyncio

import pytest

from aether.contracts.plan import ActionKind, Plan, PlanStep, StepStatus
from aether.core import Scheduler, StateStore


def _record(log, tag, delay=0.05):
    async def run(step):
        log.append(("start", tag))
        await asyncio.sleep(delay)
        log.append(("end", tag))
        return tag
    return run


@pytest.mark.asyncio
async def test_parallel_when_channels_disjoint():
    """speak(speech) 与 play_action(upper_body) 通道不冲突，应并发"""
    state, log = StateStore(), []
    sched = Scheduler(state)
    sched.register(ActionKind.SPEAK, _record(log, "speak"))
    sched.register(ActionKind.PLAY_ACTION, _record(log, "wave"))

    await sched.submit(Plan(steps=(
        PlanStep(1, ActionKind.SPEAK, {"text": "hi"}),
        PlanStep(2, ActionKind.PLAY_ACTION, {"name": "wave"}, parallel=True),
    )))

    assert log.index(("start", "wave")) < log.index(("end", "speak"))


@pytest.mark.asyncio
async def test_serial_when_channels_conflict():
    """两个 upper_body 动作即使标 parallel 也必须串行"""
    state, log = StateStore(), []
    sched = Scheduler(state)
    sched.register(ActionKind.PLAY_ACTION, _record(log, "a"))
    sched.register(ActionKind.GENERATE_MOTION, _record(log, "b"))

    await sched.submit(Plan(steps=(
        PlanStep(1, ActionKind.PLAY_ACTION, {"name": "wave"}),
        PlanStep(2, ActionKind.GENERATE_MOTION, {"prompt": "x"}, parallel=True),
    )))

    assert log.index(("end", "a")) < log.index(("start", "b"))


@pytest.mark.asyncio
async def test_full_body_blocks_movement():
    """DART full_body 占据上下半身与移动通道"""
    step = PlanStep(1, ActionKind.GENERATE_MOTION, {"prompt": "dance", "scope": "full_body"})
    from aether.contracts.plan import Channel
    assert Channel.MOVEMENT in step.channels()
    assert Channel.LOWER_BODY in step.channels()


@pytest.mark.asyncio
async def test_failure_skips_remaining_steps():
    state = StateStore()
    sched = Scheduler(state)

    async def boom(step):
        raise RuntimeError("目标丢失")

    ran = []

    async def ok(step):
        ran.append(step.seq)

    sched.register(ActionKind.PLAY_ACTION, boom)
    sched.register(ActionKind.SPEAK, ok)

    result = await sched.submit(Plan(steps=(
        PlanStep(1, ActionKind.PLAY_ACTION, {"name": "wave"}),
        PlanStep(2, ActionKind.SPEAK, {"text": "hi"}),
    )))

    assert result.status is StepStatus.FAILED
    assert "目标丢失" in result.summary()
    assert ran == []


@pytest.mark.asyncio
async def test_missing_executor_fails_cleanly():
    state = StateStore()
    sched = Scheduler(state)
    result = await sched.submit(Plan(steps=(PlanStep(1, ActionKind.MOVE, {}),)))
    assert result.status is StepStatus.FAILED


@pytest.mark.asyncio
async def test_channels_released_after_plan():
    state = StateStore()
    sched = Scheduler(state)
    sched.register(ActionKind.SPEAK, _record([], "s"))
    await sched.submit(Plan(steps=(PlanStep(1, ActionKind.SPEAK, {"text": "hi"}),)))
    assert state.facts.busy_channels() == ()
