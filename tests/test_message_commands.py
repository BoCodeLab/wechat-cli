import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import zstandard
from click.testing import CliRunner

from wechat_cli.core.messages import _build_message_record
from wechat_cli.main import cli


USERNAME = "wxid_fixture"
TABLE = "Msg_" + hashlib.md5(USERNAME.encode()).hexdigest()
FIRST_SHARD = "message/message_0.db"
SECOND_SHARD = "message/message_1.db"


class MessageCommandsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.default_config = self.root / "config.json"
        self.legacy_file = self.root / "last_check.json"
        self.shards = {}
        self.session = self.root / "session.db"
        with closing(sqlite3.connect(self.session)) as conn, conn:
            conn.execute("""
                CREATE TABLE SessionTable (
                    username, unread_count, summary, last_timestamp, last_msg_type,
                    last_msg_sender, last_sender_display_name
                )
            """)
            conn.execute(
                "INSERT INTO SessionTable VALUES (?, 1, 'initial unread', 100, 1, '', '')",
                (USERNAME,),
            )

        self.app = SimpleNamespace(
            db_dir=str(self.root / "db_storage"),
            config_path=str(self.default_config),
            decrypted_dir=str(self.root / "decrypted"),
            msg_db_keys=[],
            cache=SimpleNamespace(get=self.cache_get),
            display_name_fn=lambda username, names: names.get(username, username),
        )
        self.add_shard(FIRST_SHARD)
        self.add_message(FIRST_SHARD, 100, "old")
        state = patch.multiple(
            "wechat_cli.commands.new_messages", CURSOR_FILE=str(self.root / "cursors.sqlite3"),
            LEGACY_STATE_FILE=str(self.legacy_file), CONFIG_FILE=str(self.default_config),
        )
        state.start()
        self.addCleanup(state.stop)
        app_context = patch("wechat_cli.main.AppContext", return_value=self.app)
        app_context.start()
        self.addCleanup(app_context.stop)
        self.runner = CliRunner()

    def cache_get(self, key):
        key = key.replace("\\", "/")
        return self.session if key == "session/session.db" else self.shards.get(key)

    def add_shard(self, key):
        path = self.root / Path(key).name
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.execute("INSERT INTO Name2Id VALUES (?)", (USERNAME,))
            conn.execute(f"""
                CREATE TABLE {TABLE} (
                    local_id INTEGER PRIMARY KEY AUTOINCREMENT, local_type, create_time,
                    real_sender_id, message_content, WCDB_CT_message_content
                )
            """)
        self.shards[key] = path
        self.app.msg_db_keys.append(key)

    def add_message(self, key, timestamp, text):
        with closing(sqlite3.connect(self.shards[key])) as conn, conn:
            conn.execute(
                f"INSERT INTO {TABLE} VALUES (NULL, 1, ?, 1, ?, 0)", (timestamp, text),
            )

    def poll(self, *args):
        result = self.runner.invoke(cli, ["new-messages", *args])
        self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        return json.loads(result.output)

    def test_same_second_messages_are_returned_individually_across_polls(self):
        first = self.poll()
        self.assertTrue(first["first_call"])
        self.assertEqual(first["messages"][0]["last_message"], "initial unread")
        self.add_message(FIRST_SHARD, 101, "first")
        self.add_message(FIRST_SHARD, 101, "second")
        batch = self.poll()
        self.assertEqual([message["text"] for message in batch["messages"]], ["first", "second"])
        ids = {message["message_id"] for message in batch["messages"]}
        self.assertEqual(len(ids), 2)
        self.add_message(FIRST_SHARD, 101, "third")
        next_batch = self.poll()
        self.assertEqual(next_batch["new_count"], 1)
        self.assertNotIn(next_batch["messages"][0]["message_id"], ids)
        self.assertEqual(self.poll()["new_count"], 0)

    def test_named_cursors_read_a_new_shard_independently(self):
        self.assertTrue(self.poll()["first_call"])
        self.assertTrue(self.poll("--cursor", "report")["first_call"])
        self.add_shard(SECOND_SHARD)
        self.add_message(SECOND_SHARD, 102, "new shard")
        self.add_message(FIRST_SHARD, 102, "old shard")
        default = self.poll()
        report = self.poll("--cursor", "report")
        self.assertEqual(default["messages"], report["messages"])
        self.assertEqual({row["text"] for row in default["messages"]}, {"new shard", "old shard"})
        self.assertEqual(len({row["message_id"] for row in default["messages"]}), 2)

    def test_legacy_cursor_replays_boundary_second_once(self):
        self.legacy_file.write_text(json.dumps({USERNAME: 101}), encoding="utf-8")
        self.add_message(FIRST_SHARD, 101, "boundary")
        self.add_message(FIRST_SHARD, 102, "new")
        batch = self.poll()
        self.assertFalse(batch["first_call"])
        self.assertEqual([message["text"] for message in batch["messages"]], ["boundary", "new"])
        self.assertEqual(self.poll()["new_count"], 0)

    def test_failed_query_keeps_the_cursor_for_retry(self):
        self.poll()
        self.add_message(FIRST_SHARD, 101, "pending")
        self.add_shard(SECOND_SHARD)
        self.add_message(SECOND_SHARD, 102, "other")
        with closing(sqlite3.connect(self.shards[SECOND_SHARD])) as conn, conn:
            conn.execute(f"ALTER TABLE {TABLE} RENAME COLUMN message_content TO missing_content")
        result = self.runner.invoke(cli, ["new-messages"])
        self.assertEqual(result.exit_code, 3, result.output)
        with closing(sqlite3.connect(self.shards[SECOND_SHARD])) as conn, conn:
            conn.execute(f"ALTER TABLE {TABLE} RENAME COLUMN missing_content TO message_content")
        self.assertEqual([row["text"] for row in self.poll()["messages"]], ["pending", "other"])

    def test_global_and_chat_search_both_find_compressed_messages(self):
        self.add_message(FIRST_SHARD, 101, "example plain")
        with closing(sqlite3.connect(self.shards[FIRST_SHARD])) as conn, conn:
            conn.execute(
                f"INSERT INTO {TABLE} VALUES (NULL, 1, 102, 1, ?, 4)",
                (zstandard.ZstdCompressor().compress(b"example compressed"),),
            )
        for scope in ([], ["--chat", USERNAME]):
            with self.subTest(scope=scope):
                result = self.runner.invoke(cli, ["search", "example", *scope])
                self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
                matches = json.loads(result.output)
                self.assertEqual(matches["count"], 2)
                self.assertIn("example compressed", matches["results"][-1])

    def test_structured_history_matches_incremental_ids_and_keeps_default_strings(self):
        self.poll()
        self.add_shard(SECOND_SHARD)
        self.add_message(SECOND_SHARD, 103, "alternate shard")
        self.add_message(FIRST_SHARD, 102, "latest in first shard")
        new = self.poll()

        result = self.runner.invoke(cli, ["history", USERNAME, "--limit", "2", "--structured"])
        self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        messages = json.loads(result.output)["messages"]
        self.assertEqual([row["message_id"] for row in messages], [row["message_id"] for row in new["messages"]])
        self.assertEqual([row["timestamp"] for row in messages], [102, 103])
        self.assertEqual(messages[0]["sender"], USERNAME)
        self.assertEqual(messages[0]["type"], 1)
        self.assertEqual(messages[0]["text"], "latest in first shard")

        legacy = self.runner.invoke(cli, ["history", USERNAME, "--limit", "1", "--offset", "1"])
        self.assertEqual(legacy.exit_code, 0, legacy.output or repr(legacy.exception))
        line = json.loads(legacy.output)["messages"][0]
        self.assertTrue(line.endswith("latest in first shard"))

    def test_jsonl_streams_only_the_selected_records_with_one_object_per_line(self):
        self.add_shard(SECOND_SHARD)
        self.add_message(SECOND_SHARD, 102, "第二条")
        self.add_message(FIRST_SHARD, 103, "第一行\n第二行")
        with patch("wechat_cli.core.messages._build_message_record", wraps=_build_message_record) as build:
            result = self.runner.invoke(cli, ["export", USERNAME, "--format", "jsonl", "--limit", "2"])
        self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(build.call_count, 2)
        self.assertEqual([json.loads(line)["text"] for line in lines], ["第一行\n第二行", "第二条"])

    def test_jsonl_file_is_replaced_only_after_successful_read(self):
        target = self.root / "export.jsonl"
        target.write_text("previous export", encoding="utf-8")
        self.add_shard(SECOND_SHARD)
        self.add_message(SECOND_SHARD, 102, "other shard")
        with closing(sqlite3.connect(self.shards[SECOND_SHARD])) as conn, conn:
            conn.execute(f"ALTER TABLE {TABLE} RENAME COLUMN message_content TO missing_content")
        args = ["export", USERNAME, "--format", "jsonl", "--output", str(target)]
        bad = self.runner.invoke(cli, args)
        self.assertNotEqual(bad.exit_code, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "previous export")
        self.assertEqual(list(self.root.glob(".export.jsonl.*.tmp")), [])

        with closing(sqlite3.connect(self.shards[SECOND_SHARD])) as conn, conn:
            conn.execute(f"ALTER TABLE {TABLE} RENAME COLUMN missing_content TO message_content")
        good = self.runner.invoke(cli, args)
        self.assertEqual(good.exit_code, 0, good.output or repr(good.exception))
        self.assertEqual([json.loads(line)["text"] for line in target.read_text(encoding="utf-8").splitlines()], ["other shard", "old"])
