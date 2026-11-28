# WeChat CLI

[简体中文](README_CN.md) · [Apache 2.0](LICENSE)

Search and export your local desktop WeChat history from the command line. Look up conversations, contacts, group members, favorites, and unread sessions. Queries read the local databases and return JSON by default for scripts and AI agents.

## Install from source

You need Git and Python 3.10 or later.

```bash
git clone https://github.com/BoCodeLab/wechat-cli.git
cd wechat-cli
python -m pip install .
wechat-cli --version
```

`python -m pip install .` builds the CLI from this checkout. Use `python3` if that is your Python command. If your shell cannot find `wechat-cli`, activate the environment where you installed it or run `python -m wechat_cli.main --version` in that environment.

## Initialize and list recent conversations

Open the desktop WeChat client and sign in before initializing.

```bash
wechat-cli doctor --format text
wechat-cli init
wechat-cli sessions --limit 5 --format text
```

`doctor` works before initialization. It lists candidate data directories and checks the configuration, session database, and key file. The `sessions` command then verifies that the CLI can read the database.

If multiple accounts are listed or automatic detection misses your data, pass the path to the `db_storage` folder that contains `session/session.db`.

```bash
wechat-cli init --db-dir "PATH/TO/db_storage"
```

| Platform | Setup notes |
| --- | --- |
| Windows | Detection checks WeChat's saved paths and your Documents folder, including redirected Documents. |
| macOS | The package includes an Apple Silicon helper. Intel Macs require an x86_64 helper. The existing compatibility notes list macOS 26.3.1 or later and WeChat for Mac 4.1.8.100 or earlier. |
| Linux | The CLI supports local desktop WeChat data. Use `--db-dir` for custom storage locations. |

Initialization depends on your local WeChat version and system permissions. On macOS, initialization may attempt to re-sign the local WeChat application.

## Command reference

Replace the example names with a contact or group from your own session list. You can also pass the `username` returned by `sessions`.

| Task | Command |
| --- | --- |
| Diagnose a local setup | `wechat-cli doctor --format json` |
| Initialize access to local data | `wechat-cli init` |
| List recent or unread conversations | `wechat-cli sessions --limit 10` or `wechat-cli unread` |
| Read a conversation | `wechat-cli history "Alice" --limit 20 --format text` |
| Search messages | `wechat-cli search "deadline" --chat "Project Team"` |
| Find or inspect a contact | `wechat-cli contacts --query "Alice"` or `wechat-cli contacts --detail "Alice"` |
| List group members | `wechat-cli members "Project Team"` |
| View activity and sender statistics | `wechat-cli stats "Project Team" --format text` |
| Search favorites | `wechat-cli favorites --type article --query "guide"` |
| List local image attachments | `wechat-cli attachments "Project Team" --limit 10 --structured` |
| Decrypt one image attachment | `wechat-cli extract "<attachment_id>" --output image.jpg` |
| Export a conversation | `wechat-cli export "Alice" --format markdown --output alice.md` |
| Archive a conversation by period | `wechat-cli archive "Project Team" --output-dir data/wechat` |
| Read newly arrived messages | `wechat-cli new-messages --cursor daily-report` |

`history` and `search` support `--limit`, `--offset`, and message type filtering. Use `--chat` more than once to search several conversations. Omit it for global search. `search` matches both plain and compressed message text and returns up to 500 results per call.

`history`, `search`, `stats`, and `export` accept `--start-time` and `--end-time` in your local timezone. A date-only end time includes that entire day.

```bash
wechat-cli search "report" --type file --start-time "2026-04-01" --end-time "2026-04-03"
```

The message types for `history` and `search` are `text`, `image`, `voice`, `video`, `sticker`, `location`, `link`, `file`, `call`, and `system`. Run `wechat-cli --help` to see all commands.

## Structured output and export

```bash
wechat-cli history "Alice" --limit 20 --structured
wechat-cli export "Alice" --limit 1000 --format jsonl --output alice.jsonl
```

`history --structured` returns one object per message. Each object includes a `message_id` scoped to its source database, a Unix `timestamp` in seconds, `sender`, numeric `type` and `subtype`, a display label `msg_type`, and `text`. It also includes stable identity fields: `sender_username` (wxid or alias), `sender_contact_display` (contact display name), and `sender_group_nickname` (group nickname, empty when unavailable). Link messages also carry `url` (official-account articles and web links), empty when unavailable.

`history` and `search` JSON output includes a `meta` object for judging completeness:

- `status`: `ok`, `partial` (some queries failed), or `possibly_stale` (a message shard has no key).
- `missing_shards`: message shards without keys.
- `warnings`: messages worth showing to the user.
- `generated_at`: query time in Unix seconds.
- `latest_timestamp` / `latest_time`: the newest message time in the result or chat local data.

Add `--media` to `history` to try resolving local media paths.

JSONL writes the same fields as UTF-8, one message per line, newest first. Use `--output` to write a file or omit it to stream to stdout. Markdown and plain text (`txt`) are also available for exports intended for readers.

## Image attachments

`attachments` lists image messages that have a local file. Each entry carries `attachment_id`, `kind` (`image`, `image_hd`, or `thumbnail`), `timestamp`, `time`, sender identity fields, and `msg_id`. Pass the id to `extract` to decrypt one file:

```bash
wechat-cli attachments "Project Team" --limit 10 --structured
wechat-cli extract "wxid_xxx|message/message_0.db:Msg_xxx:42|image" --output image.jpg
```

`extract` writes the decrypted bytes and reports the detected format (`jpg`, `png`, `gif`, `webp`, `heic`, or `wxgf`), size, and decoder (`plain`, `legacy_xor`, `v1_aes`, `v2_aes`).

- Plain files and legacy single-byte XOR need no key.
- V1 (`07 08 56 31 08 07`) uses a fixed AES-128-ECB key.
- V2 (`07 08 56 32 08 07`) combines an AES segment and a single-byte XOR segment. The account key is derived once from local data and cached in `~/.wechat-cli/image_key.json`; it is never printed or uploaded. If a machine cannot derive it, `extract` fails with `V2 暂不支持` and the reason.
- `wxgf` is WeChat's HEVC container. When `ffmpeg` is on PATH, `extract` converts it to jpg or png if the output path ends in `.jpg`, `.jpeg`, or `.png`; use `--convert jpg` (or `png`) to force conversion. Otherwise the `.wxgf` bytes are written as-is.

## Archiving by month

`archive` exports one conversation into monthly (or daily) JSONL files and maintains an index that never stores message text:

```bash
wechat-cli archive "Project Team" --output-dir data/wechat
wechat-cli archive "Alice" --output-dir data/wechat --start-time 2026-01-01 --end-time 2026-06-30 --segment day
```

Files go to `<output-dir>/chats/<chat>/<start>_<end>.jsonl`. The index is `<output-dir>/index/archive-index.jsonl` with `chat`, `username`, `start`, `end`, `count`, `file`, and `exported_at`. Re-running the same range replaces the segment file and updates its index entry.

## Incremental reads and agents

Call `new-messages` with the same `--cursor` name on each run. The first call returns unread conversation summaries and records a baseline. Later calls return individual new messages, including messages that share a timestamp. Separate names let different consumers keep their own progress.

The CLI stores cursors in `~/.wechat-cli/message-cursors.sqlite3`. When the default cursor migrates from an older `last_check.json`, it reads the last recorded second once again to capture messages that share that timestamp.

For an agent integration, point the agent to [AGENTS.md](AGENTS.md) or the bundled [wechat-cli skill](.agents/skills/wechat-cli/SKILL.md). Use the installed CLI for queries and preserve message IDs when passing records between tools.

## Local files

| Location | Contents |
| --- | --- |
| Your WeChat `db_storage` | Source databases. Queries use them as read-only inputs. |
| `~/.wechat-cli/` | Default configuration, database key file, and incremental cursors. |
| `wechat_cli_cache` under the system temporary directory | Cached decrypted database snapshots. |
| Path passed to `--output` | Files you explicitly export. |

For a separate query configuration, put `--config` before the command, for example `wechat-cli --config ./config.json doctor --format text`.

## Development

```bash
python -m unittest discover -s tests -v
```

## License and acknowledgements

[Apache License 2.0](LICENSE). Use the CLI with local data you are authorized to access.

This project builds on [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt), which provides the core WeChat database decryption and data parsing capabilities.
