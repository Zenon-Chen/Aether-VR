"""Provider-independent bounded playback with an uninterrupted keepalive clock."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from enum import Enum

from ...contracts.frame import PostureFrame
from .smpl_transform import lerp_frame, standing_frame

logger = logging.getLogger(__name__)


class PlaybackMode(str, Enum):
    IDLE = "idle"
    PLAYING = "playing"
    FROZEN = "frozen"
    BLENDING = "blending"


class MotionPlayback:
    def __init__(
        self,
        fps: float,
        blend_s: float,
        emit: Callable[[PostureFrame], None],
        *,
        max_buffer_s: float = 30.0,
    ) -> None:
        self.fps = max(1.0, fps)
        self.blend_s = max(0.0, blend_s)
        self._emit = emit
        self._max_frames = max(1, int(self.fps * max_buffer_s))
        self.current = standing_frame()
        self.mode = PlaybackMode.IDLE
        self._queue: list[PostureFrame] = []
        self._cursor = 0
        self._done: asyncio.Event | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._keepalive_loop(), name="motion_keepalive")

    async def stop(self) -> None:
        self._running = False
        await self.cancel()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _keepalive_loop(self) -> None:
        period = 1.0 / self.fps
        next_tick = time.perf_counter()
        while self._running:
            try:
                async with self._lock:
                    if self.mode is PlaybackMode.PLAYING and self._cursor < len(self._queue):
                        self.current = self._queue[self._cursor]
                        self._cursor += 1
                        if self._cursor >= len(self._queue):
                            self._finish_locked()
                    frame = self.current
                self._emit(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[MotionPlayback] keepalive tick failed")
            next_tick += period
            delay = next_tick - time.perf_counter()
            if delay < -period * 5:
                next_tick = time.perf_counter()
                delay = 0.0
            await asyncio.sleep(max(0.0, delay))

    def prepare(
        self,
        frames: list[PostureFrame],
        source_fps: float,
        *,
        full_body: bool,
    ) -> list[PostureFrame]:
        scoped = _apply_scope(frames, self.current, full_body)
        return _resample(scoped, source_fps, self.fps)

    async def play_motion(self, frames: list[PostureFrame]) -> None:
        if not frames:
            raise ValueError("动作序列不能为空")
        blend_count = int(self.blend_s * self.fps)
        bridge = [
            lerp_frame(self.current, frames[0], (index + 1) / max(1, blend_count))
            for index in range(blend_count)
        ]
        queue = bridge + frames
        if len(queue) > self._max_frames:
            raise ValueError("动作超过播放缓冲上限")
        async with self._lock:
            self._signal_locked()
            self._queue = queue
            self._cursor = 0
            self._done = asyncio.Event()
            self.mode = PlaybackMode.PLAYING
            done = self._done
        await done.wait()

    async def cancel(self) -> None:
        """Cancel queued playback but keep streaming the current pose."""
        async with self._lock:
            self._queue.clear()
            self._cursor = 0
            self.mode = PlaybackMode.FROZEN
            self._signal_locked()

    async def freeze(self) -> None:
        await self.cancel()

    async def blend_to(self, frame: PostureFrame, duration_s: float | None = None) -> None:
        duration = self.blend_s if duration_s is None else max(0.0, duration_s)
        steps = max(1, int(duration * self.fps))
        sequence = [
            lerp_frame(self.current, frame, (index + 1) / steps) for index in range(steps)
        ]
        await self.play_motion(sequence)

    async def resume(self) -> None:
        await self.blend_to(standing_frame())
        async with self._lock:
            self.mode = PlaybackMode.IDLE

    async def go_idle(self) -> None:
        await self.resume()

    async def emergency(self) -> None:
        async with self._lock:
            self._queue.clear()
            self._cursor = 0
            self.current = standing_frame()
            self.mode = PlaybackMode.IDLE
            self._signal_locked()
            frame = self.current
        self._emit(frame)

    def _finish_locked(self) -> None:
        self._queue.clear()
        self._cursor = 0
        self.mode = PlaybackMode.FROZEN
        self._signal_locked()

    def _signal_locked(self) -> None:
        if self._done is not None:
            self._done.set()
            self._done = None


def _apply_scope(
    frames: list[PostureFrame], current: PostureFrame, full_body: bool
) -> list[PostureFrame]:
    if full_body:
        return list(frames)
    return [
        PostureFrame(frame.devices[:3] + current.devices[3:]) for frame in frames
    ]


def _resample(
    frames: list[PostureFrame], source_fps: float, target_fps: float
) -> list[PostureFrame]:
    if not frames or abs(source_fps - target_fps) < 0.5:
        return list(frames)
    duration = len(frames) / source_fps
    count = max(1, int(duration * target_fps))
    result: list[PostureFrame] = []
    for index in range(count):
        position = index * source_fps / target_fps
        low = int(position)
        result.append(
            frames[-1]
            if low >= len(frames) - 1
            else lerp_frame(frames[low], frames[low + 1], position - low)
        )
    return result
