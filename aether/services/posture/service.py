"""Provider-neutral posture façade over generation, projection, playback, and sink."""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from ...contracts.frame import PostureFrame
from ...contracts.motion import MotionRequest
from ...infra.config import DartConfig, PostureConfig
from ..motion.providers.dart import DartMotionProvider
from ..ports import MotionProviderPort
from .playback import MotionPlayback, PlaybackMode, _apply_scope, _resample
from .projector import TrackerProjector
from .sink import SteamVRTrackerSink
from .smpl_transform import convert_sequence, lerp_frame, standing_frame

# Preserve the public enum name while keeping a single playback state type.
PostureMode = PlaybackMode


class PostureService:
    name = "posture"

    def __init__(
        self,
        config: PostureConfig,
        dart: DartConfig,
        motion_provider: MotionProviderPort | None = None,
        *,
        projector: TrackerProjector | None = None,
        sink: SteamVRTrackerSink | None = None,
        playback: MotionPlayback | None = None,
    ) -> None:
        self._config = config
        self._provider = motion_provider or DartMotionProvider(
            f"http://{dart.host}:{dart.port}", timeout_s=dart.timeout_s
        )
        self._projector = projector or TrackerProjector()
        self._sink = sink or SteamVRTrackerSink(config.bridge_host, config.bridge_port)
        self._playback = playback or MotionPlayback(
            config.keepalive_fps, config.blend_s, self._sink.send
        )
        self._running = False

    async def start(self) -> None:
        # Keep compatibility with legacy test doubles while real providers own startup.
        start = getattr(self._provider, "start", None)
        if start is not None:
            await start()
        self._sink.start()
        self._running = True
        await self._playback.start()

    async def stop(self) -> None:
        self._running = False
        await self._playback.stop()
        await self._provider.close()
        self._sink.close()

    async def health(self) -> str:
        if not self._running:
            return "stopped"
        report = await self._provider.health()
        return "ok" if report.healthy else "degraded"

    async def check_dart(self) -> bool:
        """Compatibility probe; callers should prefer provider health reports."""
        return (await self._provider.health()).healthy

    async def play(
        self, prompt: str, duration_s: float = 2.0, full_body: bool = False
    ) -> str:
        if not self._running:
            raise RuntimeError("posture 服务未启动")
        request = MotionRequest(
            request_id=str(uuid.uuid4()),
            prompt=prompt,
            duration_s=duration_s,
            scope="full_body" if full_body else "upper_body",
        )
        # Provider latency never runs on the 45Hz playback task.
        motion = await self._provider.generate(request)
        frames = self._projector.project(motion)
        prepared = self._playback.prepare(frames, motion.fps, full_body=full_body)
        try:
            await self._playback.play_motion(prepared)
        except asyncio.CancelledError:
            await self._playback.cancel()
            raise
        return (
            f"播放 {motion.frame_count} 帧"
            f"（{len(prepared) / self._config.keepalive_fps:.1f}s）"
        )

    async def freeze(self) -> None:
        await self._playback.freeze()

    async def blend_to(
        self, frame: PostureFrame, duration_s: Optional[float] = None
    ) -> None:
        await self._playback.blend_to(frame, duration_s)

    async def resume(self) -> None:
        await self._playback.resume()

    async def go_idle(self) -> None:
        await self._playback.go_idle()

    async def emergency_reset(self) -> None:
        await self._playback.emergency()

    @property
    def mode(self) -> PostureMode:
        return self._playback.mode

    @property
    def frames_sent(self) -> int:
        return self._sink.sequence

    @property
    def current_frame(self) -> PostureFrame:
        return self._playback.current


__all__ = [
    "PostureService", "PostureMode", "convert_sequence", "standing_frame",
    "lerp_frame", "_apply_scope", "_resample",
]
