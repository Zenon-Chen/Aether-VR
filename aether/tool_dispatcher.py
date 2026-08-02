"""函数调用分发器

监听 ER2 的 tool_call 事件，按函数名路由到对应处理器：
- execute_tts → TTS 引擎
- execute_motion → DART 动作生成
- report_status → 状态记录
- recall_memory → RAG 召回（Sub-agent Runtime）

处理打断取消（ToolCallCancellation），回传执行结果。
维护本地动作状态机。
"""

import asyncio
import logging
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass, field
from enum import Enum

from google.genai import types

from .constants import (
    EMOTIONS,
    SPEECH_RATES,
    TEXT_MAX_LENGTH,
    MOTION_DURATION_MIN,
    MOTION_DURATION_MAX,
)
from .protocols import DispatcherPort

logger = logging.getLogger(__name__)

# ── 合法枚举值（单一来源：aether/constants.py）────
VALID_EMOTIONS = frozenset(EMOTIONS)
VALID_SPEECH_RATES = frozenset(SPEECH_RATES)


class MotionState(Enum):
    IDLE = "idle"
    GENERATING = "generating"   # DART 正在生成
    PLAYING = "playing"         # 动作正在播放
    CANCELLED = "cancelled"     # 被打断取消


@dataclass
class DispatcherState:
    """分发器状态"""
    motion_state: MotionState = MotionState.IDLE
    current_motion_task: Optional[asyncio.Task] = None
    current_tts_task: Optional[asyncio.Task] = None
    recent_actions: list = field(default_factory=lambda: [])  # 最近的动作记录
    emotion_state: str = "neutral"  # 当前情绪状态
    # 所有活跃的后台工具调用任务，key = function_call.id
    active_tasks: dict = field(default_factory=dict)


class ToolDispatcher(DispatcherPort):
    """ER2 函数调用分发器

    通过 set_xxx_handler() 注入外部处理器，
    handle_tool_call() 签名匹配 ER2Client.receive_loop() 的调用方式，
    返回 list[types.FunctionResponse] 供 send_tool_response() 回传。
    """

    def __init__(self):
        self.state = DispatcherState()
        # 外部注入的处理器（在 main.py 中注入）
        self._tts_handler: Optional[Callable] = None       # async (text, emotion, rate) -> str
        self._motion_handler: Optional[Callable] = None    # async (action, duration) -> dict
        self._memory_handler: Optional[Callable] = None    # async (query, max_tokens) -> str
        self._status_handler: Optional[Callable] = None    # async (status_type, detail) -> None
        # 后台任务完成后的结果回调：async (list[FunctionResponse]) -> None
        self._response_callback: Optional[Callable] = None

    # ── 处理器注入 ─────────────────────────────

    def set_tts_handler(self, handler: Callable[[str, str, str], Awaitable]):
        """注入 TTS 处理器: async (text, emotion, speech_rate) -> status_str"""
        self._tts_handler = handler

    def set_motion_handler(self, handler: Callable[[str, float], Awaitable]):
        """注入动作处理器: async (action, duration) -> result_dict"""
        self._motion_handler = handler

    def set_memory_handler(self, handler: Callable[[str, int], Awaitable]):
        """注入记忆召回处理器: async (query, max_tokens) -> recalled_text"""
        self._memory_handler = handler

    def set_status_handler(self, handler: Callable[[str, str], Awaitable]):
        """注入状态处理器: async (status_type, detail) -> None"""
        self._status_handler = handler

    def set_response_callback(self, callback: Callable[[list], Awaitable]):
        """注入结果回调: async (list[FunctionResponse]) -> None

        后台任务完成后通过此回调将结果异步回传给 ER2，
        使 receive_loop 不被阻塞。
        """
        self._response_callback = callback

    # ── 主入口 ─────────────────────────────────

    async def handle_tool_call(self, tool_call) -> list:
        """处理 ER2 的 tool_call 事件（非阻塞）。

        每个 function call 启动后台任务，立即返回空列表，
        不阻塞 receive_loop。后台任务完成后通过
        _response_callback 异步回传 FunctionResponse。
        """
        for fc in tool_call.function_calls:
            task = asyncio.create_task(
                self._run_and_respond(fc),
                name=f"tool:{fc.name}:{fc.id}",
            )
            self.state.active_tasks[fc.id] = task
            logger.info(
                "[Dispatcher] Submitted %s (id=%s) as background task",
                fc.name, fc.id,
            )

        # 返回空列表 — receive_loop 不再同步等待结果
        return []

    async def _run_and_respond(self, fc):
        """后台执行单个 function call 并通过回调回传结果"""
        fc_id = fc.id
        try:
            result = await self._dispatch_single(fc)
            response = types.FunctionResponse(
                id=fc_id,
                name=fc.name,
                response=result,
            )
        except asyncio.CancelledError:
            logger.warning("[Dispatcher] Tool call %s cancelled", fc.name)
            response = types.FunctionResponse(
                id=fc_id,
                name=fc.name,
                response={"status": "cancelled", "reason": "interrupted by user"},
            )
        except Exception as e:
            logger.error("[Dispatcher] Tool call %s failed: %s", fc.name, e)
            response = types.FunctionResponse(
                id=fc_id,
                name=fc.name,
                response={"status": "error", "error": str(e)},
            )
        finally:
            # 从活跃任务中移除
            self.state.active_tasks.pop(fc_id, None)

        # 通过回调异步回传结果
        if self._response_callback:
            try:
                await self._response_callback([response])
            except Exception as exc:
                logger.error(
                    "[Dispatcher] Response callback failed for %s: %s",
                    fc.name, exc,
                )
        else:
            logger.warning(
                "[Dispatcher] No response callback set, result for %s dropped",
                fc.name,
            )

    # ── 路由分发 ───────────────────────────────

    async def _dispatch_single(self, fc) -> dict:
        """分发单个函数调用"""
        name = fc.name
        args = fc.args or {}

        logger.info("[Dispatcher] %s(%s)", name, args)

        if name == "execute_tts":
            return await self._handle_tts(args)
        elif name == "execute_motion":
            return await self._handle_motion(args)
        elif name == "report_status":
            return await self._handle_status(args)
        elif name == "recall_memory":
            return await self._handle_memory(args)
        elif name == "cancel":
            return await self._handle_cancel(args)
        else:
            logger.warning("[Dispatcher] Unknown function: %s", name)
            return {"status": "error", "error": f"Unknown function: {name}"}

    # ── 各处理器 ───────────────────────────────

    async def _handle_tts(self, args: dict) -> dict:
        """处理 TTS 调用（含参数校验）"""
        text = args.get("text", "")
        emotion = args.get("emotion", "neutral")
        speech_rate = args.get("speech_rate", "normal")

        # ── 参数校验 ──
        if not text or not text.strip():
            logger.warning("[Dispatcher] TTS text is empty, skipping")
            return {"status": "error", "error": "text is empty"}
        if len(text) > TEXT_MAX_LENGTH:
            logger.warning("[Dispatcher] TTS text too long (%d chars), truncating", len(text))
            text = text[:TEXT_MAX_LENGTH]
        if emotion not in VALID_EMOTIONS:
            logger.warning("[Dispatcher] Invalid emotion '%s', falling back to neutral", emotion)
            emotion = "neutral"
        if speech_rate not in VALID_SPEECH_RATES:
            logger.warning("[Dispatcher] Invalid speech_rate '%s', falling back to normal", speech_rate)
            speech_rate = "normal"

        self.state.emotion_state = emotion

        if self._tts_handler:
            task = asyncio.create_task(self._tts_handler(text, emotion, speech_rate))
            self.state.current_tts_task = task
            await task
            return {"status": "played", "text": text, "emotion": emotion}
        else:
            logger.warning("[Dispatcher] No TTS handler registered")
            return {"status": "no_handler"}

    async def _handle_motion(self, args: dict) -> dict:
        """处理动作调用（含参数校验）"""
        action = args.get("action", "")
        duration = args.get("duration", 2.0)

        # ── 参数校验 ──
        if not action or not action.strip():
            logger.warning("[Dispatcher] Motion action is empty, skipping")
            return {"status": "error", "error": "action is empty"}
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            logger.warning("[Dispatcher] Invalid duration '%s', falling back to 2.0", duration)
            duration = 2.0
        if duration < MOTION_DURATION_MIN:
            logger.warning("[Dispatcher] Duration %.1f below min, clamping to %.1f", duration, MOTION_DURATION_MIN)
            duration = MOTION_DURATION_MIN
        if duration > MOTION_DURATION_MAX:
            logger.warning("[Dispatcher] Duration %.1f above max, clamping to %.1f", duration, MOTION_DURATION_MAX)
            duration = MOTION_DURATION_MAX

        self.state.motion_state = MotionState.GENERATING

        if self._motion_handler:
            task = asyncio.create_task(self._motion_handler(action, duration))
            self.state.current_motion_task = task
            try:
                result = await task
                self.state.motion_state = MotionState.PLAYING
                # 记录动作
                self.state.recent_actions.append({
                    "action": action,
                    "duration": duration,
                    "timestamp": asyncio.get_event_loop().time(),
                })
                # 只保留最近 10 个动作
                if len(self.state.recent_actions) > 10:
                    self.state.recent_actions = self.state.recent_actions[-10:]
                return {
                    "status": "motion_applied",
                    "action": action,
                    "frames": result.get("num_frames", 0),
                }
            except Exception:
                self.state.motion_state = MotionState.IDLE
                raise
        else:
            logger.warning("[Dispatcher] No motion handler registered")
            return {"status": "no_handler"}

    async def _handle_status(self, args: dict) -> dict:
        """处理状态回报"""
        status_type = args.get("status_type", "")
        detail = args.get("detail", "")

        logger.info("[Dispatcher] Status: %s - %s", status_type, detail)

        if status_type == "motion_complete":
            self.state.motion_state = MotionState.IDLE

        if self._status_handler:
            await self._status_handler(status_type, detail)

        return {"status": "acknowledged"}

    async def _handle_memory(self, args: dict) -> dict:
        """处理记忆召回"""
        query = args.get("query", "")
        max_tokens = args.get("max_tokens", 2000)

        if self._memory_handler:
            recalled = await self._memory_handler(query, max_tokens)
            return {"status": "recalled", "memory": recalled}
        else:
            logger.warning("[Dispatcher] No memory handler registered")
            return {"status": "no_handler", "memory": ""}

    async def _handle_cancel(self, args: dict) -> dict:
        """处理 ER2 端下发的取消指令"""
        cancelled_ids = args.get("ids", [])
        await self.cancel_pending(cancelled_ids)
        return {"status": "cancelled", "ids": cancelled_ids}

    # ── 打断取消 ───────────────────────────────

    async def cancel_pending(self, cancelled_ids: list):
        """处理 ToolCallCancellation — 取消待执行的调用

        通过 active_tasks 字典精确匹配被取消的 function call ID，
        对正在运行的后台任务调用 task.cancel()。
        """
        logger.info("[Dispatcher] Cancelling tool calls: %s", cancelled_ids)

        cancelled_count = 0
        for fc_id in cancelled_ids:
            task = self.state.active_tasks.get(fc_id)
            if task and not task.done():
                task.cancel()
                cancelled_count += 1
                logger.info("[Dispatcher] Cancelled task: %s", fc_id)

        # 兼容旧逻辑：如果没有指定 ID 或没找到匹配任务，取消所有活跃任务
        if cancelled_count == 0 and not cancelled_ids:
            for fc_id, task in list(self.state.active_tasks.items()):
                if not task.done():
                    task.cancel()
                    cancelled_count += 1

        # 同步更新状态
        if self.state.current_motion_task and not self.state.current_motion_task.done():
            self.state.current_motion_task.cancel()
            self.state.motion_state = MotionState.CANCELLED

        if self.state.current_tts_task and not self.state.current_tts_task.done():
            self.state.current_tts_task.cancel()

        logger.info("[Dispatcher] Cancelled %d tasks", cancelled_count)

    # ── 状态摘要 ───────────────────────────────

    def get_state_summary(self) -> str:
        """获取状态摘要（用于心跳）"""
        recent = [a["action"] for a in self.state.recent_actions[-3:]]
        return (
            f"Motion: {self.state.motion_state.value}, "
            f"Emotion: {self.state.emotion_state}, "
            f"Recent: {', '.join(recent) if recent else 'none'}"
        )
