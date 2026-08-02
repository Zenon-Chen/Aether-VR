"""内存版上下文块存储 — BlockRepository 的轻量实现

不依赖 FAISS/tiktoken：嵌入用 numpy 暴力余弦相似度，token 用简单估算。
用于单元测试、无 GPU 环境演示，或作为持久化实现之前的占位。
"""

import time
from typing import Optional

import numpy as np

from .block_store import ContextBlock


class InMemoryBlockRepository:
    """基于 dict + numpy 的 BlockRepository 实现（无外部依赖）"""

    def __init__(self, embedding_dim: int = 768):
        self.embedding_dim = embedding_dim
        self._blocks: dict[str, ContextBlock] = {}
        self._total_tokens: int = 0

    # ── 端口实现 ──────────────────────────────────

    @property
    def total_active_tokens(self) -> int:
        return self._total_tokens

    @property
    def blocks(self) -> dict:
        return self._blocks

    def add_block(self, block: ContextBlock) -> None:
        self._blocks[block.block_id] = block
        self._total_tokens += block.token_estimate

    def remove_block(self, block_id: str) -> None:
        block = self._blocks.pop(block_id, None)
        if block is not None:
            self._total_tokens -= block.token_estimate

    def search_similar(
        self, query_embedding: np.ndarray, top_k: int = 5
    ) -> list[tuple[ContextBlock, float]]:
        """余弦相似度暴力搜索（内存版，数据量小时足够）"""
        q = query_embedding.astype(np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm > 0:
            q = q / q_norm

        scored: list[tuple[ContextBlock, float]] = []
        for block in self._blocks.values():
            if block.embedding is None:
                continue
            vec = block.embedding.astype(np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            scored.append((block, float(np.dot(q, vec))))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def get_blocks_by_importance(self, threshold: float = 0.3) -> list[ContextBlock]:
        return [b for b in self._blocks.values() if b.importance_score < threshold]

    def get_old_blocks(self, max_age_seconds: float = 300) -> list[ContextBlock]:
        cutoff = time.time() - max_age_seconds
        return [b for b in self._blocks.values() if b.timestamp < cutoff]

    def estimate_token_count(self, text: str) -> int:
        """简单估算：中文按 2 字/token，其他按 4 字符/token"""
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - chinese_chars
        return max(1, chinese_chars // 2 + other_chars // 4)
