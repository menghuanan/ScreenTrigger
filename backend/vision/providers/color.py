# -*- coding: utf-8 -*-
"""``ColorProvider``（TASK-014 §4.3）：BGR→RGB/HSV（仅适配器内）→ 掩码 → 8 连通分量。

算法链（严格按 02/07 §2）：

1. 源为行主序 **BGR**；**rgb 域**分通道取 R/G/B 逐像素绝对差；**hsv 域**适配器内
   ``cv2.cvtColor(..., COLOR_BGR2HSV)``（OpenCV ``H∈[0,179]``、``S,V∈[0,255]``）→ **映射回契约域**：
   ``h = H_cv * 2``（∈{0,2,…,358}，覆盖契约 ``[0,359]``）、``s = S_cv*100/255``、
   ``v = V_cv*100/255``（**浮点等比缩放**，不二次取整）；OpenCV 的 ``H=0–179`` 语义**完全封在适配器内**；
2. 容差（全通道 AND）：rgb 逐通道绝对差 ≤ 容差；hsv 用 ``hue_distance``（**圆周最短距离**）+ s/v 绝对差；
3. **8 连通分量**产候选：**每个连通分量独立成紧致 bbox**（绝不拼全局巨大框）；
   ``min_pixel_count``（绝对像素数）与 ``min_percentage``（分母=当前检测 ROI=整帧）同时满足；
4. ``confidence = 匹配像素 / 候选框面积``（0..1，**非概率**）；基座统一 NMS/100 上限。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...types.capabilities import ProviderManifest
from ...types.conditions import _validate_color_params
from ._algo import (
    BaseAlgorithmProvider,
    clamp01,
    crop_roi,
    require_cv2,
    require_numpy,
    run_leaf_validator,
)
from .base import make_candidate

#: 缺省容差（02）。
DEFAULT_RGB_TOLERANCE: Dict[str, int] = {"r": 15, "g": 15, "b": 15}
DEFAULT_HSV_TOLERANCE: Dict[str, int] = {"h": 10, "s": 20, "v": 20}

_MANIFEST = ProviderManifest(
    provider_id="builtin.color",
    api_version=1,
    provider_version="1.0.0",
    supported_condition_types=("color",),
    model_ids=(),
    supported_languages=(),
    thread_safe=False,
    offline=True,
)


class ColorProvider(BaseAlgorithmProvider):
    """颜色检测 Provider（8 连通分量；只产原始候选）。"""

    def __init__(self, pixels: Any) -> None:
        super().__init__(_MANIFEST, pixels)

    def recognize(self, frame: Any, params: Dict[str, Any], cancel: Any,
                  roi: Any = None) -> Any:
        cancel.throw_if_cancelled()
        cv2 = require_cv2()
        np = require_numpy()
        # 命令形态：params = {image_id, params:{ColorParams}}；亦兼容直接传 ColorParams。
        color_params = params.get("params") if isinstance(params.get("params"), dict) else params
        run_leaf_validator(_validate_color_params, color_params)

        full_source = self._read(frame.image_ref.image_id)
        # TASK-016：局部 ROI —— 只在帧的 ROI 子区上检测；min_percentage 分母 = **该 ROI 面积**（02:89）。
        source = crop_roi(full_source, roi)
        if source is None:
            return self._provider_result()  # ROI 完全落帧外 → 空候选（非错误）
        off_x = int(source.offset_x)
        off_y = int(source.offset_y)
        arr = np.frombuffer(source.bgr, dtype=np.uint8).reshape(
            source.height, source.width, 3
        )
        cancel.throw_if_cancelled()
        mask = self._build_mask(arr, color_params, cv2, np)

        min_pixel_count = int(color_params.get("min_pixel_count", 1))
        min_percentage = float(color_params.get("min_percentage", 0))
        roi_area = int(source.width) * int(source.height)

        mask_u8 = mask.astype(np.uint8)
        num, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask_u8, connectivity=8)
        candidates: List[Any] = []
        for index in range(1, num):  # 0 为背景
            left, top, width, height, area = (
                int(stats[index, 0]), int(stats[index, 1]), int(stats[index, 2]),
                int(stats[index, 3]), int(stats[index, 4]),
            )
            if area < min_pixel_count:
                continue
            if roi_area > 0 and (area / roi_area * 100.0) < min_percentage:
                continue
            denominator = width * height
            confidence = (area / float(denominator)) if denominator > 0 else 0.0
            candidates.append(
                make_candidate(left + off_x, top + off_y, width, height,
                               clamp01(confidence), space=frame.space)
            )
        return self._provider_result(candidates=tuple(candidates))

    @staticmethod
    def _build_mask(arr: Any, color_params: Dict[str, Any], cv2: Any, np: Any) -> Any:
        """构造布尔掩码（rgb 逐通道绝对差 / hsv 圆周距离）；OpenCV HSV 仅在适配器内。"""
        color_space = color_params.get("color_space")
        target = color_params["color"]
        if color_space == "rgb":
            tolerance = color_params.get("tolerance", DEFAULT_RGB_TOLERANCE)
            # BGR → 通道取用（不真正转换色彩空间，避免多余拷贝）
            b = arr[:, :, 0].astype("int32")
            g = arr[:, :, 1].astype("int32")
            r = arr[:, :, 2].astype("int32")
            return (
                (np.abs(r - int(target["r"])) <= int(tolerance["r"]))
                & (np.abs(g - int(target["g"])) <= int(tolerance["g"]))
                & (np.abs(b - int(target["b"])) <= int(tolerance["b"]))
            )
        # hsv：适配器内转换 + 映射回契约域（h *2；s/v 等比缩放到 0..100）
        tolerance = color_params.get("tolerance", DEFAULT_HSV_TOLERANCE)
        hsv = cv2.cvtColor(arr, cv2.COLOR_BGR2HSV)
        h = hsv[:, :, 0].astype("int32") * 2
        s = hsv[:, :, 1].astype("float64") * (100.0 / 255.0)
        v = hsv[:, :, 2].astype("float64") * (100.0 / 255.0)
        dh = np.abs(h - int(target["h"]))
        dh = np.minimum(dh, 360 - dh)  # 圆周最短距离
        return (
            (dh <= int(tolerance["h"]))
            & (np.abs(s - float(target["s"])) <= float(tolerance["s"]))
            & (np.abs(v - float(target["v"])) <= float(tolerance["v"]))
        )
