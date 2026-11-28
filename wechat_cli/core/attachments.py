"""图片附件 — 列表、文件定位与解密。

支持四种本地图片文件形态：

- 明文图片：文件本身已经是 JPEG/PNG/GIF/WebP/HEIC。
- legacy XOR：首字节为异或密钥，其余字节逐字节异或。
- V1：``07 08 56 31 08 07`` 头，AES-128-ECB 固定密钥。
- V2：``07 08 56 32 08 07`` 头，AES 段 + 明文段 + XOR 段，密钥按账号派生。

V2 密钥只在本机派生并缓存到 ``~/.wechat-cli/image_key.json``，不会打印或上传。
"""

import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from datetime import datetime

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .config import STATE_DIR

V1_MAGIC = bytes.fromhex('070856310807')
V2_MAGIC = bytes.fromhex('070856320807')
V1_AES_KEY = b'cfcd208495d565ef'
IMAGE_KEY_CACHE = os.path.join(STATE_DIR, 'image_key.json')

IMAGE_KINDS = ('image', 'image_hd', 'thumbnail')
_KIND_SUFFIX = {'image': '', 'image_hd': '_h', 'thumbnail': '_t'}


class AttachmentError(Exception):
    """附件读取或解密失败。"""


class V2KeyError(AttachmentError):
    """V2 密钥不可用。消息以「V2 暂不支持」开头，便于调用方识别。"""


def detect_format(data):
    """按文件头识别图片格式。"""
    if not data:
        return 'unknown'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    if len(data) >= 12 and data[4:8] == b'ftyp':
        return 'heic'
    if data.startswith(b'wxgf'):
        return 'wxgf'
    return 'unknown'


def parse_packed_basename(packed_info):
    """从 packed_info_data 里取出 32 位十六进制文件名基。"""
    if not packed_info:
        return ''
    match = re.search(rb'([0-9a-fA-F]{32})', bytes(packed_info))
    return match.group(1).decode('ascii').lower() if match else ''


def parse_image_metadata(content):
    """解析图片消息 XML 里的 ``<img>`` 属性；没有则返回空字典。"""
    from .messages import _parse_xml_root, _parse_int

    root = _parse_xml_root(content)
    if root is None:
        return {}
    img = root.find('.//img')
    if img is None:
        return {}
    return {
        'md5': (img.get('md5') or '').strip(),
        'length': _parse_int(img.get('length')),
        'hdlength': _parse_int(img.get('hdlength')),
        'aeskey': (img.get('aeskey') or '').strip(),
    }


def attachment_dir(db_dir, chat_username):
    """返回某会话的图片附件目录。"""
    if not db_dir or not chat_username:
        return ''
    digest = hashlib.md5(chat_username.encode('utf-8')).hexdigest()
    return os.path.join(os.path.dirname(os.path.abspath(db_dir)), 'msg', 'attach', digest)


def attachment_files(db_dir, chat_username, create_time, basename):
    """返回 ``{kind: path}``，只包含磁盘上存在的文件。

    ``kind`` 为 ``image``（原图）、``image_hd``（高清）或 ``thumbnail``（缩略图）。
    """
    result = {}
    if not basename:
        return result
    base = attachment_dir(db_dir, chat_username)
    if not base:
        return result

    month = datetime.fromtimestamp(create_time).strftime('%Y-%m')
    month_dir = os.path.join(base, month, 'Img')
    dirs = [month_dir]
    if not os.path.isdir(month_dir):
        dirs = glob.glob(os.path.join(base, '*', 'Img'))

    def scan(folder):
        for kind, suffix in _KIND_SUFFIX.items():
            if kind in result:
                continue
            path = os.path.join(folder, basename + suffix + '.dat')
            if os.path.isfile(path):
                result[kind] = path

    for folder in dirs:
        scan(folder)
        if len(result) == len(IMAGE_KINDS):
            break

    if len(result) < len(IMAGE_KINDS):
        for folder in glob.glob(os.path.join(base, '*', 'Img')):
            if folder in dirs:
                continue
            scan(folder)
            if len(result) == len(IMAGE_KINDS):
                break
    return result


def best_kind(files):
    """按原图 > 高清 > 缩略图的顺序挑一个可用 kind。"""
    for kind in IMAGE_KINDS:
        if files.get(kind):
            return kind
    return ''


def _aligned_aes_size(size):
    """AES 段长度：对齐到 16 字节，且总留一个 PKCS#7 块。"""
    if size % 16:
        return size + (16 - size % 16)
    return size + 16


def _split_header(data):
    if len(data) < 15:
        raise AttachmentError(f'文件过短，不是有效的 .dat（{len(data)} 字节）')
    aes_size, xor_size = struct.unpack_from('<LL', data, 6)
    aes_len = _aligned_aes_size(aes_size)
    if 15 + aes_len > len(data):
        raise AttachmentError('AES 段长度超出文件大小，文件可能损坏')
    raw_end = len(data) - xor_size
    if raw_end < 15 + aes_len:
        raise AttachmentError('XOR 段长度超出文件大小，文件可能损坏')
    return aes_size, xor_size, aes_len, raw_end


def decode_dat(data, aes_key=None, xor_key=None):
    """解密 .dat 内容，返回 ``(bytes, format, decoder)``。

    ``aes_key`` 为 16 字节 ASCII 密钥，``xor_key`` 为 0-255 的整数。
    """
    if not data:
        raise AttachmentError('空文件')

    if data.startswith(V1_MAGIC) or data.startswith(V2_MAGIC):
        is_v1 = data.startswith(V1_MAGIC)
        key = V1_AES_KEY if is_v1 else aes_key
        if not key:
            raise V2KeyError('V2 暂不支持：没有可用的图像 AES 密钥')
        _, xor_size, aes_len, raw_end = _split_header(data)
        try:
            dec_aes = unpad(
                AES.new(key, AES.MODE_ECB).decrypt(data[15:15 + aes_len]),
                AES.block_size,
            )
        except ValueError as error:
            raise AttachmentError('AES 解密失败：密钥不匹配或文件损坏') from error

        raw = data[15 + aes_len:raw_end] if 15 + aes_len < raw_end else b''
        xor_data = data[raw_end:]
        if xor_data:
            if xor_key is None:
                raise AttachmentError('文件包含 XOR 段，但缺少 XOR 密钥')
            dec_xor = bytes(byte ^ xor_key for byte in xor_data)
        else:
            dec_xor = b''
        result = dec_aes + raw + dec_xor
        decoder = 'v1_aes' if is_v1 else 'v2_aes'
    else:
        if detect_format(data) != 'unknown':
            result, decoder = data, 'plain'
        else:
            xor_key_byte = data[0]
            result = bytes(byte ^ xor_key_byte for byte in data[1:])
            decoder = 'legacy_xor'

    fmt = detect_format(result)
    if fmt == 'unknown':
        raise AttachmentError('解密后无法识别图片格式')
    return result, fmt, decoder


# ---- V2 密钥派生 ----


def _find_v2_templates(account_dir, limit=3):
    base = os.path.join(account_dir, 'msg', 'attach')
    candidates = (
        glob.glob(os.path.join(base, '*', '*', 'Img', '*_t.dat'))
        + glob.glob(os.path.join(base, '*', '*', 'Img', '*.dat'))
    )
    templates = []
    for path in candidates:
        try:
            with open(path, 'rb') as handle:
                head = handle.read(31)
        except OSError:
            continue
        if len(head) >= 31 and head[:6] == V2_MAGIC:
            template = head[15:31]
            if template not in templates:
                templates.append(template)
                if len(templates) >= limit:
                    break
    return templates


def _infer_xor_key(account_dir, limit=60):
    """从 JPEG 缩略图末尾的 FF D9 反推单字节 XOR 密钥。"""
    base = os.path.join(account_dir, 'msg', 'attach')
    votes = {}
    total = 0
    for path in glob.glob(os.path.join(base, '*', '*', 'Img', '*_t.dat'))[:limit]:
        try:
            with open(path, 'rb') as handle:
                data = handle.read()
        except OSError:
            continue
        if len(data) < 32 or data[:6] != V2_MAGIC:
            continue
        key = data[-1] ^ 0xD9
        if (data[-2] ^ 0xFF) != key:
            continue
        total += 1
        votes[key] = votes.get(key, 0) + 1
    if not votes:
        return None
    key, count = max(votes.items(), key=lambda item: item[1])
    if count < 3 or count * 2 < total:
        return None
    return key


def _valid_aes_key(key, templates):
    if len(key) != 16:
        return False
    try:
        cipher = AES.new(key, AES.MODE_ECB)
        for template in templates:
            if detect_format(cipher.decrypt(template)) == 'unknown':
                return False
    except ValueError:
        return False
    return True


def _load_cached_key(account_dir, templates):
    try:
        with open(IMAGE_KEY_CACHE, encoding='utf-8') as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None
    if cached.get('account') != account_dir:
        return None
    try:
        aes_key = str(cached.get('aes_key') or '').encode('ascii')
        xor_key = int(cached.get('xor_key'))
    except (TypeError, ValueError):
        return None
    if 0 <= xor_key <= 0xFF and _valid_aes_key(aes_key, templates):
        return aes_key, xor_key
    return None


def _save_cached_key(account_dir, wxid, aes_key, xor_key):
    payload = {
        'account': account_dir,
        'wxid': wxid,
        'aes_key': aes_key.decode('ascii'),
        'xor_key': xor_key,
        'derived_at': int(datetime.now().timestamp()),
    }
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(IMAGE_KEY_CACHE, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _normalize_wxid(folder):
    match = re.match(r'^(.+)_([0-9a-fA-F]{4})$', folder)
    if match:
        return match.group(1), match.group(2).lower()
    return folder, ''


def derive_v2_keys(db_dir):
    """返回 ``{'aes_key': bytes, 'xor_key': int}``；失败时抛 V2KeyError。"""
    account_dir = os.path.dirname(os.path.abspath(db_dir))
    templates = _find_v2_templates(account_dir)
    if not templates:
        raise V2KeyError('V2 暂不支持：找不到可用于校验的本地图片样本')

    cached = _load_cached_key(account_dir, templates)
    if cached:
        return {'aes_key': cached[0], 'xor_key': cached[1]}

    folder = os.path.basename(account_dir)
    wxid, suffix = _normalize_wxid(folder)
    if not suffix:
        raise V2KeyError('V2 暂不支持：账号目录名缺少 4 位后缀，无法派生密钥')

    xor_key = _infer_xor_key(account_dir)
    if xor_key is None:
        raise V2KeyError('V2 暂不支持：无法从缩略图推断 XOR 密钥')

    for uin in range(xor_key, 1 << 32, 256):
        if hashlib.md5(str(uin).encode('utf-8')).hexdigest()[:4] != suffix:
            continue
        for candidate in (wxid, folder):
            aes_key = hashlib.md5(f'{uin}{candidate}'.encode('utf-8')).hexdigest()[:16].encode('ascii')
            if _valid_aes_key(aes_key, templates):
                _save_cached_key(account_dir, candidate, aes_key, xor_key)
                return {'aes_key': aes_key, 'xor_key': xor_key}

    raise V2KeyError('V2 暂不支持：未能从本机数据派生密钥')


def read_attachment_bytes(path):
    with open(path, 'rb') as handle:
        return handle.read()


CONVERTIBLE_FORMATS = ('jpg', 'jpeg', 'png')


def infer_output_format(output_path, source_format, convert=None):
    """判断是否需要转码以及目标格式；返回 ``None`` 表示按原格式写出。"""
    if convert:
        fmt = convert.lower()
        return 'jpg' if fmt == 'jpeg' else fmt
    extension = os.path.splitext(output_path)[1].lower().lstrip('.')
    if extension in CONVERTIBLE_FORMATS and extension != source_format:
        return 'jpg' if extension == 'jpeg' else extension
    return None


def convert_image_with_ffmpeg(data, source_format, target_format, timeout=120):
    """用本机 ffmpeg 把图片转成 jpg/png，返回转换后的字节。"""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise AttachmentError('需要 ffmpeg 转码，但 PATH 中找不到 ffmpeg；请安装后重试')

    workdir = tempfile.mkdtemp(prefix='wechat-cli-extract-')
    try:
        src_suffix = '.wxgf' if source_format == 'wxgf' else '.' + source_format
        src_path = os.path.join(workdir, 'input' + src_suffix)
        out_path = os.path.join(workdir, 'output.' + target_format)
        with open(src_path, 'wb') as handle:
            handle.write(data)

        command = [
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
            '-i', src_path, '-frames:v', '1',
        ]
        if target_format == 'jpg':
            command.extend(['-q:v', '2'])
        command.append(out_path)
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AttachmentError(f'ffmpeg 转码失败: {error}') from error
        if result.returncode != 0 or not os.path.isfile(out_path):
            detail = (result.stderr or '').strip().splitlines()
            message = detail[-1] if detail else f'退出码 {result.returncode}'
            raise AttachmentError(f'ffmpeg 转码失败: {message}')
        with open(out_path, 'rb') as handle:
            return handle.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def attachment_metadata(db_dir, chat_username, create_time, basename):
    """返回附件文件和声明信息，供列表展示。"""
    files = attachment_files(db_dir, chat_username, create_time, basename)
    kind = best_kind(files)
    size = os.path.getsize(files[kind]) if kind else None
    return {'files': files, 'kind': kind, 'size': size}
