"""planner — ER2 工具调用 → 调度器 Plan

职责边界：
    只做协议翻译与合法性裁剪，不做任何执行。
    非法参数在这里被纠正或拒绝，不让错误传播到调度器。

工具响应策略（架构评审结论）：
    短动作（<2s：speak 单句 / 预录动作 / 表情）→ 真阻塞，返回真实结果
    长动作（move / generate_motion / 多步计划）→ 立即返回 accepted，
        真实进度由后续心跳的 [STATUS] 携带
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ...contracts.plan import ActionKind, Plan, PlanStep, estimated_plan_seconds

logger = logging.getLogger(__name__)

#: 立即返回 accepted 而不阻塞 ER2 的动作
LONG_ACTIONS = frozenset({ActionKind.MOVE, ActionKind.GENERATE_MOTION})

_ALIASES: dict[str, ActionKind] = {
    "speak": ActionKind.SPEAK,
    "say": ActionKind.SPEAK,
    "expression": ActionKind.EXPRESSION,
    "set_expression": ActionKind.EXPRESSION,
    "play_action": ActionKind.PLAY_ACTION,
    "action": ActionKind.PLAY_ACTION,
    "generate_motion": ActionKind.GENERATE_MOTION,
    "motion": ActionKind.GENERATE_MOTION,
    "move": ActionKind.MOVE,
    "wait": ActionKind.WAIT,
}


class PlanRejected(ValueError):
    """计划不合法，直接回绝 ER2"""


def parse_tool_call(
    name: str,
    args: dict[str, Any],
    *,
    max_steps: int,
    max_seconds: float = 5.0,
    full_body_actions: tuple[str, ...] = (),
) -> Optional[Plan]:
    """把一次工具调用翻译为 Plan。ack / stop_all 返回 None 由上层特判。"""
    args = args or {}

    if name == "execute_plan":
        plan = _parse_plan(
            args, max_steps=max_steps, full_body_actions=full_body_actions
        )
        return _validate_budget(plan, max_seconds)

    # 单工具调用视为单步计划，同样必须通过总时长预算。
    if name == "speak":
        plan = _single(ActionKind.SPEAK, {
            "text": str(args.get("text", "")),
            "use_suggested": bool(args.get("use_suggested", False)),
        })
    elif name == "move":
        plan = _single(ActionKind.MOVE, {
            "direction": str(args.get("direction", "forward")),
            "duration_s": _as_float(args.get("duration_s"), 1.0, strict=True),
        })
    elif name in ("play_action", "action"):
        action_name = str(args.get("name") or args.get("action") or "").strip()
        normalized = _normalize_args(ActionKind.PLAY_ACTION, {"name": action_name})
        normalized["full_body"] = action_name in full_body_actions
        plan = _single(ActionKind.PLAY_ACTION, normalized)
    elif name == "generate_motion":
        plan = _single(ActionKind.GENERATE_MOTION, {
            "prompt": str(args.get("prompt", "")),
            "duration_s": _as_float(args.get("duration_s"), 2.0, strict=True),
            "scope": str(args.get("scope", "upper_body")),
        })
    elif name in ("ack", "stop_all"):
        return None
    else:
        raise PlanRejected(f"未知工具: {name}")
    return _validate_budget(plan, max_seconds)


def _parse_plan(
    args: dict[str, Any], *, max_steps: int, full_body_actions: tuple[str, ...]
) -> Plan:
    raw_steps = args.get("steps")
    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        raise PlanRejected("execute_plan 缺少 steps")

    if len(raw_steps) > max_steps:
        logger.warning("[Planner] 计划过长（%d 步），截断到 %d", len(raw_steps), max_steps)
        raw_steps = list(raw_steps)[:max_steps]

    steps: list[PlanStep] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            continue
        action = _resolve_action(raw.get("action"))
        if action is None:
            logger.warning("[Planner] 跳过未知动作：%s", raw.get("action"))
            continue
        step_args = raw.get("args")
        if not isinstance(step_args, dict):
            step_args = {}
        normalized = _normalize_args(action, step_args)
        if action is ActionKind.PLAY_ACTION:
            normalized["full_body"] = normalized["name"] in full_body_actions
        steps.append(PlanStep(
            seq=_as_int(raw.get("seq"), index),
            action=action,
            args=normalized,
            parallel=bool(raw.get("parallel", False)),
        ))

    if not steps:
        raise PlanRejected("计划中没有可执行的步骤")
    return Plan(steps=tuple(steps))


def _single(action: ActionKind, args: dict[str, Any]) -> Plan:
    return Plan(steps=(PlanStep(1, action, _normalize_args(action, args)),))


def _resolve_action(value) -> Optional[ActionKind]:
    if isinstance(value, ActionKind):
        return value
    return _ALIASES.get(str(value or "").strip().lower())


def _normalize_args(action: ActionKind, args: dict[str, Any]) -> dict[str, Any]:
    """按动作类型规整参数，补默认值"""
    out = dict(args)
    if action is ActionKind.SPEAK:
        out["text"] = str(out.get("text", "") or "")
        out["use_suggested"] = bool(out.get("use_suggested", False))
        # ER2 可能用 text_ref 引用建议台词
        if out.get("text_ref") == "suggested_reply":
            out["use_suggested"] = True
    elif action is ActionKind.MOVE:
        out["direction"] = str(out.get("direction", "forward"))
        out["duration_s"] = _as_float(out.get("duration_s"), 1.0, strict=True)
    elif action is ActionKind.GENERATE_MOTION:
        out["prompt"] = str(out.get("prompt", "") or "")
        out["duration_s"] = _as_float(out.get("duration_s"), 2.0, strict=True)
        scope = str(out.get("scope", "upper_body")).lower()
        out["scope"] = scope if scope in ("upper_body", "full_body") else "upper_body"
    elif action is ActionKind.PLAY_ACTION:
        out["name"] = str(out.get("name") or out.get("action") or "").strip()
    elif action is ActionKind.EXPRESSION:
        out["name"] = str(out.get("name") or out.get("expression") or "neutral").strip()
    elif action is ActionKind.WAIT:
        out["duration_s"] = _as_float(out.get("duration_s"), 0.5, strict=True)
    return out


def is_long_running(plan: Plan) -> bool:
    """计划是否应立即 ack 而非阻塞等待"""
    if len(plan.steps) > 1:
        return True
    return plan.steps[0].action in LONG_ACTIONS


def _validate_budget(plan: Plan, max_seconds: float) -> Plan:
    try:
        budget = float(max_seconds)
        estimated = estimated_plan_seconds(plan)
    except (TypeError, ValueError) as exc:
        raise PlanRejected(str(exc)) from exc
    if budget <= 0 or budget != budget or budget in (float("inf"), float("-inf")):
        raise PlanRejected("max_seconds 必须是有限正数")
    if estimated > budget:
        raise PlanRejected(f"计划预计 {estimated:.2f}s，超过 {budget:.2f}s 预算")
    return plan


def _as_float(value, default: float, *, strict: bool = False) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        if strict:
            raise PlanRejected("时间参数不是有效数字")
        return default
    if result < 0 or result != result or result in (float("inf"), float("-inf")):
        raise PlanRejected("时间参数必须是有限非负数")
    return result


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
