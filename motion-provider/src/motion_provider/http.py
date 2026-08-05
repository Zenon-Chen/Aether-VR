from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

try:
    from fastapi import Depends, FastAPI, Header, HTTPException
except ImportError:
    FastAPI = None

from .config import Config
from .service import Service


def create_app(config: Config | None = None, service: Service | None = None):
    if FastAPI is None:
        raise RuntimeError("server dependencies unavailable; install aether-motion-provider[server]")
    selected = service
    selected_config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal selected
        selected = selected or Service(selected_config)
        application.state.service = selected
        await selected.start()
        try:
            yield
        finally:
            await selected.stop()

    application = FastAPI(title="Aether Motion Provider", lifespan=lifespan)
    application.state.service = selected

    def authorize(authorization: str | None = Header(default=None)) -> None:
        token = selected.config.token
        if token is None:
            return
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, "invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    @application.get("/health", dependencies=[Depends(authorize)])
    def health():
        result = selected.health()
        if result["status"] != "ok":
            raise HTTPException(503, "backend is not ready")
        return result

    @application.get("/status", dependencies=[Depends(authorize)])
    def status():
        return selected.status()

    @application.get("/capabilities", dependencies=[Depends(authorize)])
    def capabilities():
        return selected.capabilities()

    @application.post("/generate", dependencies=[Depends(authorize)])
    def generate(request: dict):
        try:
            return selected.generate(request)
        except RuntimeError as exc:
            code = 429 if "another generation" in str(exc) else 503
            raise HTTPException(code, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return application


# ASGI import target. CLI creates its own app so command-line Config is retained.
app = create_app() if FastAPI else None
