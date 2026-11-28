"""解密数据库缓存 — mtime 检测变化，跨会话复用"""

import hashlib
import json
import os
import time

from .config import STATE_DIR
from .crypto import full_decrypt, decrypt_wal
from .key_utils import get_key_info


class DBCache:
    # 解密后的数据库是明文，放在用户状态目录而不是系统临时目录。
    CACHE_DIR = os.path.join(STATE_DIR, "cache")
    MTIME_FILE = os.path.join(STATE_DIR, "cache", "_mtimes.json")

    def __init__(self, all_keys, db_dir):
        self._all_keys = all_keys
        self._db_dir = db_dir
        self._cache = {}  # rel_key -> (db_mtime, wal_mtime, tmp_path)
        os.makedirs(self.CACHE_DIR, exist_ok=True)
        self._prune_stale_temp_files()
        self._load_persistent_cache()

    def _prune_stale_temp_files(self):
        cutoff = time.time() - 24 * 60 * 60
        try:
            for name in os.listdir(self.CACHE_DIR):
                if not name.endswith('.tmp'):
                    continue
                path = os.path.join(self.CACHE_DIR, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    continue
        except OSError:
            pass

    def _cache_path(self, rel_key):
        h = hashlib.md5(rel_key.encode()).hexdigest()[:12]
        return os.path.join(self.CACHE_DIR, f"{h}.db")

    def _load_persistent_cache(self):
        if not os.path.exists(self.MTIME_FILE):
            return
        try:
            with open(self.MTIME_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        for rel_key, info in saved.items():
            tmp_path = info["path"]
            if not os.path.exists(tmp_path):
                continue
            cache_root = os.path.abspath(self.CACHE_DIR) + os.sep
            if not os.path.abspath(tmp_path).startswith(cache_root):
                # 旧版本可能把明文缓存放在系统临时目录，不再复用。
                continue
            rel_path = rel_key.replace('\\', os.sep)
            db_path = os.path.join(self._db_dir, rel_path)
            wal_path = db_path + "-wal"
            try:
                db_mtime = os.path.getmtime(db_path)
                wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
            except OSError:
                continue
            if db_mtime == info["db_mt"] and wal_mtime == info["wal_mt"]:
                self._cache[rel_key] = (db_mtime, wal_mtime, tmp_path)

    def _save_persistent_cache(self):
        data = {}
        for rel_key, (db_mt, wal_mt, path) in self._cache.items():
            data[rel_key] = {"db_mt": db_mt, "wal_mt": wal_mt, "path": path}
        temp_path = f"{self.MTIME_FILE}.{os.getpid()}.tmp"
        try:
            with open(temp_path, 'w', encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(temp_path, self.MTIME_FILE)
        except OSError:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    @staticmethod
    def _install_cache_file(temp_path, target_path):
        """原子替换缓存文件，避免其他进程读到半成品。"""
        for attempt in range(6):
            try:
                os.replace(temp_path, target_path)
                return
            except OSError:
                if attempt == 5:
                    raise
                time.sleep(0.05)

    def get(self, rel_key):
        key_info = get_key_info(self._all_keys, rel_key)
        if not key_info:
            return None
        rel_path = rel_key.replace('\\', '/').replace('/', os.sep)
        db_path = os.path.join(self._db_dir, rel_path)
        wal_path = db_path + "-wal"
        if not os.path.exists(db_path):
            return None

        try:
            db_mtime = os.path.getmtime(db_path)
            wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
        except OSError:
            return None

        if rel_key in self._cache:
            c_db_mt, c_wal_mt, c_path = self._cache[rel_key]
            if c_db_mt == db_mtime and c_wal_mt == wal_mtime and os.path.exists(c_path):
                return c_path

        tmp_path = self._cache_path(rel_key)
        enc_key = bytes.fromhex(key_info["enc_key"])
        temp_path = f"{tmp_path}.{os.getpid()}.tmp"
        try:
            full_decrypt(db_path, temp_path, enc_key)
            if os.path.exists(wal_path):
                decrypt_wal(wal_path, temp_path, enc_key)
            self._install_cache_file(temp_path, tmp_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        self._cache[rel_key] = (db_mtime, wal_mtime, tmp_path)
        self._save_persistent_cache()
        return tmp_path

    def cleanup(self):
        self._save_persistent_cache()
