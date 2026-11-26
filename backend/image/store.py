# -*- coding: utf-8 -*-
"""会话级图像注册表（TASK-007，ImageRef 生命周期的提供者）。

M01 捕获与 M08 IPC 的 ImageRef 由本模块托管。它是 TASK-010 正式 **ImageStore**
（全局 256MiB 字节 LRU、跨会话资产、持久化）的**子集**：只提供会话内
``put/get/release``、会话所有权、像素预算与预览，并预留扩展接口，不提前实现
TASK-010 的全局缓存/回收/落盘。

语义（02/03/06）：

- 内部图像固定 **BGR uint8**；``ImageRef.format`` 为 ``"png"|"jpeg"``，本轮只产出
  PNG（JPEG 编码不在标准库，导入 jpeg 属 TASK-009）；
- ``sha256`` 为规范化像素内容（BGR 字节）的哈希，确定性、与编码无关；
- 引用**只在当前引擎会话有效**：``get``/``release`` 校验会话所有权，跨会话/已释放
  引用一律 ``IMAGE_EXPIRED``（不是静默成功）；
- ``release`` 幂等：同一会话重复释放返回 True；未知/跨会话引用 ``IMAGE_EXPIRED``；
- 字节预算：累计 BGR 字节超预算抛 ``RESOURCE_LIMIT``（06：超限返回资源错误；
  字节 LRU 淘汰属 TASK-010）；
- **帧位置元数据（TASK-014）**：``put(..., frame_meta=...)`` 可选登记 ``FrameMeta``
  （``image_id → {space,origin,geometry_revision,binding_id,captured_at,frame_id}``），
  ``get_frame`` 只读还原；与像素**同一 owner**、随 ``release``/``close`` 一并清理。
  未登记 → 调用方诚实回退 ``image_pixel``（严禁伪造 ``screen_physical``）。
"""

from __future__ import annotations

import base64
import hashlib
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from ..types.region import ImageRef, Point

from .png import PREVIEW_MAX_BYTES, render_preview_png

# ImageRef.format 冻结枚举（02）
IMAGE_FORMATS = ("png", "jpeg")


@dataclass(frozen=True)
class StoredImage:
    """已登记图像（只读快照）；``data`` 为 BGR uint8 行主序字节。"""

    image_id: str
    width: int
    height: int
    format: str
    sha256: str
    data: bytes
    owner: str
    original_bytes: Optional[bytes] = None


@dataclass(frozen=True)
class FrameMeta:
    """帧位置元数据（TASK-014，与像素**同一 owner**；02 ``Frame`` 的可还原子集）。

    会话像素注册表在登记像素时**同时**登记本元数据，使 ``image_id → Frame`` 可**真实
    还原**（无需把 ``Frame`` 每次构造后即丢弃）。未登记 → 调用方（``FrameResolver``）
    诚实回退 ``image_pixel``（**严禁伪造 ``screen_physical``**）。

    Attributes:
        space: 坐标空间（``screen_physical``/``window_client_physical``/``image_pixel``）。
        origin: 区域左上角（screen 物理坐标；导入图 ``(0,0)``）。
        geometry_revision: 几何版本；未绑定为 ``0``。
        binding_id: 窗口绑定 id；未绑定为 ``None``。
        captured_at: RFC3339 UTC 时间戳（与 capture/ipc 同格式）。
        frame_id: 该 ``image_id`` 对应的 ``frame_id``（保证 ``RecognitionResult.frame_id``
            与 ``capture_*`` 响应一致）。
    """

    space: str
    origin: Point
    geometry_revision: int
    binding_id: Optional[str]
    captured_at: str
    frame_id: str


class SessionImageStore:
    """会话级图像注册表（线程安全；put/get/release/preview）。"""

    DEFAULT_BUDGET_BYTES: int = 256 * 1024 * 1024  # 06 建议 256MiB
    DEFAULT_RELEASED_CAPACITY: int = 10000  # released 墓碑上限（防无界增长）

    def __init__(
        self,
        session_id: str,
        budget_bytes: Optional[int] = None,
        logger: Optional[Callable[[str], None]] = None,
        image_id_factory: Optional[Callable[[], str]] = None,
        released_capacity: Optional[int] = None,
        compression_level: Optional[Callable[[], int]] = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id 不能为空")
        self._session_id = session_id
        self._compression_level = compression_level
        self._budget = budget_bytes if budget_bytes is not None else self.DEFAULT_BUDGET_BYTES
        self._logger = logger
        self._image_id_factory = image_id_factory or self._default_image_id
        self._released_capacity = (
            released_capacity if released_capacity is not None
            else self.DEFAULT_RELEASED_CAPACITY
        )
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, StoredImage]" = OrderedDict()
        # 帧位置元数据：image_id -> FrameMeta（TASK-014，与像素同生命周期；
        # 随 release/close 一并清理，上界=现存像素条目数，**无界增长**不存在）。
        self._frames: Dict[str, FrameMeta] = {}
        # 已释放墓碑：image_id -> owner（会话）；同会话重复释放判幂等
        self._released: "OrderedDict[str, str]" = OrderedDict()
        self._bytes: int = 0
        self._closed = False

    # -- 基本信息 -------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def budget_bytes(self) -> int:
        return self._budget

    @property
    def png_compression_level(self) -> int:
        return self._compression_level() if self._compression_level else 6

    @staticmethod
    def _default_image_id() -> str:
        return "img-" + uuid.uuid4().hex

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    # -- 写入 -----------------------------------------------------------------

    def put(
        self,
        image: object,
        image_format: str = "png",
        *,
        frame_meta: Optional[FrameMeta] = None,
        original_bytes: Optional[bytes] = None,
    ) -> ImageRef:
        """登记一帧 BGR 图像，返回 ImageRef。

        Args:
            image: ``backend.capture.ports.RawImage``（duck-typed：width/height/data）。
            image_format: ``"png"`` 或 ``"jpeg"``（本轮只产出 png）。
            frame_meta: **可选**帧位置元数据（TASK-014）；``None`` 时不登记
                （等价旧行为，既有调用零影响）。登记后经 ``get_frame`` 真实还原。

        Raises:
            CaptureError: RESOURCE_LIMIT（超字节预算）或 INVALID_IMAGE（格式/缓冲非法）。
        """
        # 延迟 import 以避免 image <-> capture 顶层循环依赖。
        from ..capture.errors import image_expired, invalid_image, resource_limit

        if image_format not in IMAGE_FORMATS:
            raise invalid_image("不支持的图像格式: %s" % image_format, field="format")
        if frame_meta is not None and not isinstance(frame_meta, FrameMeta):
            raise ValueError("frame_meta 必须为 FrameMeta 或 None")
        width = int(getattr(image, "width"))
        height = int(getattr(image, "height"))
        data = bytes(getattr(image, "data"))
        if width <= 0 or height <= 0:
            raise invalid_image("图像尺寸必须为正")
        if len(data) != width * height * 3:
            raise invalid_image("BGR 缓冲区长度与 width*height*3 不符")

        size = len(data) + (len(original_bytes) if original_bytes is not None else 0)
        with self._lock:
            if self._closed:
                raise image_expired("图像会话已经关闭")
            if self._bytes + size > self._budget:
                raise resource_limit(
                    "图像字节预算超出：当前 %d + 新增 %d > 上限 %d"
                    % (self._bytes, size, self._budget)
                )
            image_id = self._image_id_factory()
            while image_id in self._entries:
                image_id = self._image_id_factory()
            sha256 = hashlib.sha256(data).hexdigest()
            stored = StoredImage(
                image_id=image_id,
                width=width,
                height=height,
                format=image_format,
                sha256=sha256,
                data=data,
                owner=self._session_id,
                original_bytes=original_bytes,
            )
            self._entries[image_id] = stored
            if frame_meta is not None:
                self._frames[image_id] = frame_meta
            self._bytes += size
        return ImageRef(
            image_id=image_id,
            width=width,
            height=height,
            format=image_format,
            sha256=sha256,
        )

    # -- 读取 -----------------------------------------------------------------

    def get_frame(self, image_id: str) -> Optional[FrameMeta]:
        """按 ``image_id`` 取帧位置元数据（TASK-014）。

        未登记/已释放 → ``None``（**不抛错**，供 ``FrameResolver`` 诚实回退
        ``image_pixel``）。只读，仅本会话可见（会话级注册表）。
        """
        with self._lock:
            return self._frames.get(image_id)

    def get(self, image_id: str) -> StoredImage:
        """按 ID 取图像；过期/跨会话引用抛 ``IMAGE_EXPIRED``。"""
        from ..capture.errors import image_expired

        with self._lock:
            entry = self._entries.get(image_id)
        if entry is None or entry.owner != self._session_id:
            raise image_expired()
        return entry

    def has(self, image_id: str) -> bool:
        """当前会话是否仍持有该引用（不抛错，供命令层快速判定）。"""
        with self._lock:
            entry = self._entries.get(image_id)
        return entry is not None and entry.owner == self._session_id

    def release(self, image_id: str) -> bool:
        """释放引用；幂等且校验所有权。

        Returns:
            True：本次释放成功或同会话重复释放（幂等）。

        Raises:
            CaptureError: IMAGE_EXPIRED（未知/跨会话引用；不是静默成功）。
        """
        from ..capture.errors import image_expired

        with self._lock:
            entry = self._entries.get(image_id)
            if entry is not None:
                if entry.owner != self._session_id:
                    raise image_expired()
                del self._entries[image_id]
                self._frames.pop(image_id, None)
                self._bytes -= len(entry.data)
                if entry.original_bytes is not None:
                    self._bytes -= len(entry.original_bytes)
                self._released[image_id] = entry.owner
                while len(self._released) > self._released_capacity:
                    self._released.popitem(last=False)
                return True
            owner = self._released.get(image_id)
            if owner == self._session_id:
                return True  # 幂等
            raise image_expired()

    def preview(self, image_id: str, max_edge: int) -> Tuple[str, str]:
        """生成预览：返回 ``(mime, base64)``；编码 ≤512KiB（03）。

        Raises:
            CaptureError: IMAGE_EXPIRED（引用无效）。
        """
        entry = self.get(image_id)
        png, _w, _h = render_preview_png(entry.data, entry.width, entry.height, max_edge,
                                          self.png_compression_level)
        if len(png) > PREVIEW_MAX_BYTES:  # 防御：render 已保证，除非 max_edge=1 仍超
            from ..capture.errors import resource_limit

            raise resource_limit("预览编码超过 512KiB 上限")
        return "image/png", base64.b64encode(png).decode("ascii")

    # -- 生命周期 -------------------------------------------------------------

    def close(self) -> None:
        """会话结束：清空全部引用（内存回收；不落盘）。"""
        with self._lock:
            self._closed = True
            self._entries.clear()
            self._frames.clear()
            self._released.clear()
            self._bytes = 0
