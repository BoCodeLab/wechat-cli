"""按消息表增量读取，按账号和游标名称持久化读取进度。"""

import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime

import click

from ..core.config import CONFIG_FILE, STATE_DIR
from ..core.contacts import get_contact_names
from ..core.messages import collect_new_messages
from ..output.formatter import output
from .unread import read_unread_sessions

CURSOR_FILE = os.path.join(STATE_DIR, "message-cursors.sqlite3")
LEGACY_STATE_FILE = os.path.join(STATE_DIR, "last_check.json")


def _load_legacy_state():
    try:
        with open(LEGACY_STATE_FILE, encoding="utf-8") as file:
            saved = json.load(file)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(saved, dict):
        return None
    return {name: timestamp for name, timestamp in saved.items() if type(timestamp) is int and timestamp > 0} or None


def _initial_unread(app, names):
    session_path = app.cache.get(os.path.join("session", "session.db"))
    if not session_path:
        raise sqlite3.DatabaseError("无法读取 session.db")

    messages = [message for message in read_unread_sessions(session_path, names) if message['timestamp'] > 0]
    for message in messages:
        message['time'] = message['time'].split()[-1]
    return messages


def _show_messages(messages, first_call, fmt):
    if fmt == 'json':
        count_field = 'unread_count' if first_call else 'new_count'
        output({'first_call': first_call, count_field: len(messages), 'messages': messages}, 'json')
        return

    if not messages:
        output("当前无未读消息（已记录读取进度）" if first_call else "无新消息", 'text')
        return

    lines = []
    for message in messages:
        entry = f"[{message['time']}] {message['chat']}"
        if message['is_group']:
            entry += " [群]"
        if first_call:
            entry += f" ({message['unread']}条未读)"
        else:
            entry += f": {message['msg_type']}"
            if message['sender']:
                entry += f" ({message['sender']})"
        lines.append(f"{entry} - {message['last_message']}")

    header = f"当前 {len(messages)} 个未读会话" if first_call else f"{len(messages)} 条新消息"
    output(header + "\n\n" + "\n".join(lines), 'text')


@click.command("new-messages")
@click.option("--cursor", "cursor_name", default="default", show_default=True, help="为调用方指定独立的增量游标")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "text"]), help="输出格式")
@click.pass_context
def new_messages(ctx, cursor_name, fmt):
    """首次建立游标并返回未读摘要，后续逐条返回新增消息。"""
    cursor_name = cursor_name.strip()
    if not cursor_name:
        raise click.BadParameter("游标名称不能为空", param_hint="--cursor")

    app = ctx.obj
    source_path = os.path.normcase(os.path.realpath(app.db_dir))
    source = hashlib.sha256(os.fsencode(source_path)).hexdigest()
    try:
        os.makedirs(os.path.dirname(CURSOR_FILE), exist_ok=True)
        with closing(sqlite3.connect(CURSOR_FILE, timeout=30)) as state, state:
            # 序列化读取和提交，避免并发轮询覆盖进度。
            state.execute("BEGIN IMMEDIATE")
            state.execute("""
                CREATE TABLE IF NOT EXISTS message_cursors (
                    source TEXT NOT NULL,
                    name TEXT NOT NULL,
                    checkpoints TEXT NOT NULL,
                    PRIMARY KEY (source, name)
                )
            """)
            saved = state.execute(
                "SELECT checkpoints FROM message_cursors WHERE source = ? AND name = ?",
                (source, cursor_name),
            ).fetchone()
            previous = json.loads(saved[0]) if saved else None
            if saved and not isinstance(previous, dict):
                raise ValueError("增量游标格式无效")

            legacy = None
            if previous is None and cursor_name == "default" and os.path.normcase(app.config_path) == os.path.normcase(CONFIG_FILE):
                legacy = _load_legacy_state()
            first_call = previous is None and legacy is None
            names = get_contact_names(app.cache, app.decrypted_dir)
            messages, checkpoints = collect_new_messages(
                app.msg_db_keys, app.cache, names, app.display_name_fn, previous, legacy,
            )
            if first_call:
                messages = _initial_unread(app, names)
            else:
                for message in messages:
                    message['last_message'] = message['text']
                    message['time'] = datetime.fromtimestamp(message['timestamp']).strftime('%H:%M:%S')

            _show_messages(messages, first_call, fmt)
            sys.stdout.flush()
            if checkpoints != previous:
                state.execute("""
                    INSERT INTO message_cursors (source, name, checkpoints) VALUES (?, ?, ?)
                    ON CONFLICT(source, name) DO UPDATE SET checkpoints = excluded.checkpoints
                """, (source, cursor_name, json.dumps(checkpoints)))
    except (OSError, sqlite3.Error, ValueError) as error:
        click.echo(f"错误: {error}", err=True)
        ctx.exit(3)
