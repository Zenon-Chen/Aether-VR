"""服务层 — 每个子目录一个可独立检修、替换、单独启动的模块

    capture/     屏幕与麦克风采集
    brain/       人格与观察（Gemini Flash Lite）
    cerebellum/  运动编排（ER2 Live）
    speech/      TTS → 虚拟麦克风
    posture/     DART → SteamVR 六设备桥
    avatar/      预录动作与表情 OSC
    locomotion/  移动 OSC
    scribe/      长期记忆与上下文压缩

约定：每个服务目录包含
    service.py    实现
    __main__.py   可独立运行（python -m aether.services.<name>）
共享的对外协议集中在 services/ports.py。
"""

from .ports import (
    Service, CapturePort, BrainPort, CerebellumPort, SpeechPort,
    PosturePort, AvatarPort, LocomotionPort, ScribePort,
)

__all__ = [
    "Service", "CapturePort", "BrainPort", "CerebellumPort", "SpeechPort",
    "PosturePort", "AvatarPort", "LocomotionPort", "ScribePort",
]
