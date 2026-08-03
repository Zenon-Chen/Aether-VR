"""capture-service — 屏幕与麦克风采集

节奏固定：
    音频 100ms PCM16 块，永不停止
    图像 1 秒 1 张 JPEG，永不停止

两条流各自独立，通过 async 队列对外提供，消费者（cerebellum / brain）
自行决定取用节奏。任一后端缺失时降级为空流而不是崩溃。
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import AsyncIterator, Optional

from ...contracts.perception import AudioChunk, VideoFrame

logger = logging.getLogger(__name__)

try:
    import sounddevice as sd
    _HAS_AUDIO = True
except Exception:  # pragma: no cover
    sd = None  # type: ignore[assignment]
    _HAS_AUDIO = False

try:
    import mss
    from PIL import Image
    _HAS_VIDEO = True
except Exception:  # pragma: no cover
    mss = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    _HAS_VIDEO = False


class CaptureService:
    """感知采集服务"""

    name = "capture"

    def __init__(self, config) -> None:
        self._config = config
        self._audio_q: asyncio.Queue[AudioChunk] = asyncio.Queue(maxsize=64)
        self._video_q: asyncio.Queue[VideoFrame] = asyncio.Queue(maxsize=4)
        self._latest: Optional[VideoFrame] = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._stream = None

    # ── 生命周期 ───────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        if not self._config.enabled:
            logger.warning("[Capture] 已在配置中禁用")
            return
        if _HAS_AUDIO:
            self._tasks.append(asyncio.create_task(self._audio_loop(), name="capture_audio"))
        else:
            logger.warning("[Capture] sounddevice 缺失，音频采集禁用")
        if _HAS_VIDEO:
            self._tasks.append(asyncio.create_task(self._video_loop(), name="capture_video"))
        else:
            logger.warning("[Capture] mss/Pillow 缺失，画面采集禁用")
        logger.info("[Capture] 启动（音频=%s 画面=%s）", _HAS_AUDIO, _HAS_VIDEO)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        logger.info("[Capture] 已停止")

    async def health(self) -> str:
        if not self._running:
            return "stopped"
        if not self._config.enabled:
            return "disabled"
        return "ok" if (_HAS_AUDIO or _HAS_VIDEO) else "degraded"

    # ── 对外流 ─────────────────────────────────────────────

    async def audio_stream(self) -> AsyncIterator[AudioChunk]:
        while self._running:
            yield await self._audio_q.get()

    async def video_stream(self) -> AsyncIterator[VideoFrame]:
        while self._running:
            yield await self._video_q.get()

    def latest_frame(self) -> Optional[VideoFrame]:
        """大脑每轮取最新一帧，不必消费整条流"""
        return self._latest

    # ── 音频 ───────────────────────────────────────────────

    async def _audio_loop(self) -> None:
        rate = self._config.input_sample_rate
        frames_per_chunk = int(rate * self._config.chunk_ms / 1000)
        loop = asyncio.get_running_loop()

        try:
            self._stream = sd.RawInputStream(
                samplerate=rate, channels=1, dtype="int16",
                blocksize=frames_per_chunk, device=self._config.audio_device,
            )
            self._stream.start()
        except Exception as exc:
            logger.error("[Capture] 麦克风打开失败：%s", exc)
            return

        logger.info("[Capture] 麦克风 %dHz / %dms 块", rate, self._config.chunk_ms)
        while self._running:
            try:
                data, overflowed = await loop.run_in_executor(
                    None, self._stream.read, frames_per_chunk
                )
                if overflowed:
                    logger.debug("[Capture] 音频溢出")
                await self._offer(self._audio_q, AudioChunk(bytes(data), rate))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Capture] 音频读取失败")
                await asyncio.sleep(0.2)

    # ── 画面 ───────────────────────────────────────────────

    async def _video_loop(self) -> None:
        period = 1.0 / max(0.1, self._config.video_fps)
        loop = asyncio.get_running_loop()
        logger.info("[Capture] 屏幕 %.1f FPS", self._config.video_fps)

        while self._running:
            started = time.perf_counter()
            try:
                frame = await loop.run_in_executor(None, self._grab)
                if frame is not None:
                    self._latest = frame
                    await self._offer(self._video_q, frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Capture] 截图失败")
            await asyncio.sleep(max(0.0, period - (time.perf_counter() - started)))

    def _grab(self) -> Optional[VideoFrame]:
        with mss.mss() as sct:
            index = min(self._config.monitor, len(sct.monitors) - 1)
            shot = sct.grab(sct.monitors[index])
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if image.width > self._config.max_width:
            ratio = self._config.max_width / image.width
            image = image.resize(
                (self._config.max_width, int(image.height * ratio)), Image.LANCZOS
            )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=self._config.jpeg_quality)
        return VideoFrame(buffer.getvalue(), image.width, image.height)

    # ── 内部 ───────────────────────────────────────────────

    @staticmethod
    async def _offer(queue: asyncio.Queue, item) -> None:
        """队列满时丢弃最旧项，保证实时性优先于完整性"""
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(item)
            except Exception:
                pass
