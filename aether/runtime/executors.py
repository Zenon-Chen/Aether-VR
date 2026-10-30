"""executors — 身体输出执行器套件（BodyExecutorSuite）

这是调度器（Scheduler）与各输出服务（Speech / Avatar / Locomotion / Posture / Vision）
之间的唯一接缝：
    - 负责全身预录动作的姿态冻结与恢复（防止 OSC Action 与 SteamVR 6-DoF 争抢躯体）；
    - 为动态动作生成提供有界生成超时，超时或后端异常时自动降级到预录回退动作；
    - 将高频视觉伺服（APPROACH）纳入调度器移动通道锁管理。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..contracts.plan import ActionKind, PlanStep
from ..contracts.vision import VisionStatus

if TYPE_CHECKING:
    from .agent import AetherRuntime

logger = logging.getLogger(__name__)


class BodyExecutorSuite:
    """运行期动作执行器集合（动态引用 runtime 服务，兼容运行期替换与白盒单测）。"""

    def __init__(self, runtime: "AetherRuntime") -> None:
        self._rt = runtime
        self.last_approach_result: VisionStatus | None = None

    def register_all(self) -> None:
        sched = self._rt.scheduler
        sched.register(ActionKind.SPEAK, self.exec_speak)
        sched.register(ActionKind.EXPRESSION, self.exec_expression)
        sched.register(ActionKind.PLAY_ACTION, self.exec_play_action)
        sched.register(ActionKind.MOVE, self.exec_move)
        sched.register(ActionKind.WAIT, self.exec_wait)
        sched.register(ActionKind.APPROACH, self.exec_approach)
        if self._rt.config.posture.enabled:
            sched.register(ActionKind.GENERATE_MOTION, self.exec_motion)

    async def exec_speak(self, step: PlanStep) -> str:
        text = step.args.get("text", "")
        if step.args.get("use_suggested"):
            text = step.args.get("suggested_reply", "")
            if not text:
                suggestion = self._rt.brain.last_output
                if suggestion and suggestion.suggested_reply:
                    text = suggestion.suggested_reply
        if not text:
            raise ValueError("没有可说的内容")
        await self._rt.speech.speak(text)
        await self._rt.state.set_last_speech(text)
        # 说了什么必须回灌，供大脑维持对话连贯
        await self._rt.scribe.note("speech", f"角色说：{text}")
        return text

    async def exec_expression(self, step: PlanStep) -> str:
        name = step.args.get("name", "neutral")
        await self._rt.avatar.set_expression(name)
        return name

    async def exec_play_action(self, step: PlanStep) -> str:
        name = step.args.get("name", "")
        # 预录全身动画独占移动与六点姿态；上身动作不碰下身姿态流。
        full_body = self._rt.avatar.is_full_body(name)
        if full_body:
            await self._rt.locomotion.halt()
            if self._rt.config.posture.enabled:
                await self._rt.posture.freeze()
        try:
            await self._rt.avatar.play_action(name)
        finally:
            if full_body:
                # 取消或 OSC 异常不得让任一来源悬空占用躯体。
                await self._rt.avatar.reset()
                if self._rt.config.posture.enabled:
                    await self._rt.posture.resume()
        return name

    async def exec_move(self, step: PlanStep) -> str:
        direction = step.args.get("direction", "forward")
        duration = float(step.args.get("duration_s", 1.0))
        await self._rt.locomotion.move(direction, duration)
        return f"{direction} {duration:.1f}s"

    async def exec_motion(self, step: PlanStep) -> str:
        prompt = step.args.get("prompt", "")
        duration = float(step.args.get("duration_s", 2.0))
        scope = "full_body" if step.args.get("scope") == "full_body" else "upper_body"
        motion_cfg = getattr(getattr(self._rt, "config", None), "motion", None)
        gen_timeout = float(
            step.args.get(
                "generation_timeout_s",
                getattr(motion_cfg, "generation_timeout_s", 15.0),
            )
        )
        try:
            coro = self._rt.posture.play(prompt, duration, scope == "full_body")
            if gen_timeout > 0:
                return await asyncio.wait_for(
                    coro, timeout=gen_timeout + max(0.0, duration)
                )
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            fallback = self._rt.config.avatar.motion_fallback_actions.get(scope, "")
            if not fallback or fallback not in self._rt.config.avatar.actions:
                raise
            logger.exception(
                "[Runtime] generate_motion 失败，回退预录动作 %s（scope=%s）",
                fallback,
                scope,
            )
            fallback_step = PlanStep(
                step.seq,
                ActionKind.PLAY_ACTION,
                {"name": fallback, "full_body": self._rt.avatar.is_full_body(fallback)},
            )
            await self.exec_play_action(fallback_step)
            return f"动作生成失败（{type(exc).__name__}: {exc}）；已回退 {fallback}"

    async def exec_wait(self, step: PlanStep) -> str:
        span = float(step.args.get("duration_s", 0.5))
        await asyncio.sleep(span)
        return f"等待 {span:.1f}s"

    async def exec_approach(self, step: PlanStep) -> str:
        target_id = str(step.args.get("target_id") or "person")
        result = await self.run_approach_direct(target_id)
        return f"{result.reason or result.state} (steps={result.steps})"

    async def run_approach_direct(self, target_id: str) -> VisionStatus:
        cfg = self._rt.config.vision
        if not cfg.enabled or getattr(self._rt.vision.tracker, "degraded", False):
            await self._rt.locomotion.halt()
            reason = "disabled" if not cfg.enabled else "degraded"
            result = VisionStatus(
                state="stopped", target_id=target_id, reason=reason, degraded=True
            )
            self._rt.vision.status = result
            self.last_approach_result = result
            await self._rt.state.set_vision_status(f"{reason}; steps=0; degraded=True")
            return result

        await self._rt.state.set_vision_status(f"tracking {target_id}")
        result: VisionStatus | None = None
        try:
            result = await self._rt.vision.approach_target(
                target_id,
                frame_source=lambda: self._rt.capture.fresh_frame(cfg.fresh_frame_timeout_s),
                locomotion=self._rt.locomotion,
                near_ratio=cfg.near_ratio,
                confidence_min=cfg.confidence_min,
                center_tolerance=cfg.center_tolerance,
                pulse_s=cfg.pulse_s,
                timeout_s=cfg.timeout_s,
                max_steps=cfg.max_steps,
                stagnation_steps=cfg.stagnation_steps,
                detect_fps=cfg.detect_fps,
            )
            self.last_approach_result = result
            return result
        finally:
            status = result or self._rt.vision.status
            self.last_approach_result = status
            await self._rt.state.set_vision_status(
                f"{status.reason or status.state}; steps={status.steps}; degraded={status.degraded}"
            )
