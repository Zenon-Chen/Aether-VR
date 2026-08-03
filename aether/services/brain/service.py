"""brain-service — 人格与观察（Gemini Flash Lite）

分工（架构评审结论）：
    人格、台词、记忆一致性全部在这一层。
    ER2 只是编排器，不负责角色是谁。

阶段 2 接入；config.brain.enabled=False 时以空实现运行，
心跳退化为纯事实模板，系统其余部分不受影响。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ...contracts.observation import BrainOutput
from ...contracts.perception import VideoFrame
from ...infra.config import BrainConfig

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types
    _HAS_GENAI = True
except Exception:  # pragma: no cover
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    _HAS_GENAI = False


PERSONA_PROMPT = """你是一个生活在 VRChat 中的虚拟角色的「意识」。

你负责：观察画面与对话、维持稳定人格、起草角色要说的话。
你不负责：具体怎么动。那是运动编排系统的事。

每轮你会收到：当前世界事实、最近的对话转写、一帧画面。
你必须只输出一个 JSON 对象，不要任何解释文字或 markdown 围栏：

{
  "observations": [{"event": "事件名", "target": "对象描述", "confidence": 0.8}],
  "suggested_reply": "角色此刻应该说的一句话，符合人设，口语化，不超过40字",
  "suggested_intent": "简短意图标签，如 greet_warmly",
  "wake_er2": false,
  "memory_write": ["值得长期记住的事实"]
}

规则：
- 没有值得回应的事情时，suggested_reply 留空字符串，wake_er2 为 false。
- 有人打招呼、靠近、提问时，wake_er2 设为 true。
- suggested_reply 是角色的真实台词，不要写成第三人称描述。
"""


class BrainService:
    """高频观察循环"""

    name = "brain"

    def __init__(self, config: BrainConfig) -> None:
        self._config = config
        self._client = None
        self._last: Optional[BrainOutput] = None
        self._failures = 0
        self._running = False

    # ── 生命周期 ───────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        if not self._config.enabled:
            logger.info("[Brain] 未启用（阶段 1 使用固定心跳模板）")
            return
        if not _HAS_GENAI or not self._config.api_key:
            logger.warning("[Brain] Gemini 不可用，观察层禁用")
            return
        self._client = genai.Client(api_key=self._config.api_key)
        logger.info("[Brain] 就绪：%s", self._config.model)

    async def stop(self) -> None:
        self._running = False
        self._client = None

    async def health(self) -> str:
        if not self._config.enabled:
            return "disabled"
        if self._client is None:
            return "degraded"
        return "ok" if self._failures < 3 else "degraded"

    @property
    def enabled(self) -> bool:
        return self._config.enabled and self._client is not None

    @property
    def last_output(self) -> Optional[BrainOutput]:
        return self._last

    # ── 观察 ───────────────────────────────────────────────

    async def observe(
        self,
        facts_text: str,
        transcript: str = "",
        frame: Optional[VideoFrame] = None,
    ) -> Optional[BrainOutput]:
        """一轮观察。解析失败返回 None，由调用方沿用上轮建议。"""
        if not self.enabled:
            return None

        parts: list = [
            f"—世界事实—\n{facts_text}",
            f"—最近对话—\n{transcript or '（无）'}",
        ]
        if frame is not None:
            parts.append(types.Part.from_bytes(data=frame.jpeg, mime_type="image/jpeg"))

        try:
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self._config.model,
                contents=parts,
                config=types.GenerateContentConfig(
                    system_instruction=PERSONA_PROMPT,
                    max_output_tokens=self._config.max_output_tokens,
                    response_mime_type="application/json",
                ),
            )
            parsed = BrainOutput.parse(response.text or "")
            if parsed is None:
                self._failures += 1
                logger.warning("[Brain] JSON 解析失败（连续 %d 次）", self._failures)
                return None
            self._failures = 0
            self._last = parsed
            if parsed.wake_er2:
                logger.info("[Brain] 事件唤醒：%s", parsed.suggested_intent or "未标注")
            return parsed
        except Exception as exc:
            self._failures += 1
            logger.error("[Brain] 观察失败：%s", exc)
            return None
