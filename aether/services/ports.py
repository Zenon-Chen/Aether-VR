"""服务端口协议 — 各服务对外的最小契约

改动服务实现前先确认这里的约定。只要 Port 不变，任何服务都可以
被替换、mock 或拆成独立进程，而不影响 runtime 装配与其他服务。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable

from ..contracts.facts import WorldFacts
from ..contracts.frame import PostureFrame
from ..contracts.observation import BrainOutput
from ..contracts.motion import (
    CanonicalMotion, MotionCapabilities, MotionHealthReport, MotionRequest,
)
from ..contracts.perception import AudioChunk, VideoFrame
from ..contracts.plan import Plan


@runtime_checkable
class Service(Protocol):
    """所有服务的通用生命周期"""
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> str: ...


@runtime_checkable
class CapturePort(Protocol):
    """感知采集：屏幕 1FPS + 麦克风 100ms 块"""
    def audio_stream(self) -> AsyncIterator[AudioChunk]: ...
    def video_stream(self) -> AsyncIterator[VideoFrame]: ...
    def latest_frame(self) -> Optional[VideoFrame]: ...


@runtime_checkable
class BrainPort(Protocol):
    """人格与观察层（Gemini Flash Lite）"""
    async def observe(
        self,
        facts_text: str,
        transcript: str,
        frame: Optional[VideoFrame],
        memories: str = "",
    ) -> Optional[BrainOutput]: ...


@runtime_checkable
class CerebellumPort(Protocol):
    """运动编排层（ER2 Live）"""
    async def connect(self) -> None: ...
    async def send_audio(self, chunk: AudioChunk) -> None: ...
    async def send_video(self, frame: VideoFrame) -> None: ...
    async def send_heartbeat(self, text: str) -> None: ...
    async def send_tool_response(self, responses: list[Any]) -> None: ...
    def set_transcript_handler(self, handler: Any) -> None: ...
    @property
    def generating(self) -> bool: ...


@runtime_checkable
class SpeechPort(Protocol):
    """TTS → 虚拟麦克风"""
    async def speak(self, text: str) -> float: ...
    async def stop(self) -> None: ...


@runtime_checkable
class MotionProviderPort(Protocol):
    """供应商中立的动作生成端口。"""
    async def close(self) -> None: ...
    async def health(self) -> MotionHealthReport: ...
    async def status(self) -> MotionHealthReport: ...
    def capabilities(self) -> MotionCapabilities: ...
    async def generate(self, request: MotionRequest) -> CanonicalMotion: ...


@runtime_checkable
class MotionTranslatorPort(Protocol):
    """把供应商响应转换为规范动作。"""
    def translate(
        self, payload: Any, *, request_id: str, provider_id: str
    ) -> CanonicalMotion: ...


@runtime_checkable
class PosturePort(Protocol):
    """DART → SteamVR 六设备桥

    必须常驻推流：驱动 1 秒收不到包即把设备标记为失联。
    """
    async def play(self, prompt: str, duration_s: float, full_body: bool) -> str: ...
    async def freeze(self) -> None: ...
    async def blend_to(self, frame: PostureFrame, duration_s: float = 0.3) -> None: ...
    async def resume(self) -> None: ...
    async def go_idle(self) -> None: ...


@runtime_checkable
class AvatarPort(Protocol):
    """预录动作与表情 —— OSC Avatar Parameters（int/float/bool）"""
    async def play_action(self, name: str) -> float: ...
    async def set_expression(self, name: str) -> None: ...
    async def reset(self) -> None: ...


@runtime_checkable
class LocomotionPort(Protocol):
    """移动 —— OSC 输入轴 + 超时归零硬保险"""
    async def move(self, direction: str, duration_s: float) -> None: ...
    async def halt(self) -> None: ...


@runtime_checkable
class ScribePort(Protocol):
    """书记官 —— 重要性标记、三档压缩、两级驱逐、落盘"""
    async def note(self, kind: str, content: str) -> None: ...
    async def recall(self, query: str, max_tokens: int = 2000) -> str: ...
    async def recall_recent(self, max_tokens: int = 2000) -> str: ...
    async def flush(self) -> None: ...


__all__ = [
    "Service", "CapturePort", "BrainPort", "CerebellumPort", "SpeechPort",
    "MotionProviderPort", "MotionTranslatorPort", "PosturePort", "AvatarPort",
    "LocomotionPort", "ScribePort",
    "WorldFacts", "Plan",
]
