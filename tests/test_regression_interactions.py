"""Cross-module regressions found during the comprehensive async review."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from aether.config import ContextConfig, DARTConfig, ER2Config, OSCConfig, TTSConfig
from aether.context.block_store import ContextBlock
from aether.context.evictor import ContextEvictor
from aether.context.memory import InMemoryBlockRepository
from aether.motion.dart_client import DARTClient
from aether.motion.osc_sender import OSCSender
from aether.tool_dispatcher import MotionState, ToolDispatcher


@pytest.mark.asyncio
async def test_embedding_failure_does_not_poison_block_or_stop_batch():
    repo = InMemoryBlockRepository(embedding_dim=2)
    failed = ContextBlock(content="failed")
    good = ContextBlock(content="good")
    repo.add_block(failed)
    repo.add_block(good)

    async def embed(text):
        if text == "failed":
            raise RuntimeError("temporary API failure")
        return np.array([1.0, 0.0], dtype=np.float32)

    assert await repo.process_pending(embed) == 1
    assert failed.embedding is None
    assert good.embedding is not None


def test_replacing_same_block_id_keeps_token_count_correct():
    repo = InMemoryBlockRepository()
    repo.add_block(ContextBlock(block_id="same", content="old", token_estimate=10))
    repo.add_block(ContextBlock(block_id="same", content="new", token_estimate=3))
    assert repo.total_active_tokens == 3


@pytest.mark.asyncio
async def test_evictor_can_adjust_tokens_through_repository_protocol():
    repo = InMemoryBlockRepository()
    block = ContextBlock(content="long text", token_estimate=100, timestamp=0, importance_score=0.9)
    repo.add_block(block)
    cfg = ContextConfig(max_active_tokens=1)
    with patch("aether.context.evictor.genai") as genai:
        response = SimpleNamespace(text="short")
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(return_value=response)
        genai.Client.return_value = client
        evictor = ContextEvictor(cfg, repo, "test")
        await evictor._summarize_blocks([block])
    assert repo.total_active_tokens == block.token_estimate


@pytest.mark.asyncio
async def test_motion_state_remains_owned_by_newer_concurrent_motion():
    dispatcher = ToolDispatcher()
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    calls = 0

    async def handler(action, duration):
        nonlocal calls
        calls += 1
        release = first_release if calls == 1 else second_release
        await release.wait()
        return {"num_frames": 1}

    dispatcher.set_motion_handler(handler)
    first = asyncio.create_task(dispatcher._handle_motion({"action": "first"}))
    await asyncio.sleep(0)
    second = asyncio.create_task(dispatcher._handle_motion({"action": "second"}))
    await asyncio.sleep(0)
    first_release.set()
    await first
    assert dispatcher.state.motion_state == MotionState.GENERATING
    second_release.set()
    await second
    assert dispatcher.state.motion_state == MotionState.IDLE


@pytest.mark.asyncio
async def test_osc_play_motion_awaits_actual_playback_completion():
    with patch("aether.motion.osc_sender.udp_client"):
        sender = OSCSender(OSCConfig())
    release = asyncio.Event()
    async def wait_for_release(data):
        await release.wait()
    sender._motion_loop = AsyncMock(side_effect=wait_for_release)
    task = asyncio.create_task(sender.play_motion({"poses": [[0.0] * 165]}))
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    await task


@pytest.mark.asyncio
async def test_dart_rejects_more_than_max_segments_before_http():
    client = DARTClient(DARTConfig())
    client._client = AsyncMock()
    with pytest.raises(ValueError, match="max_segments"):
        await client.generate("dance*31")
    client._client.post.assert_not_called()


@pytest.mark.asyncio
async def test_er2_connect_failure_cleans_exit_stack():
    with patch("aether.er2.client.genai") as genai:
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("connect failed"))
        instance = MagicMock()
        instance.aio.live.connect.return_value = ctx
        genai.Client.return_value = instance
        from aether.er2.client import ER2Client
        client = ER2Client(ER2Config(api_key="test"))
        with pytest.raises(RuntimeError, match="connect failed"):
            await client.connect()
        assert client._exit_stack is None
        assert client.session is None


@pytest.mark.asyncio
async def test_audio_input_failure_does_not_disable_tts_output():
    from aether.config import AudioConfig
    from aether.io.audio_pipeline import AudioPipeline
    from aether.io import audio_pipeline as module

    pipeline = AudioPipeline(AudioConfig())
    pipeline._no_audio = False
    output_stream = MagicMock()
    output_stream.start = MagicMock()
    with patch.object(module.sd, "RawInputStream", side_effect=RuntimeError("no mic")), \
         patch.object(module.sd, "RawOutputStream", return_value=output_stream):
        await pipeline.start()
        try:
            assert pipeline._input_stream is None
            assert pipeline._output_stream is output_stream
            assert pipeline._output_task is not None
            assert pipeline.no_audio is False
        finally:
            await pipeline.stop()
