# AGENTS.md — Aether 协作指南

在此仓库工作前先读本文件，按模块地图定位改动边界。

## 项目定位

VRChat 中的 AI 虚拟角色系统。**大脑（人格）/ 小脑（编排）/ 调度器（身体）** 三层分离。

技术栈：Gemini ER2 Live（编排）、Gemini Flash Lite（人格）、DART（动作生成，WSL2/ROCm）、
Gemini TTS（语音）、SteamVR 虚拟六设备（骨骼）、OSC（预录动作与移动）。

## 分层规则（最重要）

```text
contracts/  ← 无依赖。任何模块都可引用
core/       ← 只依赖 contracts。不知道任何 service 的存在
services/   ← 只依赖 contracts + infra。services 之间互不引用
runtime/    ← 唯一知道「谁连接谁」的地方
```

**违反此规则的改动一律拒绝。** 服务之间要通信，走 `core.StateStore` 或
在 `runtime/agent.py` 里接线，不要直接 import 对方。

## 模块地图

| 路径 | 职责 | 改动注意 |
|---|---|---|
| `contracts/frame.py` | 六设备位姿 + UDP 线格式 | **与 C++ 驱动强耦合**，改动需同步 `driver.cpp` 并重新编译 |
| `contracts/plan.py` | 计划、通道、动作类型 | 新增 ActionKind 必须同时在 `CHANNEL_USAGE` 声明通道占用 |
| `core/scheduler.py` | 序号 + 通道锁 | 全项目最关键。改动必须跑 `tests/test_scheduler.py` |
| `core/state_store.py` | 权威事实库 | 只能由执行方登记，**任何模型都不得写入** |
| `core/heartbeat.py` | 心跳闸门 | 「ER2 生成中不发」是硬规则，不要绕过 |
| `services/posture/` | DART → SteamVR | 必须常驻推流，静止也不能停 |
| `services/avatar/` | 预录动作 OSC | 只能发 int/float/bool，不要试图发骨骼 |
| `services/cerebellum/planner.py` | 工具调用 → Plan | 协议翻译层，不要在此执行任何动作 |
| `runtime/agent.py` | 装配 | 新增服务在这里注册执行器与紧急停止钩子 |

## 新增一个服务

1. `services/<name>/service.py` 实现，含 `start` / `stop` / `health`
2. 在 `services/ports.py` 加 Protocol
3. `services/<name>/__init__.py` 导出
4. `infra/config.py` 加配置段（**必须有默认值**）
5. `runtime/agent.py` 实例化 + 注册执行器 + 注册紧急停止钩子
6. `infra/logging_setup.py` 的 `MODULE_COLORS` 加颜色
7. 需要独立调试则加 `__main__.py`

## 命令

```powershell
pip install -r requirements.txt
python -m pytest tests/ -q              # 单元测试
python -m aether.runtime.smoke          # 端到端，含真实 UDP 发包
python -m aether.services.posture --prompt "wave hand"   # 单服务调试
python -m aether                        # 主程序
```

## 硬约束

- **不要**用 OSC Avatar Parameters 传骨骼旋转，VRChat 会静默丢弃。
- **不要**让 ER2 编写角色人设台词，台词来自大脑的 `suggested_reply`。
- **不要**忘记回传 `function_response`，ER2 会话会卡死。
- **不要**在 posture 播放结束后停止推流，设备会掉线。
- 每个计划不超过 5 秒；长行为拆成多个计划。
- 新增配置必须有默认值，缺省配置也要能启动。

## 历史

`_legacy_v1/` 是被推翻的第一版（单 ER2 写台词 + OSC 发骨骼），仅供参考，不要复用。
`_legacy/` 是更早的实验残留。
