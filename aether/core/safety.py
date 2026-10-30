"""紧急停止 — 最高优先级，纯本地，旁路一切

触发：本地关键词（停下/别动）、移动超时、所有云端断连、
      虚拟桥输出异常、手动快捷键。

动作（全部在 100ms 内完成）：
    OSC 移动轴归零 → DART 停止 → 虚拟追踪器回到站立姿势
    → 取消计划 → 释放全部锁 → TTS 停止
    → 状态注入 ER2 和大脑：「已紧急停止，原因 X」
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

#: 本地关键词，命中即触发，不经过任何模型
STOP_KEYWORDS = ("停下", "别动", "停止", "stop", "freeze")
_CJK_STOP_KEYWORDS = tuple(kw for kw in STOP_KEYWORDS if not kw.isascii())
_ASCII_STOP_PATTERN = re.compile(
    r"(?<![a-z0-9_])(?:"
    + "|".join(re.escape(kw) for kw in STOP_KEYWORDS if kw.isascii())
    + r")(?![a-z0-9_])",
    re.IGNORECASE,
)

#: 每个停止动作的独立超时，避免某一环卡住整体
_ACTION_TIMEOUT_S = 0.5

StopAction = Callable[[], Awaitable[None]]


class EmergencyStop:
    """按注册顺序并发触发所有停止动作"""

    def __init__(self, state=None) -> None:
        self._actions: list[tuple[str, StopAction]] = []
        self._state = state
        self._lock = asyncio.Lock()
        self._last_trigger = 0.0
        self._last_reason = ""

    def register(self, name: str, action: StopAction) -> None:
        self._actions.append((name, action))
        logger.debug("[Stop] 注册停止动作 %s", name)

    @staticmethod
    def matches_keyword(text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        if any(kw in raw for kw in _CJK_STOP_KEYWORDS):
            return True
        return bool(_ASCII_STOP_PATTERN.search(raw))

    @property
    def last_reason(self) -> str:
        return self._last_reason

    async def trigger(self, reason: str) -> float:
        """执行紧急停止，返回耗时（秒）"""
        async with self._lock:
            started = time.monotonic()
            self._last_trigger = started
            self._last_reason = reason
            logger.warning("[Stop] 紧急停止触发：%s", reason)

            results = await asyncio.gather(
                *(self._run(name, action) for name, action in self._actions),
                return_exceptions=True,
            )
            failures = [r for r in results if isinstance(r, BaseException)]

            if self._state is not None:
                try:
                    await self._state.set_emergency(True)
                    await self._state.release_all_channels()
                except Exception:
                    logger.exception("[Stop] 状态登记失败")

            elapsed = time.monotonic() - started
            logger.warning(
                "[Stop] 完成，耗时 %.0fms，失败 %d 项", elapsed * 1000, len(failures)
            )
            return elapsed

    async def clear(self) -> None:
        """解除紧急停止状态，允许恢复正常调度"""
        if self._state is not None:
            await self._state.set_emergency(False)
        self._last_reason = ""
        logger.info("[Stop] 紧急状态已解除")

    async def _run(self, name: str, action: StopAction) -> None:
        try:
            await asyncio.wait_for(action(), timeout=_ACTION_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.error("[Stop] %s 超时", name)
            raise
        except Exception:
            logger.exception("[Stop] %s 失败", name)
            raise
