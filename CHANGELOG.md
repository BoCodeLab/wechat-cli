# 更新日志

更早版本的记录未保留，从 0.2.6 开始记录。

## 0.2.6 - 2026-11-28

- `history` / `search` 的 JSON 输出新增 `meta`：`status`（`ok` / `partial` / `possibly_stale`）、`missing_shards`、`warnings`、`generated_at`、`latest_timestamp` / `latest_time`。
- `history --structured` 新增稳定身份字段：`sender_username`、`sender_contact_display`、`sender_group_nickname`（群昵称解析不到时为空字符串）。
- 链接类消息（公众号文章、网页链接）新增 `url` 字段，解析不到时为空字符串。
- 新增 `meta`、群昵称解析和链接 URL 的单元测试；`history` 测试补充身份字段断言。
