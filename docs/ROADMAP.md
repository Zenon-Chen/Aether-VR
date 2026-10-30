# Aether 路线图 · 架构 · TODO

> 生成日期：2026-08-03（项目重启梳理）。当前事实以 `ARCHITECTURE.md`、
> `DEVELOPMENT_GAPS_2026-08-05.md` 和代码为准；本文是"停了一段时间后回到项目"的入口文档。
> 设计源头：chat-export-1785765153227.md（8/2–8/3 构思对话，以其中「完整实施方案 + 最终清单」为对齐基准）。

## 1. 一句话现状

三层架构（大脑/小脑/调度器）的**软件链路已全部实现并有测试覆盖**（本机 107/108 通过），
构思对话中定稿的模块几乎逐条落地；**卡点不在写代码，而在实机验收**：
VRChat 可见动作验收工具刚做完还没跑、DART 真实 GPU 从未端到端运行、2 小时稳定性 soak 被暂停。

当前 git：`main`，最后一次提交 2026-08-06（`48e04ce`）。
工作区有一批**未提交**的「VRChat 可见动作验收」改动（见 TODO #1）。

## 2. 路线图

### 已完成 ✅

| 阶段 | 内容 | 佐证 |
|---|---|---|
| 阶段 1 | capture + ER2 Live 会话 + 固定心跳 + 预录动作 + move + TTS | `services/capture,cerebellum,speech,avatar,locomotion` |
| 阶段 2a | 大脑（Flash Lite，suggested_reply/wake_er2）+ 书记官（批量标记/三档/两级驱逐/落盘/滞回） | `services/brain,scribe` + 人格 `docs/personas/aether-default.yaml` |
| 阶段 2b | 调度器（序号+通道锁）+ DART 接入 + freeze/blend/resume | `core/scheduler.py`、`services/posture/` |
| 合流 | 全链路装配、紧急停止、补偿链（DART→预录、TTS→filler.wav）、5 秒墙钟预算 | `runtime/agent.py`、DEVELOPMENT_GAPS §5 |
| 超出原计划 | 供应商中立动作契约 + CanonicalMotion；motion-provider 独立打包 + sidecar 监督器/回滚；视觉伺服闭环 `approach_target()`；Avatar Tracking Control 身体权限 | 提交 `498a278`→`48e04ce` |
| 决定不做 | 反射模块（<1s 快速反应回路）——按 8/3 决定砍掉，接口字段留白 | 构思对话「更新后的最终清单」 |

### 当前 → 近期（暂停前的活跃线）

1. **NOW** VRChat 可见动作验收：muscle clip 生成器 + `vrchat_visible_test.ps1` 已写好（未提交），差真人进 VRChat 跑一遍。
2. **NEXT** 修 `test_motion_supervisor` 本机失败 → 提交验收成果。
3. **NEXT** Supervisor 接线进默认 Runtime（`SupervisedMotionProvider` 替换硬编码 `DartMotionProvider`）。
4. **LATER** DART 真实 GPU 验收（WSL2/ROCm）→ 恢复 2 小时 soak → 人格漂移压测。

### 远期（构想，未排期）

- SQLite 关键词检索 → 向量 RAG 记忆
- Windows 发布 Bundle（DART checkpoint/CLIP/SMPL-X 资产 + 许可证核验）
- 反射模块（当初砍掉，字段已预留）
- 多人场景目标维持（视觉伺服目前只按单人设计）

## 3. 架构图（当前真实接线）

```text
                         ┌────────────────────────────────────────────┐
   屏幕 1FPS ──►│ 感知采集 CaptureService          录音 100ms PCM ──►│
                         └───────┬──────────────────────┬─────────────┘
                    最新画面+事实│                       │音视频流
                                 ▼                       ▼
                   ┌──────────────────┐   心跳(含观察/建议) ┌───────────────────────┐
                   │ 大脑 BrainService │ ────────────────► │ 小脑 CerebellumService │
                   │ Gemini Flash Lite │ ◄── 记忆合流       │  Gemini ER2 Live       │
                   │ suggested_reply   │                   └─────────┬─────────────┘
                   │ wake_er2          │                             │ function_call
                   └────────┬─────────┘                             ▼
                            │ 块记录/召回                  工具调用解析 Planner
                            ▼                             （只翻译，不执行）
                   ┌──────────────────┐                             │ Plan(seq+channels)
                   │ 书记官 Scribe     │                             ▼
                   │ 三档标记/两级驱逐  │                   ┌──────────────────┐  紧急停止
                   │ SQLite 落盘       │                   │ 调度器 Scheduler  │◄─EmergencyStop
                   └──────────────────┘                   │ 序号+5通道锁      │  (本地关键词旁路)
                                                          └──┬────┬────┬────┬─┘
                        权威事实 WorldFacts ▲                 │    │    │    │
                        ┌─────────────┐    │执行回写          │    │    │    │
                        │  StateStore │◄───┴──────────────────┘    │    │    │
                        │ +Heartbeat  │                            │    │    │
                        └─────────────┘        SPEAK│  PLAY/EXPR│    │    │MOVE│  GENERATE_MOTION│
                                                    ▼           ▼         ▼          ▼
                                            SpeechService  AvatarService Locomotion PostureService
                                            Gemini TTS→    OSC Avatar    OSC /input  │
                                            MIXLINE 虚拟麦  Parameters   (watchdog   ▼
                                                    │      (int/float/   5s归零)  MotionProviderPort
                                                    │       bool only)      │     (默认 DartMotionProvider
                                                    ▼                       ▼      ⇄ HTTP :8900 外部 DART)
                                                VRChat ◄── OSC :9000 ───────┘      │
                                                    ▲                              ▼
                                                    │              CanonicalMotion → TrackerProjector
                                                    │              → MotionPlayback(45Hz keepalive,
                                                    │                freeze/blend/resume)
                                                    │                              │
                                                    └── SteamVR 六设备驱动 ◄─UDP────┘
                                                       (184B 帧 :4242, C++ driver)
```

未接线（虚线能力）：`BundleRegistry ┄► MotionProviderSupervisor ┄► SupervisedMotionProvider`
（sidecar 生命周期/回滚已实现，默认 Runtime 仍直连 DartMotionProvider）。

依赖硬规则：`contracts/ ← core/ ← services/（互不引用）← runtime/（唯一组合根）`。

## 4. TODO（按优先级）

### P0 — 回场第一步（本机、可立即做）

- [ ] 修 `tests/test_motion_supervisor.py::test_supervisor_restarts_runtime_exit_only_within_bound`
      （本机稳定失败：`sidecar exited during startup`，与未提交改动无关，疑 sidecar 子进程
      解释器/PATH 环境问题）。修好后全套测试才是绿的。
- [ ] 真实 VRChat 可见验收（工作区未提交线）：进私人世界载入 Aether avatar →
      `powershell -ExecutionPolicy Bypass -File .\scripts\vrchat_visible_test.ps1`
      （可加 `-Posture -Movement`；先 `-DryRun`）。标准：smile/wave/nod/shake_head/clap/
      point/think/bow/sit/stand 逐条可见、结束回 idle、移动轴归零。
- [ ] 验收通过后提交：Unity muscle-clip 改动 + AetherFX.controller + Assets/XR/ +
      vrchat_test 工具链 + CONTRIBUTING 更新。提交前确认 `.controller` 生成产物与
      `AetherAvatarSetup.cs` 一致（用文档里的 Unity batchmode 命令重生成一次最稳）。
- [ ] 清理遗留：8/2 的 stash `recovery-before-bugfix-plan-20260802`（确认无用后 drop）。

### P1 — 完成设计定稿里欠的验收

- [ ] Supervisor 接线默认 Runtime（构思清单里"调度器来源切换"的收尾）：
      `runtime/agent.py` 支持注入 `SupervisedMotionProvider`，读 `config/motion_bundles`。
- [ ] DART 真实硬件端到端：WSL2/ROCm 起 `/health`，跑通一次 `generate_motion` 全流程
      （目前 CUDA/ROCm/XPU 均未实测，不得写成已验收）。
- [ ] 恢复并完成 2 小时全链路 soak（`scripts/stage3_runtime_soak.py`，此前按用户决定暂停）。
- [ ] 人格一致性压测（构思明确要求、唯一未落地的验收项）：数百轮对话后人设漂移检查 +
      每次驱逐后校验人格核心 prompt 完整在位。

### P2 — 已知短板（不阻塞验收）

- [ ] 视觉伺服真人验收：目前只有 fake detector 闭环测试；真人在 VRChat 画面里的
      识别/多人目标维持未验证。
- [ ] Scribe 检索升级向量 RAG（当前关键词+最近记录）。
- [ ] motion-provider Windows/发布 Bundle 与模型资产许可核验。
- [ ] Unity 工程 ProjectSettings 一堆改动尚未整理说明——弄清哪些是 XR 设置必需、
      哪些是编辑器噪音，避免误提交。

## 5. 记忆恢复卡（暂停前你在做什么）

- 主线：把预录动作从"占位空动画"改为运行时生成 Humanoid muscle 曲线，让动作在 VRChat
  里真实可见；配套写了 OSC 可见性测试工具链和文档；**卡在需要人工进 VRChat 实机验收**。
- 上一轮（已提交）：Avatar 身体权限、人格版本化、书记官活动上下文、视觉伺服、补偿链、
  五秒预算（`8b00641`）+ dev 一键验证（`48e04ce`）。
- 环境事实：默认单 Python 进程；外部边界只有 DART(:8900)、SteamVR 驱动(UDP :4242)、
  Gemini 云 API。`GEMINI_API_KEY` 走环境变量。
