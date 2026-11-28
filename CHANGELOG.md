# 更新日志

更早版本的记录未保留，从 0.2.6 开始记录。

## 0.5.0 - 2026-11-28

- 新增 `links` 命令：跨会话提取公众号文章/网页链接，按 URL 去重，返回标题、URL、摘要、来源会话、发送者、时间、`message_id`、`share_count` 和 `first_shared_time`；支持 `--chat`、`--start-time`、`--end-time`、`--limit`。
- `search` 新增 `--structured`：返回与 `history --structured` 一致的消息对象，含 `message_id`、发送者身份、`url` 和 `text_truncated`。
- `favorites` 的 JSON 输出新增 `url`，文章收藏可直接取原文链接；`--format text` 也会显示链接。
- 修复 `members --format text` 抛出 `NameError`。
- 修复 `history --media` 图片路径错配：按 `packed_info_data` 精确定位；语音/视频不再返回同月任意文件。
- 聊天名匹配到多个对象时报错并列出候选，不再静默取第一个。
- 引用消息和系统消息（撤回等）提取可读文本，不再输出原始 XML。
- `attachments` 在最近图片缺少本地文件时继续向前扫描，尽量填满 `--limit`。
- 解密缓存从系统临时目录迁到 `~/.wechat-cli/cache/`，写入改为临时文件加原子替换，避免并发读到半成品、明文长期留在 temp。
- 新增 14 个回归测试，测试总数 63。

## 0.4.0 - 2026-11-28

- 新增 `archive` 命令：按会话导出 JSONL，默认按月分段（`--segment day` 可按天），写入 `<output-dir>/chats/<会话>/<起>_<止>.jsonl`。
- 维护 `<output-dir>/index/archive-index.jsonl` 索引：会话、时间段、条数、文件、导出时间，不含聊天正文；同一时间段重跑会覆盖分段文件并更新索引。
- 新增 8 个归档测试（分段、时间范围、目录名清洗、索引去重、命令端到端）。

## 0.3.1 - 2026-11-28

- `extract` 支持用本机 ffmpeg 转码 `wxgf`：输出路径以 `.jpg` / `.jpeg` / `.png` 结尾时自动转换，也可用 `--convert jpg|png` 显式指定。
- 找不到 ffmpeg 或转码失败时给出明确错误，不会把 `wxgf` 原样写进 `.jpg`。
- 新增 5 个测试（输出格式推断 4 个、ffmpeg 转码 1 个；未安装 ffmpeg 时自动跳过）。

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
