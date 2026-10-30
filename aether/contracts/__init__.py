"""Aether 数据契约层

所有跨服务边界传递的数据结构集中定义在此。任何 service 都只依赖
contracts，不直接依赖其他 service 的实现。这是模块可独立检修与替换
的前提。

分组：
    frame      —— 姿态帧与六设备位姿（posture 链路）
    plan       —— ER2 下发的动作计划与步骤
    facts      —— StateStore 权威事实快照
    observation—— 大脑输出的观察/建议
    perception —— capture 产出的音视频块
"""

from .frame import DeviceSlot, DevicePose, PostureFrame, SMPL_DEVICE_JOINTS
from .plan import Channel, ActionKind, PlanStep, Plan, StepResult, PlanResult
from .facts import (
    ChannelState, ActivePlan, ServiceHealth, WorldFacts, normalize_health_status,
)
from .observation import Observation, BrainOutput
from .perception import AudioChunk, VideoFrame
from .motion import (
    CanonicalMotion, MotionCapabilities, MotionErrorCode, MotionHealthReport,
    MotionProviderError, MotionRequest,
)
from .vision import TargetObservation, VisionStatus

__all__ = [
    # frame
    "DeviceSlot", "DevicePose", "PostureFrame", "SMPL_DEVICE_JOINTS",
    # plan
    "Channel", "ActionKind", "PlanStep", "Plan", "StepResult", "PlanResult",
    # facts
    "ChannelState", "ActivePlan", "ServiceHealth", "WorldFacts", "normalize_health_status",
    # observation
    "Observation", "BrainOutput",
    # perception
    "AudioChunk", "VideoFrame",
    # motion
    "MotionRequest", "CanonicalMotion", "MotionCapabilities", "MotionHealthReport",
    "MotionErrorCode", "MotionProviderError",
    # vision
    "TargetObservation", "VisionStatus",
]
