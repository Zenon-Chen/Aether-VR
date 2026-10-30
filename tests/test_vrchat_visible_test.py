import asyncio

from aether.infra.config import load_config
from aether.runtime import vrchat_test


def test_action_sequence_and_config():
    config = load_config("config.yaml")
    assert vrchat_test.validate_config(config) == []
    assert vrchat_test.ACTIONS == (
        "wave", "nod", "shake_head", "clap", "point", "think", "bow", "sit", "stand"
    )


def test_dry_run_sends_no_osc(capsys):
    config = load_config("config.yaml")
    asyncio.run(vrchat_test.run(config, dry_run=True))
    output = capsys.readouterr().out
    assert "DRY-RUN: 未发送 OSC" in output
    assert "当前动作" not in output


def test_runner_importable():
    assert callable(vrchat_test.main)
