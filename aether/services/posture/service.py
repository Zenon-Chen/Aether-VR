"""posture-service — DART → SteamVR 六设备桥（常驻）

与原 bridge.py 的关键差别：
    原实现是一次性 CLI，播完即退出 → 驱动 1 秒后把设备标记为失联。
    这里改为**常驻保活推流**：无论有无动作，始终以 keepalive_fps 推送
    当前姿势，设备永不掉线。

对调度器暴露四个来源切换原语：
    freeze()    冻结在当前末姿势持续输出
    blend_to()  插值到指定姿势
    resume()    从当前姿势插回动作流
    go_idle()   回到站立姿势
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from enum import Enum
from typing import Optional

import httpx

from ...contracts.frame import PostureFrame
from ...infra.config import DartConfig, PostureConfig
from .smpl_transform import convert_sequence, lerp_frame, standing_frame

logger = logging.getLogger(__name__)


class PostureMode(str, Enum):
    IDLE = "idle"        # 站立保活
    PLAYING = "playing"  # 播放动作序列
    FROZEN = "frozen"    # 冻结末姿势
    BLENDING = "blending"


class PostureService:
    """常驻姿态推流服务"""

    name = "posture"

    def __init__(self, config: PostureConfig, dart: DartConfig) -> None:
        self._config = config
        self._dart = dart
        self._sock: Optional[socket.socket] = None
        self._client: Optional[httpx.AsyncClient] = None

        self._current = standing_frame()
        self._queue: list[PostureFrame] = []
        self._queue_fps = 30.0
        self._cursor = 0
        self._mode = PostureMode.IDLE
        self._sequence = 0
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        self._playback_done: Optional[asyncio.Event] = None
        self._dart_ok = False

    # ── 生命周期 ───────────────────────────────────────────

    async def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._client = httpx.AsyncClient(timeout=self._dart.timeout_s)
        self._running = True
        self._task = asyncio.create_task(self._keepalive_loop(), name="posture_keepalive")
        logger.info(
            "[Posture] 保活推流启动 → %s:%d @%.0fHz",
            self._config.bridge_host, self._config.bridge_port,
            self._config.keepalive_fps,
        )
        await self.check_dart()

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
        if self._sock is not None:
            self._sock.close()
        logger.info("[Posture] 已停止")

    async def health(self) -> str:
        if not self._running:
            return "stopped"
        return "ok" if self._dart_ok else "degraded"

    async def check_dart(self) -> bool:
        """探测 DART 服务可用性"""
        if self._client is None:
            return False
        try:
            response = await self._client.get(self._dart.health_url, timeout=3.0)
            self._dart_ok = response.status_code == 200
        except Exception as exc:
            logger.warning("[Posture] DART 不可用：%s", exc)
            self._dart_ok = False
        return self._dart_ok

    # ── 推流主循环 ─────────────────────────────────────────

    async def _keepalive_loop(self) -> None:
        """恒定频率推送当前帧。这是设备不掉线的唯一保证。"""
        period = 1.0 / max(1.0, self._config.keepalive_fps)
        next_tick = time.perf_counter()
        while self._running:
            try:
                frame = self._advance()
                self._emit(frame)
            except Exception:
                logger.exception("[Posture] 推流迭代失败")
            next_tick += period
            delay = next_tick - time.perf_counter()
            if delay < -period * 5:      # 严重落后则重新对齐时钟
                next_tick = time.perf_counter()
                delay = 0.0
            await asyncio.sleep(max(0.0, delay))

    def _advance(self) -> PostureFrame:
        """按当前模式取下一帧"""
        if self._mode is PostureMode.PLAYING and self._queue:
            if self._cursor < len(self._queue):
                # 队列帧率与推流帧率不同：按时间比例取样
                frame = self._queue[self._cursor]
                self._cursor += 1
                self._current = frame
                return frame
            self._finish_playback()
        return self._current

    def _finish_playback(self) -> None:
        self._mode = PostureMode.FROZEN
        self._queue = []
        self._cursor = 0
        if self._playback_done is not None:
            self._playback_done.set()

    def _emit(self, frame: PostureFrame) -> None:
        if self._sock is None:
            return
        self._sequence += 1
        payload = frame.pack(self._sequence, time.time())
        try:
            self._sock.sendto(
                payload, (self._config.bridge_host, self._config.bridge_port)
            )
        except OSError as exc:
            logger.warning("[Posture] UDP 发送失败：%s", exc)

    # ── 对外原语 ───────────────────────────────────────────

    async def play(
        self, prompt: str, duration_s: float = 2.0, full_body: bool = False
    ) -> str:
        """向 DART 请求动作并播放。返回结果描述。"""
        if self._client is None:
            raise RuntimeError("posture 服务未启动")
        if not self._dart_ok and not await self.check_dart():
            raise RuntimeError("DART 服务不可用")

        text = _with_repeat(prompt, duration_s)
        logger.info("[Posture] 请求 DART：%s", text)
        response = await self._client.post(
            self._dart.generate_url,
            json={"text": text, "batch_size": 1, "guidance_param": 5.0},
        )
        response.raise_for_status()
        frames, fps = convert_sequence(response.json())
        if not frames:
            raise RuntimeError("DART 返回空动作")

        resampled = _resample(frames, fps, self._config.keepalive_fps)
        await self._enqueue(resampled)
        await self._await_playback()
        return f"播放 {len(frames)} 帧（{len(resampled)/self._config.keepalive_fps:.1f}s）"

    async def _enqueue(self, frames: list[PostureFrame]) -> None:
        """插入过渡段后切入新序列，避免跳变"""
        async with self._lock:
            blend_count = int(self._config.blend_s * self._config.keepalive_fps)
            bridge = [
                lerp_frame(self._current, frames[0], (i + 1) / max(1, blend_count))
                for i in range(blend_count)
            ]
            self._queue = bridge + frames
            self._cursor = 0
            self._playback_done = asyncio.Event()
            self._mode = PostureMode.PLAYING

    async def _await_playback(self) -> None:
        event = self._playback_done
        if event is not None:
            await event.wait()

    async def freeze(self) -> None:
        """冻结于当前姿势 —— 切换到预录动画前调用"""
        async with self._lock:
            self._mode = PostureMode.FROZEN
            self._queue = []
            self._cursor = 0
            if self._playback_done is not None:
                self._playback_done.set()
        logger.info("[Posture] 冻结于当前姿势")

    async def blend_to(self, frame: PostureFrame, duration_s: Optional[float] = None) -> None:
        """插值到目标姿势"""
        span = duration_s if duration_s is not None else self._config.blend_s
        steps = max(1, int(span * self._config.keepalive_fps))
        async with self._lock:
            self._queue = [
                lerp_frame(self._current, frame, (i + 1) / steps) for i in range(steps)
            ]
            self._cursor = 0
            self._playback_done = asyncio.Event()
            self._mode = PostureMode.PLAYING
        await self._await_playback()

    async def resume(self) -> None:
        """预录动画结束后平滑恢复 DART/FBT 的站立保活流。"""
        await self.blend_to(standing_frame(), self._config.blend_s)
        async with self._lock:
            self._mode = PostureMode.IDLE
        logger.info("[Posture] 已平滑恢复姿态流控制")

    async def go_idle(self) -> None:
        """回到站立姿势"""
        await self.blend_to(standing_frame(), self._config.blend_s)
        async with self._lock:
            self._mode = PostureMode.IDLE
        logger.info("[Posture] 回到站立姿势")

    async def emergency_reset(self) -> None:
        """紧急停止：立即跳到站立，不做插值"""
        async with self._lock:
            self._queue = []
            self._cursor = 0
            self._current = standing_frame()
            self._mode = PostureMode.IDLE
            if self._playback_done is not None:
                self._playback_done.set()
        self._emit(self._current)

    @property
    def mode(self) -> PostureMode:
        return self._mode

    @property
    def frames_sent(self) -> int:
        """已发出的帧数 —— 用于验证保活频率"""
        return self._sequence

    @property
    def current_frame(self) -> PostureFrame:
        return self._current


# ── 辅助 ───────────────────────────────────────────────────

def _with_repeat(prompt: str, duration_s: float) -> str:
    """DART 用 `动作*N` 表示重复次数；一个 primitive 约 0.5 秒"""
    if "*" in prompt:
        return prompt
    repeats = max(1, round(duration_s / 0.5))
    return f"{prompt}*{repeats}"


def _resample(frames: list[PostureFrame], src_fps: float, dst_fps: float) -> list[PostureFrame]:
    """把动作序列重采样到推流帧率"""
    if not frames or abs(src_fps - dst_fps) < 0.5:
        return list(frames)
    duration = len(frames) / src_fps
    count = max(1, int(duration * dst_fps))
    out: list[PostureFrame] = []
    for i in range(count):
        pos = i * src_fps / dst_fps
        low = int(pos)
        if low >= len(frames) - 1:
            out.append(frames[-1])
        else:
            out.append(lerp_frame(frames[low], frames[low + 1], pos - low))
    return out
