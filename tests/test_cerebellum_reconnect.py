"""ER2 Live 会话关闭与重连行为。"""

import asyncio
from types import SimpleNamespace

import pytest

from aether.services.cerebellum.service import CerebellumService


class _CleanlyClosedSession:
    def receive(self):
        async def messages():
            await asyncio.sleep(0)
            if False:
                yield None
        return messages()


@pytest.mark.asyncio
async def test_clean_receive_close_reconnects(monkeypatch):
    """1000 OK 会让 receive() 正常结束，也必须触发一次重连。"""
    service = CerebellumService(SimpleNamespace())
    old_session = _CleanlyClosedSession()
    service._session = old_session
    service._connected = True
    service._stopping = False
    reconnected = asyncio.Event()

    async def reconnect(_generation=None):
        service._session = SimpleNamespace()
        service._connected = True
        reconnected.set()

    monkeypatch.setattr(service, "_reconnect", reconnect)
    task = asyncio.create_task(service.receive_loop())
    await asyncio.wait_for(reconnected.wait(), timeout=0.2)

    service._stopping = True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


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
async def test_sends_are_silent_while_disconnected(caplog):
    service = CerebellumService(SimpleNamespace())
    service._session = SimpleNamespace()
    service._connected = False

    await service.send_audio(SimpleNamespace(pcm=b"pcm", sample_rate=16000))
    await service.send_video(SimpleNamespace(jpeg=b"jpeg"))
    await service.send_heartbeat("heartbeat")
    await service.send_tool_response([object()])

    assert caplog.records == []
