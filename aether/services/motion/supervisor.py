"""动作生成器 sidecar 的进程与版本生命周期管理。"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import httpx


@dataclass(frozen=True, slots=True)
class BundleManifest:
    provider_id: str
    version: str
    executable: str
    args: tuple[str, ...] = ()
    workdir: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    startup_timeout: float = 20.0
    max_restarts: int = 2
    health_path: str = "/health"
    log_path: str = "sidecar.log"
    sha256: str | None = None
    signature: str | None = None
    release_blockers: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BundleManifest":
        values = dict(data)
        values["args"] = tuple(values.get("args", ()))
        values["env"] = dict(values.get("env", {}))
        values["release_blockers"] = tuple(values.get("release_blockers", ()))
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "version": self.version,
            "executable": self.executable,
            "args": list(self.args),
            "workdir": self.workdir,
            "env": dict(self.env),
            "startup_timeout": self.startup_timeout,
            "max_restarts": self.max_restarts,
            "health_path": self.health_path,
            "log_path": self.log_path,
            "sha256": self.sha256,
            "signature": self.signature,
            "release_blockers": list(self.release_blockers),
        }

    def verify_integrity(
        self, bundle_dir: Path, *, allow_unreleased: bool = True
    ) -> None:
        if not allow_unreleased and self.release_blockers:
            raise RuntimeError(
                f"bundle {self.provider_id}@{self.version} has release blockers: "
                + "; ".join(self.release_blockers)
            )
        if self.sha256:
            target = Path(self.executable.replace("{bundle_dir}", str(bundle_dir)))
            if not target.is_file():
                raise FileNotFoundError(f"bundle executable not found for sha256 check: {target}")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest.lower() != self.sha256.strip().lower():
                raise ValueError(
                    f"bundle executable sha256 mismatch: expected {self.sha256}, got {digest}"
                )


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int
    token: str
    bundle_dir: Path

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class ProcessStatus:
    running: bool
    pid: int | None
    restarts: int
    healthy: bool = False
    exit_code: int | None = None
    error: str | None = None


class MotionProviderSupervisor:
    """Own one provider process, endpoint credential, probe, and process tree."""

    def __init__(
        self,
        manifest: BundleManifest,
        bundle_dir: str | Path,
        *,
        client: httpx.AsyncClient | None = None,
        allow_unreleased: bool = True,
    ) -> None:
        self.manifest = manifest
        self.bundle_dir = Path(bundle_dir)
        self.endpoint: Endpoint | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task | None = None
        self._log = None
        self._restarts = 0
        self._last_error: str | None = None
        self._client = client
        self._owns_client = client is None
        self._probe_client: httpx.AsyncClient | None = None
        self._allow_unreleased = allow_unreleased
        self._job = None
        self._stopping = False

    def _get_http_client(self, timeout: httpx.Timeout | float) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if self._probe_client is None:
            # 本地回环 HTTP 边车无需加载系统 SSL 根证书库（避免 Windows 下 ~200ms 初始化阻塞），
            # 且必须禁用环境变量代理（避免 Clash/系统代理劫持 127.0.0.1 探针）。
            self._probe_client = httpx.AsyncClient(
                timeout=timeout, trust_env=False, verify=False
            )
        return self._probe_client

    async def start(self) -> Endpoint:
        if self._process and self._process.returncode is None and self.endpoint:
            return self.endpoint
        self.manifest.verify_integrity(
            self.bundle_dir, allow_unreleased=self._allow_unreleased
        )
        self._stopping = False
        self.endpoint = self.endpoint or Endpoint(
            "127.0.0.1", _free_port(), secrets.token_urlsafe(32), self.bundle_dir
        )
        # 在 _spawn 之前预热 HTTP 客户端，确保子进程一监听即可毫秒级命中探针
        self._get_http_client(httpx.Timeout(1.0, connect=0.04))
        last_error: Exception | None = None
        for attempt in range(self.manifest.max_restarts + 1):
            if attempt:
                self._restarts += 1
                await asyncio.sleep(min(0.25 * 2 ** (attempt - 1), 2.0))
            try:
                await self._spawn()
                await self._probe_until_ready()
                self._monitor_task = asyncio.create_task(
                    self._monitor(), name=f"motion_sidecar_monitor_{self.manifest.version}"
                )
                return self.endpoint
            except Exception as exc:
                last_error = exc
                self._last_error = str(exc)
                await self._terminate()
        raise RuntimeError(
            f"sidecar failed startup after {self._restarts} restarts: {last_error}"
        ) from last_error

    async def _spawn(self) -> None:
        assert self.endpoint
        replacements = {
            "host": self.endpoint.host,
            "port": str(self.endpoint.port),
            "token": self.endpoint.token,
            "bundle_dir": str(self.bundle_dir),
        }

        def expand(value: str) -> str:
            # Expand only supervisor placeholders; sidecar source/args may contain
            # unrelated braces (for example Python dict literals).
            for key, replacement in replacements.items():
                value = value.replace("{" + key + "}", replacement)
            return value

        executable = expand(self.manifest.executable)
        args = [expand(arg) for arg in self.manifest.args]
        workdir = expand(self.manifest.workdir or str(self.bundle_dir))
        env = os.environ.copy()
        env.update({key: expand(value) for key, value in self.manifest.env.items()})
        env.update(
            {
                "MOTION_PROVIDER_HOST": self.endpoint.host,
                "MOTION_PROVIDER_PORT": str(self.endpoint.port),
                "MOTION_PROVIDER_TOKEN": self.endpoint.token,
                "MOTION_BUNDLE_DIR": str(self.bundle_dir),
            }
        )
        log_path = self.bundle_dir / expand(self.manifest.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = log_path.open("ab")
        kwargs: dict[str, Any] = {
            "cwd": workdir,
            "env": env,
            "stdout": self._log,
            "stderr": self._log,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self._process = await asyncio.create_subprocess_exec(executable, *args, **kwargs)
        if os.name == "nt":
            self._assign_windows_job(self._process.pid)

    async def _probe_until_ready(self) -> None:
        assert self.endpoint
        probe_timeout = httpx.Timeout(1.0, connect=0.04)
        client = self._get_http_client(probe_timeout)
        deadline = time.monotonic() + self.manifest.startup_timeout
        while time.monotonic() < deadline:
            if not self._process or self._process.returncode is not None:
                raise RuntimeError("sidecar exited during startup")
            try:
                response = await client.get(
                    self.endpoint.base_url + self.manifest.health_path,
                    headers={"Authorization": f"Bearer {self.endpoint.token}"},
                    timeout=probe_timeout,
                )
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.01)
        raise TimeoutError("sidecar health probe timed out")

    async def _monitor(self) -> None:
        while not self._stopping:
            process = self._process
            if process is None:
                return
            await process.wait()
            if self._stopping or process is not self._process:
                return
            self._process = None
            self._close_log()
            if self._restarts >= self.manifest.max_restarts:
                self._last_error = f"sidecar exited with code {process.returncode}"
                return
            self._restarts += 1
            await asyncio.sleep(min(0.25 * 2 ** (self._restarts - 1), 2.0))
            try:
                await self._spawn()
                await self._probe_until_ready()
            except Exception as exc:
                self._last_error = str(exc)
                await self._terminate()

    @staticmethod
    def _win32_kernel():
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        return kernel

    def _close_job(self) -> None:
        if os.name == "nt" and self._job:
            self._win32_kernel().CloseHandle(self._job)
            self._job = None

    def _assign_windows_job(self, pid: int) -> None:
        """Best-effort kill-on-job-close for the supervised process tree."""
        if os.name != "nt":
            return
        # 重启子进程前先释放上一轮遗留的 Job 句柄，防止内核句柄泄漏
        self._close_job()
        kernel = self._win32_kernel()
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            return

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters)]

        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel.CloseHandle(job)
            return
        process_handle = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process_handle:
            kernel.CloseHandle(job)
            return
        assigned = kernel.AssignProcessToJobObject(job, process_handle)
        kernel.CloseHandle(process_handle)
        if assigned:
            self._job = job
        else:
            kernel.CloseHandle(job)

    async def _terminate(self) -> None:
        process, self._process = self._process, None
        if process and process.returncode is None:
            if os.name == "nt":
                process.terminate()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), 2.0)
            except asyncio.TimeoutError:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await process.wait()
        self._close_job()
        self._close_log()

    def _close_log(self) -> None:
        if self._log:
            self._log.close()
            self._log = None

    async def stop(self) -> None:
        self._stopping = True
        if self._monitor_task:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        await self._terminate()

    async def close(self) -> None:
        await self.stop()
        if self._probe_client is not None:
            await self._probe_client.aclose()
            self._probe_client = None
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    def status(self) -> ProcessStatus:
        process = self._process
        running = bool(process and process.returncode is None)
        return ProcessStatus(
            running=running,
            pid=process.pid if process else None,
            restarts=self._restarts,
            healthy=running and self.endpoint is not None,
            exit_code=process.returncode if process else None,
            error=self._last_error,
        )

    async def health(self) -> bool:
        if not self.endpoint or not self._process or self._process.returncode is not None:
            return False
        client = self._get_http_client(2.0)
        try:
            response = await client.get(
                self.endpoint.base_url + self.manifest.health_path,
                headers={"Authorization": f"Bearer {self.endpoint.token}"},
                timeout=2.0,
            )
            return response.status_code == 200
        except httpx.HTTPError:
            return False


class BundleRegistry:
    """Small persistent manifest registry with candidate probe and rollback."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._index_path = self.root / "manifests.json"
        self.active: str | None = None
        self._previous: list[str] = []
        self._staged: dict[str, tuple[BundleManifest, Path]] = {}
        self._supervisors: dict[str, MotionProviderSupervisor] = {}
        self._load()

    def stage(
        self, manifest: BundleManifest | Mapping[str, Any], bundle_dir: str | Path
    ) -> str:
        item = manifest if isinstance(manifest, BundleManifest) else BundleManifest.from_dict(manifest)
        directory = Path(bundle_dir)
        self._staged[item.version] = (item, directory)
        record = self.root / item.provider_id / item.version
        record.mkdir(parents=True, exist_ok=True)
        (record / "manifest.json").write_text(
            json.dumps({**item.to_dict(), "bundle_dir": str(directory)}, indent=2),
            encoding="utf-8",
        )
        self._save()
        return item.version

    async def probe(self, version: str) -> bool:
        manifest, directory = self._staged[version]
        supervisor = MotionProviderSupervisor(manifest, directory)
        self._supervisors[version] = supervisor
        try:
            await supervisor.start()
            return await supervisor.health()
        except Exception:
            await supervisor.close()
            self._supervisors.pop(version, None)
            return False

    async def activate(self, version: str) -> Endpoint:
        if version not in self._staged:
            raise KeyError(version)
        if version != self.active and not await self.probe(version):
            raise RuntimeError(f"bundle {version} failed probe")
        old = self.active
        if old and old != version:
            self._previous.append(old)
            old_supervisor = self._supervisors.pop(old, None)
            if old_supervisor:
                await old_supervisor.close()
        self.active = version
        self._save()
        endpoint = self._supervisors[version].endpoint
        assert endpoint is not None
        return endpoint

    async def rollback(self) -> Endpoint | None:
        if not self._previous:
            sup = self._supervisors.get(self.active) if self.active else None
            return sup.endpoint if sup is not None else None
        target = self._previous.pop()
        current = self.active
        if not await self.probe(target):
            self._previous.append(target)
            raise RuntimeError(f"rollback target {target} failed probe")
        if current and current != target:
            old_supervisor = self._supervisors.pop(current, None)
            if old_supervisor:
                await old_supervisor.close()
        self.active = target
        self._save()
        endpoint = self._supervisors[target].endpoint
        assert endpoint is not None
        return endpoint

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(
            json.dumps({
                "active": self.active,
                "previous": self._previous,
                "versions": {
                    version: {
                        "manifest": manifest.to_dict(),
                        "bundle_dir": str(directory),
                    }
                    for version, (manifest, directory) in self._staged.items()
                },
            }, indent=2),
            encoding="utf-8",
        )

    def _load(self) -> None:
        if not self._index_path.exists():
            return
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            self.active = data.get("active")
            self._previous = list(data.get("previous", []))
            for version, item in data.get("versions", {}).items():
                self._staged[version] = (
                    BundleManifest.from_dict(item["manifest"]),
                    Path(item["bundle_dir"]),
                )
        except (OSError, ValueError, KeyError, TypeError):
            self.active = None
            self._previous = []
            self._staged = {}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


__all__ = [
    "BundleManifest", "Endpoint", "ProcessStatus", "MotionProviderSupervisor",
    "BundleRegistry",
]
