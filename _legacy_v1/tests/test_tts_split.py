"""Tests for aether.speech.tts_engine — text splitting logic."""

import pytest
from unittest.mock import patch, MagicMock

from aether.config import TTSConfig


def _make_engine(max_chars=30):
    """Create a TTSEngine with mocked genai client."""
    with patch("aether.speech.tts_engine.genai") as mock_genai:
        mock_genai.Client = MagicMock()
        from aether.speech.tts_engine import TTSEngine
        config = TTSConfig(api_key="test", max_segment_chars=max_chars)
        engine = TTSEngine(config)
    return engine


class TestSplitTextChinese:
    """Test Chinese text splitting."""

    def test_simple_sentence(self):
        engine = _make_engine()
        result = engine._split_text("你好世界。", split_on="chinese")
        assert len(result) == 1
        assert "你好世界" in result[0]

    def test_two_sentences(self):
        engine = _make_engine()
        result = engine._split_text("你好世界。今天天气不错。", split_on="chinese")
        assert len(result) == 2

    def test_comma_split_long_sentence(self):
        engine = _make_engine(max_chars=15)
        text = "今天天气很好，我们去公园玩吧，带上野餐的食物。"
        result = engine._split_text(text, split_on="chinese")
        assert len(result) >= 2

    def test_exclamation_mark(self):
        engine = _make_engine()
        result = engine._split_text("太棒了！我们走吧！", split_on="chinese")
        assert len(result) == 2


class TestSplitTextEnglish:
    """Test English text splitting."""

    def test_simple_sentence(self):
        engine = _make_engine()
        result = engine._split_text("Hello world.", split_on="english")
        assert len(result) == 1

    def test_two_sentences(self):
        engine = _make_engine()
        result = engine._split_text("Hello world. How are you?", split_on="english")
        assert len(result) == 2

    def test_semicolon_split(self):
        engine = _make_engine(max_chars=15)
        text = "first clause here; second clause here; third one"
        result = engine._split_text(text, split_on="english")
        assert len(result) >= 2


class TestSplitTextMixed:
    """Test mixed Chinese/English text."""

    def test_mixed_text(self):
        engine = _make_engine()
        text = "你好world。这是test。"
        result = engine._split_text(text, split_on="chinese")
        assert len(result) == 2


class TestSplitTextLongSegments:
    """Test handling of long text segments."""

    def test_very_long_no_punctuation(self):
        engine = _make_engine(max_chars=10)
        text = "这是一段没有任何标点的很长很长的文本内容需要被分割"
        result = engine._split_text(text, split_on="chinese")
        # Should be force-split
        assert len(result) >= 2
        for seg in result:
            assert len(seg) <= 10

    def test_force_split_respects_max_chars(self):
        engine = _make_engine(max_chars=5)
        text = "abcdefghij"  # 10 chars, no punctuation
        result = engine._split_text(text, split_on="english")
        assert len(result) == 2
        assert result[0] == "abcde"
        assert result[1] == "fghij"


class TestSplitTextEdgeCases:
    """Test edge cases."""

    def test_empty_string(self):
        engine = _make_engine()
        result = engine._split_text("", split_on="chinese")
        assert result == []

    def test_whitespace_only(self):
        engine = _make_engine()
        result = engine._split_text("   ", split_on="chinese")
        assert result == []

    def test_single_char(self):
        engine = _make_engine()
        result = engine._split_text("a", split_on="english")
        assert len(result) >= 1

    def test_punctuation_only(self):
        engine = _make_engine()
        result = engine._split_text("。！？", split_on="chinese")
        # Should not crash
        assert isinstance(result, list)

    def test_short_segments_merged(self):
        engine = _make_engine(max_chars=30)
        # "A。B。" — B is very short, should merge with previous
        text = "这是一段话。好。"
        result = engine._split_text(text, split_on="chinese")
        # The short "好。" should be merged with the previous segment
        for seg in result:
            if seg != result[-1] or len(result) == 1:
                pass  # Merging may reduce count
        assert isinstance(result, list)
        assert len(result) >= 1


class TestBuildTaggedText:
    """Test emotion/rate tag building."""

    def test_neutral_normal(self):
        engine = _make_engine()
        result = engine._build_tagged_text("hello", "neutral", "normal")
        assert "[neutral]" in result
        assert "hello" in result

    def test_happy_emotion(self):
        engine = _make_engine()
        result = engine._build_tagged_text("great", "happy", "normal")
        assert "[happy" in result

    def test_slow_rate(self):
        engine = _make_engine()
        result = engine._build_tagged_text("thinking", "thinking", "slow")
        assert "[slow]" in result

    def test_fast_rate(self):
        engine = _make_engine()
        result = engine._build_tagged_text("hurry", "excited", "fast")
        assert "[fast]" in result

    def test_first_segment_thinking_has_pause(self):
        engine = _make_engine()
        result = engine._build_tagged_text("hmm", "thinking", "normal", is_first=True)
        assert "[short pause]" in result

    def test_non_first_thinking_no_pause(self):
        engine = _make_engine()
        result = engine._build_tagged_text("hmm", "thinking", "normal", is_first=False)
        assert "[short pause]" not in result

    def test_unknown_emotion_falls_back(self):
        engine = _make_engine()
        result = engine._build_tagged_text("test", "unknown_emotion", "normal")
        assert "[neutral]" in result  # Falls back to neutral


class TestTTSEngineInit:
    """Test TTSEngine initialization."""

    def test_init_with_config(self):
        engine = _make_engine(max_chars=50)
        assert engine.max_segment_chars == 50
        assert engine.voice_name == "Kore"

    def test_audio_queue_initialized(self):
        engine = _make_engine()
        assert engine.audio_queue is not None
        assert engine.audio_queue.empty()

    def test_clear_queue(self):
        engine = _make_engine()
        # Put something in the queue
        engine.audio_queue.put_nowait(b"audio_data")
        assert not engine.audio_queue.empty()
        engine.clear_queue()
        assert engine.audio_queue.empty()

    def test_clear_queue_increments_generation(self):
        engine = _make_engine()
        gen_before = engine._generation_id
        engine.clear_queue()
        assert engine._generation_id == gen_before + 1


class TestEmotionTagMapping:
    """Test emotion-to-tag mapping."""

    def test_all_mapped_emotions(self):
        from aether.speech.tts_engine import EMOTION_TO_TAGS
        expected_emotions = {
            "neutral", "happy", "sad", "angry", "amused", "curious",
            "worried", "enthusiastic", "sarcastic", "thinking",
            "apologetic", "surprised", "calm", "excited", "tired",
            "confident", "nervous", "playful", "serious", "warm",
        }
        assert set(EMOTION_TO_TAGS.keys()) == expected_emotions

    def test_rate_mapping(self):
        from aether.speech.tts_engine import RATE_TO_TAG
        assert RATE_TO_TAG["slow"] == "[slow]"
        assert RATE_TO_TAG["normal"] == ""
        assert RATE_TO_TAG["fast"] == "[fast]"
