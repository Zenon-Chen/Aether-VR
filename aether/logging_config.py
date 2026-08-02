"""日志配置 — CLI 彩色输出 + 文件日志"""

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


# 每个模块不同的颜色（ANSI 转义码）
# key 必须与真实 logger 名匹配（logging.getLogger(__name__) 的结果）
MODULE_COLORS = {
    "aether.er2.client": "\033[36m",          # 青色 - ER2
    "aether.tool_dispatcher": "\033[33m",     # 黄色 - 分发器
    "aether.motion.dart_client": "\033[35m",  # 紫色 - DART
    "aether.speech": "\033[32m",              # 绿色 - TTS 语音
    "aether.io.audio_pipeline": "\033[32m",   # 绿色 - 音频管道
    "aether.motion.osc_sender": "\033[34m",   # 蓝色 - OSC
    "aether.context": "\033[31m",             # 红色 - 上下文管理
    "aether.main": "\033[37m",                # 白色 - 主程序
}
RESET = "\033[0m"


class ColoredFormatter(logging.Formatter):
    """带颜色的 CLI 格式化器

    按 logger name 前缀匹配 MODULE_COLORS 中的颜色，
    注入 ``module_color`` 和 ``reset`` 变量供格式字符串使用。
    """

    def format(self, record):
        color = ""
        for prefix, c in MODULE_COLORS.items():
            if record.name.startswith(prefix):
                color = c
                break
        record.module_color = color
        record.reset = RESET
        return super().format(record)


def setup_logging(config) -> logging.Logger:
    """配置日志系统

    1. CLI handler — 彩色输出到 stdout，级别由 config.log.level 控制
    2. File handler — 纯文本 RotatingFileHandler，始终记录 DEBUG 以上
    3. 根 logger ``aether`` — DEBUG 级别，同时挂载两个 handler

    Args:
        config: AetherConfig 实例（需包含 config.log 子配置）

    Returns:
        ``aether`` 根 Logger 实例
    """
    # ── 1. CLI handler（彩色） ──────────────────
    cli_handler = logging.StreamHandler(sys.stdout)
    cli_handler.setLevel(getattr(logging, config.log.level.upper(), logging.INFO))
    cli_formatter = ColoredFormatter(
        "%(module_color)s%(asctime)s [%(name)s] %(levelname)s: %(message)s%(reset)s",
        datefmt="%H:%M:%S",
    )
    cli_handler.setFormatter(cli_formatter)

    # ── 2. File handler（纯文本，大小轮转） ─────
    log_dir = Path(config.log.file).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        config.log.file,
        maxBytes=config.log.max_bytes,
        backupCount=config.log.backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)

    # ── 3. 根 logger ───────────────────────────
    root_logger = logging.getLogger("aether")
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(cli_handler)
    root_logger.addHandler(file_handler)

    return root_logger
