import tempfile
import unittest
from pathlib import Path

from wechat_cli.core.contacts import _parse_group_members
from wechat_cli.core.meta import build_meta


def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _bytes_field(number, payload):
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _member(username, nickname=''):
    entry = _bytes_field(1, username.encode())
    if nickname:
        entry += _bytes_field(2, nickname.encode())
    entry += _varint(3 << 3) + _varint(1)
    return entry


class MetaTests(unittest.TestCase):
    def test_missing_shards_make_status_possibly_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            message_dir = Path(tmp) / "message"
            message_dir.mkdir()
            (message_dir / "message_0.db").write_bytes(b'')
            (message_dir / "message_1.db").write_bytes(b'')
            meta = build_meta({"message/message_0.db": {"enc_key": "00"}}, tmp)
            self.assertEqual(meta['status'], 'possibly_stale')
            self.assertEqual(meta['missing_shards'], ['message/message_1.db'])
            self.assertTrue(meta['warnings'])

    def test_failures_make_status_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = build_meta({}, tmp, failures=['x'], latest_timestamp=1700000000)
            self.assertEqual(meta['status'], 'partial')
            self.assertEqual(meta['latest_timestamp'], 1700000000)
            self.assertIsNotNone(meta['latest_time'])

    def test_healthy_directory_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = build_meta({}, tmp)
            self.assertEqual(meta['status'], 'ok')
            self.assertEqual(meta['missing_shards'], [])
            self.assertIsNone(meta['warnings'])


class GroupNicknameTests(unittest.TestCase):
    def test_parses_member_entries(self):
        blob = _bytes_field(1, _member('wxid_a', '群昵称A')) + _bytes_field(1, _member('wxid_b'))
        self.assertEqual(_parse_group_members(blob), {'wxid_a': '群昵称A', 'wxid_b': ''})

    def test_returns_partial_result_on_truncated_data(self):
        blob = _bytes_field(1, _member('wxid_a', 'A')) + b'\x0a\x10\x08'
        self.assertEqual(_parse_group_members(blob), {'wxid_a': 'A'})

    def test_empty_blob(self):
        self.assertEqual(_parse_group_members(b''), {})
