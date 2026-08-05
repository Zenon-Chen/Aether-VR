"""runtime — 装配与生命周期

这是唯一知道「所有模块如何连接」的地方。各服务彼此不直接引用，
全部通过 contracts 与 core 交互，因此可以单独替换或拆成独立进程。

进程模型（当前默认）：
    本进程   core + capture + cerebellum + speech + avatar + locomotion + brain + posture
    外部服务 DART（默认通过固定 HTTP URL 访问）
    sidecar 组件 MotionProviderSupervisor / BundleRegistry 尚未默认接线
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..contracts.observation import BrainOutput
from ..contracts.plan import ActionKind, PlanStep, StepStatus
from ..contracts.vision import VisionStatus
from ..core import (
    EmergencyStop, HeartbeatAssembler, Scheduler, StateStore, render_facts,
)
from ..infra.config import AetherConfig
from ..services.avatar import AvatarService
from ..services.brain import BrainService
from ..services.capture import CaptureService
from ..services.cerebellum import (
    CerebellumService, PlanRejected, is_long_running, parse_tool_call,
)
from ..services.locomotion import LocomotionService
from ..services.motion.providers.dart import DartMotionProvider
from ..services.ports import MotionProviderPort
from ..services.posture import PostureService
from ..services.scribe import ScribeService
from ..services.speech import SpeechService
from ..services.vision import VisionService

logger = logging.getLogger(__name__)


class AetherRuntime:
    """组合根"""

    def __init__(
        self,
        config: AetherConfig,
        *,
        motion_provider: MotionProviderPort | None = None,
    ) -> None:
        self.config = config

        # ── 核心 ────────────────────────────────────────
        self.state = StateStore()
        self.heartbeat = HeartbeatAssembler()
        self.emergency = EmergencyStop(self.state)
        self.scheduler = Scheduler(
            self.state,
            on_plan_done=self._on_plan_done,
            max_plan_seconds=config.cerebellum.max_plan_seconds,
        )

        # ── 服务 ────────────────────────────────────────
        self.capture = CaptureService(config.capture)
        self.avatar = AvatarService(config.avatar, config.osc)
        self.locomotion = LocomotionService(config.locomotion, config.osc)
        self.vision = VisionService(config=config.vision)
        self.speech = SpeechService(config.speech)
        self.motion_provider = motion_provider or DartMotionProvider(
            f"http://{config.dart.host}:{config.dart.port}",
            timeout_s=config.dart.timeout_s,
        )
        self.posture = PostureService(
            config.posture, config.dart, motion_provider=self.motion_provider
        )
        self.brain = BrainService(config.brain)
        self.scribe = ScribeService(config.scribe)
        self.scribe.set_evaluator(self.brain.evaluate_memory)
        self.cerebellum = CerebellumService(
            config.cerebellum,
            actions=self.avatar.known_actions(),
            directions=self.locomotion.known_directions(),
        )

        self._tasks: list[asyncio.Task] = []
        self._call_tasks: set[asyncio.Task] = set()
        self._plan_tasks: set[asyncio.Task] = set()
        self._running = False
        self._user_speaking = False
        self._transcript: list[str] = []
        self._pending_calls: dict[str, Any] = {}

    # ══════════════════════════════════════════════════════
    # 启动 / 停止
    # ══════════════════════════════════════════════════════

    async def start(self) -> None:
        logger.info("=== Aether 启动 ===")

        # 输出侧优先就绪，保证紧急停止随时可用
        await self.avatar.start()
        await self.locomotion.start()
        await self.speech.start()
        if self.config.posture.enabled:
            await self.posture.start()
        await self.scribe.start()
        await self.brain.start()
        await self.capture.start()
        if self.config.vision.enabled:
            await self.vision.start()

        self._register_executors()
        self._register_emergency()

        self.cerebellum.set_tool_call_handler(self._on_tool_call)
        self.cerebellum.set_text_handler(self._on_text)
        self.cerebellum.set_transcript_handler(self.add_transcript)
        await self.cerebellum.start()

        await self._refresh_health()
        self._running = True

        self._tasks = [
            asyncio.create_task(self.cerebellum.receive_loop(), name="er2_receive"),
            asyncio.create_task(self._audio_pump(), name="audio_pump"),
            asyncio.create_task(self._video_pump(), name="video_pump"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._brain_loop(), name="brain"),
            asyncio.create_task(self._health_loop(), name="health"),
        ]
        logger.info("=== Aether 就绪 ===")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        for task in (*self._call_tasks, *self._plan_tasks):
            task.cancel()
        await asyncio.gather(
            *self._call_tasks, *self._plan_tasks, return_exceptions=True
        )
        self._call_tasks.clear()
        self._plan_tasks.clear()

        await self.scheduler.cancel_current("shutdown")
        for service in (
            self.cerebellum, self.capture, self.vision, self.brain, self.scribe,
            self.posture, self.speech, self.locomotion, self.avatar,
        ):
            try:
                await service.stop()
            except Exception:
                logger.exception("[Runtime] %s 停止失败", service.name)
        logger.info("=== Aether 已停止 ===")

    # ══════════════════════════════════════════════════════
    # 执行器注册 —— 调度器与各服务的唯一接缝
    # ══════════════════════════════════════════════════════

    def _register_executors(self) -> None:
        self.scheduler.register(ActionKind.SPEAK, self._exec_speak)
        self.scheduler.register(ActionKind.EXPRESSION, self._exec_expression)
        self.scheduler.register(ActionKind.PLAY_ACTION, self._exec_play_action)
        self.scheduler.register(ActionKind.MOVE, self._exec_move)
        self.scheduler.register(ActionKind.WAIT, self._exec_wait)
        if self.config.posture.enabled:
            self.scheduler.register(ActionKind.GENERATE_MOTION, self._exec_motion)

    async def _exec_speak(self, step: PlanStep) -> str:
        text = step.args.get("text", "")
        if step.args.get("use_suggested"):
            text = step.args.get("suggested_reply", "")
            if not text:
                suggestion = self.brain.last_output
                if suggestion and suggestion.suggested_reply:
                    text = suggestion.suggested_reply
        if not text:
            raise ValueError("没有可说的内容")
        await self.speech.speak(text)
        await self.state.set_last_speech(text)
        # 说了什么必须回灌，供大脑维持对话连贯
        await self.scribe.note("speech", f"角色说：{text}")
        return text

    async def _exec_expression(self, step: PlanStep) -> str:
        name = step.args.get("name", "neutral")
        await self.avatar.set_expression(name)
        return name

    async def _exec_play_action(self, step: PlanStep) -> str:
        name = step.args.get("name", "")
        # 预录全身动画独占移动与六点姿态；上身动作不碰下身姿态流。
        full_body = self.avatar.is_full_body(name)
        if full_body:
            await self.locomotion.halt()
            if self.config.posture.enabled:
                await self.posture.freeze()
        try:
            await self.avatar.play_action(name)
        finally:
            if full_body:
                # cancellation/OSC failure must not leave either source owning the body.
                await self.avatar.reset()
                if self.config.posture.enabled:
                    await self.posture.resume()
        return name

    async def _exec_move(self, step: PlanStep) -> str:
        direction = step.args.get("direction", "forward")
        duration = float(step.args.get("duration_s", 1.0))
        await self.locomotion.move(direction, duration)
        return f"{direction} {duration:.1f}s"

    async def _exec_motion(self, step: PlanStep) -> str:
        prompt = step.args.get("prompt", "")
        duration = float(step.args.get("duration_s", 2.0))
        scope = "full_body" if step.args.get("scope") == "full_body" else "upper_body"
        try:
            return await self.posture.play(prompt, duration, scope == "full_body")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            fallback = self.config.avatar.motion_fallback_actions.get(scope, "")
            if not fallback or fallback not in self.config.avatar.actions:
                raise
            logger.exception(
                "[Runtime] generate_motion 失败，回退预录动作 %s（scope=%s）",
                fallback, scope,
            )
            # The failed posture playback has returned before fallback starts; use the
            # same ownership path as a planned prerecorded action.
            fallback_step = PlanStep(
                step.seq, ActionKind.PLAY_ACTION,
                {"name": fallback, "full_body": self.avatar.is_full_body(fallback)},
            )
            await self._exec_play_action(fallback_step)
            return f"动作生成失败（{type(exc).__name__}: {exc}）；已回退 {fallback}"

    async def _exec_wait(self, step: PlanStep) -> str:
        span = float(step.args.get("duration_s", 0.5))
        await asyncio.sleep(span)
        return f"等待 {span:.1f}s"

    # ══════════════════════════════════════════════════════
    # 紧急停止
    # ══════════════════════════════════════════════════════

    def _register_emergency(self) -> None:
        self.emergency.register("locomotion", self.locomotion.halt)
        self.emergency.register("vision", self._cancel_vision)
        self.emergency.register("speech", self.speech.halt)
        self.emergency.register("avatar", self.avatar.reset)
        self.emergency.register(
            "scheduler", lambda: self.scheduler.cancel_current("emergency")
        )
        if self.config.posture.enabled:
            self.emergency.register("posture", self.posture.emergency_reset)

    async def _cancel_vision(self) -> None:
        self.vision.cancel()
        await self.locomotion.halt()

    async def approach_target(self, target_id: str):
        cfg = self.config.vision
        if not cfg.enabled or getattr(self.vision.tracker, "degraded", False):
            await self.locomotion.halt()
            reason = "disabled" if not cfg.enabled else "degraded"
            result = VisionStatus(state="stopped", target_id=target_id, reason=reason, degraded=True)
            self.vision.status = result
            await self.state.set_vision_status(f"{reason}; steps=0; degraded=True")
            return result
        await self.state.set_vision_status(f"tracking {target_id}")
        result = None
        try:
            result = await self.vision.approach_target(
                target_id,
                frame_source=lambda: self.capture.fresh_frame(cfg.fresh_frame_timeout_s),
                locomotion=self.locomotion,
                near_ratio=cfg.near_ratio, confidence_min=cfg.confidence_min,
                center_tolerance=cfg.center_tolerance, pulse_s=cfg.pulse_s,
                timeout_s=cfg.timeout_s, max_steps=cfg.max_steps,
                stagnation_steps=cfg.stagnation_steps, detect_fps=cfg.detect_fps,
            )
            return result
        finally:
            status = result or self.vision.status
            await self.state.set_vision_status(
                f"{status.reason or status.state}; steps={status.steps}; degraded={status.degraded}"
            )

    async def trigger_emergency(self, reason: str) -> float:
        elapsed = await self.emergency.trigger(reason)
        await self._inject_status(f"已紧急停止，原因：{reason}")
        return elapsed

    # ══════════════════════════════════════════════════════
    # ER2 回调
    # ══════════════════════════════════════════════════════

    async def _on_text(self, text: str) -> None:
        logger.debug("[Runtime] ER2 文本：%s", text[:80])
        await self.scribe.note("text", text)

    @staticmethod
    def _track_task(task: asyncio.Task, tasks: set[asyncio.Task]) -> None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _on_tool_call(self, tool_call) -> None:
        """把工具调用翻译成计划并交给调度器"""
        if not self._running:
            return
        for fc in getattr(tool_call, "function_calls", []) or []:
            task = asyncio.create_task(self._handle_call(fc), name=f"call:{fc.name}")
            self._track_task(task, self._call_tasks)

    async def _handle_call(self, fc) -> None:
        name = fc.name
        args = dict(fc.args or {})
        logger.info("[Runtime] 工具调用 %s(%s)", name, args)
        await self.scribe.note("tool", f"{name}({args})")

        if name == "stop_all":
            await self.trigger_emergency("ER2 请求停止")
            await self._respond(fc, {"status": "stopped"})
            return

        if name == "ack":
            await self._respond(fc, {"status": "ok"})
            return

        try:
            plan = parse_tool_call(
                name,
                args,
                max_steps=self.config.cerebellum.max_plan_steps,
                max_seconds=self.config.cerebellum.max_plan_seconds,
                full_body_actions=tuple(self.config.avatar.full_body_actions),
            )
        except PlanRejected as exc:
            await self._respond(fc, {"status": "rejected", "reason": str(exc)})
            return

        if plan is None:
            await self._respond(fc, {"status": "ok"})
            return

        plan = self._snapshot_suggested_replies(plan)

        if self.state.facts.emergency_stopped:
            await self.emergency.clear()

        # 长动作立即 ack，进度靠心跳 [STATUS] 回报；短动作真阻塞
        if is_long_running(plan):
            await self._respond(fc, {"status": "accepted", "plan_id": plan.plan_id})
            if not self._running:
                return
            task = asyncio.create_task(
                self.scheduler.submit(plan), name=f"plan:{plan.plan_id}"
            )
            self._track_task(task, self._plan_tasks)
        else:
            result = await self.scheduler.submit(plan)
            await self._respond(fc, {
                "status": result.status.value,
                "plan_id": result.plan_id,
                "detail": result.summary(),
            })

    def _snapshot_suggested_replies(self, plan):
        """计划接收时锁定建议台词，保证执行与机械重试使用相同输入。"""
        suggestion = self.brain.last_output
        reply = suggestion.suggested_reply if suggestion else ""
        if not reply:
            return plan
        steps = []
        for step in plan.steps:
            if step.action is ActionKind.SPEAK and step.args.get("use_suggested"):
                args = dict(step.args)
                args["suggested_reply"] = reply
                step = PlanStep(step.seq, step.action, args, step.parallel)
            steps.append(step)
        return type(plan)(steps=tuple(steps), plan_id=plan.plan_id, source=plan.source)

    async def _respond(self, fc, payload: dict) -> None:
        """闭合工具调用 —— 不回传会导致 ER2 会话卡死"""
        try:
            from google.genai import types
            await self.cerebellum.send_tool_response([
                types.FunctionResponse(id=fc.id, name=fc.name, response=payload)
            ])
        except Exception:
            logger.exception("[Runtime] 工具响应回传失败")

    async def _on_plan_done(self, result) -> None:
        """执行结果双路回流：ER2 靠心跳 STATUS，大脑靠 scribe"""
        await self.scribe.note("result", result.summary())
        if result.status is not StepStatus.SUCCESS:
            await self._inject_status(result.summary())

    async def _inject_status(self, note: str) -> None:
        text = self.heartbeat.status_only(self.state.facts, f"[STATUS] {note}")
        await self.cerebellum.send_heartbeat(text)

    # ══════════════════════════════════════════════════════
    # 后台循环
    # ══════════════════════════════════════════════════════

    async def _audio_pump(self) -> None:
        """麦克风 → ER2，并做本地关键词紧急停止"""
        async for chunk in self.capture.audio_stream():
            if not self._running:
                break
            await self.cerebellum.send_audio(chunk)

    async def _video_pump(self) -> None:
        async for frame in self.capture.video_stream():
            if not self._running:
                break
            await self.cerebellum.send_video(frame)

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)
            try:
                decision = self.heartbeat.assemble(
                    self.state.facts,
                    self.brain.last_output,
                    user_speaking=self._user_speaking,
                    er2_generating=self.cerebellum.generating,
                )
                if decision.should_send:
                    await self.cerebellum.send_heartbeat(decision.text or "")
            except Exception:
                logger.exception("[Runtime] 心跳迭代失败")

    async def _brain_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.config.brain.interval_s)
            if not self.brain.enabled:
                continue
            try:
                active = self.scribe.render_active_context()
                recent = await self.scribe.recall_recent()
                persisted_only = [line for line in recent.splitlines() if line not in active]
                memories = "\n".join(filter(None, (active, "\n".join(persisted_only))))
                logger.debug("[Scribe] context metrics=%s", self.scribe.metrics)
                output = await self.brain.observe(
                    render_facts(self.state.facts),
                    "\n".join(self._transcript[-6:]),
                    self.capture.latest_frame(),
                    memories,
                )
                if output is not None:
                    await self._absorb(output)
            except Exception:
                logger.exception("[Runtime] 大脑迭代失败")

    async def _absorb(self, output: BrainOutput) -> None:
        for memory in output.memory_write:
            await self.scribe.remember(memory, "brain")

    async def _health_loop(self) -> None:
        while self._running:
            await asyncio.sleep(10.0)
            try:
                await self._refresh_health()
            except Exception:
                logger.exception("[Runtime] 健康检查失败")

    async def _refresh_health(self) -> None:
        checks = {
            "dart": self.posture.check_dart if self.config.posture.enabled else None,
            "posture": self.posture.health if self.config.posture.enabled else None,
            "tts": self.speech.health,
            "er2": self.cerebellum.health,
            "brain": self.brain.health,
            "vrchat_osc": self.avatar.health,
            "vision": self.vision.health if self.config.vision.enabled else None,
        }
        for name, probe in checks.items():
            if probe is None:
                continue
            try:
                value = await probe()
                status = "ok" if value is True else (value if isinstance(value, str) else "ok")
            except Exception:
                status = "error"
            await self.state.set_service(name, status)

    # ══════════════════════════════════════════════════════
    # 外部触发
    # ══════════════════════════════════════════════════════

    def set_user_speaking(self, speaking: bool) -> None:
        """用户语音回合进行中不发心跳，避免打断 ER2 生成"""
        self._user_speaking = speaking

    def add_transcript(self, line: str) -> None:
        self._transcript.append(line)
        if len(self._transcript) > 20:
            self._transcript = self._transcript[-20:]
        asyncio.create_task(self.scribe.note("user", f"用户说：{line}"))
