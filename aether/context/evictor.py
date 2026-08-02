"""上下文驱逐器 — 分级驱逐策略"""
import asyncio
import logging
import time
from typing import Optional

from google import genai

from ..config import ContextConfig
from .block_store import ContextBlock
from .ports import BlockRepository

logger = logging.getLogger(__name__)


class ContextEvictor:
    """分级上下文驱逐器

    L1: 删 importance < 0.3
    L2: 删 importance < 0.5 且 > 5 分钟
    L3: 对重要旧块生成摘要替换原文
    L4: 内置 context_window_compression 兜底（ER2 侧配置）
    """

    def __init__(self, context_config: ContextConfig, block_store: BlockRepository, api_key: str):
        self.config = context_config
        self.store = block_store
        self.client = genai.Client(api_key=api_key)
        self.model = context_config.scorer_model
        self.max_tokens = context_config.max_active_tokens   # 102400
        self._usage_metadata_tokens: Optional[int] = None

    def should_evict(self) -> bool:
        """检查是否需要驱逐"""
        # 使用本地估算
        if self.store.total_active_tokens > self.max_tokens:
            return True
        # 使用 UsageMetadata（如果有）
        if self._usage_metadata_tokens and self._usage_metadata_tokens > self.max_tokens:
            return True
        return False

    def update_usage(self, usage_metadata):
        """从 ER2 UsageMetadata 更新 token 计数"""
        if hasattr(usage_metadata, "total_token_count"):
            self._usage_metadata_tokens = usage_metadata.total_token_count
            logger.debug("[Evictor] Usage: %d tokens", self._usage_metadata_tokens)

    async def evict(self):
        """执行分级驱逐"""
        logger.info("[Evictor] Starting eviction, tokens: %d", self.store.total_active_tokens)

        # L1: 删除低重要性块
        if self.store.total_active_tokens > self.max_tokens:
            low_importance = self.store.get_blocks_by_importance(0.3)
            for block in low_importance:
                self.store.remove_block(block.block_id)
            logger.info("[Evictor] L1: removed %d blocks (importance < 0.3)", len(low_importance))

        # L2: 删除中等重要性且老旧的块
        if self.store.total_active_tokens > self.max_tokens:
            old_blocks = self.store.get_old_blocks(max_age_seconds=300)   # 5 分钟
            medium_importance = [b for b in old_blocks if b.importance_score < 0.5]
            for block in medium_importance:
                self.store.remove_block(block.block_id)
            logger.info(
                "[Evictor] L2: removed %d blocks (importance < 0.5, age > 5min)",
                len(medium_importance),
            )

        # L3: 对重要旧块生成摘要
        if self.store.total_active_tokens > self.max_tokens:
            old_important = [
                b for b in self.store.get_old_blocks(300)
                if b.importance_score >= 0.5 and b.summary is None
            ]
            if old_important:
                await self._summarize_blocks(old_important[:5])   # 每次最多摘要 5 个
                logger.info(
                    "[Evictor] L3: summarized %d important old blocks",
                    min(5, len(old_important)),
                )

        logger.info("[Evictor] Eviction complete, tokens: %d", self.store.total_active_tokens)

    async def _summarize_blocks(self, blocks: list[ContextBlock]):
        """为旧块生成摘要并替换原文"""
        for block in blocks:
            try:
                response = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=f"Summarize this in under 50 words: {block.content}",
                )
                summary = response.text.strip()

                # 用摘要替换原内容
                old_tokens = block.token_estimate
                block.summary = summary
                block.content = f"[SUMMARY] {summary}"
                new_tokens = self.store.estimate_token_count(block.content)
                block.token_estimate = new_tokens
                self.store.total_active_tokens -= (old_tokens - new_tokens)

            except Exception as e:
                logger.error("[Evictor] Summarization failed for block %s: %s", block.block_id, e)
