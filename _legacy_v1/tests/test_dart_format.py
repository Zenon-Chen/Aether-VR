"""Tests for aether.motion.dart_client and aether.motion.osc_sender — DART format & OSC mapping."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aether.config import DARTConfig, OSCConfig


# ── DART Client Tests ────────────────────────────────────────────────

class TestDARTClientFormatPrompt:
    """Test DARTClient.format_prompt static method."""

    def test_format_prompt_2_seconds(self):
        from aether.motion.dart_client import DARTClient
        result = DARTClient.format_prompt("walk forward", 2.0)
        assert result == "walk forward*7"

    def test_format_prompt_1_second(self):
        from aether.motion.dart_client import DARTClient
        result = DARTClient.format_prompt("wave hand", 1.0)
        assert result == "wave hand*3"

    def test_format_prompt_half_second(self):
        from aether.motion.dart_client import DARTClient
        result = DARTClient.format_prompt("nod head", 0.5)
        assert result == "nod head*1"

    def test_format_prompt_zero_duration(self):
        from aether.motion.dart_client import DARTClient
        result = DARTClient.format_prompt("jump", 0.0)
        assert result == "jump*1"  # min segments = 1


class TestDARTClientSecondsToSegments:
    """Test seconds_to_segments conversion."""

    def test_2_seconds(self):
        from aether.motion.dart_client import DARTClient
        assert DARTClient.seconds_to_segments(2.0) == 7

    def test_0_5_seconds(self):
        from aether.motion.dart_client import DARTClient
        assert DARTClient.seconds_to_segments(0.5) == 1

    def test_minimum_is_1(self):
        from aether.motion.dart_client import DARTClient
        assert DARTClient.seconds_to_segments(0.0) == 1

    def test_10_seconds(self):
        from aether.motion.dart_client import DARTClient
        # 10 * 30 = 300 frames, 300 / 8 = 37
        assert DARTClient.seconds_to_segments(10.0) == 37

    def test_custom_framerate(self):
        from aether.motion.dart_client import DARTClient
        # 2s * 60fps = 120 frames, 120 / 8 = 15
        assert DARTClient.seconds_to_segments(2.0, framerate=60) == 15


class TestDARTClientInit:
    """Test DARTClient initialization."""

    def test_init_with_config(self):
        from aether.motion.dart_client import DARTClient
        config = DARTConfig(host="192.168.1.1", port=9000, timeout=60)
        client = DARTClient(config)
        assert client.base_url == "http://192.168.1.1:9000"
        assert client._client is None

    def test_ensure_client_raises_before_connect(self):
        from aether.motion.dart_client import DARTClient
        config = DARTConfig()
        client = DARTClient(config)
        with pytest.raises(RuntimeError, match="尚未连接"):
            client._ensure_client()


class TestDARTClientGenerate:
    """Test DART generate request/response format (mocked HTTP)."""

    @pytest.mark.asyncio
    async def test_generate_request_format(self):
        from aether.motion.dart_client import DARTClient
        config = DARTConfig()
        client = DARTClient(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "poses": [[0.0] * 165],
            "trans": [[0.0, 0.0, 0.0]],
            "betas": [0.0] * 10,
            "joints": [[[0.0, 0.0, 0.0]] * 22],
            "framerate": 30,
            "num_frames": 1,
        }
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        client._client = mock_http

        result = await client.generate("walk forward*5", guidance_param=5.0)

        mock_http.post.assert_called_once_with(
            "/generate",
            json={"text": "walk forward*5", "guidance_param": 5.0}
        )
        assert result["num_frames"] == 1
        assert result["framerate"] == 30
        assert len(result["poses"]) == 1
        assert len(result["poses"][0]) == 165

    @pytest.mark.asyncio
    async def test_generate_response_structure(self):
        from aether.motion.dart_client import DARTClient
        config = DARTConfig()
        client = DARTClient(config)

        expected = {
            "poses": [[0.1] * 66 + [0.0] * 99] * 3,
            "trans": [[0.0, 0.0, 0.0]] * 3,
            "betas": [0.5] * 10,
            "joints": [[[0.0, 0.0, 0.0]] * 22] * 3,
            "framerate": 30,
            "num_frames": 3,
        }

        mock_response = MagicMock()
        mock_response.json.return_value = expected
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        client._client = mock_http

        result = await client.generate("dance*3")
        assert len(result["poses"]) == 3
        assert len(result["joints"]) == 3
        assert len(result["joints"][0]) == 22
        assert result["betas"] == [0.5] * 10


class TestDARTClientHealthCheck:
    """Test health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check_ok(self):
        from aether.motion.dart_client import DARTClient
        config = DARTConfig()
        client = DARTClient(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "gpu_available": True,
            "model_loaded": True,
        }
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        client._client = mock_http

        result = await client.health_check()
        assert result["status"] == "ok"
        assert result["gpu_available"] is True

    @pytest.mark.asyncio
    async def test_is_available_true(self):
        from aether.motion.dart_client import DARTClient
        config = DARTConfig()
        client = DARTClient(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "ok"}
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        client._client = mock_http

        assert await client.is_available() is True


# ── OSC Sender Tests ────────────────────────────────────────────────

class TestOSCBoneMapping:
    """Test SMPL-X to VRChat bone mapping completeness."""

    def test_all_22_joints_mapped(self):
        from aether.motion.osc_sender import SMPLX_JOINTS, VRCHAT_BONE_MAP
        assert len(SMPLX_JOINTS) == 22
        for joint in SMPLX_JOINTS:
            assert joint in VRCHAT_BONE_MAP, f"Joint '{joint}' not mapped"

    def test_vrchat_bone_names(self):
        from aether.motion.osc_sender import VRCHAT_BONE_MAP
        expected_vrchat_bones = {
            "Hips", "Spine", "Chest", "UpperChest", "Neck", "Head",
            "LeftShoulder", "LeftUpperArm", "LeftLowerArm", "LeftHand",
            "RightShoulder", "RightUpperArm", "RightLowerArm", "RightHand",
            "LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "LeftToes",
            "RightUpperLeg", "RightLowerLeg", "RightFoot", "RightToes",
        }
        actual = set(VRCHAT_BONE_MAP.values())
        assert actual == expected_vrchat_bones

    def test_pelvis_maps_to_hips(self):
        from aether.motion.osc_sender import VRCHAT_BONE_MAP
        assert VRCHAT_BONE_MAP["Pelvis"] == "Hips"

    def test_head_maps_to_head(self):
        from aether.motion.osc_sender import VRCHAT_BONE_MAP
        assert VRCHAT_BONE_MAP["Head"] == "Head"


class TestOSCMessageBuilding:
    """Test OSC message construction from SMPL-X data."""

    def test_frame_messages_contain_rotation_and_position(self):
        from aether.motion.osc_sender import _build_frame_messages
        poses_66 = [0.1] * 66
        joints_22x3 = [[0.0, 0.0, 0.0]] * 22
        prefix = "/avatar/parameters"

        msgs = _build_frame_messages(0, poses_66, joints_22x3, prefix)

        # Should have: 1 FrameIndex + 22 rotations + 22 positions = 45
        assert len(msgs) == 45
        assert msgs[0].path == "/avatar/parameters/FrameIndex"
        assert msgs[0].values == [0]

    def test_frame_messages_without_joints(self):
        from aether.motion.osc_sender import _build_frame_messages
        poses_66 = [0.0] * 66
        prefix = "/avatar/parameters"

        msgs = _build_frame_messages(0, poses_66, None, prefix)

        # 1 FrameIndex + 22 rotations = 23
        assert len(msgs) == 23

    def test_rotation_values_extracted_correctly(self):
        from aether.motion.osc_sender import _build_frame_messages
        # Set specific rotation for Pelvis (joint 0): (1.0, 2.0, 3.0)
        poses_66 = [0.0] * 66
        poses_66[0] = 1.0
        poses_66[1] = 2.0
        poses_66[2] = 3.0

        msgs = _build_frame_messages(0, poses_66, None, "/avatar/parameters")
        # Second message is Pelvis/Hips rotation
        hips_msg = msgs[1]
        assert hips_msg.path == "/avatar/parameters/Hips/rotation"
        assert hips_msg.values == [1.0, 2.0, 3.0]


class TestOSCPrecomputeMessages:
    """Test precomputing all frame messages."""

    def test_precompute_multi_frame(self):
        from aether.motion.osc_sender import OSCSender
        motion_data = {
            "poses": [[0.0] * 165] * 5,  # 5 frames, 165 dims each
            "joints": [[[0.0, 0.0, 0.0]] * 22] * 5,
        }
        all_msgs = OSCSender.precompute_messages(motion_data, "/avatar/parameters")
        assert len(all_msgs) == 5
        # Each frame: 1 FrameIndex + 22 rot + 22 pos = 45
        assert len(all_msgs[0]) == 45

    def test_precompute_empty_poses(self):
        from aether.motion.osc_sender import OSCSender
        motion_data = {"poses": [], "joints": []}
        all_msgs = OSCSender.precompute_messages(motion_data, "/avatar/parameters")
        assert len(all_msgs) == 0


class TestOSCInterpolation:
    """Test frame rate interpolation."""

    def test_no_interpolation_when_same_fps(self):
        from aether.motion.osc_sender import OSCSender
        poses = [[1.0, 2.0], [3.0, 4.0]]
        result = OSCSender.interpolate_frames(poses, src_fps=30, target_fps=30)
        assert result == poses

    def test_interpolation_doubles_frames(self):
        from aether.motion.osc_sender import OSCSender
        poses = [[0.0, 0.0], [2.0, 2.0]]
        result = OSCSender.interpolate_frames(poses, src_fps=30, target_fps=60)
        # Original: 2 frames -> 3 frames (2 original + 1 interpolated)
        assert len(result) == 3
        # Middle frame should be interpolated
        assert result[1] == [1.0, 1.0]

    def test_empty_poses_returns_empty(self):
        from aether.motion.osc_sender import OSCSender
        result = OSCSender.interpolate_frames([], src_fps=30, target_fps=60)
        assert result == []
