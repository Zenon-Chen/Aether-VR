"""Brain quota cooldown behavior."""

import logging
from types import SimpleNamespace

import pytest

from aether.infra.config import BrainConfig, load_config
from aether.services.brain.service import BrainService


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def genai_types(monkeypatch):
    monkeypatch.setattr(
        "aether.services.brain.service.types",
        SimpleNamespace(GenerateContentConfig=lambda **kwargs: kwargs),
    )


def make_service(error: str):
    calls = {"count": 0}

    class Models:
        def generate_content(self, **_kwargs):
            calls["count"] += 1
            raise RuntimeError(error)

    service = BrainService(SimpleNamespace(enabled=True, model="model", max_output_tokens=10))
    service._client = SimpleNamespace(models=Models())
    return service, calls


@pytest.mark.asyncio
async def test_daily_quota_uses_six_hour_cooldown_and_blocks_both_paths(
    monkeypatch, genai_types, caplog
):
    clock = FakeClock()
    monkeypatch.setattr("aether.services.brain.service.time.monotonic", clock.monotonic)
    service, calls = make_service(
        "429 RESOURCE_EXHAUSTED quotaId "
        "GenerateRequestsPerDayPerProjectPerModel-FreeTier quotaValue=500"
    )

    with caplog.at_level(logging.WARNING):
        assert await service.observe("facts") is None
        assert calls["count"] == 1
        assert service.cooldown_reason == "daily_quota_exhausted"
        assert service.cooldown_remaining_s == pytest.approx(6 * 60 * 60)
        assert await service.health() == "degraded"

        clock.now += 61
        assert await service.observe("facts") is None
        assert await service.evaluate_memory([{"content": "memory"}]) == []

    assert calls["count"] == 1
    assert sum("每日免费额度已耗尽" in record.message for record in caplog.records) == 1


@pytest.mark.asyncio
async def test_short_429_retries_after_one_minute(monkeypatch, genai_types):
    clock = FakeClock()
    monkeypatch.setattr("aether.services.brain.service.time.monotonic", clock.monotonic)
    service, calls = make_service("429 RESOURCE_EXHAUSTED requests per minute")

    assert await service.observe("facts") is None
    assert service.cooldown_reason == "short_quota_exhausted"
    assert service.cooldown_remaining_s == pytest.approx(60)
    assert calls["count"] == 1

    clock.now += 59
    assert await service.observe("facts") is None
    assert calls["count"] == 1
    clock.now += 1
    assert service.cooldown_reason is None
    assert await service.observe("facts") is None
    assert calls["count"] == 2


def test_brain_interval_preserves_two_hour_daily_budget():
    default = BrainConfig()
    configured = load_config("config.yaml").brain

    assert default.interval_s == 60
    assert configured.interval_s == 60
    assert 2 * 60 * 60 / configured.interval_s == 120
    assert 2 * 60 * 60 / configured.interval_s < 500
