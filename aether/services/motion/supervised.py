from __future__ import annotations

from typing import Callable

from ...contracts.motion import CanonicalMotion, MotionCapabilities, MotionHealthReport, MotionRequest
from ..ports import MotionProviderPort
from .supervisor import MotionProviderSupervisor


class SupervisedMotionProvider:
    """Starts a sidecar, then delegates the provider protocol to its endpoint."""
    def __init__(
        self,
        supervisor: MotionProviderSupervisor,
        factory: Callable[[str, str], MotionProviderPort],
    ):
        self._supervisor = supervisor
        self._factory = factory
        self._provider: MotionProviderPort | None = None

    async def start(self) -> None:
        endpoint = await self._supervisor.start()
        self._provider = self._factory(endpoint.base_url, endpoint.token)
        await self._provider.start()

    def _require(self) -> MotionProviderPort:
        if self._provider is None:
            raise RuntimeError("motion provider is not started")
        return self._provider

    async def close(self) -> None:
        if self._provider is not None:
            await self._provider.close()
        await self._supervisor.close()
        self._provider = None

    async def health(self) -> MotionHealthReport:
        return await self._require().health()
    async def status(self) -> MotionHealthReport:
        return await self._require().status()

    def capabilities(self) -> MotionCapabilities:
        return self._require().capabilities()

    async def generate(self, request: MotionRequest) -> CanonicalMotion:
        return await self._require().generate(request)
