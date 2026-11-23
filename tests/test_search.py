import sqlite3
import unittest
from contextlib import closing

import zstandard

from wechat_cli.core.messages import _collect_search_entries, _query_messages


class SearchTests(unittest.TestCase):
    def test_keyword_matches_plain_and_compressed_messages_before_pagination(self):
        table = "Msg_" + "a" * 32
        context = {
            "table_name": table,
            "username": "example",
            "display_name": "Example",
            "is_group": False,
        }
        compressor = zstandard.ZstdCompressor()
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute(f"""
                CREATE TABLE {table} (
                    local_id, local_type, create_time, real_sender_id,
                    message_content, WCDB_CT_message_content
                )
            """)
            conn.executemany(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?)", [
                (1, 1, 101, 0, "Invoice plain", 0),
                (2, 1, 102, 0, compressor.compress(b"Invoice compressed"), 4),
                (3, 1, 103, 0, b"invoice UTF-8 blob", 0),
                (4, 1, 104, 0, b"not really compressed", 4),
                (5, 1, 105, 0, "Unrelated", 0),
                (6, 49, 106, 0, compressor.compress(b"invoice attachment"), 4),
            ])
            rows, failures = _collect_search_entries(
                conn, [context], {}, "invoice", lambda username, _names: username,
                start_ts=101, end_ts=106, candidate_limit=2, msg_type_filter=(1,),
            )
            self.assertEqual(failures, [])
            self.assertEqual([timestamp for timestamp, _ in rows], [103, 102])
            self.assertIn("Invoice compressed", rows[1][1])
            second_page = _query_messages(
                conn, table, keyword="invoice", offset=2, msg_type_filter=(1,),
            )
            self.assertEqual([row[0] for row in second_page], [1])
