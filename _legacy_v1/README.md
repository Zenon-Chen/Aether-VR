# Aether

**AI 驱动的虚拟伴侣系统** — 通过语音、动作与自然对话，在 VRChat 中创造有真实感的虚拟角色。

Aether 将大语言模型（Gemini ER2）、人体动作生成（DART）与语音合成（TTS）融合为统一的实时交互系统，让虚拟角色具备对话、情感表达与肢体动作能力。

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Aether VR Agent                             │
│                                                                     │
│   ┌──────────┐      ┌────────────────┐      ┌──────────────────┐   │
│   │ 麦克风   │─────▶│ AudioPipeline  │─────▶│ ER2 Live API     │   │
│   │ (16kHz)  │      │ VAD / 播放     │      │ (对话 AI + 工具) │   │
│   └──────────┘      └────────────────┘      └────────┬─────────┘   │
│                              ▲                       │              │
│                              │                       ▼              │
│                     ┌────────┴───────┐    ┌─────────────────────┐  │
│                     │ TTSEngine      │◀───│ ToolDispatcher      │  │
│                     │ (Gemini TTS)   │    │ execute_tts         │  │
│                     └────────────────┘    │ execute_motion      │  │
│                                           │ recall_memory       │  │
│   ┌──────────┐      ┌────────────────┐    │ report_status       │  │
│   │ VRChat   │◀─────│ OSCSender      │◀───└──────────┬──────────┘  │
│   │ (OSC)    │      │ 骨骼映射发送   │               │             │
│   └──────────┘      └────────────────┘               ▼             │
│                                            ┌──────────────────┐    │
│   ┌──────────┐                             │ DART (WSL2)      │    │
│   │ 上下文   │ ◀─── RAG / 评分 / 驱逐 ───▶│ 动作生成服务     │    │
│   │ 管理模块 │                             │ SMPL-X 骨架数据  │    │
│   └──────────┘                             └──────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

**数据流：**

```
语音输入 → ER2 Live API → 文本 / 工具调用
                             │
                  ┌──────────┴──────────┐
                  ▼                     ▼
           TTS 语音合成           DART 动作生成
                  │                     │
                  ▼                     ▼
           AudioPipeline         OSCSender → VRChat
```

---

## 系统要求

| 组件 | 要求 |
|------|------|
| 操作系统 | Windows 11 24H2+ |
| GPU | AMD Radeon（ROCm 支持）|
| WSL2 | Ubuntu 22.04 + ROCm 6.x |
| Python | 3.11+（Windows 端）|
| 麦克风 | 任意 USB / 内置麦克风 |
| VRChat | 已启用 OSC 输入 |
| API Key | Google Gemini API Key |

---

## 快速开始

### 1. 环境变量

```bash
# Windows PowerShell
$env:GEMINI_API_KEY = "your-api-key-here"
```

### 2. Windows 端安装

```bash
cd "Aether project"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. WSL2 DART 服务（可选，用于动作生成）

```bash
# WSL2 Ubuntu 内执行
cd /root/DART
source /root/pytorch_rocm/bin/activate
pip install -r requirements_dart.txt
bash start_server.sh
# 服务启动在 http://localhost:8900
```

### 4. 配置文件

默认配置位于 `config.yaml`，主要配置项：

```yaml
er2:
  model: "gemini-robotics-er-2-streaming-preview"
tts:
  voice_name: "Kore"
  max_segment_chars: 30
dart:
  host: "localhost"
  port: 8900
osc:
  host: "127.0.0.1"
  port: 9000          # VRChat OSC 默认端口
```

### 5. 启动

```bash
# Windows 端（确保 WSL2 DART 服务已在后台运行）
python -m aether
```

**CLI 命令：**
- `/status` — 查看系统状态
- `/tokens` — Token 用量
- `/memory` — 上下文存储摘要
- `/idle` — 发送空闲姿态
- `/motion <text>` — 测试动作生成
- `/tts <text>` — 测试语音合成
- `/quit` — 退出

---

## 目录结构

```
Aether project/
├── aether/                    # 主程序包
│   ├── config.py              # 配置管理（Pydantic）
│   ├── main.py                # 主控制器 AetherAgent
│   ├── protocols.py           # 接口协议定义
│   ├── tool_dispatcher.py     # 函数调用分发器
│   ├── logging_config.py      # 日志配置
│   ├── er2/                   # Gemini ER2 Live API 客户端
│   │   ├── client.py          # WebSocket 客户端
│   │   └── prompts.py         # 系统提示词 + 工具声明
│   ├── speech/                # TTS 语音合成
│   │   ├── base.py            # 引擎抽象基类
│   │   └── tts_engine.py      # Gemini TTS 分段合成
│   ├── motion/                # 动作生成与输出
│   │   ├── dart_client.py     # DART HTTP 客户端
│   │   └── osc_sender.py      # VRChat OSC 骨骼映射
│   ├── io/                    # 音频 I/O
│   │   └── audio_pipeline.py  # 麦克风采集 + VAD + 播放
│   └── context/               # 上下文管理（RAG + 评分驱逐）
│       ├── block_store.py     # 上下文块存储 + FAISS
│       ├── importance_scorer.py
│       ├── evictor.py
│       ├── rag_retriever.py
│       └── heartbeat.py       # 心跳状态注入
├── dart/                      # DART 动作生成服务（WSL2 侧）
│   ├── server.py              # FastAPI 推理服务
│   ├── mld/                   # MLD 扩散模型
│   └── ...                    # 训练 / 数据 / 评估脚本
├── tests/                     # 单元测试
├── logs/                      # 运行日志
├── config.yaml                # 默认配置文件
└── requirements.txt           # Python 依赖
```

---

## 配置说明

### ER2 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `er2.model` | `gemini-robotics-er-2-streaming-preview` | ER2 模型名称 |
| `er2.api_key` | `$GEMINI_API_KEY` | API Key（环境变量优先）|
| `er2.context_window_tokens` | `128000` | 上下文窗口 token 数 |

### TTS 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `tts.voice_name` | `Kore` | 预置语音角色 |
| `tts.max_segment_chars` | `30` | 每段最大字符数 |

### DART 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `dart.host` | `localhost` | DART 服务地址 |
| `dart.port` | `8900` | DART 服务端口 |
| `dart.timeout` | `30` | 推理超时（秒）|

### OSC 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `osc.host` | `127.0.0.1` | VRChat OSC 地址 |
| `osc.port` | `9000` | VRChat OSC 端口 |
| `osc.avatar_prefix` | `/avatar/parameters` | OSC 路径前缀 |

---

## 许可证

本项目仅供学习与研究使用。DART 模型受其原始许可证约束，Gemini API 使用需遵守 Google 服务条款。
