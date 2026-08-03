"""姿态帧契约 — DART SMPL 输出 → SteamVR 六设备位姿

与既有 dart-steamvr-bridge 的 UDP 线格式严格对应：

    magic   uint32  0x54524144 ("DART" little endian)
    sequence uint32
    time    double
    pose    float32[6][7]   每个设备 (x, y, z, qw, qx, qy, qz)

设备槽位顺序不可更改 —— C++ 驱动按索引取用。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

# ── 线格式常量（与 driver.cpp 对齐，改动需同步 C++ 侧）──────────────
WIRE_MAGIC = 0x54524144
WIRE_DEVICE_COUNT = 6
WIRE_STRUCT = struct.Struct("<IId" + "f" * (WIRE_DEVICE_COUNT * 7))
WIRE_SIZE = WIRE_STRUCT.size  # 184 bytes

#: 驱动 1 秒收不到包即判定 OutOfRange，保活流必须快于此
DRIVER_STALE_TIMEOUT_S = 1.0


class DeviceSlot(IntEnum):
    """六设备槽位 — 索引即 UDP 包内顺序，不可重排"""
    HEAD = 0
    LEFT_HAND = 1
    RIGHT_HAND = 2
    HIP = 3
    LEFT_FOOT = 4
    RIGHT_FOOT = 5


#: 各槽位对应的 SMPL 22 关节索引（与 bridge.py JOINTS 一致）
SMPL_DEVICE_JOINTS: tuple[int, ...] = (15, 20, 21, 0, 10, 11)


@dataclass(frozen=True, slots=True)
class DevicePose:
    """单设备位姿 — OpenVR y-up 右手系，米 / 单位四元数"""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    qw: float = 1.0
    qx: float = 0.0
    qy: float = 0.0
    qz: float = 0.0

    def as_tuple(self) -> tuple[float, ...]:
        return (self.x, self.y, self.z, self.qw, self.qx, self.qy, self.qz)

    @classmethod
    def from_sequence(cls, values) -> "DevicePose":
        return cls(*(float(v) for v in values))


@dataclass(frozen=True, slots=True)
class PostureFrame:
    """一帧完整的六设备位姿

    posture-service 以固定频率向驱动推送此结构；即使角色静止也必须
    持续推送，否则驱动会将设备标记为失联。
    """
    devices: tuple[DevicePose, ...] = field(
        default_factory=lambda: tuple(DevicePose() for _ in range(WIRE_DEVICE_COUNT))
    )

    def __post_init__(self) -> None:
        if len(self.devices) != WIRE_DEVICE_COUNT:
            raise ValueError(
                f"PostureFrame 需要 {WIRE_DEVICE_COUNT} 个设备，收到 {len(self.devices)}"
            )

    def pack(self, sequence: int, timestamp: float) -> bytes:
        """序列化为驱动可解析的 UDP 载荷"""
        flat: list[float] = []
        for device in self.devices:
            flat.extend(device.as_tuple())
        return WIRE_STRUCT.pack(WIRE_MAGIC, sequence & 0xFFFFFFFF, timestamp, *flat)

    @classmethod
    def unpack(cls, payload: bytes) -> tuple[int, float, "PostureFrame"]:
        """反序列化（用于回放测试与桥自检）"""
        if len(payload) != WIRE_SIZE:
            raise ValueError(f"载荷长度应为 {WIRE_SIZE}，收到 {len(payload)}")
        fields = WIRE_STRUCT.unpack(payload)
        if fields[0] != WIRE_MAGIC:
            raise ValueError(f"magic 不匹配: {fields[0]:#x}")
        sequence, timestamp = fields[1], fields[2]
        values = fields[3:]
        devices = tuple(
            DevicePose.from_sequence(values[i * 7:(i + 1) * 7])
            for i in range(WIRE_DEVICE_COUNT)
        )
        return sequence, timestamp, cls(devices=devices)

    def get(self, slot: DeviceSlot) -> DevicePose:
        return self.devices[int(slot)]
