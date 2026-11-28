"""查询元信息 — 数据新鲜度、缺失分片和查询状态。"""

import os
import re
import time
from datetime import datetime

from .key_utils import get_key_info

_MESSAGE_SHARD_RE = re.compile(r"message_\d+\.db$")


def find_message_shards(db_dir):
    """返回本地 message 目录下的分片相对路径，例如 ``message/message_0.db``。"""
    if not db_dir:
        return []
    message_dir = os.path.join(db_dir, "message")
    if not os.path.isdir(message_dir):
        return []
    return sorted(
        f"message/{name}"
        for name in os.listdir(message_dir)
        if _MESSAGE_SHARD_RE.fullmatch(name)
    )


def find_missing_message_shards(all_keys, db_dir):
    """返回磁盘上存在、但密钥文件里没有对应条目的消息分片。"""
    keys = all_keys or {}
    return [rel for rel in find_message_shards(db_dir) if not get_key_info(keys, rel)]


def build_meta(all_keys, db_dir, *, failures=None, latest_timestamp=None):
    """构造 history/search 的 ``meta`` 字段。

    status:
      - ``ok``：没有缺失分片，也没有查询失败
      - ``partial``：部分查询失败，结果不完整
      - ``possibly_stale``：有消息分片缺少密钥，结果可能漏消息
    """
    missing = find_missing_message_shards(all_keys, db_dir)
    failures = list(failures or [])
    warnings = []
    if missing:
        warnings.append(f"{len(missing)} 个消息分片缺少密钥，结果可能不完整")
    if failures:
        warnings.append(f"{len(failures)} 个查询失败")

    if failures:
        status = "partial"
    elif missing:
        status = "possibly_stale"
    else:
        status = "ok"

    meta = {
        'status': status,
        'missing_shards': missing,
        'warnings': warnings or None,
        'generated_at': int(time.time()),
    }
    if latest_timestamp:
        meta['latest_timestamp'] = int(latest_timestamp)
        meta['latest_time'] = datetime.fromtimestamp(latest_timestamp).strftime('%Y-%m-%d %H:%M:%S')
    else:
        meta['latest_timestamp'] = None
        meta['latest_time'] = None
    return meta
