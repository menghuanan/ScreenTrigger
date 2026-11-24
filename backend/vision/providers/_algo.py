# -*- coding: utf-8 -*-
"""算法 Provider 公共基座（TASK-014）：惰性 cv2/numpy 加载 + 纯逻辑辅助 + 候选构造。

**顶层零重依赖**：本模块（及 template/contour/color）在**模块顶层**只 import 标准库与
``backend.*`` 纯 Python 模块；``cv2``/``numpy``/``PIL`` **只在函数体内**惰性加载
（受管 Python 无这些依赖时，``import backend.vision`` 仍为零重依赖）。

职责：

- ``require_cv2``/``require_numpy``：惰性获取引擎；缺失 → ``RecognitionError``
  （``ENGINE_UNAVAILABLE``，``retryable=false``），触发注册表置能力 ``unavailable+reason``；
- 纯逻辑辅助（**无 cv2 分支**，受管 Python 亦可真实执行）：``clamp01``、
  ``hue_distance``（HSV 圆周最短距离）、``enumerate_scales``（确定性尺度取点）、
  ``rgb_within``/``hsv_within``（容差判定）、``connected_components_8``（8 连通紧致分量）；
- ``select_keys``/``run_leaf_validator``：只取算法参数并**复用** ``types.conditions``
  叶校验器（错误 field 逐字 ``conditions.params.*``）；
- ``BaseAlgorithmProvider``：``initialize`` 探测引擎依赖 + 只读像素读取 + ``_provider_result``。

分数语义（02）：三算法分数**不是概率、不可跨算法比较**；``clamp01`` 仅做值域收敛。
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from ...types.capabilities import TypeValidationError
from ..types import RecognitionError
from .base import BaseProvider  # noqa: F401  (re-export: BaseProvider)

_CV2_NAME = "cv2"
_NUMPY_NAME = "numpy"
_PIL_NAME = "PIL"


# ---------------------------------------------------------------------------
# 惰性引擎加载（顶层零重依赖）
# ---------------------------------------------------------------------------

def module_available(name: str) -> bool:
    """``importlib.util.find_spec`` 探测模块是否可导入（不真正导入）。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # 破坏的父包等：视为不可用（诚实）
        return False


def cv2_available() -> bool:
    """OpenCV（cv2）是否可导入。"""
    return module_available(_CV2_NAME)


def numpy_available() -> bool:
    """numpy 是否可导入。"""
    return module_available(_NUMPY_NAME)


def _import_module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception as exc:  # pragma: no cover - 受限环境
        raise RecognitionError(
            "ENGINE_UNAVAILABLE",
            "识别引擎不可用：无法导入 %s（须在探针 venv 运行）" % name,
            retryable=False,
        ) from exc


def require_cv2() -> Any:
    """获取 cv2；缺失 → ``ENGINE_UNAVAILABLE``（retryable=false）。"""
    if not cv2_available():
        raise RecognitionError(
            "ENGINE_UNAVAILABLE",
            "识别引擎不可用：未安装 OpenCV（cv2）（须在探针 venv 运行）",
            retryable=False,
        )
    return _import_module(_CV2_NAME)


def require_numpy() -> Any:
    """获取 numpy；缺失 → ``ENGINE_UNAVAILABLE``（retryable=false）。"""
    if not numpy_available():
        raise RecognitionError(
            "ENGINE_UNAVAILABLE",
            "识别引擎不可用：未安装 numpy（须在探针 venv 运行）",
            retryable=False,
        )
    return _import_module(_NUMPY_NAME)


# ---------------------------------------------------------------------------
# 纯逻辑辅助（无 cv2/numpy 分支；受管 Python 真实执行）
# ---------------------------------------------------------------------------

def clamp01(value: float) -> float:
    """把分数收敛到 ``[0,1]``（NaN → 0.0）。分数**不是概率**。"""
    number = float(value)
    if number != number:  # NaN
        return 0.0
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


def hue_distance(a: int, b: int) -> int:
    """HSV 色相圆周最短距离（契约 ``h∈[0,359]``；返回 ``[0,180]``）。"""
    diff = abs(int(a) - int(b)) % 360
    return diff if diff <= 180 else 360 - diff


def enumerate_scales(lo: float, hi: float, step: float) -> List[float]:
    """确定性多尺度取点（升序、去重、含端点、非整除补上界；TASK-014 Q8）。

    ``scales = {lo + k*step : k=0..K}``，``K = floor((hi-lo)/step + 1e-9)``；
    若末点 ``< hi`` 且 ``hi-lo`` 非 ``step`` 整数倍，则**补入** ``hi``。
    """
    low = float(lo)
    high = float(hi)
    delta = float(step)
    if delta <= 0:
        raise ValueError("step 必须为正")
    if low > high:
        raise ValueError("scale_range 下界不得大于上界")
    scales: List[float] = []
    k = 0
    while True:
        value = low + k * delta
        if value > high + 1e-9:
            break
        scales.append(round(value, 6))
        k += 1
        if k > 1_000_000:  # pragma: no cover - 防御
            break
    if not scales:
        scales = [round(low, 6)]
    if abs(scales[-1] - high) > 1e-9:
        scales.append(round(high, 6))
    # 升序去重（确定性）
    return sorted(set(scales))


def rgb_within(
    r: int, g: int, b: int, target: Dict[str, Any], tolerance: Dict[str, Any]
) -> bool:
    """RGB 逐通道绝对差 ≤ 容差（全通道 AND；02）。"""
    return bool(
        abs(int(r) - int(target["r"])) <= int(tolerance["r"])
        and abs(int(g) - int(target["g"])) <= int(tolerance["g"])
        and abs(int(b) - int(target["b"])) <= int(tolerance["b"])
    )


def hsv_within(
    h: int, s: int, v: int, target: Dict[str, Any], tolerance: Dict[str, Any]
) -> bool:
    """HSV 容差判定：h 用圆周最短距离，s/v 绝对差（全通道 AND；02）。

    ``s``/``v`` 为**契约域** ``[0,100]``（整数或浮点），比较用浮点（避免二次取整）。
    """
    return bool(
        hue_distance(h, target["h"]) <= int(tolerance["h"])
        and abs(float(s) - float(target["s"])) <= float(tolerance["s"])
        and abs(float(v) - float(target["v"])) <= float(tolerance["v"])
    )


def connected_components_8(
    mask_rows: Sequence[Sequence[bool]],
) -> List[Tuple[int, int, int, int, int]]:
    """8 连通的**紧致**分量包围盒：返回 ``(min_x, min_y, max_x, max_y, area)`` 列表。

    纯 Python（无 cv2/numpy）。语义与 ``cv2.connectedComponentsWithStats(..., connectivity=8)``
    一致的关键判据：**每个连通分量独立成候选**，绝不用「所有匹配像素的全局包围盒」
    （避免把左右分离的两团像素拼成一个巨大框）。
    """
    height = len(mask_rows)
    if height == 0:
        return []
    width = len(mask_rows[0])
    if width == 0:
        return []
    visited = [[False] * width for _ in range(height)]
    components: List[Tuple[int, int, int, int, int]] = []
    for y in range(height):
        row = mask_rows[y]
        for x in range(width):
            if not row[x] or visited[y][x]:
                continue
            stack = [(x, y)]
            visited[y][x] = True
            min_x = max_x = x
            min_y = max_y = y
            area = 0
            while stack:
                cx, cy = stack.pop()
                area += 1
                if cx < min_x:
                    min_x = cx
                if cx > max_x:
                    max_x = cx
                if cy < min_y:
                    min_y = cy
                if cy > max_y:
                    max_y = cy
                for dy in (-1, 0, 1):
                    ny = cy + dy
                    if ny < 0 or ny >= height:
                        continue
                    neighbour_row = mask_rows[ny]
                    for dx in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        nx = cx + dx
                        if nx < 0 or nx >= width:
                            continue
                        if neighbour_row[nx] and not visited[ny][nx]:
                            visited[ny][nx] = True
                            stack.append((nx, ny))
            components.append((min_x, min_y, max_x, max_y, area))
    return components


def select_keys(params: Any, keys: Sequence[str]) -> Dict[str, Any]:
    """只取 ``keys`` 中**出现**的键（供复用叶校验器时的算法参数子集）。"""
    if not isinstance(params, dict):
        return {}
    return {key: params[key] for key in keys if key in params}


def run_leaf_validator(validator: Any, params: Dict[str, Any]) -> None:
    """运行 ``types.conditions`` 叶校验器；``TypeValidationError`` → ``RecognitionError``。"""
    try:
        validator(params)
    except TypeValidationError as exc:
        raise RecognitionError(
            exc.code, exc.message, field=exc.field, retryable=exc.retryable
        ) from exc


# ---------------------------------------------------------------------------
# 局部 ROI（TASK-016 T16-2；纯 Python，无 cv2/numpy 分支）
# ---------------------------------------------------------------------------

class CroppedPixels(NamedTuple):
    """裁剪后的帧像素（图像局部坐标；``offset`` 为裁剪原点）。

    Provider 在裁剪区上检测后，把候选/多边形坐标 **+offset** 平移回「源帧局部像素」
    （``space`` 仍 = ``frame.space``）；**不做** ``frame.origin`` 平移（归消费层，一次）。
    """

    bgr: bytes
    width: int
    height: int
    offset_x: int
    offset_y: int


def clamp_rect(
    x: int, y: int, width: int, height: int, frame_width: int, frame_height: int
) -> Optional[Tuple[int, int, int, int]]:
    """把矩形夹取到 ``[0,frame_width]×[0,frame_height]``。

    Returns:
        ``(x0, y0, width, height)``（宽高 ≥1）；完全落帧外 → ``None``。
    """
    fw = int(frame_width)
    fh = int(frame_height)
    x0 = max(0, min(int(x), fw))
    y0 = max(0, min(int(y), fh))
    x1 = max(0, min(int(x) + int(width), fw))
    y1 = max(0, min(int(y) + int(height), fh))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def crop_roi(raw: Any, roi: Any) -> Optional[CroppedPixels]:
    """从只读像素 ``raw`` 裁剪 ``roi``（图像局部坐标；``None`` → 整帧、offset 0）。

    - ``roi is None``：行为与现状**逐字一致**（整帧、offset ``(0,0)``）；
    - ``roi = LocalRect``：夹取到帧内后按行裁剪（纯 bytes 切片，无重依赖）；
    - 完全落帧外 → ``None``（调用方据此返回空结果；不是错误）。

    Returns:
        :class:`CroppedPixels` 或 ``None``。
    """
    if roi is None:
        return CroppedPixels(
            bgr=raw.bgr, width=int(raw.width), height=int(raw.height),
            offset_x=0, offset_y=0,
        )
    clamped = clamp_rect(roi.x, roi.y, roi.width, roi.height, raw.width, raw.height)
    if clamped is None:
        return None
    x0, y0, cw, ch = clamped
    row_bytes = int(raw.width) * 3
    data = raw.bgr
    out = bytearray()
    for yy in range(y0, y0 + ch):
        start = (yy * int(raw.width) + x0) * 3
        out += data[start:start + cw * 3]
    return CroppedPixels(bgr=bytes(out), width=cw, height=ch, offset_x=x0, offset_y=y0)


# ---------------------------------------------------------------------------
# 算法 Provider 基类
# ---------------------------------------------------------------------------

class BaseAlgorithmProvider(BaseProvider):
    """算法 Provider 基类：``initialize`` 探测引擎依赖；``_read`` 读取只读像素。

    ``recognize`` 由子类实现；本类只提供：
    - ``initialize``：``require_cv2`` + ``require_numpy``（缺失 → ``ENGINE_UNAVAILABLE``，
      注册表据此置能力 ``unavailable+reason``，调用时抛 ``ENGINE_UNAVAILABLE``）；
    - ``_read``：只读像素读取（``self._pixels.read``，无写句柄）；
    - ``_provider_result``：归一原始候选（**不**做 NMS/截断，由基座统一施加）。
    """

    def __init__(self, manifest: Any, pixels: Any) -> None:
        super().__init__(manifest)
        self._pixels = pixels
        self._initialized = False

    def initialize(self, model_dir: Any) -> None:
        """探测引擎依赖（惰性）；缺失 → ``ENGINE_UNAVAILABLE``。"""
        require_cv2()
        require_numpy()
        self._initialized = True

    def _read(self, image_id: Any) -> Any:
        if not isinstance(image_id, str) or not image_id:
            raise RecognitionError("INVALID_PARAMS", "image_id 必填且为非空字符串",
                                   field="image_id")
        return self._pixels.read(image_id)

    def _crop(self, raw: Any, roi: Any) -> Optional[CroppedPixels]:
        """把只读像素裁剪到局部 ROI（TASK-016）；``roi=None`` → 整帧（offset 0）。

        组合层（evaluator）传入的 ``roi`` 为**图像局部坐标**的 ``LocalRect``；
        返回 ``None`` 表示 ROI 完全落帧外。
        """
        return crop_roi(raw, roi)
