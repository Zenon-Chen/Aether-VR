# Aether VR 开发者架构

> **文档定位**：面向开发者的当前代码架构说明，以仓库实际实现、自动化测试与默认配置为准。
>
> **当前状态**：Aether 已完成严格四层单向依赖重构（`contracts -> core -> services -> runtime`）、`runtime/` 四模块职责拆分、双预算调度与 DART 超时预录回退闭环、轻量能量 VAD 与 0ms 急停词旁路拦截，以及 Unity VRChat Avatars 3.0 `Action` / `FX` 双层控制器拆分与 15 视素精确绑定。

## 1. 一句话模型

Aether 把 VRChat 角色分成四层：**大脑（Brain）与记忆（Scribe）负责观察、人格与长期记忆；小脑（Cerebellum）负责 ER2 Live 实时音视频与动作编排；核心控制层（Core）负责权威事实、双预算调度、五通道互斥与紧急停止；输出服务负责把计划送到语音、VRChat OSC 与 SteamVR 六设备驱动**。

## 2. 运行期模块化架构（`aether/runtime/`）

原单一上帝类已拆解为**一个组合根 + 四个高内聚运行期模块**：

| 模块 | 核心类 / 函数 | 职责边界 |
|---|---|---|
| `runtime/agent.py` | `AetherRuntime` | **组合根**：按安全顺序实例化与启停核心及各服务，管理全部后台任务集（`_tasks` / `_call_tasks` / `_plan_tasks` / `_bg_tasks`）与健康检查归一化。 |
| `runtime/motion_wiring.py` | `create_motion_provider`, `compute_plan_io_allowance` | **动作生成器装配与预算宽限**：根据 `config.motion.mode`（`external` 或 `supervised`）注入 `DartMotionProvider` 或 `SupervisedMotionProvider(MotionProviderSupervisor)`，并计算 `SPEAK` / `GENERATE_MOTION` 的云端/GPU 合成宽限。 |
| `runtime/executors.py` | `BodyExecutorSuite` | **身体执行器套件**：调度器与各输出服务的唯一接缝。管理预录全身动作的 `posture.freeze()/resume()`、动态动作生成的 `generation_timeout_s` 有界超时与预录动作降级回退（`bow`/`wave`），以及持有 `Channel.MOVEMENT` 锁的 `APPROACH` 视觉伺服。 |
| `runtime/perception_gate.py` | `PerceptionGate`, `compute_pcm16_rms` | **感知网关**：驱动音视频泵；用零重依赖的 16-bit PCM 归一化 RMS 幅度维护 `user_speaking`（带 `vad_hangover_s` 尾音保持）；在 ER2 增量与最终输入转写上词边界匹配急停词，命中即 0ms 本地触发 `EmergencyStop`。 |
| `runtime/coordinator.py` | `CognitiveCoordinator` | **认知与工具协调器**：大脑启动后 `initial_delay_s` 立即首轮热观察，后续在 `interval_s` 周期与用户新发言事件（受 `min_event_interval_s` 冷却防抖保护）之间自适应唤醒；驱动 0.5s 心跳循环；在预算校验前锁定 `suggested_reply` 快照并保证 100% 回传 `FunctionResponse`。 |

## 3. 核心控制与双预算机制

### 3.1 编排时长预算 vs 合成 I/O 宽限（`Scheduler`）

- **编排动作预算（`max_plan_seconds = 5.0s`）**：约束单次计划的预期表演时长（由 `contracts/plan.py` 唯一的 `estimated_step_seconds` / `estimated_plan_seconds` 计算），强制长行为拆分为滚动短计划。
- **合成 I/O 墙钟宽限（`io_allowance_fn`）**：云端多段 TTS（`speech.synthesis_allowance_s = 10.0s`）与 GPU 动态动作生成（`motion.synthesis_allowance_s = 20.0s`）享有独立的网络/推理墙钟宽限，使超过 5 秒的远程合成不会被调度器误判超时强杀，同时让 `BodyExecutorSuite.exec_motion` 的内部 `generation_timeout_s`（`15.0s`）能够先于计划墙钟触发预录动作优雅回退。
- **长时伺服豁免（`budget_exempt_actions = {ActionKind.APPROACH}`）**：高频视觉伺服走独立的 `timeout` 上限，但完整纳入调度器的 `Channel.MOVEMENT` 互斥锁与抢占/急停生命周期。

### 3.2 心跳单次唤醒与语音避让（`HeartbeatAssembler`）

- 每个 `BrainOutput` 携带进程内单调递增的 `output_id`。当大脑置位 `wake_er2=True` 时，仅对该 `output_id` 穿透一次 `TOO_SOON` / `NO_CHANGE` 闸门；已投递后自动回归常规节流，避免在下一个 60 秒观察周期到来前以 2Hz 重复刷屏。
- 当 `PerceptionGate` 检测到用户正在说话（`Gate.USER_SPEAKING`）或 ER2 正在生成（`Gate.ER2_GENERATING`）时，严格推迟心跳发送。

### 3.3 语音可取消止播与最新优先记忆

- **`SpeechService`**：`speak()` 被取消（计划抢占或紧急停止）时立即调用 `halt()`（`sd.stop()`）打断底层 PortAudio 播放线程，等待工作线程安全退出后再释放播放锁，杜绝双声道重叠说话。
- **`ScribeService`**：`render_active_context` 从最新对话与未归档 `_pending` 逆序截取 `max_tokens` 后再正序输出，确保最新上下文永不被历史淹没；模型批量重要性评估在锁外运行，SQLite 操作通过 `asyncio.to_thread` + 异步 DB 锁串行化，不阻塞 45Hz 实时姿态循环。

## 4. Unity VRChat Avatars 3.0 双层架构（`avatar-project/`）

`AetherAvatarSetup.cs` 严格遵循 VRChat Avatars 3.0 规范生成两套独立控制器并挂载到 `AetherAvatar.unity`：

1. **`AetherAction.controller`（挂载到 `AnimLayerType.Action`）**：
   - 使用经 `HumanTrait.MuscleName` 与 `AnimationUtility.GetAnimatableBindings` 强校验的合法 Humanoid 肌肉属性，通过 `AnimationUtility.SetEditorCurve` 写入非空 `genericBindings`；
   - 包含全部 9 个动作（`wave`, `nod`, `shake_head`, `bow`, `clap`, `point`, `think`, `sit`, `stand`），其中 `sit` 在末帧保持坐姿直至 `ActionID != 8`；
   - 配合 `VRCPlayableLayerControl`（`idle` 权重 `0`、激活态权重 `1`）与 `VRCAnimatorTrackingControl`（`idle` 交还 SteamVR 追踪器、激活态切 `Animation`）。
2. **`AetherFX.controller`（挂载到 `AnimLayerType.FX`）**：
   - 仅绑定 `mesh_char_001` 上的非视素面部形态键（`vrc.lowerlid_left/right`, `vrc.blink_left/right`），零骨骼肌肉曲线（绝不锁死 `Spine Front-Back`），且绝不触碰 `vrc.v_*` 口型视素。
3. **15 视素精确一对一映射**：
   - `ConfigureVisemes` 精确绑定 `vrc.v_sil` 至 `vrc.v_ou` 共 15 个标准视素，彻底消除裸子串匹配将 `E` / `ih` 误绑到 `vrc.blink_left` 的缺陷。

## 5. 依赖规则与验证命令

```text
contracts/  ← 零依赖公共数据契约
core/       ← 仅依赖 contracts
services/   ← 仅依赖 contracts + infra + services/ports.py（严禁跨服务 import）
runtime/    ← 唯一组合根
```

常用验证命令：

```powershell
# 全量本地验证（单测 + AST 分层守卫 + Unity 资产静态契约 + 编译检查 + 45Hz UDP 冒烟 + Motion Provider 测试）
python scripts/dev_verify.py --smoke

# VRChat 可见动作配置与序列干跑校验
python -m aether.runtime.vrchat_test --dry-run
```
