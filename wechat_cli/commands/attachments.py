"""attachments / extract 命令 — 图片附件列表与解密导出。"""

import os
import sqlite3
import struct
from datetime import datetime

import click

from ..core.attachments import (
    IMAGE_KINDS,
    V1_MAGIC,
    V2_MAGIC,
    V2KeyError,
    AttachmentError,
    attachment_files,
    best_kind,
    convert_image_with_ffmpeg,
    decode_dat,
    derive_v2_keys,
    infer_output_format,
    parse_image_metadata,
    parse_packed_basename,
)
from ..core.contacts import get_contact_names
from ..core.messages import (
    _build_message_record,
    _is_safe_msg_table_name,
    _load_name2id_maps,
    _parse_message_content,
    decompress_content,
    resolve_chat_context,
    validate_pagination,
)
from ..output.formatter import output


def _human_size(size):
    if size is None:
        return '-'
    if size < 1024:
        return f'{size} B'
    if size < 1024 * 1024:
        return f'{size / 1024:.1f} KB'
    return f'{size / (1024 * 1024):.1f} MB'


def _table_ctx(chat_ctx, table):
    return {
        'query': chat_ctx['query'],
        'username': chat_ctx['username'],
        'display_name': chat_ctx['display_name'],
        'db_path': table.get('db_path'),
        'db_key': table.get('db_key'),
        'table_name': table['table_name'],
        'is_group': chat_ctx['is_group'],
    }


def _collect_attachments(app, chat_ctx, names, limit, offset):
    results = []
    failures = []
    for table in chat_ctx['message_tables']:
        try:
            conn = sqlite3.connect(table['db_path'])
            try:
                id_to_username = _load_name2id_maps(conn)
                rows = conn.execute(
                    f"SELECT local_id, local_type, create_time, real_sender_id, "
                    f"message_content, WCDB_CT_message_content, packed_info_data "
                    f"FROM [{table['table_name']}] "
                    f"WHERE (local_type & 0xFFFFFFFF) = 3 "
                    f"ORDER BY create_time DESC, local_id DESC LIMIT ?",
                    (offset + limit,),
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError) as error:
            failures.append(f"{table.get('db_key')}: {error}")
            continue

        table_ctx = _table_ctx(chat_ctx, table)
        for row in rows:
            content = decompress_content(row[4], row[5])
            if isinstance(content, str) and chat_ctx['is_group']:
                _, content = _parse_message_content(content, row[1], True)
            basename = parse_packed_basename(row[6])
            if not basename:
                continue

            try:
                record = _build_message_record(
                    row[:6], table_ctx, names, id_to_username, app.display_name_fn,
                    group_nickname_fn=getattr(app, 'group_nickname_fn', None),
                )
            except Exception as error:
                failures.append(f"local_id={row[0]}: {error}")
                continue

            files = attachment_files(app.db_dir, chat_ctx['username'], row[2], basename)
            kind = best_kind(files)
            if not kind:
                continue
            meta = parse_image_metadata(content) if content else {}
            results.append({
                'attachment_id': f"{chat_ctx['username']}|{record['message_id']}|{kind}",
                'kind': kind,
                'timestamp': record['timestamp'],
                'time': datetime.fromtimestamp(record['timestamp']).strftime('%Y-%m-%d %H:%M'),
                'sender': record['sender'],
                'sender_username': record['sender_username'],
                'sender_contact_display': record['sender_contact_display'],
                'sender_group_nickname': record['sender_group_nickname'],
                'msg_id': record['message_id'],
                'size': os.path.getsize(files[kind]),
                'available_kinds': [k for k in IMAGE_KINDS if files.get(k)],
                'md5': meta.get('md5', ''),
            })

    results.sort(key=lambda item: (item['timestamp'], item['msg_id']), reverse=True)
    return results[offset:offset + limit], failures


@click.command('attachments')
@click.argument('chat_name')
@click.option('--limit', default=20, help='返回的附件数量')
@click.option('--structured', is_flag=True, help='返回结构化对象（默认即是）')
@click.option('--format', 'fmt', default='json', type=click.Choice(['json', 'text']), help='输出格式')
@click.pass_context
def attachments(ctx, chat_name, limit, structured, fmt):
    """列出会话中的本地图片附件

    \b
    示例:
      wechat-cli attachments "AI 编程互助会 07 群" --limit 10 --structured
      wechat-cli attachments "张三" --format text
    """
    app = ctx.obj
    try:
        validate_pagination(limit, 0, limit_max=None)
    except ValueError as error:
        click.echo(f'错误: {error}', err=True)
        ctx.exit(2)

    chat_ctx = resolve_chat_context(chat_name, app.msg_db_keys, app.cache, app.decrypted_dir)
    if not chat_ctx:
        click.echo(f'找不到聊天对象: {chat_name}', err=True)
        ctx.exit(1)
    if not chat_ctx['db_path']:
        click.echo(f'找不到 {chat_ctx["display_name"]} 的消息记录', err=True)
        ctx.exit(1)

    names = get_contact_names(app.cache, app.decrypted_dir)
    items, failures = _collect_attachments(app, chat_ctx, names, limit, 0)

    payload = {
        'chat': chat_ctx['display_name'],
        'username': chat_ctx['username'],
        'is_group': chat_ctx['is_group'],
        'count': len(items),
        'attachments': items,
        'failures': failures if failures else None,
    }
    if fmt == 'json':
        output(payload, 'json')
        return

    if not items:
        output('没有找到本地可用的图片附件', 'text')
        return
    lines = []
    for item in items:
        identity = item['sender_group_nickname'] or item['sender'] or item['sender_username']
        lines.append(
            f"[{item['time']}] {item['attachment_id']}\n"
            f"  {item['kind']} · {_human_size(item['size'])} · {identity}"
        )
    output(f"{chat_ctx['display_name']} 的图片附件（{len(items)} 个）:\n\n" + "\n\n".join(lines), 'text')


def _parse_attachment_id(attachment_id):
    try:
        chat_username, msg_id, kind = attachment_id.rsplit('|', 2)
        db_key, table_name, local_id = msg_id.rsplit(':', 2)
        local_id = int(local_id)
    except (ValueError, AttributeError):
        raise AttachmentError('attachment_id 格式不正确，请先用 attachments 命令获取')
    if kind not in IMAGE_KINDS:
        raise AttachmentError(f'未知的附件类型: {kind}')
    if not chat_username or not db_key or not _is_safe_msg_table_name(table_name):
        raise AttachmentError('attachment_id 格式不正确，请先用 attachments 命令获取')
    return chat_username, db_key, table_name, local_id, kind


def _load_attachment_row(app, db_key, table_name, local_id):
    db_path = app.cache.get(db_key)
    if not db_path:
        raise AttachmentError(f'无法解密消息数据库: {db_key}')
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            f"SELECT local_id, create_time, packed_info_data FROM [{table_name}] WHERE local_id = ?",
            (local_id,),
        ).fetchone()
    finally:
        conn.close()


def _decode_for_attachment(app, data):
    if data.startswith(V2_MAGIC):
        keys = derive_v2_keys(app.db_dir)
        return decode_dat(data, aes_key=keys['aes_key'], xor_key=keys['xor_key'])
    if data.startswith(V1_MAGIC):
        xor_key = None
        if len(data) >= 15:
            xor_size = struct.unpack_from('<LL', data, 6)[1]
            if xor_size:
                xor_key = derive_v2_keys(app.db_dir)['xor_key']
        return decode_dat(data, xor_key=xor_key)
    return decode_dat(data)


@click.command('extract')
@click.argument('attachment_id')
@click.option('--output', 'output_path', required=True, help='输出文件路径')
@click.option('--convert', type=click.Choice(['jpg', 'jpeg', 'png']), default=None,
              help='用 ffmpeg 转成指定格式；输出路径为 .jpg/.png 时自动转')
@click.option('--format', 'fmt', default='json', type=click.Choice(['json', 'text']), help='输出格式')
@click.pass_context
def extract(ctx, attachment_id, output_path, convert, fmt):
    """解密单个图片附件并写入文件

    \b
    示例:
      wechat-cli extract "wxid_xxx|message/message_0.db:Msg_xxx:42|image" --output out.jpg
      wechat-cli extract "<attachment_id>" --output out.wxgf --convert jpg
    """
    app = ctx.obj
    try:
        chat_username, db_key, table_name, local_id, kind = _parse_attachment_id(attachment_id)
        row = _load_attachment_row(app, db_key, table_name, local_id)
        if not row:
            raise AttachmentError(f'找不到消息: local_id={local_id}')
        _, create_time, packed = row
        basename = parse_packed_basename(packed)
        if not basename:
            raise AttachmentError('这条消息没有本地附件文件名')
        files = attachment_files(app.db_dir, chat_username, create_time, basename)
        path = files.get(kind)
        if not path:
            available = '、'.join(k for k in IMAGE_KINDS if files.get(k)) or '无'
            raise AttachmentError(f'本地没有 {kind} 文件（可用: {available}）')

        with open(path, 'rb') as handle:
            data = handle.read()
        out_bytes, image_format, decoder = _decode_for_attachment(app, data)

        source_format = image_format
        target_format = infer_output_format(output_path, image_format, convert)
        if target_format:
            out_bytes = convert_image_with_ffmpeg(out_bytes, image_format, target_format)
            image_format = target_format
            decoder = f'{decoder}+ffmpeg'
    except V2KeyError as error:
        click.echo(f'错误: {error}', err=True)
        ctx.exit(4)
    except (AttachmentError, OSError, sqlite3.Error) as error:
        click.echo(f'错误: {error}', err=True)
        ctx.exit(3)

    output_path = os.path.abspath(output_path)
    try:
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(output_path, 'wb') as handle:
            handle.write(out_bytes)
    except OSError as error:
        click.echo(f'错误: 无法写入 {output_path}: {error}', err=True)
        ctx.exit(3)

    result = {
        'attachment_id': attachment_id,
        'kind': kind,
        'output': output_path,
        'format': image_format,
        'size': len(out_bytes),
        'decoder': decoder,
    }
    if source_format != image_format:
        result['source_format'] = source_format
    if fmt == 'json':
        output(result, 'json')
    else:
        format_label = image_format
        if source_format != image_format:
            format_label += f'（源 {source_format}）'
        output(
            f"已解密 {kind} -> {output_path}\n"
            f"格式: {format_label} · 大小: {_human_size(len(out_bytes))} · 解码器: {decoder}",
            'text',
        )
