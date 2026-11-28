"""消息查询 — 分表查找、分页、格式化"""

import hashlib
import heapq
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import ExitStack, closing
from datetime import datetime
from itertools import islice

import zstandard as zstd

from .key_utils import key_path_variants

_zstd_dctx = zstd.ZstdDecompressor()
_XML_UNSAFE_RE = re.compile(r'<!DOCTYPE|<!ENTITY', re.IGNORECASE)
_XML_PARSE_MAX_LEN = 20000
_QUERY_LIMIT_MAX = 500
_MESSAGE_COLUMNS = (
    "local_id, local_type, create_time, real_sender_id, message_content, WCDB_CT_message_content"
)
_MESSAGE_COLUMNS_WITH_PACKED = f"{_MESSAGE_COLUMNS}, packed_info_data"

# 消息类型过滤映射: 名称 -> (base_type,) 或 (base_type, sub_type)
MSG_TYPE_FILTERS = {
    'text': (1,),
    'image': (3,),
    'voice': (34,),
    'video': (43,),
    'sticker': (47,),
    'location': (48,),
    'link': (49,),
    'file': (49, 6),
    'call': (50,),
    'system': (10000,),
}
MSG_TYPE_NAMES = list(MSG_TYPE_FILTERS.keys())


# ---- 消息 DB 发现 ----

def find_msg_db_keys(all_keys):
    return sorted([
        k for k in all_keys
        if any(v.startswith("message/") for v in key_path_variants(k))
        and any(re.search(r"message_\d+\.db$", v) for v in key_path_variants(k))
    ])


def _is_safe_msg_table_name(table_name):
    return bool(re.fullmatch(r'Msg_[0-9a-f]{32}', table_name))


def _table_has_column(conn, table_name, column_name):
    try:
        return any(row[1] == column_name for row in conn.execute(f"PRAGMA table_info([{table_name}])"))
    except sqlite3.Error:
        return False


def _find_msg_tables_for_user(username, msg_db_keys, cache):
    table_hash = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"
    if not _is_safe_msg_table_name(table_name):
        return []
    matches = []
    for rel_key in msg_db_keys:
        path = cache.get(rel_key)
        if not path:
            continue
        conn = sqlite3.connect(path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if not exists:
                continue
            max_ct = conn.execute(f"SELECT MAX(create_time) FROM [{table_name}]").fetchone()[0] or 0
            matches.append({'db_path': path, 'db_key': rel_key, 'table_name': table_name, 'max_create_time': max_ct})
        except Exception:
            pass
        finally:
            conn.close()
    matches.sort(key=lambda x: x['max_create_time'], reverse=True)
    return matches


# ---- 消息类型 ----

def _split_msg_type(t):
    try:
        t = int(t)
    except (TypeError, ValueError):
        return 0, 0
    if t > 0xFFFFFFFF:
        return t & 0xFFFFFFFF, t >> 32
    return t, 0


def format_msg_type(t):
    base_type, _ = _split_msg_type(t)
    return {
        1: '文本', 3: '图片', 34: '语音', 42: '名片',
        43: '视频', 47: '表情', 48: '位置', 49: '链接/文件',
        50: '通话', 10000: '系统', 10002: '撤回',
    }.get(base_type, f'type={t}')


# ---- 内容解压 ----

def decompress_content(content, ct):
    if ct and ct == 4 and isinstance(content, bytes):
        try:
            return _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
        except Exception:
            return None
    if isinstance(content, bytes):
        try:
            return content.decode('utf-8', errors='replace')
        except Exception:
            return None
    return content


# ---- 内容解析 ----

def _parse_message_content(content, local_type, is_group):
    if content is None:
        return '', ''
    if isinstance(content, bytes):
        return '', '(二进制内容)'
    sender = ''
    text = content
    if is_group and ':\n' in content:
        sender, text = content.split(':\n', 1)
    return sender, text


def _collapse_text(text):
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text).strip()


def _parse_xml_root(content):
    if not content or len(content) > _XML_PARSE_MAX_LEN or _XML_UNSAFE_RE.search(content):
        return None
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def _parse_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _plain_text_from_inner_xml(value):
    """把引用/系统消息里的内嵌 XML 摘要成可读文本，避免把标签带进结果。"""
    value = (value or '').strip()
    if not value or not value.startswith('<'):
        return value
    root = _parse_xml_root(value)
    if root is None:
        return _collapse_text(re.sub(r'<[^>]+>', ' ', value))
    title = _collapse_text(root.findtext('.//title') or '')
    description = _collapse_text(root.findtext('.//des') or '')
    if title and description and description != title:
        return f"{title} — {description}"
    if title:
        return title
    text = _collapse_text(' '.join(root.itertext()))
    return text or '[内容]'


def _parse_app_message_fields(content, is_group=False):
    """解析 appmsg 的标题、链接、摘要和类型；不是 appmsg 时返回 None。"""
    if not content or '<appmsg' not in content:
        return None
    if is_group:
        _, content = _parse_message_content(content, 0, True)
        if '<appmsg' not in content:
            return None
    root = _parse_xml_root(content)
    if root is None:
        return None
    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return None
    return {
        'title': _collapse_text(appmsg.findtext('title') or ''),
        'url': (appmsg.findtext('url') or '').strip(),
        'description': _collapse_text(appmsg.findtext('des') or ''),
        'app_type': _parse_int((appmsg.findtext('type') or '').strip()),
    }


def _extract_app_message_url(content, is_group=False):
    """从 appmsg 里取出链接地址；没有就返回空字符串。

    群消息的原始内容带有 ``发送者:\\n`` 前缀，需先剥离再做 XML 解析。
    """
    fields = _parse_app_message_fields(content, is_group)
    return fields['url'] if fields else ''


def _format_app_message_text(content, local_type, is_group, chat_username, chat_display_name, names, _display_name_fn, resolve_media=False, db_dir=None, create_time_ts=0):
    if not content or '<appmsg' not in content:
        return None
    _, sub_type = _split_msg_type(local_type)
    root = _parse_xml_root(content)
    if root is None:
        return None
    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return None
    title = _collapse_text(appmsg.findtext('title') or '')
    app_type = _parse_int((appmsg.findtext('type') or '').strip(), _parse_int(sub_type, 0))

    if app_type == 57:
        ref = appmsg.find('.//refermsg')
        ref_content = ''
        ref_display_name = ''
        if ref is not None:
            ref_display_name = (ref.findtext('displayname') or '').strip()
            ref_raw = ref.findtext('content')
            ref_node = ref.find('content')
            if not ref_raw and ref_node is not None and len(ref_node):
                ref_raw = ET.tostring(ref_node, encoding='unicode')
            ref_content = _plain_text_from_inner_xml(ref_raw or '')
            ref_content = _collapse_text(ref_content)
        if len(ref_content) > 160:
            ref_content = ref_content[:160] + "..."
        quote_text = title or "[引用消息]"
        if ref_content:
            prefix = f"回复 {ref_display_name}: " if ref_display_name else "回复: "
            quote_text += f"\n  ↳ {prefix}{ref_content}"
        return quote_text
    if app_type == 6:
        # Try to resolve file path
        if resolve_media and db_dir:
            msg_dir = os.path.join(os.path.dirname(db_dir), "msg", "file")
            if title and os.path.isdir(msg_dir):
                from datetime import datetime as _dt
                dt = _dt.fromtimestamp(create_time_ts) if create_time_ts else None
                if dt:
                    file_dir = os.path.join(msg_dir, dt.strftime("%Y-%m"))
                    if os.path.isdir(file_dir):
                        target = os.path.join(file_dir, title)
                        if os.path.isfile(target):
                            return f"[文件] {title}\n  {target}"
                        # Fuzzy match
                        for f in os.listdir(file_dir):
                            if title in f or f in title:
                                return f"[文件] {title}\n  {os.path.join(file_dir, f)}"
        return f"[文件] {title}" if title else "[文件]"
    if app_type == 5:
        return f"[链接] {title}" if title else "[链接]"
    if app_type in (33, 36, 44):
        return f"[小程序] {title}" if title else "[小程序]"
    if title:
        return f"[链接/文件] {title}"
    return "[链接/文件]"


def _format_voip_message_text(content):
    if not content or '<voip' not in content:
        return None
    root = _parse_xml_root(content)
    if root is None:
        return "[通话]"
    raw_text = _collapse_text(root.findtext('.//msg') or '')
    if not raw_text:
        return "[通话]"
    status_map = {
        'Canceled': '已取消', 'Line busy': '对方忙线',
        'Call not answered': '未接听', "Call wasn't answered": '未接听',
    }
    if raw_text.startswith('Duration:'):
        duration = raw_text.split(':', 1)[1].strip()
        return f"[通话] 通话时长 {duration}" if duration else "[通话]"
    return f"[通话] {status_map.get(raw_text, raw_text)}"


def _format_system_message_text(content):
    """把系统消息的 XML 模板转成一句可读文本，不再原样输出标签。"""
    if not content:
        return '[系统]'
    if not content.lstrip().startswith('<'):
        text = _collapse_text(content)
        return f"[系统] {text}" if text else '[系统]'
    root = _parse_xml_root(content)
    if root is None:
        text = _collapse_text(re.sub(r'<[^>]+>', ' ', content))
        return f"[系统] {text}" if text else '[系统]'

    revoke = root.find('.//revokemsg')
    if revoke is not None:
        for path in ('replacemsg', 'content'):
            value = _plain_text_from_inner_xml(revoke.findtext(path) or '')
            value = _collapse_text(value)
            if value:
                return f"[系统] {value}"
        return '[系统] 撤回了一条消息'

    for path in ('.//content', './/template', './/replacemsg'):
        value = _plain_text_from_inner_xml(root.findtext(path) or '')
        value = _collapse_text(value)
        if value:
            return f"[系统] {value}"
    text = _collapse_text(' '.join(root.itertext()))
    return f"[系统] {text}" if text else '[系统]'


def _resolve_media_path(db_dir, content, local_type, create_time_ts, chat_username=None, packed_info=None):
    """尝试解析媒体文件在磁盘上的路径。

    Args:
        db_dir: 微信 db_storage 目录
        content: 解压后的 message_content
        local_type: 消息类型
        create_time_ts: 消息时间戳
        chat_username: 聊天对象 username（用于定位 attach 子目录）
        packed_info: 图片消息的 packed_info_data，用于精确定位文件名

    Returns:
        (path, exists) 元组，path 为 None 表示无法解析
    """
    base_type = local_type & 0xFFFFFFFF
    wechat_base = os.path.dirname(db_dir)
    msg_dir = os.path.join(wechat_base, "msg")
    if not os.path.isdir(msg_dir):
        return None, False

    from datetime import datetime
    dt = datetime.fromtimestamp(create_time_ts)
    date_prefix = dt.strftime("%Y-%m")

    # 文件消息 (type 49, sub 6): msg/file/YYYY-MM/filename
    if base_type == 49 and content:
        root = _parse_xml_root(content)
        if root is not None:
            appmsg = root.find('.//appmsg')
            if appmsg is not None:
                app_type = _parse_int((appmsg.findtext('type') or '').strip())
                if app_type == 6:
                    title = (appmsg.findtext('title') or '').strip()
                    if title:
                        file_dir = os.path.join(msg_dir, "file", date_prefix)
                        if os.path.isdir(file_dir):
                            # 精确匹配文件名
                            target = os.path.join(file_dir, title)
                            if os.path.isfile(target):
                                return target, True
                            # 模糊匹配（文件名可能有细微差异）
                            for f in os.listdir(file_dir):
                                if title in f or f in title:
                                    return os.path.join(file_dir, f), True
        return None, False

    # 图片消息 (type 3): 用 packed_info_data 里的文件名精确定位。
    # 语音/视频没有稳定的文件名映射，返回 None，避免把同月任意文件当成这条消息。
    if base_type == 3:
        if not packed_info:
            return None, False
        from .attachments import attachment_files, best_kind, parse_packed_basename
        basename = parse_packed_basename(packed_info)
        if not basename:
            return None, False
        files = attachment_files(db_dir, chat_username, create_time_ts, basename)
        kind = best_kind(files)
        if not kind:
            return None, False
        return files[kind], True

    return None, False


def _format_message_text(local_id, local_type, content, is_group, chat_username, chat_display_name, names, display_name_fn, db_dir=None, create_time_ts=0, resolve_media=False, packed_info=None):
    sender, text = _parse_message_content(content, local_type, is_group)
    base_type, _ = _split_msg_type(local_type)

    media_path = None
    media_exists = False
    if resolve_media and db_dir and content:
        try:
            media_path, media_exists = _resolve_media_path(
                db_dir, content, local_type, create_time_ts, chat_username, packed_info=packed_info
            )
        except Exception:
            pass

    if base_type == 3:
        if media_path:
            tag = f"[图片] {media_path}"
            if not media_exists:
                tag += " (文件不存在)"
        elif resolve_media:
            tag = "[图片] (本地文件不可用)"
        else:
            tag = "[图片]"
        text = tag
    elif base_type == 47:
        text = "[表情]"
    elif base_type == 50:
        text = _format_voip_message_text(text) or "[通话]"
    elif base_type == 49:
        text = _format_app_message_text(
            text, local_type, is_group, chat_username, chat_display_name, names, display_name_fn,
            resolve_media=resolve_media, db_dir=db_dir, create_time_ts=create_time_ts
        ) or "[链接/文件]"
    elif base_type == 10000:
        text = _format_system_message_text(text)
    elif base_type != 1:
        type_label = format_msg_type(local_type)
        text = f"[{type_label}] {text}" if text else f"[{type_label}]"
    return sender, text


# ---- Name2Id ----

def _load_name2id_maps(conn):
    id_to_username = {}
    try:
        rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error:
        return id_to_username
    for rowid, user_name in rows:
        if not user_name:
            continue
        id_to_username[rowid] = user_name
    return id_to_username


# ---- 发送者解析 ----

def _resolve_sender_identity(real_sender_id, sender_from_content, is_group, chat_username, chat_display_name, names, id_to_username, display_name_fn):
    sender_username = id_to_username.get(real_sender_id, '')
    if not sender_username and sender_from_content:
        candidate = sender_from_content.strip()
        if candidate and len(candidate) <= 64 and ' ' not in candidate:
            sender_username = candidate
    if is_group:
        if sender_username and sender_username != chat_username:
            label = display_name_fn(sender_username, names)
        elif sender_from_content:
            label = display_name_fn(sender_from_content, names)
        else:
            label = ''
    else:
        if sender_username == chat_username:
            label = chat_display_name
        elif sender_username:
            label = display_name_fn(sender_username, names)
        else:
            label = ''
    contact_display = display_name_fn(sender_username, names) if sender_username else ''
    return {
        'sender': label,
        'sender_username': sender_username or '',
        'sender_contact_display': contact_display,
    }


# ---- SQL 查询 ----

def _build_message_filters(start_ts=None, end_ts=None, keyword='', msg_type_filter=None):
    clauses = []
    params = []
    if start_ts is not None:
        clauses.append('create_time >= ?')
        params.append(start_ts)
    if end_ts is not None:
        clauses.append('create_time <= ?')
        params.append(end_ts)
    if msg_type_filter is not None:
        base_type = msg_type_filter[0]
        clauses.append('(local_type & 0xFFFFFFFF) = ?')
        params.append(base_type)
        if len(msg_type_filter) > 1:
            clauses.append('((local_type >> 32) & 0xFFFFFFFF) = ?')
            params.append(msg_type_filter[1])
    if keyword:
        clauses.append("""
            CASE WHEN typeof(message_content) = 'blob'
                 THEN wechat_message_text(message_content, WCDB_CT_message_content)
                 ELSE message_content
            END LIKE ?
        """.strip())
        params.append(f'%{keyword}%')
    return clauses, params


def _query_messages(conn, table_name, start_ts=None, end_ts=None, keyword='', limit=20, offset=0, msg_type_filter=None, *, stream=False, columns=_MESSAGE_COLUMNS):
    if not _is_safe_msg_table_name(table_name):
        raise ValueError(f'非法消息表名: {table_name}')
    if keyword:
        conn.create_function("wechat_message_text", 2, decompress_content, deterministic=True)
    clauses, params = _build_message_filters(start_ts, end_ts, keyword, msg_type_filter)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ''
    sql = f"""
        SELECT {columns}
        FROM [{table_name}]
        {where_sql}
        ORDER BY create_time DESC, local_id DESC
    """
    if limit is not None:
        sql += "\n        LIMIT ? OFFSET ?"
        params.extend((limit, offset))
    cursor = conn.execute(sql, params)
    return cursor if stream else cursor.fetchall()


# ---- 时间解析 ----

def parse_time_value(value, field_name, is_end=False):
    value = (value or '').strip()
    if not value:
        return None
    formats = [
        ('%Y-%m-%d %H:%M:%S', False),
        ('%Y-%m-%d %H:%M', False),
        ('%Y-%m-%d', True),
    ]
    for fmt, date_only in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if date_only and is_end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"{field_name} 格式无效: {value}。支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS")


def parse_time_range(start_time='', end_time=''):
    start_ts = parse_time_value(start_time, 'start_time', is_end=False)
    end_ts = parse_time_value(end_time, 'end_time', is_end=True)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError('start_time 不能晚于 end_time')
    return start_ts, end_ts


def validate_pagination(limit, offset=0, limit_max=_QUERY_LIMIT_MAX):
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    if limit_max is not None and limit > limit_max:
        raise ValueError(f"limit 不能大于 {limit_max}")
    if offset < 0:
        raise ValueError("offset 不能小于 0")


# ---- 聊天上下文 ----

def resolve_chat_context(chat_name, msg_db_keys, cache, decrypted_dir, *, strict=True):
    from .contacts import resolve_username, get_contact_names
    username = resolve_username(chat_name, cache, decrypted_dir, strict=strict)
    if not username:
        return None
    names = get_contact_names(cache, decrypted_dir)
    display_name = names.get(username, username)
    message_tables = _find_msg_tables_for_user(username, msg_db_keys, cache)
    if not message_tables:
        return {
            'query': chat_name, 'username': username, 'display_name': display_name,
            'db_path': None, 'table_name': None, 'message_tables': [],
            'is_group': '@chatroom' in username,
        }
    primary = message_tables[0]
    return {
        'query': chat_name, 'username': username, 'display_name': display_name,
        'db_path': primary['db_path'], 'table_name': primary['table_name'],
        'message_tables': message_tables, 'is_group': '@chatroom' in username,
    }


def _iter_table_contexts(ctx):
    tables = ctx.get('message_tables') or []
    if not tables and ctx.get('db_path') and ctx.get('table_name'):
        tables = [{'db_path': ctx['db_path'], 'table_name': ctx['table_name']}]
    for table in tables:
        yield {
            'query': ctx['query'], 'username': ctx['username'], 'display_name': ctx['display_name'],
            'db_path': table['db_path'], 'table_name': table['table_name'],
            'db_key': table.get('db_key', os.path.basename(table['db_path'])),
            'is_group': ctx['is_group'],
        }


def _candidate_page_size(limit, offset):
    return limit + offset


def _page_ranked_entries(entries, limit, offset):
    ordered = sorted(entries, key=lambda item: item[0], reverse=True)
    paged = ordered[offset:offset + limit]
    paged.sort(key=lambda item: item[0])
    return paged


# ---- 构建行 ----

def _message_table_id(ctx):
    db_key = ctx.get('db_key') or os.path.basename(ctx.get('db_path') or '')
    db_key = db_key.replace('\\', '/')
    return f"{db_key}:{ctx['table_name']}"


def _build_message_record(row, ctx, names, id_to_username, display_name_fn, resolve_media=False, db_dir=None, group_nickname_fn=None):
    local_id, local_type, create_time, real_sender_id, content, ct = row[:6]
    packed_info = row[6] if len(row) > 6 else None
    content = decompress_content(content, ct)
    if content is None:
        content = '(无法解压)'
    sender, text = _format_message_text(
        local_id, local_type, content, ctx['is_group'], ctx['username'], ctx['display_name'], names, display_name_fn,
        db_dir=db_dir, create_time_ts=create_time, resolve_media=resolve_media, packed_info=packed_info,
    )
    identity = _resolve_sender_identity(
        real_sender_id, sender, ctx['is_group'], ctx['username'], ctx['display_name'], names, id_to_username, display_name_fn,
    )
    group_nickname = ''
    if ctx['is_group'] and group_nickname_fn and identity['sender_username']:
        try:
            group_nickname = group_nickname_fn(ctx['username'], identity['sender_username']) or ''
        except Exception:
            group_nickname = ''
    base_type, subtype = _split_msg_type(local_type)
    return {
        'message_id': f"{_message_table_id(ctx)}:{local_id}",
        'chat': ctx['display_name'],
        'username': ctx['username'],
        'is_group': ctx['is_group'],
        'timestamp': create_time,
        'sender': identity['sender'],
        'sender_username': identity['sender_username'],
        'sender_contact_display': identity['sender_contact_display'],
        'sender_group_nickname': group_nickname,
        'type': base_type,
        'subtype': subtype,
        'msg_type': format_msg_type(local_type),
        'text': text,
        'url': _extract_app_message_url(content, ctx['is_group']),
    }


def _format_history_line(record):
    time_str = datetime.fromtimestamp(record['timestamp']).strftime('%Y-%m-%d %H:%M')
    sender = f"{record['sender']}: " if record['sender'] else ""
    return f"[{time_str}] {sender}{record['text']}"


def _build_search_entry(row, ctx, names, id_to_username, display_name_fn, resolve_media=False, db_dir=None, structured=False, group_nickname_fn=None):
    content = decompress_content(row[4], row[5])
    if content is None:
        return None
    record = _build_message_record(
        row, ctx, names, id_to_username, display_name_fn,
        resolve_media=resolve_media, db_dir=db_dir, group_nickname_fn=group_nickname_fn,
    )
    text = record['text']
    truncated = len(text) > 300
    if truncated:
        text = text[:300] + ('…' if structured else '...')
    if structured:
        record['text'] = text
        record['text_truncated'] = truncated
        return record['timestamp'], record

    time_str = datetime.fromtimestamp(record['timestamp']).strftime('%Y-%m-%d %H:%M')
    entry = f"[{time_str}] [{record['chat']}]"
    if record['sender']:
        entry += f" {record['sender']}:"
    entry += f" {text}"
    return record['timestamp'], entry


# ---- 聊天记录查询 ----

def _iter_history_table(table_ctx, failures, start_ts, end_ts, msg_type_filter, resolve_media=False):
    try:
        with closing(sqlite3.connect(table_ctx['db_path'])) as conn:
            id_to_username = _load_name2id_maps(conn)
            columns = _MESSAGE_COLUMNS
            if resolve_media and _table_has_column(conn, table_ctx['table_name'], 'packed_info_data'):
                columns = _MESSAGE_COLUMNS_WITH_PACKED
            for row in _query_messages(
                conn, table_ctx['table_name'], start_ts=start_ts, end_ts=end_ts,
                limit=None, msg_type_filter=msg_type_filter, stream=True, columns=columns,
            ):
                if not isinstance(row[2], (int, float)) or not isinstance(row[0], int):
                    failures.append(f"local_id={row[0]}: 无效的消息标识或时间戳")
                    continue
                yield row, table_ctx, id_to_username
    except (OSError, sqlite3.Error, ValueError) as error:
        failures.append(f"{table_ctx['db_key']}: {error}")


def iter_chat_history(ctx, names, display_name_fn, failures, *, start_ts=None, end_ts=None,
                      msg_type_filter=None, resolve_media=False, db_dir=None, structured=True,
                      group_nickname_fn=None):
    """按时间从新到旧归并消息表，逐条生成消息。"""
    with ExitStack() as stack:
        streams = [
            stack.enter_context(closing(_iter_history_table(
                table_ctx, failures, start_ts, end_ts, msg_type_filter, resolve_media=resolve_media,
            )))
            for table_ctx in _iter_table_contexts(ctx)
        ]
        rows = heapq.merge(*streams, reverse=True, key=lambda item: (item[0][2], item[1]['db_key'], item[0][0]))
        for row, table_ctx, id_to_username in rows:
            try:
                record = _build_message_record(
                    row, table_ctx, names, id_to_username, display_name_fn, resolve_media=resolve_media, db_dir=db_dir,
                    group_nickname_fn=group_nickname_fn,
                )
                yield record if structured else _format_history_line(record)
            except Exception as error:
                failures.append(f"local_id={row[0]}: {error}")


def collect_chat_history(ctx, names, display_name_fn, start_ts=None, end_ts=None, limit=20, offset=0,
                         msg_type_filter=None, resolve_media=False, db_dir=None, *, structured=False,
                         group_nickname_fn=None):
    failures = []
    with closing(iter_chat_history(
        ctx, names, display_name_fn, failures, start_ts=start_ts, end_ts=end_ts,
        msg_type_filter=msg_type_filter, resolve_media=resolve_media, db_dir=db_dir, structured=structured,
        group_nickname_fn=group_nickname_fn,
    )) as records:
        page = list(islice(records, offset, offset + limit))
    page.reverse()
    return page, failures


# ---- 搜索查询 ----

def _collect_search_entries(conn, contexts, names, keyword, display_name_fn, start_ts=None, end_ts=None, candidate_limit=20, msg_type_filter=None, structured=False, group_nickname_fn=None):
    collected = []
    failures = []
    id_to_username = _load_name2id_maps(conn)
    batch_size = candidate_limit

    for ctx in contexts:
        try:
            fetch_offset = 0
            before = len(collected)
            while len(collected) - before < candidate_limit:
                rows = _query_messages(conn, ctx['table_name'], start_ts=start_ts, end_ts=end_ts, keyword=keyword, limit=batch_size, offset=fetch_offset, msg_type_filter=msg_type_filter)
                if not rows:
                    break
                fetch_offset += len(rows)
                for row in rows:
                    formatted = _build_search_entry(
                        row, ctx, names, id_to_username, display_name_fn,
                        structured=structured, group_nickname_fn=group_nickname_fn,
                    )
                    if formatted:
                        collected.append(formatted)
                        if len(collected) - before >= candidate_limit:
                            break
                if len(rows) < batch_size:
                    break
        except Exception as e:
            failures.append(f"{ctx['display_name']}: {e}")
    return collected, failures


def collect_chat_search(ctx, names, keyword, display_name_fn, start_ts=None, end_ts=None, candidate_limit=20, msg_type_filter=None, structured=False, group_nickname_fn=None):
    collected = []
    failures = []
    contexts_by_db = {}
    for table_ctx in _iter_table_contexts(ctx):
        contexts_by_db.setdefault(table_ctx['db_path'], []).append(table_ctx)

    for db_path, db_contexts in contexts_by_db.items():
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                db_entries, db_failures = _collect_search_entries(
                    conn, db_contexts, names, keyword, display_name_fn,
                    start_ts=start_ts, end_ts=end_ts, candidate_limit=candidate_limit,
                    msg_type_filter=msg_type_filter, structured=structured,
                    group_nickname_fn=group_nickname_fn,
                )
                collected.extend(db_entries)
                failures.extend(db_failures)
        except Exception as e:
            failures.extend(f"{tc['display_name']}: {e}" for tc in db_contexts)
    return collected, failures


def search_all_messages(msg_db_keys, cache, names, keyword, display_name_fn, start_ts=None, end_ts=None, candidate_limit=20, msg_type_filter=None, structured=False, group_nickname_fn=None):
    collected = []
    failures = []
    for rel_key in msg_db_keys:
        path = cache.get(rel_key)
        if not path:
            continue
        try:
            with closing(sqlite3.connect(path)) as conn:
                contexts = _load_message_table_contexts(conn, path, rel_key, names)
                db_entries, db_failures = _collect_search_entries(
                    conn, contexts, names, keyword, display_name_fn,
                    start_ts=start_ts, end_ts=end_ts, candidate_limit=candidate_limit,
                    msg_type_filter=msg_type_filter, structured=structured,
                    group_nickname_fn=group_nickname_fn,
                )
                collected.extend(db_entries)
                failures.extend(db_failures)
        except Exception as e:
            failures.append(f"{rel_key}: {e}")
    return collected, failures


def _load_message_table_contexts(conn, db_path, db_key, names, id_to_username=None):
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*' ORDER BY name"
    )
    if id_to_username is None:
        id_to_username = _load_name2id_maps(conn)
    table_to_username = {
        f"Msg_{hashlib.md5(username.encode()).hexdigest()}": username
        for username in id_to_username.values()
    }
    contexts = []
    for (table_name,) in tables:
        if not _is_safe_msg_table_name(table_name):
            continue
        username = table_to_username.get(table_name, '')
        display_name = names.get(username, username) if username else table_name
        contexts.append({
            'query': display_name, 'username': username, 'display_name': display_name,
            'db_path': db_path, 'db_key': db_key, 'table_name': table_name, 'is_group': '@chatroom' in username,
        })
    return contexts


# ---- 链接/文章提取 ----

LINK_APP_TYPE = 5


def _link_entry_from_row(row, ctx, names, id_to_username, display_name_fn, group_nickname_fn=None):
    content = decompress_content(row[4], row[5])
    fields = _parse_app_message_fields(content, ctx['is_group'])
    if not fields or fields['app_type'] != LINK_APP_TYPE or not fields['url']:
        return None
    record = _build_message_record(
        row, ctx, names, id_to_username, display_name_fn,
        group_nickname_fn=group_nickname_fn,
    )
    return {
        'title': fields['title'],
        'url': fields['url'],
        'description': fields['description'],
        'app_type': fields['app_type'],
        'chat': record['chat'],
        'username': record['username'],
        'is_group': record['is_group'],
        'sender': record['sender'],
        'sender_username': record['sender_username'],
        'sender_contact_display': record['sender_contact_display'],
        'sender_group_nickname': record['sender_group_nickname'],
        'timestamp': record['timestamp'],
        'time': datetime.fromtimestamp(record['timestamp']).strftime('%Y-%m-%d %H:%M'),
        'message_id': record['message_id'],
    }


def _collect_link_entries(conn, contexts, names, display_name_fn, start_ts=None, end_ts=None, group_nickname_fn=None):
    entries = []
    failures = []
    id_to_username = _load_name2id_maps(conn)
    for ctx in contexts:
        clauses = ['(local_type & 0xFFFFFFFF) = 49']
        params = []
        if start_ts is not None:
            clauses.append('create_time >= ?')
            params.append(start_ts)
        if end_ts is not None:
            clauses.append('create_time <= ?')
            params.append(end_ts)
        sql = (
            f"SELECT {_MESSAGE_COLUMNS} FROM [{ctx['table_name']}] "
            f"WHERE {' AND '.join(clauses)} ORDER BY create_time DESC"
        )
        try:
            for row in conn.execute(sql, params):
                try:
                    entry = _link_entry_from_row(
                        row, ctx, names, id_to_username, display_name_fn,
                        group_nickname_fn=group_nickname_fn,
                    )
                except Exception as error:
                    failures.append(f"{ctx['display_name']}: local_id={row[0]}: {error}")
                    continue
                if entry:
                    entries.append(entry)
        except (sqlite3.Error, OSError, ValueError) as error:
            failures.append(f"{ctx['display_name']}: {error}")
    return entries, failures


def collect_chat_links(ctx, names, display_name_fn, start_ts=None, end_ts=None, group_nickname_fn=None):
    entries = []
    failures = []
    contexts_by_db = {}
    for table_ctx in _iter_table_contexts(ctx):
        contexts_by_db.setdefault(table_ctx['db_path'], []).append(table_ctx)
    for db_path, db_contexts in contexts_by_db.items():
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                db_entries, db_failures = _collect_link_entries(
                    conn, db_contexts, names, display_name_fn,
                    start_ts=start_ts, end_ts=end_ts, group_nickname_fn=group_nickname_fn,
                )
                entries.extend(db_entries)
                failures.extend(db_failures)
        except (sqlite3.Error, OSError) as error:
            failures.extend(f"{tc['display_name']}: {error}" for tc in db_contexts)
    return entries, failures


def collect_all_links(msg_db_keys, cache, names, display_name_fn, start_ts=None, end_ts=None, group_nickname_fn=None):
    entries = []
    failures = []
    for rel_key in msg_db_keys:
        path = cache.get(rel_key)
        if not path:
            continue
        try:
            with closing(sqlite3.connect(path)) as conn:
                contexts = _load_message_table_contexts(conn, path, rel_key, names)
                db_entries, db_failures = _collect_link_entries(
                    conn, contexts, names, display_name_fn,
                    start_ts=start_ts, end_ts=end_ts, group_nickname_fn=group_nickname_fn,
                )
                entries.extend(db_entries)
                failures.extend(db_failures)
        except (sqlite3.Error, OSError) as error:
            failures.append(f"{rel_key}: {error}")
    return entries, failures


def merge_link_entries(entries):
    """按 URL 去重，保留最近一次分享，并记录分享次数和首次分享时间。"""
    merged = {}
    for entry in entries:
        url = entry.get('url') or ''
        if not url:
            continue
        current = merged.get(url)
        if current is None:
            item = dict(entry)
            item['share_count'] = 1
            item['first_shared_at'] = item['timestamp']
            item['first_shared_time'] = datetime.fromtimestamp(item['timestamp']).strftime('%Y-%m-%d %H:%M')
            merged[url] = item
            continue

        current['share_count'] += 1
        if entry['timestamp'] < current['first_shared_at']:
            current['first_shared_at'] = entry['timestamp']
            current['first_shared_time'] = datetime.fromtimestamp(entry['timestamp']).strftime('%Y-%m-%d %H:%M')
        if entry['timestamp'] >= current['timestamp']:
            count = current['share_count']
            first_shared_at = current['first_shared_at']
            first_shared_time = current['first_shared_time']
            latest = dict(entry)
            latest['share_count'] = count
            latest['first_shared_at'] = first_shared_at
            latest['first_shared_time'] = first_shared_time
            merged[url] = latest
    return sorted(merged.values(), key=lambda item: item['timestamp'], reverse=True)


def collect_new_messages(msg_db_keys, cache, names, display_name_fn, previous, legacy_timestamps=None, group_nickname_fn=None):
    """记录每个分库消息表的 local_id，按实际消息读取新增行。"""
    checkpoints = dict(previous or {})
    baseline = previous is None and legacy_timestamps is None
    legacy_cutoff = max(legacy_timestamps.values(), default=0) if legacy_timestamps is not None else 0
    messages = []
    opened_any = False
    for db_key in msg_db_keys:
        db_path = cache.get(db_key)
        if not db_path:
            continue
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("BEGIN")
            opened_any = True
            id_to_username = _load_name2id_maps(conn)
            for ctx in _load_message_table_contexts(conn, db_path, db_key, names, id_to_username):
                table = ctx['table_name']
                table_id = _message_table_id(ctx)
                latest_id = conn.execute(f"SELECT MAX(local_id) FROM [{table}]").fetchone()[0]
                previous_id = checkpoints.get(table_id)
                checkpoints[table_id] = latest_id
                if baseline or latest_id is None or latest_id == previous_id:
                    continue

                if previous_id is not None and latest_id > previous_id:
                    where, params = "WHERE local_id > ?", (previous_id,)
                elif legacy_timestamps is not None:
                    # 旧版只记秒级时间戳，包含边界秒以免漏掉同秒消息。
                    where = "WHERE create_time >= ?"
                    params = (legacy_timestamps.get(ctx['username'], legacy_cutoff),)
                else:
                    # 新表或 local_id 已重置，逐条返回当前记录。
                    where, params = "", ()

                rows = conn.execute(
                    f"SELECT {_MESSAGE_COLUMNS} FROM [{table}] {where} ORDER BY local_id",
                    params,
                )
                for row in rows:
                    messages.append(_build_message_record(
                        row, ctx, names, id_to_username, display_name_fn,
                        group_nickname_fn=group_nickname_fn,
                    ))

    if not opened_any:
        raise sqlite3.DatabaseError("没有可读取的消息数据库")
    messages.sort(key=lambda message: message['timestamp'])
    return messages, checkpoints


# ---- 多聊天上下文解析 ----

def resolve_chat_contexts(chat_names, msg_db_keys, cache, decrypted_dir, *, strict=True):
    resolved = []
    unresolved = []
    missing_tables = []
    seen = set()
    for chat_name in chat_names:
        name = (chat_name or '').strip()
        if not name:
            unresolved.append('(空)')
            continue
        ctx = resolve_chat_context(name, msg_db_keys, cache, decrypted_dir, strict=strict)
        if not ctx:
            unresolved.append(name)
            continue
        if not ctx['message_tables']:
            missing_tables.append(ctx['display_name'])
            continue
        if ctx['username'] in seen:
            continue
        seen.add(ctx['username'])
        resolved.append(ctx)
    return resolved, unresolved, missing_tables


# ---- 聊天统计 ----

def collect_chat_stats(ctx, names, display_name_fn, start_ts=None, end_ts=None):
    """聚合统计指定聊天的消息数据。

    返回: {
        total, type_breakdown: {type_name: count},
        top_senders: [{name, count}],
        hourly: {0:N, ..., 23:N}
    }
    """
    type_map = {
        1: '文本', 3: '图片', 34: '语音', 42: '名片',
        43: '视频', 47: '表情', 48: '位置', 49: '链接/文件',
        50: '通话', 10000: '系统', 10002: '撤回',
    }

    total = 0
    type_counts = {}
    sender_counts = {}
    hourly_counts = {}

    for table_ctx in _iter_table_contexts(ctx):
        try:
            with closing(sqlite3.connect(table_ctx['db_path'])) as conn:
                id_to_username = _load_name2id_maps(conn)
                tbl = table_ctx['table_name']
                if not _is_safe_msg_table_name(tbl):
                    continue

                where_parts = []
                params = []
                if start_ts is not None:
                    where_parts.append('create_time >= ?')
                    params.append(start_ts)
                if end_ts is not None:
                    where_parts.append('create_time <= ?')
                    params.append(end_ts)
                where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ''

                # 总数 + 类型分布
                for bt, cnt in conn.execute(
                    f"SELECT (local_type & 0xFFFFFFFF), COUNT(*) FROM [{tbl}] {where_sql} GROUP BY (local_type & 0xFFFFFFFF)",
                    params
                ).fetchall():
                    label = type_map.get(bt, f'type={bt}')
                    type_counts[label] = type_counts.get(label, 0) + cnt
                    total += cnt

                # 发送者排名
                for sid, cnt in conn.execute(
                    f"SELECT real_sender_id, COUNT(*) FROM [{tbl}] {where_sql} GROUP BY real_sender_id ORDER BY COUNT(*) DESC LIMIT 20",
                    params
                ).fetchall():
                    uname = id_to_username.get(sid, str(sid))
                    if uname:
                        sender_counts[uname] = sender_counts.get(uname, 0) + cnt

                # 24小时分布
                for h, cnt in conn.execute(
                    f"SELECT cast(strftime('%H', create_time, 'unixepoch', 'localtime') as integer), COUNT(*) FROM [{tbl}] {where_sql} GROUP BY cast(strftime('%H', create_time, 'unixepoch', 'localtime') as integer)",
                    params
                ).fetchall():
                    if h is not None:
                        hourly_counts[h] = hourly_counts.get(h, 0) + cnt
        except Exception:
            pass

    top_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    top_senders = [{'name': display_name_fn(u, names), 'count': c} for u, c in top_senders]

    hourly = {h: hourly_counts.get(h, 0) for h in range(24)}

    return {
        'total': total,
        'type_breakdown': dict(sorted(type_counts.items(), key=lambda x: x[1], reverse=True)),
        'top_senders': top_senders,
        'hourly': hourly,
    }
