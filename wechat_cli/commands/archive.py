"""archive 命令 — 按会话和时间段归档聊天记录并维护索引。"""

import click

from ..core.archive import ArchiveError, INDEX_NAME, archive_chat
from ..core.contacts import AmbiguousUsernameError, get_contact_names
from ..core.messages import parse_time_range, resolve_chat_context
from ..output.formatter import output
from .common import exit_ambiguous


@click.command('archive')
@click.argument('chat_name')
@click.option('--output-dir', 'output_dir', required=True, help='归档根目录')
@click.option('--segment', default='month', type=click.Choice(['month', 'day']), help='分段粒度')
@click.option('--start-time', default='', help='起始时间 YYYY-MM-DD [HH:MM[:SS]]')
@click.option('--end-time', default='', help='结束时间 YYYY-MM-DD [HH:MM[:SS]]')
@click.option('--format', 'fmt', default='json', type=click.Choice(['json', 'text']), help='输出格式')
@click.pass_context
def archive(ctx, chat_name, output_dir, segment, start_time, end_time, fmt):
    """按会话和时间段归档聊天记录，并写入不含正文的索引

    \b
    示例:
      wechat-cli archive "张三" --output-dir data/wechat
      wechat-cli archive "项目群" --output-dir data/wechat --start-time 2026-01-01 --end-time 2026-06-30
      wechat-cli archive "张三" --output-dir data/wechat --segment day
    """
    app = ctx.obj
    try:
        start_ts, end_ts = parse_time_range(start_time, end_time)
    except ValueError as error:
        click.echo(f'错误: {error}', err=True)
        ctx.exit(2)

    try:
        chat_ctx = resolve_chat_context(chat_name, app.msg_db_keys, app.cache, app.decrypted_dir)
    except AmbiguousUsernameError as error:
        exit_ambiguous(ctx, error)
    if not chat_ctx:
        click.echo(f'找不到聊天对象: {chat_name}', err=True)
        ctx.exit(1)
    if not chat_ctx['db_path']:
        click.echo(f'找不到 {chat_ctx["display_name"]} 的消息记录', err=True)
        ctx.exit(1)

    names = get_contact_names(app.cache, app.decrypted_dir)
    try:
        summary = archive_chat(
            app, chat_ctx, names, app.display_name_fn, output_dir,
            start_ts=start_ts, end_ts=end_ts, segment=segment,
        )
    except ArchiveError as error:
        click.echo(f'错误: {error}', err=True)
        ctx.exit(3)

    if fmt == 'json':
        output(summary, 'json')
    else:
        lines = [
            f"{item['file']}（{item['count']} 条）" for item in summary['segments']
        ]
        body = '\n'.join(lines) if lines else '没有可归档的消息'
        output(
            f"已归档 {summary['chat']}：{summary['total']} 条，"
            f"{len(summary['segments'])} 个分段\n{body}\n"
            f"索引: {summary['output_dir']}/index/{INDEX_NAME}",
            'text',
        )

    if summary['failures']:
        click.echo(f"错误: 归档不完整（{len(summary['failures'])} 项）", err=True)
        ctx.exit(3)
