"""Tests for aether.er2.client — ER2 Live API client."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from aether.config import ER2Config


# ── Mock google.genai SDK ────────────────────────────────────────────

def _make_mock_genai():
    """Build a mock google.genai module."""
    mock_client_cls = MagicMock()
    mock_client_instance = MagicMock()
    mock_client_cls.return_value = mock_client_instance
    return mock_client_cls, mock_client_instance


@pytest.fixture
def er2_config():
    return ER2Config(api_key="test-key", model="test-model")


@pytest.fixture
def mock_session():
    """Create a mock ER2 session."""
    session = AsyncMock()
    session.send_realtime_input = AsyncMock()
    session.send_client_content = AsyncMock()
    session.send_tool_response = AsyncMock()
    return session


class TestER2ClientInit:
    """Test ER2Client initialization."""

    def test_init_creates_client(self, er2_config):
        with patch("aether.er2.client.genai") as mock_genai:
            mock_genai.Client = MagicMock()
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            assert client.config is er2_config
            assert client.session is None
            assert client._running is False
            assert client._resumption_token is None

    def test_init_stores_config(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            assert client.config.model == "test-model"
            assert client.config.api_key == "test-key"


class TestER2ClientHandlers:
    """Test handler injection methods."""

    def test_set_tool_call_handler(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            handler = AsyncMock()
            client.set_tool_call_handler(handler)
            assert client._tool_call_handler is handler

    def test_set_tool_call_cancelled_handler(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            handler = AsyncMock()
            client.set_tool_call_cancelled_handler(handler)
            assert client._tool_call_cancelled_handler is handler

    def test_set_block_interceptor(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            interceptor = AsyncMock()
            client.set_block_interceptor(interceptor)
            assert client._block_interceptor is interceptor


class TestER2ClientConnect:
    """Test connection lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_sets_session(self, er2_config):
        with patch("aether.er2.client.genai") as mock_genai:
            mock_session = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)

            mock_client_instance = MagicMock()
            mock_client_instance.aio.live.connect = MagicMock(return_value=mock_ctx)
            mock_genai.Client.return_value = mock_client_instance

            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            await client.connect()

            assert client.session is mock_session
            assert client._running is True

    @pytest.mark.asyncio
    async def test_close_clears_session(self, er2_config):
        with patch("aether.er2.client.genai") as mock_genai:
            mock_session = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)

            mock_client_instance = MagicMock()
            mock_client_instance.aio.live.connect = MagicMock(return_value=mock_ctx)
            mock_genai.Client.return_value = mock_client_instance

            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            await client.connect()
            await client.close()

            assert client.session is None
            assert client._running is False


class TestER2ClientSend:
    """Test send methods when session is active."""

    @pytest.mark.asyncio
    async def test_send_audio_with_session(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            client.session = AsyncMock()
            client.session.send_realtime_input = AsyncMock()

            await client.send_audio(b"\x00\x01\x02\x03")
            client.session.send_realtime_input.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_audio_no_session_warns(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            # No session set
            await client.send_audio(b"\x00\x01")
            # Should not raise, just log warning

    @pytest.mark.asyncio
    async def test_send_text(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            client.session = AsyncMock()
            client.session.send_client_content = AsyncMock()

            await client.send_text("hello")
            client.session.send_client_content.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_text_no_session(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            await client.send_text("hello")
            # Should not raise

    @pytest.mark.asyncio
    async def test_send_tool_response(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            client.session = AsyncMock()
            client.session.send_tool_response = AsyncMock()

            await client.send_tool_response([{"id": "1", "response": {}}])
            client.session.send_tool_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_heartbeat(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            client.session = AsyncMock()
            client.session.send_realtime_input = AsyncMock()

            await client.send_heartbeat("[WORLD STATE]")
            client.session.send_realtime_input.assert_called_once_with(text="[WORLD STATE]")


class TestER2ClientReconnect:
    """Test reconnection logic."""

    @pytest.mark.asyncio
    async def test_reconnect_creates_new_session(self, er2_config):
        with patch("aether.er2.client.genai") as mock_genai:
            mock_session_1 = AsyncMock()
            mock_session_2 = AsyncMock()
            mock_ctx_1 = AsyncMock()
            mock_ctx_1.__aenter__ = AsyncMock(return_value=mock_session_1)
            mock_ctx_1.__aexit__ = AsyncMock(return_value=False)
            mock_ctx_2 = AsyncMock()
            mock_ctx_2.__aenter__ = AsyncMock(return_value=mock_session_2)
            mock_ctx_2.__aexit__ = AsyncMock(return_value=False)

            call_count = 0

            def make_connect(**kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return mock_ctx_1
                return mock_ctx_2

            mock_client_instance = MagicMock()
            mock_client_instance.aio.live.connect = MagicMock(side_effect=make_connect)
            mock_genai.Client.return_value = mock_client_instance

            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            await client.connect()
            assert client.session is mock_session_1

            await client.reconnect()
            assert client.session is mock_session_2
            assert client._running is True


class TestBuildLiveConfig:
    """Test _build_live_config output."""

    def test_config_contains_required_keys(self, er2_config):
        with patch("aether.er2.client.genai"):
            from aether.er2.client import ER2Client
            client = ER2Client(er2_config)
            config = client._build_live_config()

            assert "response_modalities" in config
            assert "system_instruction" in config
            assert "tools" in config
            assert config["response_modalities"] == ["TEXT"]
