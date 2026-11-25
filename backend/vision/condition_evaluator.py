# -*- coding: utf-8 -*-
"""条件树求值器（TASK-016 T16-4；``ConditionEvaluator`` 端口的实现）。

语义（02:78，权威）：

- ``anchor`` 叶在**整帧**上产候选且**必须成功**（其 ``roi`` 被忽略，整帧）；锚点在整帧
  产生的候选经 NMS(IoU>0.5) + ``cap_candidates``(100) 截断（``truncated`` **只**由锚点
  候选上限决定）；
- 对**每个锚点候选**，在**候选 bbox 内**求值条件树（其叶 ``roi`` 相对候选 bbox，经
  ``leaf_roi_in_frame`` 夹取到帧内）；整体 = ``anchor 命中 AND 条件树为真``；
- **逐候选**：每个候选至多产出一条 ``Match``（**多物体不拼成一次复合命中**）；
- 未命中 → ``found=false, matches=[]``（**非错误**，job 仍 completed）；
- ``found ≡ matches 非空`` 由 ``build_recognition_result`` 强制；
- ``Match.confidence`` = **锚点分数**；``bbox/center`` 含 ``frame.origin``（消费层**只做一次**
  平移，02/06 不变量 2）；``geometry_revision`` 与 frame 一致（**同一帧、同一坐标版本**）；
- ``evidence`` 每叶 ``{condition_id, matched, confidence, text, skipped}``：短路未执行的叶
  ``skipped=true, matched=null``（**不伪造分数**）；本基座分数**非概率** → ``confidence=null``；
- 子叶 Provider 抛错 → **整次 evaluate 失败**（绝**不**吞成 ``matched=false``）。

错误口径（06，全走注册表，不新造同义码）：
- 缺就绪 Provider → ``ENGINE_UNAVAILABLE`` / ``MODEL_UNAVAILABLE`` / ``CAPABILITY_DENIED``
  （与 service ``_execute_provider`` 同口径）；
- 条件树/draft 非法（``serialize_condition_tree`` 抛 ``TypeValidationError``）→ ``INVALID_PARAMS``；
- 目标模板缺失/损坏 → ``DATA_CORRUPT`` / ``IMAGE_EXPIRED``；
- 取消 → 由 service 归一为 ``cancelled`` 终态（本器经 ``cancel.throw_if_cancelled()`` 抛出）。
"""

from __future__ import annotations

from contextlib import ExitStack

import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..types.capabilities import TypeValidationError
from ..types.region import Point, Rect
from .condition_serializer import (
    BranchSpec,
    ConditionSpec,
    LeafSpec,
    NodeSpec,
    leaf_roi_in_frame,
    serialize_condition_tree,
    validate_draft,
)
from .nms import IOU_THRESHOLD, MAX_CANDIDATES, cap_candidates, nms
from .textmatch import (
    DEFAULT_FUZZY_THRESHOLD,
    DEFAULT_MIN_CONFIDENCE,
    match_text_block_text,
)
from .types import (
    EvidenceLeaf,
    Match,
    RecognitionError,
    RecognitionResult,
    build_recognition_result,
    center_of,
)

#: 草稿路径下 ``Match`` 的占位标识（结果级 target_id/sample_no 由基座置 null；
#: 与 ``targets.domain`` 的 ``draft-placeholder`` 手法一致）。
_DRAFT_MATCH_TARGET_ID = "draft-placeholder"
_DRAFT_MATCH_SAMPLE_NO = 1


class _LeafContext:
    """单候选求值上下文（可变；仅本器内部）。"""

    __slots__ = ("candidate_bbox", "frame", "template_image_id", "cancel",
                 "results", "executed")

    def __init__(self, candidate_bbox: Rect, frame: Any, template_image_id: Optional[str],
                 cancel: Any) -> None:
        self.candidate_bbox = candidate_bbox
        self.frame = frame
        self.template_image_id = template_image_id
        self.cancel = cancel
        self.results: Dict[str, Tuple[bool, Optional[float], Optional[str]]] = {}
        self.executed: set = set()


class ConditionTreeEvaluator:
    """``ConditionEvaluator`` 端口实现（逐帧条件树求值）。"""

    def __init__(
        self,
        *,
        registry: Any,
        target_image_resolver: Any = None,
        logger: Optional[Any] = None,
        monotonic: Optional[Any] = None,
    ) -> None:
        if registry is None:
            raise ValueError("registry 不能为空")
        self._registry = registry
        self._resolver = target_image_resolver
        self._logger = logger
        self._monotonic = monotonic if monotonic is not None else time.monotonic

    # -- 端口入口 ------------------------------------------------------------

    def evaluate(self, frame: Any, snapshot: Any, cancel: Any) -> RecognitionResult:
        """对 ``frame`` 求值 ``snapshot``（``TargetSnapshot`` 或 draft dict）的条件树。"""
        start = self._monotonic()
        spec, ids = self._build_spec(snapshot)
        with ExitStack() as resources:
            template_image_id = self._template_image_id(snapshot, spec, resources)
            cancel.throw_if_cancelled()

            anchor_leaf = spec.anchor
            anchor_candidates = self._run_leaf_candidates(
                anchor_leaf, frame, template_image_id, cancel, roi=None
            )
            deduped = nms(list(anchor_candidates), IOU_THRESHOLD)
            kept, truncated = cap_candidates(deduped, MAX_CANDIDATES)

            matches: List[Match] = []
            for candidate in kept:
                cancel.throw_if_cancelled()
                matched, evidence = self._evaluate_candidate(
                    spec, candidate, frame, template_image_id, cancel
                )
                if not matched:
                    continue
                matches.append(self._build_match(candidate, frame, evidence, ids))

            elapsed_ms = int(round((self._monotonic() - start) * 1000.0))
            return build_recognition_result(
                target_id=ids["target_id"],
                sample_no=ids["sample_no"],
                target_revision=ids["target_revision"],
                frame_id=frame.frame_id,
                matches=tuple(matches),
                elapsed_ms=max(0, elapsed_ms),
                truncated=truncated,
            )


    # -- snapshot → spec / 标识 ----------------------------------------------

    def _build_spec(self, snapshot: Any) -> Tuple[ConditionSpec, Dict[str, Any]]:
        try:
            if isinstance(snapshot, Mapping):
                # TASK-016 [D]：草稿（test_recognition）先做 **TargetDraft 形状校验**（复用
                # types.conditions.validate_target 的占位手法），再解析条件树；错误 → INVALID_PARAMS。
                draft = validate_draft(snapshot)
                conditions = draft.conditions
                anchor = draft.anchor_condition_id
                ids: Dict[str, Any] = {
                    "target_id": None, "sample_no": None, "target_revision": None,
                    "is_draft": True,
                }
            else:
                conditions = getattr(snapshot, "conditions", None)
                anchor = getattr(snapshot, "anchor_condition_id", None)
                ids = {
                    "target_id": getattr(snapshot, "target_id", None),
                    "sample_no": getattr(snapshot, "sample_no", None),
                    "target_revision": getattr(snapshot, "revision", None),
                    "is_draft": False,
                }
            spec = serialize_condition_tree(conditions, anchor)
        except TypeValidationError as exc:
            raise RecognitionError(
                exc.code, exc.message, field=exc.field, retryable=exc.retryable
            ) from exc
        # [C] anchor 叶 roi 忽略（恒整帧）：记一条诊断，不报错。
        if spec.anchor.roi is not None:
            self._log(
                "条件树 anchor 叶 %s 的 roi 被忽略（恒整帧）" % spec.anchor.condition_id
            )
        return spec, ids

    def _template_image_id(self, snapshot: Any, spec: ConditionSpec, resources: ExitStack) -> Optional[str]:
        """解析模板 ``ImageAsset`` → 会话 ``ImageRef.image_id``（draft 直接取 ``image_id``）。"""
        needs_template = any(leaf.type in ("template", "contour") for leaf in spec.leaves)
        if isinstance(snapshot, Mapping):
            image_id = snapshot.get("image_id")
            return image_id if isinstance(image_id, str) and image_id else None
        asset = getattr(snapshot, "image", None)
        if asset is None:
            return None
        if not needs_template and self._resolver is None:
            return None
        if self._resolver is None:
            raise RecognitionError(
                "CAPABILITY_DENIED", "目标模板像素解析器未装配（TASK-016）"
            )
        if hasattr(self._resolver, "pin"):
            return resources.enter_context(self._resolver.pin(asset)).image_id
        image_ref = self._resolver.resolve(asset)  # 失败 → DATA_CORRUPT/IMAGE_EXPIRED（抛出）
        return image_ref.image_id

    # -- 候选求值 ------------------------------------------------------------

    def _evaluate_candidate(
        self, spec: ConditionSpec, candidate: Any, frame: Any,
        template_image_id: Optional[str], cancel: Any,
    ) -> Tuple[bool, Tuple[EvidenceLeaf, ...]]:
        context = _LeafContext(candidate.bbox, frame, template_image_id, cancel)
        # anchor 叶恒 matched=true（候选即由其整帧检测产生），never skipped。
        context.results[spec.anchor.condition_id] = (True, None, None)
        matched = self._eval_node(spec.root, spec, context)
        evidence = self._build_evidence(spec, context)
        return matched, evidence

    def _eval_node(self, node: NodeSpec, spec: ConditionSpec, context: _LeafContext) -> bool:
        """DFS 求值；``all``/``any`` 逐子短路（未执行叶留 skipped）。"""
        if isinstance(node, LeafSpec):
            return self._eval_leaf_node(node, spec, context)
        if node.op == "all":
            for child in node.children:
                if not self._eval_node(child, spec, context):
                    return False
            return True
        # any
        for child in node.children:
            if self._eval_node(child, spec, context):
                return True
        return False

    def _eval_leaf_node(self, leaf: LeafSpec, spec: ConditionSpec,
                        context: _LeafContext) -> bool:
        if leaf.is_anchor:
            context.results[leaf.condition_id] = (True, None, None)
            context.executed.add(leaf.condition_id)
            return True
        context.cancel.throw_if_cancelled()
        frame = context.frame
        roi = leaf_roi_in_frame(
            context.candidate_bbox, leaf,
            frame.image_ref.width, frame.image_ref.height,
        )
        if roi is None:
            # ROI 完全落帧外 → 该叶判 false（非错误）
            context.results[leaf.condition_id] = (False, None, None)
            context.executed.add(leaf.condition_id)
            return False
        matched, confidence, text = self._run_leaf(
            leaf, frame, context.template_image_id, context.cancel, roi
        )
        context.results[leaf.condition_id] = (matched, confidence, text)
        context.executed.add(leaf.condition_id)
        return matched

    def _build_evidence(self, spec: ConditionSpec,
                        context: _LeafContext) -> Tuple[EvidenceLeaf, ...]:
        evidence: List[EvidenceLeaf] = []
        for leaf in spec.leaves:
            result = context.results.get(leaf.condition_id)
            if leaf.is_anchor:
                # 锚点恒已执行（整帧命中），不 skipped。
                evidence.append(EvidenceLeaf(
                    condition_id=leaf.condition_id, matched=True, confidence=None,
                    text=None, skipped=False,
                ))
                continue
            if result is None:
                # 短路未执行：skipped=true, matched=null（不伪造分数）
                evidence.append(EvidenceLeaf(
                    condition_id=leaf.condition_id, matched=None, confidence=None,
                    text=None, skipped=True,
                ))
            else:
                matched, confidence, text = result
                evidence.append(EvidenceLeaf(
                    condition_id=leaf.condition_id, matched=bool(matched),
                    confidence=confidence, text=text, skipped=False,
                ))
        return tuple(evidence)

    # -- 单叶执行 ------------------------------------------------------------

    def _run_leaf_candidates(self, leaf: LeafSpec, frame: Any,
                             template_image_id: Optional[str], cancel: Any,
                             roi: Any) -> Tuple[Any, ...]:
        provider_result = self._invoke_provider(leaf, frame, template_image_id, cancel, roi)
        return tuple(getattr(provider_result, "candidates", ()) or ())

    def _run_leaf(self, leaf: LeafSpec, frame: Any, template_image_id: Optional[str],
                  cancel: Any, roi: Any) -> Tuple[bool, Optional[float], Optional[str]]:
        provider_result = self._invoke_provider(leaf, frame, template_image_id, cancel, roi)
        if leaf.type == "ocr":
            params = leaf.params or {}
            mode = params.get("match_mode", "exact")
            case_sensitive = bool(params.get("case_sensitive", True))
            fuzzy = float(params.get("fuzzy_threshold", DEFAULT_FUZZY_THRESHOLD))
            min_conf = float(params.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
            score, block = match_text_block_text(
                getattr(provider_result, "text_blocks", ()) or (),
                params.get("text", ""),
                mode=mode,
                case_sensitive=case_sensitive,
                fuzzy_threshold=fuzzy,
                min_confidence=min_conf,
            )
            if score is None:
                return (False, None, None)  # 未命中
            matched_text = getattr(block, "text", None)
            return (True, None, matched_text)
        # template / contour / color：产候选即命中（boolean）；分数非概率 → confidence=null
        matched = len(tuple(getattr(provider_result, "candidates", ()) or ())) > 0
        return (matched, None, None)

    def _invoke_provider(self, leaf: LeafSpec, frame: Any, template_image_id: Optional[str],
                         cancel: Any, roi: Any) -> Any:
        provider = self._registry.provider_for(leaf.type)
        if provider is None:
            raise self._provider_unavailable_error(leaf.type)
        params = dict(leaf.params or {})
        if leaf.type in ("template", "contour"):
            if not isinstance(template_image_id, str) or not template_image_id:
                raise RecognitionError(
                    "DATA_CORRUPT", "目标模板图像缺失或不可用", field="image.path"
                )
            params["template_image_id"] = template_image_id
        manifest = self._registry.manifest_for(provider)
        if manifest is not None and not manifest.thread_safe:
            with self._registry.serial_lock(provider):
                return provider.recognize(frame, params, cancel, roi=roi)
        return provider.recognize(frame, params, cancel, roi=roi)

    def _provider_unavailable_error(self, condition_type: str) -> RecognitionError:
        """与 ``VisionService._execute_provider`` 同口径的能力错误（06，不新造同义码）。"""
        code = None
        unavailable_code = getattr(self._registry, "unavailable_code", None)
        if callable(unavailable_code):
            code = unavailable_code(condition_type)
        if code == "ENGINE_UNAVAILABLE":
            return RecognitionError(
                "ENGINE_UNAVAILABLE",
                "识别引擎不可用：条件类型 %s 的 Provider 依赖缺失" % condition_type,
                retryable=False,
            )
        if code == "MODEL_UNAVAILABLE":
            return RecognitionError(
                "MODEL_UNAVAILABLE",
                "识别模型不可用：条件类型 %s 的 Provider 缺已安装模型" % condition_type,
                retryable=False,
            )
        return RecognitionError(
            "CAPABILITY_DENIED", "无就绪 Provider 覆盖条件类型 %s" % condition_type
        )

    # -- Match 构造 ----------------------------------------------------------

    @staticmethod
    def _build_match(candidate: Any, frame: Any, evidence: Tuple[EvidenceLeaf, ...],
                     ids: Dict[str, Any]) -> Match:
        origin = frame.origin
        bbox = Rect(
            x=int(candidate.bbox.x) + int(origin.x),
            y=int(candidate.bbox.y) + int(origin.y),
            width=int(candidate.bbox.width),
            height=int(candidate.bbox.height),
        )
        if ids.get("is_draft"):
            target_id = _DRAFT_MATCH_TARGET_ID
            sample_no = _DRAFT_MATCH_SAMPLE_NO
        else:
            target_id = ids.get("target_id")
            sample_no = ids.get("sample_no")
        return Match(
            target_id=str(target_id),
            sample_no=int(sample_no) if isinstance(sample_no, int) and not isinstance(sample_no, bool) else _DRAFT_MATCH_SAMPLE_NO,
            space=frame.space,
            bbox=bbox,
            center=center_of(bbox),
            confidence=float(getattr(candidate, "confidence", 0.0)),
            geometry_revision=int(frame.geometry_revision),
            evidence=evidence,
        )

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)


__all__ = ["ConditionTreeEvaluator"]
