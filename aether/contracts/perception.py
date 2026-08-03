"""感知契约 — capture 服务产出的原始流片段

节奏（架构评审结论）：
    音频 100ms PCM 块，永不停止
    图像 1 秒 1 张 JPEG，永不停止
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: ER2 要求的输入音频采样率
INPUT_SAMPLE_RATE = 16000
#: 音频块时长（毫秒）
AUDIO_CHUNK_MS = 100
#: 视觉采样频率
VIDEO_FPS = 1.0


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """一段 PCM16 单声道音频"""
    pcm: bytes
    sample_rate: int = INPUT_SAMPLE_RATE
    captured_at: float = field(default_factory=time.monotonic)

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / (self.sample_rate * 2)


@dataclass(frozen=True, slots=True)
class VideoFrame:
    """一帧 JPEG 截图"""
    jpeg: bytes
    width: int = 0
    height: int = 0
    captured_at: float = field(default_factory=time.monotonic)

    @property
    def size_kb(self) -> float:
        return len(self.jpeg) / 1024
