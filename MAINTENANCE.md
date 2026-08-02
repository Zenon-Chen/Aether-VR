# Aether 维护指南

> **Aether** — AI 驱动的虚拟伴侣系统
> 通过语音、动作与自然对话，在 VRChat 中创造有真实感的虚拟角色。

---

## 1. 项目概述

Aether 将大语言模型（Gemini ER2）、人体动作生成（DART）与语音合成（TTS）融合为统一的实时交互系统。

### 三服务 + 编排层架构

| 层级 | 服务 | 职责 |
|------|------|------|
| **规划大脑** | ER2 Live API (Gemini Robotics ER-2 Streaming) | 对话理解、意图推理、工具调度决策 |
| **动作生成** | DART (DartControl, ICLR 2025) | 文本 → SMPL-X 22 关节骨架数据，部署于 WSL2 + ROCm |
| **语音合成** | Gemini TTS (gemini-3.1-flash-tts-preview) | 标点驱动分段合成，情感标签控制语调 |
| **编排层** | AetherAgent (main.py) | 串联所有子系统，管理生命周期、后台循环、打断处理 |

**数据流：**

```
麦克风 (16kHz) → AudioPipeline (VAD) → ER2 Live API → 文本 / 工具调用
                                                         │
                                              ┌──────────┴──────────┐
                                              ▼                     ▼
                                       TTSEngine              DART 动作生成
                                       (Gemini TTS)           (HTTP → WSL2)
                                              │                     │
                                              ▼                     ▼
                                       AudioPipeline         OSCSender → VRChat
                                       (24kHz 播放)          (OSC UDP)
```

---

## 2. 构建过程

### Phase 0: 技术调研

| 调研项 | 结论 |
|--------|------|
| ER2 Streaming API | Google Gemini Robotics ER-2，支持全双工音频流 + 函数调用 + 会话恢复 |
| DART (DartControl) | ICLR 2025 Spotlight，文本 → SMPL-X 骨架，延迟 ~0.3s，显存 4-8GB，兼容 ROCm |
| Gemini TTS | gemini-3.1-flash-tts-preview，支持情感标签和语速控制，输出 24kHz PCM |

### Phase 1: DART 部署

- **环境**：WSL2 Ubuntu 22.04 + ROCm 6.x + AMD Radeon 7900XT
- **模型权重**：`mld_denoiser/mld_fps_clip_repeat_euler/checkpoint_300000.pt` (~400MB)
- **PyTorch3D 绕过**：DART 源码依赖 PyTorch3D（需编译 ROCm 版本），通过补丁绕过
- **FastAPI 封装**：`server.py` 将 `rollout_mld.rollout()` 提取为独立函数，常驻 GPU 内存
- **重要发现**：DART 实际只预测 22 个身体关节（poses 165 维中前 66 维有值，后 99 维全零填充）

### Phase 2: 核心集成

通过 10 个并行任务实现全部子系统：

| # | 子系统 | 关键文件 | 核心实现 |
|---|--------|---------|---------|
| 1 | 项目结构与配置 | `config.py` | Pydantic BaseModel + YAML + 环境变量覆盖 |
| 2 | DART FastAPI 服务 | `dart/server.py` | lifespan 上下文管理、run_in_executor 推理、线程锁 |
| 3 | ER2 Live API 客户端 | `aether/er2/client.py` | AsyncExitStack 管理 asynccontextmanager、流式收发 |
| 4 | 工具分发器 | `aether/tool_dispatcher.py` | 状态机 + 后台任务 + 打断取消 |
| 5 | TTS 分段引擎 | `aether/speech/tts_engine.py` | 标点驱动切片 + 情感标签 + 代际 ID 打断 |
| 6 | 音频 I/O 管道 | `aether/io/audio_pipeline.py` | 麦克风 + Silero VAD + sounddevice 回调播放 |
| 7 | DART 客户端 + OSC | `aether/motion/` | httpx 连接池 + VRChat 骨骼映射 + 逐帧发送 |
| 8 | 上下文管理 | `aether/context/` | FAISS 向量索引 + 评分 + 驱逐 + RAG + 心跳 |
| 9 | 主循环集成 | `aether/main.py` | 并行协程 + CLI 交互 + 打断处理 |
| 10 | 日志系统 | `aether/logging_config.py` | 彩色 CLI + RotatingFileHandler |

### Phase 3: 首次审计与 Bug 修复

来自 reviewer 的代码审查，发现并修复 7 个问题（详见第 3 节）。

### Phase 4: 分发准备

- DART 源码去除 AMD 专属依赖，GPU 无关化（CUDA / ROCm 自适应）
- `setup_guide.md` 编写（NVIDIA 和 AMD 双路线安装指南）
- `download_models.py` 模型下载脚本

### Phase 5: 深度审计与模块化重构

5 份审计报告驱动的全面修复与重构（详见第 4、5 节）：
- 修复 4 个 P0 致命问题 + 6 个 P1 高危问题
- 引入 Protocol 接口层
- 子包重组（er2/ motion/ speech/ io/ context/）
- 系统提示词提取到 `prompts.py`

---

## 3. 首次集中审计发现的 Bug

第一份审计（commit `c447a2c`）发现的 7 个问题：

| # | 问题 | 严重度 | 位置 | 修复方案 |
|---|------|--------|------|---------|
| 1 | OSC 动作播放逻辑批量发送而非逐帧 | **高** | `osc_sender.py` `play_motion` | 改为逐帧发送 + 可中断 sleep + 启用 `interpolate_frames()` |
| 2 | 打断只停声音不停动作 | **高** | `main.py` `_on_speech_start` | 添加 `osc.stop()` + `send_idle_pose()` |
| 3 | Token 计数严重低估（中文 0.5 vs 实际 ~1.3） | **中** | `block_store.py` `estimate_token_count` | 换用 tiktoken `cl100k_base` 精确计算 |
| 4 | API Key 空串不报错 | **中** | `config.py` `load_config` | 启动时校验非空，空则 `ValueError` |
| 5 | DART 不可用只 warning 后续仍调用 | **低** | `main.py` `start` | `_dart_available` 标志位，不可用时跳过 |
| 6 | FAISS 向量删不掉（索引膨胀） | **中** | `block_store.py` `remove_block` | `_pending_removals` 累积 50 次触发 `_rebuild_index()` |
| 7 | 工具参数零校验 | **中** | `tool_dispatcher.py` | emotion/speech_rate 枚举校验 + duration clamp |

### Bug #1：OSC 批量发送而非逐帧

- **现象**：整个动作序列一次性发送全部 OSC 消息，VRChat 角色瞬移而非动画
- **根因**：`play_motion()` 对所有帧调用 `_send_batch()`，没有帧间延时
- **修复**：引入 `_motion_loop()` 后台任务，预计算所有帧消息后逐帧发送，帧间用 `asyncio.wait_for(_stop_event.wait(), timeout=1/framerate)` 实现可中断 sleep
- **涉及文件**：`aether/motion/osc_sender.py`

### Bug #2：打断只停声音不停动作

- **现象**：用户说话时 TTS 音频停了，但角色仍在执行上一个动作
- **根因**：`_on_speech_start()` 只调用了 `audio.stop_playback()` 和 `tts.clear_queue()`，没有停止 OSC
- **修复**：添加 `self.osc.stop()` + `self.osc.send_idle_pose()`，让角色立即回到站立姿态
- **涉及文件**：`aether/main.py`

### Bug #3：Token 计数严重低估

- **现象**：上下文窗口实际 token 远超估算值，导致驱逐不及时
- **根因**：旧算法对中文按 0.5 token/字估算，实际中文在 cl100k_base 中约 1.3 token/字
- **修复**：引入 `tiktoken.get_encoding("cl100k_base")` 精确计算，tiktoken 失败时回退到改进的中英文分别估算
- **涉及文件**：`aether/context/block_store.py`

### Bug #4：API Key 空串不报错

- **现象**：未设置 `GEMINI_API_KEY` 环境变量时，系统静默启动，首次 API 调用才报 401
- **根因**：`load_config()` 未校验 api_key 是否为空
- **修复**：在 `load_config()` 末尾添加 `if not cfg.er2.api_key: raise ValueError(...)`
- **涉及文件**：`aether/config.py`

### Bug #5：DART 不可用仍调用

- **现象**：DART 服务未启动时，每次 `execute_motion` 都超时等待
- **根因**：健康检查失败只打印 warning，后续 motion_handler 仍调用 `dart.generate()`
- **修复**：`AetherAgent.start()` 中设置 `self._dart_available` 标志位，`motion_handler` 先检查标志位
- **涉及文件**：`aether/main.py`

### Bug #6：FAISS 向量删不掉

- **现象**：长时间运行后 FAISS 索引体积持续增长，搜索性能下降
- **根因**：`IndexFlatIP` 不支持单条删除，`remove_block()` 只从字典中移除但 FAISS 中残留
- **修复**：引入 `_pending_removals` 计数器，累积 50 次删除后触发 `_rebuild_index()` 重建整个索引
- **涉及文件**：`aether/context/block_store.py`

### Bug #7：工具参数零校验

- **现象**：ER2 下发非法 emotion（如 "joyful"）或超长 duration（如 999 秒），系统原样执行
- **根因**：`_handle_tts()` 和 `_handle_motion()` 未对参数做任何校验
- **修复**：添加 `VALID_EMOTIONS` / `VALID_SPEECH_RATES` 枚举校验 + `MOTION_DURATION_MIN/MAX` clamp + 文本长度截断
- **涉及文件**：`aether/tool_dispatcher.py`

---

## 4. 深度审计发现的 P0/P1 Bug（第二波审计）

5 份审计报告发现的致命和高危问题（commit `b77e5f0`）：

### P0 致命问题（4 个）

#### P0-1：`await` asynccontextmanager

- **位置**：`er2/client.py:221`（修复前）
- **根因**：SDK 的 `client.aio.live.connect()` 返回 `asynccontextmanager`，不能直接 `await` 获取 session
- **修复**：使用 `contextlib.AsyncExitStack` + `enter_async_context()` 管理生命周期，在 `close()` 时通过 `aclose()` 退出

```python
# 修复后
self._exit_stack = contextlib.AsyncExitStack()
self.session = await self._exit_stack.enter_async_context(
    self.client.aio.live.connect(model=..., config=...)
)
```

#### P0-2：tool_call 读错位置

- **位置**：`er2/client.py:373`（修复前）
- **根因**：原代码读 `chunk.server_content.tool_call`，但 SDK 中 `tool_call` 在 `LiveServerMessage` 顶层
- **修复**：改为 `chunk.tool_call`，同理 `tool_call_cancellation` 也在顶层

#### P0-3：receive_loop 首轮后 break

- **位置**：`er2/client.py:396`（修复前）
- **根因**：SDK 的 `session.receive()` 是一个 async generator，每 turn 结束即终止迭代。原代码在 `async for` 结束后 `break`，导致只处理一轮
- **修复**：`async for` 循环结束后 `continue`，外层 `while self._running` 会自动进入下一轮 `receive()`

```python
# 修复后
async for chunk in self.session.receive():
    # ... 处理 chunk ...

# receive() 一个 turn 结束后继续下一轮（不 break）
continue
```

#### P0-4：会话恢复是假的

- **位置**：`er2/client.py:242-268`（修复前）
- **根因**：`_resumption_token` 从未被赋值，`reconnect()` 始终建立全新会话
- **修复**：在 `receive_loop` 中监听 `chunk.session_resumption_update`，当收到 `new_handle` 且 `resumable=True` 时更新 `_resumption_token`；在 `_build_live_config()` 中注入 `SessionResumptionConfig(handle=self._resumption_token)`；监听 `chunk.go_away` 触发主动重连

### P1 高危问题（6 个）

#### P1-5：中文文本用英文标点切分

- **位置**：`speech/tts_engine.py`（修复前）
- **根因**：`_split_text()` 默认使用英文标点 `([.!?])` / `([,;])` 分割，但系统提示词要求模型输出中文
- **修复**：添加 `SPLIT_MODES` 字典支持 `chinese` / `english` 两种模式，`synthesize()` 默认传 `split_on="chinese"`

#### P1-6：工具调用串行阻塞接收循环

- **位置**：`tool_dispatcher.py`（修复前）
- **根因**：`handle_tool_call()` 同步 `await` 每个 handler，阻塞了 `receive_loop`
- **修复**：每个 function call 启动 `asyncio.create_task()` 后台执行，`handle_tool_call()` 立即返回空列表；后台任务完成后通过 `_response_callback` 异步回传 `FunctionResponse`

```python
# 修复后：非阻塞分发
async def handle_tool_call(self, tool_call) -> list:
    for fc in tool_call.function_calls:
        task = asyncio.create_task(
            self._run_and_respond(fc),
            name=f"tool:{fc.name}:{fc.id}",
        )
        self.state.active_tasks[fc.id] = task
    return []  # 立即返回，不阻塞 receive_loop
```

#### P1-7：ToolCallCancellation 未接线

- **位置**：`er2/client.py` + `tool_dispatcher.py`
- **根因**：ER2 下发的 `tool_call_cancellation` 消息无人处理
- **修复**：`ER2Client` 添加 `set_tool_call_cancelled_handler()`；`receive_loop` 监听 `chunk.tool_call_cancellation`；`ToolDispatcher.cancel_pending()` 通过 `active_tasks` 字典精确匹配并 `task.cancel()`

#### P1-8：打断后残余语音照播

- **位置**：`speech/tts_engine.py`（修复前）
- **根因**：打断时 `clear_queue()` 清空了队列，但飞行中的 `_call_tts_api()` 完成后仍将结果入队
- **修复**：引入 `_generation_id` 代际计数器，`clear_queue()` 时递增；`_call_tts_api()` 返回前检查代际 ID，不匹配则抛出 `_StaleGeneration` 丢弃结果

#### P1-9：OSC `_playing` 竞态

- **位置**：`osc_sender.py`（修复前）
- **根因**：`_playing` 布尔标志在 asyncio 协程和同步 `stop()` 之间存在竞态
- **修复**：用 `asyncio.Event` (`_stop_event`) 替代布尔标志，`_playing` 变为属性 `not _stop_event.is_set()`；帧间 sleep 用 `wait_for(_stop_event.wait(), timeout=...)` 实现可中断

#### P1-10：`_print_status` 访问私有属性

- **位置**：`main.py`（修复前）
- **根因**：`_print_status()` 直接访问 `dart._client` 私有属性
- **修复**：改用 `self._dart_available` 标志位判断 DART 状态

---

## 5. 模块化重构过程

### 3 服务 + 编排层架构

重构后系统分为 3 个独立服务子包和 1 个编排层：

```
aether/
├── er2/           ← ER2 服务（对话 AI）
├── motion/        ← Motion 服务（动作生成 + OSC 输出）
├── speech/        ← Speech 服务（TTS 合成）
├── io/            ← I/O 适配器（音频输入输出）
├── context/       ← 上下文管理（RAG + 评分 + 驱逐）
├── main.py        ← 编排层（串联所有服务）
└── protocols.py   ← Protocol 接口层（解耦）
```

### Protocol 接口层

`protocols.py` 定义了 6 个 Protocol 接口，实现服务间解耦：

| Protocol | 方法 | 用途 |
|----------|------|------|
| `ER2Port` | connect/close/send_audio/send_text/receive_loop | ER2 通信抽象 |
| `MotionPort` | connect/close/generate/health_check | DART 客户端抽象 |
| `OSCPort` | connect/play_motion/stop/send_idle_pose | OSC 输出抽象 |
| `AudioPort` | set_callbacks/start/stop/play_audio/stop_playback | 音频管道抽象 |
| `TTSPort` | synthesize/clear_queue/get_next_audio | TTS 引擎抽象 |
| `DispatcherPort` | handle_tool_call/cancel_pending | 分发器抽象 |

### 子包重组

- `er2/`：`client.py`（WebSocket 客户端） + `prompts.py`（系统提示词 + 工具声明）
- `motion/`：`dart_client.py`（HTTP 客户端） + `osc_sender.py`（骨骼映射发送）
- `speech/`：`base.py`（抽象基类） + `tts_engine.py`（Gemini TTS 实现）
- `io/`：`audio_pipeline.py`（麦克风 + VAD + 播放）
- `context/`：`block_store.py` + `importance_scorer.py` + `evictor.py` + `rag_retriever.py` + `heartbeat.py`

### 配置注入统一

每个子模块接受独立的配置子对象，不依赖完整的 `AetherConfig`：

- `ER2Client` ← `ER2Config`
- `TTSEngine` ← `TTSConfig`
- `DARTClient` ← `DARTConfig`
- `OSCSender` ← `OSCConfig`
- `AudioPipeline` ← `AudioConfig`
- `ContextBlockStore` / `ImportanceScorer` / `ContextEvictor` / `RAGRetriever` ← `ContextConfig`

### 遗留代码清理

- 删除 `task_tracker.py`（功能已被 `DispatcherState` 替代）
- 删除 `speech/` 空壳模块
- 系统提示词和工具声明从 `client.py` 提取到 `prompts.py`

---

## 6. 当前目录结构

```
Aether project/
├── aether/                        # 主程序包
│   ├── __init__.py
│   ├── __main__.py                # python -m aether 入口
│   ├── main.py                    # 主控制器 AetherAgent
│   ├── config.py                  # Pydantic 配置管理
│   ├── protocols.py               # Protocol 接口定义（6 个）
│   ├── tool_dispatcher.py         # 函数调用分发器
│   ├── logging_config.py          # 彩色 CLI + 文件日志
│   ├── er2/                       # Gemini ER2 Live API
│   │   ├── __init__.py
│   │   ├── client.py              # WebSocket 客户端（会话管理、收发）
│   │   └── prompts.py             # 系统提示词 + 工具声明
│   ├── speech/                    # TTS 语音合成
│   │   ├── __init__.py
│   │   ├── base.py                # BaseTTSEngine 抽象基类
│   │   ├── tts_engine.py          # Gemini TTS 分段合成引擎
│   │   └── chatterbox.py          # Chatterbox TTS（备选引擎）
│   ├── motion/                    # 动作生成与输出
│   │   ├── __init__.py
│   │   ├── dart_client.py         # DART HTTP 客户端（httpx 连接池）
│   │   └── osc_sender.py          # VRChat OSC 骨骼映射发送
│   ├── io/                        # 音频 I/O
│   │   ├── __init__.py
│   │   └── audio_pipeline.py      # 麦克风 + Silero VAD + 播放
│   └── context/                   # 上下文管理
│       ├── __init__.py
│       ├── block_store.py         # 上下文块存储 + FAISS + tiktoken
│       ├── importance_scorer.py   # 规则引擎 + LLM 混合评分
│       ├── evictor.py             # 分级驱逐策略（L1-L4）
│       ├── rag_retriever.py       # FAISS top-K + 贪心选择
│       └── heartbeat.py           # 世界状态心跳生成
├── dart/                          # DART 动作生成服务（WSL2 侧）
│   ├── server.py                  # FastAPI 推理服务
│   ├── download_models.py         # 模型下载脚本
│   ├── start_server.sh            # WSL2 启动脚本
│   ├── requirements_dart.txt      # DART 专用依赖
│   ├── setup_guide.md             # 安装指南
│   ├── mld/                       # MLD 扩散模型
│   ├── mld_denoiser/              # 去噪器 checkpoint
│   ├── mvae/                      # Motion VAE checkpoint
│   ├── model/                     # 模型定义
│   ├── data_loaders/              # 数据加载器
│   ├── utils/                     # 工具函数
│   └── ...                        # 训练 / 数据 / 评估脚本
├── tests/                         # 单元测试
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_dart_format.py
│   ├── test_er2_connection.py
│   ├── test_protocols.py
│   ├── test_tool_dispatcher.py
│   └── test_tts_split.py
├── logs/                          # 运行日志
│   └── aether.log
├── config.yaml                    # 默认配置文件
├── requirements.txt               # Python 依赖
└── README.md                      # 项目说明
```

---

## 7. 已知遗留问题与后续计划

### 模型与数据

| 问题 | 说明 | 状态 |
|------|------|------|
| DART 大模型权重需下载 | `checkpoint_300000.pt` (~400MB) 和 `checkpoint_200000.pt` (~150MB) 需从项目维护者获取 | 待解决 |
| SMPL-X 模型需手动下载 | 需从 smpl-x.is.tue.mpg.de 注册下载，但核心推理不依赖（仅训练时使用） | 低优先级 |
| DART 实际只输出 22 关节 | 手部面部为零填充，如需手指/面部数据需后处理方案（SMPLify-X 或 HaMeR） | 已知限制 |

### 通信协议

| 问题 | 说明 | 状态 |
|------|------|------|
| OSC 协议与 VRChat 匹配 | VRChat 的 OSC 骨骼参数路径可能因 Avatar 而异，需根据实际模型调整 `avatar_prefix` 和参数映射 | 需实测 |
| 视觉感知未实现 | `send_video_frame()` 已定义但未接线，需要摄像头采集 + JPEG 编码 | 待实现 |

### 上下文管理

| 问题 | 说明 | 状态 |
|------|------|------|
| RAG embedding worker 待完整接线 | 嵌入计算目前按需调用，未实现后台 worker 批量嵌入 `_pending_embeddings` | 待优化 |
| 上下文压缩策略 | ER2 侧 `context_window_compression` 配置为 120000 token 触发 / 80000 token 滑窗，需实测调优 | 待调优 |

### 平台适配

| 问题 | 说明 | 状态 |
|------|------|------|
| 多 OS 适配 | 当前以 Windows 为主，Linux 作为逃生通道（DART 直连无需 WSL2） | 部分支持 |
| AMD Windows 推理路线 | ZLUDA vs TheRock（AMD 官方 ROCm for Windows），均处于实验阶段 | 观望中 |
| NVIDIA 兼容 | DART server.py 已 GPU 无关化（`torch.cuda.is_available()` 自动检测），NVIDIA 卡可直接运行 | 已支持 |

---

## 8. 维护建议

### 8.1 如何添加新的工具/函数调用

1. **在 `prompts.py` 中添加函数声明**：

```python
# aether/er2/prompts.py → TOOL_DECLARATIONS
types.FunctionDeclaration(
    name="your_new_tool",
    description="工具描述",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={...},
        required=[...],
    ),
)
```

2. **在 `tool_dispatcher.py` 中添加路由**：

```python
# _dispatch_single() 中添加 elif 分支
elif name == "your_new_tool":
    return await self._handle_your_tool(args)
```

3. **添加处理器方法**：

```python
async def _handle_your_tool(self, args: dict) -> dict:
    # 参数校验
    # 调用实际逻辑
    return {"status": "ok", ...}
```

4. **在 `main.py` 中注入外部处理器**（如需要）：

```python
# _wire_handlers() 中添加
self.dispatcher.set_your_handler(your_handler)
```

5. **更新 `protocols.py`**（如接口变更）。

### 8.2 如何替换 TTS 引擎

1. **继承 `BaseTTSEngine`**（`aether/speech/base.py`）：

```python
class MyTTSEngine(BaseTTSEngine):
    async def synthesize(self, text, emotion="neutral", speech_rate="normal") -> str:
        ...

    def clear_queue(self):
        ...

    async def get_next_audio(self) -> Optional[bytes]:
        ...
```

2. **在 `main.py` 中替换初始化**：

```python
# 原：self.tts = TTSEngine(config.tts)
self.tts = MyTTSEngine(config.tts)
```

3. 由于 `TTSPort` Protocol 定义了统一接口，只要实现 `synthesize/clear_queue/get_next_audio` 即可无缝替换。

### 8.3 测试运行

```powershell
# 设置 API Key
$env:GEMINI_API_KEY = "your-api-key"

# 安装依赖
cd "c:\open code project\Aether project"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 运行单元测试
pytest tests/ -v

> **最近验证结果**（2026-08-02）：`pytest tests/ -q` → 154 passed in 4.66s。改动后请保持该套件全绿，并在提交说明中附上测试结果。

# 启动 DART 服务（WSL2，可选）
wsl -- bash -c "cd /root/DART && source /root/pytorch_rocm/bin/activate && bash start_server.sh"

# 启动主程序
python -m aether
```

**CLI 调试命令：**

| 命令 | 用途 |
|------|------|
| `/status` | 查看所有子系统状态 |
| `/tokens` | 查看当前上下文 token 用量 |
| `/memory` | 查看块存储摘要 |
| `/idle` | 发送空闲姿态（测试 OSC） |
| `/motion walk forward*5` | 测试动作生成（需 DART 在线） |
| `/tts 你好世界` | 测试语音合成 |

### 8.4 常见问题排查

| 症状 | 排查方向 |
|------|---------|
| `ValueError: ER2 API Key is required` | 检查 `GEMINI_API_KEY` 环境变量是否设置 |
| ER2 连接后立即断开 | 检查 API Key 有效性；查看 `logs/aether.log` 中的错误详情 |
| DART 服务不可用 | 确认 WSL2 中 `uvicorn server:app --host 0.0.0.0 --port 8900` 已启动；`curl http://localhost:8900/health` 测试 |
| DART 生成超时 | 检查 GPU 显存（`/health` 端点返回 GPU 信息）；确认 checkpoint 文件完整 |
| OSC 无响应 | 确认 VRChat 已启动且 OSC 已启用；检查 `config.yaml` 中 `osc.port` 是否为 9000 |
| 音频无声 | 检查 `sounddevice` 是否安装；查看 `no_audio` 标志是否为 `True` |
| VAD 不灵敏 | 调整 `config.yaml` 中 `audio.vad_threshold`（降低到 0.3 提高灵敏度） |
| TTS 声音断续 | 检查网络连接（Gemini TTS API）；查看 `logs/aether.log` 中的 API 错误 |
| 上下文溢出 | 检查 `context.max_active_tokens` 配置；确认驱逐循环正常运行 |
| 打断后仍有残余语音 | 检查 `_generation_id` 机制是否正常递增（代际 ID 不匹配会丢弃飞行中结果） |

### 8.5 日志与调试

- **日志文件**：`logs/aether.log`（RotatingFileHandler，10MB/文件，保留 7 份）
- **CLI 输出**：按模块着色（ER2=青色，DART/OSC=紫色，TTS/音频=绿色，Context=红色）
- **颜色映射**：`aether/logging_config.py` 的 `MODULE_COLORS`（按 logger 前缀匹配），新增模块时同步更新
- **日志级别**：通过 `config.yaml` 的 `log.level` 配置（默认 INFO）
- **DART 服务日志**：WSL2 终端 stdout（FastAPI 默认输出）

### 8.6 依赖管理

| 依赖 | 用途 | 备注 |
|------|------|------|
| `google-genai` | ER2 Live API + TTS API | 核心依赖 |
| `httpx` | DART HTTP 客户端 | 异步连接池 |
| `python-osc` | VRChat OSC 通信 | UDP 发送 |
| `pydantic` + `pyyaml` | 配置管理 | 类型安全 |
| `numpy` | 音频/骨骼数据处理 | 通用 |
| `sounddevice` | 麦克风 + 扬声器 | 可选（无则 no-audio 模式） |
| `faiss-cpu` | 向量相似度搜索 | 可选（无则 RAG 禁用） |
| `tiktoken` | Token 精确计算 | 回退到简单估算 |
| `torch` | Silero VAD | 可选（无则回退能量检测） |
