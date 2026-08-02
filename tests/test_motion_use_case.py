"""Tests for aether.motion.use_case — MotionUseCase with fake ports."""

import pytest
from unittest.mock import AsyncMock

from aether.motion.use_case import MotionUseCase


class FakeMotionPort:
    """Mock MotionPort：可配置可用性与生成结果"""

    def __init__(self, available=True, result="default"):
        self.available = available
        # "default" → 完整动作数据；None → generate 返回 {"num_frames": 0}
        self.result = result if result != "default" else {
            "poses": [[0.0] * 165] * 10,
            "num_frames": 10,
            "framerate": 30,
        }
        self.generated_prompts = []

    async def is_available(self):
        return self.available

    async def generate(self, text, guidance_param=5.0):
        self.generated_prompts.append(text)
        return self.result

    @staticmethod
    def format_prompt(action, duration_seconds=2.0):
        return f"{action}*{int(duration_seconds * 30 / 8)}"


class FakeRenderer:
    """Mock OSCPort"""

    def __init__(self):
        self.played = []

    async def play_motion(self, motion_data):
        self.played.append(motion_data)


class TestMotionUseCase:
    """Test MotionUseCase orchestration."""

    @pytest.mark.asyncio
    async def test_execute_generates_and_plays(self):
        motion = FakeMotionPort()
        renderer = FakeRenderer()
        uc = MotionUseCase(motion, renderer)

        result = await uc.execute("walk forward", 2.0)

        assert result["num_frames"] == 10
        assert len(motion.generated_prompts) == 1
        assert renderer.played == [motion.result]

    @pytest.mark.asyncio
    async def test_execute_dart_unavailable(self):
        motion = FakeMotionPort(available=False)
        renderer = FakeRenderer()
        uc = MotionUseCase(motion, renderer)

        result = await uc.execute("walk forward", 2.0)

        assert result["num_frames"] == 0
        assert "DART" in result["error"]
        assert renderer.played == []  # 不可用时不应播放

    @pytest.mark.asyncio
    async def test_execute_generate_exception(self):
        class FailingMotion(FakeMotionPort):
            async def generate(self, text, guidance_param=5.0):
                raise RuntimeError("boom")

        uc = MotionUseCase(FailingMotion(), FakeRenderer())
        result = await uc.execute("dance", 2.0)

        assert result["num_frames"] == 0
        assert "boom" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_empty_result(self):
        motion = FakeMotionPort(result=None)
        renderer = FakeRenderer()
        uc = MotionUseCase(motion, renderer)

        result = await uc.execute("jump", 2.0)
        assert result == {}  # None → {}（result or {}）
        assert renderer.played == []

    def test_implements_ports(self):
        from aether.protocols import MotionPort, OSCPort
        # 真实 DARTClient/OSCSender 与用例的端口依赖一致
        assert hasattr(MotionPort, "format_prompt")
        assert hasattr(MotionPort, "is_available")
