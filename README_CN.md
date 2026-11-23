# WeChat CLI

[English](README.md) · [Apache 2.0](LICENSE)

在终端查询和导出电脑版微信的本地聊天记录。支持会话、联系人、群成员、收藏、未读和聊天统计。查询以本地数据库为只读输入，默认输出 JSON，方便脚本和 AI Agent 调用。

## 从源码安装

需要 Git 和 Python 3.10 或更高版本。

```bash
git clone https://github.com/BoCodeLab/wechat-cli.git
cd wechat-cli
python -m pip install .
wechat-cli --version
```

`python -m pip install .` 直接从当前仓库构建。如果你的 Python 命令是 `python3`，请替换上面的 `python`。如果终端找不到 `wechat-cli`，请激活安装时使用的 Python 环境，或在该环境中运行 `python -m wechat_cli.main --version`。

## 初始化并读取最近会话

先打开并登录电脑版微信，再初始化并验证读取。

```bash
wechat-cli doctor --format text
wechat-cli init
wechat-cli sessions --limit 5 --format text
```

`doctor` 在初始化前也能运行，会列出候选数据目录，并检查配置、会话数据库和密钥文件。最后用 `sessions` 确认能读取会话。

如果检测到多个账号，或自动检测找不到你的数据，请选择包含 `session/session.db` 的 `db_storage` 目录，并在初始化时指定。

```bash
wechat-cli init --db-dir "你的数据目录/db_storage"
```

| 平台 | 初始化说明 |
| --- | --- |
| Windows | 检查微信保存的路径和用户的「文档」目录，也支持重定向的「文档」。 |
| macOS | 随包提供 Apple Silicon 辅助程序，Intel Mac 需 x86_64 版本。现有兼容性说明列出 macOS 26.3.1 或更高版本，以及微信 Mac 版 4.1.8.100 或更早版本。 |
| Linux | 支持桌面微信的本地数据。使用自定义存储位置时，可通过 `--db-dir` 指定目录。 |

初始化受本机微信版本和系统权限影响。macOS 上的初始化可能尝试重新签名本机的微信应用。

## 命令速查

将示例中的名字替换为你自己的联系人或群名，也可以直接使用 `sessions` 返回的 `username`。

| 用途 | 命令 |
| --- | --- |
| 检查配置和本机数据目录 | `wechat-cli doctor --format json` |
| 初始化本地数据访问 | `wechat-cli init` |
| 查看最近会话或未读会话 | `wechat-cli sessions --limit 10` 或 `wechat-cli unread` |
| 读取指定聊天 | `wechat-cli history "张三" --limit 20 --format text` |
| 搜索消息 | `wechat-cli search "截止日期" --chat "项目群"` |
| 搜索联系人或查看详情 | `wechat-cli contacts --query "张三"` 或 `wechat-cli contacts --detail "张三"` |
| 查看群成员 | `wechat-cli members "项目群"` |
| 查看活跃时段和发言统计 | `wechat-cli stats "项目群" --format text` |
| 搜索收藏 | `wechat-cli favorites --type article --query "使用指南"` |
| 导出聊天记录 | `wechat-cli export "张三" --format markdown --output chat.md` |
| 逐条读取新消息 | `wechat-cli new-messages --cursor daily-report` |

`history` 和 `search` 支持 `--limit`、`--offset` 与消息类型筛选。给 `search` 传入多个 `--chat` 可搜索多个会话，省略则全局搜索。普通文本和压缩消息都可以匹配，每次搜索最多返回 500 条。

`history`、`search`、`stats` 和 `export` 都支持以本地时区表示的 `--start-time` 和 `--end-time`。结束时间只写日期时，包含该日全天。

```bash
wechat-cli search "报告" --type file --start-time "2026-04-01" --end-time "2026-04-03"
```

`history` 和 `search` 的消息类型包括 `text`、`image`、`voice`、`video`、`sticker`、`location`、`link`、`file`、`call` 和 `system`。运行 `wechat-cli --help` 可查看全部命令。

## 结构化消息与导出

```bash
wechat-cli history "张三" --limit 20 --structured
wechat-cli export "张三" --limit 1000 --format jsonl --output chat.jsonl
```

`history --structured` 每条消息返回一个对象，包含来源数据库内的 `message_id`、Unix 秒级 `timestamp`、发送者 `sender`、数字类型 `type` 和 `subtype`、类型名称 `msg_type` 及正文 `text`。

给 `history` 加上 `--media` 可尝试解析本地媒体路径。

JSONL 使用相同字段和 UTF-8 编码，每行一条，最新消息在前。指定 `--output` 可写入文件，省略则写入标准输出。面向阅读的导出也支持 Markdown 和纯文本（`txt`）。

## 增量读取与 Agent 集成

每次调用 `new-messages` 时使用同一个 `--cursor` 名称。首次返回未读会话摘要并建立游标，后续逐条返回新消息，同一秒收到的消息也能分别读取。不同调用方可以使用各自的名称维护独立进度。

游标保存在 `~/.wechat-cli/message-cursors.sqlite3`。默认游标从旧的 `last_check.json` 迁移时会重读最后一秒，以补回同秒消息。

接入 Agent 时，可以让它参考 [AGENTS.md](AGENTS.md) 或仓库自带的 [wechat-cli 技能](.agents/skills/wechat-cli/SKILL.md)。使用已安装的 CLI 查询，在工具间传递消息时保留 `message_id`。

## 本地文件

| 位置 | 内容 |
| --- | --- |
| 微信的 `db_storage` | 原始数据库，查询时作为只读输入。 |
| `~/.wechat-cli/` | 默认配置、数据库密钥文件和增量游标。 |
| 系统临时目录下的 `wechat_cli_cache` | 解密后的数据库缓存。 |
| `--output` 指定的位置 | 用户主动导出的文件。 |

如需使用单独的查询配置，请把 `--config` 放在子命令之前，例如 `wechat-cli --config ./config.json doctor --format text`。

## 开发测试

```bash
python -m unittest discover -s tests -v
```

## 许可证与致谢

项目采用 [Apache License 2.0](LICENSE)。请在获授权的本地数据上使用。

本项目基于 [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) 开发，该仓库提供了微信数据库解密和数据解析的核心能力。
