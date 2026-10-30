"""中立视觉跟踪契约。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class TargetObservation:
    target_id: str
    confidence: float
    x: float
    y: float
    width: float
    height: float
    frame_width: int
    frame_height: int

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2.0

    @property
    def area_ratio(self) -> float:
        return (self.width * self.height) / max(1, self.frame_width * self.frame_height)

    @property
    def height_ratio(self) -> float:
        return self.height / max(1, self.frame_height)


@dataclass(frozen=True, slots=True)
class VisionStatus:
    state: str = "idle"
    target_id: str = ""
    confidence: float = 0.0
    error: float = 0.0
    size_ratio: float = 0.0
    steps: int = 0
    reason: str = ""
    degraded: bool = False


__all__ = ["TargetObservation", "VisionStatus"]
