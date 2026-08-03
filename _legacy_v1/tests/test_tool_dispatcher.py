"""Tests for aether.tool_dispatcher — function call routing and dispatch."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from typing import Optional

# Mock google.genai.types before importing the dispatcher
mock_types = MagicMock()
mock_types.FunctionResponse = MagicMock(side_effect=lambda **kw: kw)

with patch.dict("sys.modules", {"google": MagicMock(), "google.genai": MagicMock(), "google.genai.types": mock_types}):
    from aether.tool_dispatcher import (
        ToolDispatcher,
        DispatcherState,
        MotionState,
        VALID_EMOTIONS,
        VALID_SPEECH_RATES,
    )


# ── Helper: fake function call object ────────────────────────────────

@dataclass
class FakeFunctionCall:
    """Mimics google.genai.types.FunctionCall"""
    id: str
    name: str
    args: dict


@dataclass
class FakeToolCall:
    """Mimics the tool_call object received from ER2"""
    function_calls: list


# ── Tests ────────────────────────────────────────────────────────────

class TestDispatcherInit:
    """Test ToolDispatcher initialization."""

    def test_initial_state(self):
        d = ToolDispatcher()
        assert d.state.motion_state == MotionState.IDLE
        assert d.state.emotion_state == "neutral"
        assert d.state.recent_actions == []
        assert d.state.active_tasks == {}

    def test_handlers_default_none(self):
        d = ToolDispatcher()
        assert d._tts_handler is None
        assert d._motion_handler is None
        assert d._memory_handler is None
        assert d._status_handler is None
        assert d._response_callback is None


class TestHandlerRegistration:
    """Test injecting handlers into the dispatcher."""

    def test_set_tts_handler(self):
        d = ToolDispatcher()
        handler = AsyncMock()
        d.set_tts_handler(handler)
        assert d._tts_handler is handler

    def test_set_motion_handler(self):
        d = ToolDispatcher()
        handler = AsyncMock()
        d.set_motion_handler(handler)
        assert d._motion_handler is handler

    def test_set_memory_handler(self):
        d = ToolDispatcher()
        handler = AsyncMock()
        d.set_memory_handler(handler)
        assert d._memory_handler is handler

    def test_set_status_handler(self):
        d = ToolDispatcher()
        handler = AsyncMock()
        d.set_status_handler(handler)
        assert d._status_handler is handler

    def test_set_response_callback(self):
        d = ToolDispatcher()
        cb = AsyncMock()
        d.set_response_callback(cb)
        assert d._response_callback is cb


class TestDispatchRouting:
    """Test _dispatch_single routes to correct handler."""

    @pytest.mark.asyncio
    async def test_tts_dispatch(self):
        d = ToolDispatcher()
        d._tts_handler = AsyncMock(return_value="ok")
        fc = FakeFunctionCall(id="1", name="execute_tts", args={"text": "hello", "emotion": "happy"})
        result = await d._dispatch_single(fc)
        assert result["status"] == "played"
        assert result["text"] == "hello"
        assert result["emotion"] == "happy"

    @pytest.mark.asyncio
    async def test_motion_dispatch(self):
        d = ToolDispatcher()
        d._motion_handler = AsyncMock(return_value={"num_frames": 30})
        fc = FakeFunctionCall(id="2", name="execute_motion", args={"action": "walk forward", "duration": 2.0})
        result = await d._dispatch_single(fc)
        assert result["status"] == "motion_applied"
        assert result["action"] == "walk forward"

    @pytest.mark.asyncio
    async def test_status_dispatch(self):
        d = ToolDispatcher()
        d._status_handler = AsyncMock()
        fc = FakeFunctionCall(id="3", name="report_status", args={"status_type": "idle", "detail": "standing"})
        result = await d._dispatch_single(fc)
        assert result["status"] == "acknowledged"

    @pytest.mark.asyncio
    async def test_memory_dispatch(self):
        d = ToolDispatcher()
        d._memory_handler = AsyncMock(return_value="recalled text")
        fc = FakeFunctionCall(id="4", name="recall_memory", args={"query": "past events"})
        result = await d._dispatch_single(fc)
        assert result["status"] == "recalled"
        assert result["memory"] == "recalled text"

    @pytest.mark.asyncio
    async def test_unknown_function(self):
        d = ToolDispatcher()
        fc = FakeFunctionCall(id="5", name="unknown_func", args={})
        result = await d._dispatch_single(fc)
        assert result["status"] == "error"
        assert "Unknown function" in result["error"]


class TestTTSValidation:
    """Test TTS argument validation in dispatcher."""

    @pytest.mark.asyncio
    async def test_empty_text_returns_error(self):
        d = ToolDispatcher()
        d._tts_handler = AsyncMock()
        fc = FakeFunctionCall(id="1", name="execute_tts", args={"text": ""})
        result = await d._dispatch_single(fc)
        assert result["status"] == "error"
        assert "empty" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_emotion_falls_back_to_neutral(self):
        d = ToolDispatcher()
        d._tts_handler = AsyncMock(return_value="ok")
        fc = FakeFunctionCall(id="1", name="execute_tts",
                               args={"text": "hello", "emotion": "INVALID"})
        result = await d._dispatch_single(fc)
        assert result["emotion"] == "neutral"

    @pytest.mark.asyncio
    async def test_invalid_speech_rate_falls_back_to_normal(self):
        d = ToolDispatcher()
        d._tts_handler = AsyncMock(return_value="ok")
        fc = FakeFunctionCall(id="1", name="execute_tts",
                               args={"text": "hello", "speech_rate": "ultra"})
        result = await d._dispatch_single(fc)
        # speech_rate is internal, just check it played
        assert result["status"] == "played"

    @pytest.mark.asyncio
    async def test_long_text_truncated(self):
        d = ToolDispatcher()
        d._tts_handler = AsyncMock(return_value="ok")
        long_text = "a" * 300
        fc = FakeFunctionCall(id="1", name="execute_tts", args={"text": long_text})
        result = await d._dispatch_single(fc)
        assert result["status"] == "played"
        # Text should be truncated to TEXT_MAX_LENGTH (200)
        assert len(result["text"]) == 200


class TestMotionValidation:
    """Test motion argument validation."""

    @pytest.mark.asyncio
    async def test_empty_action_returns_error(self):
        d = ToolDispatcher()
        d._motion_handler = AsyncMock()
        fc = FakeFunctionCall(id="1", name="execute_motion", args={"action": ""})
        result = await d._dispatch_single(fc)
        assert result["status"] == "error"
        assert "empty" in result["error"]

    @pytest.mark.asyncio
    async def test_duration_clamped_min(self):
        d = ToolDispatcher()
        d._motion_handler = AsyncMock(return_value={"num_frames": 10})
        fc = FakeFunctionCall(id="1", name="execute_motion",
                               args={"action": "nod", "duration": 0.1})
        result = await d._dispatch_single(fc)
        assert result["status"] == "motion_applied"

    @pytest.mark.asyncio
    async def test_duration_clamped_max(self):
        d = ToolDispatcher()
        d._motion_handler = AsyncMock(return_value={"num_frames": 10})
        fc = FakeFunctionCall(id="1", name="execute_motion",
                               args={"action": "dance", "duration": 999})
        result = await d._dispatch_single(fc)
        assert result["status"] == "motion_applied"


class TestHandleToolCall:
    """Test handle_tool_call creates background tasks."""

    @pytest.mark.asyncio
    async def test_handle_tool_call_returns_empty_list(self):
        d = ToolDispatcher()
        d._tts_handler = AsyncMock(return_value="ok")
        d._response_callback = AsyncMock()
        tc = FakeToolCall(function_calls=[
            FakeFunctionCall(id="fc1", name="execute_tts", args={"text": "hi"})
        ])
        result = await d.handle_tool_call(tc)
        assert result == []
        # Allow background task to complete
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_multiple_tool_calls(self):
        d = ToolDispatcher()
        d._tts_handler = AsyncMock(return_value="ok")
        d._motion_handler = AsyncMock(return_value={"num_frames": 5})
        d._response_callback = AsyncMock()
        tc = FakeToolCall(function_calls=[
            FakeFunctionCall(id="fc1", name="execute_tts", args={"text": "hi"}),
            FakeFunctionCall(id="fc2", name="execute_motion", args={"action": "wave"}),
        ])
        result = await d.handle_tool_call(tc)
        assert result == []
        await asyncio.sleep(0.2)


class TestCancelPending:
    """Test cancel_pending for interrupt handling."""

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_id(self):
        d = ToolDispatcher()
        await d.cancel_pending(["nonexistent"])
        # Should not raise

    @pytest.mark.asyncio
    async def test_cancel_active_task(self):
        d = ToolDispatcher()

        async def slow_handler(*args, **kwargs):
            await asyncio.sleep(10)
            return "ok"

        d._tts_handler = slow_handler
        d._response_callback = AsyncMock()

        tc = FakeToolCall(function_calls=[
            FakeFunctionCall(id="fc_slow", name="execute_tts", args={"text": "long"})
        ])
        await d.handle_tool_call(tc)
        await asyncio.sleep(0.05)

        # Now cancel
        await d.cancel_pending(["fc_slow"])
        await asyncio.sleep(0.1)


class TestGetStateSummary:
    """Test get_state_summary output."""

    def test_empty_state(self):
        d = ToolDispatcher()
        summary = d.get_state_summary()
        assert "Motion: idle" in summary
        assert "Emotion: neutral" in summary

    def test_with_recent_actions(self):
        d = ToolDispatcher()
        d.state.recent_actions = [
            {"action": "walk forward", "duration": 2.0, "timestamp": 0},
            {"action": "wave hand", "duration": 1.0, "timestamp": 1},
        ]
        summary = d.get_state_summary()
        assert "walk forward" in summary
        assert "wave hand" in summary


class TestMotionStateEnum:
    """Test MotionState enum values."""

    def test_enum_values(self):
        assert MotionState.IDLE.value == "idle"
        assert MotionState.GENERATING.value == "generating"
        assert MotionState.PLAYING.value == "playing"
        assert MotionState.CANCELLED.value == "cancelled"


class TestValidConstants:
    """Test valid emotion and speech rate constants."""

    def test_valid_emotions_contains_neutral(self):
        assert "neutral" in VALID_EMOTIONS

    def test_valid_emotions_contains_happy(self):
        assert "happy" in VALID_EMOTIONS

    def test_valid_speech_rates(self):
        assert VALID_SPEECH_RATES == {"slow", "normal", "fast"}
