"""StateStore — 权威事实库

唯一原则：**谁执行谁登记**。没有任何模型参与这一层的写入。
所有 mutate 都在锁内完成并原子发布一个新的不可变 WorldFacts 快照，
读者（大脑输入、心跳拼装器、CLI）永远看到一致的版本。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import replace
from typing import Awaitable, Callable, Optional

from ..contracts.facts import ActivePlan, ChannelState, ServiceHealth, WorldFacts
from ..contracts.plan import Channel

logger = logging.getLogger(__name__)

Listener = Callable[[WorldFacts], Awaitable[None]]

_MAX_RECENT_RESULTS = 3
_MAX_REFLEX_LOG = 5


class StateStore:
    """内存单例事实库"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._version = 0
        self._channels: dict[Channel, ChannelState] = {
            ch: ChannelState() for ch in Channel
        }
        self._active_plan: Optional[ActivePlan] = None
        self._recent_results: deque[str] = deque(maxlen=_MAX_RECENT_RESULTS)
        self._services = ServiceHealth()
        self._last_speech = ""
        self._reflex_log: deque[str] = deque(maxlen=_MAX_REFLEX_LOG)
        self._emergency = False
        self._listeners: list[Listener] = []
        self._snapshot = self._build()

    # ── 读取 ───────────────────────────────────────────────

    @property
    def facts(self) -> WorldFacts:
        """当前快照 —— 无锁读取，因快照不可变"""
        return self._snapshot

    def subscribe(self, listener: Listener) -> None:
        """注册事实变更监听器（心跳唤醒、日志等）"""
        self._listeners.append(listener)

    # ── 通道占用 ───────────────────────────────────────────

    async def acquire_channels(
        self, channels: frozenset[Channel], holder: str, duration_s: float
    ) -> None:
        """标记通道被占用。调度器已在外层保证互斥，这里只做登记。"""
        if not channels:
            return
        until = time.monotonic() + max(0.0, duration_s)
        async with self._lock:
            for ch in channels:
                self._channels[ch] = ChannelState(True, holder, until)
            await self._publish()

    async def release_channels(self, channels: frozenset[Channel]) -> None:
        if not channels:
            return
        async with self._lock:
            for ch in channels:
                self._channels[ch] = ChannelState()
            await self._publish()

    async def release_all_channels(self) -> None:
        async with self._lock:
            for ch in Channel:
                self._channels[ch] = ChannelState()
            await self._publish()

    # ── 计划进度 ───────────────────────────────────────────

    async def set_active_plan(self, plan: Optional[ActivePlan]) -> None:
        async with self._lock:
            self._active_plan = plan
            await self._publish()

    async def advance_plan(self, step: int) -> None:
        async with self._lock:
            if self._active_plan is not None:
                self._active_plan = replace(self._active_plan, step=step)
                await self._publish()

    async def record_result(self, summary: str) -> None:
        async with self._lock:
            self._recent_results.append(summary)
            self._active_plan = None
            await self._publish()

    # ── 其他事实 ───────────────────────────────────────────

    async def set_last_speech(self, text: str) -> None:
        async with self._lock:
            self._last_speech = text
            await self._publish()

    async def set_service(self, name: str, status: str) -> None:
        async with self._lock:
            if not hasattr(self._services, name):
                logger.warning("[State] 未知服务名 %s，忽略", name)
                return
            self._services = replace(self._services, **{name: status})
            await self._publish()

    async def append_reflex(self, entry: str) -> None:
        """预留接口：反射模块第一版被砍，字段保留以便将来直接插入"""
        async with self._lock:
            self._reflex_log.append(entry)
            await self._publish()

    async def set_emergency(self, stopped: bool) -> None:
        async with self._lock:
            self._emergency = stopped
            await self._publish()

    # ── 内部 ───────────────────────────────────────────────

    def _build(self) -> WorldFacts:
        return WorldFacts(
            version=self._version,
            captured_at=time.monotonic(),
            channels=dict(self._channels),
            active_plan=self._active_plan,
            recent_results=tuple(self._recent_results),
            services=self._services,
            last_speech=self._last_speech,
            reflex_log=tuple(self._reflex_log),
            emergency_stopped=self._emergency,
        )

    async def _publish(self) -> None:
        """在锁内构建新快照并通知监听器（监听器异常不影响写入）"""
        self._version += 1
        self._snapshot = self._build()
        for listener in self._listeners:
            try:
                await listener(self._snapshot)
            except Exception:
                logger.exception("[State] 监听器执行失败")
