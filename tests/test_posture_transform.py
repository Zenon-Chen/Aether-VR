"""六设备线格式与 SMPL 坐标转换"""

import math

from aether.contracts.frame import (
    WIRE_MAGIC, WIRE_SIZE, DevicePose, DeviceSlot, PostureFrame,
)
from aether.services.posture.smpl_transform import (
    axis_angle_to_quat, convert_sequence, lerp_frame, qslerp, standing_frame,
    to_y_up, to_y_up_quat,
)


def test_wire_roundtrip():
    """线格式必须与 C++ 驱动的 184 字节结构完全一致"""
    frame = PostureFrame(tuple(
        DevicePose(i, i + 0.5, i - 0.5, 1.0, 0.0, 0.0, 0.0) for i in range(6)
    ))
    payload = frame.pack(42, 1234.5)
    assert len(payload) == WIRE_SIZE == 184

    seq, ts, back = PostureFrame.unpack(payload)
    assert seq == 42 and abs(ts - 1234.5) < 1e-6
    assert abs(back.get(DeviceSlot.RIGHT_FOOT).x - 5.0) < 1e-5


def test_wire_magic():
    assert WIRE_MAGIC == 0x54524144  # "DART"


def test_z_up_to_y_up():
    """DART z-up (x,y,z) → OpenVR y-up (x,z,-y)"""
    assert to_y_up((1.0, 2.0, 3.0)) == (1.0, 3.0, -2.0)


def test_identity_quat_survives_basis_change():
    q = to_y_up_quat((1.0, 0.0, 0.0, 0.0))
    assert abs(q[0] - 1.0) < 1e-9


def test_axis_angle_zero_is_identity():
    assert axis_angle_to_quat((0.0, 0.0, 0.0)) == (1.0, 0.0, 0.0, 0.0)


def test_slerp_endpoints():
    a = (1.0, 0.0, 0.0, 0.0)
    b = axis_angle_to_quat((0.0, 0.0, math.pi / 2))
    assert abs(qslerp(a, b, 0.0)[0] - a[0]) < 1e-6
    assert abs(qslerp(a, b, 1.0)[0] - b[0]) < 1e-6


def test_convert_sequence_produces_six_devices():
    """22 关节 × 3 轴角的两帧输入 → 两帧六设备输出"""
    data = {
        "poses": [[0.0] * 66, [0.0] * 66],
        "trans": [[0.0, 0.0, 0.9], [0.0, 0.1, 0.9]],
        "framerate": 30,
    }
    frames, fps = convert_sequence(data)
    assert len(frames) == 2 and fps == 30
    assert len(frames[0].devices) == 6


def test_floor_offset_lifts_feet_to_ground():
    """首帧确定落地补偿，脚不应穿地"""
    data = {"poses": [[0.0] * 66], "trans": [[0.0, 0.0, 0.9]], "framerate": 30}
    frames, _ = convert_sequence(data)
    left = frames[0].get(DeviceSlot.LEFT_FOOT)
    right = frames[0].get(DeviceSlot.RIGHT_FOOT)
    assert min(left.y, right.y) > -1e-6


def test_lerp_midpoint():
    a = PostureFrame(tuple(DevicePose() for _ in range(6)))
    b = PostureFrame(tuple(DevicePose(x=2.0) for _ in range(6)))
    mid = lerp_frame(a, b, 0.5)
    assert abs(mid.devices[0].x - 1.0) < 1e-6


def test_standing_frame_is_upright():
    frame = standing_frame(1.7)
    assert frame.get(DeviceSlot.HEAD).y > frame.get(DeviceSlot.HIP).y
    assert frame.get(DeviceSlot.HIP).y > frame.get(DeviceSlot.LEFT_FOOT).y
