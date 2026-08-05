from motion_provider.backend import detect, MockBackend
from motion_provider.config import Config
from motion_provider.service import Service

def test_cpu_detection_without_torch_requirement():
    d=detect("cpu"); assert d.backend == "cpu"; assert "platform" in d.capabilities

def test_mock_end_to_end():
    s=Service(Config(backend="mock")); assert s.health()["status"] == "ok"
    out=s.generate({"text":"walk*2"}); assert out["num_frames"] == 20

def test_rocm_windows_not_claimed(monkeypatch):
    monkeypatch.setattr("motion_provider.backend.platform.system", lambda: "Windows")
    d=detect("rocm"); assert not d.supported
