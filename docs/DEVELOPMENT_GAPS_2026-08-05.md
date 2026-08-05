# 2026-08-05 开发缺口落地说明

本文记录本轮针对 Avatar、人格/长上下文、视觉伺服、失败补偿和计划预算的实现，供参与开发者定位边界与验证方式。独立动作生成软件包详见 [`../motion-provider/README.md`](../motion-provider/README.md)。

## 1. Avatar Animator 与身体控制权

- Unity 生成器 `avatar-project/Assets/Aether/Editor/AetherAvatarSetup.cs` 现在为 FX idle 状态显式恢复 Tracking。
- 普通预录动作取得左右手 Animation 控制权；`bow` / `sit` / `stand` 取得 head、hands、hip、feet 的 Animation 控制权。
- `AetherFX.controller` 已通过 Unity 2022.3.22f1 batch mode 重新生成。
- Python Planner 对单步和多步 `play_action` 都标记 `full_body`；全身动作占用 `UPPER_BODY + LOWER_BODY + MOVEMENT`。
- Runtime 执行全身预录动作前停止移动并冻结 posture，结束、失败或取消后恢复 Animator idle 与 posture；上身动作不冻结下半身。

## 2. 明确、版本化的人格

默认人格位于 `docs/personas/aether-default.yaml`，包含稳定身份、价值观、表达风格、边界、关系记忆规则和响应 schema。Brain 启动时加载并暴露 persona id、version 和 SHA-256；system instruction 由人格 profile 与固定 JSON 输出契约组成。资源路径相对于 `config.yaml`，不依赖进程 cwd。

## 3. Scribe 活动长上下文

- pending 块在未达到 batch size 时也计入 `context_tokens` 并进入活动上下文。
- normal 块在活动上下文中只渲染、计算摘要，原文不再继续占用活动窗口。
- `render_active_context()` 把 pending/active 内容喂给 Brain，并与 SQLite recent memory 去重合流。
- important 二级驱逐按持久价值关键字、引用数和近期性评分，而不是简单 FIFO；驱逐内容写 SQLite 并保留 summary pointer。
- `metrics` 暴露 pending_blocks、active_blocks、active_tokens、persisted_blocks。

SQLite 检索目前仍是关键词/最近记录，不是向量 RAG。

## 4. 高频视觉伺服

`aether/services/vision/` 提供独立视觉契约与 OpenCV CPU HOG person tracker。Runtime 的 `approach_target()` 使用独立 fresh screenshot，不受 Brain/ER2 的 1 FPS 推送限制；CLI 可用 `/approach person` 触发。

闭环严格执行：观察 → 转向或前进一个 pulse → 重新观察。使用 bbox 高度比例判断“足够近”，不伪造绝对距离。以下情况一律停止并调用 locomotion halt：目标丢失、低置信、停滞、超时、最大步数、取消、异常、detector degraded。多人目标通过稳定候选与位置邻近维持，不可靠时停止而非静默换人。

当前没有声称真实 VRChat 人物识别已验收；已完成 fake detector/frame 的闭环与安全测试。

## 5. 补偿链与五秒硬预算

- DART 动作生成失败后，Runtime 按 scope 回退到配置中确实存在的预录动作；默认 `upper_body → wave`、`full_body → bow`。
- Speech 启动时验证 `assets/filler.wav` 为 PCM16、mono、24kHz、非空。TTS 失败时播放该非语义故障提示音；它只表示语音链路故障，不冒充原台词。缺失或播放失败会明确返回错误。
- Planner 对 ER2 单工具/多步计划做预计墙钟预算校验，拒绝负数、NaN、Infinity 和超过 `max_plan_seconds` 的计划。
- Scheduler 对直接构造的 Plan 再校验，并用真实 wall-clock deadline 强制取消，释放通道、清除 active plan、记录失败并触发完成回调。

## 6. 独立 Motion Provider 支持边界

`motion-provider/` 是独立 Python 软件包，提供 `/health`、`/status`、`/capabilities`、`/generate`、Bearer token、CLI、wheel/sdist、Aether Supervisor manifest 和显式 mock backend。

- NVIDIA：PyTorch CUDA。
- AMD：Linux PyTorch ROCm；不声称 Windows AMD/ROCm 可用。
- Intel：PyTorch XPU 已按 `xpu` 设备接线，不静默回退 CPU；DART 深层算子仍标记 experimental/unverified。
- CPU：真实 DART CPU 路径可选择，但性能未做承诺。
- 模型、checkpoint、CLIP、SMPL-X 等资产与 wheel 分离；必须单独配置 `DART_ROOT` / `DART_CHECKPOINT`。

真实 CUDA、ROCm、XPU 和完整 DART 模型尚未在本轮硬件上逐一运行，因此不得把包级测试写成硬件验收。

## 7. 直接证据

```powershell
python -m pytest -q
# 105 passed

cd motion-provider
python -m pytest -q
# 3 passed
python -m build
# wheel + sdist built
```

Unity FX Controller 已用：

```powershell
Unity.exe -batchmode -quit -projectPath avatar-project `
  -executeMethod Aether.AvatarSetup.GenerateFxController
```

完成序列化；这不等于已在 VRChat 客户端中进行真人实机验收。
