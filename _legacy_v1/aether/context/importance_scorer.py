"""重要性评分器 — 规则引擎 + LLM 混合评分"""
import asyncio
import json
import logging
import time
from typing import Optional

from google import genai

from ..config import ContextConfig
from .block_store import ContextBlock
from .ports import BlockRepository

logger = logging.getLogger(__name__)


class ImportanceScorer:
    """双触发重要性评分：每 N 个新块 或 每 T 秒"""

    def __init__(self, context_config: ContextConfig, block_store: BlockRepository, api_key: str):
        self.config = context_config
        self.store = block_store
        self.client = genai.Client(api_key=api_key)
        self.model = context_config.scorer_model          # gemini-2.0-flash
        self.batch_size = context_config.scoring_batch_size  # 10
        self.interval = context_config.scoring_interval    # 30 秒

        self._unscored_blocks: list[ContextBlock] = []
        self._last_score_time = time.time()

    def queue_for_scoring(self, block: ContextBlock):
        """将块加入待评分队列"""
        self._unscored_blocks.append(block)

    def should_score(self) -> bool:
        """检查是否应该触发评分"""
        if len(self._unscored_blocks) >= self.batch_size:
            return True
        if time.time() - self._last_score_time >= self.interval and self._unscored_blocks:
            return True
        return False

    async def score_pending(self):
        """对队列中的块进行评分"""
        if not self._unscored_blocks:
            return

        blocks_to_score = self._unscored_blocks.copy()
        self._unscored_blocks.clear()
        self._last_score_time = time.time()

        # 先用规则引擎快速评分
        rule_scored = []
        llm_needed = []
        for block in blocks_to_score:
            score = self._rule_based_score(block)
            if score is not None:
                block.importance_score = score
                rule_scored.append(block)
            else:
                llm_needed.append(block)

        # 对模糊场景用 LLM 批量评分
        if llm_needed:
            await self._llm_batch_score(llm_needed)

        logger.info(
            "[Scorer] Scored %d blocks (rule: %d, llm: %d)",
            len(blocks_to_score), len(rule_scored), len(llm_needed),
        )

    def _rule_based_score(self, block: ContextBlock) -> Optional[float]:
        """规则引擎评分，返回 None 表示需要 LLM 判断"""
        content = block.content.lower()

        # 明确的低重要性
        if block.block_type == "tool_call" and "report_status" in content:
            return 0.2
        if "heartbeat" in block.metadata.get("source", ""):
            return 0.1

        # 明确的高重要性
        if block.block_type == "tool_call" and "execute_motion" in content:
            return 0.7   # 动作指令比较重要
        if block.block_type == "tool_call" and "recall_memory" in content:
            return 0.8   # 记忆召回很重要

        # 无法确定，需要 LLM
        return None

    async def _llm_batch_score(self, blocks: list[ContextBlock]):
        """用 LLM 批量评估重要性"""
        block_descriptions = []
        for i, block in enumerate(blocks):
            desc = f"[{i}] type={block.block_type}, content={block.content[:100]}"
            block_descriptions.append(desc)

        prompt = (
            "Rate the importance of each context block on a scale of 0.0 to 1.0.\n"
            "- 0.9-1.0: Critical user instructions, major decisions\n"
            "- 0.7-0.8: Important events, state changes\n"
            "- 0.5-0.6: Notable actions or results\n"
            "- 0.3-0.4: Routine reasoning, intermediate steps\n"
            "- 0.0-0.2: Redundant, filler, low-value content\n\n"
            "Blocks:\n"
            + "\n".join(block_descriptions)
            + '\n\nRespond with JSON: {"scores": [0.5, 0.3, ...]} (one score per block, in order)'
        )

        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            result = json.loads(response.text)
            scores = result.get("scores", [])

            for i, block in enumerate(blocks):
                if i < len(scores):
                    block.importance_score = max(0.0, min(1.0, float(scores[i])))
                else:
                    block.importance_score = 0.5

        except Exception as e:
            logger.error("[Scorer] LLM scoring failed: %s", e)
            # Fallback: 默认 0.5
            for block in blocks:
                block.importance_score = 0.5
