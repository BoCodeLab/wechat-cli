import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

from wechat_cli.core.archive import (
    ArchiveError,
    INDEX_NAME,
    chat_time_range,
    sanitize_name,
    segment_ranges,
    update_index,
)

TABLE = 'Msg_' + 'a' * 32


class SegmentTests(unittest.TestCase):
    def test_monthly_segments_cross_year(self):
        start = int(datetime(2025, 11, 28, 10, 0).timestamp())
        end = int(datetime(2026, 2, 1).timestamp())
        ranges = segment_ranges(start, end, 'month')
        self.assertEqual(
            [(item[2], item[3]) for item in ranges],
            [
                ('2025-11-28', '2025-11-30'),
                ('2025-12-01', '2025-12-31'),
                ('2026-01-01', '2026-01-31'),
            ],
        )

    def test_daily_segments(self):
        start = int(datetime(2026, 1, 1, 22, 0).timestamp())
        end = int(datetime(2026, 1, 4).timestamp())
        ranges = segment_ranges(start, end, 'day')
        self.assertEqual(len(ranges), 3)
        self.assertEqual(ranges[0][2:], ('2026-01-01', '2026-01-01'))
        self.assertEqual(ranges[-1][2:], ('2026-01-03', '2026-01-03'))

    def test_empty_or_invalid(self):
        self.assertEqual(segment_ranges(10, 10), [])
        with self.assertRaises(ArchiveError):
            segment_ranges(1, 2, 'week')

    def test_sanitize_name(self):
        self.assertEqual(sanitize_name('a/b:c*?"<>|'), 'a_b_c______')
        self.assertEqual(sanitize_name(''), 'chat')
        self.assertEqual(sanitize_name('AI 群'), 'AI 群')


class ChatRangeTests(unittest.TestCase):
    def test_min_and_max_across_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index, times in enumerate(([100, 300], [200, 500])):
                path = os.path.join(tmp, f'message_{index}.db')
                conn = sqlite3.connect(path)
                conn.execute(f'CREATE TABLE [{TABLE}] (local_id INTEGER, create_time INTEGER)')
                conn.executemany(f'INSERT INTO [{TABLE}] VALUES (?, ?)', list(enumerate(times)))
                conn.commit()
                conn.close()
                paths.append({'db_path': path, 'table_name': TABLE})
            self.assertEqual(chat_time_range({'message_tables': paths}), (100, 500))

    def test_no_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'message_0.db')
            conn = sqlite3.connect(path)
            conn.execute(f'CREATE TABLE [{TABLE}] (local_id INTEGER, create_time INTEGER)')
            conn.commit()
            conn.close()
            self.assertEqual(
                chat_time_range({'message_tables': [{'db_path': path, 'table_name': TABLE}]}),
                (None, None),
            )


class IndexTests(unittest.TestCase):
    def test_writes_and_dedupes_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {
                'chat': 'A', 'username': 'u', 'start': '2026-01-01',
                'end': '2026-01-31', 'count': 1, 'file': 'chats/A/x.jsonl',
            }
            update_index(tmp, [entry])
            update_index(tmp, [dict(entry, count=2), {
                'chat': 'B', 'username': 'v', 'start': '2026-02-01',
                'end': '2026-02-28', 'count': 3, 'file': 'chats/B/y.jsonl',
            }])
            path = os.path.join(tmp, 'index', INDEX_NAME)
            with open(path, encoding='utf-8') as stream:
                rows = [json.loads(line) for line in stream if line.strip()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['count'], 2)
            self.assertEqual(rows[1]['username'], 'v')


if __name__ == '__main__':
    unittest.main()
