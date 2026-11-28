# 更新日志

更早版本的记录未保留，从 0.2.6 开始记录。

## 0.3.0 - 2026-11-28

- 新增 `attachments` 命令：列出会话中本地可用的图片附件，返回 `attachment_id`、`kind`、`timestamp`、`time`、发送者身份字段和 `msg_id`。
- 新增 `extract` 命令：解密单个图片附件，支持明文、legacy 单字节 XOR、V1 固定 AES 和 V2 AES+XOR，输出识别到的格式、大小和解码器。
- V2 账号密钥首次使用时从本机目录派生并缓存到 `~/.wechat-cli/image_key.json`，不打印、不上传；无法派生时明确报 `V2 暂不支持` 并说明原因。
- 新增 12 个合成 `.dat` 单测，覆盖明文 JPEG、legacy XOR、V1 AES、V2 AES+XOR、文件头识别和错误处理。

## 0.2.6 - 2026-11-28

- `history` / `search` 的 JSON 输出新增 `meta`：`status`（`ok` / `partial` / `possibly_stale`）、`missing_shards`、`warnings`、`generated_at`、`latest_timestamp` / `latest_time`。
- `history --structured` 新增稳定身份字段：`sender_username`、`sender_contact_display`、`sender_group_nickname`（群昵称解析不到时为空字符串）。
- 链接类消息（公众号文章、网页链接）新增 `url` 字段，解析不到时为空字符串。
- 新增 `meta`、群昵称解析和链接 URL 的单元测试；`history` 测试补充身份字段断言。
