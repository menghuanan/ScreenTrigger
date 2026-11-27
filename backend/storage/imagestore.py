# -*- coding: utf-8 -*-
"""会话级 ImageStore：复用/包裹 ``SessionImageStore`` + 不可变资产落盘（TASK-010，Q9）。

- **会话级**：``put/get/has/release/preview/close`` 直接委托既有的
  ``backend.image.store.SessionImageStore``（会话所有权 + 字节预算），**不另起一套**。
- **资产持久化**：``images/<sha256>.<ext>`` 内容寻址、不可变（重复写同内容跳过；
  内容不符 → DATA_CORRUPT）。``ImageRef`` **不持久化**（02）；持久化时转为
  ``ImageAsset``（``ImageRef``→``ImageAsset`` 转换在提交阶段完成）。
- 全局 256MiB LRU/GC 与孤儿回收**不在本任务**（Q2/Q9/Q11：只保留不回收）。
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from backend.image.formats import FORMAT_EXTENSIONS
from backend.capture.errors import CaptureError
from backend.image.decode_common import decode_image_to_bgr
from backend.image.png import render_preview_png
from backend.image.store import SessionImageStore, StoredImage
from backend.types.region import ImageAsset, ImageRef

from . import fsops
from .errors import StorageError, data_corrupt, invalid_params
from .generation import sha256_hex
from .paths import DataRoot, join_relative

ASSET_DIR: str = "images"

Logger = Callable[[str], None]


@dataclass(frozen=True)
class AssetWrite:
    """一次资产写请求（内容寻址后的落盘输入）。"""

    raw: bytes
    width: int
    height: int
    format: str
    storage_mode: str


class ImageStore:
    """会话级图像注册表（包裹 ``SessionImageStore``）+ 资产落盘。"""

    def __init__(
        self,
        session_store: SessionImageStore,
        data_root: DataRoot,
        *,
        logger: Optional[Logger] = None,
    ) -> None:
        if session_store is None:
            raise ValueError("session_store 不能为空")
        self._session = session_store
        self._root = data_root
        self._logger = logger

    # -- 会话级委托 ----------------------------------------------------------

    @property
    def session_store(self) -> SessionImageStore:
        return self._session

    @property
    def data_root(self) -> DataRoot:
        return self._root

    def put(self, image: object, image_format: str = "png") -> ImageRef:
        return self._session.put(image, image_format)

    def get(self, image_id: str) -> StoredImage:
        return self._session.get(image_id)

    def has(self, image_id: str) -> bool:
        return self._session.has(image_id)

    def release(self, image_id: str) -> bool:
        return self._session.release(image_id)

    def preview(self, image_id: str, max_edge: int) -> Tuple[str, str]:
        return self._session.preview(image_id, max_edge)

    def close(self) -> None:
        self._session.close()

    # -- 资产落盘 ------------------------------------------------------------

    def asset_relative_path(self, sha256: str, image_format: str) -> str:
        """资产数据根相对路径 ``images/<sha256>.<ext>``（ext 由格式映射）。"""
        ext = FORMAT_EXTENSIONS.get(image_format)
        if ext is None:
            raise invalid_params("format", "不支持的图像格式: %s" % image_format)
        return join_relative((ASSET_DIR, "%s.%s" % (sha256, ext)))

    def persist(self, write: AssetWrite) -> ImageAsset:
        """落盘一个不可变资产（内容寻址；同内容幂等）。"""
        if write.format not in FORMAT_EXTENSIONS:
            raise invalid_params("format", "不支持的图像格式: %s" % write.format)
        if write.width <= 0 or write.height <= 0:
            raise invalid_params("dimensions", "资产宽高必须为正")
        sha = sha256_hex(bytes(write.raw))
        relative = self.asset_relative_path(sha, write.format)
        absolute = self._root.resolve(relative)
        if os.path.isfile(absolute):
            existing = fsops.read_bytes(absolute)
            if sha256_hex(existing) != sha:
                raise data_corrupt("资产内容与文件名 hash 不符: %s" % relative, field="asset")
        else:
            fsops.write_atomic_bytes(absolute, bytes(write.raw))
            if self._logger is not None:
                self._logger("资产已落盘: %s" % relative)
        return ImageAsset(
            path=relative,
            sha256=sha,
            width=int(write.width),
            height=int(write.height),
            format=write.format,
            storage_mode=write.storage_mode,
        )

    def verify(self, asset: ImageAsset) -> None:
        """校验资产存在且 hash 与声明一致（F5：hash 不符 → DATA_CORRUPT）。"""
        self._read_verified(asset)

    def _read_verified(self, asset: ImageAsset) -> bytes:
        absolute = self._root.resolve(asset.path)
        if not os.path.isfile(absolute):
            raise data_corrupt("图像资产缺失: %s" % asset.path, field="image.path")
        data = fsops.read_bytes(absolute)
        if sha256_hex(data) != asset.sha256:
            raise data_corrupt("图像资产 hash 不符: %s" % asset.path, field="image.sha256")
        return data

    def asset_preview(self, asset: ImageAsset, max_edge: int) -> dict:
        data = self._read_verified(asset)
        try:
            pixels, width, height = decode_image_to_bgr(data, asset.format)
        except CaptureError as exc:
            raise StorageError(exc.code, exc.message, details=exc.details) from exc
        png, _, _ = render_preview_png(pixels, width, height, max_edge)
        return {"mime": "image/png", "base64": base64.b64encode(png).decode("ascii")}

    def remove_asset(self, asset: ImageAsset) -> None:
        """best-effort 删除资产（Q11：仅显式调用，不自动 GC）。"""
        try:
            absolute = self._root.resolve(asset.path)
        except Exception:
            return
        fsops.remove_file(absolute)
