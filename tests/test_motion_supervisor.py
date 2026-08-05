from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from aether.services.motion.supervised import SupervisedMotionProvider
from aether.services.motion.supervisor import (
    BundleManifest, BundleRegistry, MotionProviderSupervisor,
)
from aether.services.motion.providers.dart import DartMotionProvider


_SIDECAR = r'''
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["MOTION_PROVIDER_TOKEN"]
PORT = int(os.environ["MOTION_PROVIDER_PORT"])
class Handler(BaseHTTPRequestHandler):
    def _auth(self):
        return self.headers.get("Authorization") == "Bearer " + TOKEN
    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        if not self._auth():
            self._send(401, {"status": "unauthorized"})
        elif self.path in ("/health", "/status"):
            self._send(200, {"status": "ok", "model_loaded": True})
        else:
            self._send(404, {})
    def do_POST(self):
        if not self._auth():
            self._send(401, {})
        elif self.path == "/generate":
            self._send(200, {"poses": [[0.0] * 66], "trans": [[0.0, 0.0, 0.9]], "framerate": 30})
        else:
            self._send(404, {})
    def log_message(self, *_args):
        pass

print("fake-sidecar-ready", flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''

_EXITING = "import sys; print('fake-sidecar-exit', flush=True); sys.exit(7)"


def _manifest(version: str, code: str = _SIDECAR, *, max_restarts: int = 1,
              startup_timeout: float = 2.0) -> BundleManifest:
    return BundleManifest(
        provider_id="fake-motion",
        version=version,
        executable=sys.executable,
        args=("-u", "-c", code),
        startup_timeout=startup_timeout,
        max_restarts=max_restarts,
        log_path="logs/provider.log",
    )


@pytest.mark.asyncio
async def test_supervisor_dynamic_endpoint_token_probe_logs_and_stop(tmp_path: Path):
    supervisor = MotionProviderSupervisor(_manifest("1"), tmp_path)
    endpoint = await supervisor.start()
    try:
        assert endpoint.host == "127.0.0.1"
        assert endpoint.port > 0
        assert len(endpoint.token) >= 32
        assert await supervisor.health()
        async with httpx.AsyncClient() as client:
            denied = await client.get(endpoint.base_url + "/health")
            allowed = await client.get(
                endpoint.base_url + "/health",
                headers={"Authorization": f"Bearer {endpoint.token}"},
            )
        assert denied.status_code == 401
        assert allowed.status_code == 200
        await asyncio.sleep(0.05)
        log = (tmp_path / "logs" / "provider.log").read_text()
        assert "fake-sidecar-ready" in log
        assert endpoint.token not in log
    finally:
        await supervisor.close()
    assert not supervisor.status().running


@pytest.mark.asyncio
async def test_supervisor_bounds_failed_start_retries(tmp_path: Path):
    supervisor = MotionProviderSupervisor(
        _manifest("bad", _EXITING, max_restarts=2, startup_timeout=0.5), tmp_path
    )
    with pytest.raises(RuntimeError, match="failed startup"):
        await supervisor.start()
    assert supervisor.status().restarts == 2
    await supervisor.close()


@pytest.mark.asyncio
async def test_registry_persists_activation_and_rolls_back_candidate(tmp_path: Path):
    registry = BundleRegistry(tmp_path / "registry")
    registry.stage(_manifest("v1"), tmp_path / "v1")
    registry.stage(_manifest("v2"), tmp_path / "v2")
    first = await registry.activate("v1")
    second = await registry.activate("v2")
    try:
        assert first.port != second.port
        assert registry.active == "v2"
        restored = await registry.rollback()
        assert restored is not None
        assert registry.active == "v1"
        saved = json.loads((tmp_path / "registry" / "manifests.json").read_text())
        assert saved["active"] == "v1"
    finally:
        await registry._supervisors[registry.active].close()


@pytest.mark.asyncio
async def test_failed_candidate_does_not_replace_active_bundle(tmp_path: Path):
    registry = BundleRegistry(tmp_path / "registry")
    registry.stage(_manifest("good"), tmp_path / "good")
    registry.stage(_manifest("bad", _EXITING, max_restarts=0), tmp_path / "bad")
    await registry.activate("good")
    try:
        with pytest.raises(RuntimeError, match="failed probe"):
            await registry.activate("bad")
        assert registry.active == "good"
        assert await registry._supervisors["good"].health()
    finally:
        await registry._supervisors["good"].close()


@pytest.mark.asyncio
async def test_supervisor_restarts_runtime_exit_only_within_bound(tmp_path: Path):
    code = _SIDECAR.replace(
        'ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()',
        'server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)\n'
        'import threading, time\n'
        'threading.Thread(target=server.serve_forever, daemon=True).start()\n'
        'time.sleep(0.12)\n'
        'server.shutdown()',
    )
    supervisor = MotionProviderSupervisor(
        _manifest("flaky", code, max_restarts=1), tmp_path
    )
    await supervisor.start()
    try:
        for _ in range(30):
            if not supervisor.status().running:
                break
            await asyncio.sleep(0.05)
        assert supervisor.status().restarts == 1
        assert not supervisor.status().running
    finally:
        await supervisor.close()


@pytest.mark.asyncio
async def test_supervised_provider_passes_runtime_token_to_adapter(tmp_path: Path):
    supervisor = MotionProviderSupervisor(_manifest("wrapped"), tmp_path)
    seen: dict[str, str] = {}

    def factory(base_url: str, token: str):
        seen.update(base_url=base_url, token=token)
        return DartMotionProvider(base_url, token=token)

    provider = SupervisedMotionProvider(supervisor, factory)
    await provider.start()
    try:
        assert seen["base_url"] == supervisor.endpoint.base_url
        assert seen["token"] == supervisor.endpoint.token
        assert (await provider.health()).healthy
    finally:
        await provider.close()
