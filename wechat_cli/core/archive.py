"""归档 — 按会话和时间段导出 JSONL，并维护不含正文的索引表。"""

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from .messages import _is_safe_msg_table_name, iter_chat_history

INDEX_NAME = 'archive-index.jsonl'
SEGMENTS = ('month', 'day')


class ArchiveError(Exception):
    """归档过程中的可报告错误。"""


def sanitize_name(name):
    """把会话名转换成安全的目录名。"""
    cleaned = re.sub(r'[\\/:*?"<>|]', '_', name or '').strip()
    return cleaned or 'chat'


def segment_ranges(start_ts, end_ts_exclusive, segment='month'):
    """把半开区间 [start, end) 按本地时间切成月或天。

    返回 ``(start_ts, end_ts_exclusive, start_label, end_label)`` 列表，
    标签为用于文件名的日期，``end_label`` 是该段最后一天。
    """
    if segment not in SEGMENTS:
        raise ArchiveError(f'不支持的分段方式: {segment}')
    if start_ts is None or end_ts_exclusive is None or start_ts >= end_ts_exclusive:
        return []

    ranges = []
    current = datetime.fromtimestamp(start_ts)
    end_dt = datetime.fromtimestamp(end_ts_exclusive)
    while current < end_dt:
        if segment == 'day':
            boundary = datetime(current.year, current.month, current.day) + timedelta(days=1)
        elif current.month == 12:
            boundary = datetime(current.year + 1, 1, 1)
        else:
            boundary = datetime(current.year, current.month + 1, 1)
        segment_end = min(boundary, end_dt)
        ranges.append((
            int(current.timestamp()),
            int(segment_end.timestamp()),
            current.strftime('%Y-%m-%d'),
            (segment_end - timedelta(seconds=1)).strftime('%Y-%m-%d'),
        ))
        current = segment_end
    return ranges


def chat_time_range(chat_ctx):
    """返回会话在本地数据中的 ``(最早时间, 最晚时间)``，没有消息时返回 ``(None, None)``。"""
    minimum = None
    maximum = None
    for table in chat_ctx.get('message_tables') or []:
        table_name = table.get('table_name')
        if not _is_safe_msg_table_name(table_name):
            raise ArchiveError(f'非法消息表名: {table_name}')
        conn = sqlite3.connect(table['db_path'])
        try:
            row = conn.execute(
                f"SELECT MIN(create_time), MAX(create_time) FROM [{table_name}]"
            ).fetchone()
        finally:
            conn.close()
        if not row or row[0] is None:
            continue
        minimum = row[0] if minimum is None else min(minimum, row[0])
        maximum = row[1] if maximum is None else max(maximum, row[1])
    return minimum, maximum


def _write_segment(records, failures, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    count = 0
    try:
        with NamedTemporaryFile(
            mode='w', encoding='utf-8', newline='\n', dir=str(target.parent),
            prefix=f'.{target.name}.', suffix='.tmp', delete=False,
        ) as stream:
            temporary = Path(stream.name)
            for record in records:
                json.dump(record, stream, ensure_ascii=False, separators=(',', ':'))
                stream.write('\n')
                count += 1
        if failures:
            raise ArchiveError(f'读取消息失败（{len(failures)} 项）: {failures[0]}')
        if count == 0:
            return 0
        temporary.replace(target)
        return count
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def update_index(output_dir, entries):
    """把新条目合并进 ``index/archive-index.jsonl``，按会话和开始时间排序。"""
    path = Path(output_dir) / 'index' / INDEX_NAME
    existing = []
    if path.exists():
        with open(path, encoding='utf-8') as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    existing.append(json.loads(line))
                except ValueError:
                    continue

    merged = {}
    for item in existing + list(entries):
        key = (item.get('username'), item.get('start'), item.get('end'))
        merged[key] = item
    ordered = sorted(merged.values(), key=lambda item: (item.get('chat', ''), item.get('start', '')))

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode='w', encoding='utf-8', newline='\n', dir=str(path.parent),
            prefix=f'.{path.name}.', suffix='.tmp', delete=False,
        ) as stream:
            temporary = Path(stream.name)
            for item in ordered:
                json.dump(item, stream, ensure_ascii=False, separators=(',', ':'))
                stream.write('\n')
        temporary.replace(path)
    except OSError as error:
        raise ArchiveError(f'无法写入索引: {error}') from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def archive_chat(app, chat_ctx, names, display_name_fn, output_dir,
                 start_ts=None, end_ts=None, segment='month'):
    """导出会话并按段更新索引；``end_ts`` 为包含的最后一秒。"""
    output_dir = os.path.abspath(os.path.expanduser(output_dir))
    minimum, maximum = chat_time_range(chat_ctx)
    if minimum is None:
        raise ArchiveError(f"{chat_ctx['display_name']} 没有消息记录")

    start = minimum if start_ts is None else start_ts
    end_exclusive = (maximum + 1) if end_ts is None else end_ts + 1
    ranges = segment_ranges(start, end_exclusive, segment)

    folder = Path(output_dir) / 'chats' / sanitize_name(chat_ctx['display_name'])
    segments = []
    failures = []
    for seg_start, seg_end, start_label, end_label in ranges:
        segment_failures = []
        target = folder / f'{start_label}_{end_label}.jsonl'
        with closing(iter_chat_history(
            chat_ctx, names, display_name_fn, segment_failures,
            start_ts=seg_start, end_ts=seg_end - 1, structured=True,
            group_nickname_fn=getattr(app, 'group_nickname_fn', None),
        )) as records:
            count = _write_segment(records, segment_failures, target)
        if segment_failures:
            failures.extend(segment_failures)
            continue
        if not count:
            continue
        segments.append({
            'start': start_label,
            'end': end_label,
            'count': count,
            'file': str(target.relative_to(Path(output_dir))).replace('\\', '/'),
        })

    exported_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entries = [{
        'chat': chat_ctx['display_name'],
        'username': chat_ctx['username'],
        'start': item['start'],
        'end': item['end'],
        'count': item['count'],
        'file': item['file'],
        'exported_at': exported_at,
    } for item in segments]
    update_index(output_dir, entries)

    return {
        'chat': chat_ctx['display_name'],
        'username': chat_ctx['username'],
        'output_dir': output_dir,
        'segment': segment,
        'start': datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M:%S'),
        'end': datetime.fromtimestamp(end_exclusive - 1).strftime('%Y-%m-%d %H:%M:%S'),
        'total': sum(item['count'] for item in segments),
        'segments': segments,
        'failures': failures or None,
    }
