"""上下文子系统端口 — 存储与协作者的抽象边界

BlockRepository 抽象了上下文块存储（FAISS + tiktoken 为一种实现），
ImportanceScorer / ContextEvictor / RAGRetriever 只依赖此端口，
测试时可注入 InMemory 实现（见 memory.py），无需 FAISS/tiktoken。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from .block_store import ContextBlock


@runtime_checkable
class BlockRepository(Protocol):
    """上下文块存储端口

    注意：属性用注解声明而非 @property——
    若用 @property，实现类继承后会获得只读 descriptor，
    覆盖其普通实例属性（如 self.blocks = {}）。
    """

    total_active_tokens: int
    blocks: dict

    def add_block(self, block: ContextBlock) -> None: ...

    def remove_block(self, block_id: str) -> None: ...

    def search_similar(
        self, query_embedding: np.ndarray, top_k: int = 5
    ) -> list[tuple[ContextBlock, float]]: ...

    def get_blocks_by_importance(self, threshold: float = 0.3) -> list[ContextBlock]: ...

    def get_old_blocks(self, max_age_seconds: float = 300) -> list[ContextBlock]: ...

    def estimate_token_count(self, text: str) -> int: ...
