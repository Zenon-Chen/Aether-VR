"""阶段 3：Runtime 四模块拆分、闭环接线与分层架构守卫测试。"""

from __future__ import annotations

import ast
import asyncio
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from aether.contracts.plan import ActionKind, Channel, Plan, PlanStep, StepStatus
from aether.contracts.vision import VisionStatus
from aether.infra.config import AetherConfig
from aether.runtime import AetherRuntime, create_motion_provider
from aether.services.motion.supervised import SupervisedMotionProvider
from aether.services.ports import (
    AvatarPort,
    BrainPort,
    CapturePort,
    CerebellumPort,
    LocomotionPort,
    MotionProviderPort,
    PosturePort,
    ScribePort,
    SpeechPort,
    VisionPort,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AETHER_PKG = REPO_ROOT / "aether"


def _resolve_import_target(file_path: Path, module: str | None, level: int) -> str:
    rel_parts = list(file_path.relative_to(AETHER_PKG).parts)
    pkg_parts = ["aether", *rel_parts[:-1]]
    if level == 0:
        return module or ""
    up = level - 1
    base = pkg_parts[: len(pkg_parts) - up] if up > 0 else pkg_parts
    if module:
        return ".".join([*base, module])
    return ".".join(base)


def test_agents_md_layering_direction_enforced():
    """静态 AST 校验：严格执行 contracts <- core <- services <- runtime 单向依赖。"""
    violations: list[str] = []

    for py_file in sorted(AETHER_PKG.rglob("*.py")):
        if py_file.name == "__main__.py":
            continue
        rel = py_file.relative_to(AETHER_PKG)
        layer = rel.parts[0]
        if layer not in ("contracts", "core", "services"):
            continue
        service_name = rel.parts[1] if layer == "services" and len(rel.parts) > 2 else ""

        tree = ast.parse(py_file.read_text(encoding="utf-8-sig"), filename=str(py_file))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = _resolve_import_target(py_file, node.module, node.level)
                if node.module:
                    targets.append(base)
                for alias in node.names:
                    if alias.name != "*":
                        targets.append(f"{base}.{alias.name}" if base else alias.name)

            for target in targets:
                if not target.startswith("aether."):
                    continue
                parts = target.split(".")
                if len(parts) < 2:
                    continue
                target_layer = parts[1]

                if layer == "contracts" and target_layer != "contracts":
                    violations.append(f"{rel}:{node.lineno} -> {target}")
                elif layer == "core" and target_layer not in ("contracts", "core"):
                    violations.append(f"{rel}:{node.lineno} -> {target}")
                elif layer == "services":
                    if target_layer in ("core", "runtime"):
                        violations.append(f"{rel}:{node.lineno} -> {target}")
                    elif target_layer == "services" and len(parts) >= 3:
                        other = parts[2]
                        if other != "ports" and service_name and other != service_name:
                            violations.append(
                                f"{rel}:{node.lineno} 跨服务引用 -> {target}"
                            )

    assert violations == []


def test_all_services_satisfy_port_protocols():
    runtime = AetherRuntime(AetherConfig())
    assert isinstance(runtime.capture, CapturePort)
    assert isinstance(runtime.avatar, AvatarPort)
    assert isinstance(runtime.locomotion, LocomotionPort)
    assert isinstance(runtime.vision, VisionPort)
    assert isinstance(runtime.speech, SpeechPort)
    assert isinstance(runtime.motion_provider, MotionProviderPort)
    assert isinstance(runtime.posture, PosturePort)
    assert isinstance(runtime.brain, BrainPort)
    assert isinstance(runtime.scribe, ScribePort)
    assert isinstance(runtime.cerebellum, CerebellumPort)


@pytest.mark.asyncio
async def test_slow_dart_generation_times_out_into_prerecorded_fallback_within_plan_allowance():
    cfg = AetherConfig()
    cfg.cerebellum.max_plan_seconds = 0.2
    cfg.motion.generation_timeout_s = 0.05
    cfg.motion.synthesis_allowance_s = 0.6

    runtime = AetherRuntime(cfg)
    runtime._register_executors()

    events: list[str] = []

    async def slow_play(*_args):
        await asyncio.sleep(1.0)
        return "unreachable"

    async def record(tag: str):
        events.append(tag)

    runtime.posture.play = slow_play
    runtime.locomotion.halt = lambda: record("halt")
    runtime.posture.freeze = lambda: record("freeze")
    runtime.avatar.play_action = lambda name: record(f"fallback:{name}")
    runtime.avatar.reset = lambda: record("reset")
    runtime.posture.resume = lambda: record("resume")

    plan = Plan(
        steps=(
            PlanStep(
                1,
                ActionKind.GENERATE_MOTION,
                {"prompt": "dance", "duration_s": 0.05, "scope": "full_body"},
            ),
        )
    )
    result = await runtime.scheduler.submit(plan)

    assert result.status is StepStatus.SUCCESS
    assert "已回退 bow" in result.steps[0].detail
    assert events == ["halt", "freeze", "fallback:bow", "reset", "resume"]


@pytest.mark.asyncio
async def test_multi_segment_slow_tts_succeeds_via_speech_synthesis_allowance():
    cfg = AetherConfig()
    cfg.cerebellum.max_plan_seconds = 0.5
    cfg.speech.synthesis_allowance_s = 0.5

    runtime = AetherRuntime(cfg)
    runtime._register_executors()

    spoken: list[str] = []

    async def slow_speak(text: str):
        await asyncio.sleep(0.62)
        spoken.append(text)

    runtime.speech.speak = slow_speak
    result = await runtime.scheduler.submit(
        Plan(steps=(PlanStep(1, ActionKind.SPEAK, {"text": "你好"}),))
    )

    assert result.status is StepStatus.SUCCESS
    assert spoken == ["你好"]


@pytest.mark.asyncio
async def test_refresh_health_marks_false_dart_probe_as_offline():
    runtime = AetherRuntime(AetherConfig())

    async def false_probe():
        return False

    async def ok_probe():
        return "ok"

    runtime.posture.check_dart = false_probe
    runtime.posture.health = ok_probe
    runtime.speech.health = ok_probe
    runtime.cerebellum.health = ok_probe
    runtime.brain.health = ok_probe
    runtime.avatar.health = ok_probe
    runtime.vision.health = ok_probe

    await runtime._refresh_health()
    assert runtime.state.facts.services.dart == "offline"
    assert runtime.state.facts.services.posture == "ok"


@pytest.mark.asyncio
async def test_partial_and_final_transcript_stop_keyword_triggers_emergency_once():
    runtime = AetherRuntime(AetherConfig())
    triggered: list[str] = []

    async def fake_trigger(reason: str) -> float:
        triggered.append(reason)
        return 0.001

    runtime.trigger_emergency = fake_trigger  # type: ignore[method-assign]
    gate = runtime._get_perception()

    # 单词内部子串不误触发
    gate.on_partial_transcript("She is unstoppable today")
    await asyncio.sleep(0)
    assert triggered == []

    # 增量转写命中独立词 stop 立即触发一次，完成句不重复触发
    gate.on_partial_transcript("please stop moving")
    gate.on_transcript("please stop moving")
    await asyncio.sleep(0)
    assert len(triggered) == 1
    assert "stop" in triggered[0]


def test_pcm_vad_updates_user_speaking_with_hangover():
    cfg = AetherConfig()
    cfg.capture.vad_rms_threshold = 0.05
    cfg.capture.vad_hangover_s = 0.4
    runtime = AetherRuntime(cfg)
    gate = runtime._get_perception()

    loud_pcm = struct.pack("<8h", *([8000, -8000] * 4))
    silent_pcm = struct.pack("<8h", *([0] * 8))

    assert runtime.user_speaking is False
    assert gate.observe_audio_rms(loud_pcm, now=10.0) is True
    assert runtime.user_speaking is True

    # 尾音保持期内静音不掉线
    assert gate.observe_audio_rms(silent_pcm, now=10.25) is True
    assert runtime.user_speaking is True

    # 超过 hangover 后自动复位
    assert gate.observe_audio_rms(silent_pcm, now=10.45) is False
    assert runtime.user_speaking is False


@pytest.mark.asyncio
async def test_approach_target_acquires_movement_channel_lock():
    cfg = AetherConfig()
    cfg.vision.enabled = True
    runtime = AetherRuntime(cfg)
    runtime._register_executors()
    runtime.vision.tracker = SimpleNamespace(degraded=False)

    busy_snapshots: list[tuple] = []

    async def fake_approach(target_id: str, **_kwargs):
        busy_snapshots.append(runtime.state.facts.busy_channels())
        return VisionStatus(state="arrived", target_id=target_id, reason="near", steps=2)

    runtime.vision.approach_target = fake_approach  # type: ignore[method-assign]
    result = await runtime.approach_target("person")

    assert result.reason == "near"
    assert busy_snapshots and Channel.MOVEMENT in busy_snapshots[0]
    assert runtime.state.facts.busy_channels() == ()


@pytest.mark.asyncio
async def test_brain_loop_warm_start_and_debounced_utterance_wake():
    cfg = AetherConfig()
    cfg.brain.enabled = True
    cfg.brain.interval_s = 60.0
    cfg.brain.initial_delay_s = 0.01
    cfg.brain.min_event_interval_s = 0.03

    runtime = AetherRuntime(cfg)
    runtime._running = True

    observed_calls: list[str] = []
    first_done = asyncio.Event()
    second_done = asyncio.Event()

    async def fake_observe(_facts, transcript, _frame, _memories):
        observed_calls.append(transcript)
        if len(observed_calls) == 1:
            first_done.set()
        elif len(observed_calls) >= 2:
            second_done.set()
            runtime._running = False
        return None

    runtime.brain = SimpleNamespace(enabled=True, observe=fake_observe, last_output=None)  # type: ignore[assignment]
    loop_task = asyncio.create_task(runtime._brain_loop())

    # 首轮热观察在几十毫秒内完成，不必苦等 60s
    await asyncio.wait_for(first_done.wait(), timeout=0.5)
    runtime.add_transcript("你好 Aether")
    await asyncio.wait_for(second_done.wait(), timeout=0.5)
    await asyncio.wait_for(loop_task, timeout=0.5)

    assert len(observed_calls) == 2
    assert "你好 Aether" in observed_calls[1]


def test_supervised_motion_provider_wiring(tmp_path):
    exe = tmp_path / "run_stub.py"
    exe.write_text("print('ok')\n", encoding="utf-8")
    manifest = {
        "provider_id": "dart-sidecar",
        "version": "0.1.0",
        "executable": "run_stub.py",
        "args": [],
        "workdir": ".",
        "env": {},
    }
    manifest_path = tmp_path / "bundle-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    cfg = AetherConfig()
    cfg.motion.mode = "supervised"
    cfg.motion.bundle_manifest = str(manifest_path)
    cfg.motion.allow_unreleased_bundle = True

    provider = create_motion_provider(cfg)
    assert isinstance(provider, SupervisedMotionProvider)
