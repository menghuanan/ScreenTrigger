# -*- coding: utf-8 -*-
"""TASK-015 OCR 测试替身与夹具（**只依赖标准库 + 基座**）。

提供：

- ``FakeOCRResult`` / ``FakePaddleOCR`` / ``FakePaddleOCRModule``：可编程 paddleocr 替身
  （返回固定 ``rec_texts``/``rec_scores``/``rec_polys``；可注错），使 OCR Provider 的
  **TextBlock 转换 / 坐标 / 语言 / 取消** 等契约在**受管 Python**（无 paddleocr）下真实执行；
- ``build_fake_catalog``：构造内存 ``OcrModelCatalog`` + 临时权重库（文件 hash 与目录一致），
  使 ``provider.initialize`` 的 sha256 校验分支可控；
- ``make_frame``：构造 ``Frame``（可指定 ``space``/``origin``）用于坐标/space 断言。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.types.region import Frame, ImageRef, Point
from backend.vision.providers.ocr_models import (
    OcrEngine,
    OcrModelCatalog,
    OcrModelEntry,
    OcrModelFile,
)


# ---------------------------------------------------------------------------
# paddleocr 替身
# ---------------------------------------------------------------------------

class FakeOCRResult(dict):
    """paddleocr ``predict`` 结果的鸭子类型替身（dict 子集：``rec_texts`` 等）。

    ``rec_polys`` 用嵌套列表 ``[[x,y], ...]`` 表示（与真实 ``ndarray`` 逐行 ``p[i]`` 取用
    一致，受管 Python 无需 numpy）。
    """

    def __init__(
        self,
        texts: Sequence[str],
        scores: Optional[Sequence[float]] = None,
        polys: Optional[Sequence[Sequence[Sequence[int]]]] = None,
    ) -> None:
        super().__init__()
        texts = list(texts)
        self["rec_texts"] = texts
        self["rec_scores"] = list(scores) if scores is not None else [0.9] * len(texts)
        if polys is None:
            polys = [
                [[index * 10, 0], [index * 10 + 8, 0], [index * 10 + 8, 8], [index * 10, 8]]
                for index in range(len(texts))
            ]
        self["rec_polys"] = [list(poly) for poly in polys]


class FakePaddleOCR:
    """可编程 ``PaddleOCR`` 替身。

    Attributes:
        scripted: 类级脚本 ``recognition_model_name -> {"results":[...]} | {"error":exc}``；
            未命中的模型 → 空结果（等价「无文本」）。
        instances: 已构造实例（断言 ``enable_mkldnn=False`` 等 kwargs）。
    """

    scripted: Dict[str, Dict[str, Any]] = {}
    instances: List["FakePaddleOCR"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs: Dict[str, Any] = dict(kwargs)
        self.calls: List[Any] = []
        FakePaddleOCR.instances.append(self)

    def predict(self, input: Any, **kwargs: Any) -> List[FakeOCRResult]:
        self.calls.append(input)
        recognition_model = self.kwargs.get("text_recognition_model_name")
        spec = FakePaddleOCR.scripted.get(str(recognition_model))
        if spec is not None and spec.get("error") is not None:
            raise spec["error"]
        results = (spec or {}).get("results") or []
        return [FakeOCRResult(**item) for item in results]

    @classmethod
    def reset(cls) -> None:
        cls.scripted = {}
        cls.instances = []


class FakePaddleOCRModule:
    """``import paddleocr`` 的替身模块（顶层属性 ``PaddleOCR``）。"""

    def __init__(self, ocr_class: Any = FakePaddleOCR) -> None:
        self.PaddleOCR = ocr_class
        self.__name__ = "fake_paddleocr"
        self.__spec__ = None


# ---------------------------------------------------------------------------
# 假目录 + 临时权重库
# ---------------------------------------------------------------------------

def build_fake_catalog(
    det_id: str = "fake_det",
    rec_id: str = "fake_rec",
    *,
    languages: Sequence[str] = ("zh", "en"),
    primary_language: Optional[str] = "zh",
    root: Optional[str] = None,
    corrupt: bool = False,
) -> Tuple[OcrModelCatalog, str]:
    """构造内存目录 + 临时权重库（文件 hash 与目录一致；``corrupt=True`` 时故意不符）。"""
    base = root if root is not None else tempfile.mkdtemp(prefix="st-ocr-lib-")

    def _write(model_id: str) -> Tuple[OcrModelFile, ...]:
        model_dir = os.path.join(base, model_id)
        os.makedirs(model_dir, exist_ok=True)
        files = []
        for name in ("inference.yml", "inference.pdiparams"):
            path = os.path.join(model_dir, name)
            data = ("%s/%s" % (model_id, name)).encode("utf-8")
            with open(path, "wb") as handle:
                handle.write(data)
            digest = hashlib.sha256(data).hexdigest()
            if corrupt and name == "inference.pdiparams":
                digest = "0" * 64
            files.append(OcrModelFile(name=name, bytes=len(data), sha256=digest))
        return tuple(files)

    det_entry = OcrModelEntry(
        model_id=det_id, family="fake", kind="det", languages=(), primary_language=None,
        license="Apache-2.0", source="official_models/%s" % det_id, files=_write(det_id),
    )
    rec_entry = OcrModelEntry(
        model_id=rec_id, family="fake", kind="rec", languages=tuple(languages),
        primary_language=primary_language, license="Apache-2.0",
        source="official_models/%s" % rec_id, files=_write(rec_id),
    )
    catalog = OcrModelCatalog(
        entries=(det_entry, rec_entry),
        engines=(OcrEngine(detection_model=det_id, recognition_model=rec_id,
                           languages=tuple(languages)),),
        first_batch_languages=("zh", "en", "ja", "ko"),
    )
    return catalog, base


# ---------------------------------------------------------------------------
# Frame 夹具
# ---------------------------------------------------------------------------

def make_frame(
    *,
    image_id: str = "img-1",
    space: str = "image_pixel",
    origin: Tuple[int, int] = (0, 0),
    width: int = 100,
    height: int = 40,
    geometry_revision: int = 0,
    binding_id: Optional[str] = None,
) -> Frame:
    """构造 ``Frame``（可指定 space/origin；用于坐标与 space 断言）。"""
    return Frame(
        frame_id="frame-" + image_id,
        image_ref=ImageRef(image_id=image_id, width=width, height=height, format="png",
                           sha256="a" * 64),
        space=space,
        origin=Point(x=int(origin[0]), y=int(origin[1])),
        captured_at="2026-10-13T00:00:00Z",
        geometry_revision=int(geometry_revision),
        binding_id=binding_id,
    )
