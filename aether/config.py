"""VR Agent 系统配置管理

基于 Pydantic BaseModel + pyyaml 加载配置。
支持环境变量覆盖，API Key 优先从 GEMINI_API_KEY 环境变量读取。
"""

import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ER2Config(BaseModel):
    """Gemini Robotics ER-2 流式模型配置"""

    model: str = "gemini-robotics-er-2-streaming-preview"
    api_key: str = Field(
        default_factory=lambda: os.environ.get("GEMINI_API_KEY", "")
    )
    response_modalities: list[str] = ["TEXT"]
    context_window_tokens: int = 128000
    compression_trigger_tokens: int = 120000
    compression_target_tokens: int = 80000


class TTSConfig(BaseModel):
    """Gemini TTS 语音合成配置"""

    model: str = "gemini-3.1-flash-tts-preview"
    api_key: str = Field(
        default_factory=lambda: os.environ.get("GEMINI_API_KEY", "")
    )
    voice_name: str = "Kore"
    max_segment_chars: int = 30  # 每段最大字数


class DARTConfig(BaseModel):
    """DART 推理服务连接配置"""

    host: str = "localhost"
    port: int = 8900
    timeout: int = 30  # 推理超时秒数


class OSCConfig(BaseModel):
    """OSC 协议通信配置"""

    host: str = "127.0.0.1"
    port: int = 9000
    avatar_prefix: str = "/avatar/parameters"
    target_fps: int = 30  # 播放帧率插值目标；DART 帧率低于此值时插值平滑


class AudioConfig(BaseModel):
    """音频输入输出管线配置"""

    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
    channels: int = 1
    sample_width: int = 2  # 16-bit
    vad_threshold: float = 0.5


class ContextConfig(BaseModel):
    """上下文管理（旁路 Agent + RAG）配置"""

    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768  # MRL 降维后
    scorer_model: str = "gemini-2.0-flash"
    max_active_tokens: int = 102400  # 80% of 128k
    heartbeat_interval: int = 30  # 秒
    scoring_interval: int = 30  # 秒
    scoring_batch_size: int = 10  # 块数触发评分


class LogConfig(BaseModel):
    """日志系统配置"""

    level: str = "INFO"
    file: str = "logs/aether.log"
    max_bytes: int = 10_000_000  # 10MB
    backup_count: int = 7


class AetherConfig(BaseModel):
    """Aether VR Agent 顶层配置聚合"""

    er2: ER2Config = Field(default_factory=ER2Config)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    dart: DARTConfig = Field(default_factory=DARTConfig)
    osc: OSCConfig = Field(default_factory=OSCConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    log: LogConfig = Field(default_factory=LogConfig)


def load_config(config_path: str = "config.yaml") -> AetherConfig:
    """从 YAML 文件加载配置，环境变量优先。

    Args:
        config_path: YAML 配置文件路径，默认当前目录下 config.yaml

    Returns:
        AetherConfig 实例，文件不存在时返回默认配置
    """
    path = Path(config_path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cfg = AetherConfig(**raw)
    else:
        logger.warning("[Config] %s not found, using defaults", config_path)
        cfg = AetherConfig()

    # API Key 非空校验
    if not cfg.er2.api_key:
        raise ValueError(
            "ER2 API Key is required. "
            "Set GEMINI_API_KEY environment variable or configure in config.yaml"
        )
    if not cfg.tts.api_key:
        logger.warning("[Config] TTS api_key is empty, falling back to ER2 key")
        cfg.tts.api_key = cfg.er2.api_key

    return cfg
