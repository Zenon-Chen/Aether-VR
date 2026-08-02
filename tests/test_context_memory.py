"""Tests for aether.context.memory and aether.context.ports — InMemory repository."""

import time
import pytest
import numpy as np

from aether.context.block_store import ContextBlock
from aether.context.memory import InMemoryBlockRepository
from aether.context.ports import BlockRepository


class TestInMemoryBlockRepository:
    """Test InMemoryBlockRepository implements BlockRepository."""

    def test_implements_protocol(self):
        repo = InMemoryBlockRepository()
        assert isinstance(repo, BlockRepository)

    def test_add_and_count_tokens(self):
        repo = InMemoryBlockRepository()
        block = ContextBlock(content="hello", token_estimate=5)
        repo.add_block(block)
        assert len(repo.blocks) == 1
        assert repo.total_active_tokens == 5

    def test_remove_block(self):
        repo = InMemoryBlockRepository()
        block = ContextBlock(content="hello", token_estimate=5)
        repo.add_block(block)
        repo.remove_block(block.block_id)
        assert len(repo.blocks) == 0
        assert repo.total_active_tokens == 0

    def test_remove_nonexistent_is_safe(self):
        repo = InMemoryBlockRepository()
        repo.remove_block("missing")  # should not raise

    def test_search_similar_ranks_by_cosine(self):
        repo = InMemoryBlockRepository()
        block_a = ContextBlock(content="cat", embedding=np.array([1.0, 0.0], dtype=np.float32))
        block_b = ContextBlock(content="dog", embedding=np.array([0.0, 1.0], dtype=np.float32))
        repo.add_block(block_a)
        repo.add_block(block_b)

        results = repo.search_similar(np.array([1.0, 0.0], dtype=np.float32), top_k=2)
        assert len(results) == 2
        assert results[0][0].block_id == block_a.block_id  # 最相似排第一

    def test_search_similar_empty_store(self):
        repo = InMemoryBlockRepository()
        assert repo.search_similar(np.zeros(4, dtype=np.float32)) == []

    def test_get_blocks_by_importance(self):
        repo = InMemoryBlockRepository()
        low = ContextBlock(content="low", importance_score=0.1)
        high = ContextBlock(content="high", importance_score=0.9)
        repo.add_block(low)
        repo.add_block(high)
        low_blocks = repo.get_blocks_by_importance(0.3)
        assert low_blocks == [low]

    def test_get_old_blocks(self):
        repo = InMemoryBlockRepository()
        old = ContextBlock(content="old", timestamp=time.time() - 1000)
        new = ContextBlock(content="new", timestamp=time.time())
        repo.add_block(old)
        repo.add_block(new)
        old_blocks = repo.get_old_blocks(max_age_seconds=300)
        assert old_blocks == [old]

    def test_estimate_token_count(self):
        repo = InMemoryBlockRepository()
        assert repo.estimate_token_count("hello") >= 1
        assert repo.estimate_token_count("你好世界") >= 1

    def test_works_with_scorer_and_evictor(self):
        """三件套依赖 BlockRepository 端口，可脱离 FAISS/tiktoken 运行"""
        from aether.config import ContextConfig
        from aether.context.importance_scorer import ImportanceScorer
        from aether.context.evictor import ContextEvictor
        from unittest.mock import patch

        repo = InMemoryBlockRepository()
        config = ContextConfig()

        with patch("aether.context.importance_scorer.genai") as mock_genai, \
             patch("aether.context.evictor.genai"):
            mock_genai.Client = lambda **kw: None
            scorer = ImportanceScorer(config, repo, api_key="test")
            evictor = ContextEvictor(config, repo, api_key="test")

            # 规则评分（report_status → 0.2）
            block = ContextBlock(block_type="tool_call", content="report_status(...)",
                                 token_estimate=10)
            repo.add_block(block)
            scorer.queue_for_scoring(block)
            assert scorer.should_score() is False  # 未达批量阈值

            # 驱逐器能看到 store token 数
            assert evictor.should_evict() is False  # 10 < 102400
