"""locomotion-service — 移动通路

关键点：
    DART 生成「走路动作」只解决骨架看起来在走，**不会**让玩家在
    VRChat 世界中真正位移。位移必须靠 OSC 输入轴。

硬保险：ER2 断连或超时无续期 → watchdog 强制归零，防止角色一直走。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

try:
    from pythonosc import udp_client
except ImportError:  # pragma: no cover
    udp_client = None  # type: ignore[assignment]

from ...infra.config import LocomotionConfig, OSCConfig

logger = logging.getLogger(__name__)

#: 方向 → (OSC 轴, 取值)
AXES: dict[str, tuple[str, float]] = {
    "forward":  ("Vertical", 1.0),
    "backward": ("Vertical", -1.0),
    "left":     ("Horizontal", -1.0),
    "right":    ("Horizontal", 1.0),
    "turn_left":  ("LookHorizontal", -1.0),
    "turn_right": ("LookHorizontal", 1.0),
}

_ALL_AXES = ("Vertical", "Horizontal", "LookHorizontal")


class LocomotionService:
    """OSC 移动轴控制"""

    name = "locomotion"

    def __init__(self, config: LocomotionConfig, osc: OSCConfig) -> None:
        self._config = config
        self._osc = osc
        self._client: Optional["udp_client.SimpleUDPClient"] = None
        self._watchdog: Optional[asyncio.Task] = None
        self._moving = False

    async def start(self) -> None:
        if udp_client is None:
            raise ImportError("需要 python-osc：pip install python-osc")
        self._client = udp_client.SimpleUDPClient(self._osc.host, self._osc.port)
        await self.halt()
        logger.info("[Locomotion] OSC → %s:%d", self._osc.host, self._osc.port)

    async def stop(self) -> None:
        await self.halt()
        self._client = None

    async def health(self) -> str:
        return "ok" if self._client is not None else "stopped"

    @property
    def moving(self) -> bool:
        return self._moving

    def known_directions(self) -> tuple[str, ...]:
        return tuple(AXES)

    # ── 移动 ───────────────────────────────────────────────

    async def move(self, direction: str, duration_s: float) -> None:
        """按方向移动指定时长，结束后自动归零"""
        axis_value = AXES.get(direction)
        if axis_value is None:
            raise KeyError(f"未知移动方向: {direction}")

        span = max(0.0, min(float(duration_s), self._config.max_duration_s))
        axis, value = axis_value

        self._cancel_watchdog()
        self._send(axis, value)
        self._moving = True
        self._watchdog = asyncio.create_task(self._watch(), name="locomotion_watchdog")
        logger.info("[Locomotion] %s %.1fs", direction, span)

        try:
            await asyncio.sleep(span)
        finally:
            await self.halt()

    async def halt(self) -> None:
        """所有轴归零 —— 幂等，紧急停止亦调用此方法"""
        self._cancel_watchdog()
        for axis in _ALL_AXES:
            self._send(axis, 0.0)
        if self._moving:
            logger.info("[Locomotion] 已归零")
        self._moving = False

    # ── 内部 ───────────────────────────────────────────────

    async def _watch(self) -> None:
        """硬保险：超过 watchdog_s 仍在移动则强制归零"""
        try:
            await asyncio.sleep(self._config.watchdog_s)
        except asyncio.CancelledError:
            return
        if self._moving:
            logger.error("[Locomotion] watchdog 触发，强制归零")
            for axis in _ALL_AXES:
                self._send(axis, 0.0)
            self._moving = False

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None and not self._watchdog.done():
            self._watchdog.cancel()
        self._watchdog = None

    def _send(self, axis: str, value: float) -> None:
        if self._client is None:
            logger.warning("[Locomotion] OSC 未连接，丢弃 %s=%s", axis, value)
            return
        self._client.send_message(f"{self._osc.input_prefix}/{axis}", float(value))
