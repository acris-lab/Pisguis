# Pisguis

Pisguis 是面向 AstrBot 4.29.x 的单插件聊天增强包，集中提供命令权限、提示词组装、群聊接话、回复分段和群聊/私聊记忆。

## 安装

在 AstrBot WebUI 中从 GitHub 仓库安装：

```text
https://github.com/acris-lab/Pisguis
```

也可以把仓库根目录复制为 `AstrBot/data/plugins/astrbot_plugin_pisguis/`，然后重启 AstrBot。

首次启动会自动创建：

```text
data/plugin_data/astrbot_plugin_pisguis/
├── access_config.json
├── addressee_config.json
├── chat_style_config.json
├── memory_config.json
├── chat_rules.md
├── prompt/
├── prompts/
├── cmd_list/
└── mem_archive/
```

从旧五插件版本升级时，Pisguis 只会复制缺失数据，不会覆盖现有文件。安装单插件后应移除旧的五个 `astrbot_plugin_pisguis_*` 插件目录，避免重复响应。

## 首次使用

主人列表为空时，第一个私聊机器人的人会自动成为主人。主人可在私聊或群聊中管理二级管理员：

```text
/guard
/guard add <QQ>
/guard remove <QQ>
/guard list
```

## 命令

```text
/config
/config mode [名字]
/config ctx 2-32
/config think on|off
/prompt
/cmd

/style
/style delay 0-10

/mem
/mem show
/mem clear
/mem page 0-100
/mem book 3-5
/mem pagecap 1-20
/mem bookcap 1-50
/mem gen
```

只有半角 `/` 开头的命令会进入权限检查。未知命令、无权限命令和纯字符串命令都按普通聊天处理。

## 开发

入口为 `main.py`，业务模块位于 `pisguis/`。运行静态编译检查：

```powershell
python -m compileall -q main.py pisguis
```

发布前至少验证单插件加载、命令鉴权、空数据初始化、旧数据迁移、群聊隔离记忆和分段发送。
