"""高频视觉伺服：看一帧、一个移动脉冲、再看一帧。"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Callable, Optional

from ...contracts.vision import TargetObservation, VisionStatus, VisionTrackerPort


class DegradedVisionTracker(VisionTrackerPort):
    degraded = True

    async def detect(self, frame) -> tuple[TargetObservation, ...]:
        return ()


class OpenCVPersonTracker(VisionTrackerPort):
    """CPU HOG person detector. The model is constructed once and reused."""

    degraded = False

    def __init__(self, cv2_module=None) -> None:
        if cv2_module is None:
            import cv2 as cv2_module
        self._cv2 = cv2_module
        self._hog = cv2_module.HOGDescriptor()
        self._hog.setSVMDetector(cv2_module.HOGDescriptor_getDefaultPeopleDetector())

    async def detect(self, frame) -> tuple[TargetObservation, ...]:
        return await asyncio.to_thread(self._detect_sync, frame)

    def _detect_sync(self, frame) -> tuple[TargetObservation, ...]:
        image = self._decode(frame)
        if image is None:
            return ()
        height, width = image.shape[:2]
        boxes, weights = self._hog.detectMultiScale(
            image, winStride=(8, 8), padding=(8, 8), scale=1.05
        )
        candidates = []
        flat_weights = list(weights) if weights is not None else []
        for index, box in enumerate(boxes):
            x, y, w, h = (float(value) for value in box)
            raw = float(flat_weights[index]) if index < len(flat_weights) else 0.0
            confidence = max(0.0, min(1.0, 1.0 / (1.0 + math.exp(-raw))))
            candidates.append((x, y, w, h, confidence))
        # Stable deterministic initial choice: largest area, then closest to frame centre.
        candidates.sort(key=lambda item: (-(item[2] * item[3]), abs(item[0] + item[2] / 2 - width / 2)))
        return tuple(
            TargetObservation(f"person:{index}", confidence, x, y, w, h, width, height)
            for index, (x, y, w, h, confidence) in enumerate(candidates)
        )

    def _decode(self, frame):
        if frame is None:
            return None
        encoded_bytes = getattr(frame, "jpeg", None)
        if encoded_bytes is None:
            encoded_bytes = getattr(frame, "data", None)
        if encoded_bytes is not None:
            import numpy as np
            encoded = np.frombuffer(encoded_bytes, dtype=np.uint8)
            return self._cv2.imdecode(encoded, self._cv2.IMREAD_COLOR)
        return frame


def create_tracker(mode: str = "auto", *, cv2_module=None) -> VisionTrackerPort:
    if mode == "degraded":
        return DegradedVisionTracker()
    if mode not in {"auto", "opencv"}:
        raise ValueError(f"unsupported vision detector: {mode}")
    try:
        return OpenCVPersonTracker(cv2_module)
    except (ImportError, ModuleNotFoundError):
        if mode == "opencv":
            raise
        return DegradedVisionTracker()


class VisionService:
    name = "vision"

    def __init__(self, tracker: VisionTrackerPort | None = None, *, config=None, now: Callable[[], float] = time.monotonic) -> None:
        self.tracker = tracker or create_tracker(getattr(config, "detector", "auto"))
        self._now = now
        self.status = VisionStatus(degraded=getattr(self.tracker, "degraded", False))
        self._cancelled = False

    async def start(self) -> None:
        self._cancelled = False

    async def stop(self) -> None:
        self._cancelled = True

    async def health(self) -> str:
        return "degraded" if getattr(self.tracker, "degraded", False) else "ok"

    def cancel(self) -> None:
        self._cancelled = True

    async def approach_target(
        self, target_id: str, *, frame_source, locomotion, near_ratio: float = .25,
        confidence_min: float = .6, center_tolerance: float = .12, pulse_s: float = .4,
        timeout_s: float = 30.0, max_steps: int = 30, stagnation_steps: int = 4,
        detect_fps: float = 4.0,
    ) -> VisionStatus:
        self._cancelled = False
        started = self._now()
        previous_size: Optional[float] = None
        previous_target: TargetObservation | None = None
        stagnant = 0
        step = 0
        if getattr(self.tracker, "degraded", False):
            return await self._stop(locomotion, "degraded", 0, degraded=True)
        try:
            for step in range(1, max_steps + 1):
                if self._cancelled:
                    return await self._stop(locomotion, "cancelled", step)
                remaining = timeout_s - (self._now() - started)
                if remaining <= 0:
                    return await self._stop(locomotion, "timeout", step)
                try:
                    async with asyncio.timeout(remaining):
                        frame = await frame_source()
                        observations = await self.tracker.detect(frame) if frame is not None else ()
                except TimeoutError:
                    return await self._stop(locomotion, "timeout", step)
                target = self._select_target(target_id, observations, previous_target)
                if target is None or target.confidence < confidence_min:
                    return await self._stop(locomotion, "target_lost_or_low_confidence", step)
                previous_target = target
                size = target.height_ratio
                if size >= near_ratio:
                    return await self._stop(locomotion, "near", step, target)
                if previous_size is not None and size <= previous_size + 1e-4:
                    stagnant += 1
                else:
                    stagnant = 0
                if stagnant >= stagnation_steps:
                    return await self._stop(locomotion, "stagnation", step, target, degraded=True)
                previous_size = size
                error = target.center_x / max(1, target.frame_width) - .5
                direction = "turn_right" if error > center_tolerance else "turn_left" if error < -center_tolerance else "forward"
                self.status = VisionStatus("servo", target_id, target.confidence, error, size, step)
                await locomotion.move(direction, pulse_s)
                minimum_period = 1.0 / max(.1, detect_fps)
                elapsed = self._now() - started
                next_tick = step * minimum_period
                if elapsed < next_tick:
                    await asyncio.sleep(next_tick - elapsed)
        except asyncio.CancelledError:
            await locomotion.halt()
            self.status = VisionStatus("stopped", target_id, steps=step, reason="cancelled")
            raise
        except Exception as exc:
            await locomotion.halt()
            self.status = VisionStatus("stopped", target_id, steps=step, reason=f"error:{type(exc).__name__}")
            raise
        return await self._stop(locomotion, "max_steps", max_steps)

    @staticmethod
    def _select_target(target_id: str, observations, previous: TargetObservation | None):
        people = [item for item in observations if item.target_id.startswith("person:") or item.target_id == "person"]
        if not target_id.startswith("person"):
            return next((item for item in observations if item.target_id == target_id), None)
        if previous is None:
            if target_id != "person":
                return next((item for item in people if item.target_id == target_id), None)
            return min(people, key=lambda item: (-item.width * item.height, abs(item.center_x / max(1, item.frame_width) - .5)), default=None)
        # Keep the acquired person by overlap/proximity; never silently switch to a distant candidate.
        def distance(item):
            dx = (item.center_x - previous.center_x) / max(1, previous.frame_width)
            dy = ((item.y + item.height / 2) - (previous.y + previous.height / 2)) / max(1, previous.frame_height)
            return (dx * dx + dy * dy) ** .5
        candidate = min(people, key=distance, default=None)
        return candidate if candidate is not None and distance(candidate) <= .40 else None

    async def servo(self, *args, **kwargs) -> VisionStatus:
        return await self.approach_target(*args, **kwargs)

    async def _stop(self, locomotion, reason: str, steps: int, target: TargetObservation | None = None, degraded: bool = False) -> VisionStatus:
        await locomotion.halt()
        self.status = VisionStatus("stopped", target.target_id if target else "", target.confidence if target else 0.0, ((target.center_x / target.frame_width) - .5) if target else 0.0, target.height_ratio if target else 0.0, steps, reason, degraded)
        return self.status
