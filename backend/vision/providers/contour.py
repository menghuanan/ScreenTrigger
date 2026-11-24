# -*- coding: utf-8 -*-
"""``ContourProvider``（TASK-014 §4.2）：灰度 → 固定 Canny → 外轮廓 → matchShapes I1。

算法链（严格按 02/07 §2）：

1. **统一灰度预处理**：内部缓冲为行主序 **BGR**；用 ``cv2.COLOR_BGR2GRAY``（**非** RGB）
   转灰度（R/B 权重不同，用错会漂移灰度→边缘→轮廓）；源、模板**同一口径**；
2. **固定 Canny**：``cv2.Canny(gray, canny_low, canny_high)``（``low<high`` 由校验保证）；
3. **外轮廓**：``cv2.findContours(RETR_EXTERNAL, CHAIN_APPROX_NONE)``；
4. **``min_area`` 过滤**：模板侧取**面积最大**的合法外轮廓作参考形状；
5. **``matchShapes I1``**：Hu 矩不变量（平移/缩放/旋转不敏感）；
6. **归一分数** ``score = 1/(1+distance)``（``d≥0 ⇒ score∈(0,1]``）；``score ≥ threshold`` 记候选；
7. 候选 bbox = ``cv2.boundingRect``；基座统一 NMS/100 上限。

能力区分（07 §2 硬要求）：**纹理变色但形状不变**的夹具下，本 Provider 命中（Hu 距离≈0）
而模板 Provider 失配（像素不同 → TM_CCOEFF_NORMED 下降）。分数**不是概率**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ...types.capabilities import ProviderManifest
from ...types.conditions import _validate_contour_params
from ..types import RecognitionError
from ._algo import (
    BaseAlgorithmProvider,
    clamp01,
    crop_roi,
    require_cv2,
    require_numpy,
    run_leaf_validator,
    select_keys,
)
from .base import make_candidate

#: 缺省参数（02/07 §2）。
DEFAULT_THRESHOLD: float = 0.85
DEFAULT_MIN_AREA: int = 16
DEFAULT_CANNY_LOW: int = 50
DEFAULT_CANNY_HIGH: int = 150

_CONTOUR_KEYS: Tuple[str, ...] = ("threshold", "min_area", "canny_low", "canny_high")

_MANIFEST = ProviderManifest(
    provider_id="builtin.contour",
    api_version=1,
    provider_version="1.0.0",
    supported_condition_types=("contour",),
    model_ids=(),
    supported_languages=(),
    thread_safe=False,
    offline=True,
)


class ContourProvider(BaseAlgorithmProvider):
    """轮廓匹配 Provider（Hu 矩形状相似度；只产原始候选）。"""

    def __init__(self, pixels: Any) -> None:
        super().__init__(_MANIFEST, pixels)

    def recognize(self, frame: Any, params: Dict[str, Any], cancel: Any,
                  roi: Any = None) -> Any:
        cancel.throw_if_cancelled()
        cv2 = require_cv2()
        np = require_numpy()
        algo = select_keys(params, _CONTOUR_KEYS)
        # 叶校验器要求 threshold/canny_low/canny_high 存在 → 先补缺省再校验（min_area 可选）。
        algo.setdefault("threshold", DEFAULT_THRESHOLD)
        algo.setdefault("canny_low", DEFAULT_CANNY_LOW)
        algo.setdefault("canny_high", DEFAULT_CANNY_HIGH)
        run_leaf_validator(_validate_contour_params, algo)
        threshold = float(algo["threshold"])
        min_area = int(algo.get("min_area", DEFAULT_MIN_AREA))
        canny_low = int(algo["canny_low"])
        canny_high = int(algo["canny_high"])

        full_source = self._read(frame.image_ref.image_id)
        template_id = params.get("template_image_id")
        if not isinstance(template_id, str) or not template_id:
            raise RecognitionError("INVALID_PARAMS", "template_image_id 必填且为非空字符串",
                                   field="template_image_id")
        template = self._read(template_id)
        cancel.throw_if_cancelled()
        # TASK-016：局部 ROI —— 只在帧的 ROI 子区上检测；参考形状用整模板。
        source = crop_roi(full_source, roi)
        if source is None:
            return self._provider_result()  # ROI 完全落帧外 → 空候选（非错误）
        off_x = int(source.offset_x)
        off_y = int(source.offset_y)

        src_gray = self._grayscale(source, cv2, np)
        tmpl_gray = self._grayscale(template, cv2, np)

        t_edges = cv2.Canny(tmpl_gray, canny_low, canny_high)
        t_contours, _ = cv2.findContours(t_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        reference = self._largest_contour(cv2, t_contours, min_area)
        if reference is None:
            raise RecognitionError("INVALID_IMAGE", "轮廓参考无有效形状", field="template_image_id")

        s_edges = cv2.Canny(src_gray, canny_low, canny_high)
        s_contours, _ = cv2.findContours(s_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        candidates: List[Any] = []
        for contour in s_contours:
            cancel.throw_if_cancelled()
            if cv2.contourArea(contour) < min_area:
                continue
            distance = float(cv2.matchShapes(contour, reference, cv2.CONTOURS_MATCH_I1, 0.0))
            score = clamp01(1.0 / (1.0 + max(0.0, distance)))
            if score < threshold:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            candidates.append(
                make_candidate(int(x) + off_x, int(y) + off_y, int(w), int(h), score,
                               space=frame.space)
            )
        return self._provider_result(candidates=tuple(candidates))

    @staticmethod
    def _grayscale(raw: Any, cv2: Any, np: Any) -> Any:
        """行主序 BGR uint8 → 灰度（``COLOR_BGR2GRAY``，与内部 BGR 约定一致）。"""
        arr = np.frombuffer(raw.bgr, dtype=np.uint8).reshape(raw.height, raw.width, 3)
        return cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)

    @staticmethod
    def _largest_contour(cv2: Any, contours: Any, min_area: int) -> Any:
        """取面积最大的合法外轮廓（≥ ``min_area``）；无 → None。"""
        best = None
        best_area = 0.0
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area >= min_area and area > best_area:
                best = contour
                best_area = area
        return best
