"""动作用例 — 编排 DART 生成与 OSC 播放

应用层用例（use case）：将"执行动作"的业务语义显式化，
依赖 MotionPort（生成）与 OSCPort（渲染）两个端口，
可在测试中注入 fake 实现，无需真实 DART/OSC。
"""

import logging
from typing import Optional

from ..config import DARTConfig
from ..protocols import MotionPort, OSCPort

logger = logging.getLogger(__name__)


class MotionUseCase:
    """动作用例：生成动作骨架数据并交给渲染器播放

    原实现位于 main._wire_handlers 的匿名闭包中，
    提取为独立用例类以便独立测试与替换。
    """

    def __init__(self, motion: MotionPort, renderer: OSCPort,
                 config: Optional[DARTConfig] = None):
        self._motion = motion
        self._renderer = renderer
        self._config = config or DARTConfig()

    async def execute(self, action: str, duration: float = 2.0) -> dict:
        """执行动作：DART 生成 → OSC 播放

        Args:
            action: 英文动作描述，如 "walk forward"
            duration: 动作持续时间（秒）

        Returns:
            DART 返回的骨架数据 dict（含 poses/joints/num_frames），
            失败时返回 {"num_frames": 0, "error": ...}
        """
        if not await self._motion.is_available():
            logger.warning("Motion skipped: DART service unavailable")
            return {"num_frames": 0, "error": "DART service unavailable"}

        prompt = self._motion.format_prompt(action, duration)
        logger.info("Generating motion: %s", prompt)
        try:
            result = await self._motion.generate(prompt)
            if result and "poses" in result:
                await self._renderer.play_motion(result)
            return result or {}
        except Exception as exc:
            logger.error("Motion generation failed: %s", exc)
            return {"num_frames": 0, "error": str(exc)}
