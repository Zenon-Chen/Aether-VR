"""日志 — 彩色 CLI + 轮转文件

新增服务时在 MODULE_COLORS 添加前缀映射，便于在密集日志中区分链路。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

RESET = "\033[0m"
DIM = "\033[2m"

#: logger 名前缀 → ANSI 颜色
MODULE_COLORS: dict[str, str] = {
    "aether.core.state_store": "\033[38;5;39m",     # 蓝 — 事实
    "aether.core.scheduler": "\033[38;5;208m",      # 橙 — 调度
    "aether.core.heartbeat": "\033[38;5;141m",      # 紫 — 心跳
    "aether.core.safety": "\033[38;5;196m",         # 红 — 安全
    "aether.services.capture": "\033[38;5;244m",    # 灰 — 感知
    "aether.services.brain": "\033[38;5;213m",      # 粉 — 大脑
    "aether.services.cerebellum": "\033[38;5;51m",  # 青 — 小脑
    "aether.services.speech": "\033[38;5;220m",     # 黄 — 语音
    "aether.services.posture": "\033[38;5;46m",     # 绿 — 姿态
    "aether.services.avatar": "\033[38;5;120m",     # 浅绿 — 化身
    "aether.services.locomotion": "\033[38;5;123m", # 浅青 — 移动
    "aether.services.scribe": "\033[38;5;180m",     # 棕 — 记忆
    "aether.runtime": "\033[38;5;255m",             # 白 — 装配
}

_LEVEL_COLORS = {
    logging.DEBUG: DIM,
    logging.INFO: "",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}


class ColorFormatter(logging.Formatter):
    def __init__(self, use_color: bool = True) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        color = ""
        for prefix, value in MODULE_COLORS.items():
            if record.name.startswith(prefix):
                color = value
                break
        level_color = _LEVEL_COLORS.get(record.levelno, "")
        return f"{color or level_color}{text}{RESET}" if (color or level_color) else text


def setup_logging(config) -> None:
    """按配置初始化根 logger（幂等）"""
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(getattr(logging, str(config.level).upper(), logging.INFO))

    use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ColorFormatter(use_color))
    root.addHandler(console)

    if config.file:
        path = Path(config.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=config.max_bytes,
            backupCount=config.backup_count, encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "websockets", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
