import hashlib
from types import SimpleNamespace

import pytest

from aether.infra.config import BrainConfig, ScribeConfig
from aether.services.brain.service import BrainService, _load_persona
from aether.services.scribe import ScribeService


def test_default_persona_is_versioned_stable_and_contains_contract():
    service = BrainService(BrainConfig())
    persona_id, version, digest, instruction = _load_persona(service._config.persona_path)
    raw = open(service._config.persona_path, "rb").read()
    assert (persona_id, version) == ("aether-default", "1.0.0")
    assert digest == hashlib.sha256(raw).hexdigest()
    assert "我是 Aether" in instruction
    assert "固定输出契约" in instruction
    assert '"wake_er2"' in instruction


@pytest.mark.asyncio
async def test_pending_context_is_visible_before_batch_full(tmp_path):
    scribe = ScribeService(ScribeConfig(enabled=True, batch_size=10, db_path=str(tmp_path / "m.db")))
    await scribe.start()
    await scribe.note("user", "用户说：请记住蓝色")
    assert scribe.context_tokens > 0
    assert "请记住蓝色" in scribe.render_active_context()
    assert scribe.metrics["pending_blocks"] == 1
    await scribe.stop()


@pytest.mark.asyncio
async def test_normal_block_renders_and_counts_summary_not_original(tmp_path):
    scribe = ScribeService(ScribeConfig(enabled=True, batch_size=1, db_path=str(tmp_path / "m.db")))
    async def evaluate(_blocks):
        return [{"importance": "normal", "summary": "短摘要"}]
    scribe.set_evaluator(evaluate)
    await scribe.start()
    await scribe.note("text", "这是一段绝对不应继续出现在活动上下文中的很长原文")
    rendered = scribe.render_active_context()
    assert "短摘要" in rendered
    assert "绝对不应" not in rendered
    assert scribe.context_tokens <= 4
    await scribe.stop()


@pytest.mark.asyncio
async def test_important_eviction_is_value_based_not_fifo(tmp_path):
    scribe = ScribeService(ScribeConfig(
        enabled=True, batch_size=1, db_path=str(tmp_path / "m.db"),
        context_limit_tokens=20, evict_target_tokens=14,
    ))
    async def important(_blocks):
        return [{"importance": "important"}]
    scribe.set_evaluator(important)
    await scribe.start()
    await scribe.note("text", "请记住我答应永远喜欢蓝色")
    await scribe.note("text", "一段普通但足够长的过程信息")
    assert "喜欢蓝色" in scribe.render_active_context()
    rows = scribe._db.execute("SELECT content, summary FROM memory WHERE kind='evicted_text'").fetchall()
    assert rows and "普通" in rows[-1][0] and rows[-1][1]
    await scribe.stop()


@pytest.mark.asyncio
async def test_render_active_context_prioritizes_newest_and_pending_blocks(tmp_path):
    scribe = ScribeService(ScribeConfig(
        enabled=True, batch_size=2, db_path=str(tmp_path / "newest.db"),
    ))

    async def important(_blocks):
        return [{"importance": "important"}, {"importance": "important"}]

    scribe.set_evaluator(important)
    await scribe.start()
    await scribe.note("user", "很早以前的第一条超长历史记录内容" * 4)
    await scribe.note("user", "中间的第二条记录")
    await scribe.note("user", "刚刚发生的最新待处理消息")

    # 限制极小 token 窗口时，最新 pending 消息必须可见，最旧记录被裁剪
    rendered = scribe.render_active_context(max_tokens=24)
    assert "刚刚发生的最新待处理消息" in rendered
    assert "很早以前的第一条" not in rendered
    await scribe.stop()


@pytest.mark.asyncio
async def test_heuristic_drops_ack_tool_notes(tmp_path):
    scribe = ScribeService(ScribeConfig(
        enabled=True, batch_size=2, db_path=str(tmp_path / "ack.db"),
    ))
    await scribe.start()
    await scribe.note("tool", "ack({})")
    await scribe.note("user", "我叫小明")
    assert len(scribe._blocks) == 1
    assert "小明" in scribe.render_active_context()
    await scribe.stop()
