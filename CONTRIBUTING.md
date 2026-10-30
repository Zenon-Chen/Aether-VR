# 贡献指南

感谢你对 Aether 项目的关注！以下是参与开发的基本规范。

---

## 开发环境设置

### 1. 基础环境

```powershell
# Windows 端
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
pip install pytest pytest-asyncio
```

### 2. WSL2 DART 开发（可选）

外部动作生成服务部署在 WSL2（默认监听 `127.0.0.1:8900`）：

```bash
cd /root/DART
source /root/pytorch_rocm/bin/activate
pip install -r requirements_dart.txt
```

### 3. 环境变量

```powershell
# 开发时使用测试 Key 或 mock，不要将真实 Key 提交到代码中
$env:GEMINI_API_KEY = "your-dev-key"
```

---

## 架构分层与代码规范（硬约束）

### 单向依赖原则

仓库严格执行 `AGENTS.md` 规定的单向依赖边界（由 `tests/test_runtime_modular_wiring.py` 进行 AST 静态守卫）：

```text
contracts/  ← 零依赖公共数据契约
core/       ← 仅依赖 contracts；不知道任何具体 service
services/   ← 仅依赖 contracts + infra + services/ports.py；严禁跨服务相互 import
runtime/    ← 唯一组合根（agent / motion_wiring / executors / perception_gate / coordinator）
```

### Python 风格与命名约定

- 遵循 **PEP 8**，行宽 100 字符，函数签名标注类型提示（Type Hints）。
- 异步函数使用 `async/await`；阻塞文件/数据库/音频 I/O 不得直接卡死主事件循环。
- 新增配置项统一在 `aether/infra/config.py` 定义，**必须提供安全默认值**，确保空配置可启动。

| 类型 | 风格 | 示例 |
|------|------|------|
| 类名 | PascalCase | `AetherRuntime`, `BodyExecutorSuite`, `SpeechService` |
| 端口协议 | PascalCase + `Port` | `SpeechPort`, `MotionProviderPort`, `VisionPort` |
| 函数/方法 | snake_case | `send_audio`, `parse_tool_call`, `approach_target` |
| 常量 | UPPER_SNAKE_CASE | `STOP_KEYWORDS`, `DEFAULT_STEP_TIMEOUT` |
| 私有属性 | 前缀 `_` | `_running`, `_bg_tasks` |

### 日志

- 使用标准 `logging` 模块，logger 名称格式：`aether.<layer>.<module>`（并在 `aether/infra/logging_setup.py` 的 `MODULE_COLORS` 登记模块颜色）。
- 日志前缀格式：`[模块名]`，例如 `[Cerebellum]`, `[Speech]`, `[Sched]`, `[Stop]`。

---

## 测试

### 一键 dev 本机验证（推荐）

从仓库根目录运行：

```powershell
# 已安装依赖时：全量单测、分层守卫、Unity 资产契约、编译检查、UDP 冒烟、Motion Provider 测试
powershell -ExecutionPolicy Bypass -File .\scripts\dev_test.ps1

# 底层 Python 入口
python scripts/dev_verify.py --smoke
```

该入口只使用本地 Python、mock 和 loopback UDP，不启动 Gemini、WSL、SteamVR、VRChat 或真实音频设备。

### VRChat 可见动作验收（真实运行）

先手动进入 VRChat 私人安全世界并载入 Aether Avatar，然后在仓库根目录运行：

```powershell
# 仅检查配置和动作序列，不发送 OSC
python -m aether.runtime.vrchat_test --dry-run

# 真实发送 OSC 驱动表情与 9 个预录动作
powershell -ExecutionPolicy Bypass -File .\scripts\vrchat_visible_test.ps1
```

### 单独测试

```powershell
# 运行全部测试
python -m pytest tests/ -v

# 运行架构与运行期接线测试
python -m pytest tests/test_runtime_modular_wiring.py -v

# 运行 Unity Avatar 生成资产静态契约测试
python -m pytest tests/test_unity_avatar_assets.py -v
```

**测试原则：**
- 所有外部依赖（云端 API、网络、硬件、子进程）在单测中必须使用 mock 或本地测试桩；
- 后台 `asyncio.create_task` 必须纳入生命周期集合并在 `stop()` 时取消等待，严禁悬空裸任务。

---

## 目录说明

| 路径 | 说明 |
|------|------|
| `aether/contracts/` | 跨模块不可变数据契约与线格式 |
| `aether/core/` | 状态库、双预算通道锁调度器、心跳闸门与紧急停止 |
| `aether/services/` | 各独立输入/认知/输出服务与 `ports.py` 协议定义 |
| `aether/runtime/` | 运行时组合根、四大协作者模块、CLI 与冒烟/验收脚本 |
| `motion-provider/` | 独立可打包的动作生成 HTTP sidecar 包 |
| `avatar-project/` | Unity 2022.3 + VRChat Avatars 3.0 角色工程与自动生成脚本 |
| `dart-steamvr-bridge/` | C++ SteamVR 虚拟六设备 OpenVR 驱动 |
| `tests/` | pytest 自动化测试套件 |
| `config.yaml` | 默认配置，不提交敏感信息 |
