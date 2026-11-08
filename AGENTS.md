# Repository Guidelines

## Project Structure

Pisguis 是面向 AstrBot 4.29.x 的单插件。`main.py` 是唯一插件入口与事件注册处，`metadata.yaml` 提供仓库安装元数据。业务代码位于 `pisguis/`：

- `common.py`：设置、提示词总装、命令表。
- `guard.py`：命令鉴权、工具限制、会话身份。
- `group_reply.py`：群聊接话。
- `reply_style.py`：文本清洗、分段与延迟。
- `memory.py`：群聊/私聊页、本两层记忆。
- `paths.py`：统一数据路径与旧版本迁移。

运行数据只写入 `data/plugin_data/astrbot_plugin_pisguis/`，不得提交配置、记忆或日志。

## Build and Test

项目没有独立构建步骤。提交前至少执行：

```powershell
python -m compileall -q main.py pisguis
```

功能测试应把 `PISGUIS_PLUGIN_DATA_ROOT` 指向临时目录，避免污染真实数据。重点验证单插件只注册一个 `Star`、命令处理器可执行、开关持久化、空目录初始化及旧数据迁移。部署后重启 AstrBot，并检查日志中的插件加载错误。

## Coding Style

使用 Python 3.12、四空格缩进、类型标注和异步接口。事件与命令只在根 `main.py` 注册；`pisguis/` 内部类保持普通服务类，不继承 `Star`。模块通过 `self.context` 访问 AstrBot，避免新增对内核 `data/` 配置的直接修改。

配置读写使用 UTF-8，JSON 更新优先采用临时文件加 `os.replace`。模块开关统一接受 `on|off` 和 `开|关`，状态命令无参数时显示当前状态。

## Naming and Commands

插件名为 `astrbot_plugin_pisguis`，模块使用小写蛇形命名。用户命令只接受半角 `/` 前缀；`common` 保持常开，其他模块提供 `/guard`、`/group`、`/style`、`/mem` 的 `on|off` 子命令。

## Commit and Pull Request

使用 Conventional Commits，例如 `feat: add module toggles`、`fix: preserve memory migration`。提交应小而聚焦，禁止包含 QQ 号、昵称、本机路径、实例 UUID、密钥或运行备份。

PR 需说明用户可见行为、数据兼容性和验证命令；涉及回复格式时附前后示例。未经明确要求，不修改 AstrBot 内核或实例配置。
