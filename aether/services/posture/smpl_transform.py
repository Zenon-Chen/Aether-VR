"""SMPL → 六设备位姿转换

从既有 dart-steamvr-bridge/bridge.py 提炼并固化。转换链：

    DART poses (轴角, z-up) → 局部四元数 → FK 得世界旋转
    → 取 6 个关节 → 坐标系变换 z-up → y-up → 落地偏移

坐标约定与 C++ 驱动一致，改动必须同步实测。
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

from ...contracts.frame import (
    SMPL_DEVICE_JOINTS, DevicePose, PostureFrame, WIRE_DEVICE_COUNT,
)

Quat = tuple[float, float, float, float]
Vec3 = tuple[float, float, float]

#: SMPL 22 关节的父索引
PARENTS: tuple[int, ...] = (
    -1, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9, 9, 12, 13, 14, 16, 17, 18, 19,
)

#: 近似 rest 骨长偏移，仅在 DART 未返回绝对 joints 时用于 FK
REST_OFFSETS: tuple[Vec3, ...] = (
    (0, 0, 0), (0.09, 0, -0.08), (-0.09, 0, -0.08), (0, 0, 0.12),
    (0, -0.01, -0.42), (0, -0.01, -0.42), (0, 0, 0.18), (0, 0, -0.43),
    (0, 0, -0.43), (0, 0, 0.16), (0, 0.11, -0.05), (0, 0.11, -0.05),
    (0, 0, 0.14), (0.08, 0, 0.08), (-0.08, 0, 0.08), (0, 0, 0.18),
    (0.18, 0, 0), (-0.18, 0, 0), (0.25, 0, 0), (-0.25, 0, 0),
    (0.23, 0, 0), (-0.23, 0, 0),
)

_SQRT_HALF = math.sqrt(0.5)


# ── 四元数基本运算 ─────────────────────────────────────────

def qmul(a: Quat, b: Quat) -> Quat:
    w, x, y, z = a
    W, X, Y, Z = b
    return (
        w * W - x * X - y * Y - z * Z,
        w * X + x * W + y * Z - z * Y,
        w * Y - x * Z + y * W + z * X,
        w * Z + x * Y - y * X + z * W,
    )


def qrot(q: Quat, v: Vec3) -> Vec3:
    r = qmul(qmul(q, (0.0, *v)), (q[0], -q[1], -q[2], -q[3]))
    return (r[1], r[2], r[3])


def axis_angle_to_quat(v: Sequence[float]) -> Quat:
    angle = math.sqrt(sum(float(x) * float(x) for x in v))
    if angle < 1e-8:
        return (1.0, 0.0, 0.0, 0.0)
    s = math.sin(angle / 2) / angle
    return (math.cos(angle / 2), v[0] * s, v[1] * s, v[2] * s)


def qnormalize(q: Quat) -> Quat:
    n = math.sqrt(sum(c * c for c in q))
    if n < 1e-9:
        return (1.0, 0.0, 0.0, 0.0)
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def qslerp(a: Quat, b: Quat, t: float) -> Quat:
    """最短路径球面插值 —— 段间与来源切换的平滑过渡"""
    dot = sum(x * y for x, y in zip(a, b))
    if dot < 0.0:
        b = (-b[0], -b[1], -b[2], -b[3])
        dot = -dot
    if dot > 0.9995:
        return qnormalize(tuple(x + (y - x) * t for x, y in zip(a, b)))  # type: ignore[arg-type]
    theta_0 = math.acos(max(-1.0, min(1.0, dot)))
    theta = theta_0 * t
    sin_theta_0 = math.sin(theta_0)
    s0 = math.sin(theta_0 - theta) / sin_theta_0
    s1 = math.sin(theta) / sin_theta_0
    return qnormalize(tuple(x * s0 + y * s1 for x, y in zip(a, b)))  # type: ignore[arg-type]


# ── 坐标系变换 z-up → y-up ────────────────────────────────

def to_y_up(v: Vec3) -> Vec3:
    return (v[0], v[2], -v[1])


def to_y_up_quat(q: Quat) -> Quat:
    """基变换 R_x(-90°) · q · R_x(+90°)"""
    return qmul(qmul((_SQRT_HALF, -_SQRT_HALF, 0.0, 0.0), q),
                (_SQRT_HALF, _SQRT_HALF, 0.0, 0.0))


# ── 帧转换 ─────────────────────────────────────────────────

def _as_joint_rows(pose) -> list[Sequence[float]]:
    """把 66 维扁平帧或 22×3 帧统一为 22 行轴角"""
    if len(pose) >= 66 and not isinstance(pose[0], (list, tuple)):
        return [pose[i:i + 3] for i in range(0, 66, 3)]
    return list(pose[:22])


def _normalize_frames(value, expect_rows: bool = False):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not len(value):
        return None
    first = value[0]
    if not isinstance(first, (list, tuple)):
        return [value] if len(value) in (3, 66) else None
    if expect_rows and len(value) == 22 and isinstance(first, (list, tuple)) \
            and len(first) == 3 and not isinstance(first[0], (list, tuple)):
        return [value]
    return list(value)


def convert_sequence(data: dict) -> tuple[list[PostureFrame], float]:
    """DART /generate 响应 → 六设备帧序列 + 帧率"""
    poses = _normalize_frames(data.get("poses") or data.get("pose"))
    if not poses:
        raise ValueError("DART 响应缺少 poses")
    trans = _normalize_frames(data.get("trans")) or [(0.0, 0.0, 0.0)]
    joints_frames = _normalize_frames(data.get("joints"), expect_rows=True)

    fps = float(data.get("framerate") or data.get("fps") or 30.0)
    frames: list[PostureFrame] = []
    floor_offset: Optional[float] = None

    for index, pose in enumerate(poses):
        rows = _as_joint_rows(pose)
        local = [axis_angle_to_quat(row) for row in rows]
        root = trans[min(index, len(trans) - 1)]

        world_q: list[Quat] = []
        derived: list[Vec3] = []
        for j in range(22):
            parent = PARENTS[j]
            world_q.append(local[j] if parent < 0 else qmul(world_q[parent], local[j]))
            if parent < 0:
                derived.append((float(root[0]), float(root[1]), float(root[2])))
            else:
                offset = qrot(world_q[parent], REST_OFFSETS[j])
                base = derived[parent]
                derived.append((base[0] + offset[0], base[1] + offset[1], base[2] + offset[2]))

        joints = (
            joints_frames[min(index, len(joints_frames) - 1)]
            if joints_frames else derived
        )
        # 首帧确定落地补偿，整段沿用以避免上下漂移
        if floor_offset is None:
            floor_offset = -min(float(joints[10][2]), float(joints[11][2]))

        devices: list[DevicePose] = []
        for joint_index in SMPL_DEVICE_JOINTS:
            pos = to_y_up(tuple(float(c) for c in joints[joint_index][:3]))  # type: ignore[arg-type]
            quat = to_y_up_quat(world_q[joint_index])
            devices.append(DevicePose(pos[0], pos[1] + floor_offset, pos[2], *quat))
        frames.append(PostureFrame(tuple(devices)))

    return frames, fps


def lerp_frame(a: PostureFrame, b: PostureFrame, t: float) -> PostureFrame:
    """两帧之间插值 —— 位置线性、旋转球面"""
    t = max(0.0, min(1.0, t))
    devices = []
    for da, db in zip(a.devices, b.devices):
        qa: Quat = (da.qw, da.qx, da.qy, da.qz)
        qb: Quat = (db.qw, db.qx, db.qy, db.qz)
        qw, qx, qy, qz = qslerp(qa, qb, t)
        devices.append(DevicePose(
            da.x + (db.x - da.x) * t,
            da.y + (db.y - da.y) * t,
            da.z + (db.z - da.z) * t,
            qw, qx, qy, qz,
        ))
    return PostureFrame(tuple(devices))


def standing_frame(height: float = 1.7) -> PostureFrame:
    """兜底站立姿势 —— 紧急停止与冷启动时使用"""
    half = height / 2
    devices = (
        DevicePose(0.0, height * 0.94, 0.0),        # head
        DevicePose(-0.20, height * 0.55, 0.05),     # left hand
        DevicePose(0.20, height * 0.55, 0.05),      # right hand
        DevicePose(0.0, half * 1.06, 0.0),          # hip
        DevicePose(-0.10, 0.05, 0.0),               # left foot
        DevicePose(0.10, 0.05, 0.0),                # right foot
    )
    return PostureFrame(devices)
