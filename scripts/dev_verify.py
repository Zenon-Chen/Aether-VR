"""本机 dev 验证入口：只运行可重复、无云端、无真实硬件的检查。

用法：
    python scripts/dev_verify.py
    python scripts/dev_verify.py --build-provider
    python scripts/dev_verify.py --full

--full 仍只包含本机自动化；它不会启动 VRChat、SteamVR、WSL、Gemini 或麦克风。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDER = ROOT / "motion-provider"
PYTHON = sys.executable


def run(label: str, args: list[str], *, cwd: Path = ROOT) -> None:
    print(f"\n== {label} ==", flush=True)
    print("$", " ".join(args), flush=True)
    result = subprocess.run(args, cwd=cwd, text=True)
    if result.returncode:
        raise SystemExit(f"{label} failed with exit code {result.returncode}")


def verify_files() -> None:
    required = (
        ROOT / "config.yaml",
        ROOT / "assets" / "filler.wav",
        ROOT / "docs" / "personas" / "aether-default.yaml",
        PROVIDER / "bundle-manifest.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("missing required files:\n" + "\n".join(missing))

    import wave

    with wave.open(str(ROOT / "assets" / "filler.wav"), "rb") as wav:
        audio = (
            wav.getnchannels(),
            wav.getsampwidth(),
            wav.getframerate(),
            wav.getnframes(),
        )
    if audio != (1, 2, 24000, audio[3]) or audio[3] <= 0:
        raise SystemExit(f"invalid fallback WAV metadata: {audio}")

    manifest = json.loads((PROVIDER / "bundle-manifest.json").read_text(encoding="utf-8"))
    required_keys = {"provider_id", "version", "executable", "args", "workdir", "env"}
    if not required_keys.issubset(manifest):
        raise SystemExit(f"manifest missing keys: {sorted(required_keys - set(manifest))}")
    print(f"files: ok; fallback_wav={audio}; manifest={manifest['provider_id']}@{manifest['version']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-provider", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="run the local UDP/core smoke test")
    parser.add_argument("--full", action="store_true", help="run all local automated checks")
    args = parser.parse_args()

    os.chdir(ROOT)
    print(f"Aether dev verify | root={ROOT} | python={PYTHON}")
    if args.full:
        print("mode: full local suite")
    verify_files()

    # The current dev suite is small enough to run in one pass. Keep --full as
    # a forward-compatible flag, but do not run the same tests twice.
    run("Aether full tests" if args.full else "Aether tests", [PYTHON, "-m", "pytest", "-q"])

    run("Aether compile check", [PYTHON, "-m", "compileall", "-q", "aether"])
    if args.smoke:
        run("Aether core/UDP smoke", [PYTHON, "-m", "aether.runtime.smoke"])

    provider_python = PROVIDER / ".venv" / "Scripts" / "python.exe"
    if provider_python.is_file():
        provider_python_cmd = str(provider_python)
    else:
        # The provider tests are stdlib-only; reuse the root interpreter when
        # its package-test dependencies are already available.
        provider_python_cmd = PYTHON
        print("motion-provider/.venv not found; reusing root Python for provider tests")
        print("optional isolated setup: cd motion-provider; py -m venv .venv; .venv\\Scripts\\python -m pip install -e '.[server,test]'")

    run(
        "Motion provider tests",
        [provider_python_cmd, "-m", "pytest", "-q"],
        cwd=PROVIDER,
    )
    if args.build_provider:
        run("Motion provider package build", [provider_python_cmd, "-m", "build"], cwd=PROVIDER)

    print("\nPASS: local dev checks completed; no cloud, WSL, SteamVR, VRChat, or audio hardware was used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
