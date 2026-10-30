"""coordinator — 大脑观察闭环、心跳驱动与小脑工具协调器（CognitiveCoordinator）

职责：
    1. 驱动 Brain 观察循环：启动后经短延迟（initial_delay_s）立即完成首轮热观察，
       之后在周期轮询（interval_s）与用户新发言事件（受 min_event_interval_s 冷却保护）
       之间自适应唤醒，兼顾响应速度与每日配额预算；
    2. 驱动 0.5s 心跳循环，将世界事实与大脑建议按闸门投喂给 ER2；
    3. 处理 ER2 工具调用：在预算校验前锁定 suggested_reply 快照，区分短阻塞计划与
       长异步计划，并确保 100% 回传 FunctionResponse。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..contracts.observation import BrainOutput
from ..contracts.plan import ActionKind, PlanStep, StepStatus
from ..core import render_facts
from ..services.cerebellum import PlanRejected, is_long_running, parse_tool_call

if TYPE_CHECKING:
    from .agent import AetherRuntime

logger = logging.getLogger(__name__)


class CognitiveCoordinator:
    """大脑 / 书记官 / 心跳 / 小脑工具调用的运行期协调器。"""

    def __init__(self, runtime: "AetherRuntime") -> None:
        self._rt = runtime
        self._utterance_event = asyncio.Event()
        self._last_observe_ts: float = 0.0

    def notify_user_utterance(self) -> None:
        """通知协调器有新的用户发言完成，供大脑在冷却允许时提前观察。"""
        self._utterance_event.set()

    # ── ER2 回调与计划分发 ─────────────────────────────────

    async def on_text(self, text: str) -> None:
        logger.debug("[Runtime] ER2 文本：%s", text[:80])
        await self._rt.scribe.note("text", text)

    async def on_tool_call(self, tool_call) -> None:
        """把工具调用翻译成计划并交给调度器。"""
        if not self._rt._running:
            return
        for fc in getattr(tool_call, "function_calls", []) or []:
            task = asyncio.create_task(self.handle_call(fc), name=f"call:{fc.name}")
            self._rt._track_task(task, self._rt._call_tasks)

    async def handle_call(self, fc) -> None:
        name = fc.name
        args = dict(fc.args or {})
        logger.info("[Runtime] 工具调用 %s(%s)", name, args)
        await self._rt.scribe.note("tool", f"{name}({args})")

        if name == "stop_all":
            await self._rt.trigger_emergency("ER2 请求停止")
            await self.respond(fc, {"status": "stopped"})
            return

        if name == "ack":
            await self.respond(fc, {"status": "ok"})
            return

        suggestion = (
            self._rt.brain.last_output
            if getattr(self._rt, "brain", None) is not None
            else None
        )
        suggested_reply = suggestion.suggested_reply if suggestion else ""

        try:
            plan = parse_tool_call(
                name,
                args,
                max_steps=self._rt.config.cerebellum.max_plan_steps,
                max_seconds=self._rt.config.cerebellum.max_plan_seconds,
                full_body_actions=tuple(self._rt.config.avatar.full_body_actions),
                suggested_reply=suggested_reply,
            )
        except PlanRejected as exc:
            await self.respond(fc, {"status": "rejected", "reason": str(exc)})
            return

        if plan is None:
            await self.respond(fc, {"status": "ok"})
            return

        plan = self.snapshot_suggested_replies(plan)

        if self._rt.state.facts.emergency_stopped:
            await self._rt.emergency.clear()

        # 长动作立即 ack，进度靠心跳 [STATUS] 回报；短动作真阻塞
        if is_long_running(plan):
            await self.respond(fc, {"status": "accepted", "plan_id": plan.plan_id})
            if not self._rt._running:
                return
            task = asyncio.create_task(
                self._rt.scheduler.submit(plan), name=f"plan:{plan.plan_id}"
            )
            self._rt._track_task(task, self._rt._plan_tasks)
        else:
            result = await self._rt.scheduler.submit(plan)
            await self.respond(
                fc,
                {
                    "status": result.status.value,
                    "plan_id": result.plan_id,
                    "detail": result.summary(),
                },
            )

    def snapshot_suggested_replies(self, plan):
        """计划接收时锁定建议台词，保证执行与机械重试使用相同输入。"""
        suggestion = (
            self._rt.brain.last_output
            if getattr(self._rt, "brain", None) is not None
            else None
        )
        reply = suggestion.suggested_reply if suggestion else ""
        if not reply:
            return plan
        steps = []
        for step in plan.steps:
            if step.action is ActionKind.SPEAK and step.args.get("use_suggested"):
                step_args = dict(step.args)
                step_args["suggested_reply"] = reply
                step = PlanStep(step.seq, step.action, step_args, step.parallel)
            steps.append(step)
        return type(plan)(steps=tuple(steps), plan_id=plan.plan_id, source=plan.source)

    async def respond(self, fc, payload: dict) -> None:
        """闭合工具调用 —— 不回传会导致 ER2 会话卡死。"""
        try:
            from google.genai import types

            await self._rt.cerebellum.send_tool_response([
                types.FunctionResponse(id=fc.id, name=fc.name, response=payload)
            ])
        except Exception:
            logger.exception("[Runtime] 工具响应回传失败")

    async def on_plan_done(self, result) -> None:
        """执行结果双路回流：ER2 靠心跳 STATUS，大脑靠 scribe。"""
        await self._rt.scribe.note("result", result.summary())
        if result.status is not StepStatus.SUCCESS:
            await self.inject_status(result.summary())

    async def inject_status(self, note: str) -> None:
        text = self._rt.heartbeat.status_only(self._rt.state.facts, f"[STATUS] {note}")
        await self._rt.cerebellum.send_heartbeat(text)

    # ── 心跳与大脑观察循环 ─────────────────────────────────

    async def heartbeat_loop(self) -> None:
        while self._rt._running:
            await asyncio.sleep(0.5)
            try:
                decision = self._rt.heartbeat.assemble(
                    self._rt.state.facts,
                    self._rt.brain.last_output,
                    user_speaking=self._rt._user_speaking,
                    er2_generating=self._rt.cerebellum.generating,
                )
                if decision.should_send:
                    await self._rt.cerebellum.send_heartbeat(decision.text or "")
            except Exception:
                logger.exception("[Runtime] 心跳迭代失败")

    async def brain_loop(self) -> None:
        brain_cfg = self._rt.config.brain
        interval_s = max(0.0, float(brain_cfg.interval_s))
        initial_delay_s = max(
            0.0, float(getattr(brain_cfg, "initial_delay_s", min(1.0, interval_s)))
        )

        # 启动后经短延迟立即执行首轮热观察，消除冷启动前 60s 无建议台词窗口
        await asyncio.sleep(initial_delay_s)
        if not self._rt._running:
            return
        await self._observe_once()

        while self._rt._running:
            interval_s = max(0.0, float(self._rt.config.brain.interval_s))
            min_event_s = max(
                0.0,
                float(
                    getattr(
                        self._rt.config.brain,
                        "min_event_interval_s",
                        min(15.0, interval_s),
                    )
                ),
            )
            await self._wait_next_brain_cycle(interval_s, min_event_s)
            if not self._rt._running:
                break
            await self._observe_once()

    async def _wait_next_brain_cycle(
        self, interval_s: float, min_event_interval_s: float
    ) -> None:
        if interval_s <= 0.0:
            self._utterance_event.clear()
            await asyncio.sleep(0)
            return

        while self._rt._running:
            elapsed = time.monotonic() - self._last_observe_ts
            remaining_periodic = max(0.0, interval_s - elapsed)
            if remaining_periodic <= 0.0:
                self._utterance_event.clear()
                return

            if self._utterance_event.is_set():
                cooldown_left = max(0.0, min_event_interval_s - elapsed)
                if cooldown_left <= 0.0:
                    self._utterance_event.clear()
                    return
                await asyncio.sleep(min(cooldown_left, remaining_periodic))
                continue

            try:
                await asyncio.wait_for(
                    self._utterance_event.wait(), timeout=remaining_periodic
                )
            except TimeoutError:
                return

    async def _observe_once(self) -> None:
        self._last_observe_ts = time.monotonic()
        if not self._rt.brain.enabled:
            return
        try:
            active = self._rt.scribe.render_active_context()
            recent = await self._rt.scribe.recall_recent()
            persisted_only = [
                line for line in recent.splitlines() if line not in active
            ]
            memories = "\n".join(filter(None, (active, "\n".join(persisted_only))))
            logger.debug("[Scribe] context metrics=%s", self._rt.scribe.metrics)
            output = await self._rt.brain.observe(
                render_facts(self._rt.state.facts),
                "\n".join(self._rt._transcript[-6:]),
                self._rt.capture.latest_frame(),
                memories,
            )
            if output is not None:
                await self.absorb(output)
        except Exception:
            logger.exception("[Runtime] 大脑迭代失败")

    async def absorb(self, output: BrainOutput) -> None:
        for memory in output.memory_write:
            await self._rt.scribe.remember(memory, "brain")
