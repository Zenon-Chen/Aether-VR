"""Minimal real VRChat OSC visibility test; never starts VRChat or SteamVR."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Sequence

from ..infra.config import AetherConfig, load_config
from ..services.avatar import AvatarService
from ..services.locomotion import LocomotionService

ACTIONS: tuple[str, ...] = (
    "wave", "nod", "shake_head", "clap", "point", "think", "bow", "sit", "stand"
)


def validate_config(config: AetherConfig) -> list[str]:
    errors: list[str] = []
    for name in ACTIONS:
        if name not in config.avatar.actions:
            errors.append(f"avatar.actions 缺少 {name}")
        if name not in config.avatar.action_durations:
            errors.append(f"avatar.action_durations 缺少 {name}")
        elif config.avatar.action_durations[name] <= 0:
            errors.append(f"avatar.action_durations.{name} 必须大于 0")
    if config.avatar.actions.get("idle") is None:
        errors.append("avatar.actions 缺少 idle")
    if config.avatar.expressions.get("smile") is None:
        errors.append("avatar.expressions 缺少 smile")
    ids = list(config.avatar.actions.values())
    if len(ids) != len(set(ids)):
        errors.append("avatar.actions 的 ActionID 必须唯一")
    return errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aether VRChat visible OSC test")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--hold", type=float, default=0.0, help="每个动作额外保持秒数")
    parser.add_argument("--action-delay", type=float, default=0.25)
    parser.add_argument("--movement", action="store_true")
    parser.add_argument("--posture", action="store_true", help="调用真实 DART posture")
    parser.add_argument("--dry-run", action="store_true")
    return parser


async def run(config: AetherConfig, *, hold: float = 0.0, action_delay: float = 0.25,
              movement: bool = False, posture: bool = False, dry_run: bool = False) -> None:
    errors = validate_config(config)
    print("VRChat visible test checklist:")
    print(f"  OSC target: {config.osc.host}:{config.osc.port}")
    print(f"  Actions: expression smile, {', '.join(ACTIONS)}")
    print(f"  Movement: {'enabled' if movement else 'disabled'}; posture: {'enabled' if posture else 'disabled'}")
    if errors:
        raise ValueError("配置校验失败: " + "; ".join(errors))
    if dry_run:
        for name in ACTIONS:
            print(f"  [dry-run] {name}: {config.avatar.action_durations[name] + hold:.2f}s")
        print("DRY-RUN: 未发送 OSC")
        return

    avatar = AvatarService(config.avatar, config.osc)
    locomotion = LocomotionService(config.locomotion, config.osc)
    posture_service = None
    try:
        await avatar.start()
        await locomotion.start()
        if posture:
            from ..services.posture import PostureService
            from .motion_wiring import create_motion_provider
            posture_service = PostureService(
                config.posture, motion_provider=create_motion_provider(config)
            )
            await posture_service.start()
        print("已连接 OSC；请在 VRChat 中观察 Avatar。")
        print("当前动作: expression smile, 等待 0.25s")
        await avatar.set_expression("smile")
        await asyncio.sleep(action_delay)
        for name in ACTIONS:
            duration = config.avatar.action_durations[name] + max(0.0, hold)
            print(f"当前动作: {name}, 等待 {duration:.2f}s")
            if posture_service is not None:
                await posture_service.play(name, config.avatar.action_durations[name],
                                           full_body=avatar.is_full_body(name))
            else:
                await avatar.play_action(name)
                if hold:
                    await asyncio.sleep(hold)
            if movement and name == "stand":
                print("移动: forward 0.4s, turn_right 0.4s")
                await locomotion.move("forward", 0.4)
                await locomotion.move("turn_right", 0.4)
            await asyncio.sleep(action_delay)
    finally:
        if posture_service is not None:
            try:
                await posture_service.emergency_reset()
            finally:
                await posture_service.stop()
        await avatar.reset()
        await locomotion.halt()
        await avatar.stop()
        await locomotion.stop()


async def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    config = load_config(Path(args.config))
    await run(config, hold=args.hold, action_delay=args.action_delay,
              movement=args.movement, posture=args.posture, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
