"""Chatterbox 语音克隆引擎

使用 Resemble AI 的 Chatterbox 模型实现：
- 语音克隆（Voice Cloning）：用几秒参考音频生成相似语音
- 零样本 TTS（Zero-shot TTS）：无需微调即可克隆
- 情感控制：通过 exaggeration 参数控制表达力

参考：https://github.com/resemble-ai/chatterbox
"""

import asyncio
import logging
from typing import Optional
from pathlib import Path

from .base import BaseTTSEngine
from ..protocols import TTSPort

logger = logging.getLogger(__name__)


class ChatterboxEngine(BaseTTSEngine, TTSPort):
    """Chatterbox 语音克隆引擎（扩展功能）"""

    def __init__(self, device: str = "cuda", reference_audio: Optional[str] = None):
        """
        初始化 Chatterbox 引擎。

        Args:
            device: 运行设备 ("cuda" 或 "cpu")
            reference_audio: 参考音频路径（用于语音克隆）
        """
        self.device = device
        self.reference_audio = reference_audio
        self.model = None
        self.audio_queue: asyncio.Queue = asyncio.Queue()
        self._loaded = False

    def load_model(self):
        """加载 Chatterbox 模型（延迟加载）"""
        try:
            import torch
            from chatterbox.tts import ChatterboxTTS

            self.model = ChatterboxTTS.from_pretrained(device=self.device)
            self._loaded = True
            logger.info(f"[Chatterbox] Model loaded on {self.device}")
        except ImportError:
            logger.warning("[Chatterbox] chatterbox package not installed, skipping")
            self._loaded = False
        except Exception as e:
            logger.error(f"[Chatterbox] Failed to load model: {e}")
            self._loaded = False

    async def synthesize(self, text: str, emotion: str = "neutral",
                         speech_rate: str = "normal") -> str:
        """
        使用 Chatterbox 合成语音。

        Args:
            text: 要说的文本
            emotion: 情绪状态（通过 exaggeration 参数映射）
            speech_rate: 语速

        Returns:
            状态字符串
        """
        if not self._loaded:
            logger.warning("[Chatterbox] Model not loaded, attempting lazy load")
            self.load_model()
            if not self._loaded:
                return "error: model not available"

        logger.info(f"[Chatterbox] synthesize: text='{text}', emotion={emotion}")

        # 情感 → exaggeration 映射
        exaggeration = self._emotion_to_exaggeration(emotion)

        # 在线程池中运行同步的模型推理
        try:
            loop = asyncio.get_event_loop()
            wav = await loop.run_in_executor(
                None,
                lambda: self.model.generate(
                    text=text,
                    audio_prompt=self._load_reference_audio(),
                    exaggeration=exaggeration,
                )
            )

            # 转换为 PCM bytes
            pcm_data = self._wav_to_pcm(wav)
            await self.audio_queue.put(pcm_data)
            return "synthesized 1 segment (cloned voice)"

        except Exception as e:
            logger.error(f"[Chatterbox] Synthesis failed: {e}")
            return f"error: {e}"

    def _emotion_to_exaggeration(self, emotion: str) -> float:
        """将情感映射到 exaggeration 参数 (0.0 - 1.0)"""
        mapping = {
            "neutral": 0.5,
            "happy": 0.7,
            "sad": 0.3,
            "angry": 0.8,
            "excited": 0.9,
            "calm": 0.2,
            "surprised": 0.8,
            "thinking": 0.4,
        }
        return mapping.get(emotion, 0.5)

    def _load_reference_audio(self):
        """加载参考音频（用于语音克隆）"""
        if not self.reference_audio:
            return None
        path = Path(self.reference_audio)
        if not path.exists():
            logger.warning(f"[Chatterbox] Reference audio not found: {path}")
            return None
        # 返回音频路径或加载后的音频数据
        return str(path)

    def _wav_to_pcm(self, wav, sample_rate: int = 24000) -> bytes:
        """将 WAV 格式转换为 PCM bytes"""
        import io
        import wave

        if isinstance(wav, bytes):
            # 如果已经是 bytes，尝试解析为 WAV
            with io.BytesIO(wav) as buf:
                with wave.open(buf, "rb") as wf:
                    return wf.readframes(wf.getnframes())
        return wav  # 假设已经是 PCM

    def clear_queue(self):
        """清空播放队列"""
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        logger.info("[Chatterbox] Queue cleared")

    async def get_next_audio(self) -> Optional[bytes]:
        """从播放队列获取下一段音频"""
        try:
            return await asyncio.wait_for(self.audio_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None

    def set_reference_audio(self, audio_path: str):
        """动态设置参考音频（切换克隆目标）"""
        self.reference_audio = audio_path
        logger.info(f"[Chatterbox] Reference audio set to: {audio_path}")
