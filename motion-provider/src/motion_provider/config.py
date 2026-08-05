from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    backend: str = "auto"
    device: str | None = None
    dtype: str = "float32"
    dart_root: str | None = None
    checkpoint: str | None = None
    host: str = "127.0.0.1"
    port: int = 8901
    token: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")

    @classmethod
    def from_env(cls) -> "Config":
        try:
            port = int(os.getenv("MOTION_PROVIDER_PORT", "8901"))
        except ValueError as exc:
            raise ValueError("MOTION_PROVIDER_PORT must be an integer") from exc
        return cls(
            backend=os.getenv("MOTION_PROVIDER_BACKEND", "auto"),
            device=os.getenv("MOTION_PROVIDER_DEVICE"),
            dtype=os.getenv("MOTION_PROVIDER_DTYPE", "float32"),
            dart_root=os.getenv("DART_ROOT"),
            checkpoint=os.getenv("DART_CHECKPOINT"),
            host=os.getenv("MOTION_PROVIDER_HOST", "127.0.0.1"),
            port=port,
            token=os.getenv("MOTION_PROVIDER_TOKEN") or None,
        )
