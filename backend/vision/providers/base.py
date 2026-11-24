# -*- coding: utf-8 -*-
"""Provider 组装基类与内置替身（TASK-013 §4/§15.2）。

- ``BaseProvider``：``ProviderResult`` 组装基类（提供 ``_provider_result`` 便捷构造 + 幂等 ``close``）；
- ``NoopProvider``：空实现（始终返回空 ``ProviderResult``，用于装配/能力测试）；
- ``FakeProvider``：**可编程**替身（返回固定候选 / 空（未命中）/ 抛错 / 阻塞模拟超时 /
  初始化失败模拟缺模型）——供 contract tests 在**无 numpy 的受管 Python** 下真实执行。

隔离：本模块（``backend/vision/providers/**``）**禁止** import ``targets.repository`` /
``ipc`` / ``emitter`` / Input（由 ``test_vision_isolation.py`` 的 AST 断言固化）；Provider
**只**获得只读 ``PixelReader``（无写句柄 / UI）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from ...types.capabilities import ProviderManifest
from ...types.region import Point, Rect
from ..cancel import CancelToken
from ..ports import RawPixels
from ..types import (
    Candidate,
    ProviderResult,
    RecognitionError,
    TextBlock,
    center_of,
)


def make_candidate(
    x: int, y: int, width: int, height: int, confidence: float, *, space: str = "image_pixel"
) -> Candidate:
    """便捷构造一个 ``Candidate``（``center`` 取 ``bbox`` 中心 floor）。"""
    bbox = Rect(x=x, y=y, width=width, height=height)
    return Candidate(bbox=bbox, center=center_of(bbox), confidence=float(confidence), space=space)


def make_text_block(
    text: str,
    confidence: float,
    polygon: Sequence[Point],
    *,
    space: str = "image_pixel",
    model_id: str = "fake-model",
    language: Optional[str] = None,
) -> TextBlock:
    """便捷构造一个 ``TextBlock``（``polygon`` 恰四点、同序）。"""
    return TextBlock(
        text=text,
        confidence=float(confidence),
        polygon=tuple(polygon),
        space=space,
        model_id=model_id,
        language=language,
    )


class BaseProvider:
    """Provider 组装基类：便捷构造 ``ProviderResult`` + 幂等 ``close``。"""

    def __init__(self, manifest: ProviderManifest) -> None:
        self._manifest = manifest
        self._closed = False

    def capabilities(self) -> ProviderManifest:
        return self._manifest

    def initialize(self, model_dir: Any) -> None:  # pragma: no cover - 子类覆盖
        return None

    def recognize(self, frame: Any, params: Dict[str, Any], cancel: CancelToken) -> ProviderResult:
        return self._provider_result()

    def close(self) -> None:
        self._closed = True

    @staticmethod
    def _provider_result(
        candidates: Sequence[Candidate] = (),
        text_blocks: Sequence[TextBlock] = (),
    ) -> ProviderResult:
        return ProviderResult(candidates=tuple(candidates), text_blocks=tuple(text_blocks))

    @property
    def pixel_reader(self) -> Optional[Any]:
        """只读像素端口（缺省 None；子类可在构造期接收）。"""
        return getattr(self, "_pixels", None)


class NoopProvider(BaseProvider):
    """空实现：始终返回空结果（用于装配/能力置位测试）。"""

    def __init__(self, manifest: ProviderManifest, pixels: Optional[Any] = None) -> None:
        super().__init__(manifest)
        self._pixels = pixels


class FakeProvider(BaseProvider):
    """可编程替身（contract tests）：返回 / 未命中 / 抛错 / 超时 / 初始化失败。

    Attributes:
        calls: ``recognize`` 调用次数（断言 provider 是否被触发）。
        initialized: 是否已 ``initialize`` 成功。
        initialize_raised: ``initialize`` 是否已抛出注入错误（模拟缺模型）。
    """

    def __init__(
        self,
        manifest: ProviderManifest,
        pixels: Optional[Any] = None,
        *,
        candidates: Sequence[Candidate] = (),
        text_blocks: Sequence[TextBlock] = (),
        raise_code: Optional[str] = None,
        raise_field: Optional[str] = None,
        raise_retryable: bool = False,
        spin: bool = False,
        spinner: int = 1_000_000,
        initialize_error: Optional[BaseException] = None,
        on_recognize: Optional[Callable[[Any, Dict[str, Any]], None]] = None,
    ) -> None:
        super().__init__(manifest)
        self._pixels = pixels
        self._candidates = tuple(candidates)
        self._text_blocks = tuple(text_blocks)
        self._raise_code = raise_code
        self._raise_field = raise_field
        self._raise_retryable = raise_retryable
        self._spin = spin
        self._spinner = spinner
        self._initialize_error = initialize_error
        self._on_recognize = on_recognize
        self.calls = 0
        self.initialized = False
        self.initialize_raised = False
        self.model_dir: Optional[Any] = None
        self.last_frame: Optional[Any] = None
        self.last_params: Optional[Dict[str, Any]] = None

    def initialize(self, model_dir: Any) -> None:
        self.model_dir = model_dir
        if self._initialize_error is not None:
            self.initialize_raised = True
            raise self._initialize_error
        self.initialized = True

    def recognize(self, frame: Any, params: Dict[str, Any], cancel: CancelToken) -> ProviderResult:
        self.calls += 1
        self.last_frame = frame
        self.last_params = params
        if self._on_recognize is not None:
            self._on_recognize(frame, params)
        if self._spin:
            # 协作式取消点：注入时钟推进 → 触发 DeadlineCancelToken 超时
            cancel.throw_if_cancelled()
            for _ in range(self._spinner):
                cancel.throw_if_cancelled()
        if self._raise_code is not None:
            raise RecognitionError(
                self._raise_code,
                "fake provider 抛错: %s" % self._raise_code,
                field=self._raise_field,
                retryable=self._raise_retryable,
            )
        return self._provider_result(candidates=self._candidates, text_blocks=self._text_blocks)
