"""配置加载 — 从 ~/.wechat-cli/ 读取自包含配置"""

import glob as glob_mod
import json
import os
import platform
import sys
from pathlib import Path

_SYSTEM = platform.system().lower()

if _SYSTEM == "linux":
    _DEFAULT_PROCESS = "wechat"
elif _SYSTEM == "darwin":
    _DEFAULT_PROCESS = "WeChat"
else:
    _DEFAULT_PROCESS = "Weixin.exe"

# CLI 状态目录
STATE_DIR = os.path.expanduser("~/.wechat-cli")
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")
KEYS_FILE = os.path.join(STATE_DIR, "all_keys.json")


def _choose_candidate(candidates):
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        if not sys.stdin.isatty():
            return candidates[0]
        print("[!] 检测到多个微信数据目录:")
        for i, c in enumerate(candidates, 1):
            print(f"    {i}. {c}")
        print("    0. 跳过")
        try:
            while True:
                choice = input(f"请选择 [0-{len(candidates)}]: ").strip()
                if choice == "0":
                    return None
                if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                    return candidates[int(choice) - 1]
                print("    无效输入")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
    return None


def _windows_documents_dir():
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Personal")
        return Path(os.path.expandvars(value))
    except (ImportError, OSError, TypeError):
        return Path.home() / "Documents"


def _windows_config_roots():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    config_dir = Path(appdata) / "Tencent" / "xwechat" / "config"
    for ini_file in sorted(config_dir.glob("*.ini")):
        try:
            with ini_file.open("rb") as file:
                raw = file.read(4096)
        except OSError:
            continue
        for encoding in ("utf-8-sig", "utf-16", "gbk"):
            try:
                value = raw.decode(encoding).strip().strip('"')
            except UnicodeError:
                continue
            if value and not any(char in value for char in "\n\r\x00"):
                path = Path(os.path.expanduser(os.path.expandvars(value)))
                if path.is_absolute() and path.is_dir():
                    yield path
                    break


def _detect_db_dirs_windows():
    roots = [*_windows_config_roots(), _windows_documents_dir(), Path.home() / "Documents"]
    seen = set()
    candidates = []
    for root in roots:
        if root.name.casefold() == "db_storage":
            matches = [root]
        else:
            base = root if root.name.casefold() == "xwechat_files" else root / "xwechat_files"
            matches = sorted(base.glob("*/db_storage"))
        for match in matches:
            normalized = os.path.normcase(os.path.realpath(match))
            if match.is_dir() and normalized not in seen:
                seen.add(normalized)
                candidates.append(str(match))
    return candidates


def _detect_db_dirs_linux():
    seen = set()
    candidates = []
    search_roots = [os.path.expanduser("~/Documents/xwechat_files")]
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd
        try:
            sudo_home = pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            sudo_home = None
        if sudo_home:
            fallback = os.path.join(sudo_home, "Documents", "xwechat_files")
            if fallback not in search_roots:
                search_roots.append(fallback)
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        pattern = os.path.join(root, "*", "db_storage")
        for match in glob_mod.glob(pattern):
            normalized = os.path.normcase(os.path.normpath(match))
            if os.path.isdir(match) and normalized not in seen:
                seen.add(normalized)
                candidates.append(match)
    old_path = os.path.expanduser("~/.local/share/weixin/data/db_storage")
    if os.path.isdir(old_path):
        normalized = os.path.normcase(os.path.normpath(old_path))
        if normalized not in seen:
            candidates.append(old_path)

    def _mtime(path):
        msg_dir = os.path.join(path, "message")
        target = msg_dir if os.path.isdir(msg_dir) else path
        try:
            return os.path.getmtime(target)
        except OSError:
            return 0
    candidates.sort(key=_mtime, reverse=True)
    return candidates


def _detect_db_dirs_macos():
    base = os.path.expanduser("~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")
    if not os.path.isdir(base):
        return []
    seen = set()
    candidates = []
    pattern = os.path.join(base, "*", "db_storage")
    for match in glob_mod.glob(pattern):
        normalized = os.path.normcase(os.path.normpath(match))
        if os.path.isdir(match) and normalized not in seen:
            seen.add(normalized)
            candidates.append(match)
    return candidates


def detect_db_dirs():
    if _SYSTEM == "windows":
        return _detect_db_dirs_windows()
    if _SYSTEM == "linux":
        return _detect_db_dirs_linux()
    if _SYSTEM == "darwin":
        return _detect_db_dirs_macos()
    return []


def auto_detect_db_dir():
    return _choose_candidate(detect_db_dirs())


def read_config_file(config_path):
    try:
        with open(config_path, encoding="utf-8") as file:
            cfg = json.load(file)
    except FileNotFoundError:
        return {}
    if not isinstance(cfg, dict):
        raise ValueError("配置文件必须包含 JSON 对象")
    return cfg


def resolve_config_paths(cfg, config_path):
    cfg = dict(cfg)
    state_dir = os.path.dirname(os.path.abspath(config_path))
    cfg.setdefault("keys_file", os.path.join(state_dir, "all_keys.json"))
    cfg.setdefault("decrypted_dir", os.path.join(state_dir, "decrypted"))
    cfg.setdefault("decoded_image_dir", os.path.join(state_dir, "decoded_images"))
    cfg.setdefault("wechat_process", _DEFAULT_PROCESS)

    for key in ("db_dir", "keys_file", "decrypted_dir", "decoded_image_dir"):
        if key in cfg:
            if key == "db_dir" and not cfg[key]:
                continue
            path = os.path.expanduser(cfg[key])
            cfg[key] = os.path.abspath(os.path.join(state_dir, path))

    db_dir = cfg.get("db_dir", "")
    if db_dir and os.path.basename(db_dir) == "db_storage":
        cfg["wechat_base_dir"] = os.path.dirname(db_dir)
    else:
        cfg["wechat_base_dir"] = db_dir
    return cfg


def load_config(config_path=None):
    """加载配置。默认从 ~/.wechat-cli/ 读取。"""
    config_path = config_path or CONFIG_FILE
    try:
        cfg = read_config_file(config_path)
    except json.JSONDecodeError:
        cfg = {}

    if not cfg.get("db_dir"):
        detected = auto_detect_db_dir()
        if not detected:
            raise FileNotFoundError(
                "未找到微信数据目录。\n"
                "请运行: wechat-cli doctor"
            )
        cfg["db_dir"] = detected
    return resolve_config_paths(cfg, config_path)
