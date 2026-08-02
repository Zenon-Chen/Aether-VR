"""心跳生成器 — 纯函数式世界状态摘要

HeartbeatGenerator 不再持有可变状态（原 TaskState 由 Agent 侧统一管理），
generate(state) 接收 AgentState 快照，输出心跳文本或 None（无变化时）。

状态哈希去重：无变化时不发送。
"""

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentState:
    """世界状态快照 — 心跳生成的唯一输入（不可变）

    由 Agent 组合根从各模块状态投影而来（如 dispatcher.state），
    HeartbeatGenerator 只读不写，保证单一事实来源。
    """
    current_task: str = "idle"
    recent_actions: tuple = ()          # 最近动作名列表
    known_objects: dict = field(default_factory=dict)
    environment_summary: str = "Unknown environment"
    emotion_state: str = "neutral"


class HeartbeatGenerator:
    """心跳生成器

    定期生成世界状态摘要，通过 send_realtime_input 注入 ER2。
    纯函数式：generate(state) 无副作用，仅做文本投影与哈希去重。
    """

    def __init__(self, interval_seconds: int = 30):
        self.interval = interval_seconds
        self._last_heartbeat_time = 0
        self._last_state_hash = ""

    def should_send(self) -> bool:
        """检查是否应该发送心跳"""
        return time.time() - self._last_heartbeat_time >= self.interval

    def generate(self, state: AgentState) -> Optional[str]:
        """从状态快照生成心跳消息，如果状态无变化返回 None

        Args:
            state: 当前世界状态（由 Agent 投影而来）

        Returns:
            心跳文本；状态与上次相同时返回 None
        """
        recent = list(state.recent_actions)

        heartbeat = (
            f"[WORLD STATE UPDATE]\n"
            f"Task: {state.current_task}\n"
            f"Emotion: {state.emotion_state}\n"
            f"Recent Actions: {', '.join(recent[-3:]) if recent else 'none'}\n"
            f"Known Objects: {', '.join(state.known_objects.keys()) if state.known_objects else 'none'}\n"
            f"[END UPDATE]"
        )

        # 状态哈希去重
        state_hash = hashlib.md5(heartbeat.encode()).hexdigest()[:8]
        if state_hash == self._last_state_hash:
            return None   # 状态未变化，跳过

        self._last_state_hash = state_hash
        self._last_heartbeat_time = time.time()
        return heartbeat
