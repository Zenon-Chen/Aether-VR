from pathlib import Path

from aether.infra.config import load_config


def test_resource_paths_resolve_relative_to_config_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.yaml").write_text(
        """
brain:
  persona_path: docs/persona.yaml
speech:
  fallback_sound: assets/filler.wav
scribe:
  db_path: data/memory.sqlite3
log:
  file: logs/aether.log
""".strip(),
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = load_config(project / "config.yaml")

    assert Path(config.brain.persona_path) == project / "docs/persona.yaml"
    assert Path(config.speech.fallback_sound) == project / "assets/filler.wav"
    assert Path(config.scribe.db_path) == project / "data/memory.sqlite3"
    assert Path(config.log.file) == project / "logs/aether.log"
