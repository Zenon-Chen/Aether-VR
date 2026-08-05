"""SpeechService 的瞬时过载恢复与失败传播。"""

from types import SimpleNamespace

import pytest

from aether.contracts.plan import ActionKind, Plan, PlanStep, StepStatus
from aether.core import Scheduler, StateStore
from aether.infra.config import SpeechConfig
from aether.services.speech.service import SpeechService, SpeechUnavailableError


class _Models:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def generate_content(self, **_kwargs):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(
            parts=[SimpleNamespace(inline_data=SimpleNamespace(data=outcome))]
        ))])


class _Client:
    def __init__(self, outcomes):
        self.models = _Models(outcomes)


def _service(outcomes):
    service = SpeechService(SpeechConfig(api_key="test", fallback_sound="missing.wav"))
    service._client = _Client(outcomes)
    service._healthy = True
    service._fallback = None
    return service


@pytest.mark.asyncio
async def test_503_retries_with_bounded_backoff_and_recovers_health(monkeypatch):
    service = _service([
        RuntimeError("503 UNAVAILABLE: high demand"),
        RuntimeError("503 UNAVAILABLE: high demand"),
        b"\x00\x00",
    ])
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def fake_play(_pcm):
        return 0.1

    monkeypatch.setattr("aether.services.speech.service.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(service, "_play", fake_play)

    assert await service.speak("恢复。") == pytest.approx(0.1)
    assert service._client.models.calls == 3
    assert sleeps == [0.25, 0.75]
    assert await service.health() == "degraded"
    status = await service.status()
    assert status["tts_ready"] is True
    assert status["fallback_ready"] is False


@pytest.mark.asyncio
async def test_tts_failure_plays_fallback_once_and_returns_duration(monkeypatch):
    service = _service([RuntimeError("permanent failure")])
    service._fallback = b"\x01\x00" * 100
    service._fallback_ready = True
    played = []

    async def fake_play(pcm):
        played.append(pcm)
        return 0.2

    monkeypatch.setattr(service, "_play", fake_play)
    assert await service.speak("故障。") == pytest.approx(0.2)
    assert played == [service._fallback]


@pytest.mark.asyncio
async def test_start_rejects_missing_fallback(monkeypatch):
    service = SpeechService(SpeechConfig(api_key="", fallback_sound="missing.wav"))
    monkeypatch.setattr(service, "_resolve_device", lambda: None)
    with pytest.raises(SpeechUnavailableError, match="fallback WAV 不可用"):
        await service.start()


@pytest.mark.asyncio
async def test_all_segments_failed_without_fallback_raises_retryable_error(monkeypatch):
    # 两段，每段最多三次；证明重试有界，且不会静默登记成功。
    service = _service([RuntimeError("503 UNAVAILABLE") for _ in range(6)])

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("aether.services.speech.service.asyncio.sleep", fake_sleep)

    with pytest.raises(SpeechUnavailableError, match="不可用|播放失败"):
        await service.speak("第一段。第二段。")

    assert service._client.models.calls == 3
    assert await service.health() == "degraded"


@pytest.mark.asyncio
async def test_scheduler_retries_when_speech_produces_nothing(monkeypatch):
    # Service 内三次 + Scheduler 一次机械重试，共六次后计划必须失败。
    service = _service([RuntimeError("503 UNAVAILABLE") for _ in range(6)])

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("aether.services.speech.service.asyncio.sleep", fake_sleep)
    scheduler = Scheduler(StateStore())

    async def speak_executor(step):
        await service.speak(step.args["text"])
        return "spoken"

    scheduler.register(ActionKind.SPEAK, speak_executor)
    result = await scheduler.submit(Plan(steps=(
        PlanStep(1, ActionKind.SPEAK, {"text": "一句话"}),
    )))

    assert result.status is StepStatus.FAILED
    assert service._client.models.calls == 6
    assert "fallback WAV 不可用" in result.detail


@pytest.mark.asyncio
async def test_playback_failure_does_not_count_as_success(monkeypatch):
    service = _service([b"\x00\x00"])

    async def failed_play(_pcm):
        service._healthy = False
        return 0.0

    monkeypatch.setattr(service, "_play", failed_play)

    with pytest.raises(SpeechUnavailableError, match="不可用|播放失败"):
        await service.speak("播放失败。")
    assert await service.health() == "degraded"
