"""联系人管理 — 加载、缓存、模糊匹配"""

import os
import re
import sqlite3


_contact_names = None  # {username: display_name}
_contact_full = None   # [{username, nick_name, remark}]
_self_username = None


def _load_contacts_from(db_path):
    names = {}
    full = []
    conn = sqlite3.connect(db_path)
    try:
        for r in conn.execute("SELECT username, nick_name, remark FROM contact").fetchall():
            uname, nick, remark = r
            display = remark if remark else nick if nick else uname
            names[uname] = display
            full.append({'username': uname, 'nick_name': nick or '', 'remark': remark or ''})
    finally:
        conn.close()
    return names, full


def get_contact_names(cache, decrypted_dir):
    global _contact_names, _contact_full
    if _contact_names is not None:
        return _contact_names

    pre_decrypted = os.path.join(decrypted_dir, "contact", "contact.db")
    if os.path.exists(pre_decrypted):
        try:
            _contact_names, _contact_full = _load_contacts_from(pre_decrypted)
            return _contact_names
        except Exception:
            pass

    path = cache.get(os.path.join("contact", "contact.db"))
    if path:
        try:
            _contact_names, _contact_full = _load_contacts_from(path)
            return _contact_names
        except Exception:
            pass

    return {}


def get_contact_full(cache, decrypted_dir):
    global _contact_full
    if _contact_full is None:
        get_contact_names(cache, decrypted_dir)
    return _contact_full or []


class AmbiguousUsernameError(ValueError):
    """一个名称匹配到多个联系人或群聊。"""

    def __init__(self, query, candidates):
        self.query = query
        self.candidates = candidates
        names = '、'.join(item['display_name'] for item in candidates[:5])
        super().__init__(f"{query} 匹配到多个聊天对象: {names}")


def resolve_username(chat_name, cache, decrypted_dir, *, strict=False):
    names = get_contact_names(cache, decrypted_dir)
    if chat_name in names or chat_name.startswith('wxid_') or '@chatroom' in chat_name:
        return chat_name
    chat_lower = chat_name.lower()

    def candidates_for(matches):
        return sorted(
            ({'username': username, 'display_name': names.get(username, username)} for username in matches),
            key=lambda item: item['display_name'],
        )

    exact = [username for username, display in names.items() if display.lower() == chat_lower]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        if strict:
            raise AmbiguousUsernameError(chat_name, candidates_for(exact))
        return exact[0]

    partial = [username for username, display in names.items() if chat_lower in display.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        if strict:
            raise AmbiguousUsernameError(chat_name, candidates_for(partial))
        return partial[0]
    return None


def get_self_username(db_dir, cache, decrypted_dir):
    global _self_username
    if _self_username:
        return _self_username
    if not db_dir:
        return ''
    names = get_contact_names(cache, decrypted_dir)
    account_dir = os.path.basename(os.path.dirname(db_dir))
    candidates = [account_dir]
    m = re.fullmatch(r'(.+)_([0-9a-fA-F]{4,})', account_dir)
    if m:
        candidates.insert(0, m.group(1))
    for candidate in candidates:
        if candidate and candidate in names:
            _self_username = candidate
            return _self_username
    return ''


def get_group_members(chatroom_username, cache, decrypted_dir):
    """获取群聊成员列表。

    通过 contact.db 的 chatroom_member 关联表查询。

    Returns:
        dict: {'members': [...], 'owner': str}
        每个 member: {'username': ..., 'nick_name': ..., 'remark': ..., 'display_name': ...}
    """
    pre_decrypted = os.path.join(decrypted_dir, "contact", "contact.db")
    if os.path.exists(pre_decrypted):
        db_path = pre_decrypted
    else:
        db_path = cache.get(os.path.join("contact", "contact.db"))

    if not db_path:
        return {'members': [], 'owner': ''}

    names = get_contact_names(cache, decrypted_dir)
    conn = sqlite3.connect(db_path)
    try:
        # 1. 找到 chatroom 的 contact.id
        row = conn.execute("SELECT id FROM contact WHERE username = ?", (chatroom_username,)).fetchone()
        if not row:
            return {'members': [], 'owner': ''}
        room_id = row[0]

        # 2. 获取群主
        owner = ''
        owner_row = conn.execute("SELECT owner FROM chat_room WHERE id = ?", (room_id,)).fetchone()
        if owner_row and owner_row[0]:
            owner = names.get(owner_row[0], owner_row[0])

        # 3. 获取成员 ID 列表
        member_ids = [r[0] for r in conn.execute(
            "SELECT member_id FROM chatroom_member WHERE room_id = ?", (room_id,)
        ).fetchall()]
        if not member_ids:
            return {'members': [], 'owner': owner}

        # 4. 批量查询成员信息
        placeholders = ','.join('?' * len(member_ids))
        members = []
        for uid, username, nick, remark in conn.execute(
            f"SELECT id, username, nick_name, remark FROM contact WHERE id IN ({placeholders})",
            member_ids
        ):
            display = remark if remark else nick if nick else username
            members.append({
                'username': username,
                'nick_name': nick or '',
                'remark': remark or '',
                'display_name': display,
            })

        # 按 display_name 排序，群主排最前
        members.sort(key=lambda m: (0 if m['username'] == (owner_row[0] if owner_row else '') else 1, m['display_name']))

        return {'members': members, 'owner': owner}
    finally:
        conn.close()


def get_contact_detail(username, cache, decrypted_dir):
    """获取联系人详情。

    Returns:
        dict or None: 联系人详细信息
    """
    pre_decrypted = os.path.join(decrypted_dir, "contact", "contact.db")
    if os.path.exists(pre_decrypted):
        db_path = pre_decrypted
    else:
        db_path = cache.get(os.path.join("contact", "contact.db"))
    if not db_path:
        return None

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT username, nick_name, remark, alias, description, "
            "small_head_url, big_head_url, verify_flag, local_type "
            "FROM contact WHERE username = ?",
            (username,)
        ).fetchone()
        if not row:
            return None
        uname, nick, remark, alias, desc, small_url, big_url, verify, ltype = row
        return {
            'username': uname,
            'nick_name': nick or '',
            'remark': remark or '',
            'alias': alias or '',
            'description': desc or '',
            'avatar': small_url or big_url or '',
            'verify_flag': verify or 0,
            'local_type': ltype,
            'is_group': '@chatroom' in uname,
            'is_subscription': uname.startswith('gh_'),
        }
    finally:
        conn.close()


def display_name_for_username(username, names, db_dir, cache, decrypted_dir):
    if not username:
        return ''
    if username == get_self_username(db_dir, cache, decrypted_dir):
        return 'me'
    return names.get(username, username)


# ---- 群昵称 ----

_group_nicknames = {}  # {room_username: {member_username: nickname}}


def _contact_db_path(cache, decrypted_dir):
    pre_decrypted = os.path.join(decrypted_dir, "contact", "contact.db")
    if os.path.exists(pre_decrypted):
        return pre_decrypted
    return cache.get(os.path.join("contact", "contact.db"))


def _read_varint(data, offset):
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _read_length_delimited(data, offset):
    length, offset = _read_varint(data, offset)
    end = offset + length
    if end > len(data):
        raise ValueError("truncated field")
    return data[offset:end], end


def _parse_group_member_entry(chunk):
    offset = 0
    username = ''
    nickname = ''
    while offset < len(chunk):
        tag, offset = _read_varint(chunk, offset)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            _, offset = _read_varint(chunk, offset)
        elif wire == 2:
            value, offset = _read_length_delimited(chunk, offset)
            if field == 1:
                username = value.decode('utf-8', errors='replace')
            elif field == 2:
                nickname = value.decode('utf-8', errors='replace')
        elif wire == 5:
            offset += 4
        elif wire == 1:
            offset += 8
        else:
            return None
    if not username:
        return None
    return username, nickname


def _parse_group_members(blob):
    """解析 chat_room.ext_buffer_ 里的群成员昵称。

    结构是 protobuf 的 repeated 成员条目（字段 1），每条含
    username（字段 1）和 nickname（字段 2）。解析失败时返回已读到的部分。
    """
    if not blob:
        return {}
    members = {}
    offset = 0
    try:
        while offset < len(blob):
            tag, offset = _read_varint(blob, offset)
            field, wire = tag >> 3, tag & 7
            if wire == 0:
                _, offset = _read_varint(blob, offset)
            elif wire == 2:
                value, offset = _read_length_delimited(blob, offset)
                if field == 1:
                    entry = _parse_group_member_entry(value)
                    if entry:
                        members[entry[0]] = entry[1]
            elif wire == 5:
                offset += 4
            elif wire == 1:
                offset += 8
            else:
                break
    except (ValueError, IndexError):
        pass
    return members


def get_group_nickname_map(chatroom_username, cache, decrypted_dir):
    """返回某个群的 {成员 username: 群昵称}，失败时返回空字典。"""
    if not chatroom_username:
        return {}
    if chatroom_username in _group_nicknames:
        return _group_nicknames[chatroom_username]

    db_path = _contact_db_path(cache, decrypted_dir)
    if not db_path:
        _group_nicknames[chatroom_username] = {}
        return {}

    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT ext_buffer FROM chat_room WHERE username = ?",
                (chatroom_username,),
            ).fetchone()
        finally:
            conn.close()
        members = _parse_group_members(row[0] if row and row[0] else b'')
    except (sqlite3.Error, OSError, ValueError):
        members = {}

    _group_nicknames[chatroom_username] = members
    return members


def get_group_nickname(chatroom_username, member_username, cache, decrypted_dir):
    if not chatroom_username or not member_username:
        return ''
    return get_group_nickname_map(chatroom_username, cache, decrypted_dir).get(member_username, '')


def reset_contact_cache():
    """清空进程内联系人缓存，供测试和长驻进程在数据变化后重载。"""
    global _contact_names, _contact_full, _self_username
    _contact_names = None
    _contact_full = None
    _self_username = None
    _group_nicknames.clear()
