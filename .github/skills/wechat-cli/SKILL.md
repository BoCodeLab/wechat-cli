---
name: wechat-cli
description: Query the user's local WeChat data (chat history, messages, contacts, groups, members, favorites, statistics, unread/new messages) via the `wechat-cli` CLI. Use when the user asks to look up, search, read, export, or analyze their WeChat chats or contacts — e.g. "看看微信未读消息", "查一下和某人的聊天记录", "在群里搜关键词", "谁在群里发言最多", "导出聊天记录".
---

# WeChat CLI 快速使用

查询本地微信数据的命令行工具，默认输出 JSON（适合 AI 直接读取）。数据在本机解密，只读不写。

## 常用命令

```bash
wechat-cli doctor --format json                       # 检查本地配置和数据目录
wechat-cli sessions --limit 10                       # 最近会话
wechat-cli history "名字" --limit 20                  # 和某人的聊天记录
wechat-cli history "名字" --structured                # 获取结构化消息对象
wechat-cli history "群名" --limit 50 --format text    # 纯文本聊天记录
wechat-cli search "关键词" --chat "群名"               # 在指定聊天里搜消息
wechat-cli search "关键词" --limit 20                 # 全局搜索
wechat-cli contacts --query "名字"                    # 搜索联系人
wechat-cli contacts --detail "名字"                   # 联系人详情（昵称/备注/微信号/头像）
wechat-cli members "群名"                             # 群成员列表
wechat-cli stats "群名" --format text                 # 聊天统计（发言排行/类型分布/活跃图）
wechat-cli export "名字" --format markdown             # 导出聊天记录到 stdout
wechat-cli export "名字" --format txt --output x.txt   # 导出到文件
wechat-cli export "名字" --format jsonl                # 最新在前，逐条输出消息对象
wechat-cli favorites --query "关键词"                 # 搜索收藏
wechat-cli unread                                     # 未读会话
wechat-cli new-messages --cursor agent                # 使用独立游标逐条读取新增消息
```

## 常用选项

- `--limit N` / `--offset N`：分页
- `--start-time "2026-04-01"` / `--end-time "2026-04-03"`：时间范围过滤
- `--chat "名字"`：限定聊天（可多次指定，如 `--chat "群A" --chat "群B"`）
- `--type text|image|voice|video|sticker|location|link|file|call|system`：消息类型过滤
- `--format json|text`：输出格式（默认 JSON）
- `--query "关键词"`：contacts / favorites 的搜索词

## 注意

- 默认 JSON 输出，直接解析；需要人类可读时加 `--format text`。
- 终端中文乱码是 Windows 控制台 GBK/UTF-8 显示差异，数据本身正确；可设 `PYTHONIOENCODING=utf-8` 或加 `--format text` 缓解。
- 若提示「未找到微信数据目录」，先用 `wechat-cli doctor` 查看候选目录和初始化状态。
- `new-messages` 首次返回未读摘要，后续逐条返回新消息。各账号和游标名称的读取进度保存在 `~/.wechat-cli/message-cursors.sqlite3`。
