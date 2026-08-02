"""VRChat OSC 骨骼映射与发送

将 DART 输出的 SMPL-X 22 关节数据映射到 VRChat Humanoid 骨骼，
通过 OSC 协议发送到 VRChat。

发送策略（逐帧模式）：
    1. 预计算整个动作序列所有帧的 OSC 消息（含 frame_index）
    2. 可选帧率插值平滑帧间过渡
    3. 逐帧发送，每帧之间 sleep 帧间隔时间（1/framerate 秒）
"""

import asyncio
import logging
import math
from typing import Optional

try:
    from pythonosc import udp_client
except ImportError:
    udp_client = None  # type: ignore[assignment]

import numpy as np

from ..config import OSCConfig
from ..protocols import OSCPort

logger = logging.getLogger(__name__)

# ======================================================================
# SMPL-X 22 关节名称（按 DART 输出 poses 前 66 维顺序）
# ======================================================================

SMPLX_JOINTS = [
    "Pelvis",          # 0  - 根关节
    "L_Hip",           # 1
    "R_Hip",           # 2
    "Spine1",          # 3
    "L_Knee",          # 4
    "R_Knee",          # 5
    "Spine2",          # 6
    "L_Ankle",         # 7
    "R_Ankle",         # 8
    "Spine3",          # 9
    "L_Foot",          # 10
    "R_Foot",          # 11
    "Neck",            # 12
    "L_Collar",        # 13
    "R_Collar",        # 14
    "Head",            # 15
    "L_Shoulder",      # 16
    "R_Shoulder",      # 17
    "L_Elbow",         # 18
    "R_Elbow",         # 19
    "L_Wrist",         # 20
    "R_Wrist",         # 21
]

# ======================================================================
# SMPL-X → VRChat Humanoid 骨骼映射
# ======================================================================

VRCHAT_BONE_MAP = {
    "Pelvis":   "Hips",
    "Spine1":   "Spine",
    "Spine2":   "Chest",
    "Spine3":   "UpperChest",
    "Neck":     "Neck",
    "Head":     "Head",
    "L_Collar": "LeftShoulder",
    "L_Shoulder": "LeftUpperArm",
    "L_Elbow":  "LeftLowerArm",
    "L_Wrist":  "LeftHand",
    "R_Collar": "RightShoulder",
    "R_Shoulder": "RightUpperArm",
    "R_Elbow":  "RightLowerArm",
    "R_Wrist":  "RightHand",
    "L_Hip":    "LeftUpperLeg",
    "L_Knee":   "LeftLowerLeg",
    "L_Ankle":  "LeftFoot",
    "L_Foot":   "LeftToes",
    "R_Hip":    "RightUpperLeg",
    "R_Knee":   "RightLowerLeg",
    "R_Ankle":  "RightFoot",
    "R_Foot":   "RightToes",
}


# ======================================================================
# 消息构建辅助
# ======================================================================

class _OSCMessage:
    """内部消息结构：(osc_path, values_list)"""
    __slots__ = ("path", "values")

    def __init__(self, path: str, values: list):
        self.path = path
        self.values = values


def _build_frame_messages(
    frame_index: int,
    poses_66: list,
    joints_22x3: Optional[list],
    prefix: str,
) -> list:
    """为一个帧构建全部 OSC 消息列表

    Args:
        frame_index: 帧序号（用于同步参数）
        poses_66: 当前帧 22 关节 × 3 轴角 = 66 维
        joints_22x3: 当前帧 22 关节 × 3 坐标，可为 None
        prefix: OSC 路径前缀

    Returns:
        list[_OSCMessage]
    """
    msgs: list[_OSCMessage] = []

    # 发送 frame_index 供接收端同步
    msgs.append(_OSCMessage(f"{prefix}/FrameIndex", [frame_index]))

    # --- 旋转（轴角） ---
    for joint_idx in range(22):
        ax = poses_66[joint_idx * 3]
        ay = poses_66[joint_idx * 3 + 1]
        az = poses_66[joint_idx * 3 + 2]

        smplx_name = SMPLX_JOINTS[joint_idx]
        vrchat_name = VRCHAT_BONE_MAP.get(smplx_name)
        if vrchat_name:
            msgs.append(_OSCMessage(
                f"{prefix}/{vrchat_name}/rotation", [ax, ay, az]
            ))

    # --- 位置 ---
    if joints_22x3 is not None:
        for joint_idx in range(22):
            x = joints_22x3[joint_idx][0]
            y = joints_22x3[joint_idx][1]
            z = joints_22x3[joint_idx][2]

            smplx_name = SMPLX_JOINTS[joint_idx]
            vrchat_name = VRCHAT_BONE_MAP.get(smplx_name)
            if vrchat_name:
                msgs.append(_OSCMessage(
                    f"{prefix}/{vrchat_name}/position", [x, y, z]
                ))

    return msgs


# ======================================================================
# OSCSender
# ======================================================================

class OSCSender(OSCPort):
    """VRChat OSC 骨骼数据发送器

    使用 python-osc 的 SimpleUDPClient 发送 OSC 消息。
    SimpleUDPClient 本身是同步的，在 async 上下文中通过
    run_in_executor 进行逐帧发送以避免阻塞事件循环。
    """

    def __init__(self, config: OSCConfig):
        if udp_client is None:
            raise ImportError(
                "python-osc 未安装，请运行: pip install python-osc"
            )
        self.config = config
        self.client: Optional[udp_client.SimpleUDPClient] = None
        self._stop_event = asyncio.Event()
        self._stop_event.set()  # 初始状态为「未播放」
        self._current_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """建立 OSC UDP 连接"""
        self.client = udp_client.SimpleUDPClient(
            self.config.host,
            self.config.port,
        )
        logger.info(
            "[OSC] Connected to %s:%d", self.config.host, self.config.port
        )

    def _ensure_client(self) -> "udp_client.SimpleUDPClient":
        if self.client is None:
            raise RuntimeError(
                "OSCSender 尚未连接，请先调用 connect()"
            )
        return self.client

    # ------------------------------------------------------------------
    # 预计算：将整个动作序列编译为消息列表
    # ------------------------------------------------------------------

    @staticmethod
    def precompute_messages(
        motion_data: dict,
        prefix: str,
    ) -> list[list[_OSCMessage]]:
        """预计算所有帧的 OSC 消息

        Args:
            motion_data: DART 返回的骨架数据 dict
                poses:   (T, 165) — 只取前 66 维
                joints:  (T, 22, 3)，可选
            prefix: OSC 路径前缀

        Returns:
            list[list[_OSCMessage]]: 每帧对应一个消息列表
        """
        poses = motion_data.get("poses", [])
        joints = motion_data.get("joints", [])
        all_frames: list[list[_OSCMessage]] = []

        for frame_idx in range(len(poses)):
            frame_poses = poses[frame_idx][:66]  # 只取有效 22 关节
            frame_joints = joints[frame_idx] if frame_idx < len(joints) else None
            msgs = _build_frame_messages(
                frame_idx, frame_poses, frame_joints, prefix
            )
            all_frames.append(msgs)

        return all_frames

    # ------------------------------------------------------------------
    # 批量发送
    # ------------------------------------------------------------------

    def _send_batch(self, messages: list[_OSCMessage]) -> None:
        """同步批量发送一组 OSC 消息（在线程池中调用）"""
        client = self._ensure_client()
        for msg in messages:
            client.send_message(msg.path, msg.values)

    async def _send_batch_async(
        self, messages: list[_OSCMessage]
    ) -> None:
        """异步批量发送：将同步 send 放到 executor 避免阻塞"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._send_batch, messages)

    # ------------------------------------------------------------------
    # 播放控制
    # ------------------------------------------------------------------

    @property
    def _playing(self) -> bool:
        """基于 _stop_event 反值，避免竞态"""
        return not self._stop_event.is_set()

    async def play_motion(self, motion_data: dict) -> None:
        """异步播放动作序列（非阻塞，后台 Task）

        Args:
            motion_data: DART 返回的骨架数据 dict
        """
        if self._playing:
            logger.warning("[OSC] Already playing, cancelling current motion")
            old_task = self._current_task
            self.stop()
            # 等待旧 task 完全结束，避免 finally 块与新 task 交错
            if old_task is not None:
                try:
                    await old_task
                except (asyncio.CancelledError, Exception):
                    pass

        self._stop_event.clear()
        self._current_task = asyncio.create_task(
            self._motion_loop(motion_data)
        )

    async def _motion_loop(self, motion_data: dict) -> None:
        """核心播放循环：预计算 → 逐帧发送 → 帧间隔 sleep"""
        framerate = motion_data.get("framerate", 30)
        num_frames = motion_data.get("num_frames", len(motion_data.get("poses", [])))

        logger.info(
            "[OSC] Pre-computing %d frames at %d fps",
            num_frames, framerate,
        )

        # 1) 预计算所有帧消息
        all_frame_msgs = self.precompute_messages(
            motion_data, self.config.avatar_prefix
        )

        if not all_frame_msgs:
            logger.warning("[OSC] No frames to play")
            self._stop_event.set()
            return

        # 2) 可选：帧率插值平滑过渡（src_fps → target_fps）
        target_fps = self.config.target_fps
        if framerate < target_fps:
            logger.info(
                "[OSC] Interpolating frames from %d fps to %d fps",
                framerate, target_fps,
            )
            # 对 poses 进行插值，然后重新预计算消息
            poses = motion_data.get("poses", [])
            joints = motion_data.get("joints", [])
            trans = motion_data.get("trans", [])
            interpolated_poses = self.interpolate_frames(poses, framerate, target_fps)
            # 同步插值 joints 和 trans（如果有）
            interpolated_joints = self.interpolate_frames(joints, framerate, target_fps) if joints else []
            interpolated_trans = self.interpolate_frames(trans, framerate, target_fps) if trans else []
            interpolated_data = {
                **motion_data,
                "poses": interpolated_poses,
                "joints": interpolated_joints,
                "trans": interpolated_trans,
            }
            all_frame_msgs = self.precompute_messages(
                interpolated_data, self.config.avatar_prefix
            )
            framerate = target_fps
            num_frames = len(all_frame_msgs)

        frame_interval = 1.0 / framerate
        total_frames = len(all_frame_msgs)
        logger.info(
            "[OSC] Playing %d frames at %.1f fps (interval=%.3fs)",
            total_frames, framerate, frame_interval,
        )

        try:
            for frame_idx, frame_msgs in enumerate(all_frame_msgs):
                # 检查停止信号
                if self._stop_event.is_set():
                    logger.info("[OSC] Stop event received at frame %d/%d", frame_idx, total_frames)
                    break

                # 发送当前帧的全部 OSC 消息
                await self._send_batch_async(frame_msgs)

                # sleep 帧间隔时间（用 stop_event.wait 实现可中断 sleep）
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=frame_interval)
                    # 如果 wait_for 正常返回（没有超时），说明 stop_event 被设置了
                    break
                except asyncio.TimeoutError:
                    # 超时是正常的——表示帧间隔已过，继续下一帧
                    pass

        except asyncio.CancelledError:
            logger.info("[OSC] Motion playback cancelled")
        finally:
            self._stop_event.set()
            logger.info("[OSC] Motion playback complete")

    def stop(self) -> None:
        """停止当前动作播放"""
        self._stop_event.set()
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        self._current_task = None

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def send_idle_pose(self) -> None:
        """发送站立/空闲姿态（所有旋转归零）"""
        client = self._ensure_client()
        prefix = self.config.avatar_prefix
        for vrchat_name in VRCHAT_BONE_MAP.values():
            client.send_message(
                f"{prefix}/{vrchat_name}/rotation", [0.0, 0.0, 0.0]
            )
        client.send_message(f"{prefix}/FrameIndex", [0])
        logger.info("[OSC] Idle pose sent")

    @staticmethod
    def interpolate_frames(
        poses: list,
        src_fps: int = 30,
        target_fps: int = 60,
    ) -> list:
        """帧率插值：线性插值将 src_fps 提升到 target_fps

        当未来需要更高帧率时调用，当前 30fps 直接批量发送即可。

        Args:
            poses: 原始帧列表，每帧为 float 列表
            src_fps: 源帧率
            target_fps: 目标帧率

        Returns:
            插值后的帧列表
        """
        if src_fps >= target_fps or not poses:
            return list(poses)

        ratio = target_fps / src_fps
        new_poses: list[list[float]] = []

        for i in range(len(poses) - 1):
            new_poses.append(poses[i])
            for j in range(1, int(ratio)):
                alpha = j / ratio
                interpolated = [
                    poses[i][k] * (1 - alpha) + poses[i + 1][k] * alpha
                    for k in range(len(poses[i]))
                ]
                new_poses.append(interpolated)

        # 最后一帧
        new_poses.append(poses[-1])
        return new_poses
