"""speech-service — TTS → 虚拟声卡 → VRChat 麦克风

链路：
    文本 → 流式 TTS → 本地播放队列（一次只播一条）
        → 虚拟声卡 → VRChat 麦克风 → 自动口型（Avatar 需预配 Viseme）

播放开始/结束回写 StateStore；合成失败播放预录兜底音。
"""

from __future__ import annotations

import asyncio
import logging
import re
import wave
from pathlib import Path
from typing import Optional

from ...infra.config import SpeechConfig

logger = logging.getLogger(__name__)

try:
    import sounddevice as sd
    import numpy as np
    _HAS_AUDIO = True
except Exception:  # pragma: no cover
    sd = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    _HAS_AUDIO = False

try:
    from google import genai
    from google.genai import types as genai_types
    _HAS_GENAI = True
except Exception:  # pragma: no cover
    genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]
    _HAS_GENAI = False

#: 中文/英文句子边界
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")

#: Gemini 瞬时过载只在服务内做少量重试；Scheduler 仍保留一次机械重试。
_TRANSIENT_RETRY_DELAYS = (0.25, 0.75)


class SpeechUnavailableError(OSError):
    """没有任何可播放语音 —— 可由 Scheduler 作为机械失败重试。"""


def _is_transient_unavailable(exc: BaseException) -> bool:
    """识别 Gemini 过载响应，兼容 SDK 异常文本和 code/status 属性。"""
    code = str(getattr(exc, "code", "")).upper()
    status = str(getattr(exc, "status", "")).upper()
    message = str(exc).upper()
    return "503" in code or "503" in message or "UNAVAILABLE" in status or "UNAVAILABLE" in message


class SpeechService:
    """语音合成与播放"""

    name = "speech"

    def __init__(self, config: SpeechConfig) -> None:
        self._config = config
        self._client = None
        self._device_index: Optional[int] = None
        self._current: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._stop_flag = asyncio.Event()
        self._fallback: Optional[bytes] = None
        self._fallback_ready = False
        self._healthy = False

    # ── 生命周期 ───────────────────────────────────────────

    async def start(self) -> None:
        if _HAS_GENAI and self._config.api_key:
            self._client = genai.Client(api_key=self._config.api_key)
            self._healthy = True
        else:
            logger.warning("[Speech] Gemini 不可用，TTS 降级为静默")

        self._device_index = self._resolve_device()
        self._fallback = self._load_fallback()
        self._fallback_ready = self._fallback is not None
        if not self._fallback_ready:
            raise SpeechUnavailableError(
                f"fallback WAV 不可用: {self._config.fallback_sound}"
            )
        logger.info(
            "[Speech] 就绪（设备=%s 音色=%s）",
            self._config.output_device or "系统默认", self._config.voice_name,
        )

    async def stop(self) -> None:
        await self.halt()
        self._client = None

    async def health(self) -> str:
        return "ok" if self._healthy and self._fallback_ready else "degraded"

    async def status(self) -> dict[str, object]:
        """返回可观察详情；health 保持 ServiceHealth 所需的稳定枚举值。"""
        return {
            "status": await self.health(),
            "tts_ready": self._healthy,
            "fallback_ready": self._fallback_ready,
            "fallback_sound": self._config.fallback_sound,
        }

    # ── 说话 ───────────────────────────────────────────────

    async def speak(self, text: str) -> float:
        """合成并播放。返回播放时长（秒）。同一时刻只播一条。"""
        clean = (text or "").strip()
        if not clean:
            return 0.0

        async with self._lock:
            self._stop_flag.clear()
            segments = self._segment(clean)
            logger.info("[Speech] 说：%s（%d 段）", clean[:40], len(segments))

            total = 0.0
            try:
                for segment in segments:
                    if self._stop_flag.is_set():
                        logger.info("[Speech] 被打断，剩余段落丢弃")
                        break
                    pcm = await self._synthesize(segment)
                    using_fallback = pcm is None
                    if using_fallback:
                        pcm = self._fallback
                    if pcm is None:
                        raise SpeechUnavailableError("TTS 失败且 fallback WAV 不可用")
                    played = await self._play(pcm)
                    if played <= 0.0:
                        source = "fallback WAV" if using_fallback else "TTS 音频"
                        raise SpeechUnavailableError(f"{source} 播放失败")
                    total += played
            except asyncio.CancelledError:
                await self.halt()
                raise

            if not self._stop_flag.is_set() and total <= 0.0:
                raise SpeechUnavailableError("TTS 未产生可播放音频")
            return total

    async def halt(self) -> None:
        """立即停止播放 —— 用户打断与紧急停止调用"""
        self._stop_flag.set()
        if _HAS_AUDIO and sd is not None:
            try:
                sd.stop()
            except Exception:
                pass

    # ── 合成 ───────────────────────────────────────────────

    def _segment(self, text: str) -> list[str]:
        """按句子边界切分，过长句再按字数硬切，降低首字延迟"""
        limit = self._config.max_segment_chars
        pieces: list[str] = []
        for raw in _SENTENCE_SPLIT.split(text):
            sentence = raw.strip()
            if not sentence:
                continue
            while len(sentence) > limit:
                pieces.append(sentence[:limit])
                sentence = sentence[limit:]
            if sentence:
                pieces.append(sentence)
        return pieces or [text]

    async def _synthesize(self, text: str) -> Optional[bytes]:
        if self._client is None:
            return None

        attempts = len(_TRANSIENT_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self._config.model,
                    contents=text,
                    config=genai_types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=genai_types.SpeechConfig(
                            voice_config=genai_types.VoiceConfig(
                                prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                                    voice_name=self._config.voice_name,
                                )
                            )
                        ),
                    ),
                )
                part = response.candidates[0].content.parts[0]
                self._healthy = True
                return part.inline_data.data
            except Exception as exc:
                self._healthy = False
                if _is_transient_unavailable(exc) and attempt < attempts - 1:
                    delay = _TRANSIENT_RETRY_DELAYS[attempt]
                    logger.warning(
                        "[Speech] Gemini 暂时不可用，%.2fs 后重试（%d/%d）：%s",
                        delay, attempt + 1, attempts - 1, exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error("[Speech] 合成失败：%s", exc)
                return None
        return None

    # ── 播放 ───────────────────────────────────────────────

    async def _play(self, pcm: bytes) -> float:
        if not _HAS_AUDIO or not pcm:
            return 0.0
        rate = self._config.output_sample_rate
        duration = len(pcm) / (rate * 2)
        loop = asyncio.get_running_loop()
        samples = np.frombuffer(pcm, dtype=np.int16)
        play_future = loop.run_in_executor(
            None,
            lambda: sd.play(
                samples, samplerate=rate, device=self._device_index, blocking=True
            ),
        )
        try:
            await play_future
        except asyncio.CancelledError:
            await self.halt()
            await asyncio.gather(play_future, return_exceptions=True)
            raise
        except Exception as exc:
            self._healthy = False
            logger.error("[Speech] 播放失败：%s", exc)
            return 0.0
        return duration

    def _resolve_device(self) -> Optional[int]:
        """按名称匹配虚拟声卡；找不到则退回系统默认"""
        target = self._config.output_device
        if not target or not _HAS_AUDIO:
            return None
        try:
            for index, device in enumerate(sd.query_devices()):
                if device["max_output_channels"] > 0 and target.lower() in device["name"].lower():
                    logger.info("[Speech] 输出设备 #%d %s", index, device["name"])
                    return index
        except Exception:
            logger.exception("[Speech] 枚举音频设备失败")
        logger.warning("[Speech] 未找到虚拟声卡「%s」，使用系统默认", target)
        return None

    def _load_fallback(self) -> Optional[bytes]:
        path = Path(self._config.fallback_sound)
        if not path.exists():
            return None
        try:
            with wave.open(str(path), "rb") as handle:
                if handle.getsampwidth() != 2:
                    raise ValueError("必须为 PCM16")
                if handle.getnchannels() != 1:
                    raise ValueError("必须为 mono")
                if handle.getframerate() != self._config.output_sample_rate:
                    raise ValueError(
                        f"采样率必须为 {self._config.output_sample_rate}Hz"
                    )
                frames = handle.readframes(handle.getnframes())
                if not frames:
                    raise ValueError("WAV 无音频帧")
                return frames
        except Exception as exc:
            logger.warning("[Speech] 兜底音加载失败：%s（%s）", path, exc)
            return None
