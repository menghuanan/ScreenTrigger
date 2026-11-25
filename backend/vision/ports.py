# -*- coding: utf-8 -*-
"""Vision 端口层（TASK-013 §4.1；可注入端口 + 只读适配器）。

逐字对齐 00 的 ``RecognitionProvider`` Protocol；并为本基座实现补充：

- ``ProviderFactory = Callable[[PixelReader], RecognitionProvider]``：Provider 工厂
  **只**注入只读 ``PixelReader``（拿不到 Repository 写句柄 / Input / UI）；
- ``PixelReader``：只读像素访问（Provider 仅得此端口；**无** ``put``/``release``/``persist``）；
- ``TargetSnapshotProvider``：只读目标快照（Vision **不**直接 import targets/repository；
  具体 ``Repository`` 仅由组合根 ``main.py`` 包装注入）；
- ``ConditionEvaluator``：条件树求值端口（TASK-016 实现；本基座不注入 → 诚实
  ``CAPABILITY_DENIED``）；
- ``FrameResolver``：``image_id → Frame`` 元数据解析（Q3：缺省 identity，``space=image_pixel``、
  ``origin=(0,0)``、``geometry_revision=0``、``binding_id=null``，**严禁伪造 screen_physical**）。

只读适配器（``SessionPixelReader``/``RepositoryTargetSnapshotProvider``）对下层异常做
**鸭子类型**归一（不 import capture/targets，守 00 允许依赖）：任何带 ``code`` 的异常
→ ``RecognitionError``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

from ..ipc.envelope import format_timestamp
from ..types.region import Frame, ImageRef, Point
from .cancel import CancelToken
from .types import ProviderResult, RecognitionError

#: Provider 工厂：**只**注入只读像素端口（隔离保证的结构性手段）。
ProviderFactory = Callable[["PixelReader"], "RecognitionProvider"]


@runtime_checkable
class RecognitionProvider(Protocol):
    """00 Provider 接口（逐字对齐；识别**只返回数据、不执行动作**）。"""

    def capabilities(self) -> Any:
        """返回本 Provider 的 ``ProviderManifest``（不触 IO / 不初始化）。"""
        ...

    def initialize(self, model_dir: Any) -> None:
        """加载**显式已安装**模型；``model_dir`` 为数据根 ``models/`` 绝对路径（只读、禁联网）。"""
        ...

    def recognize(self, frame: Frame, params: Dict[str, Any], cancel: CancelToken) -> ProviderResult:
        """执行一次推理；只返回 ``ProviderResult``；协作式取消点调用 ``cancel.throw_if_cancelled()``。"""
        ...

    def close(self) -> None:
        """释放模型/句柄；幂等。"""
        ...


@dataclass(frozen=True)
class RawPixels:
    """只读像素快照（BGR uint8 行主序；来自 ``SessionImageStore``）。"""

    image_id: str
    width: int
    height: int
    sha256: str
    format: str
    bgr: bytes


@runtime_checkable
class PixelReader(Protocol):
    """只读像素访问（会话所有权由底层 ImageStore 校验）。"""

    def read(self, image_id: str) -> RawPixels:
        """按 ``image_id`` 读取只读像素；过期/跨会话 → ``IMAGE_EXPIRED``。"""
        ...


@dataclass(frozen=True)
class TargetSnapshot:
    """只读目标快照（不可变；Vision 据此外部化条件树求值）。

    Attributes:
        image: 目标模板 ``ImageAsset``（02:85「模板来自 Target.image」）；TASK-016 **加法式**
            新增可空字段。为 ``Any``（不 import storage/targets，保持 Vision 边界）；由
            ``TargetImageResolver`` 物化为会话内 ``ImageRef``。缺省 ``None``（旧构造兼容）。
    """

    target_id: str
    sample_no: Optional[int]
    revision: Optional[int]
    anchor_condition_id: Optional[str]
    conditions: Optional[Dict[str, Any]]
    image: Optional[Any] = None


@runtime_checkable
class TargetSnapshotProvider(Protocol):
    """只读目标快照端口（Vision 不读可变目标库；实现包 ``Repository.read_snapshot``）。"""

    def snapshot(self, target_id: str) -> TargetSnapshot:
        """返回不可变快照；不存在 → ``TARGET_NOT_FOUND``。"""
        ...


@runtime_checkable
class TargetImageResolver(Protocol):
    """``ImageAsset → ImageRef``（TASK-016）：把目标模板像素物化进会话 ``SessionImageStore``。

    组合根（``main.py``）实现：读资产 PNG → 解码 BGR → ``SessionImageStore.put`` → ``ImageRef``，
    并按 ``sha256`` 会话内**有界缓存**（同一模板复用同一 ``ImageRef``，避免重复解码）。
    Provider 只需 ``ImageRef`` 即可经只读 ``PixelReader`` 读到模板像素。
    """

    def resolve(self, asset: Any) -> ImageRef:
        """把 ``ImageAsset`` 物化为会话 ``ImageRef``（读失败/损坏 → ``DATA_CORRUPT``/``IMAGE_EXPIRED``）。"""
        ...


@runtime_checkable
class ConditionEvaluator(Protocol):
    """条件树求值端口（TASK-016 实现；基座不注入 → 诚实 ``CAPABILITY_DENIED``）。"""

    def evaluate(self, frame: Frame, snapshot: Any, cancel: CancelToken):
        """求值条件树，返回 ``RecognitionResult``（或等价 dict）。"""
        ...


@runtime_checkable
class FrameResolver(Protocol):
    """``image_id → Frame`` 元数据解析端口（Q3）。"""

    def resolve(self, image_ref: ImageRef) -> Frame:
        """解析帧（含 space/origin/geometry_revision/binding_id）。"""
        ...


# ---------------------------------------------------------------------------
# 只读适配器（供组合根 main.py 包装共享 store/repository 注入）
# ---------------------------------------------------------------------------

class SessionPixelReader:
    """``PixelReader`` 实现：包裹共享 ``SessionImageStore``（**只读**）。

    对下层异常做鸭子类型归一（不 import capture）：带 ``code`` 的异常 → ``RecognitionError``。
    本类**只暴露** ``read``；不暴露 ``put``/``release``/``persist``（Provider 无写句柄）。
    """

    def __init__(self, store: Any) -> None:
        if store is None:
            raise ValueError("store 不能为空")
        self._store = store

    def read(self, image_id: str) -> RawPixels:
        if not isinstance(image_id, str) or not image_id:
            raise RecognitionError("INVALID_PARAMS", "image_id 必填", field="image_id")
        try:
            stored = self._store.get(image_id)
        except Exception as exc:  # 鸭子类型归一（CaptureError/IMAGE_EXPIRED 等）
            code = getattr(exc, "code", "IMAGE_EXPIRED")
            details = getattr(exc, "details", None) or {}
            field = details.get("field") if isinstance(details, dict) else None
            raise RecognitionError(
                code,
                str(exc),
                field=field or "image_id",
                retryable=bool(getattr(exc, "retryable", False)),
            ) from exc
        return RawPixels(
            image_id=str(stored.image_id),
            width=int(stored.width),
            height=int(stored.height),
            sha256=str(stored.sha256),
            format=str(stored.format),
            bgr=bytes(stored.data),
        )


class RepositoryTargetSnapshotProvider:
    """``TargetSnapshotProvider`` 实现：包裹共享 ``Repository``（**只读**）。

    只调用 ``read_snapshot()``（不 commit / 不写）；目标不存在 → ``TARGET_NOT_FOUND``。
    """

    def __init__(self, repository: Any, logger: Optional[Callable[[str], None]] = None) -> None:
        if repository is None:
            raise ValueError("repository 不能为空")
        self._repository = repository
        self._logger = logger

    def snapshot(self, target_id: str) -> TargetSnapshot:
        if not isinstance(target_id, str) or not target_id:
            raise RecognitionError("INVALID_PARAMS", "target_id 必填", field="target_id")
        return self.from_catalog(self._repository.read_snapshot(), target_id)

    @staticmethod
    def from_catalog(snapshot, target_id: str) -> TargetSnapshot:
        for target in getattr(snapshot, "targets", ()) or ():
            if target.get("id") == target_id:
                return TargetSnapshot(
                    target_id=target_id,
                    sample_no=target.get("sample_no"),
                    revision=target.get("revision"),
                    anchor_condition_id=target.get("anchor_condition_id"),
                    conditions=target.get("conditions"),
                    image=target.get("image"),
                )
        raise RecognitionError("TARGET_NOT_FOUND", "目标不存在: %s" % target_id,
                               field="target_id")


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class IdentityFrameResolver:
    """缺省 ``FrameResolver``：只返回 **02 合法**的 identity 元数据（Q3）。

    未知元数据**只允许**：``space="image_pixel"``、``origin=(0,0)``、``geometry_revision=0``、
    ``binding_id=null``；**严禁伪造 ``screen_physical``**（真实坐标空间需上层补齐）。
    """

    def __init__(self, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._clock: Callable[[], datetime] = clock if clock is not None else _default_clock

    def resolve(self, image_ref: ImageRef) -> Frame:
        return Frame(
            frame_id="frame-img-" + image_ref.image_id,
            image_ref=image_ref,
            space="image_pixel",
            origin=Point(0, 0),
            captured_at=format_timestamp(self._clock()),
            geometry_revision=0,
            binding_id=None,
        )


class StoreFrameResolver:
    """``image_id → Frame``：优先用 store 登记的帧元数据；未登记 → identity 兜底（TASK-014）。

    **鸭子类型**读同一 ``SessionImageStore``（调用 ``get_frame``，不 import ``image.store``，
    守 00 允许依赖表——不引入 ``Vision→Capture``）：

    - 已登记 → 用真实 ``space/origin/geometry_revision/binding_id``（含 window 的真实
      ``origin`` 与真实 ``geometry_revision``，未绑定 ``geometry_revision=0``/``binding_id=null``）；
    - 未登记/已释放/无 ``get_frame`` → **诚实回退** ``image_pixel``（``origin=(0,0)``、
      ``geometry_revision=0``、``binding_id=null``）；**严禁伪造 ``screen_physical``**。
    """

    def __init__(self, store: Any, clock: Optional[Callable[[], datetime]] = None) -> None:
        if store is None:
            raise ValueError("store 不能为空")
        self._store = store
        self._clock = clock

    def resolve(self, image_ref: ImageRef) -> Frame:
        meta = None
        get_frame = getattr(self._store, "get_frame", None)
        if callable(get_frame):
            meta = get_frame(image_ref.image_id)  # 未登记/已释放 → None
        if meta is None:
            return IdentityFrameResolver(clock=self._clock).resolve(image_ref)
        return Frame(
            frame_id=meta.frame_id,
            image_ref=image_ref,
            space=meta.space,
            origin=meta.origin,
            captured_at=meta.captured_at,
            geometry_revision=meta.geometry_revision,
            binding_id=meta.binding_id,
        )
