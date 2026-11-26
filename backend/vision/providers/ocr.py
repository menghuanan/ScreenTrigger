# -*- coding: utf-8 -*-
"""``OCREngineProvider``（TASK-015 T03）：第 4 个内置 Provider（``condition_type=ocr``）。

- **只加载显式已安装模型**（Q6）：``initialize`` 只读仓内目录 ``manifest/models.catalog.json``
  与权重库根（``~/.paddlex/official_models``，可配置），**逐文件 sha256 校验**；缺引擎/缺模型
  → 各自明确错误码 + 能力披露（``unavailable + reason``，规范格式见 ``ocr_models``）；**绝不下载/联网**。
- **构造引擎**：显式传入 ``text_detection_model_name/dir`` 与 ``text_recognition_model_name/dir``
  （Q12 探针实证：仅传 dir 会与默认模型名不匹配；配对来自 ``engines``），
  并固定 ``enable_mkldnn=False``（PIR/onednn 崩溃规避）与关闭 doc 预处理流水线。
- **输出结构**：paddleocr 结果 → ``TextBlock{text 原文, confidence, polygon(源图 0 基像素), space,
  model_id, language}``；``space=frame.space``（**不做** origin 平移，Q1；平移归消费层
  ``textmatch.translate_polygon_to_space``）；``language`` 取模型身份语言，无法判定 → ``None``（不编造）。
- **只读端口**：仅 ``self._pixels.read``；**不执行动作 / 不写库**。协作式取消点覆盖推理前置与逐块转换。

**顶层零重依赖**：模块顶层只 import 标准库与 ``backend.*`` 纯 Python 模块；``paddleocr/numpy/cv2``
**只在函数体内**惰性加载（受管 Python 无这些依赖时 ``import backend.vision`` 仍为零重依赖）。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from typing import Any, Dict, List, Optional, Tuple

from ...types.capabilities import ProviderManifest
from ...types.region import Point
from ..cancel import CancelToken, CancelledError
from ..textmatch import translate_polygon_to_space  # noqa: F401  (re-export：消费层平移入口)
from ..types import ProviderResult, RecognitionError, TextBlock
from .base import BaseProvider
from ._algo import clamp01, crop_roi
from .ocr_models import (
    REPO_CATALOG_PATH,
    OcrModelCatalog,
    format_ocr_reason,
    resolve_library_root,
    seed_repo_model_metadata,
)

_PADDLEOCR_NAME = "paddleocr"
_NUMPY_NAME = "numpy"

#: ``recognize_text`` 的请求模型必须 ∈ 此清单（识别端；检测模型内部依赖，不可请求）。
_MANIFEST = ProviderManifest(
    provider_id="builtin.ocr",
    api_version=1,
    provider_version="1.0.0",
    supported_condition_types=("ocr",),
    # 与 manifest/models.catalog.json 的 recognition_models 一致（实证：2 个已装识别模型）。
    model_ids=("PP-OCRv6_medium_rec", "korean_PP-OCRv5_mobile_rec"),
    # 日语映射依据 PaddleOCR 3.7.0 路由及同权重固定样本，见 DEC-002。
    supported_languages=("zh", "en", "ja", "ko"),
    thread_safe=False,
    offline=True,
)

#: 预热图尺寸（把小图冷启动移出 5s job 窗口）。
_WARMUP_WIDTH = 32
_WARMUP_HEIGHT = 16
_KOREAN_SMALL_REGION_MAX_SIDE = 1024
_KOREAN_DETECTOR_MAX_SIDE = 512


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class OCREngineProvider(BaseProvider):
    """OCR 离线引擎 Provider（第 4 个内置 Provider）。

    Attributes（测试/诊断可见）:
        models_dir: ``initialize`` 收到的模型目录（registry 读 manifest 处）。
        library_root: 实际权重库根（可审计，Q6）。
        seeded: 本次写入 ``models_dir`` 的仓内元数据 model_id（幂等）。
    """

    def __init__(
        self,
        pixels: Any,
        *,
        paddleocr_module: Any = None,
        catalog: Optional[OcrModelCatalog] = None,
        library_root: Optional[str] = None,
        model_id: Optional[str] = None,
        component_roots: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(_MANIFEST)
        self._pixels = pixels
        self._paddleocr_module = paddleocr_module
        self._catalog: Optional[OcrModelCatalog] = catalog
        self._library_root_override = library_root
        self._selected_model = model_id
        self._component_roots = component_roots or {}
        self._engines: Dict[str, Tuple[Any, Optional[str]]] = {}
        self._initialized = False
        self.models_dir: Optional[str] = None
        self.library_root: Optional[str] = None
        self.library_source: Optional[str] = None
        self.seeded: List[str] = []

    # -- 契约 ---------------------------------------------------------------

    def capabilities(self) -> ProviderManifest:
        if self._selected_model is None:
            return _MANIFEST
        from dataclasses import replace
        return replace(_MANIFEST, model_ids=(self._selected_model,))

    def initialize(self, models_dir: Any) -> None:
        """加载**已安装**模型（只读、离线）；任何失败 → 显式 ``RecognitionError``（不静默）。

        Raises:
            RecognitionError: ``ENGINE_UNAVAILABLE``（缺 paddleocr）/
                ``MODEL_UNAVAILABLE``（目录/模型文件缺失）/``DATA_CORRUPT``（sha256 不符）/
                ``RECOGNITION_FAILED``（引擎构造或预热失败）。
        """
        self.models_dir = models_dir if isinstance(models_dir, str) else None
        self._ensure_catalog()
        self.library_root, self.library_source = resolve_library_root(self._library_root_override)
        paddle = self._require_paddleocr()
        self._verify_library()
        # 成功路径：把仓内 02 布局元数据 seed 到 models_dir（仅元数据、幂等），
        # 使 registry.load_model_manifest 可读到「已安装模型」。
        self.seeded = seed_repo_model_metadata(self.models_dir or "")
        for engine in self._catalog.engines:  # type: ignore[union-attr]
            if self._selected_model is not None and engine.recognition_model != self._selected_model:
                continue
            self._engines[engine.recognition_model] = (
                self._build_engine(paddle, engine.detection_model, engine.recognition_model),
                self._catalog.language_of(engine.recognition_model),  # type: ignore[union-attr]
            )
        self._warmup()
        self._initialized = True

    def recognize(self, frame: Any, params: Dict[str, Any], cancel: CancelToken,
                  roi: Any = None) -> ProviderResult:
        """单次 OCR 推理 → ``ProviderResult(text_blocks=...)``（只产原始，NMS/上限由基座）。

        TASK-016：``roi`` 非 None 时只在帧的 ROI 子区上推理，并把 polygon 平移回
        「源帧局部像素」（``space`` 仍 = ``frame.space``；**不做** origin 平移，归消费层）。
        """
        cancel.throw_if_cancelled()
        if not self._initialized:
            raise self._unavailable_error("识别 Provider 未初始化")
        model_id = params.get("model_id")
        engine = self._engines.get(model_id) if isinstance(model_id, str) else None
        if engine is None:
            raise RecognitionError("MODEL_UNAVAILABLE", "模型未安装: %s" % (model_id,),
                                   field="model_id", retryable=False)
        full_raw = self._pixels.read(frame.image_ref.image_id)
        cancel.throw_if_cancelled()
        cropped = crop_roi(full_raw, roi)
        if cropped is None:
            return self._provider_result()  # ROI 完全落帧外 → 空文本块（非错误）
        raw = cropped
        off_x = int(cropped.offset_x)
        off_y = int(cropped.offset_y)
        engine_ocr, language = engine
        detection_options = {}
        if language == "ko" and max(raw.width, raw.height) <= _KOREAN_SMALL_REGION_MAX_SIDE:
            detection_options = {"text_det_limit_type": "max", "text_det_limit_side_len": _KOREAN_DETECTOR_MAX_SIDE}
        try:
            result = engine_ocr.predict(self._to_input(raw), **detection_options)
        except RecognitionError:
            raise
        except Exception as exc:  # 单次原生推理不可中断；异常归一为 RECOGNITION_FAILED
            raise RecognitionError(
                "RECOGNITION_FAILED",
                "OCR 推理内部故障: %s" % type(exc).__name__,
                retryable=False,
            ) from exc
        cancel.throw_if_cancelled()
        # F-01 修复：转换期异常**必须**归为故障（RECOGNITION_FAILED，走 recognition_error），
        # 绝不能静默变成「未命中（found=false）」；取消信号原样透传。
        try:
            blocks = self._to_text_blocks(result, model_id, frame, cancel,
                                          off_x=off_x, off_y=off_y)
        except (CancelledError, RecognitionError):
            raise
        except Exception as exc:
            raise RecognitionError(
                "RECOGNITION_FAILED",
                "OCR 结果转换失败: %s" % type(exc).__name__,
                retryable=False,
            ) from exc
        return self._provider_result(text_blocks=blocks)

    def close(self) -> None:
        """释放引擎（幂等）；失败不抛出。"""
        for engine_ocr, _language in list(self._engines.values()):
            closer = getattr(engine_ocr, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - 释放失败不阻断
                    continue
        self._engines.clear()
        self._closed = True

    # -- 诊断 / 能力披露 -----------------------------------------------------

    def disclosure(self) -> Dict[str, Any]:
        """可审计装配信息（models_dir/library 来源；Q6.b）。"""
        return {
            "provider_id": _MANIFEST.provider_id,
            "models_dir": self.models_dir,
            "library_root": self.library_root,
            "library_source": self.library_source,
            "seeded": list(self.seeded),
            "initialized": self._initialized,
        }

    def cache_fingerprint(self, model_id: Any) -> Dict[str, Any]:
        """缓存键补充分量（§9）：所选模型的**内容 hash** + **语言**。

        service 将其折入 ``params``（不新增 ``cache_key`` 形参）；模型内容/语言变 → 新键 → 必 miss。
        """
        catalog = self._catalog
        if catalog is None or not isinstance(model_id, str) or not model_id:
            return {}
        return {
            "model_sha256": catalog.weight_sha256(model_id) or "",
            "language": catalog.language_of(model_id),
        }

    def _unavailable_reason(self, code: str, detail: str) -> str:
        catalog = self._catalog
        return format_ocr_reason(
            state="unavailable",
            code=code,
            models=(catalog.recognition_models() if catalog is not None else ()),
            verified=(catalog.supported_languages() if catalog is not None else ()),
            unverified=(catalog.unverified_languages() if catalog is not None else ()),
            models_dir=self.models_dir,
            library=self.library_root,
            detail=detail,
        )

    def _unavailable_error(self, detail: str) -> RecognitionError:
        return RecognitionError(
            "ENGINE_UNAVAILABLE", self._unavailable_reason("ENGINE_UNAVAILABLE", detail),
            retryable=False,
        )

    # -- 内部 ---------------------------------------------------------------

    def _ensure_catalog(self) -> None:
        if self._catalog is None:
            try:
                self._catalog = OcrModelCatalog.load(REPO_CATALOG_PATH)
            except RecognitionError as exc:
                raise RecognitionError(exc.code, self._unavailable_reason(exc.code, exc.message),
                                       retryable=False) from exc

    def _require_paddleocr(self) -> Any:
        """惰性获取 paddleocr；缺失 → ``ENGINE_UNAVAILABLE``（能力诚实置 unavailable+reason）。"""
        if self._paddleocr_module is not None:
            return self._paddleocr_module
        if not _module_available(_PADDLEOCR_NAME):
            raise RecognitionError(
                "ENGINE_UNAVAILABLE",
                self._unavailable_reason("ENGINE_UNAVAILABLE",
                                         "未安装 paddleocr（须在探针 venv 运行）"),
                retryable=False,
            )
        try:
            return importlib.import_module(_PADDLEOCR_NAME)
        except Exception as exc:  # 导入期失败（缺 paddlepaddle 等）
            raise RecognitionError(
                "ENGINE_UNAVAILABLE",
                self._unavailable_reason("ENGINE_UNAVAILABLE",
                                         "paddleocr 导入失败: %s" % type(exc).__name__),
                retryable=False,
            ) from exc

    def _verify_library(self) -> None:
        try:
            if self._selected_model is None:
                self._catalog.verify(self.library_root)
            else:
                engine = self._catalog.engine_for(self._selected_model)
                if engine is None:
                    raise RecognitionError("MODEL_UNAVAILABLE", "未知 OCR 管线")
                for key in (engine.detection_model, engine.recognition_model):
                    self._catalog.verify(self._component_roots.get(key, self.library_root), (key,))
        except RecognitionError as exc:
            raise RecognitionError(exc.code, self._unavailable_reason(exc.code, exc.message),
                                   retryable=False) from exc

    def _build_engine(self, paddle: Any, detection_model: str, recognition_model: str) -> Any:
        """构造单个识别引擎（显式本地模型名 + 目录；**不触发下载**）。"""
        if not hasattr(paddle, "PaddleOCR"):
            raise self._unavailable_error("paddleocr 缺少 PaddleOCR 类")
        root = self.library_root or ""
        try:
            return paddle.PaddleOCR(
                text_detection_model_name=detection_model,
                text_detection_model_dir=os.path.join(self._component_roots.get(detection_model, root), detection_model),
                text_recognition_model_name=recognition_model,
                text_recognition_model_dir=os.path.join(self._component_roots.get(recognition_model, root), recognition_model),
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
            )
        except FileNotFoundError as exc:
            raise RecognitionError(
                "MODEL_UNAVAILABLE",
                self._unavailable_reason("MODEL_UNAVAILABLE", "模型文件缺失: %s" % exc),
                retryable=False,
            ) from exc
        except Exception as exc:  # mkldnn/版本/构造失败 → 明确内部故障
            raise RecognitionError(
                "RECOGNITION_FAILED",
                self._unavailable_reason("RECOGNITION_FAILED",
                                         "OCR 引擎构造失败: %s" % type(exc).__name__),
                retryable=False,
            ) from exc

    def _warmup(self) -> None:
        """对小图跑一次推理，把冷启动移出单次 job 的 5s 窗口。"""
        for engine_ocr, _language in self._engines.values():
            try:
                engine_ocr.predict(self._blank_input())
            except Exception as exc:  # 预热失败 = 引擎不可用（诚实置不可用）
                raise RecognitionError(
                    "RECOGNITION_FAILED",
                    self._unavailable_reason("RECOGNITION_FAILED",
                                             "OCR 预热失败: %s" % type(exc).__name__),
                    retryable=False,
                ) from exc

    # -- 输入/输出适配 -------------------------------------------------------

    @staticmethod
    def _numpy_or_none() -> Any:
        try:
            return importlib.import_module(_NUMPY_NAME)
        except Exception:  # noqa: BLE001 - 受管 Python 无 numpy：替身路径容忍原始字节
            return None

    def _to_input(self, raw: Any) -> Any:
        """``RawPixels`` → BGR ndarray（行主序）；无 numpy（替身路径）→ 原始 BGR 字节。"""
        numpy = self._numpy_or_none()
        if numpy is None:
            return raw.bgr
        return numpy.frombuffer(raw.bgr, dtype=numpy.uint8).reshape(
            int(raw.height), int(raw.width), 3
        )

    def _blank_input(self) -> Any:
        width, height = _WARMUP_WIDTH, _WARMUP_HEIGHT
        numpy = self._numpy_or_none()
        if numpy is None:
            return b"\xff" * (width * height * 3)
        return numpy.full((height, width, 3), 255, dtype=numpy.uint8)

    def _to_text_blocks(
        self, result: Any, model_id: str, frame: Any, cancel: CancelToken,
        *, off_x: int = 0, off_y: int = 0,
    ) -> Tuple[TextBlock, ...]:
        """paddleocr 结果 → ``TextBlock``（原文保留；polygon 源图 0 基像素；space=frame.space）。

        TASK-016：``off_x``/``off_y`` 为局部 ROI 原点；polygon 平移回「源帧局部像素」。

        F-01/F-02：paddleocr 字段值可能是 ``numpy.ndarray``（如 ``rec_boxes`` shape=(N,4)），
        **禁止**用 ``value or []``（ndarray 真值判断抛 ``ValueError``）；统一经
        ``_as_sequence`` 显式 None 判定，序号/长度一律用 ``len()``。
        """
        catalog = self._catalog
        language = catalog.language_of(model_id) if catalog is not None else None
        blocks: List[TextBlock] = []
        for page in self._as_sequence(result):
            texts = self._as_sequence(page.get("rec_texts"))
            scores = self._as_sequence(page.get("rec_scores"))
            polys = page.get("rec_polys")
            if polys is None:
                polys = page.get("dt_polys")
            polys = self._as_sequence(polys)
            boxes = self._as_sequence(page.get("rec_boxes"))
            for index, text in enumerate(texts):
                cancel.throw_if_cancelled()
                confidence = clamp01(scores[index]) if index < len(scores) else 0.0
                polygon = self._polygon(polys, boxes, index)
                if polygon is None:
                    continue
                if off_x or off_y:
                    polygon = tuple(
                        Point(x=int(p.x) + int(off_x), y=int(p.y) + int(off_y))
                        for p in polygon
                    )
                blocks.append(
                    TextBlock(
                        text=str(text),
                        confidence=confidence,
                        polygon=polygon,
                        space=frame.space,
                        model_id=str(model_id),
                        language=language,
                    )
                )
        return tuple(blocks)

    @staticmethod
    def _as_sequence(value: Any) -> Any:
        """把 paddleocr 结果字段规整为**可索引序列**（``None`` → ``()``）。

        **禁止真值判断**：真实 ``OCRResult.rec_boxes`` 为 ``numpy.ndarray``，``bool(ndarray)``
        在元素数 >1 时抛 ``ValueError``（F-01 根因）。此 helper 只做**显式 None 判定**，
        绝不调用 ``bool()``；空序列由下游 ``index < len(...)`` 判定。
        """
        if value is None:
            return ()
        return value

    @staticmethod
    def _polygon(polys: Any, boxes: Any, index: int) -> Optional[Tuple[Point, ...]]:
        """取第 ``index`` 个四点 polygon（源图 0 基像素）；无有效几何 → ``None``。"""
        if index < len(polys):
            poly = polys[index]
            points = [(int(round(float(p[0]))), int(round(float(p[1])))) for p in poly]
            if len(points) == 4:
                return tuple(Point(x=x, y=y) for x, y in points)
            if points:
                xs = [x for x, _ in points]
                ys = [y for _, y in points]
                return OCREngineProvider._corners(min(xs), min(ys), max(xs), max(ys))
        if index < len(boxes):
            box = boxes[index]
            if len(box) >= 4:
                return OCREngineProvider._corners(
                    int(round(float(box[0]))), int(round(float(box[1]))),
                    int(round(float(box[2]))), int(round(float(box[3]))),
                )
        return None

    @staticmethod
    def _corners(x1: int, y1: int, x2: int, y2: int) -> Tuple[Point, ...]:
        """由轴对齐 bbox 构造四点 polygon（左上→右上→右下→左下，同序）。"""
        return (Point(x=x1, y=y1), Point(x=x2, y=y1), Point(x=x2, y=y2), Point(x=x1, y=y2))
