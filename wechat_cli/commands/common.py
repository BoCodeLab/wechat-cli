"""命令共用的错误处理。"""

import click


def exit_ambiguous(ctx, error):
    """打印歧义候选并退出，避免静默查到错误的人或群。"""
    click.echo(f"错误: {error}", err=True)
    for item in error.candidates:
        click.echo(f"  - {item['display_name']}  ({item['username']})", err=True)
    ctx.exit(2)
