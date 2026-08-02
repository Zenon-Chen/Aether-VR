"""音频流 — TTS 生产端与播放消费端之间的显式数据通路

TTSEngine（生产者）与 AudioPipeline（消费者）共享同一个 AudioStream 实例，
数据流经单一队列传输，不再需要主循环桥接搬运。

典型装配（composition root）::

    stream = AudioStream()
    tts = TTSEngine(config.tts, stream)
    audio = AudioPipeline(config.audio, stream)
"""

import asyncio
from typing import Optional


class AudioStream:
    """TTS → 播放 的音频流（单队列，producer/consumer 两端可见）"""

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()

    # ── 生产者接口（TTSEngine 使用）────────────────

    async def produce(self, pcm_data: bytes) -> None:
        """放入一段 PCM 音频（24kHz, 16-bit, mono）"""
        await self._queue.put(pcm_data)

    # ── 消费者接口（AudioPipeline 使用）────────────

    async def consume(self, timeout: float = 0.1) -> Optional[bytes]:
        """取出一段音频；超时返回 None（用于可中断轮询）"""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # ── 打断管理 ───────────────────────────────────

    def clear(self) -> None:
        """清空队列（打断场景）"""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    @property
    def queue(self) -> asyncio.Queue:
        """底层队列（兼容/调试用途）"""
        return self._queue

    @property
    def is_empty(self) -> bool:
        return self._queue.empty()
