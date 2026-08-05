"""世界事实契约 — StateStore 的快照结构

核心原则（架构评审结论）：
    世界状态不是「生成」出来的，是「登记」出来的。
    没有任何模型参与这一层；大脑与 ER2 都只是它的读者。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .plan import Channel


@dataclass(frozen=True, slots=True)
class ChannelState:
    """单个身体通道的占用情况"""
    busy: bool = False
    holder: Optional[str] = None       # 形如 "p31.s2"
    until: Optional[float] = None      # 预计释放的 monotonic 时刻

    def remaining(self, now: float) -> float:
        if not self.busy or self.until is None:
            return 0.0
        return max(0.0, self.until - now)


@dataclass(frozen=True, slots=True)
class ActivePlan:
    plan_id: str
    step: int
    total: int
    status: str = "running"


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    """各外部依赖的健康度 —— 由心跳探测与调用失败共同登记"""
    dart: str = "unknown"
    tts: str = "unknown"
    er2: str = "unknown"
    brain: str = "unknown"
    posture: str = "unknown"
    vrchat_osc: str = "unknown"
    vision: str = "unknown"

    def degraded(self) -> tuple[str, ...]:
        return tuple(
            name for name, value in (
                ("dart", self.dart), ("tts", self.tts), ("er2", self.er2),
                ("brain", self.brain), ("posture", self.posture),
                ("vrchat_osc", self.vrchat_osc), ("vision", self.vision),
            ) if value not in ("ok", "unknown")
        )


@dataclass(frozen=True, slots=True)
class WorldFacts:
    """权威事实快照 —— 不可变，由 StateStore 原子替换"""
    version: int = 0
    captured_at: float = field(default_factory=time.monotonic)
    channels: dict[Channel, ChannelState] = field(default_factory=dict)
    active_plan: Optional[ActivePlan] = None
    recent_results: tuple[str, ...] = ()      # 最近 3 条计划结果摘要
    services: ServiceHealth = field(default_factory=ServiceHealth)
    last_speech: str = ""                     # 角色最近说出口的话
    reflex_log: tuple[str, ...] = ()          # 预留：反射模块动作记录
    emergency_stopped: bool = False
    vision_status: str = "idle"

    def channel(self, ch: Channel) -> ChannelState:
        return self.channels.get(ch, ChannelState())

    def busy_channels(self) -> tuple[Channel, ...]:
        return tuple(ch for ch, st in self.channels.items() if st.busy)
