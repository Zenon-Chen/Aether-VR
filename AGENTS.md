# AGENTS.md — Aether 项目协作指南

面向在此仓库中工作的编码 Agent 的入口说明。开始任务前先读本节，按模块地图定位改动边界。

## 项目目标

AI 驱动的虚拟伴侣系统：通过语音、动作与自然对话，在 VRChat 中创造有真实感的虚拟角色。

技术栈：Gemini ER2（对话 + 工具调度）、DART（动作生成，WSL2 侧服务）、Gemini TTS（语音合成）、OSC（VRChat 输出）。

## 模块地图

```
aether/
├── er2/          ER2 Live API 客户端（client.py）+ 系统提示词（prompts.py）
├── speech/       TTS：base.py 抽象基类、tts_engine.py Gemini 实现、chatterbox.py 备选
├── motion/       DART HTTP 客户端（dart_client.py）+ VRChat OSC 发送（osc_sender.py）
├── io/           音频输入输出（audio_pipeline.py：麦克风 + VAD + 播放）
├── context/      上下文管理（block_store / importance_scorer / evictor / rag_retriever / heartbeat）
├── main.py       编排层 AetherAgent（生命周期、打断、CLI 交互）
├── config.py     Pydantic 配置（config.yaml + 环境变量覆盖）
├── protocols.py  Protocol 接口层（服务解耦）
└── tool_dispatcher.py  函数调用分发（状态机 + 后台任务 + 打断取消）
```

## 改动边界

- 核心逻辑位于 `aether/` 包；`dart/` 是独立动作生成服务（WSL2 侧），改动需注意跨环境与模型权重（不纳入版本控制）。
- 修改服务实现前先确认 `protocols.py` 的接口约定（ER2Port / MotionPort / OSCPort / AudioPort / TTSPort / DispatcherPort），避免破坏解耦。
- 日志模块名映射维护在 `aether/logging_config.py` 的 `MODULE_COLORS`（按 logger 前缀匹配），新增模块时同步更新。
- 配置项集中在 `config.yaml` 与 `aether/config.py`；新增配置必须提供默认值。
- 不把大文件、二进制产物或 IDE 运行目录加入版本控制（见 `.gitignore`）。

## 常用命令

- 安装依赖：`pip install -r requirements.txt`（含 pytest / pytest-asyncio）
- 运行测试：`pytest tests/ -v`（tests/conftest.py 自动设置 dummy API key）
- 启动主程序：`python -m aether`（需设置 `GEMINI_API_KEY`）
- 启动 DART 服务（WSL2）：`wsl -- bash -c "cd /root/DART && source /root/pytorch_rocm/bin/activate && bash start_server.sh"`
- 提交门槛检查：`./check_commit.ps1`；资源检查：`./check_resources.ps1`

## 验证与交付

- 改动后运行相关测试并保留结果（`pytest tests/` 的输出）。
- 提交前确认 `git status` 干净：无未跟踪的交付物；二进制产物（logs/*.pkl、logs/*.wav）已在 `.gitignore` 中。
- 大改动遵循 MAINTENANCE.md 中的维护流程（新增工具、替换 TTS 引擎等）。
