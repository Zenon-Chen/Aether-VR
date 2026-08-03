"""独立运行 posture 服务

    python -m aether.services.posture --prompt "walk forward" --duration 3

用于脱离主控制器单独调试桥接与坐标系，不依赖 ER2 或大脑。
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from ...infra.config import load_config
from ...infra.logging_setup import setup_logging
from .service import PostureService

logger = logging.getLogger("aether.services.posture")


async def main() -> None:
    parser = argparse.ArgumentParser(description="DART → SteamVR 姿态服务")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--prompt", default="", help="动作提示词；留空则只保活站立")
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--full-body", action="store_true")
    parser.add_argument("--hold", type=float, default=10.0, help="播放后保活秒数")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.log)

    service = PostureService(config.posture, config.dart)
    await service.start()
    try:
        if args.prompt:
            result = await service.play(args.prompt, args.duration, args.full_body)
            logger.info("播放完成：%s", result)
        else:
            logger.info("仅保活站立姿势，Ctrl+C 退出")
        await asyncio.sleep(args.hold)
        await service.go_idle()
    finally:
        await service.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
