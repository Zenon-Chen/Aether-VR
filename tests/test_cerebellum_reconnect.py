"""ER2 Live 会话关闭与重连行为。"""

import asyncio
from types import SimpleNamespace

import pytest

from aether.services.cerebellum.service import CerebellumService


class _TurnEndedSession:
    def __init__(self, stop_after: int = 2):
        self.calls = 0
        self.stop_after = stop_after
        self.owner = None

    def receive(self):
        async def messages():
            self.calls += 1
            await asyncio.sleep(0)
            if self.calls >= self.stop_after:
                self.owner._stopping = True
            if False:
                yield None
        return messages()


@pytest.mark.asyncio
async def test_receive_turn_end_does_not_reconnect(monkeypatch):
    """Live SDK 的 receive() 每个 turn 完成都会正常返回，不应误判为断线。"""
    service = CerebellumService(SimpleNamespace())
    session = _TurnEndedSession()
    session.owner = service
    service._session = session
    service._connected = True
    service._stopping = False
    reconnect_calls = 0

    async def reconnect(_generation=None):
        nonlocal reconnect_calls
        reconnect_calls += 1

    monkeypatch.setattr(service, "_reconnect", reconnect)
    await service.receive_loop()

    assert session.calls == 2
    assert reconnect_calls == 0

@pytest.mark.asyncio
async def test_send_failure_starts_only_one_reconnect_and_other_sends_drop(monkeypatch):
    """首个旧 session 发送失败后，后续并发发送不再命中它。"""
    service = CerebellumService(SimpleNamespace())
    reconnect_started = asyncio.Event()
    release_reconnect = asyncio.Event()
    reconnect_calls = 0

    class BrokenSession:
        def __init__(self):
            self.send_calls = 0

        async def send_realtime_input(self, **_kwargs):
            self.send_calls += 1
            raise RuntimeError("sent 1000 (OK); then received 1000 (OK)")

    old_session = BrokenSession()
    service._session = old_session
    service._connected = True
    service._stopping = False

    async def reconnect(_generation=None):
        nonlocal reconnect_calls
        reconnect_calls += 1
        reconnect_started.set()
        await release_reconnect.wait()

    monkeypatch.setattr(service, "_reconnect", reconnect)
    chunk = SimpleNamespace(pcm=b"pcm", sample_rate=16000)
    frame = SimpleNamespace(jpeg=b"jpeg")

    await asyncio.gather(
        service.send_audio(chunk),
        service.send_video(frame),
        service.send_heartbeat("[HEARTBEAT]"),
    )
    await asyncio.wait_for(reconnect_started.wait(), timeout=0.2)

    assert old_session.send_calls == 1
    assert reconnect_calls == 1
    assert service._session is None
    assert not service.connected

    release_reconnect.set()
    await service._reconnect_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_message", "token_after"),
    [
        ("received 1007 (invalid frame payload data)", None),
        ("Request contains an invalid argument", None),
        ("received 1006 (connection closed abnormally)", "resume-me"),
        ("network unreachable", "resume-me"),
    ],
)
async def test_send_failure_only_discards_invalid_resumption_handle(
    monkeypatch, error_message, token_after
):
    service = CerebellumService(SimpleNamespace())
    service._resumption_token = "resume-me"
    service._session = object()
    service._connected = True
    monkeypatch.setattr(service, "_ensure_reconnect", lambda _generation: None)

    async def fail(_session):
        raise RuntimeError(error_message)

    await service._send(fail)

    assert service._resumption_token == token_after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_message", "token_after"),
    [
        ("received 1007; invalid frame payload", None),
        ("Request contains an invalid argument", None),
        ("received 1006 (connection closed abnormally)", "resume-me"),
    ],
)
async def test_receive_failure_only_discards_invalid_resumption_handle(
    monkeypatch, error_message, token_after
):
    service = CerebellumService(SimpleNamespace())
    service._resumption_token = "resume-me"
    service._connected = True

    class BrokenSession:
        def receive(self):
            async def messages():
                raise RuntimeError(error_message)
                yield  # pragma: no cover

            return messages()

    service._session = BrokenSession()

    async def reconnect(_generation=None):
        service._stopping = True

    monkeypatch.setattr(service, "_reconnect", reconnect)
    await service.receive_loop()

    assert service._resumption_token == token_after


@pytest.mark.asyncio
async def test_sends_are_silent_while_disconnected(caplog):
    service = CerebellumService(SimpleNamespace())
    service._session = SimpleNamespace()
    service._connected = False

    await service.send_audio(SimpleNamespace(pcm=b"pcm", sample_rate=16000))
    await service.send_video(SimpleNamespace(jpeg=b"jpeg"))
    await service.send_heartbeat("heartbeat")
    await service.send_tool_response([object()])

    assert caplog.records == []
