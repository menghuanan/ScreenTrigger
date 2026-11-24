# -*- coding: utf-8 -*-
"""backend.image：会话级图像引用服务（TASK-007）与图片导入（TASK-009）。

- TASK-007：会话内 ImageRef 生命周期（put/get/release/preview、会话所有权、
  字节预算）与最小 PNG **编码**器；全局 256MiB LRU / 不可变资产 / 落盘属 TASK-010。
- TASK-009：``import_image`` 的 token 化导入（标准库 PNG 解码 + 可选 Pillow 的
  JPEG 解码 + EXIF 方向 + 资产 sha256 命名），复用同一 SessionImageStore。

循环依赖：本 barrel 只 import 纯 Python 模块（capture.errors 为叶子模块）；
``.commands`` 内对 capture.commands 的引用为**函数内延迟 import**，不构成顶层环。
"""

from .commands import register_host_frame_command, register_image_commands
from .exif import read_orientation
from .formats import (
    EXTENSION_FORMATS,
    FORMAT_EXTENSIONS,
    ImageFormatError,
    detect_format,
    extension_format,
    header_dimensions,
)
from .host_frame import (
    DEFAULT_HOST_FRAME_TTL_SECONDS,
    HOST_FRAME_TOKEN_PREFIX,
    HostFrameRegistrationService,
    HostFrameTokenAuthority,
    NullHostFrameAuthority,
    ResolvedHostFrame,
    mint_host_frame_token,
    resolve_temp_file,
    temp_dir_for,
)
from .import_service import (
    MAX_EDGE,
    MAX_FILE_BYTES,
    MAX_PIXELS,
    STORAGE_MODES,
    ImageImportService,
    ImportedAsset,
    build_import_service,
)
from .jpeg import JpegBackendUnavailableError, JpegDecodeError, pillow_available
from .pixelops import apply_orientation, bgr_to_rgb, rgb_to_bgr
from .png import (
    PNG_SIGNATURE,
    PREVIEW_MAX_BYTES,
    downsample_bgr,
    encode_png_bgr,
    render_preview_png,
)
from .png_decode import PngDecodeError, decode_png_to_rgb
from .store import IMAGE_FORMATS, FrameMeta, SessionImageStore, StoredImage
from .tokens import (
    DEFAULT_TTL_SECONDS,
    HmacTokenAuthority,
    NullTokenAuthority,
    ResolvedImport,
    mint_import_token,
)

__all__ = [
    "DEFAULT_HOST_FRAME_TTL_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "EXTENSION_FORMATS",
    "FORMAT_EXTENSIONS",
    "FrameMeta",
    "HOST_FRAME_TOKEN_PREFIX",
    "HmacTokenAuthority",
    "HostFrameRegistrationService",
    "HostFrameTokenAuthority",
    "IMAGE_FORMATS",
    "ImageFormatError",
    "ImageImportService",
    "ImportedAsset",
    "JpegBackendUnavailableError",
    "JpegDecodeError",
    "MAX_EDGE",
    "MAX_FILE_BYTES",
    "MAX_PIXELS",
    "NullHostFrameAuthority",
    "NullTokenAuthority",
    "PNG_SIGNATURE",
    "PREVIEW_MAX_BYTES",
    "PngDecodeError",
    "ResolvedHostFrame",
    "ResolvedImport",
    "STORAGE_MODES",
    "SessionImageStore",
    "StoredImage",
    "apply_orientation",
    "bgr_to_rgb",
    "build_import_service",
    "decode_png_to_rgb",
    "detect_format",
    "downsample_bgr",
    "encode_png_bgr",
    "extension_format",
    "header_dimensions",
    "mint_host_frame_token",
    "mint_import_token",
    "pillow_available",
    "read_orientation",
    "register_host_frame_command",
    "register_image_commands",
    "render_preview_png",
    "resolve_temp_file",
    "rgb_to_bgr",
    "temp_dir_for",
]
