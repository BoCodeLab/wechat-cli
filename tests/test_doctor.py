import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner

from wechat_cli.core import config
from wechat_cli.main import cli


class DoctorTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.documents = self.home / "Documents"
        self.settings = self.home / "cli" / "config.json"
        self.settings.parent.mkdir()
        mocks = (
            patch.object(config, "_SYSTEM", "windows"),
            patch.object(config.Path, "home", return_value=self.home),
            patch.object(config, "_windows_documents_dir", side_effect=lambda: self.documents),
            patch.dict(config.os.environ, {"APPDATA": str(self.home / "AppData")}),
        )
        for mock in mocks:
            mock.start()
            self.addCleanup(mock.stop)

    def create_account(self, account):
        root = self.documents / "xwechat_files" / account / "db_storage"
        session = root / "session" / "session.db"
        session.parent.mkdir(parents=True)
        session.touch()
        return root

    def doctor(self):
        result = CliRunner().invoke(cli, ["--config", str(self.settings), "doctor", "--format", "json"])
        self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        return json.loads(result.output)

    def test_documents_detected_even_without_tencent_config(self):
        root = self.create_account("account")
        self.assertEqual(config.auto_detect_db_dir(), str(root))
        report = self.doctor()
        self.assertFalse(report["ready"])
        self.assertEqual(report["data_dir"]["source"], "auto")
        self.assertEqual(report["data_dir"]["path"], str(root))
        self.assertEqual(report["session_db"]["status"], "ready")
        self.assertEqual(report["keys"]["status"], "missing")
        self.assertFalse(self.settings.exists())

    def test_redirected_documents_and_ini_pointing_to_container_are_deduplicated(self):
        self.documents = self.home / "redirected"
        root = self.create_account("account")
        ini_dir = self.home / "AppData" / "Tencent" / "xwechat" / "config"
        ini_dir.mkdir(parents=True)
        (ini_dir / "paths.ini").write_text(str(root.parent.parent), encoding="utf-16")
        self.assertEqual(config.detect_db_dirs(), [str(root)])

    def test_diagnosis_lists_multiple_accounts_and_respects_explicit_configuration(self):
        self.create_account("first")
        preferred = self.create_account("second")
        ambiguous = self.doctor()
        self.assertEqual(ambiguous["data_dir"]["status"], "ambiguous")
        self.assertEqual(len(ambiguous["data_dir"]["candidates"]), 2)
        self.assertIsNone(ambiguous["data_dir"]["path"])

        self.settings.write_text(
            json.dumps({"db_dir": str(preferred), "keys_file": "local-keys.json"}), encoding="utf-8",
        )
        sample_key = "ab" * 32
        (self.settings.parent / "local-keys.json").write_text(
            json.dumps({"session\\session.db": {"enc_key": sample_key}}), encoding="utf-8",
        )
        ready = self.doctor()
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["data_dir"]["path"], str(preferred))
        self.assertEqual(ready["data_dir"]["source"], "config")
        self.assertNotIn(sample_key, json.dumps(ready))

    def test_invalid_config_gets_diagnosis_before_normal_initialization(self):
        self.create_account("account")
        self.settings.write_text("[]", encoding="utf-8")
        report = self.doctor()
        self.assertFalse(report["ready"])
        self.assertEqual(report["config"]["status"], "invalid")
        self.assertEqual(report["data_dir"]["source"], "auto")
        self.assertTrue(report["next_steps"])
