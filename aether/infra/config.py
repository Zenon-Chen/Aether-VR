"""配置 — Pydantic 模型 + YAML + 环境变量覆盖

新增配置项必须提供默认值，保证缺省配置也能启动。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path("config.yaml")


class CaptureConfig(BaseModel):
    enabled: bool = True
    video_fps: float = 1.0
    jpeg_quality: int = 70
    max_width: int = 1024
    monitor: int = 1
    audio_device: Optional[str] = None
    input_sample_rate: int = 16000
    chunk_ms: int = 100
    vad_threshold: float = 0.5
    vad_rms_threshold: float = 0.02
    vad_hangover_s: float = 0.4


class CerebellumConfig(BaseModel):
    """ER2 —— 小脑，只做编排"""
    model: str = "gemini-robotics-er-2-streaming-preview"
    api_key: str = ""
    response_modalities: list[str] = Field(default_factory=lambda: ["TEXT"])
    compression_trigger_tokens: int = 120000
    compression_target_tokens: int = 80000
    max_plan_steps: int = 6
    max_plan_seconds: float = 5.0


class BrainConfig(BaseModel):
    """人格与观察 —— Gemini Flash Lite"""
    enabled: bool = False          # 阶段 2 接入
    model: str = "gemini-3.5-flash-lite"
    api_key: str = ""
    persona_path: str = "docs/personas/aether-default.yaml"
    interval_s: float = 60.0
    initial_delay_s: float = 1.0
    min_event_interval_s: float = 15.0
    max_output_tokens: int = 512
    context_limit_tokens: int = 500_000
    evict_target_tokens: int = 350_000


class ScribeConfig(BaseModel):
    """长期记忆与上下文压缩"""
    enabled: bool = False
    batch_size: int = 10
    db_path: str = "data/memory.sqlite3"
    dedupe_window_s: float = 3600.0
    context_limit_tokens: int = 120_000
    evict_target_tokens: int = 80_000


class SpeechConfig(BaseModel):
    model: str = "gemini-3.1-flash-tts-preview"
    api_key: str = ""
    voice_name: str = "Kore"
    max_segment_chars: int = 30
    output_sample_rate: int = 24000
    #: 虚拟声卡设备名（TTS → VRChat 麦克风）。None 表示系统默认设备。
    output_device: Optional[str] = None
    fallback_sound: str = "assets/filler.wav"
    synthesis_allowance_s: float = 10.0


class DartConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8900
    timeout_s: float = 60.0

    @property
    def generate_url(self) -> str:
        return f"http://{self.host}:{self.port}/generate"

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"


class MotionRuntimeConfig(BaseModel):
    """动作生成器装配与超时预算配置（支持外部 HTTP 或受管 sidecar bundle）。"""
    mode: str = "external"                     # "external" | "supervised"
    bundle_manifest: str = ""
    bundle_dir: str = ""
    allow_unreleased_bundle: bool = False
    generation_timeout_s: float = 15.0         # 单次动态生成上限，超时回退预录动作
    synthesis_allowance_s: float = 20.0        # 调度器针对动态动作的额外 I/O 墙钟宽限


class PostureConfig(BaseModel):
    """姿态输出配置：SteamVR 六设备桥与持续保活参数。"""
    enabled: bool = True
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 4242        # driver.cpp kPort，改动需同步 C++
    keepalive_fps: float = 45.0    # 必须显著快于驱动 1 秒失联阈值
    blend_s: float = 0.3           # 段间/来源切换插值时长
    idle_pose: str = "stand naturally*1"


class OSCConfig(BaseModel):
    """VRChat OSC —— 只能传 int/float/bool，不能传骨骼旋转"""
    host: str = "127.0.0.1"
    port: int = 9000
    parameter_prefix: str = "/avatar/parameters"
    input_prefix: str = "/input"


class AvatarConfig(BaseModel):
    """预录动作与表情映射 —— 对应 VRChat Animator 里的 Int 参数"""
    action_parameter: str = "ActionID"
    expression_parameter: str = "Expression"
    actions: dict[str, int] = Field(default_factory=lambda: {
        "idle": 0, "wave": 1, "nod": 2, "shake_head": 3, "bow": 4,
        "clap": 5, "point": 6, "think": 7, "sit": 8, "stand": 9,
    })
    action_durations: dict[str, float] = Field(default_factory=lambda: {
        "wave": 1.8, "nod": 1.0, "shake_head": 1.2, "bow": 2.0,
        "clap": 1.5, "point": 1.2, "think": 2.5, "sit": 2.0, "stand": 2.0,
    })
    full_body_actions: list[str] = Field(default_factory=lambda: ["bow", "sit", "stand"])
    motion_fallback_actions: dict[str, str] = Field(default_factory=lambda: {
        "full_body": "bow", "upper_body": "wave",
    })
    expressions: dict[str, int] = Field(default_factory=lambda: {
        "neutral": 0, "smile": 1, "surprised": 2, "sad": 3, "angry": 4,
    })


class LocomotionConfig(BaseModel):
    max_duration_s: float = 3.0
    #: ER2 断连或超时无续期 → 强制归零
    watchdog_s: float = 5.0
    pulse_s: float = 0.4           # 走-看-走 的单次脉冲时长


class VisionConfig(BaseModel):
    enabled: bool = True
    detector: str = "auto"
    detect_fps: float = 4.0
    fresh_frame_timeout_s: float = 1.0
    near_ratio: float = 0.25
    confidence_min: float = 0.6
    center_tolerance: float = 0.12
    pulse_s: float = 0.4
    timeout_s: float = 30.0
    max_steps: int = 30
    stagnation_steps: int = 4


class LogConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/aether.log"
    max_bytes: int = 10_000_000
    backup_count: int = 7


class AetherConfig(BaseModel):
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    cerebellum: CerebellumConfig = Field(default_factory=CerebellumConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    scribe: ScribeConfig = Field(default_factory=ScribeConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    dart: DartConfig = Field(default_factory=DartConfig)
    motion: MotionRuntimeConfig = Field(default_factory=MotionRuntimeConfig)
    posture: PostureConfig = Field(default_factory=PostureConfig)
    osc: OSCConfig = Field(default_factory=OSCConfig)
    avatar: AvatarConfig = Field(default_factory=AvatarConfig)
    locomotion: LocomotionConfig = Field(default_factory=LocomotionConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    log: LogConfig = Field(default_factory=LogConfig)


def load_config(path: str | Path | None = None) -> AetherConfig:
    """加载配置：YAML → 环境变量覆盖 API Key"""
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict = {}
    if target.exists():
        with target.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    config = AetherConfig.model_validate(data)

    # 资源路径相对于配置文件目录，而不是易变的进程 cwd。
    base_dir = target.resolve().parent if target.exists() else Path.cwd()
    for section, field_name in (
        (config.brain, "persona_path"),
        (config.speech, "fallback_sound"),
        (config.scribe, "db_path"),
        (config.log, "file"),
        (config.motion, "bundle_manifest"),
        (config.motion, "bundle_dir"),
    ):
        raw_val = getattr(section, field_name)
        if not raw_val:
            continue
        value = Path(raw_val)
        if not value.is_absolute():
            setattr(section, field_name, str(base_dir / value))

    # 环境变量优先：单一 Key 供三处使用
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        for section in (config.cerebellum, config.brain, config.speech):
            if not section.api_key:
                section.api_key = api_key
    return config
