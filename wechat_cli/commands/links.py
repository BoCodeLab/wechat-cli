"""links 命令 — 跨会话提取公众号文章和网页链接。"""

import click

from ..core.contacts import AmbiguousUsernameError, get_contact_names
from ..core.meta import build_meta
from ..core.messages import (
    collect_all_links,
    collect_chat_links,
    merge_link_entries,
    parse_time_range,
    resolve_chat_contexts,
    validate_pagination,
)
from ..output.formatter import output
from .common import exit_ambiguous


@click.command('links')
@click.option('--chat', multiple=True, help='限定聊天对象（可多次指定）')
@click.option('--start-time', default='', help='起始时间 YYYY-MM-DD [HH:MM[:SS]]')
@click.option('--end-time', default='', help='结束时间 YYYY-MM-DD [HH:MM[:SS]]')
@click.option('--limit', default=50, help='返回数量（最大500）')
@click.option('--format', 'fmt', default='json', type=click.Choice(['json', 'text']), help='输出格式')
@click.pass_context
def links(ctx, chat, start_time, end_time, limit, fmt):
    """提取公众号文章和网页链接，按 URL 去重

    \b
    示例:
      wechat-cli links --start-time "2026-11-27" --limit 20
      wechat-cli links --chat "AI 编程互助会 07 群" --start-time "2026-11-01"
      wechat-cli links --format text
    """
    app = ctx.obj
    try:
        validate_pagination(limit, 0)
        start_ts, end_ts = parse_time_range(start_time, end_time)
    except ValueError as error:
        click.echo(f'错误: {error}', err=True)
        ctx.exit(2)

    names = get_contact_names(app.cache, app.decrypted_dir)
    group_nickname_fn = getattr(app, 'group_nickname_fn', None)
    chat_names = list(chat)

    if chat_names:
        try:
            resolved, unresolved, missing = resolve_chat_contexts(
                chat_names, app.msg_db_keys, app.cache, app.decrypted_dir,
            )
        except AmbiguousUsernameError as error:
            exit_ambiguous(ctx, error)
        entries = []
        failures = []
        for chat_ctx in resolved:
            found, chat_failures = collect_chat_links(
                chat_ctx, names, app.display_name_fn,
                start_ts=start_ts, end_ts=end_ts, group_nickname_fn=group_nickname_fn,
            )
            entries.extend(found)
            failures.extend(chat_failures)
        if unresolved:
            failures.append('未找到: ' + '、'.join(unresolved))
        if missing:
            failures.append('没有消息记录: ' + '、'.join(missing))
        scope = f"{len(resolved)} 个聊天对象"
    else:
        entries, failures = collect_all_links(
            app.msg_db_keys, app.cache, names, app.display_name_fn,
            start_ts=start_ts, end_ts=end_ts, group_nickname_fn=group_nickname_fn,
        )
        scope = '全部消息'

    unique = merge_link_entries(entries)
    paged = unique[:limit]
    latest_timestamp = max((item['timestamp'] for item in paged), default=None)
    meta = build_meta(
        getattr(app, 'all_keys', {}) or {}, app.db_dir,
        failures=failures, latest_timestamp=latest_timestamp,
    )

    if fmt == 'json':
        output({
            'scope': scope,
            'count': len(paged),
            'raw_count': len(entries),
            'start_time': start_time or None,
            'end_time': end_time or None,
            'links': paged,
            'failures': failures if failures else None,
            'meta': meta,
        }, 'json')
        return

    if not paged:
        output(f'在 {scope} 中没有找到链接', 'text')
        return
    lines = []
    for item in paged:
        line = f"[{item['time']}] [{item['chat']}] {item['title'] or '(无标题)'}"
        if item['sender']:
            line += f" — {item['sender']}"
        line += f"\n  {item['url']}"
        if item['share_count'] > 1:
            line += f"\n  被分享 {item['share_count']} 次，最早 {item['first_shared_time']}"
        lines.append(line)
    header = f'在 {scope} 中找到 {len(paged)} 条链接（原始 {len(entries)} 条，按 URL 去重）'
    if failures:
        header += '\n查询失败: ' + '；'.join(failures)
    if meta.get('warnings'):
        header += '\n数据提示: ' + '；'.join(meta['warnings'])
    output(header + ':\n\n' + '\n\n'.join(lines), 'text')
