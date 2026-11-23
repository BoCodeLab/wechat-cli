"""检查安装和本地配置，初始化前也可运行。"""

import json
import platform
from pathlib import Path

import click

from .. import __version__
from ..core.config import CONFIG_FILE, detect_db_dirs, read_config_file, resolve_config_paths
from ..core.key_utils import get_key_info, strip_key_metadata
from ..core.messages import find_msg_db_keys
from ..output.formatter import output


def _check_keys(path):
    result = {'path': str(path), 'status': 'missing', 'message_databases': 0}
    try:
        with path.open(encoding="utf-8") as file:
            keys = json.load(file)
        if not isinstance(keys, dict):
            raise ValueError("密钥文件必须包含 JSON 对象")
        keys = strip_key_metadata(keys)
        result['message_databases'] = len(find_msg_db_keys(keys))
        session = get_key_info(keys, "session/session.db")
        if session is None:
            result['status'] = 'session_key_missing'
        else:
            if not isinstance(session, dict) or len(bytes.fromhex(session.get('enc_key', ''))) != 32:
                raise ValueError("会话数据库的密钥格式无效")
            result['status'] = 'ready'
    except FileNotFoundError:
        pass
    except (OSError, TypeError, ValueError):
        result['status'] = 'invalid'
    return result


def _diagnose(config_path):
    candidates = detect_db_dirs()
    config_status = 'ready' if config_path.is_file() else 'missing'
    try:
        cfg = resolve_config_paths(read_config_file(config_path), config_path)
    except (OSError, TypeError, ValueError):
        config_status = 'invalid'
        cfg = resolve_config_paths({}, config_path)

    config_dir = cfg.get('db_dir')
    if config_dir:
        data_path, source = Path(config_dir), 'config'
    elif len(candidates) == 1:
        data_path, source = Path(candidates[0]), 'auto'
    else:
        data_path, source = None, None
    data_status = 'ambiguous' if data_path is None and len(candidates) > 1 else 'missing'
    if data_path is not None and data_path.is_dir():
        data_status = 'ready'
    session_path = data_path / "session" / "session.db" if data_path is not None else None
    session_ready = session_path is not None and session_path.is_file()
    keys = _check_keys(Path(cfg['keys_file']))

    next_steps = []
    if config_status == 'invalid':
        next_steps.append("检查配置文件是否为有效的 JSON，且路径字段为字符串。")
    if data_status == 'ambiguous':
        next_steps.append("在候选目录中选定账号，再用 wechat-cli init --db-dir 指定目录。")
    elif not session_ready:
        next_steps.append("在微信设置中确认本地存储位置，选择包含 session/session.db 的 db_storage 目录。")
    elif keys['status'] != 'ready':
        if config_status == 'missing' and config_path == Path(CONFIG_FILE):
            next_steps.append("运行 wechat-cli init 完成首次配置。")
        else:
            next_steps.append("检查配置指定的密钥文件及会话数据库条目。")

    return {
        'ready': config_status != 'invalid' and session_ready and keys['status'] == 'ready',
        'version': __version__,
        'platform': platform.system(),
        'python': platform.python_version(),
        'config': {'path': str(config_path), 'status': config_status},
        'data_dir': {
            'path': str(data_path) if data_path is not None else None,
            'status': data_status,
            'source': source,
            'candidates': candidates,
        },
        'session_db': {
            'path': str(session_path) if session_path is not None else None,
            'status': 'ready' if session_ready else 'missing',
        },
        'keys': keys,
        'next_steps': next_steps,
    }


@click.command()
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "text"]), help="输出格式")
@click.pass_context
def doctor(ctx, fmt):
    """报告配置、数据库目录和密钥文件的就绪状态。"""
    config_path = Path(ctx.find_root().params['config_path'] or CONFIG_FILE).expanduser().absolute()
    report = _diagnose(config_path)
    if fmt == 'json':
        output(report, 'json')
        return

    status_labels = {
        'ready': '就绪', 'missing': '缺失', 'invalid': '无效',
        'ambiguous': '多个候选', 'session_key_missing': '缺少会话密钥',
    }
    lines = [f"wechat-cli {report['version']}（{report['platform']}，Python {report['python']}）"]
    for key, label in (('config', '配置'), ('data_dir', '微信数据目录'), ('session_db', '会话数据库'), ('keys', '密钥文件')):
        check = report[key]
        path = f"  {check['path']}" if check['path'] else ""
        lines.append(f"{label}  {status_labels[check['status']]}{path}")
    if report['data_dir']['status'] == 'ambiguous':
        lines.extend(f"  {candidate}" for candidate in report['data_dir']['candidates'])
    lines.extend(report['next_steps'])
    output("\n".join(lines), 'text')
