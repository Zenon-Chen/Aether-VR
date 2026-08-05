"""CLI — 交互式控制台

阶段 1 的验收入口：不接大脑时，用命令直接驱动各执行器，
验证「ER2 → 调度器 → 执行器 → VRChat」整条链路。
"""

from __future__ import annotations

import asyncio
import logging

from ..contracts.plan import ActionKind, Plan, PlanStep
from ..core import render_facts

logger = logging.getLogger(__name__)

BANNER = """
=== Aether ===
  /status            系统状态与世界事实
  /facts             仅显示事实渲染文本
  /say <文本>        测试 TTS → 虚拟麦
  /do <动作>         测试预录动作（wave / nod / bow ...）
  /face <表情>       测试表情
  /move <方向> [秒]  测试移动（forward / turn_left ...）
  /motion <描述>     测试 DART 动作生成
  /approach [目标]   高频视觉伺服接近人物（默认 person）
  /idle              姿态回到站立
  /plan              测试并行计划（说话 + 挥手）
  /stop              紧急停止
  /quit              退出
"""


async def run_cli(runtime) -> None:
    print(BANNER)
    loop = asyncio.get_running_loop()

    while True:
        try:
            line = (await loop.run_in_executor(None, lambda: input("> "))).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue

        command, _, argument = line.partition(" ")
        argument = argument.strip()

        try:
            if command in ("/quit", "/exit"):
                break
            elif command == "/status":
                _print_status(runtime)
            elif command == "/facts":
                print(render_facts(runtime.state.facts))
            elif command == "/say":
                await runtime.scheduler.submit(_one(ActionKind.SPEAK, {"text": argument}))
            elif command == "/do":
                await runtime.scheduler.submit(_one(ActionKind.PLAY_ACTION, {"name": argument}))
            elif command == "/face":
                await runtime.scheduler.submit(_one(ActionKind.EXPRESSION, {"name": argument}))
            elif command == "/move":
                parts = argument.split()
                direction = parts[0] if parts else "forward"
                seconds = float(parts[1]) if len(parts) > 1 else 1.0
                await runtime.scheduler.submit(
                    _one(ActionKind.MOVE, {"direction": direction, "duration_s": seconds})
                )
            elif command == "/motion":
                await runtime.scheduler.submit(
                    _one(ActionKind.GENERATE_MOTION, {"prompt": argument, "duration_s": 3.0})
                )
            elif command == "/approach":
                result = await runtime.approach_target(argument or "person")
                print(f"视觉伺服停止：{result.reason}（{result.steps} 步）")
            elif command == "/idle":
                await runtime.posture.go_idle()
            elif command == "/plan":
                await runtime.scheduler.submit(Plan(steps=(
                    PlanStep(1, ActionKind.SPEAK, {"text": "你好呀，我在这边！"}),
                    PlanStep(2, ActionKind.PLAY_ACTION, {"name": "wave"}, parallel=True),
                )))
            elif command == "/stop":
                elapsed = await runtime.trigger_emergency("手动触发")
                print(f"紧急停止完成，耗时 {elapsed * 1000:.0f}ms")
            else:
                print(f"未知命令：{command}")
        except Exception as exc:
            print(f"执行失败：{exc}")
            logger.exception("[CLI] 命令失败")


def _one(action: ActionKind, args: dict) -> Plan:
    return Plan(steps=(PlanStep(1, action, args),), source="cli")


def _print_status(runtime) -> None:
    facts = runtime.state.facts
    print(f"— 事实版本 v{facts.version} —")
    print(render_facts(facts))
    print("— 服务 —")
    services = facts.services
    for name in ("er2", "brain", "tts", "dart", "posture", "vrchat_osc", "vision"):
        print(f"  {name:<12} {getattr(services, name)}")
    if runtime.config.posture.enabled:
        print(f"  posture mode {runtime.posture.mode.value}")
