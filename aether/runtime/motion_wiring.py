"""motion_wiring — 动作生成器装配与调度器 I/O 宽限计算

将「连接外部固定端口 DART」与「由 MotionProviderSupervisor 拉起受管 sidecar」
统一收敛到本模块，使 PostureService 与 AetherRuntime 保持对具体后端的无感知。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..contracts.plan import ActionKind, Plan
from ..infra.config import AetherConfig
from ..services.motion.providers.dart import DartMotionProvider
from ..services.motion.supervised import SupervisedMotionProvider
from ..services.motion.supervisor import BundleManifest, MotionProviderSupervisor
from ..services.ports import MotionProviderPort


def create_motion_provider(config: AetherConfig) -> MotionProviderPort:
    """根据 config.motion.mode 构建对应的 MotionProviderPort 实现。"""
    mode = (config.motion.mode or "external").strip().lower()
    if mode == "supervised":
        if not config.motion.bundle_manifest:
            raise ValueError("motion.mode='supervised' 需要配置 motion.bundle_manifest")
        manifest_path = Path(config.motion.bundle_manifest)
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = BundleManifest.from_dict(raw)
        bundle_dir = (
            Path(config.motion.bundle_dir)
            if config.motion.bundle_dir
            else manifest_path.parent
        )
        supervisor = MotionProviderSupervisor(
            manifest,
            bundle_dir,
            allow_unreleased=config.motion.allow_unreleased_bundle,
        )
        return SupervisedMotionProvider(
            supervisor,
            lambda base_url, token: DartMotionProvider(
                base_url, token=token, timeout_s=config.dart.timeout_s
            ),
        )

    if mode != "external":
        raise ValueError(f"不支持的 motion.mode: {config.motion.mode}")

    return DartMotionProvider(
        f"http://{config.dart.host}:{config.dart.port}",
        timeout_s=config.dart.timeout_s,
    )


def compute_plan_io_allowance(plan: Plan, config: AetherConfig) -> float:
    """计算计划所需的云端 TTS / GPU 动作生成额外墙钟宽限（秒）。

    编排动作时长仍受 max_plan_seconds (5s) 严格约束，而网络合成/推理耗时
    通过此宽限独立预算，避免多段 TTS 或 DART 生成被 5s 墙钟硬截断。
    """
    allowance = 0.0
    if any(step.action is ActionKind.SPEAK for step in plan.steps):
        allowance += max(0.0, float(config.speech.synthesis_allowance_s))
    if any(step.action is ActionKind.GENERATE_MOTION for step in plan.steps):
        allowance += max(0.0, float(config.motion.synthesis_allowance_s))
    return allowance
