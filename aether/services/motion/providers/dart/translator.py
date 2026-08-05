"""DART 专有响应到公共动作契约的转换。"""

from __future__ import annotations

from typing import Any

from .....contracts.motion import (
    CanonicalMotion, MotionErrorCode, MotionProviderError, Quaternion, Vec3,
)
from .math3d import axis_angle_to_quaternion


class DartTranslator:
    def translate(
        self, payload: Any, *, request_id: str, provider_id: str = "dart"
    ) -> CanonicalMotion:
        if not isinstance(payload, dict):
            raise MotionProviderError(MotionErrorCode.INVALID_RESPONSE, "动作响应不是对象")
        poses = _normalize_frames(payload.get("poses") or payload.get("pose"))
        if not poses:
            raise MotionProviderError(MotionErrorCode.INVALID_RESPONSE, "动作响应缺少姿态")
        translations = _normalize_frames(payload.get("trans")) or [(0.0, 0.0, 0.0)]
        try:
            rotations = tuple(
                tuple(axis_angle_to_quaternion(row) for row in _joint_rows(pose))
                for pose in poses
            )
            roots: tuple[Vec3, ...] = tuple(
                tuple(float(v) for v in translations[min(i, len(translations) - 1)][:3])
                for i in range(len(poses))
            )  # type: ignore[assignment]
            fps = float(payload.get("framerate") or payload.get("fps") or 30.0)
        except (TypeError, ValueError, IndexError) as exc:
            raise MotionProviderError(
                MotionErrorCode.INVALID_RESPONSE, f"动作响应格式无效: {exc}"
            ) from exc
        if any(len(frame) != 22 for frame in rotations):
            raise MotionProviderError(MotionErrorCode.INVALID_RESPONSE, "动作姿态需要 22 个关节")
        return CanonicalMotion(
            schema_version="1.0",
            request_id=request_id,
            provider_id=provider_id,
            fps=fps,
            frame_count=len(rotations),
            skeleton_id="humanoid-22",
            up_axis="z",
            units="meters",
            root_translations=roots,
            joint_rotations=rotations,
        )


def _joint_rows(pose: Any) -> list[Any]:
    if len(pose) >= 66 and not isinstance(pose[0], (list, tuple)):
        return [pose[i:i + 3] for i in range(0, 66, 3)]
    return list(pose[:22])


def _normalize_frames(value: Any) -> list[Any] | None:
    if value is None or not isinstance(value, (list, tuple)) or not value:
        return None
    first = value[0]
    if not isinstance(first, (list, tuple)):
        return [value] if len(value) in (3, 66) else None
    if len(value) == 22 and len(first) == 3 and not isinstance(first[0], (list, tuple)):
        return [value]
    return list(value)
