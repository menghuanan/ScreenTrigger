# -*- coding: utf-8 -*-
"""统一识别结果值对象与校验（TASK-013 §8.1；纯函数，无 IO）。

逐字对齐 02 字段表：

- ``Candidate``：``{bbox,center,confidence,space}``；
- ``TextBlock``：``{text,confidence,polygon,space,model_id,language}``；
- ``EvidenceLeaf``：``{condition_id,matched,confidence?,text?,skipped?}``（短路未执行的叶
  记 ``skipped=true``、``matched=null``，**不伪造分数**，02 §8.3）；
- ``Match``：``RecognitionResult.matches`` 元素；
- ``RecognitionResult``：``{target_id,sample_no,target_revision,frame_id,found,matches,
  elapsed_ms,truncated}``；
- ``ProviderResult``：Provider ``recognize`` 的原始产出（未 NMS / 未截断）。

``validate_*`` 为纯函数，复用 ``types.region.validate_rect/validate_point`` 与
``REGIONS_SPACES``，错误统一抛 ``TypeValidationError``（``details.field`` 逐字定位）。

``RecognitionError`` 为厂商无关识别错误载体（06 四字段），供 Provider 抛出、由 service
归一为 job error；不在 ``types.capabilities`` 的枚举内再加码（码取自 06 注册表）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..types.capabilities import (
    REGIONS_SPACES,
    TypeValidationError,
    check_keys,
    enum_field,
    int_field,
    number_field,
    str_field,
)
from ..types.region import Point, Rect, validate_point, validate_rect


class RecognitionError(Exception):
    """识别厂商无关错误（06 四字段；service 归一为 job error + 事件）。"""

    def __init__(
        self,
        code: str,
        message: str,
        field: Optional[str] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message
        self.details: Dict[str, Any] = {"field": field} if field is not None else {}
        self.retryable: bool = retryable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
            "retryable": self.retryable,
        }


# ---------------------------------------------------------------------------
# 值对象（frozen dataclass）
# ---------------------------------------------------------------------------

def center_of(bbox: Rect) -> Point:
    """``bbox`` 中心（左闭右开；取 floor，02）。"""
    return Point(x=int(bbox.x) + int(bbox.width) // 2, y=int(bbox.y) + int(bbox.height) // 2)


@dataclass(frozen=True)
class Candidate:
    """02/03：``{bbox,center,confidence,space}``。"""

    bbox: Rect
    center: Point
    confidence: float
    space: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bbox": {"x": self.bbox.x, "y": self.bbox.y,
                     "width": self.bbox.width, "height": self.bbox.height},
            "center": {"x": self.center.x, "y": self.center.y},
            "confidence": float(self.confidence),
            "space": self.space,
        }


@dataclass(frozen=True)
class TextBlock:
    """02：``{text,confidence,polygon,space,model_id,language}``。"""

    text: str
    confidence: float
    polygon: Tuple[Point, ...]
    space: str
    model_id: str
    language: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "confidence": float(self.confidence),
            "polygon": [{"x": p.x, "y": p.y} for p in self.polygon],
            "space": self.space,
            "model_id": self.model_id,
            "language": self.language,
        }


@dataclass(frozen=True)
class EvidenceLeaf:
    """02：``{condition_id,matched,confidence?,text?,skipped?}``（短路未执行 → skipped/null）。"""

    condition_id: str
    matched: Optional[bool]
    confidence: Optional[float]
    text: Optional[str] = None
    skipped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "matched": self.matched,
            "confidence": self.confidence,
            "text": self.text,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class Match:
    """02：``RecognitionResult.matches`` 元素。"""

    target_id: str
    sample_no: int
    space: str
    bbox: Rect
    center: Point
    confidence: float
    geometry_revision: int
    evidence: Tuple[EvidenceLeaf, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_id": self.target_id,
            "sample_no": self.sample_no,
            "space": self.space,
            "bbox": {"x": self.bbox.x, "y": self.bbox.y,
                     "width": self.bbox.width, "height": self.bbox.height},
            "center": {"x": self.center.x, "y": self.center.y},
            "confidence": float(self.confidence),
            "geometry_revision": self.geometry_revision,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass(frozen=True)
class RecognitionResult:
    """02：识别结果（``found ≡ matches 非空``，由基座强制）。"""

    target_id: Optional[str]
    sample_no: Optional[int]
    target_revision: Optional[int]
    frame_id: str
    found: bool
    matches: Tuple[Match, ...]
    elapsed_ms: int
    truncated: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_id": self.target_id,
            "sample_no": self.sample_no,
            "target_revision": self.target_revision,
            "frame_id": self.frame_id,
            "found": self.found,
            "matches": [m.to_dict() for m in self.matches],
            "elapsed_ms": int(self.elapsed_ms),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class ProviderResult:
    """Provider ``recognize`` 原始产出（未 NMS / 未截断；``space = frame.space``）。"""

    candidates: Tuple[Candidate, ...] = ()
    text_blocks: Tuple[TextBlock, ...] = ()


# ---------------------------------------------------------------------------
# 纯校验（dict → 值对象；抛 TypeValidationError）
# ---------------------------------------------------------------------------

def validate_candidate(value: Any) -> Candidate:
    """校验 ``{bbox,center,confidence,space}``（02）。"""
    if not isinstance(value, dict):
        raise TypeValidationError("INVALID_PARAMS", "candidate", "candidate 必须为对象")
    check_keys(value, ("bbox", "center", "confidence", "space"), "candidate")
    return Candidate(
        bbox=validate_rect(value.get("bbox")),
        center=validate_point(value.get("center")),
        confidence=number_field(value, "confidence", "candidate.confidence", 0.0, 1.0),
        space=enum_field(value, "space", "candidate.space", REGIONS_SPACES),
    )


def validate_text_block(value: Any) -> TextBlock:
    """校验 ``{text,confidence,polygon,space,model_id,language}``（02；polygon 恰四点）。"""
    if not isinstance(value, dict):
        raise TypeValidationError("INVALID_PARAMS", "text_block", "text_block 必须为对象")
    check_keys(
        value,
        ("text", "confidence", "polygon", "space", "model_id", "language"),
        "text_block",
    )
    polygon = value.get("polygon")
    if not isinstance(polygon, list):
        raise TypeValidationError("INVALID_PARAMS", "text_block.polygon",
                                  "text_block.polygon 必须为数组")
    points = tuple(validate_point(item) for item in polygon)
    language = value.get("language")
    if language is not None and not isinstance(language, str):
        raise TypeValidationError("INVALID_PARAMS", "text_block.language",
                                  "text_block.language 必须为字符串或 null")
    return TextBlock(
        text=str_field(value, "text", "text_block.text", min_length=0),
        confidence=number_field(value, "confidence", "text_block.confidence", 0.0, 1.0),
        polygon=points,
        space=enum_field(value, "space", "text_block.space", REGIONS_SPACES),
        model_id=str_field(value, "model_id", "text_block.model_id"),
        language=language,
    )


def validate_evidence_leaf(value: Any) -> EvidenceLeaf:
    """校验 ``{condition_id,matched,confidence?,text?,skipped?}``（02 §8.3）。"""
    if not isinstance(value, dict):
        raise TypeValidationError("INVALID_PARAMS", "evidence", "evidence 必须为对象")
    check_keys(value, ("condition_id", "matched", "confidence", "text", "skipped"), "evidence")
    matched = value.get("matched")
    if matched is not None and not isinstance(matched, bool):
        raise TypeValidationError("INVALID_PARAMS", "evidence.matched",
                                  "evidence.matched 必须为布尔或 null")
    confidence = value.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise TypeValidationError("INVALID_PARAMS", "evidence.confidence",
                                      "evidence.confidence 必须为数值或 null")
        confidence = float(confidence)
    text = value.get("text")
    if text is not None and not isinstance(text, str):
        raise TypeValidationError("INVALID_PARAMS", "evidence.text",
                                  "evidence.text 必须为字符串或 null")
    skipped = value.get("skipped", False)
    if not isinstance(skipped, bool):
        raise TypeValidationError("INVALID_PARAMS", "evidence.skipped",
                                  "evidence.skipped 必须为布尔")
    if skipped and matched is not None:
        # 02 §8.3：短路未执行的叶记 skipped=true、matched=null（不伪造分数）
        raise TypeValidationError("INVALID_PARAMS", "evidence.matched",
                                  "skipped=true 时 matched 必须为 null")
    return EvidenceLeaf(
        condition_id=str_field(value, "condition_id", "evidence.condition_id"),
        matched=matched,
        confidence=confidence,
        text=text,
        skipped=skipped,
    )


def validate_match(value: Any) -> Match:
    """校验 ``Match``（02）。"""
    if not isinstance(value, dict):
        raise TypeValidationError("INVALID_PARAMS", "match", "match 必须为对象")
    check_keys(
        value,
        ("target_id", "sample_no", "space", "bbox", "center", "confidence",
         "geometry_revision", "evidence"),
        "match",
    )
    evidence = value.get("evidence")
    if not isinstance(evidence, list):
        raise TypeValidationError("INVALID_PARAMS", "match.evidence",
                                  "match.evidence 必须为数组")
    return Match(
        target_id=str_field(value, "target_id", "match.target_id"),
        sample_no=int_field(value, "sample_no", "match.sample_no", minimum=1),
        space=enum_field(value, "space", "match.space", REGIONS_SPACES),
        bbox=validate_rect(value.get("bbox")),
        center=validate_point(value.get("center")),
        confidence=number_field(value, "confidence", "match.confidence", 0.0, 1.0),
        geometry_revision=int_field(value, "geometry_revision", "match.geometry_revision",
                                    minimum=0),
        evidence=tuple(validate_evidence_leaf(item) for item in evidence),
    )


def build_recognition_result(
    *,
    target_id: Optional[str],
    sample_no: Optional[int],
    target_revision: Optional[int],
    frame_id: str,
    matches: Tuple[Match, ...],
    elapsed_ms: int,
    truncated: bool,
) -> RecognitionResult:
    """构造 ``RecognitionResult`` 并**强制** ``found ≡ matches 非空``（02 §8.2）。"""
    if target_id is not None and not isinstance(target_id, str):
        raise TypeValidationError("INVALID_PARAMS", "target_id", "target_id 必须为字符串或 null")
    if sample_no is not None and (isinstance(sample_no, bool) or not isinstance(sample_no, int)):
        raise TypeValidationError("INVALID_PARAMS", "sample_no", "sample_no 必须为整数或 null")
    if target_revision is not None and (
        isinstance(target_revision, bool) or not isinstance(target_revision, int)
    ):
        raise TypeValidationError("INVALID_PARAMS", "target_revision",
                                  "target_revision 必须为整数或 null")
    if elapsed_ms < 0:
        raise TypeValidationError("INVALID_PARAMS", "elapsed_ms", "elapsed_ms 不得为负")
    return RecognitionResult(
        target_id=target_id,
        sample_no=sample_no,
        target_revision=target_revision,
        frame_id=frame_id,
        found=bool(matches),
        matches=tuple(matches),
        elapsed_ms=int(elapsed_ms),
        truncated=bool(truncated),
    )


def validate_recognition_result(value: Any) -> RecognitionResult:
    """校验 ``RecognitionResult``（含 ``found ≡ matches 非空``）。"""
    if not isinstance(value, dict):
        raise TypeValidationError("INVALID_PARAMS", "recognition_result",
                                  "recognition_result 必须为对象")
    check_keys(
        value,
        ("target_id", "sample_no", "target_revision", "frame_id", "found",
         "matches", "elapsed_ms", "truncated"),
        "recognition_result",
    )
    matches_raw = value.get("matches")
    if not isinstance(matches_raw, list):
        raise TypeValidationError("INVALID_PARAMS", "recognition_result.matches",
                                  "recognition_result.matches 必须为数组")
    matches = tuple(validate_match(item) for item in matches_raw)
    found = value.get("found")
    if not isinstance(found, bool):
        raise TypeValidationError("INVALID_PARAMS", "recognition_result.found",
                                  "recognition_result.found 必须为布尔")
    if found != bool(matches):
        raise TypeValidationError("INVALID_PARAMS", "recognition_result.found",
                                  "found 必须与 matches 非空一致")
    truncated = value.get("truncated")
    if not isinstance(truncated, bool):
        raise TypeValidationError("INVALID_PARAMS", "recognition_result.truncated",
                                  "recognition_result.truncated 必须为布尔")
    return build_recognition_result(
        target_id=value.get("target_id"),
        sample_no=value.get("sample_no"),
        target_revision=value.get("target_revision"),
        frame_id=str_field(value, "frame_id", "recognition_result.frame_id"),
        matches=matches,
        elapsed_ms=int_field(value, "elapsed_ms", "recognition_result.elapsed_ms", minimum=0),
        truncated=truncated,
    )
