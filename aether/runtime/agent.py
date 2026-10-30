"""runtime — 装配与生命周期（组合根）

这是唯一知道「所有模块如何连接」的地方。各服务彼此不直接引用，
全部通过 contracts、core 与 ports 交互。

运行期职责拆分为四个高内聚协作者：
    - motion_wiring.py    动作生成器装配（external HTTP / supervised sidecar）与 I/O 宽限预算
    - executors.py        BodyExecutorSuite：调度器到身体输出服务的唯一接缝
    - perception_gate.py  PerceptionGate：音视频流泵、轻量能量 VAD 与本地语音急停拦截
    - coordinator.py      CognitiveCoordinator：大脑热启动/防抖观察、心跳与 ER2 工具协调
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

from ..contracts.facts import normalize_health_status
from ..contracts.observation import BrainOutput
from ..contracts.plan import ActionKind, Plan, PlanStep, StepStatus
from ..contracts.vision import VisionStatus
from ..core import EmergencyStop, HeartbeatAssembler, Scheduler, StateStore
from ..infra.config import AetherConfig
from ..services.avatar import AvatarService
from ..services.brain import BrainService
from ..services.capture import CaptureService
from ..services.cerebellum import CerebellumService
from ..services.locomotion import LocomotionService
from ..services.ports import MotionProviderPort
from ..services.posture import PostureService
from ..services.scribe import ScribeService
from ..services.speech import SpeechService
from ..services.vision import VisionService
from .coordinator import CognitiveCoordinator
from .executors import BodyExecutorSuite
from .motion_wiring import compute_plan_io_allowance, create_motion_provider
from .perception_gate import PerceptionGate

logger = logging.getLogger(__name__)


class AetherRuntime:
    """主运行时组合根：负责核心与服务装配、协作者接线与生命周期管理。"""

    def __init__(
        self,
        config: AetherConfig,
        *,
        motion_provider: MotionProviderPort | None = None,
    ) -> None:
        self.config = config

        # ── 核心控制层 ──────────────────────────────────
        self.state = StateStore()
        self.heartbeat = HeartbeatAssembler()
        self.emergency = EmergencyStop(self.state)
        self.scheduler = Scheduler(
            self.state,
            on_plan_done=self._on_plan_done,
            max_plan_seconds=config.cerebellum.max_plan_seconds,
            io_allowance_fn=lambda plan: compute_plan_io_allowance(plan, self.config),
        )

        # ── 领域服务层 ──────────────────────────────────
        self.capture = CaptureService(config.capture)
        self.avatar = AvatarService(config.avatar, config.osc)
        self.locomotion = LocomotionService(config.locomotion, config.osc)
        self.vision = VisionService(config=config.vision)
        self.speech = SpeechService(config.speech)
        self.motion_provider = motion_provider or create_motion_provider(config)
        self.posture = PostureService(
            config.posture, motion_provider=self.motion_provider
        )
        self.brain = BrainService(config.brain)
        self.scribe = ScribeService(config.scribe)
        self.scribe.set_evaluator(self.brain.evaluate_memory)
        self.cerebellum = CerebellumService(
            config.cerebellum,
            actions=self.avatar.known_actions(),
            directions=self.locomotion.known_directions(),
            expressions=self.avatar.known_expressions(),
        )

        # ── 运行期协作者 ────────────────────────────────
        self._executors = BodyExecutorSuite(self)
        self._perception = PerceptionGate(self)
        self._coordinator = CognitiveCoordinator(self)

        # ── 任务与状态跟踪 ──────────────────────────────
        self._tasks: list[asyncio.Task] = []
        self._call_tasks: set[asyncio.Task] = set()
        self._plan_tasks: set[asyncio.Task] = set()
        self._bg_tasks: set[asyncio.Task] = set()
        self._running = False
        self._user_speaking = False
        self._transcript: list[str] = []
        self._pending_calls: dict[str, Any] = {}

    # ── 延迟协作者访问（兼容绕过 __init__ 的白盒测试） ──────

    def _get_executors(self) -> BodyExecutorSuite:
        suite = getattr(self, "_executors", None)
        if suite is None:
            suite = BodyExecutorSuite(self)
            self._executors = suite
        return suite

    def _get_perception(self) -> PerceptionGate:
        gate = getattr(self, "_perception", None)
        if gate is None:
            gate = PerceptionGate(self)
            self._perception = gate
        return gate

    def _get_coordinator(self) -> CognitiveCoordinator:
        coord = getattr(self, "_coordinator", None)
        if coord is None:
            coord = CognitiveCoordinator(self)
            self._coordinator = coord
        return coord

    @staticmethod
    def _track_task(task: asyncio.Task, tasks: set[asyncio.Task]) -> None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def spawn_bg_task(
        self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
    ) -> asyncio.Task:
        bg_tasks = getattr(self, "_bg_tasks", None)
        if bg_tasks is None:
            bg_tasks = set()
            self._bg_tasks = bg_tasks
        task = asyncio.create_task(coro, name=name)
        self._track_task(task, bg_tasks)
        return task

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
        self.cerebellum.set_partial_transcript_handler(
            self._get_perception().on_partial_transcript
        )
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

        bg_tasks: set[asyncio.Task] = getattr(self, "_bg_tasks", set())
        tracked = (*self._call_tasks, *self._plan_tasks, *bg_tasks)
        for task in tracked:
            task.cancel()
        if tracked:
            await asyncio.gather(*tracked, return_exceptions=True)
        self._call_tasks.clear()
        self._plan_tasks.clear()
        bg_tasks.clear()

        await self.scheduler.cancel_current("shutdown")
        for service in (
            self.cerebellum,
            self.capture,
            self.vision,
            self.brain,
            self.scribe,
            self.posture,
            self.speech,
            self.locomotion,
            self.avatar,
        ):
            try:
                await service.stop()
            except Exception:
                logger.exception("[Runtime] %s 停止失败", service.name)
        logger.info("=== Aether 已停止 ===")

    # ══════════════════════════════════════════════════════
    # 执行器与紧急停止接线
    # ══════════════════════════════════════════════════════

    def _register_executors(self) -> None:
        self._get_executors().register_all()

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

    async def trigger_emergency(self, reason: str) -> float:
        elapsed = await self.emergency.trigger(reason)
        await self._inject_status(f"已紧急停止，原因：{reason}")
        return elapsed

    async def go_idle(self) -> None:
        """取消在跑计划并恢复站立空闲姿态。"""
        await self.scheduler.cancel_current("idle")
        await self.locomotion.halt()
        await self.avatar.reset()
        if self.config.posture.enabled:
            await self.posture.go_idle()

    async def approach_target(self, target_id: str) -> VisionStatus:
        """通过调度器移动通道锁执行高频视觉伺服逼近。"""
        cfg = self.config.vision
        suite = self._get_executors()
        if not cfg.enabled or getattr(self.vision.tracker, "degraded", False):
            return await suite.run_approach_direct(target_id)

        sched = getattr(self, "scheduler", None)
        if sched is not None and ActionKind.APPROACH in sched.registered_kinds():
            step = PlanStep(
                1,
                ActionKind.APPROACH,
                {"target_id": target_id, "timeout": float(cfg.timeout_s) + 5.0},
            )
            plan_result = await sched.submit(Plan(steps=(step,), source="vision"))
            if suite.last_approach_result is not None:
                return suite.last_approach_result
            if plan_result.status is StepStatus.CANCELLED:
                cancelled = VisionStatus(
                    state="stopped",
                    target_id=target_id,
                    reason="cancelled",
                    steps=self.vision.status.steps,
                    degraded=self.vision.status.degraded,
                )
                self.vision.status = cancelled
                return cancelled
            return self.vision.status

        return await suite.run_approach_direct(target_id)

    # ── 动作执行器薄委托 ──────────────────────────────────

    async def _exec_speak(self, step: PlanStep) -> str:
        return await self._get_executors().exec_speak(step)

    async def _exec_expression(self, step: PlanStep) -> str:
        return await self._get_executors().exec_expression(step)

    async def _exec_play_action(self, step: PlanStep) -> str:
        return await self._get_executors().exec_play_action(step)

    async def _exec_move(self, step: PlanStep) -> str:
        return await self._get_executors().exec_move(step)

    async def _exec_motion(self, step: PlanStep) -> str:
        return await self._get_executors().exec_motion(step)

    async def _exec_wait(self, step: PlanStep) -> str:
        return await self._get_executors().exec_wait(step)

    async def _exec_approach(self, step: PlanStep) -> str:
        return await self._get_executors().exec_approach(step)

    # ── 认知与工具协调薄委托 ──────────────────────────────

    async def _on_text(self, text: str) -> None:
        await self._get_coordinator().on_text(text)

    async def _on_tool_call(self, tool_call) -> None:
        await self._get_coordinator().on_tool_call(tool_call)

    async def _handle_call(self, fc) -> None:
        await self._get_coordinator().handle_call(fc)

    def _snapshot_suggested_replies(self, plan):
        return self._get_coordinator().snapshot_suggested_replies(plan)

    async def _respond(self, fc, payload: dict) -> None:
        await self._get_coordinator().respond(fc, payload)

    async def _on_plan_done(self, result) -> None:
        await self._get_coordinator().on_plan_done(result)

    async def _inject_status(self, note: str) -> None:
        await self._get_coordinator().inject_status(note)

    async def _heartbeat_loop(self) -> None:
        await self._get_coordinator().heartbeat_loop()

    async def _brain_loop(self) -> None:
        await self._get_coordinator().brain_loop()

    async def _absorb(self, output: BrainOutput) -> None:
        await self._get_coordinator().absorb(output)

    # ── 感知与健康探测 ────────────────────────────────────

    async def _audio_pump(self) -> None:
        await self._get_perception().audio_pump()

    async def _video_pump(self) -> None:
        await self._get_perception().video_pump()

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
                status = normalize_health_status(value)
            except Exception:
                status = "error"
            await self.state.set_service(name, status)

    # ── 外部触发 ──────────────────────────────────────────

    @property
    def user_speaking(self) -> bool:
        return self._user_speaking

    def set_user_speaking(self, speaking: bool) -> None:
        """用户语音回合进行中不发心跳，避免打断 ER2 生成。"""
        self._user_speaking = bool(speaking)

    def add_transcript(self, line: str) -> None:
        self._get_perception().on_transcript(line)
