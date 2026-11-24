# -*- coding: utf-8 -*-
"""``TemplateProvider``（TASK-014 §4.1）：尺寸/方差校验 → 可选尺度枚举 → TM_CCOEFF_NORMED。

算法链（严格按 02/07 §2）：

1. 尺寸/方差校验：模板空/尺寸非法 → ``INVALID_IMAGE``（field=``template_image_id``）；
   模板纯色（灰度零方差）→ ``INVALID_IMAGE``（field=``template_image_id``）；
2. 可选尺度枚举（``allow_scale=false`` 时仅 ``{1.0}``）：只缩放**模板**（源帧保持原分辨率），
   故响应坐标即**源帧原生坐标**；模板大于源帧且无合法缩放候选 → ``INVALID_IMAGE``；
3. ``cv2.matchTemplate(..., TM_CCOEFF_NORMED)`` + ``np.nan_to_num(nan=-1.0)``（零方差窗口防御）；
4. 阈值 + 贪心峰值抑制（避免「每像素一个候选」；内部上限 ``MAX_INTERNAL_PEAKS``）；
5. ``score = clamp01(≤1)``（TM_CCOEFF_NORMED ∈ [-1,1]）；返回**原始**候选，
   NMS(IoU>0.5)/100 上限由**基座统一**施加。

分数语义：TM_CCOEFF_NORMED 归一相似度，**不是概率**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ...types.capabilities import ProviderManifest
from ...types.conditions import _validate_template_params
from ..types import RecognitionError
from ._algo import (
    BaseAlgorithmProvider,
    clamp01,
    crop_roi,
    enumerate_scales,
    require_cv2,
    require_numpy,
    run_leaf_validator,
    select_keys,
)
from .base import make_candidate

#: TM_CCOEFF_NORMED 分数阈值缺省（02：threshold?:number=.85）。
DEFAULT_THRESHOLD: float = 0.85
#: 启用尺度时的缺省上下界/步长（相对模板原尺寸比例）。
DEFAULT_SCALE_RANGE: Tuple[float, float] = (0.95, 1.05)
DEFAULT_SCALE_STEP: float = 0.01
#: 算法内部峰值上限（防爆；最终 100 上限由基座 ``cap_candidates`` 施加）。
MAX_INTERNAL_PEAKS: int = 400

_TEMPLATE_KEYS: Tuple[str, ...] = ("threshold", "allow_scale", "scale_range", "step")

_MANIFEST = ProviderManifest(
    provider_id="builtin.template",
    api_version=1,
    provider_version="1.0.0",
    supported_condition_types=("template",),
    model_ids=(),
    supported_languages=(),
    thread_safe=False,
    offline=True,
)


class TemplateProvider(BaseAlgorithmProvider):
    """模板匹配 Provider（只产原始候选；NMS/截断归基座）。"""

    def __init__(self, pixels: Any) -> None:
        super().__init__(_MANIFEST, pixels)

    def recognize(self, frame: Any, params: Dict[str, Any], cancel: Any,
                  roi: Any = None) -> Any:
        cancel.throw_if_cancelled()
        cv2 = require_cv2()
        np = require_numpy()
        algo = select_keys(params, _TEMPLATE_KEYS)
        algo.setdefault("threshold", DEFAULT_THRESHOLD)  # 叶校验器要求 threshold 存在
        run_leaf_validator(_validate_template_params, algo)
        threshold = float(algo["threshold"])
        allow_scale = bool(algo.get("allow_scale", False))
        scale_range = algo.get("scale_range", list(DEFAULT_SCALE_RANGE))
        step = float(algo.get("step", DEFAULT_SCALE_STEP))

        full_source = self._read(frame.image_ref.image_id)
        template_id = params.get("template_image_id")
        if not isinstance(template_id, str) or not template_id:
            raise RecognitionError("INVALID_PARAMS", "template_image_id 必填且为非空字符串",
                                   field="template_image_id")
        template = self._read(template_id)
        cancel.throw_if_cancelled()
        # TASK-016：局部 ROI —— 只在帧的 ROI 子区上检测；模板始终用整模板。
        source = crop_roi(full_source, roi)
        if source is None:
            return self._provider_result()  # ROI 完全落帧外 → 空候选（非错误）
        candidates = self._match(
            source, template, threshold, allow_scale, scale_range, step, cv2, np, cancel, frame.space
        )
        return self._provider_result(candidates=candidates)

    def _match(
        self, source: Any, template: Any, threshold: float, allow_scale: bool,
        scale_range: Any, step: float, cv2: Any, np: Any, cancel: Any, space: str,
    ) -> Tuple[Any, ...]:
        if template.width < 1 or template.height < 1:
            raise RecognitionError("INVALID_IMAGE", "模板为空/尺寸非法（宽高须 ≥1）",
                                   field="template_image_id")
        off_x = int(getattr(source, "offset_x", 0))
        off_y = int(getattr(source, "offset_y", 0))
        src = np.frombuffer(source.bgr, dtype=np.uint8).reshape(source.height, source.width, 3)
        tmpl = np.frombuffer(template.bgr, dtype=np.uint8).reshape(
            template.height, template.width, 3
        )
        # 零方差（纯色）模板：TM_CCOEFF_NORMED 分母为 0 → INVALID_IMAGE。
        tmpl_gray = cv2.cvtColor(tmpl, cv2.COLOR_BGR2GRAY)
        if float(tmpl_gray.std()) == 0.0:
            raise RecognitionError(
                "INVALID_IMAGE",
                "模板为纯色（零方差），TM_CCOEFF_NORMED 分母为 0",
                field="template_image_id",
            )
        if allow_scale:
            scales: List[float] = enumerate_scales(scale_range[0], scale_range[1], step)
        else:
            scales = [1.0]

        candidates: List[Any] = []
        any_valid = False
        peaks = 0
        for scale in scales:
            cancel.throw_if_cancelled()
            w_t = max(1, int(round(template.width * scale)))
            h_t = max(1, int(round(template.height * scale)))
            if w_t > source.width or h_t > source.height:
                continue  # 该尺度不合法（模板大于源帧）
            any_valid = True
            if w_t == template.width and h_t == template.height:
                t_scaled = tmpl
            else:
                t_scaled = cv2.resize(tmpl, (w_t, h_t), interpolation=cv2.INTER_AREA)
            result = cv2.matchTemplate(src, t_scaled, cv2.TM_CCOEFF_NORMED)
            result = np.nan_to_num(result, nan=-1.0)
            found = self._extract_peaks(
                result, w_t, h_t, threshold, space, np, budget=MAX_INTERNAL_PEAKS - peaks,
                off_x=off_x, off_y=off_y,
            )
            candidates.extend(found)
            peaks += len(found)
            if peaks >= MAX_INTERNAL_PEAKS:
                break
        if not any_valid:
            raise RecognitionError(
                "INVALID_IMAGE", "模板大于源帧且无合法缩放候选", field="template_image_id"
            )
        return tuple(candidates)

    @staticmethod
    def _extract_peaks(
        result: Any, w_t: int, h_t: int, threshold: float, space: str, np: Any, *,
        budget: int, off_x: int = 0, off_y: int = 0,
    ) -> List[Any]:
        """贪心峰值抽取：取全局最大，记录候选，邻域置 -1，重复至 <threshold 或耗尽预算。

        ``off_x``/``off_y`` 为局部 ROI 原点（TASK-016）；候选坐标平移到「源帧局部像素」。
        """
        work = result.copy()
        height, width = work.shape
        suppress = max(1, min(w_t, h_t) // 2)
        found: List[Any] = []
        while budget > 0 and work.size > 0:
            idx = int(np.argmax(work))
            y, x = divmod(idx, width)
            score = float(work[y, x])
            if score < threshold:
                break
            found.append(
                make_candidate(int(x) + int(off_x), int(y) + int(off_y), w_t, h_t,
                               clamp01(score), space=space)
            )
            budget -= 1
            y0 = max(0, y - suppress)
            y1 = min(height, y + suppress + 1)
            x0 = max(0, x - suppress)
            x1 = min(width, x + suppress + 1)
            work[y0:y1, x0:x1] = -1.0
        return found
