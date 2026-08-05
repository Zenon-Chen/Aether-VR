import asyncio
from types import SimpleNamespace

import pytest

from aether.runtime.agent import AetherRuntime


@pytest.mark.asyncio
async def test_stop_cancels_tracked_tool_and_plan_tasks():
    runtime = AetherRuntime.__new__(AetherRuntime)
    runtime._running = True
    runtime._tasks = []
    runtime._call_tasks = set()
    runtime._plan_tasks = set()

    cancelled = []

    async def blocked(tag):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(tag)

    call = asyncio.create_task(blocked("call"))
    plan = asyncio.create_task(blocked("plan"))
    runtime._track_task(call, runtime._call_tasks)
    runtime._track_task(plan, runtime._plan_tasks)
    await asyncio.sleep(0)

    async def cancel_current(_reason):
        return None

    runtime.scheduler = SimpleNamespace(cancel_current=cancel_current)

    async def stop_service():
        return None

    service = SimpleNamespace(name="stub", stop=stop_service)
    runtime.cerebellum = runtime.capture = runtime.vision = service
    runtime.brain = runtime.scribe = runtime.posture = service
    runtime.speech = runtime.locomotion = runtime.avatar = service

    await runtime.stop()

    assert set(cancelled) == {"call", "plan"}
    assert not runtime._call_tasks and not runtime._plan_tasks
