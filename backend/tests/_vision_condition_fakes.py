# -*- coding: utf-8 -*-
"""TASK-016 条件树测试替身（ROI 感知 provider + 注册表/帧构造）。

- ``ScriptedProvider``：可编程 provider，**接受并记录** ``roi=None`` 关键字（TASK-016 加法式
  ROI 通道），``recognize`` 委托给注入的 ``recognize_fn(frame, params, cancel, roi)``；
- ``build_registry``：用替身 provider 组装 ``ProviderRegistry``（``initialize_all`` 后就绪）；
- ``make_frame`` / ``make_target_snapshot`` / ``make_draft``：确定性测试夹具。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from backend.types.capabilities import ProviderManifest
from backend.types.region import Frame, ImageRef, Point, Rect
from backend.vision.ports import TargetSnapshot
from backend.vision.providers.base import (
    BaseProvider,
    make_candidate,
    make_text_block,
)
from backend.vision.registry import ProviderRegistry
from backend.vision.types import ProviderResult

RecognizeFn = Callable[[Any, Dict[str, Any], Any, Any], ProviderResult]

_MANIFEST_BY_TYPE = {
    "template": "builtin.template",
    "contour": "builtin.contour",
    "color": "builtin.color",
    "ocr": "builtin.ocr",
}


def make_condition_manifest(condition_type: str, *, provider_id: Optional[str] = None,
                            model_ids: Sequence[str] = ()) -> ProviderManifest:
    return ProviderManifest(
        provider_id=provider_id or _MANIFEST_BY_TYPE[condition_type],
        api_version=1,
        provider_version="1.0.0",
        supported_condition_types=(condition_type,),
        model_ids=tuple(model_ids),
        supported_languages=("zh", "en") if condition_type == "ocr" else (),
        thread_safe=False,
        offline=True,
    )


class ScriptedProvider(BaseProvider):
    """记录 ``roi``/``params`` 的可编程 provider（测试替身）。"""

    def __init__(self, condition_type: str, recognize_fn: RecognizeFn,
                 *, model_ids: Sequence[str] = ()) -> None:
        super().__init__(make_condition_manifest(condition_type, model_ids=model_ids))
        self._fn = recognize_fn
        self.calls = 0
        self.rois: List[Any] = []
        self.params_seen: List[Dict[str, Any]] = []

    def initialize(self, model_dir: Any) -> None:
        return None

    def recognize(self, frame: Any, params: Dict[str, Any], cancel: Any,
                  roi: Any = None) -> ProviderResult:
        self.calls += 1
        self.rois.append(roi)
        self.params_seen.append(dict(params))
        cancel.throw_if_cancelled()
        return self._fn(frame, params, cancel, roi)


def provider_returning(condition_type: str, *, candidates: Sequence[Any] = (),
                       text_blocks: Sequence[Any] = ()) -> ScriptedProvider:
    """返回固定候选/文本块的 provider（忽略 roi）。"""
    def _fn(frame: Any, params: Dict[str, Any], cancel: Any, roi: Any) -> ProviderResult:
        return ProviderResult(candidates=tuple(candidates), text_blocks=tuple(text_blocks))
    return ScriptedProvider(condition_type, _fn)


def provider_echoing_roi(condition_type: str, *, size: int = 4) -> ScriptedProvider:
    """在 ROI 内回声一个候选（锚点 bbox = ROI 原点）——用于断言 ROI 传递与坐标。"""
    def _fn(frame: Any, params: Dict[str, Any], cancel: Any, roi: Any) -> ProviderResult:
        if roi is None:
            return ProviderResult(candidates=(make_candidate(0, 0, size, size, 0.9),))
        return ProviderResult(candidates=(make_candidate(roi.x, roi.y, size, size, 0.9),))
    return ScriptedProvider(condition_type, _fn)


def provider_always_matches(condition_type: str, *, size: int = 4,
                            confidence: float = 0.8) -> ScriptedProvider:
    """恒命中（在给定 roi 内回声一个候选；roi=None 时整帧原点）。"""
    def _fn(frame: Any, params: Dict[str, Any], cancel: Any, roi: Any) -> ProviderResult:
        if roi is None:
            return ProviderResult(candidates=(make_candidate(0, 0, size, size, confidence),))
        return ProviderResult(candidates=(make_candidate(roi.x, roi.y, size, size, confidence),))
    return ScriptedProvider(condition_type, _fn)


def provider_never_matches(condition_type: str) -> ScriptedProvider:
    """恒未命中（空候选 / 空文本块）。"""
    def _fn(frame: Any, params: Dict[str, Any], cancel: Any, roi: Any) -> ProviderResult:
        return ProviderResult()
    return ScriptedProvider(condition_type, _fn)


def make_registry(providers_by_id: Dict[str, ScriptedProvider], pixel_reader: Any,
                  logger: Any = None) -> ProviderRegistry:
    """用替身 provider 组装 ``ProviderRegistry`` 并 ``initialize_all``（就绪）。"""
    factories = {pid: _closure(prov) for pid, prov in providers_by_id.items()}
    registry = ProviderRegistry(pixel_reader, factories=factories, logger=logger)
    registry.initialize_all("")
    return registry


def _closure(provider: ScriptedProvider) -> Callable[[Any], ScriptedProvider]:
    def factory(pixels: Any) -> ScriptedProvider:
        return provider
    return factory


def make_frame(*, width: int = 100, height: int = 100,
               origin_x: int = 0, origin_y: int = 0,
               space: str = "image_pixel", geometry_revision: int = 0,
               frame_id: str = "frame-1", image_id: str = "img-1") -> Frame:
    return Frame(
        frame_id=frame_id,
        image_ref=ImageRef(image_id=image_id, width=width, height=height,
                           format="png", sha256="a" * 64),
        space=space,
        origin=Point(origin_x, origin_y),
        captured_at="2026-01-01T00:00:00Z",
        geometry_revision=geometry_revision,
        binding_id=None,
    )


def make_target_snapshot(*, target_id: str = "t1", sample_no: int = 1, revision: int = 1,
                         anchor: str = "a1", conditions: Any = None,
                         image: Any = None) -> TargetSnapshot:
    return TargetSnapshot(
        target_id=target_id, sample_no=sample_no, revision=revision,
        anchor_condition_id=anchor, conditions=conditions, image=image,
    )


def leaf(condition_id: str, leaf_type: str, params: Dict[str, Any],
         roi: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    node: Dict[str, Any] = {"id": condition_id, "type": leaf_type, "params": params}
    if roi is not None:
        node["roi"] = roi
    return node


__all__ = [
    "ScriptedProvider",
    "make_condition_manifest",
    "provider_returning",
    "provider_echoing_roi",
    "provider_always_matches",
    "provider_never_matches",
    "make_registry",
    "make_frame",
    "make_target_snapshot",
    "leaf",
]
