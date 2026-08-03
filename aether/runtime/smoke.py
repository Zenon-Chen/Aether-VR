"""端到端冒烟：不依赖任何云端 API

验证 runtime 装配、执行器接线、UDP 实际发包。
    python -m aether.runtime.smoke
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

from ..contracts.frame import PostureFrame, WIRE_SIZE
from ..contracts.plan import ActionKind, Plan, PlanStep
from ..core import EmergencyStop, HeartbeatAssembler, Scheduler, StateStore, render_facts
from ..infra.config import load_config
from ..infra.logging_setup import setup_logging
from ..services.posture.service import PostureService

logger = logging.getLogger("aether.runtime")


def _driver_listening(config) -> bool:
    """探测 SteamVR 驱动是否已在桥端口监听"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((config.posture.bridge_host, config.posture.bridge_port))
    except OSError:
        return True      # 端口被占用 = 驱动在跑
    finally:
        probe.close()
    return False


async def check_udp_stream(config, seconds: float = 2.0) -> tuple[int, float, str]:
    """验证 posture 服务真的在按保活频率推送合法包

    驱动未运行时用本地回环自检；驱动在跑时直接向其发包，
    并用本地计数确认发送频率。
    """
    live = _driver_listening(config)
    service = PostureService(config.posture, config.dart)

    if live:
        # 驱动占用端口，无法旁听；改为统计实际发出的帧数
        sent_before = 0
        await service.start()
        started = time.perf_counter()
        await asyncio.sleep(seconds)
        sent = service.frames_sent - sent_before
        elapsed = time.perf_counter() - started
        await service.stop()
        return sent, sent / elapsed, "live-driver"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((config.posture.bridge_host, config.posture.bridge_port))
    sock.settimeout(0.3)
    await service.start()

    received, started = 0, time.perf_counter()
    loop = asyncio.get_running_loop()
    try:
        while time.perf_counter() - started < seconds:
            try:
                payload = await loop.run_in_executor(None, lambda: sock.recv(4096))
            except socket.timeout:
                continue
            if len(payload) == WIRE_SIZE:
                PostureFrame.unpack(payload)   # 校验 magic 与结构
                received += 1
    finally:
        await service.stop()
        sock.close()

    elapsed = time.perf_counter() - started
    return received, received / elapsed, "loopback"


async def check_scheduler() -> None:
    """通道锁 + 事实登记 + 紧急停止"""
    state = StateStore()
    scheduler = Scheduler(state)
    order: list[str] = []

    async def speak(step):
        order.append("speak:start")
        await asyncio.sleep(0.12)
        order.append("speak:end")
        return step.args.get("text", "")

    async def wave(step):
        order.append("wave:start")
        await asyncio.sleep(0.12)
        order.append("wave:end")
        return "wave"

    scheduler.register(ActionKind.SPEAK, speak)
    scheduler.register(ActionKind.PLAY_ACTION, wave)

    result = await scheduler.submit(Plan(steps=(
        PlanStep(1, ActionKind.SPEAK, {"text": "你好呀，我在这边！"}),
        PlanStep(2, ActionKind.PLAY_ACTION, {"name": "wave"}, parallel=True),
    )))

    parallel_ok = order.index("wave:start") < order.index("speak:end")
    logger.info("计划结果：%s", result.summary())
    logger.info("执行顺序：%s", " → ".join(order))
    logger.info("并行执行：%s", "是" if parallel_ok else "否")
    logger.info("通道已释放：%s", not state.facts.busy_channels())

    heartbeat = HeartbeatAssembler()
    decision = heartbeat.assemble(state.facts, None, force=True)
    logger.info("心跳：\n%s", decision.text)

    stop = EmergencyStop(state)
    stop.register("noop", _noop)
    elapsed = await stop.trigger("冒烟测试")
    logger.info("紧急停止耗时：%.0fms", elapsed * 1000)


async def _noop() -> None:
    return None


async def main() -> None:
    config = load_config("config.yaml")
    setup_logging(config.log)

    logger.info("──── 调度器与核心 ────")
    await check_scheduler()

    logger.info("──── 姿态桥 UDP 推流 ────")
    count, fps, mode = await check_udp_stream(config)
    if mode == "live-driver":
        logger.info("检测到 SteamVR 驱动已占用 %d 端口，直接向其推流",
                    config.posture.bridge_port)
    logger.info("发送 %d 个包，实测 %.1f Hz（须 >1Hz 驱动才不判失联）", count, fps)
    logger.info("保活达标：%s", "是" if fps > 1.0 else "否")


if __name__ == "__main__":
    asyncio.run(main())
