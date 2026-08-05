"""SteamVR tracker UDP sink and private 184-byte wire ownership."""
from __future__ import annotations

import socket
import time

from ...contracts.frame import PostureFrame


class SteamVRTrackerSink:
    def __init__(
        self, host: str, port: int, *, sock: socket.socket | None = None
    ) -> None:
        self._address = (host, port)
        self._sock = sock
        self._owns_socket = sock is None
        self._sequence = 0

    def start(self) -> None:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, frame: PostureFrame) -> None:
        if self._sock is None:
            raise RuntimeError("SteamVR tracker sink is not started")
        self._sequence += 1
        self._sock.sendto(frame.pack(self._sequence, time.time()), self._address)

    @property
    def sequence(self) -> int:
        return self._sequence

    def close(self) -> None:
        if self._sock is not None and self._owns_socket:
            self._sock.close()
        self._sock = None
