"""DART HTTP API 的动作生成器适配器。"""

from __future__ import annotations

from typing import Any

import httpx

from .....contracts.motion import (
    CanonicalMotion, MotionCapabilities, MotionErrorCode, MotionHealthReport,
    MotionProviderError, MotionRequest,
)
from .translator import DartTranslator


class DartMotionProvider:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_s: float = 60.0,
        guidance: float = 5.0,
        client: httpx.AsyncClient | None = None,
        translator: DartTranslator | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._guidance = guidance
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None
        self._translator = translator or DartTranslator()
        self._token = token

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def capabilities(self) -> MotionCapabilities:
        return MotionCapabilities(provider_id="dart")

    async def health(self) -> MotionHealthReport:
        return await self._report("/health")

    async def status(self) -> MotionHealthReport:
        return await self._report("/status")

    async def generate(self, request: MotionRequest) -> CanonicalMotion:
        body = {
            "text": _with_repeat(request.prompt, request.duration_s),
            "batch_size": 1,
            "guidance_param": self._guidance,
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/generate", json=body, headers=self._headers
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise MotionProviderError(MotionErrorCode.TIMEOUT, str(exc), retryable=True) from exc
        except httpx.HTTPStatusError as exc:
            code = _http_error_code(exc.response.status_code)
            raise MotionProviderError(code, str(exc), retryable=exc.response.status_code >= 500) from exc
        except httpx.RequestError as exc:
            raise MotionProviderError(MotionErrorCode.UNAVAILABLE, str(exc), retryable=True) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise MotionProviderError(
                MotionErrorCode.INVALID_RESPONSE, "动作响应不是有效JSON"
            ) from exc
        return self._translator.translate(
            payload, request_id=request.request_id, provider_id="dart"
        )

    async def _report(self, path: str) -> MotionHealthReport:
        try:
            response = await self._client.get(
                f"{self._base_url}{path}", headers=self._headers
            )
            healthy = response.status_code == 200
            try:
                details: dict[str, Any] = response.json()
            except ValueError:
                details = {"body": response.text}
            service_status = str(details.get("status", "")).lower()
            model_loaded = details.get("model_loaded")
            ready = healthy and service_status not in {"loading", "error"} \
                and model_loaded is not False
            status = "ok" if ready else (service_status or "degraded")
            return MotionHealthReport("dart", ready, status, details)
        except httpx.RequestError as exc:
            return MotionHealthReport("dart", False, "unavailable", {"error": str(exc)})


def _with_repeat(prompt: str, duration_s: float) -> str:
    normalized = " and ".join(part.strip() for part in prompt.split(",") if part.strip())
    if "*" in normalized:
        return normalized
    return f"{normalized}*{max(1, round(duration_s / 0.5))}"


def _http_error_code(status: int) -> MotionErrorCode:
    if status in (401, 403):
        return MotionErrorCode.AUTHENTICATION
    if 400 <= status < 500:
        return MotionErrorCode.INVALID_REQUEST
    return MotionErrorCode.UNAVAILABLE
