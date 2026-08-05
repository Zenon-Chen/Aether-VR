from __future__ import annotations

import asyncio
import socket
import time

from aether.contracts.frame import DevicePose, PostureFrame
from aether.infra.config import DartConfig, PostureConfig
from aether.services.posture.service import (
    PostureMode, PostureService, _apply_scope, _with_repeat,
)


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_natural_language_commas_are_not_dart_segment_delimiters() -> None:
    prompt = (
        "clutch hair and head in frustration, then shake head, "
        "then throw hands down in anger"
    )
    result = _with_repeat(prompt, 4.0)

    assert "," not in result
    assert result.endswith("*8")
    assert "frustration and then shake head" in result


def test_motion_scope_selects_three_or_six_generated_devices() -> None:
    current = PostureFrame(tuple(DevicePose(x=float(i)) for i in range(6)))
    generated = PostureFrame(tuple(DevicePose(x=float(i + 10)) for i in range(6)))

    upper = _apply_scope([generated], current, full_body=False)[0]
    full = _apply_scope([generated], current, full_body=True)[0]

    assert upper.devices[:3] == generated.devices[:3]
    assert upper.devices[3:] == current.devices[3:]
    assert full == generated


def test_freeze_blend_resume_keeps_stream_alive() -> None:
    async def scenario() -> None:
        port = _free_udp_port()
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", port))
        receiver.setblocking(False)

        service = PostureService(
            PostureConfig(
                bridge_port=port,
                keepalive_fps=45.0,
                blend_s=0.1,
            ),
            DartConfig(timeout_s=0.2),
        )
        await service.start()
        await asyncio.sleep(0.1)
        before = service.frames_sent

        await service.freeze()
        assert service.mode is PostureMode.FROZEN
        frozen = service.current_frame
        await asyncio.sleep(0.1)
        assert service.frames_sent > before
        assert service.current_frame == frozen

        await service.resume()
        assert service.mode is PostureMode.IDLE
        assert service.current_frame == service.current_frame

        packets: list[bytes] = []
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            try:
                payload = await asyncio.wait_for(loop.sock_recv(receiver, 512), 0.05)
                packets.append(payload)
            except asyncio.TimeoutError:
                pass

        await service.stop()
        receiver.close()

        assert len(packets) >= 5
        sequences = [PostureFrame.unpack(payload)[0] for payload in packets]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)

    asyncio.run(scenario())
