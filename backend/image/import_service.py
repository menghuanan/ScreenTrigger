# -*- coding: utf-8 -*-
"""图片导入与引用服务（TASK-009，M01 ``import_image``）。

职责：token 化导入 PNG/JPEG → 校验（扩展名与内容双重、≤50MiB、≤40,000,000 像素、
宽高各≤16384）→ EXIF 方向应用到派生图 → 登记到会话级图像注册表（产出 ImageRef）
→ 按 storage_mode 产出资产（``original`` 保留原字节 / ``png_lossless`` 无损 PNG），
资产按 sha256 命名（为 TASK-010 的 ``images/<sha256>.<ext>`` 布局预留）。

非目标（TASK-010）：不落盘、不建 Repository/全局 ImageStore、不做 generation。
脚本**不得**传任意路径：只能出示文件选择器签发的一次性 ``import_token``。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from ..capture.errors import CaptureError, invalid_image, invalid_params, resource_limit
from ..capture.ports import RawImage, default_wall_clock, format_utc_timestamp
from ..types.region import ImageRef, Point

from .decode_common import decode_image_to_bgr
from .formats import (
    FORMAT_EXTENSIONS,
    detect_format,
    extension_format,
    header_dimensions,
)
from .store import FrameMeta, SessionImageStore
from .tokens import ResolvedImport

# 06：导入边界
MAX_FILE_BYTES = 50 * 1024 * 1024  # ≤50MiB
MAX_PIXELS = 40_000_000  # 解码 ≤40,000,000 像素
MAX_EDGE = 16384  # 宽高各 ≤16384
STORAGE_MODES = ("original", "png_lossless")


@dataclass(frozen=True)
class ImportedAsset:
    """02 ImageAsset：数据根相对路径 + 资产字节 sha256 + 尺寸 + 格式 + storage_mode。"""

    path: str
    sha256: str
    width: int
    height: int
    format: str
    storage_mode: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "storage_mode": self.storage_mode,
        }


def _default_file_reader(path: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    """缺省读文件：二进制读取（可注入替身以避免真实 IO）。"""
    with open(path, "rb") as handle:
        return handle.read(max_bytes + 1)


class ImageImportService:
    """token 化图片导入服务（格式化校验 + 方向 + 资产 + ImageRef 所有权）。"""

    def __init__(
        self,
        session_id: str,
        store: SessionImageStore,
        token_authority: object,
        file_reader: Optional[Callable[[str], bytes]] = None,
        storage_mode: str = "original",
        logger: Optional[Callable[[str], None]] = None,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_pixels: int = MAX_PIXELS,
        max_edge: int = MAX_EDGE,
        clock: Optional[Callable[[], object]] = None,
        frame_id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id 不能为空")
        if storage_mode not in STORAGE_MODES:
            raise ValueError("storage_mode 必须为 %s 之一" % "/".join(STORAGE_MODES))
        self._session_id = session_id
        self._store = store
        self._authority = token_authority
        self._file_reader = file_reader
        self._storage_mode = storage_mode
        self._logger = logger
        self._max_file_bytes = int(max_file_bytes)
        self._max_pixels = int(max_pixels)
        self._max_edge = int(max_edge)
        # TASK-014：导入图登记帧元数据（image_pixel/origin=(0,0)/gr=0/binding=null）。
        self._clock: Callable[[], object] = clock if clock is not None else default_wall_clock
        self._frame_id_factory: Callable[[], str] = (
            frame_id_factory if frame_id_factory is not None else self._default_frame_id
        )

    @staticmethod
    def _default_frame_id() -> str:
        return "frame-" + uuid.uuid4().hex

    @property
    def storage_mode(self) -> str:
        return self._storage_mode

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    # -- 主入口 ---------------------------------------------------------------

    def import_image(self, import_token: str) -> Tuple[ImageRef, ImportedAsset]:
        """消费 token 导入图片，返回 (ImageRef, ImportedAsset)。

        Raises:
            CaptureError: INVALID_PARAMS / INVALID_IMAGE / IMAGE_EXPIRED /
                RESOURCE_LIMIT / ENGINE_UNAVAILABLE（06）。
        """
        if not isinstance(import_token, str) or not import_token:
            raise invalid_params("import_token", "import_token 必须为非空字符串")

        resolved: ResolvedImport = self._authority.consume(import_token, self._session_id)
        data = self._read_file(resolved)

        image_format = self._validate_format(data, resolved)
        width, height = self._check_budget_from_header(data, image_format)

        bgr, out_w, out_h = decode_image_to_bgr(data, image_format)

        # TASK-014：导入图按 02 明文登记 image_pixel / origin=(0,0) / gr=0 / binding=null
        # （**不得**成 screen_physical）；frame_id/captured_at 与像素一并登记，供
        # FrameResolver 真实还原且与消费方一致。
        frame_meta = FrameMeta(
            space="image_pixel",
            origin=Point(0, 0),
            geometry_revision=0,
            binding_id=None,
            captured_at=format_utc_timestamp(self._clock()),
            frame_id=self._frame_id_factory(),
        )
        asset = self._build_asset(data, bgr, image_format, out_w, out_h)
        image_ref = self._store.put(
            RawImage(width=out_w, height=out_h, data=bgr), image_format,
            frame_meta=frame_meta, original_bytes=data,
        )
        self._log(
            "import ok image_id=%s %dx%d format=%s mode=%s asset=%s"
            % (image_ref.image_id, out_w, out_h, image_format,
               self._storage_mode, asset.path)
        )
        return image_ref, asset

    # -- 内部步骤 -------------------------------------------------------------

    def _read_file(self, resolved: ResolvedImport) -> bytes:
        try:
            data = (self._file_reader(resolved.path) if self._file_reader is not None
                    else _default_file_reader(resolved.path, self._max_file_bytes))
        except CaptureError:
            raise
        except Exception as exc:
            raise invalid_image("无法读取所选文件: %s" % exc) from exc
        if not isinstance(data, (bytes, bytearray)):
            raise invalid_image("文件读取结果不是字节")
        data = bytes(data)
        if len(data) == 0:
            raise invalid_image("文件为空")
        if len(data) > self._max_file_bytes:
            raise resource_limit(
                "文件超过 %d 字节上限（实际 %d）" % (self._max_file_bytes, len(data))
            )
        return data

    def _validate_format(self, data: bytes, resolved: ResolvedImport) -> str:
        ext_format = extension_format(resolved.display_name or resolved.path)
        if ext_format is None:
            raise invalid_image("仅支持 PNG/JPEG（扩展名不受支持）", field="format")
        content_format = detect_format(data)
        if content_format is None:
            raise invalid_image("无法识别图像内容（非法格式）", field="format")
        if content_format != ext_format:
            raise invalid_image(
                "扩展名与内容不符（伪装扩展名：扩展名=%s 内容=%s）"
                % (ext_format, content_format),
                field="format",
            )
        return content_format

    def _check_budget_from_header(self, data: bytes, image_format: str) -> Tuple[int, int]:
        """解码前先检查像素预算（06）。"""
        try:
            width, height = header_dimensions(data, image_format)
        except ValueError as exc:
            raise invalid_image("图像头损坏: %s" % exc, field="format") from exc
        if width > self._max_edge or height > self._max_edge:
            raise resource_limit(
                "宽高超过 %d 上限（%dx%d）" % (self._max_edge, width, height)
            )
        if width * height > self._max_pixels:
            raise resource_limit(
                "像素超过 %d 上限（%dx%d=%d）"
                % (self._max_pixels, width, height, width * height)
            )
        return width, height

    def _build_asset(
        self, data: bytes, bgr: bytes, image_format: str, width: int, height: int
    ) -> ImportedAsset:
        from .png import encode_png_bgr

        if self._storage_mode == "png_lossless":
            asset_bytes = encode_png_bgr(bgr, width, height)
            asset_format, ext = "png", "png"
        else:  # original：保留原始字节
            asset_bytes = data
            asset_format = image_format
            ext = FORMAT_EXTENSIONS[image_format]
        sha256 = hashlib.sha256(asset_bytes).hexdigest()
        return ImportedAsset(
            path="images/%s.%s" % (sha256, ext),
            sha256=sha256,
            width=width,
            height=height,
            format=asset_format,
            storage_mode=self._storage_mode,
        )


def build_import_service(
    session_id: str,
    store: SessionImageStore,
    token_authority: object,
    storage_mode: str = "original",
    logger: Optional[Callable[[str], None]] = None,
) -> ImageImportService:
    """便捷构造（供 main.py / 测试注入）。"""
    return ImageImportService(
        session_id=session_id,
        store=store,
        token_authority=token_authority,
        storage_mode=storage_mode,
        logger=logger,
    )
