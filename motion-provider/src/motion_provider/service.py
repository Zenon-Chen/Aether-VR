from __future__ import annotations

import re
import threading
from typing import Any

from .backend import detect, load_backend
from .config import Config

_SEGMENT = re.compile(r"^\s*[^,*]+\s*\*\s*([1-9]\d*)\s*$")


class Service:
    def __init__(self, config: Config | None = None, backend=None):
        self.config = config or Config.from_env()
        self.backend = backend or load_backend(self.config)
        self._lock = threading.Lock()
        self._started = bool(self.diagnostics["backend"] == "mock")
        if self._started:
            self.backend.loaded = True

    @property
    def diagnostics(self) -> dict[str, Any]:
        return self.backend.diagnostics.as_dict()

    async def start(self) -> None:
        if self._started:
            return
        await self.backend.start()
        self._started = True

    async def stop(self) -> None:
        try:
            if self._started:
                await self.backend.stop()
        finally:
            self._started = False

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self._started and self.backend.loaded else "starting",
            "backend": self.diagnostics["backend"],
            "model_loaded": bool(self._started and self.backend.loaded),
        }

    def status(self) -> dict[str, Any]:
        return {
            "model_loaded": bool(self._started and self.backend.loaded),
            "generating": self._lock.locked(),
            **self.diagnostics,
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "generation": "mock" if self.diagnostics["backend"] == "mock" else "real",
            "backends": ["mock", "cpu", "cuda", "rocm(Linux)", "xpu(experimental/unverified)"],
            "diagnostics": self.diagnostics,
        }

    def generate(self, request) -> dict[str, Any]:
        if not self._started or not self.backend.loaded:
            raise RuntimeError("backend is not ready")
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        text = request.get("text")
        batch = request.get("batch_size", 1)
        guidance = request.get("guidance_param", 5.0)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        segments = text.split(",")
        if len(segments) > 30 or any(not _SEGMENT.fullmatch(part) for part in segments):
            raise ValueError("text must contain at most 30 comma-separated action*positive_segments entries")
        if isinstance(batch, bool) or not isinstance(batch, int) or not 1 <= batch <= 8:
            raise ValueError("batch_size must be an integer between 1 and 8")
        if isinstance(guidance, bool) or not isinstance(guidance, (int, float)) or not 0 <= guidance <= 20:
            raise ValueError("guidance_param must be a number between 0 and 20")
        if not self._lock.acquire(False):
            raise RuntimeError("another generation is in progress")
        try:
            return self.backend.generate(text, batch, float(guidance))
        finally:
            self._lock.release()
