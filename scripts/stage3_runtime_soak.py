"""Stage 3: full AetherRuntime soak with health/rate/memory metrics."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.infra.config import load_config
from aether.infra.logging_setup import setup_logging
from aether.runtime.agent import AetherRuntime

DURATION_S = float(os.getenv("AETHER_SOAK_SECONDS", str(2 * 60 * 60)))
INTERVAL_S = float(os.getenv("AETHER_SOAK_INTERVAL", "60"))


def memory_rows(db_path: str) -> int:
    path = Path(db_path)
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(path) as db:
            return int(db.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
    except sqlite3.Error:
        return -1


async def main() -> None:
    config = load_config()
    setup_logging(config.log)
    log = Path("logs/stage3_runtime_soak.jsonl")
    log.parent.mkdir(exist_ok=True)
    log.write_text("", encoding="utf-8")
    tracemalloc.start()
    runtime = AetherRuntime(config)
    started = time.monotonic()
    last_frames = 0
    last_at = started
    failures = 0
    cycle = 0
    consecutive_healthy_s = 0.0
    await runtime.start()
    try:
        while time.monotonic() - started < DURATION_S:
            await asyncio.sleep(min(INTERVAL_S, DURATION_S - (time.monotonic() - started)))
            cycle += 1
            await runtime._refresh_health()
            now = time.monotonic()
            frames = runtime.posture.frames_sent
            sample_span = max(now - last_at, 1e-6)
            hz = (frames - last_frames) / sample_span
            last_frames, last_at = frames, now
            health = runtime.state.facts.services
            health_dict = {
                name: getattr(health, name)
                for name in ("dart", "tts", "er2", "brain", "posture", "vrchat_osc")
            }
            all_healthy = all(value == "ok" for value in health_dict.values())
            if all_healthy:
                consecutive_healthy_s += sample_span
            else:
                failures += 1
                consecutive_healthy_s = 0.0
            reconnect_events = runtime.cerebellum.reconnect_events
            frame = runtime.capture.latest_frame()
            current, peak = tracemalloc.get_traced_memory()
            row = {
                "t": round(time.time(), 3),
                "cycle": cycle,
                "elapsed_s": round(now - started, 1),
                "health": health_dict,
                "health_failures": failures,
                "consecutive_healthy_s": round(consecutive_healthy_s, 1),
                "er2_reconnect_events": reconnect_events,
                "posture_hz": round(hz, 2),
                "posture_frames": frames,
                "capture_frame_bytes": 0 if frame is None else len(frame.jpeg),
                "brain_failures": runtime.brain._failures,
                "brain_has_output": runtime.brain.last_output is not None,
                "scribe_context_tokens": runtime.scribe.context_tokens,
                "memory_rows": memory_rows(config.scribe.db_path),
                "tasks": len(asyncio.all_tasks()),
                "mem_current": current,
                "mem_peak": peak,
            }
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        await runtime.stop()
        current, peak = tracemalloc.get_traced_memory()
        print(json.dumps({
            "final": True,
            "elapsed_s": round(time.monotonic() - started, 1),
            "health_failures": failures,
            "consecutive_healthy_s": round(consecutive_healthy_s, 1),
            "er2_reconnect_events": runtime.cerebellum.reconnect_events,
            "posture_avg_hz": round(runtime.posture.frames_sent / max(time.monotonic() - started, 1), 2),
            "memory_rows": memory_rows(config.scribe.db_path),
            "mem_current": current,
            "mem_peak": peak,
        }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
