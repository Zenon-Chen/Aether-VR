"""perception_gate — 感知流泵、轻量能量 VAD 与本地语音急停拦截（PerceptionGate）

职责：
    1. 将 CaptureService 采集的音频/视频流持续推送至 CerebellumService (ER2)；
    2. 基于 16-bit PCM 归一化 RMS 幅度维护 user_speaking 状态（含尾音保持），
       使 HeartbeatAssembler 的 Gate.USER_SPEAKING 在真实语音输入期间生效；
    3. 监听 ER2 增量与最终输入转写，命中急停词时 0ms 本地直发 EmergencyStop，
       并将用户转写纳入受跟踪的 Scribe 写入任务与大脑防抖唤醒事件。
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from ..core.safety import EmergencyStop

if TYPE_CHECKING:
    from .agent import AetherRuntime


def compute_pcm16_rms(pcm: bytes) -> float:
    """计算 16-bit 小端有符号 PCM 的归一化均方根幅度 [0.0, 1.0]。"""
    n_bytes = len(pcm) & ~1
    if n_bytes < 2:
        return 0.0
    samples = memoryview(pcm)[:n_bytes].cast("h")
    mean_sq = sum(sample * sample for sample in samples) / len(samples)
    return min(1.0, math.sqrt(mean_sq) / 32768.0)


class PerceptionGate:
    """感知输入网关：音视频泵 + PCM VAD + 急停词旁路拦截。"""

    def __init__(self, runtime: "AetherRuntime") -> None:
        self._rt = runtime
        self._last_voice_ts: float = 0.0
        self._vad_active: bool = False
        self._stop_triggered_in_turn: bool = False

    def observe_audio_rms(self, pcm: bytes, *, now: float | None = None) -> bool:
        """根据单块 PCM 更新 VAD 状态，返回当前 VAD 是否判定用户正在说话。"""
        ts = time.monotonic() if now is None else now
        capture_cfg = getattr(getattr(self._rt, "config", None), "capture", None)
        threshold = float(getattr(capture_cfg, "vad_rms_threshold", 0.02))
        hangover_s = float(getattr(capture_cfg, "vad_hangover_s", 0.4))

        rms = compute_pcm16_rms(pcm)
        if rms >= threshold:
            self._last_voice_ts = ts
            self._vad_active = True
            self._rt.set_user_speaking(True)
        elif self._vad_active and (ts - self._last_voice_ts) >= hangover_s:
            self._vad_active = False
            self._rt.set_user_speaking(False)
        return self._vad_active

    async def audio_pump(self) -> None:
        """麦克风 → 本地轻量 VAD → ER2 实时音频输入。"""
        async for chunk in self._rt.capture.audio_stream():
            if not self._rt._running:
                break
            self.observe_audio_rms(chunk.pcm)
            await self._rt.cerebellum.send_audio(chunk)

    async def video_pump(self) -> None:
        """屏幕帧 → ER2 实时视频输入。"""
        async for frame in self._rt.capture.video_stream():
            if not self._rt._running:
                break
            await self._rt.cerebellum.send_video(frame)

    def on_partial_transcript(self, text: str) -> None:
        """ER2 增量转写回调：不等整句结束即可本地 0ms 拦截急停关键词。"""
        cleaned = (text or "").strip()
        if not cleaned or self._stop_triggered_in_turn:
            return
        if getattr(self._rt, "emergency", None) is not None and EmergencyStop.matches_keyword(cleaned):
            self._stop_triggered_in_turn = True
            self._rt.spawn_bg_task(
                self._rt.trigger_emergency(f"语音急停关键词: {cleaned}"),
                name="emergency:partial_transcript",
            )

    def on_transcript(self, line: str) -> None:
        """ER2 完成句转写回调：急停兜底、更新转写窗口、登记 Scribe 并通知大脑。"""
        cleaned = (line or "").strip()
        if not cleaned:
            self._stop_triggered_in_turn = False
            return

        already_stopped = self._stop_triggered_in_turn
        self._stop_triggered_in_turn = False
        if (
            not already_stopped
            and getattr(self._rt, "emergency", None) is not None
            and EmergencyStop.matches_keyword(cleaned)
        ):
            self._rt.spawn_bg_task(
                self._rt.trigger_emergency(f"语音急停关键词: {cleaned}"),
                name="emergency:transcript",
            )

        if self._vad_active:
            self._vad_active = False
            self._rt.set_user_speaking(False)

        self._rt._transcript.append(cleaned)
        if len(self._rt._transcript) > 20:
            self._rt._transcript = self._rt._transcript[-20:]

        if getattr(self._rt, "scribe", None) is not None:
            self._rt.spawn_bg_task(
                self._rt.scribe.note("user", f"用户说：{cleaned}"),
                name="scribe:user_transcript",
            )

        if getattr(self._rt, "_coordinator", None) is not None or getattr(self._rt, "brain", None) is not None:
            self._rt._get_coordinator().notify_user_utterance()
