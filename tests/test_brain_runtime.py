"""Brain quota cooldown behavior."""

from types import SimpleNamespace

import pytest

from aether.services.brain.service import BrainService


@pytest.mark.asyncio
async def test_quota_cooldown_prevents_observe_and_memory_requests(monkeypatch):
    service = BrainService(SimpleNamespace(enabled=True, model="model", max_output_tokens=10))
    calls = 0

    class Models:
        def generate_content(self, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    service._client = SimpleNamespace(models=Models())
    monkeypatch.setattr("aether.services.brain.service.types", SimpleNamespace(
        GenerateContentConfig=lambda **kwargs: kwargs,
    ))

    assert await service.observe("facts") is None
    assert calls == 1
    assert await service.observe("facts") is None
    assert await service.evaluate_memory([{"content": "memory"}]) == []
    assert calls == 1
    assert await service.health() == "degraded"
