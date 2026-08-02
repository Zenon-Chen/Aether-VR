"""TTS 分段引擎

接收 execute_tts(text, emotion, speech_rate) 调用：
1. 标点驱动文本切片（按句号/逗号分割，每段 15-30 字）
2. 情感标签映射（emotion → TTS 音频标签）
3. 分段并发调用 gemini-3.1-flash-tts-preview
4. 音频段入 asyncio.Queue 播放队列
"""

import asyncio
import re
import logging
import wave
from typing import Optional, Literal

from google import genai
from google.genai import types

from ..config import TTSConfig
from ..io.audio_stream import AudioStream
from ..protocols import TTSPort
from .base import BaseTTSEngine

logger = logging.getLogger(__name__)


class _StaleGeneration(Exception):
    """当 synthesize 代际已过期（被用户打断）时抛出，用于丢弃飞行中的 API 结果"""


# 情感 → TTS 标签映射（20+ 种）
EMOTION_TO_TAGS = {
    "neutral": "[neutral]",
    "happy": "[happy, upbeat]",
    "sad": "[sad, slow]",
    "angry": "[angry]",
    "amused": "[amused, lighthearted]",
    "curious": "[curiosity, interested]",
    "worried": "[worried, uncertain]",
    "enthusiastic": "[enthusiasm, energetic]",
    "sarcastic": "[sarcasm]",
    "thinking": "[thoughtful, slow]",
    "apologetic": "[apologetic, soft]",
    "surprised": "[surprised, gasp]",
    "calm": "[calm, peaceful]",
    "excited": "[excited, fast]",
    "tired": "[tired, slow]",
    "confident": "[confident, assertive]",
    "nervous": "[nervous, hesitant]",
    "playful": "[playful, fun]",
    "serious": "[serious, formal]",
    "warm": "[warm, gentle]",
}

# 语速 → TTS 标签
RATE_TO_TAG = {
    "slow": "[slow]",
    "normal": "",
    "fast": "[fast]",
}

# 分割模式预设
SPLIT_MODES = {
    "english": {
        "sentence_end": r"([.!?])",
        "clause_sep": r"([,;])",
    },
    "chinese": {
        "sentence_end": r"([。！？])",
        "clause_sep": r"([，；])",
    },
}


class TTSEngine(BaseTTSEngine, TTSPort):
    """TTS 分段合成引擎（Gemini TTS API）"""

    def __init__(self, config: TTSConfig, stream: Optional[AudioStream] = None):
        self.config = config
        self.client = genai.Client(api_key=config.api_key)
        self.model = config.model  # gemini-3.1-flash-tts-preview
        self.voice_name = config.voice_name  # Kore
        self.max_segment_chars = config.max_segment_chars  # 30

        # 音频流：生产端（AudioPipeline 作为消费者共享同一实例）
        # 默认自建；composition root 可注入共享 stream 消除桥接循环
        self._stream = stream if stream is not None else AudioStream()
        self._playing = False

        # 代际 ID，用于打断时使飞行中的 API 调用失效
        self._generation_id: int = 0

    @property
    def audio_queue(self) -> asyncio.Queue:
        """播放队列（兼容别名 → stream 底层队列）"""
        return self._stream.queue

    # ── 主入口 ────────────────────────────────────

    async def synthesize(self, text: str, emotion: str = "neutral",
                         speech_rate: str = "normal") -> str:
        """
        合成语音并加入播放队列。

        这是 ToolDispatcher 调用的入口方法。
        签名: async (text, emotion, speech_rate) -> status_str

        Args:
            text: 要说的文本
            emotion: 情绪状态
            speech_rate: 语速

        Returns:
            状态字符串
        """
        logger.info(f"[TTS] synthesize: text='{text}', emotion={emotion}, rate={speech_rate}")

        # 1. 文本切片（默认中文标点，系统提示词要求模型输出中文）
        segments = self._split_text(text, split_on="chinese")
        logger.info(f"[TTS] Split into {len(segments)} segments")

        # 2. 为每段构建带标签的 TTS 文本
        tagged_segments = []
        for i, segment in enumerate(segments):
            tagged = self._build_tagged_text(segment, emotion, speech_rate, is_first=(i == 0))
            tagged_segments.append(tagged)

        # 递增代际 ID 并捕获，用于打断时丢弃旧结果
        self._generation_id += 1
        current_gen = self._generation_id

        # 3. 并发调用 TTS（但按序入队）
        tasks = []
        for tagged in tagged_segments:
            task = asyncio.create_task(self._call_tts_api(tagged, current_gen))
            tasks.append(task)

        # 按顺序等待并加入播放流
        for i, task in enumerate(tasks):
            try:
                pcm_data = await task
                if pcm_data is not None:
                    await self._stream.produce(pcm_data)
                    logger.debug(f"[TTS] Segment {i+1}/{len(tasks)} queued")
            except _StaleGeneration:
                logger.debug(f"[TTS] Segment {i+1}/{len(tasks)} discarded (stale generation)")
                break
            except Exception as e:
                logger.error(f"[TTS] Segment {i+1} failed: {e}")

        return f"synthesized {len(segments)} segments"

    # ── 文本切片 ──────────────────────────────────

    def _split_text(self, text: str,
                    split_on: Literal["english", "chinese"] = "english") -> list[str]:
        """
        标点驱动文本切片。

        Args:
            text: 待分割文本
            split_on: 分割模式，"english"（默认，TTS语音）或 "chinese"

        分割规则：
        1. 先按句子结束标点 (.!? 或 。！？) 分割
        2. 如果子句超过 max_segment_chars，再按从句标点 (,; 或 ，；) 分割
        3. 保证每段 3-max_segment_chars 字
        4. 太短的段（<3字）合并到上一段
        """
        mode = SPLIT_MODES.get(split_on, SPLIT_MODES["english"])
        sentence_end = mode["sentence_end"]
        clause_sep = mode["clause_sep"]

        text = text.strip()
        if not text:
            return []

        # 第一轮：按句子结束标点分割
        sentences = re.split(sentence_end, text)

        # 将标点重新附加到前面的句子
        merged_sentences: list[str] = []
        buf = ""
        for part in sentences:
            buf += part
            if re.match(sentence_end, part):
                merged_sentences.append(buf.strip())
                buf = ""
        if buf.strip():
            merged_sentences.append(buf.strip())

        # 第二轮：对超长句子按从句标点再分
        segments: list[str] = []
        for sentence in merged_sentences:
            if len(sentence) <= self.max_segment_chars:
                segments.append(sentence)
            else:
                # 按从句标点分割
                sub_parts = re.split(clause_sep, sentence)
                sub_buf = ""
                sub_segments: list[str] = []
                for sp in sub_parts:
                    sub_buf += sp
                    if re.match(clause_sep, sp):
                        sub_segments.append(sub_buf.strip())
                        sub_buf = ""
                if sub_buf.strip():
                    sub_segments.append(sub_buf.strip())

                # 如果子段仍然超长，强制按 max_segment_chars 切分
                for ss in sub_segments:
                    while len(ss) > self.max_segment_chars:
                        segments.append(ss[:self.max_segment_chars])
                        ss = ss[self.max_segment_chars:]
                    if ss:
                        segments.append(ss)

        # 第三轮：合并过短的段（<3字）到上一段
        final_segments: list[str] = []
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            if final_segments and len(seg) < 3:
                # 合并到上一段
                final_segments[-1] = final_segments[-1] + seg
            else:
                final_segments.append(seg)

        # 如果合并后某段超长，再切一次
        result: list[str] = []
        for seg in final_segments:
            if len(seg) <= self.max_segment_chars:
                result.append(seg)
            else:
                while len(seg) > self.max_segment_chars:
                    result.append(seg[:self.max_segment_chars])
                    seg = seg[self.max_segment_chars:]
                if seg:
                    result.append(seg)

        return result if result else [text]

    # ── 标签构建 ──────────────────────────────────

    def _build_tagged_text(self, text: str, emotion: str,
                           speech_rate: str, is_first: bool = False) -> str:
        """
        为文本段添加 TTS 情感标签。

        示例输出: "[happy, upbeat] okay, I'm coming over"
        """
        tags = []

        # 添加情感标签
        emotion_tag = EMOTION_TO_TAGS.get(emotion, EMOTION_TO_TAGS["neutral"])
        tags.append(emotion_tag)

        # 添加语速标签
        rate_tag = RATE_TO_TAG.get(speech_rate, "")
        if rate_tag:
            tags.append(rate_tag)

        # 第一段可以加个思考标记（如果是 thinking 情绪）
        if is_first and emotion == "thinking":
            tags.append("[short pause]")

        tag_str = " ".join(t for t in tags if t)
        return f"{tag_str} {text}"

    # ── API 调用 ──────────────────────────────────

    async def _call_tts_api(self, tagged_text: str,
                            generation_id: int = 0) -> Optional[bytes]:
        """
        调用 Gemini TTS API。

        Args:
            tagged_text: 带情感标签的文本
            generation_id: 本次合成代际 ID，用于打断检测

        Returns:
            PCM 音频数据 (24kHz, 16-bit, mono) 或 None

        Raises:
            _StaleGeneration: 当 generation_id 已过期（被打断）
        """
        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=tagged_text,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=self.voice_name
                            )
                        )
                    )
                )
            )

            # 打断检测：如果代际 ID 已变化，丢弃结果
            if generation_id != self._generation_id:
                raise _StaleGeneration(
                    f"gen {generation_id} != current {self._generation_id}"
                )

            # 提取 PCM 音频数据
            audio_data = response.candidates[0].content.parts[0].inline_data.data
            return audio_data

        except _StaleGeneration:
            raise
        except Exception as e:
            logger.error(f"[TTS] API call failed for '{tagged_text[:30]}...': {e}")
            return None

    # ── 队列管理 ──────────────────────────────────

    def clear_queue(self):
        """清空播放队列（用于打断场景），同时递增代际 ID 使飞行中的 API 调用失效"""
        self._generation_id += 1
        self._stream.clear()
        logger.info(f"[TTS] Queue cleared (generation -> {self._generation_id})")

    async def get_next_audio(self) -> Optional[bytes]:
        """从播放流获取下一段音频（供 audio_pipeline 使用）"""
        return await self._stream.consume()

    # ── 调试工具 ──────────────────────────────────

    def save_wav(self, filename: str, pcm_data: bytes,
                 sample_rate: int = 24000, channels: int = 1, sample_width: int = 2):
        """保存 PCM 数据为 WAV 文件（调试用）"""
        with wave.open(filename, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        logger.info(f"[TTS] Saved WAV: {filename}")
