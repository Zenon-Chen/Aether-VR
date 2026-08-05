"""阶段 2a：Scribe 裁决、驱逐落盘与 Runtime 记忆回流。"""

from types import SimpleNamespace

import pytest

from aether.contracts.observation import BrainOutput
from aether.contracts.plan import ActionKind, Plan, PlanStep
from aether.core import StateStore
from aether.infra.config import ScribeConfig
from aether.runtime.agent import AetherRuntime
from aether.services.cerebellum.service import CerebellumService
from aether.services.scribe import ScribeService
from aether.services.scribe import service as scribe_service


@pytest.mark.asyncio
async def test_scribe_uses_model_batch_decisions(tmp_path):
    scribe = ScribeService(ScribeConfig(
        enabled=True, batch_size=2, db_path=str(tmp_path / "memory.sqlite3")
    ))
    seen = []

    async def evaluate(blocks):
        seen.extend(blocks)
        return [
            {"importance": "important"},
            {"importance": "normal", "summary": "压缩摘要"},
        ]

    scribe.set_evaluator(evaluate)
    await scribe.start()
    await scribe.note("text", "必须记住的事实")
    await scribe.note("text", "普通过程内容")

    assert seen == [
        {"kind": "text", "content": "必须记住的事实"},
        {"kind": "text", "content": "普通过程内容"},
    ]
    assert [block.importance.value for block in scribe._blocks] == ["important", "normal"]
    assert scribe._blocks[1].summary == "压缩摘要"
    await scribe.stop()


@pytest.mark.asyncio
async def test_scribe_eviction_is_persisted_and_recently_recalled(tmp_path):
    scribe = ScribeService(ScribeConfig(
        enabled=True,
        batch_size=1,
        db_path=str(tmp_path / "memory.sqlite3"),
        context_limit_tokens=3,
        evict_target_tokens=1,
    ))

    async def important(_blocks):
        return [{"importance": "important"}]

    scribe.set_evaluator(important)
    await scribe.start()
    await scribe.note("text", "这是一段会被驱逐并落盘的长期事实")

    assert scribe._blocks == []
    assert "长期事实" in await scribe.recall_recent()
    row = scribe._db.execute("SELECT kind FROM memory").fetchone()
    assert row == ("evicted_text",)
    await scribe.stop()


@pytest.mark.asyncio
async def test_scribe_remember_deduplicates_same_kind_within_window(tmp_path):
    scribe = ScribeService(ScribeConfig(
        enabled=True, db_path=str(tmp_path / "memory.sqlite3")
    ))
    await scribe.start()

    await scribe.remember("相同观察", "brain")
    await scribe.remember("相同观察", "brain")

    rows = scribe._db.execute(
        "SELECT kind, content FROM memory ORDER BY id"
    ).fetchall()
    assert rows == [("brain", "相同观察")]
    await scribe.stop()


@pytest.mark.asyncio
async def test_scribe_remember_allows_same_content_for_different_kinds(tmp_path):
    scribe = ScribeService(ScribeConfig(
        enabled=True, db_path=str(tmp_path / "memory.sqlite3")
    ))
    await scribe.start()

    await scribe.remember("相同内容", "brain")
    await scribe.remember("相同内容", "evicted_text")

    rows = scribe._db.execute(
        "SELECT kind FROM memory ORDER BY id"
    ).fetchall()
    assert rows == [("brain",), ("evicted_text",)]
    await scribe.stop()


@pytest.mark.asyncio
async def test_scribe_remember_allows_duplicate_after_window(tmp_path, monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(scribe_service.time, "time", lambda: now)
    scribe = ScribeService(ScribeConfig(
        enabled=True,
        db_path=str(tmp_path / "memory.sqlite3"),
        dedupe_window_s=3600,
    ))
    await scribe.start()

    await scribe.remember("周期观察", "brain")
    now += 3601
    await scribe.remember("周期观察", "brain")

    count = scribe._db.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    assert count == 2
    await scribe.stop()


@pytest.mark.asyncio
async def test_cerebellum_sends_frame_as_live_video():
    service = CerebellumService(SimpleNamespace())
    sent = {}

    class Session:
        async def send_realtime_input(self, **kwargs):
            sent.update(kwargs)

    service._session = Session()
    service._connected = True
    frame = SimpleNamespace(jpeg=b"jpeg")
    await service.send_video(frame)

    assert "video" in sent
    assert sent["video"].mime_type == "image/jpeg"
    assert sent["video"].data == b"jpeg"


@pytest.mark.asyncio
async def test_cerebellum_emits_finished_input_transcript():
    service = CerebellumService(SimpleNamespace())
    lines = []
    service.set_transcript_handler(lines.append)
    first = SimpleNamespace(
        server_content=SimpleNamespace(
            input_transcription=SimpleNamespace(text="你好", finished=False),
            turn_complete=False,
            model_turn=None,
        ),
        text=None, tool_call=None,
        session_resumption_update=None, go_away=None,
    )
    final = SimpleNamespace(
        server_content=SimpleNamespace(
            input_transcription=SimpleNamespace(text=" Aether", finished=True),
            turn_complete=False,
            model_turn=None,
        ),
        text=None, tool_call=None,
        session_resumption_update=None, go_away=None,
    )

    await service._handle(first)
    await service._handle(final)

    assert lines == ["你好 Aether"]


def test_runtime_snapshots_suggested_reply_into_plan():
    runtime = AetherRuntime.__new__(AetherRuntime)
    runtime.brain = SimpleNamespace(last_output=BrainOutput(
        suggested_reply="锁定的台词",
    ))
    plan = Plan(steps=(
        PlanStep(1, ActionKind.SPEAK, {"use_suggested": True}),
        PlanStep(2, ActionKind.WAIT, {"duration_s": 0.1}),
    ))

    frozen = runtime._snapshot_suggested_replies(plan)
    runtime.brain.last_output = BrainOutput(suggested_reply="后来变化的台词")

    assert frozen.steps[0].args["suggested_reply"] == "锁定的台词"
    assert frozen.steps[1] is plan.steps[1]
    assert frozen.plan_id == plan.plan_id


@pytest.mark.asyncio
async def test_exec_speak_uses_snapshot_when_brain_changes():
    runtime = AetherRuntime.__new__(AetherRuntime)
    spoken = []

    async def speak(text):
        spoken.append(text)

    runtime.speech = SimpleNamespace(speak=speak)
    runtime.state = SimpleNamespace(set_last_speech=_async_sink())
    runtime.scribe = SimpleNamespace(note=_async_sink())
    runtime.brain = SimpleNamespace(last_output=BrainOutput())
    step = PlanStep(1, ActionKind.SPEAK, {
        "use_suggested": True,
        "suggested_reply": "锁定的台词",
    })

    assert await runtime._exec_speak(step) == "锁定的台词"
    assert await runtime._exec_speak(step) == "锁定的台词"
    assert spoken == ["锁定的台词", "锁定的台词"]


@pytest.mark.asyncio
async def test_runtime_feeds_recent_memory_to_brain_observe():
    runtime = AetherRuntime.__new__(AetherRuntime)
    runtime._running = True
    runtime.config = SimpleNamespace(brain=SimpleNamespace(interval_s=0))
    runtime.state = StateStore()
    runtime._transcript = ["用户说：你好"]
    runtime.capture = SimpleNamespace(latest_frame=lambda: None)
    runtime.scribe = SimpleNamespace(recall_recent=_async_value("用户喜欢蓝色"))
    observed = {}

    async def observe(facts, transcript, frame, memories):
        observed.update(transcript=transcript, frame=frame, memories=memories)
        runtime._running = False
        return None

    runtime.brain = SimpleNamespace(enabled=True, observe=observe)
    await runtime._brain_loop()

    assert observed["transcript"] == "用户说：你好"
    assert observed["memories"] == "用户喜欢蓝色"


@pytest.mark.asyncio
async def test_add_transcript_registers_user_dialogue_with_scribe():
    runtime = AetherRuntime.__new__(AetherRuntime)
    runtime._transcript = []
    noted = []

    async def note(kind, content):
        noted.append((kind, content))

    runtime.scribe = SimpleNamespace(note=note)
    runtime.add_transcript("今晚一起散步")
    await __import__("asyncio").sleep(0)

    assert runtime._transcript == ["今晚一起散步"]
    assert noted == [("user", "用户说：今晚一起散步")]


def _async_value(value):
    async def get():
        return value
    return get


def _async_sink():
    async def sink(*_args, **_kwargs):
        return None
    return sink
