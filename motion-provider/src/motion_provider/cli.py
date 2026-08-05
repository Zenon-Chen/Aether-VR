from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace

from .config import Config
from .service import Service


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="motion-provider")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--generate")
    parser.add_argument("--backend", choices=["auto", "mock", "cpu", "cuda", "rocm", "xpu"])
    parser.add_argument("--device")
    parser.add_argument("--dtype", choices=["float32", "float16", "bfloat16"])
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)
    config = Config.from_env()
    config = replace(
        config,
        backend=args.backend or config.backend,
        device=args.device or config.device,
        dtype=args.dtype or config.dtype,
    )
    if args.serve:
        try:
            import uvicorn
            from .http import create_app
        except ImportError:
            parser.error("server dependencies unavailable; install aether-motion-provider[server]")
        uvicorn.run(create_app(config=config), host=config.host, port=config.port)
        return 0

    service = Service(config)
    if args.diagnose:
        print(json.dumps(service.diagnostics, indent=2))
        return 0
    if args.generate is None:
        parser.error("choose --diagnose, --generate TEXT, or --serve")

    async def run() -> dict:
        await service.start()
        try:
            return service.generate({"text": args.generate})
        finally:
            await service.stop()

    print(json.dumps(asyncio.run(run()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
