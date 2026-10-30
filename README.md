# Aether VR

**Aether VR** 是一个基于 **Omni 全模态模型**、**动作生成模型** 以及 **Agent 系统** 实现的 **VR 虚拟角色扮演系统**。

系统赋予 VRChat 中的虚拟角色实时视听感知、稳定的人格与长期记忆、自然语音对话，以及全身动作、面部表情与世界内自由移动能力。

---

## 基本功能

- **实时视听感知与自然对话**：持续感知麦克风语音与画面，角色可根据所见所闻自然对话，并维持连贯的人格设定与长期记忆。
- **动态动作生成与预录动作**：支持根据语义实时生成全身/上半身动作，同时内置挥手、点头、摇头、拍手、指向、思考、鞠躬、坐下、站立等基础动作与多种面部表情。
- **世界内移动与视觉跟随**：支持前后左右移动、转向，以及自动寻找并走近视野中的目标人物。
- **实时语音急停**：支持通过语音关键词（如“停下”、“别动”、“stop”）或控制台指令立即中断当前所有语音、动作与移动。

---

## 使用方式

### 1. 环境准备

```powershell
# 进入项目目录
cd "Aether project"

# 创建虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# 设置 API Key
$env:GEMINI_API_KEY = "your-api-key"
```

### 2. 启动动作生成服务（可选）

```powershell
wsl -d Ubuntu -- bash -lc "cd /root/DART && bash start_server.sh"
```

### 3. 启动 Aether VR

```powershell
python -m aether
```

启动后可直接通过麦克风与角色语音交互，也可在控制台输入以下指令：

| 命令 | 说明 |
|---|---|
| `/status` | 查看系统当前状态 |
| `/say <文本>` | 让角色说出指定台词 |
| `/do <动作>` | 触发预录动作（`wave` / `nod` / `shake_head` / `clap` / `point` / `think` / `bow` / `sit` / `stand`） |
| `/face <表情>` | 切换表情（`neutral` / `smile` / `surprised` / `sad` / `angry`） |
| `/motion <描述>` | 实时生成并播放动作 |
| `/move <方向> [秒]` | 控制角色移动（`forward` / `back` / `left` / `right` / `turn_left` / `turn_right`） |
| `/approach [目标]` | 自动走近目标人物（默认 `person`） |
| `/idle` | 取消当前动作并恢复待机站立姿态 |
| `/stop` | 紧急停止所有语音、动作与移动 |
| `/quit` | 退出程序 |

### 4. 快速自检与动作测试

```powershell
# 本机快速自检
python scripts/dev_verify.py --smoke

# VRChat 角色表情与动作序列测试（需开启 VRChat 并载入 Aether Avatar）
powershell -ExecutionPolicy Bypass -File .\scripts\vrchat_visible_test.ps1
```

---

## 许可

仅供学习与研究使用。
