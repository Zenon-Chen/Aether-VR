from __future__ import annotations

import asyncio
import socket
import time

from aether.contracts.motion import (
    CanonicalMotion, MotionCapabilities, MotionHealthReport, MotionRequest,
)
from aether.contracts.plan import ActionKind, PlanStep
from aether.infra.config import AetherConfig, DartConfig, PostureConfig
from aether.runtime.agent import AetherRuntime
from aether.services.posture import PostureMode, PostureService


def _motion(request_id: str, frames: int = 8) -> CanonicalMotion:
    identity = (1.0, 0.0, 0.0, 0.0)
    return CanonicalMotion(
        schema_version="1.0",
        request_id=request_id,
        provider_id="fake",
        fps=30.0,
        frame_count=frames,
        skeleton_id="humanoid-22",
        up_axis="z",
        units="meters",
        root_translations=tuple((0.0, 0.0, 0.9) for _ in range(frames)),
        joint_rotations=tuple(tuple(identity for _ in range(22)) for _ in range(frames)),
    )


class FakeProvider:
    def __init__(self, *, delay: float = 0.0, frames: int = 8) -> None:
        self.delay = delay
        self.frames = frames
        self.requests: list[MotionRequest] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def health(self) -> MotionHealthReport:
        return MotionHealthReport("fake", True, "ok")

    async def status(self) -> MotionHealthReport:
        return await self.health()

    def capabilities(self) -> MotionCapabilities:
        return MotionCapabilities("fake")

    async def generate(self, request: MotionRequest) -> CanonicalMotion:
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        return _motion(request.request_id, self.frames)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_slow_provider_does_not_interrupt_keepalive() -> None:
    async def scenario() -> None:
        provider = FakeProvider(delay=0.25)
        service = PostureService(
            PostureConfig(bridge_port=_free_port(), keepalive_fps=45.0, blend_s=0.05),
            DartConfig(),
            motion_provider=provider,
        )
        await service.start()
        before = service.frames_sent
        task = asyncio.create_task(service.play("slow", 1.0, False))
        await asyncio.sleep(0.15)
        assert not task.done()
        assert service.frames_sent - before >= 5
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        sent = service.frames_sent
        await asyncio.sleep(0.1)
        assert service.frames_sent > sent
        assert service.mode in {PostureMode.IDLE, PostureMode.FROZEN}
        await service.stop()
        assert provider.closed

    asyncio.run(scenario())


def test_cancel_queued_motion_keeps_streaming_frozen_pose() -> None:
    async def scenario() -> None:
        provider = FakeProvider(frames=120)
        service = PostureService(
            PostureConfig(bridge_port=_free_port(), keepalive_fps=45.0, blend_s=0.05),
            DartConfig(),
            motion_provider=provider,
        )
        await service.start()
        task = asyncio.create_task(service.play("long", 4.0, True))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        frozen = service.current_frame
        sent = service.frames_sent
        assert service.mode is PostureMode.FROZEN
        await asyncio.sleep(0.1)
        assert service.frames_sent > sent
        assert service.current_frame == frozen
        await service.stop()

    asyncio.run(scenario())


def test_runtime_accepts_provider_injection() -> None:
    provider = FakeProvider()
    runtime = AetherRuntime(AetherConfig(), motion_provider=provider)
    assert runtime.motion_provider is provider
    assert runtime.posture._provider is provider


def test_runtime_motion_failure_uses_configured_prerecorded_fallback() -> None:
    async def scenario() -> None:
        runtime = AetherRuntime(AetherConfig())
        calls: list[str] = []

        async def fail(*_args):
            raise RuntimeError("provider down")

        async def play(name: str):
            calls.append(name)

        runtime.posture.play = fail
        runtime.avatar.play_action = play
        result = await runtime._exec_motion(PlanStep(
            1, ActionKind.GENERATE_MOTION,
            {"prompt": "hello", "scope": "upper_body", "duration_s": 1.0},
        ))
        assert calls == ["wave"]
        assert "provider down" in result and "wave" in result

    asyncio.run(scenario())


def test_runtime_full_body_prerecorded_action_owns_and_restores_body() -> None:
    async def scenario() -> None:
        runtime = AetherRuntime(AetherConfig())
        calls: list[str] = []

        async def record(name):
            calls.append(name)

        runtime.locomotion.halt = lambda: record("halt")
        runtime.posture.freeze = lambda: record("freeze")
        runtime.avatar.play_action = lambda _name: record("play")
        runtime.avatar.reset = lambda: record("idle")
        runtime.posture.resume = lambda: record("resume")
        await runtime._exec_play_action(PlanStep(
            1, ActionKind.PLAY_ACTION, {"name": "bow", "full_body": True},
        ))
        assert calls == ["halt", "freeze", "play", "idle", "resume"]

    asyncio.run(scenario())
