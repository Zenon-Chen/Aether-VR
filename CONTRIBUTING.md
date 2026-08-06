# 贡献指南

感谢你对 Aether 项目的关注！以下是参与开发的基本规范。

---

## 开发环境设置

### 1. 基础环境

```bash
# Windows 端
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install pytest pytest-asyncio
```

### 2. WSL2 DART 开发（可选）

动作生成模块部署在 WSL2，需要独立的 ROCm 环境：

```bash
cd /root/DART
source /root/pytorch_rocm/bin/activate
pip install -r requirements_dart.txt
```

### 3. 环境变量

```bash
# 开发时使用测试 Key 或 mock，不要将真实 Key 提交到代码中
$env:GEMINI_API_KEY = "your-dev-key"
```

---

## 代码规范

### Python 风格

- 遵循 **PEP 8**，行宽 100 字符
- 使用 **类型提示**（Type Hints）标注函数签名
- 异步函数使用 `async/await`，同步回调需注明原因
- 模块顶层添加 docstring 说明职责

### 命名约定

| 类型 | 风格 | 示例 |
|------|------|------|
| 类名 | PascalCase | `AetherAgent`, `TTSEngine` |
| 函数/方法 | snake_case | `send_audio`, `handle_tool_call` |
| 常量 | UPPER_SNAKE_CASE | `VALID_EMOTIONS`, `TEXT_MAX_LENGTH` |
| 私有属性 | 前缀 `_` | `_running`, `_exit_stack` |

### 模块结构

- 每个子包（`er2/`, `speech/`, `motion/`, `context/`, `io/`）维护独立的 `__init__.py`
- 公共接口通过 `__all__` 导出
- 配置类统一在 `config.py` 中定义

### 日志

- 使用标准 `logging` 模块，logger 名称格式：`aether.<module>`
- 日志前缀格式：`[模块名]`，例如 `[ER2]`, `[TTS]`, `[DART]`

---

## 测试

### 一键 dev 本机验证（推荐）

从仓库根目录运行：

```powershell
# 已安装依赖时：测试、编译检查、核心 UDP 冒烟、Motion Provider mock 测试
powershell -ExecutionPolicy Bypass -File .\scripts\dev_test.ps1

# 首次使用或依赖可能不完整时
powershell -ExecutionPolicy Bypass -File .\scripts\dev_test.ps1 -Install

# 额外构建 Motion Provider wheel/sdist
powershell -ExecutionPolicy Bypass -File .\scripts\dev_test.ps1 -BuildProvider

# 只运行无硬件的 Python 检查，不执行 UDP 冒烟
powershell -ExecutionPolicy Bypass -File .\scripts\dev_test.ps1 -SkipSmoke
```

该入口只使用本地 Python、mock 和 loopback UDP，不启动 Gemini、WSL、SteamVR、VRChat 或真实音频设备。成功输出 `PASS: local dev checks completed` 后，才进入真实硬件测试。日志和测试数据库仍写入已被 `.gitignore` 忽略的 `logs/`、`data/`。

底层 Python 入口也可以直接运行：

```powershell
python scripts/dev_verify.py --full --smoke --build-provider
```

### 单独测试

```bash
# 运行全部测试
python -m pytest tests/ -v

# 运行单个测试文件
python -m pytest tests/test_config.py -v

# 带覆盖率报告
python -m pytest tests/ -v --cov=aether
```

**测试原则：**
- 所有外部依赖（API、网络、硬件）必须 mock
- 测试文件命名：`test_<module>.py`
- 异步测试使用 `@pytest.mark.asyncio` 装饰器

---

## PR 流程

1. **Fork** 项目并创建功能分支：
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **编写代码与测试**，确保所有测试通过：
   ```bash
   python -m pytest tests/ -v
   ```

3. **提交变更**，commit message 使用约定式前缀：
   - `feat:` 新功能
   - `fix:` Bug 修复
   - `refactor:` 重构
   - `test:` 测试相关
   - `docs:` 文档更新

4. **发起 Pull Request**，描述改动内容和测试情况

---

## 目录说明

| 路径 | 说明 |
|------|------|
| `aether/` | 主程序包，Windows 端运行 |
| `dart/` | DART 动作生成服务，WSL2 端运行 |
| `tests/` | 单元测试（pytest） |
| `config.yaml` | 默认配置，不提交敏感信息 |
| `logs/` | 运行时日志（gitignore） |
