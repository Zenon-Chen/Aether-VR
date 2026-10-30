"""avatar-service — 预录动作与表情

架构前提（重要）：
    VRChat OSC Avatar Parameters 只接受 int / float / bool，
    **不能**输入骨骼旋转。骨骼级控制必须走 posture-service
    的 SteamVR 虚拟设备路径。

因此本服务只负责触发 Animator 里预先配置好的动作与表情。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

try:
    from pythonosc import udp_client
except ImportError:  # pragma: no cover
    udp_client = None  # type: ignore[assignment]

from ...infra.config import AvatarConfig, OSCConfig

logger = logging.getLogger(__name__)

DEFAULT_ACTION_DURATION = 1.5


class AvatarService:
    """预录动作 / 表情 OSC 触发器"""

    name = "avatar"

    def __init__(self, config: AvatarConfig, osc: OSCConfig) -> None:
        self._config = config
        self._osc = osc
        self._client: Optional["udp_client.SimpleUDPClient"] = None
        self._current_action: Optional[str] = None

    async def start(self) -> None:
        if udp_client is None:
            raise ImportError("需要 python-osc：pip install python-osc")
        self._client = udp_client.SimpleUDPClient(self._osc.host, self._osc.port)
        logger.info("[Avatar] OSC → %s:%d", self._osc.host, self._osc.port)
        await self.reset()

    async def stop(self) -> None:
        try:
            await self.reset()
        except Exception:
            pass
        self._client = None

    async def health(self) -> str:
        return "ok" if self._client is not None else "stopped"

    # ── 动作 ───────────────────────────────────────────────

    def known_actions(self) -> tuple[str, ...]:
        return tuple(k for k in self._config.actions if k != "idle")

    def known_expressions(self) -> tuple[str, ...]:
        return tuple(self._config.expressions)

    def is_full_body(self, name: str) -> bool:
        return name in self._config.full_body_actions

    async def play_action(self, name: str) -> float:
        """触发预录动作并等待其时长。返回实际占用秒数。"""
        action_id = self._config.actions.get(name)
        if action_id is None:
            raise KeyError(f"未配置的预录动作: {name}")

        duration = self._config.action_durations.get(name, DEFAULT_ACTION_DURATION)
        self._send(self._config.action_parameter, action_id)
        self._current_action = name
        logger.info("[Avatar] 播放 %s (ActionID=%d, %.1fs)", name, action_id, duration)

        try:
            await asyncio.sleep(duration)
        finally:
            # 回到 idle，避免 Animator 停在动作状态
            self._send(self._config.action_parameter, self._config.actions.get("idle", 0))
            self._current_action = None
        return duration

    # ── 表情 ───────────────────────────────────────────────

    async def set_expression(self, name: str) -> None:
        expression_id = self._config.expressions.get(name)
        if expression_id is None:
            logger.warning("[Avatar] 未配置的表情 %s，回退 neutral", name)
            expression_id = self._config.expressions.get("neutral", 0)
        self._send(self._config.expression_parameter, expression_id)
        logger.info("[Avatar] 表情 %s (%d)", name, expression_id)

    async def reset(self) -> None:
        """回到 idle 动作与 neutral 表情"""
        self._send(self._config.action_parameter, self._config.actions.get("idle", 0))
        self._send(self._config.expression_parameter, self._config.expressions.get("neutral", 0))
        self._current_action = None

    # ── 内部 ───────────────────────────────────────────────

    def _send(self, parameter: str, value: int) -> None:
        if self._client is None:
            logger.warning("[Avatar] OSC 未连接，丢弃 %s=%s", parameter, value)
            return
        self._client.send_message(f"{self._osc.parameter_prefix}/{parameter}", int(value))
