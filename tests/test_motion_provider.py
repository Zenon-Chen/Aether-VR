from __future__ import annotations

import asyncio
from dataclasses import asdict, fields

import httpx

from aether.contracts.motion import CanonicalMotion, MotionRequest
from aether.services.motion.providers.dart import DartMotionProvider, DartTranslator
from aether.services.ports import MotionProviderPort, MotionTranslatorPort


def _payload() -> dict:
    return {
        "poses": [[0.0] * 66, [0.0] * 66],
        "trans": [[0.0, 0.0, 0.9], [0.1, 0.0, 0.9]],
        "joints": [[[0.0, 0.0, 0.0]] * 22] * 2,
        "framerate": 30,
    }


def test_public_motion_contract_is_serializable_and_vendor_neutral() -> None:
    motion = DartTranslator().translate(_payload(), request_id="req-contract")
    serialized = asdict(motion)
    names = {field.name.lower() for field in fields(CanonicalMotion)}

    assert motion.request_id == "req-contract"
    assert motion.skeleton_id == "humanoid-22"
    assert motion.frame_count == 2
    assert len(motion.joint_rotations[0]) == 22
    assert serialized["root_translations"][1] == (0.1, 0.0, 0.9)
    assert not any("dart" in name or "steamvr" in name or "device" in name for name in names)
    assert not {"poses", "pose", "trans", "joints", "framerate"} & names


def test_ports_are_structurally_implemented() -> None:
    provider = DartMotionProvider("http://motion.invalid")
    try:
        assert isinstance(provider, MotionProviderPort)
        assert isinstance(DartTranslator(), MotionTranslatorPort)
    finally:
        asyncio.run(provider.close())


def test_dart_provider_http_adapter() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/generate":
                return httpx.Response(200, json=_payload())
            return httpx.Response(200, json={"status": "ok", "model_loaded": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DartMotionProvider(
                "http://motion.test", token="secret", guidance=6.5, client=client
            )
            health = await provider.health()
            status = await provider.status()
            motion = await provider.generate(MotionRequest("req-1", "wave, then bow", 2.0))

        assert health.healthy and status.healthy
        assert motion.request_id == "req-1"
        assert motion.provider_id == "dart" and motion.frame_count == 2
        generated = requests[-1]
        assert generated.headers["Authorization"] == "Bearer secret"
        body = __import__("json").loads(generated.content)
        assert body == {
            "text": "wave and then bow*4", "batch_size": 1, "guidance_param": 6.5
        }

    asyncio.run(scenario())


def test_dart_loading_health_is_not_ready() -> None:
    async def scenario() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "loading", "model_loaded": False})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            report = await DartMotionProvider("http://motion.test", client=client).health()

        assert not report.healthy
        assert report.status == "loading"

    asyncio.run(scenario())
