# -*- coding: utf-8 -*-
"""TASK-009 测试替身与图像构造工具（单测不依赖真实 UI/真实屏幕）。

- ``FakeTokenAuthority`` / ``FakeFileReader``：注入式替身；
- ``HmacTokenAuthority`` + ``mint_import_token``：真实 token 语义（一次性/过期/跨会话）；
- PNG 构造：真彩（复用编码器）、灰度、调色板、16 位、带 CRC 篡改的坏图；
- JPEG 构造：经 Pillow（可选依赖），未安装时相关用例 ``skip``。
"""

from __future__ import annotations

import struct
import zlib
from typing import Callable, Dict, Optional

from backend.image.formats import PNG_SIGNATURE
from backend.image.png import encode_png_bgr
from backend.image.tokens import ResolvedImport

try:  # 可选：仅 JPEG 用例需要
    import PIL.Image  # noqa: F401
    from PIL import Image

    _PILLOW = True
except Exception:  # pragma: no cover - 依赖环境
    Image = None  # type: ignore
    _PILLOW = False


def pillow_available() -> bool:
    return _PILLOW


class FakeTokenAuthority:
    """token → 路径 的固定映射替身；未知 token 抛 IMAGE_EXPIRED。"""

    def __init__(self, mapping: Dict[str, str]) -> None:
        self._mapping = dict(mapping)
        self.calls: list = []

    def consume(self, token: str, session_id: str) -> ResolvedImport:
        self.calls.append((token, session_id))
        if token not in self._mapping:
            from backend.capture.errors import image_expired

            raise image_expired("未知 token（替身）")
        path = self._mapping[token]
        return ResolvedImport(path=path, display_name=path.replace("\\", "/").rsplit("/", 1)[-1])


class FakeFileReader:
    """path → bytes 的替身；未登记路径抛 FileNotFoundError。"""

    def __init__(self, files: Dict[str, bytes]) -> None:
        self._files = dict(files)
        self.read_paths: list = []

    def __call__(self, path: str) -> bytes:
        self.read_paths.append(path)
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    def register(self, path: str, data: bytes) -> None:
        self._files[path] = data


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def png_truecolor(bgr: bytes, width: int, height: int) -> bytes:
    return encode_png_bgr(bgr, width, height)


def png_grayscale(gray: bytes, width: int, height: int, bit_depth: int = 8) -> bytes:
    """构造灰度 PNG（color_type=0，非隔行，filter=0）。"""
    if bit_depth == 8:
        row_bytes = width
        raw_rows = [bytes([0]) + gray[y * width:(y + 1) * width] for y in range(height)]
    elif bit_depth == 16:
        row_bytes = width * 2
        raw_rows = []
        for y in range(height):
            row = bytearray([0])
            for x in range(width):
                value = gray[y * width + x]
                row += struct.pack(">H", value * 257)
            raw_rows.append(bytes(row))
    else:
        raise ValueError("仅支持灰度位深 8/16")
    raw = b"".join(raw_rows)
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, 0, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def png_palette(indices: bytes, palette: list, width: int, height: int, bit_depth: int = 8) -> bytes:
    """构造调色板 PNG（color_type=3，非隔行，filter=0）。"""
    if bit_depth != 8:
        raise ValueError("仅支持调色板位深 8")
    plte = b"".join(bytes(entry) for entry in palette)
    raw = b"".join(
        bytes([0]) + indices[y * width:(y + 1) * width] for y in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, 3, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"PLTE", plte)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def corrupt_crc_png(width: int = 4, height: int = 4) -> bytes:
    """真彩 PNG，但 IHDR CRC 被破坏（坏图）。"""
    bgr = bytes(width * height * 3)
    good = encode_png_bgr(bgr, width, height)
    # 第 8+4+4+13 .. +4 是 IHDR 的 CRC；翻一个 bit
    crc_pos = 8 + 4 + 4 + 13
    tampered = bytearray(good)
    tampered[crc_pos] ^= 0xFF
    return bytes(tampered)


def truncated_png(width: int = 8, height: int = 8) -> bytes:
    bgr = bytes(width * height * 3)
    good = encode_png_bgr(bgr, width, height)
    return good[: len(good) // 2]


def jpeg_bytes(width: int, height: int, color=(200, 100, 50), quality: int = 85) -> bytes:
    """经 Pillow 生成 JPEG 字节（未安装 Pillow 时抛 RuntimeError，由用例 skip）。"""
    if not _PILLOW:
        raise RuntimeError("Pillow 未安装")
    import io

    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def jpeg_with_orientation(width: int, height: int, orientation: int) -> bytes:
    """生成带 EXIF Orientation 的 JPEG（Pillow 写 APP1）。"""
    if not _PILLOW:
        raise RuntimeError("Pillow 未安装")
    import io

    image = Image.new("RGB", (width, height), (10, 20, 30))
    exif = Image.Exif()
    exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def make_token(
    secret: bytes,
    session_id: str,
    path: str,
    ttl_seconds: int = 300,
    now: Optional[float] = None,
    nonce: Optional[str] = None,
) -> str:
    from backend.image.tokens import mint_import_token

    return mint_import_token(
        secret, session_id, path, ttl_seconds=ttl_seconds, now=now, nonce=nonce
    )


def make_service(
    session_id: str = "s1",
    authority=None,
    file_reader=None,
    storage_mode: str = "original",
    clock: Optional[Callable] = None,
    **kwargs,
):
    from backend.image.import_service import ImageImportService
    from backend.image.store import SessionImageStore

    store = SessionImageStore(session_id=session_id)
    if isinstance(authority, type(None)):
        authority = FakeTokenAuthority({})
    return ImageImportService(
        session_id=session_id,
        store=store,
        token_authority=authority,
        file_reader=file_reader,
        storage_mode=storage_mode,
        **kwargs,
    ), store
