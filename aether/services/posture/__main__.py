"""独立运行 posture 服务

    python -m aether.services.posture --prompt "walk forward" --duration 3

说明：
    库模块 services/posture 严格保持供应商中立、零跨服务导入；
    命令行入口在 __main__ 执行时委托给 runtime.posture_cli 完成 Provider 装配。
"""

from __future__ import annotations

import asyncio


if __name__ == "__main__":
    from ...runtime.posture_cli import main

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
