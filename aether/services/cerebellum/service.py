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


class _ReconnectRequested(Exception):
    """服务端要求当前 receive 流退出并重连。"""


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
        self._stopping = False
        self._generation = 0
        self._on_tool_call: Optional[ToolCallHandler] = None
        self._on_text: Optional[TextHandler] = None
        self._on_transcript: Optional[TranscriptHandler] = None
        self._input_transcript_parts: list[str] = []
        self._send_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_task: Optional[asyncio.Task] = None

    # ── 注入 ───────────────────────────────────────────────

    def set_tool_call_handler(self, handler: ToolCallHandler) -> None:
        self._on_tool_call = handler

    def set_text_handler(self, handler: TextHandler) -> None:
        self._on_text = handler

    def set_transcript_handler(self, handler: TranscriptHandler) -> None:
        self._on_transcript = handler

    # ── 生命周期 ───────────────────────────────────────────

    async def start(self) -> None:
        self._stopping = False
        await self.connect()

    async def connect(self) -> None:
        if not _HAS_GENAI:
            raise ImportError("需要 google-genai：pip install google-genai")
        if not self._config.api_key:
            raise RuntimeError("缺少 GEMINI_API_KEY")

        self._client = genai.Client(api_key=self._config.api_key)
        config = self._build_config()

        logger.info("[Cerebellum] 连接 %s ...", self._config.model)
        session_cm = self._client.aio.live.connect(
            model=self._config.model, config=config
        )
        session = await session_cm.__aenter__()
        self._session_cm = session_cm
        self._session = session
        self._generation += 1
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
        self._stopping = True
        self._connected = False
        reconnect_task = self._reconnect_task
        if reconnect_task is not None and reconnect_task is not asyncio.current_task():
            reconnect_task.cancel()
            await asyncio.gather(reconnect_task, return_exceptions=True)
        await self._discard_session()
        logger.info("[Cerebellum] 已断开")

    async def _discard_session(self) -> None:
        """先摘除 session，使所有发送立即静默丢弃，再关闭旧连接。"""
        session_cm = self._session_cm
        self._session = None
        self._session_cm = None
        self._connected = False
        if session_cm is not None:
            try:
                await session_cm.__aexit__(None, None, None)
            except Exception:
                pass

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

    @staticmethod
    def _invalidates_resumption(exc: Exception) -> bool:
        """1007/无效参数类协议错误表示当前 resumption handle 不可再用。"""
        code = getattr(exc, "code", None)
        message = str(exc).lower()
        return code == 1007 or any(
            marker in message
            for marker in ("1007", "invalid argument", "invalid frame payload")
        )

    async def _send(self, operation: Callable[[Any], Awaitable[None]]) -> None:
        if not self._connected or self._session is None or self._stopping:
            return
        async with self._send_lock:
            session = self._session
            generation = self._generation
            if not self._connected or session is None or self._stopping:
                return
            try:
                await operation(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 仅第一个失败者记录并摘除旧会话；排队发送随后会静默丢弃。
                if session is self._session:
                    logger.warning("[Cerebellum] 会话发送中断，准备重连：%s", exc)
                    if self._invalidates_resumption(exc):
                        self._resumption_token = None
                    await self._discard_session()
                    self._ensure_reconnect(generation)

    def _ensure_reconnect(self, generation: int) -> asyncio.Task:
        task = self._reconnect_task
        if task is None or task.done():
            task = asyncio.create_task(
                self._reconnect(generation), name="er2_reconnect"
            )
            self._reconnect_task = task
        return task

    async def send_audio(self, chunk: AudioChunk) -> None:
        async def send(session) -> None:
            await session.send_realtime_input(
                audio=types.Blob(
                    data=chunk.pcm,
                    mime_type=f"audio/pcm;rate={chunk.sample_rate}",
                )
            )
        await self._send(send)

    async def send_video(self, frame: VideoFrame) -> None:
        async def send(session) -> None:
            await session.send_realtime_input(
                video=types.Blob(data=frame.jpeg, mime_type="image/jpeg")
            )
        await self._send(send)

    async def send_heartbeat(self, text: str) -> None:
        async def send(session) -> None:
            await session.send_realtime_input(text=text)
            logger.debug("[Cerebellum] 心跳已发送")
        await self._send(send)

    async def send_tool_response(self, responses: list) -> None:
        """回传 function_response —— 必须闭合，否则 ER2 会话会卡死"""
        if not responses:
            return

        async def send(session) -> None:
            await session.send_tool_response(function_responses=responses)
        await self._send(send)

    # ── 接收 ───────────────────────────────────────────────

    async def receive_loop(self) -> None:
        """持续接收服务端消息；包括 1000 OK 正常结束在内均重连。"""
        while not self._stopping:
            if not self._connected or self._session is None:
                await self._ensure_reconnect(self._generation)
                continue

            session = self._session
            generation = self._generation
            try:
                async for chunk in session.receive():
                    await self._handle(chunk)
                # SDK 的 receive() 在每个模型回合结束后正常返回；这不是
                # WebSocket 断开，继续下一次 receive 即可。真正断连会抛异常。
                continue
            except asyncio.CancelledError:
                raise
            except _ReconnectRequested:
                pass
            except Exception as exc:
                if session is self._session:
                    logger.warning("[Cerebellum] 接收中断，准备重连：%s", exc)
                    if self._invalidates_resumption(exc):
                        self._resumption_token = None
            finally:
                self._generating = False

            if not self._stopping and session is self._session:
                await self._discard_session()
            if not self._stopping:
                await self._ensure_reconnect(generation)

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
            raise _ReconnectRequested

    async def _reconnect(self, generation: Optional[int] = None) -> None:
        """串行重连；失败时退避重试，成功后仅保留一个新 session。"""
        if generation is None:
            generation = self._generation
        async with self._reconnect_lock:
            if self._stopping:
                return
            if self._connected and self._generation != generation:
                return

            await self._discard_session()
            delay = 1.0
            while not self._stopping:
                try:
                    await asyncio.sleep(delay)
                    await self.connect()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # 指数退避同时限制持续故障时的日志频率。
                    logger.error(
                        "[Cerebellum] 重连失败，%.0f 秒后重试：%s", delay, exc
                    )
                    self._resumption_token = None
                    delay = min(delay * 2.0, 60.0)
