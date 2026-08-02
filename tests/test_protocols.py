"""Tests for aether.protocols and aether.context.block_store — data protocols."""

import time
import pytest
import numpy as np
from unittest.mock import patch, MagicMock

from aether.context.block_store import ContextBlock, ContextBlockStore


class TestContextBlock:
    """Test ContextBlock data class."""

    def test_default_values(self):
        block = ContextBlock()
        assert block.block_id is not None
        assert len(block.block_id) == 8
        assert block.block_type == ""
        assert block.content == ""
        assert block.token_estimate == 0
        assert block.importance_score == 0.5
        assert block.embedding is None
        assert block.summary is None
        assert block.metadata == {}

    def test_custom_values(self):
        block = ContextBlock(
            block_id="test1234",
            block_type="text",
            content="hello world",
            token_estimate=5,
            importance_score=0.8,
            metadata={"source": "test"},
        )
        assert block.block_id == "test1234"
        assert block.block_type == "text"
        assert block.content == "hello world"
        assert block.token_estimate == 5
        assert block.importance_score == 0.8
        assert block.metadata["source"] == "test"

    def test_unique_ids(self):
        b1 = ContextBlock()
        b2 = ContextBlock()
        assert b1.block_id != b2.block_id

    def test_timestamp_auto_set(self):
        before = time.time()
        block = ContextBlock()
        after = time.time()
        assert before <= block.timestamp <= after

    def test_tool_call_block(self):
        block = ContextBlock(
            block_type="tool_call",
            content="execute_tts({'text': 'hello'})",
            token_estimate=10,
            metadata={"function": "execute_tts", "args": {"text": "hello"}},
        )
        assert block.block_type == "tool_call"
        assert block.metadata["function"] == "execute_tts"

    def test_embedding_array(self):
        emb = np.random.randn(768).astype(np.float32)
        block = ContextBlock(embedding=emb)
        assert block.embedding is not None
        assert block.embedding.shape == (768,)


class TestContextBlockStore:
    """Test ContextBlockStore operations."""

    def test_init_defaults(self):
        store = ContextBlockStore()
        assert store.embedding_dim == 768
        assert len(store.blocks) == 0
        assert store.total_active_tokens == 0

    def test_init_custom_dim(self):
        store = ContextBlockStore(embedding_dim=512)
        assert store.embedding_dim == 512

    def test_add_block(self):
        store = ContextBlockStore()
        block = ContextBlock(content="test", token_estimate=10)
        store.add_block(block)
        assert block.block_id in store.blocks
        assert store.total_active_tokens == 10

    def test_add_multiple_blocks(self):
        store = ContextBlockStore()
        b1 = ContextBlock(content="first", token_estimate=5)
        b2 = ContextBlock(content="second", token_estimate=8)
        store.add_block(b1)
        store.add_block(b2)
        assert len(store.blocks) == 2
        assert store.total_active_tokens == 13

    def test_remove_block(self):
        store = ContextBlockStore()
        block = ContextBlock(content="test", token_estimate=10)
        store.add_block(block)
        store.remove_block(block.block_id)
        assert block.block_id not in store.blocks
        assert store.total_active_tokens == 0

    def test_remove_nonexistent_block(self):
        store = ContextBlockStore()
        store.remove_block("nonexistent")  # Should not raise

    def test_get_blocks_by_importance(self):
        store = ContextBlockStore()
        high = ContextBlock(content="important", importance_score=0.9)
        low = ContextBlock(content="trivial", importance_score=0.1)
        store.add_block(high)
        store.add_block(low)
        result = store.get_blocks_by_importance(threshold=0.3)
        assert len(result) == 1
        assert result[0].content == "trivial"

    def test_get_old_blocks(self):
        store = ContextBlockStore()
        old_block = ContextBlock(content="old")
        old_block.timestamp = time.time() - 600  # 10 minutes ago
        new_block = ContextBlock(content="new")
        store.add_block(old_block)
        store.add_block(new_block)
        result = store.get_old_blocks(max_age_seconds=300)
        assert len(result) == 1
        assert result[0].content == "old"

    def test_estimate_token_count(self):
        store = ContextBlockStore()
        # English text
        count = store.estimate_token_count("Hello, how are you?")
        assert count >= 1

    def test_estimate_token_count_chinese(self):
        store = ContextBlockStore()
        count = store.estimate_token_count("你好世界")
        assert count >= 1

    def test_estimate_token_count_empty(self):
        store = ContextBlockStore()
        count = store.estimate_token_count("")
        assert count >= 1  # min 1

    def test_get_summary(self):
        store = ContextBlockStore()
        store.add_block(ContextBlock(content="a", token_estimate=3))
        store.add_block(ContextBlock(content="b", token_estimate=5))
        summary = store.get_summary()
        assert summary["total_blocks"] == 2
        assert summary["total_active_tokens"] == 8
        assert summary["pending_embeddings"] == 2  # no embeddings provided
        assert summary["faiss_size"] == 0

    def test_add_block_with_embedding(self):
        """Blocks with embeddings go to pending (FAISS lazy init)."""
        store = ContextBlockStore()
        emb = np.random.randn(768).astype(np.float32)
        block = ContextBlock(content="emb", token_estimate=5, embedding=emb)
        store.add_block(block)
        assert block.block_id in store.blocks
        # Without faiss installed, _add_to_faiss will try to init and may skip
        # The important thing is the block is stored


class TestContextBlockWithFAISS:
    """Test search_similar with mock FAISS."""

    def test_search_similar_empty_store(self):
        store = ContextBlockStore()
        query = np.random.randn(768).astype(np.float32)
        result = store.search_similar(query, top_k=5)
        assert result == []

    def test_search_similar_no_faiss(self):
        store = ContextBlockStore()
        store._faiss_index = None
        query = np.random.randn(768).astype(np.float32)
        result = store.search_similar(query)
        assert result == []


class TestProtocolsInterface:
    """Test that protocol definitions exist and are importable."""

    def test_protocol_imports(self):
        from aether.protocols import (
            ER2Port,
            MotionPort,
            OSCPort,
            AudioPort,
            TTSPort,
            DispatcherPort,
        )
        # Just verify they're importable (Protocol classes)
        assert ER2Port is not None
        assert MotionPort is not None
        assert OSCPort is not None
        assert AudioPort is not None
        assert TTSPort is not None
        assert DispatcherPort is not None

    def test_er2_port_has_required_methods(self):
        from aether.protocols import ER2Port
        assert hasattr(ER2Port, "connect")
        assert hasattr(ER2Port, "close")
        assert hasattr(ER2Port, "send_audio")
        assert hasattr(ER2Port, "send_text")
        assert hasattr(ER2Port, "receive_loop")

    def test_motion_port_has_required_methods(self):
        from aether.protocols import MotionPort
        assert hasattr(MotionPort, "generate")
        assert hasattr(MotionPort, "health_check")
        assert hasattr(MotionPort, "is_available")

    def test_osc_port_has_required_methods(self):
        from aether.protocols import OSCPort
        assert hasattr(OSCPort, "connect")
        assert hasattr(OSCPort, "play_motion")
        assert hasattr(OSCPort, "stop")
        assert hasattr(OSCPort, "send_idle_pose")


class TestHeartbeatGenerator:
    """Test HeartbeatGenerator pure-function state projection."""

    def test_init(self):
        from aether.context.heartbeat import HeartbeatGenerator
        hb = HeartbeatGenerator(interval_seconds=30)
        assert hb.interval == 30

    def test_should_send_initial(self):
        from aether.context.heartbeat import HeartbeatGenerator
        hb = HeartbeatGenerator(interval_seconds=0)  # 0s for test
        assert hb.should_send() is True

    def test_generate_returns_message(self):
        from aether.context.heartbeat import HeartbeatGenerator, AgentState
        hb = HeartbeatGenerator(interval_seconds=0)
        msg = hb.generate(AgentState())
        assert msg is not None
        assert "WORLD STATE UPDATE" in msg

    def test_generate_deduplicates(self):
        from aether.context.heartbeat import HeartbeatGenerator, AgentState
        hb = HeartbeatGenerator(interval_seconds=0)
        msg1 = hb.generate(AgentState())
        # Same state, same hash -> should return None
        msg2 = hb.generate(AgentState())
        assert msg2 is None

    def test_generate_reflects_state(self):
        from aether.context.heartbeat import HeartbeatGenerator, AgentState
        hb = HeartbeatGenerator(interval_seconds=0)
        state = AgentState(
            current_task="executing: walk forward",
            recent_actions=("walk forward", "wave hand"),
            emotion_state="happy",
        )
        msg = hb.generate(state)
        assert "walk forward" in msg
        assert "happy" in msg

    def test_generate_emits_new_state_after_change(self):
        from aether.context.heartbeat import HeartbeatGenerator, AgentState
        hb = HeartbeatGenerator(interval_seconds=0)
        assert hb.generate(AgentState()) is not None
        changed = AgentState(current_task="executing: dance", recent_actions=("dance",))
        msg2 = hb.generate(changed)
        assert msg2 is not None
        assert "dance" in msg2

    def test_agent_state_defaults(self):
        from aether.context.heartbeat import AgentState
        ts = AgentState()
        assert ts.current_task == "idle"
        assert ts.emotion_state == "neutral"
        assert ts.environment_summary == "Unknown environment"
        assert ts.recent_actions == ()
