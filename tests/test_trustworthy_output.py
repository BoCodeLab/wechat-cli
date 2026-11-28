import hashlib
import json
import os
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from wechat_cli.commands.attachments import _collect_attachments
from wechat_cli.core.contacts import (
    AmbiguousUsernameError,
    reset_contact_cache,
    resolve_username,
)
from wechat_cli.core.messages import _format_message_text, _resolve_media_path
from wechat_cli.main import cli

USERNAME = "wxid_fixture"
TABLE = "Msg_" + hashlib.md5(USERNAME.encode()).hexdigest()
SHARD = "message/message_0.db"

LINK_XML = (
    '<msg><appmsg><title>测试文章</title><type>5</type>'
    '<url>https://example.com/a</url><des>一段摘要</des></appmsg></msg>'
)


def _contact_db(root):
    path = Path(root) / "decrypted" / "contact" / "contact.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("""
            CREATE TABLE contact (
                id INTEGER PRIMARY KEY, username TEXT, nick_name TEXT, remark TEXT,
                alias TEXT, description TEXT, small_head_url TEXT, big_head_url TEXT,
                verify_flag INTEGER, local_type INTEGER
            )
        """)
        conn.execute("CREATE TABLE chat_room (id INTEGER, username TEXT, owner TEXT, ext_buffer BLOB)")
        conn.execute("CREATE TABLE chatroom_member (room_id INTEGER, member_id INTEGER)")
    return path


class FixtureApp:
    def __init__(self, root):
        self.root = Path(root)
        self.db_dir = str(self.root / "db_storage")
        (self.root / "db_storage").mkdir(parents=True, exist_ok=True)
        self.decrypted_dir = str(self.root / "decrypted")
        self.msg_db_keys = []
        self.shards = {}
        self.cache = SimpleNamespace(get=self.cache_get)
        self.display_name_fn = lambda username, names: names.get(username, username)

    def cache_get(self, key):
        return self.shards.get(key.replace("\\", "/"))

    def add_shard(self):
        path = self.root / "message_0.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.execute("INSERT INTO Name2Id VALUES (?)", (USERNAME,))
            conn.execute(f"""
                CREATE TABLE {TABLE} (
                    local_id INTEGER PRIMARY KEY AUTOINCREMENT, local_type, create_time,
                    real_sender_id, message_content, WCDB_CT_message_content
                )
            """)
        self.shards[SHARD] = path
        self.msg_db_keys.append(SHARD)
        return path

    def add_message(self, timestamp, content, local_type=1):
        with closing(sqlite3.connect(self.shards[SHARD])) as conn, conn:
            conn.execute(
                f"INSERT INTO {TABLE} VALUES (NULL, ?, ?, 1, ?, 0)",
                (local_type, timestamp, content),
            )


class CommandFixture(unittest.TestCase):
    def setUp(self):
        reset_contact_cache()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = FixtureApp(self.root)
        self.app.add_shard()
        context = patch("wechat_cli.main.AppContext", return_value=self.app)
        context.start()
        self.addCleanup(context.stop)
        self.runner = CliRunner()

    def invoke(self, *args):
        result = self.runner.invoke(cli, list(args))
        return result


class StructuredSearchTests(CommandFixture):
    def test_structured_search_returns_identity_url_and_message_id(self):
        self.app.add_message(101, LINK_XML, local_type=49)
        self.app.add_message(102, "普通文本", local_type=1)

        result = self.invoke("search", "测试", "--structured")
        self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        payload = json.loads(result.output)
        self.assertTrue(payload["structured"])
        self.assertEqual(payload["count"], 1)
        record = payload["results"][0]
        self.assertEqual(record["url"], "https://example.com/a")
        self.assertEqual(record["text"], "[链接] 测试文章")
        self.assertEqual(record["sender_username"], USERNAME)
        self.assertTrue(record["message_id"].endswith(f":{TABLE}:1"))
        self.assertFalse(record["text_truncated"])

    def test_structured_search_requires_json(self):
        result = self.invoke("search", "测试", "--structured", "--format", "text")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--structured", result.output)


class LinksCommandTests(CommandFixture):
    def test_links_dedupes_by_url_and_counts_shares(self):
        self.app.add_message(101, LINK_XML, local_type=49)
        self.app.add_message(102, LINK_XML, local_type=49)
        second = (
            '<msg><appmsg><title>另一篇</title><type>5</type>'
            '<url>https://example.com/b</url></appmsg></msg>'
        )
        self.app.add_message(103, second, local_type=49)
        file_xml = (
            '<msg><appmsg><title>附件.pdf</title><type>6</type>'
            '<url>https://example.com/file</url></appmsg></msg>'
        )
        self.app.add_message(104, file_xml, local_type=49)

        result = self.invoke("links", "--limit", "10")
        self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        payload = json.loads(result.output)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["raw_count"], 3)
        by_url = {item["url"]: item for item in payload["links"]}
        self.assertEqual(by_url["https://example.com/a"]["share_count"], 2)
        self.assertEqual(by_url["https://example.com/a"]["first_shared_at"], 101)
        self.assertEqual(by_url["https://example.com/a"]["timestamp"], 102)
        self.assertNotIn("https://example.com/file", by_url)

    def test_links_can_scope_to_one_chat_and_time_range(self):
        self.app.add_message(101, LINK_XML, local_type=49)
        self.app.add_message(102, LINK_XML, local_type=49)

        scoped = self.invoke("links", "--chat", USERNAME, "--limit", "10")
        self.assertEqual(scoped.exit_code, 0, scoped.output or repr(scoped.exception))
        self.assertEqual(json.loads(scoped.output)["count"], 1)

        outside = self.invoke(
            "links", "--start-time", "2026-11-20", "--limit", "10",
        )
        self.assertEqual(outside.exit_code, 0, outside.output or repr(outside.exception))
        self.assertEqual(json.loads(outside.output)["count"], 0)


class MembersCommandTests(CommandFixture):
    def setUp(self):
        super().setUp()
        path = _contact_db(self.root)
        self.group = "123@chatroom"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "INSERT INTO contact VALUES (1, ?, '测试群', '', '', '', '', '', 0, 2)",
                (self.group,),
            )
            conn.execute("INSERT INTO chat_room VALUES (1, ?, 'wxid_owner', NULL)", (self.group,))
            conn.execute("INSERT INTO chatroom_member VALUES (1, 2)")
            conn.execute("INSERT INTO chatroom_member VALUES (1, 3)")
            conn.execute("INSERT INTO contact VALUES (2, 'wxid_a', '张三', '', '', '', '', '', 0, 1)")
            conn.execute("INSERT INTO contact VALUES (3, 'wxid_b', '李四', '', '', '', '', '', 0, 1)")

    def test_text_output_lists_every_member(self):
        result = self.invoke("members", "测试群", "--format", "text")
        self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        self.assertIn("张三", result.output)
        self.assertIn("李四", result.output)
        self.assertIn("共 2 人", result.output)
        self.assertNotIn("NameError", result.output)


class AmbiguityTests(unittest.TestCase):
    def test_duplicate_display_names_raise_with_candidates(self):
        reset_contact_cache()
        with TemporaryDirectory() as tmp:
            path = _contact_db(tmp)
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("INSERT INTO contact VALUES (1, 'wxid_1', '小文', '', '', '', '', '', 0, 1)")
                conn.execute("INSERT INTO contact VALUES (2, 'wxid_2', '小文', '', '', '', '', '', 0, 1)")

            cache = SimpleNamespace(get=lambda key: None)
            with self.assertRaises(AmbiguousUsernameError) as caught:
                resolve_username("小文", cache, str(Path(tmp) / "decrypted"), strict=True)
            self.assertEqual(len(caught.exception.candidates), 2)
            self.assertIn("匹配到多个", str(caught.exception))
        reset_contact_cache()


class AmbiguousCommandTests(CommandFixture):
    def test_history_reports_candidates_instead_of_picking_one(self):
        path = _contact_db(self.root)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("INSERT INTO contact VALUES (1, 'wxid_1', '小文', '', '', '', '', '', 0, 1)")
            conn.execute("INSERT INTO contact VALUES (2, 'wxid_2', '小文', '', '', '', '', '', 0, 1)")

        result = self.invoke("history", "小文", "--limit", "1")
        self.assertEqual(result.exit_code, 2, result.output or repr(result.exception))
        self.assertIn("匹配到多个", result.output)
        self.assertIn("wxid_1", result.output)
        self.assertIn("wxid_2", result.output)


class MediaResolutionTests(unittest.TestCase):
    def test_image_uses_packed_basename_not_first_file_in_month(self):
        with TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, "db_storage")
            os.makedirs(os.path.join(tmp, "msg"))
            chat = "wxid_chat"
            folder = os.path.join(tmp, "msg", "attach", hashlib.md5(chat.encode()).hexdigest(), "2026-11", "Img")
            os.makedirs(folder)
            wanted = "1" * 32
            other = "2" * 32
            for base in (wanted, other):
                with open(os.path.join(folder, base + ".dat"), "wb") as handle:
                    handle.write(b"x")
            timestamp = int(datetime(2026, 11, 20, 12, 0).timestamp())

            path, exists = _resolve_media_path(
                db_dir, "", 3, timestamp, chat, packed_info=b"\x08" + wanted.encode(),
            )
            self.assertTrue(exists)
            self.assertTrue(path.endswith(wanted + ".dat"))
            self.assertNotIn(other, path)

    def test_voice_and_video_do_not_return_arbitrary_files(self):
        with TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, "db_storage")
            os.makedirs(os.path.join(tmp, "msg"))
            chat = "wxid_chat"
            folder = os.path.join(tmp, "msg", "attach", hashlib.md5(chat.encode()).hexdigest(), "2026-11", "Voice")
            os.makedirs(folder)
            open(os.path.join(folder, "a.dat"), "wb").close()
            timestamp = int(datetime(2026, 11, 20, 12, 0).timestamp())
            self.assertEqual(_resolve_media_path(db_dir, "", 34, timestamp, chat), (None, False))
            self.assertEqual(_resolve_media_path(db_dir, "", 43, timestamp, chat), (None, False))


class TextFormattingTests(unittest.TestCase):
    def test_quote_message_extracts_inner_title_instead_of_xml(self):
        inner = "<msg><appmsg><title>引用标题</title><des>引用摘要</des></appmsg></msg>"
        xml = (
            '<msg><appmsg><title>回复</title><type>57</type><refermsg>'
            f'<displayname>张三</displayname><content>{inner}</content>'
            '</refermsg></appmsg></msg>'
        )
        _, text = _format_message_text(0, 49, xml, True, "g@chatroom", "群", {}, lambda u, n: u)
        self.assertIn("引用标题", text)
        self.assertNotIn("<msg>", text)

    def test_system_revoke_is_human_readable(self):
        xml = (
            '<sysmsg type="revokemsg"><revokemsg>'
            '<content>"张三" 撤回了一条消息</content>'
            '</revokemsg></sysmsg>'
        )
        _, text = _format_message_text(0, 10000, xml, False, "wxid_a", "张三", {}, lambda u, n: u)
        self.assertEqual(text, '[系统] "张三" 撤回了一条消息')
        self.assertNotIn("<", text)


class AttachmentLimitTests(unittest.TestCase):
    def test_limit_is_filled_even_when_recent_images_have_no_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db_storage"
            db_dir.mkdir()
            app = SimpleNamespace(
                db_dir=str(db_dir),
                display_name_fn=lambda username, names: names.get(username, username),
            )
            chat = "wxid_img"
            folder = root / "msg" / "attach" / hashlib.md5(chat.encode()).hexdigest() / "2026-11" / "Img"
            folder.mkdir(parents=True)
            timestamp = int(datetime(2026, 11, 20, 12, 0).timestamp())
            table = "Msg_" + "b" * 32
            conn = sqlite3.connect(root / "messages.db")
            conn.execute(f"""
                CREATE TABLE {table} (
                    local_id INTEGER PRIMARY KEY AUTOINCREMENT, local_type, create_time,
                    real_sender_id, message_content, WCDB_CT_message_content, packed_info_data
                )
            """)
            # 最新两条没有本地文件，较早两条才有。
            conn.execute(f"INSERT INTO {table} VALUES (NULL, 3, ?, 1, '', 0, ?)", (timestamp + 2, b"a" * 32))
            conn.execute(f"INSERT INTO {table} VALUES (NULL, 3, ?, 1, '', 0, ?)", (timestamp + 1, b"b" * 32))
            for offset, base in ((0, "c" * 32), (-1, "d" * 32)):
                path = folder / f"{base}.dat"
                path.write_bytes(b"x")
                conn.execute(
                    f"INSERT INTO {table} VALUES (NULL, 3, ?, 1, '', 0, ?)",
                    (timestamp + offset, base.encode()),
                )
            conn.commit()
            conn.close()
            chat_ctx = {
                "query": "图片群",
                "username": chat,
                "display_name": "图片群",
                "is_group": False,
                "message_tables": [{"db_path": str(root / "messages.db"), "db_key": "message/message_0.db", "table_name": table}],
            }
            items, failures = _collect_attachments(app, chat_ctx, {}, limit=2)
            self.assertEqual(failures, [])
            self.assertEqual(len(items), 2)
            self.assertEqual({item["msg_id"].rsplit(":", 1)[1] for item in items}, {"3", "4"})


if __name__ == "__main__":
    unittest.main()
