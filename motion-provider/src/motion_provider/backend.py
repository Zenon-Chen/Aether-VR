from __future__ import annotations

import importlib.util
import os
import platform
import random
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Diagnostics:
    backend: str
    device: str
    device_name: str
    dtype: str
    torch_version: str | None
    runtime_version: str | None
    supported: bool
    reason: str
    capabilities: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _torch():
    try:
        import torch
        return torch
    except ImportError:
        return None


def detect(requested: str = "auto", device: str | None = None, dtype: str = "float32") -> Diagnostics:
    allowed = {"auto", "mock", "cuda", "rocm", "xpu", "cpu"}
    if requested not in allowed:
        raise ValueError(f"unsupported backend: {requested}")
    if dtype not in {"float32", "float16", "bfloat16"}:
        raise ValueError(f"unsupported dtype: {dtype}")

    torch = _torch()
    system = platform.system()
    cuda = bool(torch and torch.cuda.is_available())
    hip_version = getattr(getattr(torch, "version", None), "hip", None) if torch else None
    rocm = bool(cuda and hip_version)
    xpu = bool(torch and hasattr(torch, "xpu") and torch.xpu.is_available())

    chosen = requested
    reason = "explicit selection"
    if requested == "auto":
        chosen = "rocm" if rocm else "cuda" if cuda else "xpu" if xpu else "cpu"
        reason = "auto-detected"

    expected_prefix = "cuda" if chosen in {"cuda", "rocm"} else chosen
    actual_device = device or expected_prefix
    prefix = actual_device.split(":", 1)[0]
    if chosen != "mock" and prefix != expected_prefix:
        raise ValueError(f"device {actual_device!r} is incompatible with backend {chosen!r}")
    if ":" in actual_device:
        suffix = actual_device.split(":", 1)[1]
        if not suffix.isdigit():
            raise ValueError(f"invalid device: {actual_device}")

    if chosen == "mock":
        return Diagnostics("mock", "mock", "deterministic mock", dtype,
                           getattr(torch, "__version__", None), None, True,
                           "explicit deterministic test backend", {"mock": True, "generation": "mock"})

    supported = (
        chosen == "cpu"
        or (chosen == "cuda" and cuda and not rocm)
        or (chosen == "rocm" and rocm and system == "Linux")
        or (chosen == "xpu" and xpu)
    )
    if torch is None:
        supported = False
        reason = "PyTorch is not installed"
    elif chosen == "rocm" and system != "Linux":
        reason = "PyTorch ROCm is supported by this provider on Linux only; Windows AMD is unsupported"
    elif chosen == "cuda" and rocm:
        reason = "installed PyTorch runtime is ROCm, not NVIDIA CUDA"
    elif not supported:
        reason = f"requested {chosen} device is unavailable"
    if dtype != "float32" and chosen != "mock":
        supported = False
        reason = "DART inference currently supports dtype=float32 only"

    device_name = "CPU"
    runtime_version = None
    if torch and chosen in {"cuda", "rocm"} and cuda:
        device_name = torch.cuda.get_device_name(actual_device)
        runtime_version = str(hip_version if chosen == "rocm" else getattr(torch.version, "cuda", None))
    elif torch and chosen == "xpu" and xpu:
        device_name = torch.xpu.get_device_name(actual_device)
        runtime_version = str(getattr(torch, "xpu", None).__class__.__module__)

    capabilities: dict[str, Any] = {
        "platform": system,
        "cuda_available": cuda and not rocm,
        "rocm_available": rocm and system == "Linux",
        "xpu_available": xpu,
        "cpu_available": True,
        "generation": "real",
    }
    if chosen == "xpu":
        capabilities.update({"experimental": True, "verified": False,
                             "note": "DART deep operators on Intel XPU are experimental and unverified"})
    return Diagnostics(chosen, actual_device, device_name, dtype,
                       getattr(torch, "__version__", None), runtime_version,
                       supported, reason, capabilities)


class MockBackend:
    def __init__(self, diagnostics: Diagnostics):
        self.diagnostics = diagnostics
        self.loaded = False

    async def start(self) -> None:
        self.loaded = True

    async def stop(self) -> None:
        self.loaded = False

    def generate(self, text: str, batch_size: int = 1, guidance_param: float = 5.0) -> dict[str, Any]:
        if not self.loaded:
            raise RuntimeError("backend is not started")
        count = sum(int(part.rsplit("*", 1)[1]) for part in text.split(","))
        frames = max(1, count * 10)
        return {
            "poses": [[0.0] * 165 for _ in range(frames)],
            "trans": [[0.0] * 3 for _ in range(frames)],
            "betas": [0.0] * 10,
            "joints": [[[0.0, 0.0, 0.0] for _ in range(22)] for _ in range(frames)],
            "framerate": 30,
            "num_frames": frames,
            "text_prompt": text,
        }


class DartBackend:
    """Loads the existing DART model store without invoking dart/server.py lifespan."""

    def __init__(self, diagnostics: Diagnostics, dart_root: str, checkpoint: str | None):
        self.diagnostics = diagnostics
        self.root = Path(dart_root).resolve()
        self.checkpoint = Path(checkpoint).resolve() if checkpoint else (
            self.root / "mld_denoiser" / "mld_fps_clip_repeat_euler" / "checkpoint_300000.pt"
        )
        self.module = None
        self.loaded = False
        self._cwd_lock = threading.Lock()

    def _import_server(self):
        server = self.root / "server.py"
        if not server.is_file():
            raise RuntimeError(f"DART server.py not found under DART_ROOT: {self.root}")
        spec = importlib.util.spec_from_file_location("motion_provider_external_dart", server)
        if not spec or not spec.loader:
            raise RuntimeError(f"cannot import DART server: {server}")
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(self.root))
        old_cwd = os.getcwd()
        try:
            spec.loader.exec_module(module)
        finally:
            os.chdir(old_cwd)
            if sys.path[0] == str(self.root):
                sys.path.pop(0)
        return module

    async def start(self) -> None:
        if self.loaded:
            return
        if not self.checkpoint.is_file():
            raise RuntimeError(f"DART checkpoint not found: {self.checkpoint}")
        torch = _torch()
        if torch is None:
            raise RuntimeError("PyTorch is not installed")
        device = torch.device(self.diagnostics.device)
        module = self._import_server()
        old_cwd = os.getcwd()
        original_load = torch.load

        def mapped_load(*args, **kwargs):
            kwargs.setdefault("map_location", device)
            return original_load(*args, **kwargs)

        try:
            os.chdir(self.root)
            torch.load = mapped_load
            random.seed(0)
            module.np.random.seed(0)
            torch.manual_seed(0)
            torch.set_default_dtype(torch.float32)
            denoiser_args, denoiser_model, vae_args, vae_model = module.load_mld(str(self.checkpoint), device)
            diffusion_args = denoiser_args.diffusion_args
            diffusion_args.respacing = module.DEFAULT_RESPACING
            dataset_path = "./data/stand.pkl" if module.DATASET_TYPE == "babel" else "./data/stand_20fps.pkl"
            store = module.store
            store.denoiser_args = denoiser_args
            store.denoiser_model = denoiser_model
            store.vae_args = vae_args
            store.vae_model = vae_model
            store.diffusion = module.create_gaussian_diffusion(diffusion_args)
            store.dataset = module.SinglePrimitiveDataset(
                cfg_path=vae_args.data_args.cfg_path,
                dataset_path=vae_args.data_args.data_dir,
                body_type=vae_args.data_args.body_type,
                sequence_path=dataset_path,
                batch_size=module.DEFAULT_BATCH_SIZE,
                device=device,
                enforce_gender="male",
                enforce_zero_beta=1,
            )
            import asyncio
            store.lock = asyncio.Lock()
            store.device = device
            store.loaded = True
            self.module = module
            self.loaded = True
        except Exception as exc:
            raise RuntimeError(
                f"failed to load DART on {self.diagnostics.backend}/{self.diagnostics.device}: {exc}"
            ) from exc
        finally:
            torch.load = original_load
            os.chdir(old_cwd)

    async def stop(self) -> None:
        if self.module is not None:
            self.module.store.loaded = False
        self.loaded = False
        self.module = None
        if self.diagnostics.backend in {"cuda", "rocm"}:
            torch = _torch()
            if torch:
                torch.cuda.empty_cache()
        elif self.diagnostics.backend == "xpu":
            torch = _torch()
            if torch and hasattr(torch.xpu, "empty_cache"):
                torch.xpu.empty_cache()

    def generate(self, text: str, batch_size: int = 1, guidance_param: float = 5.0) -> dict[str, Any]:
        if not self.loaded or self.module is None:
            raise RuntimeError("backend is not started")
        with self._cwd_lock:
            old_cwd = os.getcwd()
            try:
                os.chdir(self.root)
                return self.module.generate_motion(text, batch_size, guidance_param)
            finally:
                os.chdir(old_cwd)


def load_backend(config):
    diagnostics = detect(config.backend, config.device, config.dtype)
    if not diagnostics.supported:
        raise RuntimeError(diagnostics.reason)
    if diagnostics.backend == "mock":
        return MockBackend(diagnostics)
    if not config.dart_root:
        raise RuntimeError("DART_ROOT is required for every real backend; select --backend mock explicitly for mock generation")
    return DartBackend(diagnostics, config.dart_root, config.checkpoint)
