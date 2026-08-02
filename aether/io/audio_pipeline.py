"""音频输入/输出管道

输入端：
- 麦克风采集（16kHz, 16-bit PCM, mono）
- Silero VAD 本地语音活动检测（不可用时退回能量阈值检测）
- 通过 ER2 的 send_realtime_input 发送音频流

输出端：
- 从 TTS 引擎的 audio_queue 获取 PCM 音频
- sounddevice 回调模式播放（24kHz, 16-bit, mono）
- 队列空时播放静音

打断处理：
- VAD 检测到用户插话 → 清空播放队列 + 播放垫音
- 垫音掩盖 TTS 切换延迟
"""

import asyncio
import logging
import threading
import wave
from pathlib import Path
from typing import Optional, Callable, Awaitable

import numpy as np

from ..config import AudioConfig
from ..io.audio_stream import AudioStream
from ..protocols import AudioPort

logger = logging.getLogger(__name__)

# sounddevice 可选导入
try:
    import sounddevice as sd
    _HAS_SOUNDDEVICE = True
except ImportError:
    sd = None
    _HAS_SOUNDDEVICE = False
    logger.warning("[Audio] sounddevice not installed, audio I/O disabled")


class AudioPipeline(AudioPort):
    """音频输入/输出管道

    管理麦克风采集、VAD 检测、音频播放，并与 ER2/TTS 引擎对接。
    """

    # 每个麦克风块的采样数（30ms @ 16kHz = 480 samples）
    _INPUT_BLOCK_SAMPLES = 480
    # 输出回调每次请求的帧数
    _OUTPUT_BLOCKSIZE = 480

    def __init__(self, config: AudioConfig, stream: Optional[AudioStream] = None):
        self.config = config
        self.input_sample_rate: int = config.input_sample_rate   # 16000
        self.output_sample_rate: int = config.output_sample_rate  # 24000
        self.channels: int = config.channels                      # 1
        self.sample_width: int = config.sample_width              # 2 (16-bit)
        self.vad_threshold: float = config.vad_threshold          # 0.5

        # 播放流：消费者端（TTSEngine 作为生产者共享同一实例）
        # 默认自建；composition root 可注入共享 stream 消除桥接循环
        self._stream = stream if stream is not None else AudioStream()

        # sounddevice 流
        self._input_stream: Optional["sd.RawInputStream"] = None
        self._output_stream: Optional["sd.RawOutputStream"] = None

        # VAD 模型（Silero，可选）
        self._vad_model = None
        self._vad_utils = None
        self._use_silero_vad = False

        # 回调
        self._on_speech_start: Optional[Callable] = None
        self._on_speech_end: Optional[Callable] = None
        self._on_audio_chunk: Optional[Callable[[bytes], Awaitable]] = None

        # 播放队列来自共享 AudioStream（线程安全：sounddevice 回调在独立线程）
        self._is_playing = False
        self._is_speaking = False  # 用户是否在说话

        # 垫音音频（预加载到内存，PCM int16 numpy arrays）
        self._filler_sounds: list[np.ndarray] = []

        # 运行标志
        self._running = False
        self._input_task: Optional[asyncio.Task] = None
        self._output_task: Optional[asyncio.Task] = None

        # 输出端线程缓冲（sounddevice 回调 → asyncio 桥接）
        self._output_buffer = bytearray()
        self._output_lock = threading.Lock()

        # VAD 状态
        self._speech_active = False
        self._speech_silence_frames = 0
        self._speech_active_frames = 0
        # 连续静音帧数阈值（约 500ms / 30ms ≈ 17 帧）
        self._silence_end_threshold = 17
        # 连续语音帧数阈值（约 100ms / 30ms ≈ 3 帧）
        self._speech_start_threshold = 3

        # 能量检测回退阈值（int16 RMS）
        self._energy_threshold = 300

        # no-audio 模式
        self._no_audio = not _HAS_SOUNDDEVICE

    # ── 回调设置 ──────────────────────────────────

    def set_callbacks(
        self,
        on_speech_start: Optional[Callable] = None,
        on_speech_end: Optional[Callable] = None,
        on_audio_chunk: Optional[Callable[[bytes], Awaitable]] = None,
    ):
        """设置回调函数

        Args:
            on_speech_start: VAD 检测到用户开始说话时调用（可用于打断）
            on_speech_end: VAD 检测到用户停止说话时调用
            on_audio_chunk: 每个麦克风音频块回调（用于发送到 ER2）
        """
        self._on_speech_start = on_speech_start
        self._on_speech_end = on_speech_end
        self._on_audio_chunk = on_audio_chunk

    # ── VAD 初始化 ────────────────────────────────

    def _init_vad(self):
        """初始化 VAD 模型（优先 Silero，回退能量检测）"""
        try:
            import torch
            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                trust_repo=True,
            )
            self._vad_model = model
            self._vad_utils = utils
            self._use_silero_vad = True
            logger.info("[Audio] Silero VAD loaded successfully")
        except Exception as exc:
            logger.warning(
                "[Audio] Silero VAD unavailable (%s), falling back to energy detection",
                exc,
            )
            self._use_silero_vad = False

    def _detect_speech(self, pcm_int16: np.ndarray) -> float:
        """对一段 PCM int16 音频进行语音活动检测

        Returns:
            语音概率 0.0 ~ 1.0
        """
        if self._use_silero_vad and self._vad_model is not None:
            try:
                import torch
                # Silero 需要 float32, 归一化到 [-1, 1]
                tensor = torch.from_numpy(pcm_int16.astype(np.float32) / 32768.0)
                confidence = self._vad_model(tensor, self.input_sample_rate).item()
                return float(confidence)
            except Exception:
                pass

        # 回退：简单 RMS 能量检测
        rms = np.sqrt(np.mean(pcm_int16.astype(np.float64) ** 2))
        # 将 RMS 映射到 0~1 概率（粗略）
        return min(rms / 3000.0, 1.0)

    # ── 麦克风输入 ────────────────────────────────

    async def start_input(self):
        """启动麦克风输入流"""
        if self._no_audio:
            logger.warning("[Audio] No-audio mode, skipping input start")
            return

        self._init_vad()

        try:
            self._input_stream = sd.RawInputStream(
                samplerate=self.input_sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=self._INPUT_BLOCK_SAMPLES,
            )
            self._input_stream.start()
            logger.info(
                "[Audio] Input stream opened: %d Hz, %d ch, blocksize=%d",
                self.input_sample_rate, self.channels, self._INPUT_BLOCK_SAMPLES,
            )
        except Exception as exc:
            logger.error("[Audio] Failed to open input stream: %s", exc)
            self._no_audio = True
            return

    async def stop_input(self):
        """停止麦克风输入"""
        if self._input_stream is not None:
            try:
                self._input_stream.stop()
                self._input_stream.close()
            except Exception as exc:
                logger.warning("[Audio] Error closing input stream: %s", exc)
            finally:
                self._input_stream = None

    async def input_loop(self):
        """麦克风输入主循环 — 在后台 asyncio Task 中运行

        持续读取麦克风音频块：
        1. VAD 检测语音活动
        2. 触发 speech_start / speech_end 回调
        3. 通过 on_audio_chunk 回调发送音频到 ER2
        """
        if self._no_audio or self._input_stream is None:
            logger.info("[Audio] Input loop skipped (no-audio mode)")
            return

        loop = asyncio.get_running_loop()
        logger.info("[Audio] Input loop started")

        while self._running:
            try:
                # sounddevice read 是阻塞的，放到线程池
                data, overflow = await loop.run_in_executor(
                    None, self._input_stream.read, self._INPUT_BLOCK_SAMPLES
                )
                if overflow:
                    logger.debug("[Audio] Input buffer overflow")

                pcm_bytes = bytes(data)
                pcm_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)

                # VAD 检测
                speech_prob = self._detect_speech(pcm_int16)
                self._process_vad_result(speech_prob)

                # 发送音频块到 ER2
                if self._on_audio_chunk is not None:
                    try:
                        await self._on_audio_chunk(pcm_bytes)
                    except Exception as exc:
                        logger.warning("[Audio] on_audio_chunk callback error: %s", exc)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[Audio] Input loop error: %s", exc)
                await asyncio.sleep(0.1)

        logger.info("[Audio] Input loop stopped")

    def _process_vad_result(self, speech_prob: float):
        """处理 VAD 结果，维护语音状态机"""
        is_speech = speech_prob >= self.vad_threshold

        if is_speech:
            self._speech_silence_frames = 0
            self._speech_active_frames += 1

            if not self._speech_active and self._speech_active_frames >= self._speech_start_threshold:
                self._speech_active = True
                self._is_speaking = True
                logger.info("[Audio] Speech started (prob=%.2f)", speech_prob)
                if self._on_speech_start is not None:
                    try:
                        self._on_speech_start()
                    except Exception as exc:
                        logger.warning("[Audio] on_speech_start callback error: %s", exc)
        else:
            self._speech_active_frames = 0
            self._speech_silence_frames += 1

            if self._speech_active and self._speech_silence_frames >= self._silence_end_threshold:
                self._speech_active = False
                self._is_speaking = False
                logger.info("[Audio] Speech ended")
                if self._on_speech_end is not None:
                    try:
                        self._on_speech_end()
                    except Exception as exc:
                        logger.warning("[Audio] on_speech_end callback error: %s", exc)

    # ── 音频输出 ──────────────────────────────────

    async def start_output(self):
        """启动音频输出流（sounddevice 回调模式）"""
        if self._no_audio:
            logger.warning("[Audio] No-audio mode, skipping output start")
            return

        try:
            self._output_stream = sd.RawOutputStream(
                samplerate=self.output_sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=self._OUTPUT_BLOCKSIZE,
                callback=self._output_callback,
            )
            self._output_stream.start()
            self._is_playing = True
            logger.info(
                "[Audio] Output stream opened: %d Hz, %d ch, blocksize=%d",
                self.output_sample_rate, self.channels, self._OUTPUT_BLOCKSIZE,
            )
        except Exception as exc:
            logger.error("[Audio] Failed to open output stream: %s", exc)
            self._no_audio = True

    def _output_callback(self, outdata: "np.ndarray", frames: int,
                         time_info, status):
        """sounddevice 输出回调（在独立线程中运行）

        从内部缓冲区取数据，不够则填静音。
        """
        if status:
            logger.debug("[Audio] Output callback status: %s", status)

        needed_bytes = frames * self.channels * self.sample_width
        with self._output_lock:
            if len(self._output_buffer) >= needed_bytes:
                chunk = bytes(self._output_buffer[:needed_bytes])
                del self._output_buffer[:needed_bytes]
            else:
                # 缓冲区不够，全部填静音
                chunk = b"\x00" * needed_bytes
                self._output_buffer.clear()

        outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(
            (frames, self.channels)
        )

    async def output_loop(self):
        """音频输出主循环 — 从共享播放流获取音频，写入输出缓冲

        在后台 asyncio Task 中运行。持续从 AudioStream 获取 PCM 数据，
        写入 _output_buffer 供 sounddevice 回调消费。
        """
        if self._no_audio or self._output_stream is None:
            logger.info("[Audio] Output loop skipped (no-audio mode)")
            return

        logger.info("[Audio] Output loop started")

        while self._running:
            try:
                pcm_data = await self._stream.consume(timeout=0.1)
                if pcm_data:
                    with self._output_lock:
                        self._output_buffer.extend(pcm_data)
            except asyncio.TimeoutError:
                # 队列空，正常（静音由回调处理）
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[Audio] Output loop error: %s", exc)
                await asyncio.sleep(0.05)

        logger.info("[Audio] Output loop stopped")

    # ── 播放控制 ──────────────────────────────────

    async def play_audio(self, pcm_data: bytes):
        """将 PCM 音频数据放入共享播放流

        Args:
            pcm_data: 24kHz, 16-bit, mono PCM 数据
        """
        await self._stream.produce(pcm_data)

    def stop_playback(self):
        """立即停止播放（打断场景）

        清空共享播放流 + 清空输出缓冲。
        """
        # 清空 asyncio 队列
        self._stream.clear()

        # 清空 sounddevice 回调缓冲
        with self._output_lock:
            self._output_buffer.clear()

        logger.info("[Audio] Playback stopped (interrupted)")

    async def play_filler_sound(self):
        """播放垫音（打断时使用）

        从预加载的垫音中随机选一个播放。
        如果没有垫音文件，生成一个短促的柔和提示音。
        """
        filler: Optional[np.ndarray] = None

        if self._filler_sounds:
            import random
            filler = random.choice(self._filler_sounds)
        else:
            # 生成一个 ~150ms 的柔和提示音（正弦波淡入淡出）
            filler = self._generate_filler_tone()

        if filler is not None and not self._no_audio:
            pcm_bytes = filler.astype(np.int16).tobytes()
            with self._output_lock:
                self._output_buffer.extend(pcm_bytes)
            logger.debug("[Audio] Filler sound played (%d samples)", len(filler))

    def _generate_filler_tone(
        self, duration_ms: int = 150, frequency: float = 220.0
    ) -> np.ndarray:
        """生成一个短促正弦波垫音（淡入淡出避免爆音）

        Args:
            duration_ms: 持续时间（毫秒）
            frequency: 频率（Hz）

        Returns:
            int16 numpy 数组
        """
        num_samples = int(self.output_sample_rate * duration_ms / 1000)
        t = np.linspace(0, duration_ms / 1000, num_samples, endpoint=False)
        tone = np.sin(2 * np.pi * frequency * t)

        # 汉宁窗淡入淡出
        window = np.hanning(num_samples)
        tone = tone * window

        # 缩放到 int16 范围（低音量）
        tone = tone * 4000  # 约 -18 dB
        return tone.astype(np.int16)

    # ── 垫音加载 ──────────────────────────────────

    def load_filler_sounds(self, sound_dir: str = "sounds/fillers"):
        """从目录加载垫音 .wav 文件

        Args:
            sound_dir: 垫音目录路径，包含 .wav 文件
        """
        path = Path(sound_dir)
        if not path.exists():
            logger.info(
                "[Audio] Filler sound directory '%s' not found, "
                "will use generated tones",
                sound_dir,
            )
            return

        loaded = 0
        for wav_file in sorted(path.glob("*.wav")):
            try:
                with wave.open(str(wav_file), "rb") as wf:
                    if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
                        logger.warning(
                            "[Audio] Skipping %s: expected 16-bit mono",
                            wav_file.name,
                        )
                        continue

                    sr = wf.getframerate()
                    frames = wf.readframes(wf.getnframes())
                    pcm = np.frombuffer(frames, dtype=np.int16)

                    # 如果采样率不匹配，简单重采样
                    if sr != self.output_sample_rate:
                        ratio = self.output_sample_rate / sr
                        new_len = int(len(pcm) * ratio)
                        pcm = np.interp(
                            np.linspace(0, len(pcm), new_len),
                            np.arange(len(pcm)),
                            pcm.astype(np.float64),
                        ).astype(np.int16)

                    self._filler_sounds.append(pcm)
                    loaded += 1
            except Exception as exc:
                logger.warning("[Audio] Failed to load filler '%s': %s", wav_file, exc)

        logger.info("[Audio] Loaded %d filler sounds from '%s'", loaded, sound_dir)

    # ── 生命周期 ──────────────────────────────────

    async def start(self):
        """启动音频管道（输入 + 输出 + 后台循环）"""
        if self._running:
            logger.warning("[Audio] Pipeline already running")
            return

        self._running = True

        await self.start_input()
        await self.start_output()

        # 启动后台循环
        if not self._no_audio:
            self._input_task = asyncio.create_task(self.input_loop())
            self._output_task = asyncio.create_task(self.output_loop())
            logger.info("[Audio] Pipeline started (input + output)")
        else:
            logger.warning("[Audio] Pipeline started in no-audio mode")

    async def stop(self):
        """停止所有音频流并清理资源"""
        logger.info("[Audio] Stopping pipeline ...")
        self._running = False

        # 取消后台任务
        for task in (self._input_task, self._output_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._input_task = None
        self._output_task = None

        await self.stop_input()
        await self._stop_output()

        self.stop_playback()
        logger.info("[Audio] Pipeline stopped")

    async def _stop_output(self):
        """停止输出流"""
        if self._output_stream is not None:
            try:
                self._output_stream.stop()
                self._output_stream.close()
            except Exception as exc:
                logger.warning("[Audio] Error closing output stream: %s", exc)
            finally:
                self._output_stream = None
                self._is_playing = False

    # ── 工具方法 ──────────────────────────────────

    @property
    def is_speaking(self) -> bool:
        """用户是否正在说话"""
        return self._is_speaking

    @property
    def is_playing(self) -> bool:
        """是否正在播放音频"""
        return self._is_playing

    @property
    def no_audio(self) -> bool:
        """是否处于无音频模式"""
        return self._no_audio

    @no_audio.setter
    def no_audio(self, value: bool):
        self._no_audio = value

    @staticmethod
    def pcm_to_float32(pcm_int16: np.ndarray) -> np.ndarray:
        """int16 PCM → float32 [-1.0, 1.0]"""
        return pcm_int16.astype(np.float32) / 32768.0

    @staticmethod
    def float32_to_pcm(audio_float32: np.ndarray) -> np.ndarray:
        """float32 [-1.0, 1.0] → int16 PCM"""
        clipped = np.clip(audio_float32, -1.0, 1.0)
        return (clipped * 32767).astype(np.int16)
