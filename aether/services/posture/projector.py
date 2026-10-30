"""Project canonical humanoid motion into the private six-tracker contract."""
from __future__ import annotations

import math
from collections.abc import Sequence

from ...contracts.frame import DevicePose, PostureFrame
from ...contracts.motion import CanonicalMotion, Quaternion, Vec3

# humanoid-22 canonical parent order and approximate z-up rest offsets.
_PARENTS = (-1, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9, 9, 12, 13, 14, 16, 17, 18, 19)
_REST_Z_UP: tuple[Vec3, ...] = (
    (0, 0, 0), (0.09, 0, -0.08), (-0.09, 0, -0.08), (0, 0, 0.12),
    (0, -0.01, -0.42), (0, -0.01, -0.42), (0, 0, 0.18), (0, 0, -0.43),
    (0, 0, -0.43), (0, 0, 0.16), (0, 0.11, -0.05), (0, 0.11, -0.05),
    (0, 0, 0.14), (0.08, 0, 0.08), (-0.08, 0, 0.08), (0, 0, 0.18),
    (0.18, 0, 0), (-0.18, 0, 0), (0.25, 0, 0), (-0.25, 0, 0),
    (0.23, 0, 0), (-0.23, 0, 0),
)
# head, left hand, right hand, pelvis, left foot, right foot.
_TRACKED_JOINTS = (15, 20, 21, 0, 10, 11)
_SQRT_HALF = math.sqrt(0.5)


def _qmul(a: Quaternion, b: Quaternion) -> Quaternion:
    w, x, y, z = a
    W, X, Y, Z = b
    return (
        w * W - x * X - y * Y - z * Z,
        w * X + x * W + y * Z - z * Y,
        w * Y - x * Z + y * W + z * X,
        w * Z + x * Y - y * X + z * W,
    )


def _qrot(q: Quaternion, v: Vec3) -> Vec3:
    result = _qmul(_qmul(q, (0.0, *v)), (q[0], -q[1], -q[2], -q[3]))
    return (result[1], result[2], result[3])


def _z_to_y_position(value: Sequence[float]) -> Vec3:
    return (float(value[0]), float(value[2]), -float(value[1]))


def _z_to_y_rotation(value: Quaternion) -> Quaternion:
    return _qmul(
        _qmul((_SQRT_HALF, -_SQRT_HALF, 0.0, 0.0), value),
        (_SQRT_HALF, _SQRT_HALF, 0.0, 0.0),
    )


class TrackerProjector:
    """The only canonical-motion boundary that knows tracker slot ordering."""

    def project(self, motion: CanonicalMotion) -> list[PostureFrame]:
        if motion.skeleton_id != "humanoid-22":
            raise ValueError(f"unsupported skeleton: {motion.skeleton_id}")
        if motion.units != "meters" or motion.up_axis not in {"z", "y"}:
            raise ValueError("canonical motion must use meters and y/z up axis")
        if len(motion.joint_rotations) != motion.frame_count:
            raise ValueError("frame_count does not match joint rotations")
        if len(motion.root_translations) != motion.frame_count:
            raise ValueError("frame_count does not match root translations")
        if motion.joint_positions and len(motion.joint_positions) != motion.frame_count:
            raise ValueError("frame_count does not match joint positions")

        frames: list[PostureFrame] = []
        floor_offset: float | None = None
        for frame_index, local in enumerate(motion.joint_rotations):
            if len(local) != 22:
                raise ValueError("humanoid-22 requires 22 joint rotations per frame")
            world_rotations: list[Quaternion] = []
            derived_positions: list[Vec3] = []
            root = motion.root_translations[frame_index]
            offsets = _REST_Z_UP if motion.up_axis == "z" else tuple(
                _z_to_y_position(value) for value in _REST_Z_UP
            )
            for joint_index, rotation in enumerate(local):
                parent = _PARENTS[joint_index]
                world = rotation if parent < 0 else _qmul(world_rotations[parent], rotation)
                world_rotations.append(world)
                if parent < 0:
                    derived_positions.append(root)
                else:
                    offset = _qrot(world_rotations[parent], offsets[joint_index])
                    base = derived_positions[parent]
                    derived_positions.append(tuple(base[i] + offset[i] for i in range(3)))

            positions = (
                motion.joint_positions[frame_index]
                if motion.joint_positions else tuple(derived_positions)
            )
            if len(positions) < 22:
                raise ValueError(
                    f"frame {frame_index} joint_positions requires at least 22 joints, got {len(positions)}"
                )
            output_positions = (
                tuple(_z_to_y_position(value) for value in positions)
                if motion.up_axis == "z" else positions
            )
            output_rotations = (
                tuple(_z_to_y_rotation(value) for value in world_rotations)
                if motion.up_axis == "z" else tuple(world_rotations)
            )
            if floor_offset is None:
                floor_offset = -min(output_positions[10][1], output_positions[11][1])

            devices = tuple(
                DevicePose(
                    output_positions[index][0],
                    output_positions[index][1] + floor_offset,
                    output_positions[index][2],
                    *output_rotations[index],
                )
                for index in _TRACKED_JOINTS
            )
            frames.append(PostureFrame(devices))
        return frames
