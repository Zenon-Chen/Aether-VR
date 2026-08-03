"""核心层 — 与具体服务实现无关的编排机制

    state_store     权威事实库（谁执行谁登记）
    facts_renderer  事实 → 自然语言
    scheduler       序号队列 + 通道锁
    heartbeat       心跳拼装与发送闸门
    safety          紧急停止
"""

from .state_store import StateStore
from .facts_renderer import render_facts
from .scheduler import Scheduler, SemanticError, MechanicalError
from .heartbeat import HeartbeatAssembler, HeartbeatDecision, Gate
from .safety import EmergencyStop, STOP_KEYWORDS

__all__ = [
    "StateStore", "render_facts",
    "Scheduler", "SemanticError", "MechanicalError",
    "HeartbeatAssembler", "HeartbeatDecision", "Gate",
    "EmergencyStop", "STOP_KEYWORDS",
]
