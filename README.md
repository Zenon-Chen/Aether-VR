# Aether

**VRChat 虚拟角色系统** — 大脑负责人格，小脑负责编排，调度器负责身体。

---

## 架构

```text
┌─────────────────────────────────────────────────────────┐
│                        感知层                            │
│   截图 1FPS      录音 100ms 块      StateStore(事实库)   │
└─────┬──────────────┬───────────────────┬────────▲───────┘
      │              │                   │        │执行回写
      ▼              ▼                   ▼        │
   ┌──────────────────────┐                       │
   │   大脑 Flash Lite     │  人格 / 观察 / 台词    │
   │   (书记官 subagent)   │                       │
   └──────────┬───────────┘                       │
              │ 观察 + 建议台词                     │
              ▼                                   │
   ┌─────────────────────┐                        │
   │  心跳拼装器(纯代码)   │◄───────────────────────┤ 事实
   └──────────┬──────────┘                        │
              │ 心跳                               │
              ▼                                   │
   ┌─────────────────────┐                        │
   │     ER2(小脑)        │  音频直触发=快路        │
   │     只做编排         │  心跳触发=慢路          │
   └──────────┬──────────┘                        │
              │ execute_plan                      │
              ▼                                   │
   ┌──────────────────────────────────────┐       │
   │  调度器：序号队列 + 五通道锁            │───────┘
   │  语音│面部│上半身│下半身│移动           │
   └──┬────┬────┬────┬────┬────────────────┘
      ▼    ▼    ▼    ▼    ▼
    TTS  表情  预录  DART  移动
      │         动作   │     │
      ▼               ▼     ▼
   虚拟麦        六设备桥  OSC输入
      │               │     │
      └───────┬───────┴─────┘
              ▼
   ┌─────────────────────────┐
   │ VRChat                  │
   │ SteamVR 虚拟六设备(DART) │
   │ OSC 参数(预录动作/表情)   │
   │ OSC 输入(移动)           │
   │ 麦克风(TTS→自动口型)     │
   └─────────────────────────┘
```

### 两条不可混淆的输出路径

| 要控制的东西 | 路径 | 原因 |
|---|---|---|
| 骨骼级姿态 | posture → SteamVR 虚拟六设备 → FBT IK | OSC 参数**不能**传骨骼旋转 |
| 预录动作 / 表情 | avatar → OSC Avatar Parameters | 只接受 int / float / bool |
| 世界中的位移 | locomotion → OSC `/input/*` | DART 走路只是骨架在走，不改变位置 |

---

## 目录结构

```text
aether/
├── contracts/          跨服务数据契约（无依赖，可被任何模块引用）
│   ├── frame.py        六设备位姿 + UDP 线格式（与 C++ 驱动对齐）
│   ├── plan.py         动作计划、通道、步骤
│   ├── facts.py        世界事实快照
│   ├── observation.py  大脑输出
│   └── perception.py   音视频块
│
├── core/               编排机制（不知道任何服务实现细节）
│   ├── state_store.py  权威事实库：谁执行谁登记
│   ├── facts_renderer.py 事实 → 自然语言
│   ├── scheduler.py    序号 + 通道锁          ← 全项目最关键
│   ├── heartbeat.py    心跳拼装与发送闸门
│   └── safety.py       紧急停止
│
├── services/           每个都可独立检修 / 替换 / 单独启动
│   ├── ports.py        所有服务的对外协议
│   ├── capture/        屏幕 1FPS + 麦克风 100ms
│   ├── brain/          人格与观察（Flash Lite）
│   ├── scribe/         批量标记 + 三档压缩 + 两级驱逐 + 落盘
│   ├── cerebellum/     ER2 会话 + 六工具 + planner
│   ├── speech/         TTS → 虚拟声卡
│   ├── posture/        DART → SteamVR 六设备（常驻保活）
│   ├── avatar/         预录动作与表情 OSC
│   └── locomotion/     移动 OSC + watchdog
│
├── infra/              配置、日志
└── runtime/            装配、CLI、冒烟测试
```

每个 service 目录内：`service.py` 实现、`__init__.py` 导出，
部分带 `__main__.py` 可独立运行。服务之间**不直接互相引用**，
只通过 `contracts` 与 `core` 交互。

---

## 进程模型

| 进程 | 位置 | 说明 |
|---|---|---|
| main-controller | 本地 | core + capture + cerebellum + speech + avatar + locomotion + brain |
| posture-service | 本地常驻 | DART 拉流 → 六设备 UDP 保活，可独立启动调试 |
| dart-service | WSL2 | `/root/DART`，ROCm / 7900 XT，端口 8900 |
| dartsteamvr driver | SteamVR | C++ OpenVR 驱动，监听 UDP 4242 |

---

## 快速开始

```powershell
# 1. 环境
$env:GEMINI_API_KEY = "your-key"
python -m venv .venv; .\.venv\Scripts\activate
pip install -r requirements.txt

# 2. DART 服务（WSL2）
wsl -d Ubuntu -- bash -lc "cd /root/DART && bash start_server.sh"

# 3. 冒烟测试（不需要任何云端 API）
python -m aether.runtime.smoke

# 4. 单独调试姿态桥
python -m aether.services.posture --prompt "walk forward" --duration 3

# 5. 启动
python -m aether
```

CLI：`/status` `/facts` `/say` `/do` `/face` `/move` `/motion` `/plan` `/stop` `/quit`

---

## 实施阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | capture + ER2 六工具 + 固定心跳 + 预录动作 + move + TTS | 骨架完成，待真机验收 |
| 2a | 大脑 Flash Lite + 书记官（人格、建议台词、长期记忆） | 接口就绪，`brain.enabled=false` |
| 2b | 调度器来源切换 + DART 联调（与 2a 并行，因桥已完成） | posture 保活已通 |
| 3 | 合流，全链路 2 小时稳定性 | 未开始 |
| — | 反射模块：第一版砍掉，`reflex_log` 字段保留 | 已留接口 |

**阶段 1 验收标准**：对角色说「你好」，1~2 秒内挥手并回话。

---

## 关键约束

- `contracts/frame.py` 的线格式与 `dart-steamvr-bridge/driver/src/driver.cpp`
  严格对应（184 字节，magic `0x54524144`）。**改动任一侧必须同步另一侧。**
- 驱动 1 秒收不到包即把设备标记为 OutOfRange，
  因此 posture 必须**常驻推流**，静止时也不能停。
- ER2 的工具调用必须回传 `function_response`，否则会话卡死。
- 台词只出自大脑；ER2 不自行编写人设台词。
- 每个计划不超过 5 秒，长行为拆成多个计划滚动执行。

---

## 测试

```powershell
python -m pytest tests/ -q          # 36 项
python -m aether.runtime.smoke      # 端到端，含真实 UDP 发包
```

---

## 许可

仅供学习研究。DART 模型受其原始许可证约束，Gemini API 使用需遵守 Google 条款。
