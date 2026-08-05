"""DART 专属轴角转换；不得依赖姿态播放或设备输出层。"""
from __future__ import annotations

import math
from collections.abc import Sequence

from .....contracts.motion import Quaternion


def axis_angle_to_quaternion(value: Sequence[float]) -> Quaternion:
    angle = math.sqrt(sum(float(component) ** 2 for component in value))
    if angle < 1e-8:
        return (1.0, 0.0, 0.0, 0.0)
    scale = math.sin(angle / 2.0) / angle
    return (
        math.cos(angle / 2.0),
        float(value[0]) * scale,
        float(value[1]) * scale,
        float(value[2]) * scale,
    )
