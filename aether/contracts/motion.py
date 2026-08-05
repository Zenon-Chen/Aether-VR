"""供应商中立的动作生成契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


Vec3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]


class MotionErrorCode(str, Enum):
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class MotionRequest:
    request_id: str
    prompt: str
    duration_s: float
    scope: str = "full_body"


@dataclass(frozen=True, slots=True)
class CanonicalMotion:
    schema_version: str
    request_id: str
    provider_id: str
    fps: float
    frame_count: int
    skeleton_id: str
    up_axis: str
    units: str
    root_translations: tuple[Vec3, ...]
    joint_rotations: tuple[tuple[Quaternion, ...], ...]
    joint_positions: tuple[tuple[Vec3, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class MotionCapabilities:
    provider_id: str
    supported_scopes: tuple[str, ...] = ("upper_body", "full_body")
    skeleton_ids: tuple[str, ...] = ("humanoid-22",)
    max_duration_s: float | None = None


@dataclass(frozen=True, slots=True)
class MotionHealthReport:
    provider_id: str
    healthy: bool
    status: str
    details: dict[str, Any] = field(default_factory=dict)


class MotionProviderError(RuntimeError):
    """动作供应商边界上的稳定错误类型。"""

    def __init__(self, code: MotionErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable}


__all__ = [
    "Vec3", "Quaternion", "MotionRequest", "CanonicalMotion",
    "MotionCapabilities", "MotionHealthReport", "MotionErrorCode",
    "MotionProviderError",
]
