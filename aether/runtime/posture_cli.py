"""独立调试姿态输出与动作生成（Runtime 装配入口）

    python -m aether.runtime.posture_cli --prompt "walk forward" --duration 3

将 PostureService 与 MotionProviderPort 的接线保持在 runtime 层，
确保 services/posture 包内零跨服务导入。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Sequence

from ..infra.config import load_config
from ..infra.logging_setup import setup_logging
from ..services.posture import PostureService
from .motion_wiring import create_motion_provider

logger = logging.getLogger("aether.services.posture")


async def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="DART → SteamVR 姿态服务调试")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--prompt", default="", help="动作提示词；留空则只保活站立")
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--full-body", action="store_true")
    parser.add_argument("--hold", type=float, default=10.0, help="播放后保活秒数")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    setup_logging(config.log)

    provider = create_motion_provider(config) if args.prompt else None
    service = PostureService(config.posture, motion_provider=provider)
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
