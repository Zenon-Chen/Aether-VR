"""cerebellum-service — ER2 Live API 客户端（小脑）

只负责会话管理与消息收发；工具调用的语义解释交给 planner，
执行交给调度器。这样 ER2 的接口变动被隔离在本文件内。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from ...contracts.perception import AudioChunk, VideoFrame
from ...infra.config import CerebellumConfig
from .prompts import build_system_prompt, build_tool_declarations

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types
    _HAS_GENAI = True
except Exception:  # pragma: no cover
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    _HAS_GENAI = False

ToolCallHandler = Callable[[Any], Awaitable[None]]
TextHandler = Callable[[str], Awaitable[None]]
TranscriptHandler = Callable[[str], None]


class CerebellumService:
    """ER2 Live 会话"""

    name = "cerebellum"

    def __init__(
        self,
        config: CerebellumConfig,
        actions: tuple[str, ...] = (),
        directions: tuple[str, ...] = (),
    ) -> None:
        self._config = config
        self._actions = actions
        self._directions = directions
        self._client = None
        self._session = None
        self._session_cm = None
        self._resumption_token: Optional[str] = None
        self._generating = False
        self._connected = False
        self._on_tool_call: Optional[ToolCallHandler] = None
        self._on_text: Optional[TextHandler] = None
        self._on_transcript: Optional[TranscriptHandler] = None
        self._input_transcript_parts: list[str] = []
        self._send_lock = asyncio.Lock()

    # ── 注入 ───────────────────────────────────────────────

    def set_tool_call_handler(self, handler: ToolCallHandler) -> None:
        self._on_tool_call = handler

    def set_text_handler(self, handler: TextHandler) -> None:
        self._on_text = handler

    def set_transcript_handler(self, handler: TranscriptHandler) -> None:
        self._on_transcript = handler

    # ── 生命周期 ───────────────────────────────────────────

    async def start(self) -> None:
        await self.connect()

    async def connect(self) -> None:
        if not _HAS_GENAI:
            raise ImportError("需要 google-genai：pip install google-genai")
        if not self._config.api_key:
            raise RuntimeError("缺少 GEMINI_API_KEY")

        self._client = genai.Client(api_key=self._config.api_key)
        config = self._build_config()

        logger.info("[Cerebellum] 连接 %s ...", self._config.model)
        self._session_cm = self._client.aio.live.connect(
            model=self._config.model, config=config
        )
        self._session = await self._session_cm.__aenter__()
        self._connected = True
        logger.info("[Cerebellum] 会话已建立")

    def _build_config(self) -> dict:
        config: dict[str, Any] = {
            "response_modalities": self._config.response_modalities,
            "system_instruction": build_system_prompt(self._actions, self._directions),
            "tools": build_tool_declarations(),
            "input_audio_transcription": types.AudioTranscriptionConfig(),
            "context_window_compression": types.ContextWindowCompressionConfig(
                trigger_tokens=self._config.compression_trigger_tokens,
                sliding_window=types.SlidingWindow(
                    target_tokens=self._config.compression_target_tokens
                ),
            ),
        }
        if self._resumption_token:
            config["session_resumption"] = types.SessionResumptionConfig(
                handle=self._resumption_token
            )
        return config

    async def stop(self) -> None:
        self._connected = False
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._session = None
        self._session_cm = None
        logger.info("[Cerebellum] 已断开")

    async def health(self) -> str:
        return "ok" if self._connected else "stopped"

    @property
    def generating(self) -> bool:
        """ER2 是否正在生成 —— 心跳闸门据此推迟发送"""
        return self._generating

    @property
    def connected(self) -> bool:
        return self._connected

    # ── 发送 ───────────────────────────────────────────────

    async def send_audio(self, chunk: AudioChunk) -> None:
        if self._session is None:
            return
        async with self._send_lock:
            try:
                await self._session.send_realtime_input(
                    audio=types.Blob(
                        data=chunk.pcm,
                        mime_type=f"audio/pcm;rate={chunk.sample_rate}",
                    )
                )
            except Exception as exc:
                logger.warning("[Cerebellum] 音频发送失败：%s", exc)
                self._connected = False

    async def send_video(self, frame: VideoFrame) -> None:
        if self._session is None:
            return
        async with self._send_lock:
            try:
                await self._session.send_realtime_input(
                    video=types.Blob(data=frame.jpeg, mime_type="image/jpeg")
                )
            except Exception as exc:
                logger.warning("[Cerebellum] 画面发送失败：%s", exc)

    async def send_heartbeat(self, text: str) -> None:
        if self._session is None:
            return
        async with self._send_lock:
            try:
                await self._session.send_realtime_input(text=text)
                logger.debug("[Cerebellum] 心跳已发送")
            except Exception as exc:
                logger.warning("[Cerebellum] 心跳发送失败：%s", exc)

    async def send_tool_response(self, responses: list) -> None:
        """回传 function_response —— 必须闭合，否则 ER2 会话会卡死"""
        if self._session is None or not responses:
            return
        async with self._send_lock:
            try:
                await self._session.send_tool_response(function_responses=responses)
            except Exception as exc:
                logger.error("[Cerebellum] 工具响应回传失败：%s", exc)

    # ── 接收 ───────────────────────────────────────────────

    async def receive_loop(self) -> None:
        """持续接收服务端消息"""
        while self._connected:
            try:
                if self._session is None:
                    await asyncio.sleep(0.5)
                    continue
                async for chunk in self._session.receive():
                    await self._handle(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[Cerebellum] 接收中断：%s", exc)
                self._generating = False
                await self._reconnect()

    async def _handle(self, chunk) -> None:
        server_content = getattr(chunk, "server_content", None)
        if server_content is not None:
            transcription = getattr(server_content, "input_transcription", None)
            if transcription is not None:
                text = str(getattr(transcription, "text", "") or "").strip()
                if text:
                    self._input_transcript_parts.append(text)
                if getattr(transcription, "finished", False):
                    line = " ".join(self._input_transcript_parts).strip()
                    self._input_transcript_parts.clear()
                    if line and self._on_transcript is not None:
                        self._on_transcript(line)

            if getattr(server_content, "turn_complete", False):
                self._generating = False
            elif getattr(server_content, "model_turn", None) is not None:
                self._generating = True

        text = getattr(chunk, "text", None)
        if text and self._on_text is not None:
            await self._on_text(text)

        tool_call = getattr(chunk, "tool_call", None)
        if tool_call is not None and self._on_tool_call is not None:
            self._generating = False
            await self._on_tool_call(tool_call)

        update = getattr(chunk, "session_resumption_update", None)
        if update is not None and getattr(update, "resumable", False):
            self._resumption_token = update.new_handle

        if getattr(chunk, "go_away", None) is not None:
            logger.warning("[Cerebellum] 收到 GoAway，准备重连")
            await self._reconnect()

    async def _reconnect(self) -> None:
        """用恢复句柄重连；失败则冷启动"""
        await asyncio.sleep(1.0)
        try:
            if self._session_cm is not None:
                try:
                    await self._session_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            await self.connect()
        except Exception as exc:
            logger.error("[Cerebellum] 重连失败：%s", exc)
            self._resumption_token = None
            self._connected = False
