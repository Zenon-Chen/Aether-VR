"""心跳闸门、事实渲染、planner 翻译、紧急停止"""

import pytest

from aether.contracts.observation import BrainOutput
from aether.contracts.plan import ActionKind, Channel
from aether.core import EmergencyStop, Gate, HeartbeatAssembler, StateStore, render_facts
from aether.services.cerebellum.planner import (
    PlanRejected, is_long_running, parse_tool_call,
)


# ── 心跳闸门 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_heartbeat_while_user_speaking():
    state, hb = StateStore(), HeartbeatAssembler()
    assert hb.assemble(state.facts, None, user_speaking=True).gate is Gate.USER_SPEAKING


@pytest.mark.asyncio
async def test_no_heartbeat_while_er2_generating():
    state, hb = StateStore(), HeartbeatAssembler()
    assert hb.assemble(state.facts, None, er2_generating=True).gate is Gate.ER2_GENERATING


@pytest.mark.asyncio
async def test_rate_limited_then_forced():
    state, hb = StateStore(), HeartbeatAssembler()
    assert hb.assemble(state.facts, None).should_send
    assert hb.assemble(state.facts, None).gate is Gate.TOO_SOON
    # wake_er2 立即穿透节流，但同一份 BrainOutput 只能唤醒一次，防止 2Hz 心跳风暴
    brain = BrainOutput(wake_er2=True, suggested_reply="你好呀")
    assert hb.assemble(state.facts, brain).should_send
    assert hb.assemble(state.facts, brain).gate is Gate.TOO_SOON
    # 新一轮 BrainOutput 再次带 wake_er2 时可再次唤醒
    brain2 = BrainOutput(wake_er2=True, suggested_reply="你好呀")
    assert hb.assemble(state.facts, brain2).should_send


@pytest.mark.asyncio
async def test_suggested_reply_appears_in_heartbeat():
    state, hb = StateStore(), HeartbeatAssembler()
    brain = BrainOutput(suggested_reply="你回来啦", suggested_intent="greet")
    decision = hb.assemble(state.facts, brain, force=True)
    assert "你回来啦" in decision.text
    assert "greet" in decision.text


@pytest.mark.asyncio
async def test_ack_only_instruction_when_plan_active():
    from aether.contracts.facts import ActivePlan
    state, hb = StateStore(), HeartbeatAssembler()
    await state.set_active_plan(ActivePlan("p1", 1, 3))
    decision = hb.assemble(state.facts, None, force=True)
    assert "仅确认" in decision.text


# ── 事实渲染 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_facts_reflect_channel_occupancy():
    state = StateStore()
    await state.acquire_channels(frozenset({Channel.UPPER_BODY}), "p1.s2", 2.0)
    text = render_facts(state.facts)
    assert "上半身执行中" in text and "p1.s2" in text


@pytest.mark.asyncio
async def test_facts_report_degraded_services():
    state = StateStore()
    await state.set_service("dart", "error")
    assert "dart" in render_facts(state.facts)


# ── planner ───────────────────────────────────────────────

def test_parse_execute_plan():
    plan = parse_tool_call("execute_plan", {"steps": [
        {"seq": 1, "action": "speak", "args": {"text": "hi"}},
        {"seq": 2, "action": "play_action", "args": {"name": "wave"}, "parallel": True},
    ]}, max_steps=6)
    assert len(plan.steps) == 2
    assert plan.steps[1].parallel is True


def test_full_body_action_is_marked_before_scheduling():
    plan = parse_tool_call("execute_plan", {"steps": [
        {"action": "play_action", "args": {"name": "bow"}},
        {"action": "play_action", "args": {"name": "wave"}},
    ]}, max_steps=6, full_body_actions=("bow", "sit", "stand"))

    assert plan.steps[0].args["full_body"] is True
    assert plan.steps[0].channels() == frozenset({
        Channel.UPPER_BODY, Channel.LOWER_BODY, Channel.MOVEMENT,
    })
    assert plan.steps[1].args["full_body"] is False
    assert plan.steps[1].channels() == frozenset({Channel.UPPER_BODY})


def test_single_play_action_marks_full_body_before_scheduling():
    bow = parse_tool_call(
        "play_action", {"name": "bow"}, max_steps=6,
        full_body_actions=("bow", "sit", "stand"),
    )
    wave = parse_tool_call(
        "play_action", {"name": "wave"}, max_steps=6,
        full_body_actions=("bow", "sit", "stand"),
    )
    assert bow.steps[0].args["full_body"] is True
    assert bow.steps[0].channels() == frozenset({
        Channel.UPPER_BODY, Channel.LOWER_BODY, Channel.MOVEMENT,
    })
    assert wave.steps[0].args["full_body"] is False


def test_text_ref_maps_to_use_suggested():
    """ER2 用 text_ref 引用建议台词时应转成 use_suggested"""
    plan = parse_tool_call("execute_plan", {"steps": [
        {"seq": 1, "action": "speak", "args": {"text_ref": "suggested_reply"}},
    ]}, max_steps=6)
    assert plan.steps[0].args["use_suggested"] is True


def test_plan_truncated_to_max_steps():
    steps = [{"seq": i, "action": "wait", "args": {}} for i in range(1, 12)]
    plan = parse_tool_call("execute_plan", {"steps": steps}, max_steps=6)
    assert len(plan.steps) == 6


def test_ack_and_stop_return_none():
    assert parse_tool_call("ack", {}, max_steps=6) is None
    assert parse_tool_call("stop_all", {}, max_steps=6) is None


def test_unknown_tool_rejected():
    with pytest.raises(PlanRejected):
        parse_tool_call("execute_tts", {}, max_steps=6)


def test_empty_plan_rejected():
    with pytest.raises(PlanRejected):
        parse_tool_call("execute_plan", {"steps": []}, max_steps=6)


def test_top_level_expression_and_empty_motion_prompt_validation():
    expr_plan = parse_tool_call("expression", {"name": "smile"}, max_steps=6)
    assert expr_plan.steps[0].action is ActionKind.EXPRESSION
    assert expr_plan.steps[0].args["name"] == "smile"

    with pytest.raises(PlanRejected, match="prompt"):
        parse_tool_call("generate_motion", {"prompt": "   "}, max_steps=6)
    with pytest.raises(PlanRejected, match="动作名称"):
        parse_tool_call("play_action", {"name": ""}, max_steps=6)


def test_tool_declarations_preserve_nested_step_schema():
    from aether.services.cerebellum.prompts import build_system_prompt, build_tool_declarations

    tools = build_tool_declarations()
    decls = {d.name: d for d in tools[0].function_declarations}
    assert {"execute_plan", "speak", "play_action", "expression", "move", "generate_motion", "ack", "stop_all"} <= set(decls)

    steps_schema = decls["execute_plan"].parameters.properties["steps"]
    item_props = steps_schema.items.properties
    assert {"seq", "action", "args", "parallel"} <= set(item_props)
    assert "play_action" in item_props["action"].enum

    prompt = build_system_prompt(("wave",), ("forward",), ("neutral", "smile"))
    assert "neutral、smile" in prompt


def test_long_running_classification():
    """move / generate_motion / 多步 → 立即 ack；单句 speak → 真阻塞"""
    assert is_long_running(parse_tool_call("move", {"direction": "forward"}, max_steps=6))
    assert not is_long_running(parse_tool_call("speak", {"text": "hi"}, max_steps=6))


def test_invalid_duration_rejected():
    for value in ("bad", -1, float("nan"), float("inf")):
        with pytest.raises(PlanRejected):
            parse_tool_call("move", {"duration_s": value}, max_steps=6)


def test_plan_duration_budget_serial_and_parallel():
    with pytest.raises(PlanRejected, match="超过"):
        parse_tool_call("execute_plan", {"steps": [
            {"action": "wait", "args": {"duration_s": 3}},
            {"action": "wait", "args": {"duration_s": 3}},
        ]}, max_steps=6, max_seconds=5)

    plan = parse_tool_call("execute_plan", {"steps": [
        {"action": "speak", "args": {"text": "abcdefgh"}},
        {"action": "play_action", "args": {"name": "wave"}, "parallel": True},
    ]}, max_steps=6, max_seconds=2)
    assert len(plan.steps) == 2


# ── 大脑输出解析 ───────────────────────────────────────────

def test_brain_parses_fenced_json():
    text = '```json\n{"suggested_reply":"嗨","wake_er2":true}\n```'
    parsed = BrainOutput.parse(text)
    assert parsed.suggested_reply == "嗨" and parsed.wake_er2 is True


def test_brain_parse_failure_returns_none():
    assert BrainOutput.parse("我觉得应该打招呼") is None


# ── 紧急停止 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emergency_runs_all_actions_and_marks_state():
    state = StateStore()
    await state.acquire_channels(frozenset({Channel.MOVEMENT}), "p1.s1", 3.0)
    stop = EmergencyStop(state)
    hit = []

    async def make(tag):
        hit.append(tag)

    stop.register("osc", lambda: make("osc"))
    stop.register("tts", lambda: make("tts"))

    elapsed = await stop.trigger("测试")
    assert set(hit) == {"osc", "tts"}
    assert state.facts.emergency_stopped
    assert state.facts.busy_channels() == ()
    assert elapsed < 0.5


def test_stop_keyword_detection():
    assert EmergencyStop.matches_keyword("你给我停下")
    assert EmergencyStop.matches_keyword("STOP")
    assert EmergencyStop.matches_keyword("please stop!")
    assert EmergencyStop.matches_keyword("freeze now")
    assert not EmergencyStop.matches_keyword("继续走")
    assert not EmergencyStop.matches_keyword("she is unstoppable today")
    assert not EmergencyStop.matches_keyword("freezing weather")


def test_normalize_health_status_handles_bool_and_reports():
    from types import SimpleNamespace
    from aether.contracts.facts import normalize_health_status

    assert normalize_health_status(True) == "ok"
    assert normalize_health_status(False) == "offline"
    assert normalize_health_status(SimpleNamespace(healthy=True)) == "ok"
    assert normalize_health_status(SimpleNamespace(healthy=False)) == "degraded"
    assert normalize_health_status("degraded") == "degraded"
    assert normalize_health_status(None) == "unknown"


@pytest.mark.asyncio
async def test_emergency_survives_failing_action():
    stop = EmergencyStop(StateStore())

    async def boom():
        raise RuntimeError("坏了")

    async def fine():
        pass

    stop.register("bad", boom)
    stop.register("good", fine)
    await stop.trigger("测试")  # 不应抛出
