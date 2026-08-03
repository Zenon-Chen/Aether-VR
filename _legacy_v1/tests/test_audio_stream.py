"""Tests for aether.io.audio_stream — shared TTS→playback stream."""

import asyncio
import pytest

from aether.io.audio_stream import AudioStream


class TestAudioStream:
    """Test AudioStream producer/consumer contract."""

    @pytest.mark.asyncio
    async def test_produce_consume(self):
        stream = AudioStream()
        await stream.produce(b"pcm-data")
        result = await stream.consume(timeout=0.05)
        assert result == b"pcm-data"

    @pytest.mark.asyncio
    async def test_consume_timeout_returns_none(self):
        stream = AudioStream()
        result = await stream.consume(timeout=0.05)
        assert result is None

    @pytest.mark.asyncio
    async def test_fifo_order(self):
        stream = AudioStream()
        await stream.produce(b"a")
        await stream.produce(b"b")
        assert await stream.consume(timeout=0.05) == b"a"
        assert await stream.consume(timeout=0.05) == b"b"

    def test_clear(self):
        stream = AudioStream()
        stream._queue.put_nowait(b"a")
        stream._queue.put_nowait(b"b")
        stream.clear()
        assert stream.is_empty

    @pytest.mark.asyncio
    async def test_tts_and_pipeline_share_same_queue(self):
        """TTSEngine 与 AudioPipeline 共享实例时数据直通"""
        from aether.config import TTSConfig, AudioConfig
        from aether.speech.tts_engine import TTSEngine
        from aether.io.audio_pipeline import AudioPipeline

        stream = AudioStream()
        config = TTSConfig(api_key="test")
        audio_config = AudioConfig()

        with __import__("unittest.mock").mock.patch(
            "aether.speech.tts_engine.genai"
        ), __import__("unittest.mock").mock.patch(
            "aether.io.audio_pipeline.sd"
        ):
            tts = TTSEngine(config, stream)
            audio = AudioPipeline(audio_config, stream)

        # TTS 生产 → 管道消费（无需桥接循环）
        await tts._stream.produce(b"hello")
        pcm = await audio._stream.consume(timeout=0.05)
        assert pcm == b"hello"
        # 两端的 audio_queue / playback_queue 指向同一底层队列
        assert tts.audio_queue is audio._stream.queue
