"""Tests for aether.config — configuration management."""

import os
import pytest
from pydantic import ValidationError

from aether.config import (
    AetherConfig,
    ER2Config,
    TTSConfig,
    DARTConfig,
    OSCConfig,
    AudioConfig,
    ContextConfig,
    LogConfig,
    load_config,
)


class TestAetherConfigDefaults:
    """Test AetherConfig default values."""

    def test_default_er2_model(self):
        cfg = AetherConfig()
        assert cfg.er2.model == "gemini-robotics-er-2-streaming-preview"

    def test_default_tts_voice(self):
        cfg = AetherConfig()
        assert cfg.tts.voice_name == "Kore"

    def test_default_dart_port(self):
        cfg = AetherConfig()
        assert cfg.dart.port == 8900

    def test_default_osc_port(self):
        cfg = AetherConfig()
        assert cfg.osc.port == 9000

    def test_default_audio_sample_rate(self):
        cfg = AetherConfig()
        assert cfg.audio.input_sample_rate == 16000
        assert cfg.audio.output_sample_rate == 24000

    def test_default_context_embedding_dim(self):
        cfg = AetherConfig()
        assert cfg.context.embedding_dim == 768

    def test_default_log_level(self):
        cfg = AetherConfig()
        assert cfg.log.level == "INFO"

    def test_all_sub_configs_present(self):
        cfg = AetherConfig()
        assert isinstance(cfg.er2, ER2Config)
        assert isinstance(cfg.tts, TTSConfig)
        assert isinstance(cfg.dart, DARTConfig)
        assert isinstance(cfg.osc, OSCConfig)
        assert isinstance(cfg.audio, AudioConfig)
        assert isinstance(cfg.context, ContextConfig)
        assert isinstance(cfg.log, LogConfig)


class TestAetherConfigFromDict:
    """Test creating config from dict (as YAML loader would)."""

    def test_partial_er2_override(self):
        cfg = AetherConfig(er2={"model": "custom-model", "api_key": "k"})
        assert cfg.er2.model == "custom-model"
        assert cfg.er2.api_key == "k"
        # Other fields keep defaults
        assert cfg.er2.context_window_tokens == 128000

    def test_partial_osc_override(self):
        cfg = AetherConfig(osc={"port": 7777})
        assert cfg.osc.port == 7777
        assert cfg.osc.host == "127.0.0.1"  # default preserved

    def test_full_nested_dict(self):
        raw = {
            "er2": {"model": "m", "api_key": "k"},
            "dart": {"host": "192.168.1.10", "port": 9999},
        }
        cfg = AetherConfig(**raw)
        assert cfg.er2.model == "m"
        assert cfg.dart.host == "192.168.1.10"
        assert cfg.dart.port == 9999


class TestER2Config:
    """Test ER2Config sub-config."""

    def test_default_response_modalities(self):
        cfg = ER2Config()
        assert cfg.response_modalities == ["TEXT"]

    def test_api_key_from_env(self):
        # conftest.py sets GEMINI_API_KEY
        cfg = ER2Config()
        assert cfg.api_key == "test-dummy-key-for-unit-tests"

    def test_explicit_api_key_overrides_env(self):
        cfg = ER2Config(api_key="explicit-key")
        assert cfg.api_key == "explicit-key"

    def test_context_window_tokens(self):
        cfg = ER2Config(context_window_tokens=64000)
        assert cfg.context_window_tokens == 64000


class TestContextConfig:
    """Test ContextConfig sub-config."""

    def test_defaults(self):
        cfg = ContextConfig()
        assert cfg.embedding_model == "gemini-embedding-001"
        assert cfg.max_active_tokens == 100000
        assert cfg.heartbeat_interval == 30
        assert cfg.scoring_batch_size == 10

    def test_custom_values(self):
        cfg = ContextConfig(embedding_dim=512, heartbeat_interval=60)
        assert cfg.embedding_dim == 512
        assert cfg.heartbeat_interval == 60


class TestConfigValidation:
    """Test Pydantic validation on invalid values."""

    def test_invalid_dart_port_type(self):
        with pytest.raises(ValidationError):
            AetherConfig(dart={"port": "not-a-number"})

    def test_invalid_audio_sample_rate_type(self):
        with pytest.raises(ValidationError):
            AetherConfig(audio={"input_sample_rate": "bad"})

    def test_invalid_osc_port_type(self):
        with pytest.raises(ValidationError):
            AetherConfig(osc={"port": [9000]})


class TestLoadConfig:
    """Test load_config with missing file and env-based key."""

    def test_load_config_missing_file_uses_defaults(self, tmp_path):
        fake_path = str(tmp_path / "nonexistent.yaml")
        # Should not raise because GEMINI_API_KEY is set
        cfg = load_config(fake_path)
        assert isinstance(cfg, AetherConfig)
        assert cfg.er2.api_key == "test-dummy-key-for-unit-tests"

    def test_load_config_from_yaml(self, tmp_path):
        yaml_content = (
            "er2:\n"
            "  model: yaml-model\n"
            "  api_key: yaml-key\n"
            "dart:\n"
            "  port: 1234\n"
        )
        yaml_file = tmp_path / "test_config.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")
        cfg = load_config(str(yaml_file))
        assert cfg.er2.model == "yaml-model"
        assert cfg.er2.api_key == "yaml-key"
        assert cfg.dart.port == 1234

    def test_load_config_raises_without_api_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        fake_path = str(tmp_path / "nonexistent.yaml")
        with pytest.raises(ValueError, match="API Key is required"):
            load_config(fake_path)
