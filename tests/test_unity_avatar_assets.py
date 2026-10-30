"""阶段 4：Unity VRChat Avatars 3.0 控制器拆分、肌肉曲线绑定与 15 视素静态契约测试。"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AVATAR_DIR = REPO_ROOT / "avatar-project" / "Assets" / "Aether"
GEN_DIR = AVATAR_DIR / "Generated"

EXPECTED_ACTIONS = (
    "wave",
    "nod",
    "shake_head",
    "bow",
    "clap",
    "point",
    "think",
    "sit",
    "stand",
)

EXPECTED_EXPRESSIONS = ("smile", "surprised", "sad", "angry")

EXPECTED_15_VISEMES = [
    "vrc.v_sil",
    "vrc.v_pp",
    "vrc.v_ff",
    "vrc.v_th",
    "vrc.v_dd",
    "vrc.v_kk",
    "vrc.v_ch",
    "vrc.v_ss",
    "vrc.v_nn",
    "vrc.v_rr",
    "vrc.v_aa",
    "vrc.v_ee",
    "vrc.v_ih",
    "vrc.v_oh",
    "vrc.v_ou",
]

BROKEN_LEGACY_MUSCLE_NAMES = (
    "Right Arm Stretch",
    "Left Arm Stretch",
    "Right Elbow Flex",
    "Left Leg Upper Leg Front-Back",
    "Right Leg Upper Leg Front-Back",
    "Left Leg Lower Leg Stretch",
    "Right Leg Lower Leg Stretch",
)


def _split_yaml_docs(text: str) -> list[str]:
    return [chunk for chunk in re.split(r"(?m)^--- !u!", text) if chunk.strip()]


def _extract_guid(meta_path: Path) -> str:
    match = re.search(r"(?m)^guid:\s*([0-9a-fA-F]+)\s*$", meta_path.read_text(encoding="utf-8"))
    assert match, f"{meta_path} 中未找到 guid"
    return match.group(1)


def test_action_controller_has_valid_mecanim_bindings_and_layer_weight_control():
    action_ctrl = GEN_DIR / "AetherAction.controller"
    assert action_ctrl.is_file(), "缺少生成的 AetherAction.controller"
    raw = action_ctrl.read_text(encoding="utf-8")

    for bad_name in BROKEN_LEGACY_MUSCLE_NAMES:
        assert f"attribute: {bad_name}" not in raw, f"发现残留非法肌肉名: {bad_name}"
    assert "attribute: Right Arm Twist\n" not in raw

    clip_docs = [
        doc for doc in _split_yaml_docs(raw) if doc.lstrip().startswith("74 ")
    ]
    clip_by_name: dict[str, str] = {}
    for doc in clip_docs:
        m = re.search(r"(?m)^\s*m_Name:\s*(\S+)\s*$", doc)
        if m:
            clip_by_name[m.group(1)] = doc

    for name in EXPECTED_ACTIONS:
        clip_name = f"{name}_action"
        assert clip_name in clip_by_name, f"AetherAction.controller 缺少剪辑 {clip_name}"
        clip_yaml = clip_by_name[clip_name]
        assert "genericBindings: []" not in clip_yaml, (
            f"{clip_name} 的 genericBindings 为空，Mecanim 不会驱动骨骼"
        )
        assert "genericBindings:" in clip_yaml
        assert "classID: 95" in clip_yaml

    # shake_head 必须使用左右转头 Turn，而不是歪头 Tilt
    assert "Neck Turn Left-Right" in clip_by_name["shake_head_action"]
    assert "Neck Tilt Left-Right" not in clip_by_name["shake_head_action"]

    # VRChat Action 层必须包含 VRCPlayableLayerControl 切换权重 0 <-> 1
    assert "goalWeight: 1" in raw
    assert "goalWeight: 0" in raw
    assert "trackingLeftHand: 2" in raw
    assert "trackingLeftHand: 1" in raw


def test_fx_controller_uses_face_blendshapes_only_without_muscle_or_viseme_locks():
    fx_ctrl = GEN_DIR / "AetherFX.controller"
    assert fx_ctrl.is_file(), "缺少生成的 AetherFX.controller"
    raw = fx_ctrl.read_text(encoding="utf-8")

    assert "Spine Front-Back" not in raw, "FX 表情层不得污染 Spine Front-Back 骨骼肌肉"
    assert "classID: 95" not in raw, "FX 表情层不得包含 Animator 肌肉曲线"
    assert "blendShape.vrc.v_" not in raw, "FX 表情层不得绑定 vrc.v_* 口型视素以免锁死 LipSync"

    clip_docs = [
        doc for doc in _split_yaml_docs(raw) if doc.lstrip().startswith("74 ")
    ]
    clip_by_name: dict[str, str] = {}
    for doc in clip_docs:
        m = re.search(r"(?m)^\s*m_Name:\s*(\S+)\s*$", doc)
        if m:
            clip_by_name[m.group(1)] = doc

    for expr in EXPECTED_EXPRESSIONS:
        clip_name = f"{expr}_face"
        assert clip_name in clip_by_name, f"AetherFX.controller 缺少剪辑 {clip_name}"
        clip_yaml = clip_by_name[clip_name]
        assert "genericBindings: []" not in clip_yaml, f"{clip_name} 的 genericBindings 为空"
        assert "path: mesh_char_001" in clip_yaml
        assert "classID: 137" in clip_yaml
        assert "attribute: blendShape.vrc." in clip_yaml


def test_scene_descriptor_binds_action_and_fx_layers_and_exact_15_visemes():
    scene_path = AVATAR_DIR / "AetherAvatar.unity"
    raw = scene_path.read_text(encoding="utf-8")

    action_guid = _extract_guid(GEN_DIR / "AetherAction.controller.meta")
    fx_guid = _extract_guid(GEN_DIR / "AetherFX.controller.meta")

    assert action_guid in raw, "AetherAvatar.unity 未挂载 AetherAction.controller"
    assert fx_guid in raw, "AetherAvatar.unity 未挂载 AetherFX.controller"

    # 校验 Action (type: 4) 与 FX (type: 5) 均已启用自定义控制器（isDefault: 0）
    action_layer_pattern = re.compile(
        r"type:\s*4\s+animatorController:\s*\{[^}]*guid:\s*"
        + re.escape(action_guid)
        + r"[^}]*\}\s+mask:\s*\{[^}]*\}\s+isDefault:\s*0",
        re.DOTALL,
    )
    fx_layer_pattern = re.compile(
        r"type:\s*5\s+animatorController:\s*\{[^}]*guid:\s*"
        + re.escape(fx_guid)
        + r"[^}]*\}\s+mask:\s*\{[^}]*\}\s+isDefault:\s*0",
        re.DOTALL,
    )
    assert action_layer_pattern.search(raw), "Playable Layer Action (type: 4) 未正确绑定"
    assert fx_layer_pattern.search(raw), "Playable Layer FX (type: 5) 未正确绑定"

    # 校验 15 视素精确一对一映射，杜绝 vrc.blink_left 误绑
    viseme_block_match = re.search(
        r"VisemeBlendShapes:\n((?:\s*-\s*.*\n){15})", raw
    )
    assert viseme_block_match, "未找到 15 项 VisemeBlendShapes 列表"
    visemes = [
        line.split("-", 1)[1].strip()
        for line in viseme_block_match.group(1).strip().splitlines()
    ]
    assert visemes == EXPECTED_15_VISEMES
    assert "vrc.blink_left" not in visemes
    assert "vrc.lowerlid_left" not in visemes
