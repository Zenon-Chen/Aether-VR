from __future__ import annotations

import asyncio
import socket
import time

from aether.contracts.frame import PostureFrame
from aether.infra.config import DartConfig, PostureConfig
from aether.services.posture.service import PostureMode, PostureService


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


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
