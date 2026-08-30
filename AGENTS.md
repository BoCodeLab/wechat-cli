# AGENTS.md

本项目是 `wechat-cli` —— 一个查询本地微信数据的命令行工具（本地解密、只读、默认 JSON 输出，专为 AI 集成设计）。

## 查询本地微信数据

本项目内置了一个跨 Agent 通用的 skill：`wechat-cli`，用于查询微信聊天记录、联系人、群成员、收藏、统计、未读等。

- 完整命令见 skill 文件：`.claude/skills/wechat-cli/SKILL.md`
- 同一份 skill 也同步在以下发现目录，供不同 Agent 自动加载：
  - `.claude/skills/`（Claude Code）
  - `.agents/skills/`（通用 / Warp / OpenCode）
  - `.github/skills/`（GitHub Copilot）
  - `.codex/skills/`（OpenAI Codex）
  - `.cursor/skills/`（Cursor）

常用命令速查：

```bash
wechat-cli sessions --limit 10            # 最近会话
wechat-cli history "名字" --limit 20       # 聊天记录
wechat-cli history "群名" --limit 50 --format text  # 纯文本聊天记录
wechat-cli search "关键词" --chat "群名"    # 在指定聊天里搜消息
wechat-cli contacts --query "名字"         # 搜联系人
wechat-cli members "群名"                  # 群成员
wechat-cli stats "群名" --format text      # 聊天统计
wechat-cli favorites --query "关键词"      # 收藏
wechat-cli unread                          # 未读会话
wechat-cli new-messages                    # 增量新消息
```

默认输出 JSON，直接解析；需要人类可读时加 `--format text`。
