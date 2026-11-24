# -*- coding: utf-8 -*-
"""TASK-013 测试替身集合（design §15.2）；**只依赖标准库 + 基座**。

提供：可编程 provider、注入单调时钟、``CancelFlag``（复用基座）、``RecordingPixelReader``
（无写方法）、``FakeTargetSnapshotProvider``、``FakeConditionEvaluator``、``RecordingTransport``、
``FakeSession``、并发探测 provider 等替身注入点。
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.types.capabilities import ProviderManifest
from backend.types.region import Rect
from backend.vision.emitter import NullRecognitionEmitter, RecordingRecognitionEmitter
from backend.vision.nms import MAX_CANDIDATES
from backend.vision.ports import RawPixels, TargetSnapshot
from backend.vision.providers.base import (
    BaseProvider,
    FakeProvider,
    NoopProvider,
    make_candidate,
    make_text_block,
)
from backend.vision.types import (
    Candidate,
    EvidenceLeaf,
    Match,
    ProviderResult,
    RecognitionError,
    RecognitionResult,
    TextBlock,
    build_recognition_result,
    center_of,
)


# ---------------------------------------------------------------------------
# manifest 构造
# ---------------------------------------------------------------------------

def make_manifest(
    provider_id: str = "fake",
    *,
    api_version: int = 1,
    condition_types: Sequence[str] = ("template",),
    model_ids: Sequence[str] = (),
    languages: Sequence[str] = (),
    thread_safe: bool = False,
    offline: bool = True,
    provider_version: str = "1.0.0",
) -> ProviderManifest:
    return ProviderManifest(
        provider_id=provider_id,
        api_version=api_version,
        provider_version=provider_version,
        supported_condition_types=tuple(condition_types),
        model_ids=tuple(model_ids),
        supported_languages=tuple(languages),
        thread_safe=thread_safe,
        offline=offline,
    )


def make_model_manifest_dict(model_id: str = "m1", **overrides: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "provider_id": "fake",
        "api_version": 1,
        "provider_version": "1.0.0",
        "supported_condition_types": ["ocr"],
        "model_ids": [model_id],
        "supported_languages": ["zh", "en"],
        "thread_safe": False,
        "offline": True,
        "license": "Apache-2.0",
        "source": "file://models/%s" % model_id,
        "sha256": "0" * 64,
        "supported_charsets": ["utf-8"],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# 时钟
# ---------------------------------------------------------------------------

class FakeMonotonic:
    """可控单调时钟：每次调用返回值后自增 ``step``（确定性超时，不真实 sleep）。"""

    def __init__(self, start: float = 0.0, step: float = 0.0) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value


def constant_monotonic(value: float = 0.0):
    """恒定时钟（永不触发 deadline）。"""
    return lambda: value


# ---------------------------------------------------------------------------
# 只读像素端口替身（无 put/release/persist）
# ---------------------------------------------------------------------------

class RecordingPixelReader:
    """只读像素端口替身：记录读取；**无**写方法（隔离断言用）。"""

    def __init__(self) -> None:
        self.reads: List[str] = []
        self._images: Dict[str, RawPixels] = {}

    def add(
        self,
        image_id: str,
        *,
        width: int = 8,
        height: int = 8,
        value: int = 0,
        sha256: Optional[str] = None,
        image_format: str = "png",
    ) -> str:
        data = bytes([value, value, value]) * (width * height)
        if sha256 is None:
            sha256 = hashlib.sha256(data).hexdigest()
        self._images[image_id] = RawPixels(
            image_id=image_id, width=width, height=height, sha256=sha256,
            format=image_format, bgr=data,
        )
        return image_id

    def read(self, image_id: str) -> RawPixels:
        self.reads.append(image_id)
        raw = self._images.get(image_id)
        if raw is None:
            raise RecognitionError("IMAGE_EXPIRED", "image 不存在: %s" % image_id, field="image_id")
        return raw


# ---------------------------------------------------------------------------
# 目标快照 / 条件求值替身
# ---------------------------------------------------------------------------

class FakeTargetSnapshotProvider:
    def __init__(self) -> None:
        self._targets: Dict[str, TargetSnapshot] = {}

    def add(
        self,
        target_id: str,
        *,
        sample_no: int = 1,
        revision: int = 1,
        anchor: str = "c1",
        conditions: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._targets[target_id] = TargetSnapshot(
            target_id=target_id, sample_no=sample_no, revision=revision,
            anchor_condition_id=anchor,
            conditions=conditions if conditions is not None else {"op": "all", "children": []},
        )

    def snapshot(self, target_id: str) -> TargetSnapshot:
        if target_id not in self._targets:
            raise RecognitionError("TARGET_NOT_FOUND", "目标不存在: %s" % target_id,
                                   field="target_id")
        return self._targets[target_id]


def sample_match(
    *,
    target_id: str = "t1",
    sample_no: int = 1,
    space: str = "image_pixel",
    x: int = 0,
    y: int = 0,
    width: int = 4,
    height: int = 4,
    confidence: float = 0.9,
    geometry_revision: int = 0,
    evidence: Sequence[EvidenceLeaf] = (),
) -> Match:
    bbox = Rect(x=x, y=y, width=width, height=height)
    return Match(
        target_id=target_id, sample_no=sample_no, space=space, bbox=bbox, center=center_of(bbox),
        confidence=confidence, geometry_revision=geometry_revision, evidence=tuple(evidence),
    )


class FakeConditionEvaluator:
    """条件树求值替身：产出一条含 ``skipped=true`` 短路叶的 ``RecognitionResult``。

    证明基座 schema 可承载短路（TASK-016 填充语义）。
    """

    def __init__(
        self,
        *,
        matches: Optional[Sequence[Match]] = None,
        target_id: Optional[str] = "t1",
        sample_no: Optional[int] = 1,
        target_revision: Optional[int] = 1,
        elapsed_ms: int = 3,
        truncated: bool = False,
        skipped_leaf: bool = True,
    ) -> None:
        if matches is None:
            evidence = (
                EvidenceLeaf(condition_id="c1", matched=True, confidence=0.9),
                EvidenceLeaf(condition_id="c2", matched=None, confidence=None, skipped=True),
            ) if skipped_leaf else (
                EvidenceLeaf(condition_id="c1", matched=True, confidence=0.9),
            )
            matches = (sample_match(evidence=evidence),)
        self._matches = tuple(matches)
        self._target_id = target_id
        self._sample_no = sample_no
        self._target_revision = target_revision
        self._elapsed_ms = elapsed_ms
        self._truncated = truncated
        self.calls = 0

    def evaluate(self, frame: Any, snapshot: Any, cancel: Any) -> RecognitionResult:
        self.calls += 1
        cancel.throw_if_cancelled()
        return build_recognition_result(
            target_id=self._target_id,
            sample_no=self._sample_no,
            target_revision=self._target_revision,
            frame_id=frame.frame_id,
            matches=self._matches,
            elapsed_ms=self._elapsed_ms,
            truncated=self._truncated,
        )


# ---------------------------------------------------------------------------
# 并发探测 provider（串行化断言）
# ---------------------------------------------------------------------------

class ConcurrencyProvider(BaseProvider):
    """记录并发调用峰值（``thread_safe=false`` 串行化断言）。"""

    def __init__(self, manifest: ProviderManifest, pixels: Any = None, *, delay: float = 0.04) -> None:
        super().__init__(manifest)
        self._pixels = pixels
        self._delay = delay
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.initialized = False

    def initialize(self, model_dir: Any) -> None:
        self.initialized = True

    def recognize(self, frame: Any, params: Dict[str, Any], cancel: Any) -> ProviderResult:
        with self._lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self._delay)
            return ProviderResult()
        finally:
            with self._lock:
                self.active -= 1


# ---------------------------------------------------------------------------
# Dispatcher 依赖替身
# ---------------------------------------------------------------------------

class RecordingTransport:
    """记录 ``send`` 的信封；``read_line`` 恒 None（不驱动读循环）。"""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def send(self, envelope: Dict[str, Any]) -> bool:
        self.sent.append(envelope)
        return True

    def read_line(self) -> Optional[str]:
        return None


class FakeSession:
    def __init__(self, session_id: str = "s-test") -> None:
        self.session_id = session_id

    def verify(self, envelope: Dict[str, Any]) -> bool:
        return envelope.get("session_id") == self.session_id


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
