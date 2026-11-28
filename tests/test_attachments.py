import hashlib
import os
import struct
import tempfile
import unittest
from datetime import datetime

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from wechat_cli.core.attachments import (
    AttachmentError,
    V1_AES_KEY,
    V1_MAGIC,
    V2KeyError,
    V2_MAGIC,
    attachment_files,
    best_kind,
    decode_dat,
    detect_format,
    parse_packed_basename,
)

JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 96 + b'\xff\xd9'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 112


def _v2_dat(plain, key, xor_key, aes_size=32):
    aes_part = plain[:aes_size]
    xor_part = plain[aes_size:]
    cipher = AES.new(key, AES.MODE_ECB).encrypt(pad(aes_part, AES.block_size))
    header = V2_MAGIC + struct.pack('<LL', len(aes_part), len(xor_part)) + b'\x00'
    return header + cipher + bytes(byte ^ xor_key for byte in xor_part)


def _v1_dat(plain):
    cipher = AES.new(V1_AES_KEY, AES.MODE_ECB).encrypt(pad(plain, AES.block_size))
    header = V1_MAGIC + struct.pack('<LL', len(plain), 0) + b'\x00'
    return header + cipher


class DecodeTests(unittest.TestCase):
    def test_plain_jpeg_passes_through(self):
        out, fmt, decoder = decode_dat(JPEG)
        self.assertEqual(out, JPEG)
        self.assertEqual((fmt, decoder), ('jpg', 'plain'))

    def test_legacy_xor_jpeg(self):
        data = bytes([0x5A]) + bytes(byte ^ 0x5A for byte in JPEG)
        out, fmt, decoder = decode_dat(data)
        self.assertEqual(out, JPEG)
        self.assertEqual((fmt, decoder), ('jpg', 'legacy_xor'))

    def test_v1_aes_png(self):
        out, fmt, decoder = decode_dat(_v1_dat(PNG))
        self.assertEqual(out, PNG)
        self.assertEqual((fmt, decoder), ('png', 'v1_aes'))

    def test_v2_aes_and_xor(self):
        key = b'0123456789abcdef'
        data = _v2_dat(JPEG, key, 0x69)
        out, fmt, decoder = decode_dat(data, aes_key=key, xor_key=0x69)
        self.assertEqual(out, JPEG)
        self.assertEqual((fmt, decoder), ('jpg', 'v2_aes'))

    def test_v2_without_key_says_unsupported(self):
        data = _v2_dat(JPEG, b'0123456789abcdef', 0x69)
        with self.assertRaises(V2KeyError) as caught:
            decode_dat(data)
        self.assertIn('V2 暂不支持', str(caught.exception))

    def test_derive_without_samples_says_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, 'db_storage')
            os.makedirs(db_dir)
            with self.assertRaises(V2KeyError) as caught:
                from wechat_cli.core.attachments import derive_v2_keys
                derive_v2_keys(db_dir)
            self.assertIn('V2 暂不支持', str(caught.exception))

    def test_unknown_format_raises(self):
        with self.assertRaises(AttachmentError):
            decode_dat(b'not an image at all, just bytes')

    def test_truncated_header_raises(self):
        with self.assertRaises(AttachmentError):
            decode_dat(V2_MAGIC + b'\x00' * 3)

    def test_detect_format_variants(self):
        self.assertEqual(detect_format(JPEG), 'jpg')
        self.assertEqual(detect_format(PNG), 'png')
        self.assertEqual(detect_format(b'GIF89a' + b'\x00' * 20), 'gif')
        self.assertEqual(detect_format(b'RIFF' + b'\x00' * 4 + b'WEBP' + b'\x00' * 8), 'webp')
        self.assertEqual(detect_format(b'\x00\x00\x00\x18ftypheic' + b'\x00' * 8), 'heic')
        self.assertEqual(detect_format(b'wxgf' + b'\x00' * 20), 'wxgf')
        self.assertEqual(detect_format(b'\x00\x01\x02'), 'unknown')


class PackedInfoTests(unittest.TestCase):
    def test_extracts_basename(self):
        packed = b'\x08\xf8\x04\x1a\x22\x22\x20' + b'12511f191d0fd6dd579dc40a320687bf' + b'\x58\x00'
        self.assertEqual(parse_packed_basename(packed), '12511f191d0fd6dd579dc40a320687bf')

    def test_empty_or_invalid(self):
        self.assertEqual(parse_packed_basename(b''), '')
        self.assertEqual(parse_packed_basename(b'no hash here'), '')


class AttachmentFileTests(unittest.TestCase):
    def test_locates_all_kinds_and_prefers_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, 'db_storage')
            os.makedirs(db_dir)
            chat = 'wxid_test'
            folder = os.path.join(
                tmp, 'msg', 'attach', hashlib.md5(chat.encode()).hexdigest(), '2026-11', 'Img'
            )
            os.makedirs(folder)
            base = 'a' * 32
            for suffix in ('', '_h', '_t'):
                open(os.path.join(folder, base + suffix + '.dat'), 'wb').close()

            files = attachment_files(
                db_dir, chat, datetime(2026, 11, 20, 12, 0).timestamp(), base
            )
            self.assertEqual(set(files), {'image', 'image_hd', 'thumbnail'})
            self.assertEqual(best_kind(files), 'image')

    def test_missing_files_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, 'db_storage')
            os.makedirs(db_dir)
            self.assertEqual(
                attachment_files(db_dir, 'wxid_test', datetime(2026, 11, 20).timestamp(), 'b' * 32),
                {},
            )


if __name__ == '__main__':
    unittest.main()
