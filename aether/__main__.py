"""python -m aether — 主入口"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .infra.config import load_config
from .infra.logging_setup import setup_logging
from .runtime import AetherRuntime, run_cli

logger = logging.getLogger("aether.runtime")


async def main() -> int:
    parser = argparse.ArgumentParser(prog="aether", description="Aether VRChat 虚拟角色")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--no-posture", action="store_true", help="不启动姿态桥")
    parser.add_argument("--no-capture", action="store_true", help="不采集音视频")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.no_posture:
        config.posture.enabled = False
    if args.no_capture:
        config.capture.enabled = False
    setup_logging(config.log)

    if not config.cerebellum.api_key:
        logger.error("缺少 GEMINI_API_KEY，请设置环境变量或写入配置")
        return 1

    runtime = AetherRuntime(config)
    try:
        await runtime.start()
        await run_cli(runtime)
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    except Exception:
        logger.exception("运行失败")
        return 1
    finally:
        await runtime.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
