# Aether VR

**Aether VR** 是一个结合 **Omni 全模态模型**、**人体动作生成模型** 与 **多层级 Agent 系统** 实现的 **VRChat 虚拟角色扮演系统**。

系统让虚拟角色在 VR 世界中同时具备实时音视频感知、稳定的人格与长期记忆、自然语音对话，以及全身骨骼动作、面部表情与世界内自由移动能力。

---

## 技术栈

- **全模态交互与 Agent 认知**：Google Gemini Live (Omni 实时音视频流) + Gemini Flash Lite（角色人格、环境观察与长期记忆压缩）
- **人体动作生成**：可插拔动作生成后端（默认接入 DART 扩散动作生成模型，支持 WSL2 / ROCm 及独立 Sidecar 扩展）
- **语音合成与口型同步**：Gemini TTS + 虚拟声卡音频管线 + VRChat 15 视素（Viseme）实时口型驱动
- **身体控制与 VR 桥接**：
  - **SteamVR 虚拟六点追踪驱动（C++ OpenVR）**：45Hz UDP 实时全身骨骼姿态注入（FBT IK）
  - **VRChat OSC**：驱动 Avatars 3.0 预录动作（Action 层）、面部表情（FX 层）与世界位移（Locomotion）
  - **高频视觉伺服**：基于 OpenCV 的目标检测与脉冲式“走-看-走”自动逼近

---

## 基本功能

- **实时视听感知與自然对话**：持续采集麦克风语音与屏幕画面，角色可根据所见所闻主动搭话或回应，并保持连贯的人设与长期记忆（SQLite 持久化）。
- **全身动态动作与预录动作协同**：支持用自然语言实时生成任意全身/上半身骨骼动作，同时内置挥手、点头、摇头、拍手、指向、思考、鞠躬、坐下、站立等预录动作与多种面部表情，自动处理身体部位互斥与故障平滑回退。
- **世界内自主移动与视觉跟随**：支持前后左右移动、转向，以及自动寻找并走近视野中的人物。
- **毫秒级本地安全急停**：支持语音关键词（如“停下”、“别动”、“stop”）或指令 0ms 本地直通急停，立即中断语音、动作与移动。

---

## 使用方式

### 1. 环境准备

```powershell
# 克隆并进入项目目录
cd "Aether project"

# 创建虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# 设置 Gemini API Key
$env:GEMINI_API_KEY = "your-gemini-api-key"
```

### 2. 启动动作生成服务（可选，用于实时动态动作生成）

```powershell
wsl -d Ubuntu -- bash -lc "cd /root/DART && bash start_server.sh"
```

### 3. 启动 Aether VR

```powershell
python -m aether
```

启动后即可直接通过麦克风与 VRChat 中的角色语音互动，也可在控制台使用以下快捷命令：

| 命令 | 说明 |
|---|---|
| `/status` | 查看系统状态与各模块连接情况 |
| `/say <文本>` | 让角色说出指定台词 |
| `/do <动作>` | 触发预录动作（`wave` / `nod` / `shake_head` / `clap` / `point` / `think` / `bow` / `sit` / `stand`） |
| `/face <表情>` | 切换表情（`neutral` / `smile` / `surprised` / `sad` / `angry`） |
| `/motion <描述>` | 使用动作生成模型实时生成并播放动作 |
| `/move <方向> [秒]` | 控制角色移动（`forward` / `back` / `left` / `right` / `turn_left` / `turn_right`） |
| `/approach [目标]` | 视觉伺服自动走近目标人物（默认 `person`） |
| `/idle` | 取消当前动作并恢复待机站立姿态 |
| `/stop` | 紧急停止所有语音、动作与移动 |
| `/quit` | 退出程序 |

### 4. 本机自检与动作测试

```powershell
# 本机快速自检（无需 API Key 与外部硬件）
python scripts/dev_verify.py --smoke

# VRChat 角色表情与动作序列肉眼验收（需先开启 VRChat 并载入 Aether Avatar）
powershell -ExecutionPolicy Bypass -File .\scripts\vrchat_visible_test.ps1
```

---

## 许可

仅供学习与研究使用。动作生成模型受其原始许可证约束，Gemini API 使用需遵守 Google 服务条款。
