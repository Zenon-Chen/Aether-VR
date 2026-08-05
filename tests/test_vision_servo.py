import asyncio

import pytest

from aether.contracts.vision import TargetObservation
from aether.infra.config import CaptureConfig
from aether.services.capture import CaptureService
from aether.services.vision import DegradedVisionTracker, OpenCVPersonTracker, VisionService, create_tracker


def obs(x=.45, h=.1, confidence=.9):
    return TargetObservation("person", confidence, x * 100, 10, 10, h * 100, 100, 100)


class Tracker:
    degraded = False
    def __init__(self, batches): self.batches = iter(batches)
    async def detect(self, frame): return next(self.batches)


class Loco:
    def __init__(self): self.calls = []; self.halts = 0
    async def move(self, direction, span): self.calls.append(direction)
    async def halt(self): self.halts += 1


async def frames(): return object()


@pytest.mark.asyncio
async def test_walk_look_walk_and_arrival_stop():
    loco = Loco()
    service = VisionService(Tracker([(obs(x=.8),), (obs(h=.15),), (obs(h=.3),)]))
    result = await service.approach_target("person", frame_source=frames, locomotion=loco, near_ratio=.25)
    assert loco.calls == ["turn_right", "forward"]
    assert result.reason == "near" and loco.halts == 1


@pytest.mark.asyncio
async def test_lost_target_stops():
    loco = Loco()
    result = await VisionService(Tracker([()])).approach_target("person", frame_source=frames, locomotion=loco)
    assert not loco.calls and result.reason == "target_lost_or_low_confidence" and loco.halts == 1


@pytest.mark.asyncio
async def test_cancel_stops():
    gate = asyncio.Event()
    async def waiting_frame():
        await gate.wait()
        return object()
    loco = Loco(); service = VisionService(Tracker([(obs(),)]))
    task = asyncio.create_task(service.approach_target("person", frame_source=waiting_frame, locomotion=loco))
    await asyncio.sleep(0); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert loco.halts == 1


class FakeHog:
    builds = 0
    def __init__(self): FakeHog.builds += 1
    def setSVMDetector(self, detector): pass
    def detectMultiScale(self, image, **kwargs):
        return [(60, 0, 20, 40), (10, 0, 40, 80)], [2.0, 1.0]


class FakeCV2:
    HOGDescriptor = FakeHog
    IMREAD_COLOR = 1
    decoded = object()
    @staticmethod
    def HOGDescriptor_getDefaultPeopleDetector(): return object()
    @staticmethod
    def imdecode(_encoded, _mode): return FakeCV2.decoded


def test_auto_opencv_detector_is_real_and_model_reused():
    FakeHog.builds = 0
    tracker = create_tracker("auto", cv2_module=FakeCV2)
    assert isinstance(tracker, OpenCVPersonTracker)
    assert not tracker.degraded and FakeHog.builds == 1


@pytest.mark.asyncio
async def test_opencv_returns_stable_normalized_candidates():
    tracker = OpenCVPersonTracker(FakeCV2)
    found = await tracker.detect(FakeImage())
    assert [item.target_id for item in found] == ["person:0", "person:1"]
    assert found[0].x == 10 and found[0].frame_width == 100
    assert 0.0 <= found[0].confidence <= 1.0


@pytest.mark.asyncio
async def test_opencv_decodes_capture_video_frame_jpeg():
    from aether.contracts.perception import VideoFrame

    tracker = OpenCVPersonTracker(FakeCV2)
    FakeCV2.decoded = FakeImage()
    found = await tracker.detect(VideoFrame(b"jpeg", 100, 100))
    assert found and found[0].frame_width == 100


class FakeImage:
    shape = (100, 100, 3)


@pytest.mark.asyncio
async def test_fresh_frame_disabled_does_not_hang():
    capture = CaptureService(CaptureConfig(enabled=False))
    await capture.start()
    assert await asyncio.wait_for(capture.fresh_frame(), .1) is None


@pytest.mark.asyncio
async def test_frame_source_timeout_stops():
    async def blocked(): await asyncio.Event().wait()
    loco = Loco()
    result = await VisionService(Tracker([])).approach_target(
        "person", frame_source=blocked, locomotion=loco, timeout_s=.01
    )
    assert result.reason == "timeout" and loco.halts == 1


@pytest.mark.asyncio
async def test_degraded_never_moves():
    loco = Loco()
    result = await VisionService(DegradedVisionTracker()).approach_target(
        "person", frame_source=frames, locomotion=loco
    )
    assert result.reason == "degraded" and result.degraded
    assert not loco.calls and loco.halts == 1
