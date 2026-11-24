# -*- coding: utf-8 -*-
"""TASK-015 T01 单测：OCR 模型目录 / 库解析 / 元数据 seed / reason 规范格式。

覆盖（纯标准库；受管 Python 与探针 venv 两环境**同样真实执行**）：

- ``OcrModelCatalog.load`` 读仓内权威目录 ``manifest/models.catalog.json``（模型/引擎/语言）；
- 语言映射（``language_of``/``model_ids_for_language``/``supported_languages``/``unverified_languages``）
  与 ``required_model_ids``/``recognition_models``/``weight_sha256``；
- ``verify`` 三态（存在 → 通过；缺文件 → ``MODEL_UNAVAILABLE``；hash 不符 → ``DATA_CORRUPT``）；
- ``resolve_models_dir``（优先 ``--models-dir``/``ST_MODELS_DIR``，其次 ``<data_dir>/models/``）
  与 ``resolve_library_root``（``ST_OCR_MODELS_DIR`` 覆盖，缺省 ``~/.paddlex/official_models``）；
- ``seed_repo_model_metadata``（把仓内 02 布局 manifest 落盘，幂等、不覆盖既有）；
- ``format_ocr_reason``/``parse_ocr_reason`` 固定规范格式（Q8）与注入防护。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.tests._vision_ocr_fakes import build_fake_catalog  # noqa: E402
from backend.vision.types import RecognitionError  # noqa: E402
from backend.vision.providers.ocr_models import (  # noqa: E402
    ENV_LIBRARY_ROOT,
    ENV_MODELS_DIR,
    REPO_CATALOG_PATH,
    REASON_FIELD_ORDER,
    OcrModelCatalog,
    format_ocr_reason,
    parse_ocr_reason,
    resolve_library_root,
    resolve_models_dir,
    seed_repo_model_metadata,
)


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class CatalogLoadTest(unittest.TestCase):
    """仓内权威目录加载与访问器。"""

    def setUp(self):
        self.catalog = OcrModelCatalog.load(REPO_CATALOG_PATH)

    def test_repo_catalog_has_models_and_engines(self):
        self.assertGreaterEqual(len(self.catalog.entries), 2)
        self.assertIn("PP-OCRv6_medium_rec", self.catalog.installed_model_ids())
        self.assertIn("korean_PP-OCRv5_mobile_rec", self.catalog.installed_model_ids())
        self.assertTrue(self.catalog.engines)

    def test_engine_pairing(self):
        zh = self.catalog.engine_for("PP-OCRv6_medium_rec")
        self.assertIsNotNone(zh)
        self.assertEqual(zh.detection_model, "PP-OCRv6_medium_det")
        kor = self.catalog.engine_for("korean_PP-OCRv5_mobile_rec")
        self.assertIsNotNone(kor)
        self.assertEqual(kor.detection_model, "PP-OCRv5_server_det")

    def test_language_of_only_confirmed(self):
        # korean 模型身份语言来自 `korean` 前缀（实证）；多语通用模型 → None（不编造，Q4）
        self.assertEqual(self.catalog.language_of("korean_PP-OCRv5_mobile_rec"), "ko")
        self.assertIsNone(self.catalog.language_of("PP-OCRv6_medium_rec"))
        self.assertIsNone(self.catalog.language_of("nonexistent-model"))

    def test_supported_and_unverified_languages(self):
        supported = set(self.catalog.supported_languages())
        # 实证语言：en/zh（PP-OCRv6 rec）+ ko（korean rec）
        self.assertTrue({"en", "zh", "ko"} <= supported)
        # 首批声明含 ja，但无 japan 识别模型 → 未实证（如实披露，不宣称）
        self.assertIn("ja", self.catalog.unverified_languages())
        self.assertNotIn("ja", supported)

    def test_model_ids_for_language(self):
        self.assertIn("korean_PP-OCRv5_mobile_rec", self.catalog.model_ids_for_language("ko"))
        self.assertEqual(self.catalog.model_ids_for_language("ja"), ())

    def test_recognition_and_required_models(self):
        rec = self.catalog.recognition_models()
        self.assertIn("PP-OCRv6_medium_rec", rec)
        required = self.catalog.required_model_ids()
        # 引擎运行所需 = 检测 + 识别（去重、有序）
        self.assertIn("PP-OCRv6_medium_det", required)
        self.assertIn("korean_PP-OCRv5_mobile_rec", required)
        self.assertEqual(len(required), len(set(required)))

    def test_weight_sha256_real_value(self):
        digest = self.catalog.weight_sha256("PP-OCRv6_medium_rec")
        self.assertIsInstance(digest, str)
        self.assertEqual(len(digest), 64)
        self.assertIsNone(self.catalog.weight_sha256("nonexistent-model"))

    def test_load_missing_file_raises_model_unavailable(self):
        with self.assertRaises(RecognitionError) as ctx:
            OcrModelCatalog.load(os.path.join(tempfile.mkdtemp(), "nope.json"))
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")

    def test_load_bad_json_raises_model_unavailable(self):
        path = os.path.join(tempfile.mkdtemp(), "bad.json")
        _write(path, "{not json")
        with self.assertRaises(RecognitionError) as ctx:
            OcrModelCatalog.load(path)
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")

    def test_load_bad_structure_raises_model_unavailable(self):
        path = os.path.join(tempfile.mkdtemp(), "struct.json")
        _write(path, json.dumps({"models": "not-a-list"}))
        with self.assertRaises(RecognitionError) as ctx:
            OcrModelCatalog.load(path)
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")

    def test_entry_missing_model_id_raises(self):
        path = os.path.join(tempfile.mkdtemp(), "entry.json")
        _write(path, json.dumps({"models": [{"family": "x"}]}))
        with self.assertRaises(RecognitionError) as ctx:
            OcrModelCatalog.load(path)
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")


class VerifyTest(unittest.TestCase):
    """逐文件 sha256 校验三态。"""

    def test_verify_ok(self):
        catalog, root = build_fake_catalog()
        catalog.verify(root)  # 不抛即通过

    def test_verify_missing_file_raises_model_unavailable(self):
        catalog, root = build_fake_catalog()
        os.remove(os.path.join(root, "fake_rec", "inference.yml"))
        with self.assertRaises(RecognitionError) as ctx:
            catalog.verify(root)
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")

    def test_verify_corrupt_raises_data_corrupt(self):
        catalog, root = build_fake_catalog(corrupt=True)
        with self.assertRaises(RecognitionError) as ctx:
            catalog.verify(root)
        self.assertEqual(ctx.exception.code, "DATA_CORRUPT")

    def test_verify_unknown_model_raises_model_unavailable(self):
        catalog, root = build_fake_catalog()
        with self.assertRaises(RecognitionError) as ctx:
            catalog.verify(root, ("mystery",))
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")

    def test_verify_empty_root_raises_model_unavailable(self):
        catalog, _root = build_fake_catalog()
        with self.assertRaises(RecognitionError) as ctx:
            catalog.verify("")
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")


class ResolveTest(unittest.TestCase):
    """目录/库解析（Q6：优先数据根 models/，其次可配置；不联网）。"""

    def test_models_dir_default_is_data_dir_models(self):
        data_dir = tempfile.mkdtemp(prefix="st-ocr-data-")
        resolved, source = resolve_models_dir(data_dir)
        self.assertEqual(source, "data_dir_default")
        self.assertTrue(resolved.endswith(os.path.join(os.path.abspath(data_dir), "models")))

    def test_models_dir_override(self):
        target = tempfile.mkdtemp(prefix="st-ocr-override-")
        resolved, source = resolve_models_dir("", override=target)
        self.assertEqual(source, "config")
        self.assertEqual(resolved, os.path.abspath(target))

    def test_models_dir_env(self):
        target = tempfile.mkdtemp(prefix="st-ocr-env-")
        previous = os.environ.get(ENV_MODELS_DIR)
        os.environ[ENV_MODELS_DIR] = target
        try:
            resolved, source = resolve_models_dir("")
            self.assertEqual(source, "config")
            self.assertEqual(resolved, os.path.abspath(target))
        finally:
            if previous is None:
                os.environ.pop(ENV_MODELS_DIR, None)
            else:
                os.environ[ENV_MODELS_DIR] = previous

    def test_models_dir_requires_input(self):
        previous = os.environ.pop(ENV_MODELS_DIR, None)
        try:
            with self.assertRaises(ValueError):
                resolve_models_dir("")
        finally:
            if previous is not None:
                os.environ[ENV_MODELS_DIR] = previous

    def test_library_root_default(self):
        target = tempfile.mkdtemp(prefix="st-ocr-libenv-")
        previous = os.environ.get(ENV_LIBRARY_ROOT)
        os.environ.pop(ENV_LIBRARY_ROOT, None)
        try:
            resolved, source = resolve_library_root()
            self.assertEqual(source, "default")
            self.assertTrue(resolved.endswith(os.path.join(".paddlex", "official_models")))
        finally:
            if previous is not None:
                os.environ[ENV_LIBRARY_ROOT] = previous
        shutil.rmtree(target, ignore_errors=True)

    def test_library_root_env_override(self):
        target = tempfile.mkdtemp(prefix="st-ocr-lib-")
        previous = os.environ.get(ENV_LIBRARY_ROOT)
        os.environ[ENV_LIBRARY_ROOT] = target
        try:
            resolved, source = resolve_library_root()
            self.assertEqual(source, "config")
            self.assertEqual(resolved, os.path.abspath(target))
        finally:
            if previous is None:
                os.environ.pop(ENV_LIBRARY_ROOT, None)
            else:
                os.environ[ENV_LIBRARY_ROOT] = previous


class SeedTest(unittest.TestCase):
    """仓内元数据 seed（仅元数据、幂等、不覆盖）。"""

    def test_seed_copies_repo_metadata(self):
        models_dir = tempfile.mkdtemp(prefix="st-ocr-seed-")
        seeded = seed_repo_model_metadata(models_dir)
        self.assertIn("PP-OCRv6_medium_rec", seeded)
        self.assertIn("korean_PP-OCRv5_mobile_rec", seeded)
        self.assertTrue(
            os.path.isfile(os.path.join(models_dir, "PP-OCRv6_medium_rec", "manifest.json"))
        )

    def test_seed_idempotent(self):
        models_dir = tempfile.mkdtemp(prefix="st-ocr-seed2-")
        seed_repo_model_metadata(models_dir)
        self.assertEqual(seed_repo_model_metadata(models_dir), [])

    def test_seed_does_not_overwrite_existing(self):
        models_dir = tempfile.mkdtemp(prefix="st-ocr-seed3-")
        target = os.path.join(models_dir, "PP-OCRv6_medium_rec", "manifest.json")
        _write(target, "SENTINEL")
        seed_repo_model_metadata(models_dir)
        with open(target, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "SENTINEL")

    def test_seed_empty_dir_is_noop(self):
        self.assertEqual(seed_repo_model_metadata(""), [])


class ReasonFormatTest(unittest.TestCase):
    """`capabilities[ocr].reason` 固定规范格式（Q8）。"""

    def test_format_has_prefix_and_field_order(self):
        reason = format_ocr_reason(
            state="unavailable", code="ENGINE_UNAVAILABLE",
            models=("PP-OCRv6_medium_rec",), verified=("en", "zh", "ko"),
            unverified=("ja",), models_dir="/d/models", library="/l/official_models",
            detail="未安装 paddleocr",
        )
        self.assertTrue(reason.startswith("OCR|"))
        keys = [chunk.split("=", 1)[0] for chunk in reason.split("|")[1:]]
        self.assertEqual(tuple(keys), REASON_FIELD_ORDER)

    def test_roundtrip_parse(self):
        reason = format_ocr_reason(
            state="unavailable", code="MODEL_UNAVAILABLE",
            models=("a", "b"), verified=("en",), unverified=("ja", "ko"),
            models_dir="/d", library="/l", detail="x",
        )
        parsed = parse_ocr_reason(reason)
        self.assertEqual(parsed["state"], "unavailable")
        self.assertEqual(parsed["code"], "MODEL_UNAVAILABLE")
        self.assertEqual(parsed["models"], ["a", "b"])
        self.assertEqual(parsed["verified"], ["en"])
        self.assertEqual(parsed["unverified"], ["ja", "ko"])

    def test_empty_values_use_placeholder(self):
        reason = format_ocr_reason(state="unavailable")
        parsed = parse_ocr_reason(reason)
        self.assertIsNone(parsed["code"])
        self.assertEqual(parsed["models"], [])
        self.assertIsNone(parsed["models_dir"])

    def test_pipe_injection_is_sanitized(self):
        reason = format_ocr_reason(state="unavailable", detail="a|b")
        # 注入的 `|` 被替换 → 仍可解析且字段数不漂移
        self.assertIsNotNone(parse_ocr_reason(reason))
        self.assertNotIn("a|b", reason)

    def test_parse_rejects_malformed(self):
        self.assertIsNone(parse_ocr_reason(None))
        self.assertIsNone(parse_ocr_reason("not-ocr"))
        self.assertIsNone(parse_ocr_reason("OCR|state=unavailable"))
        self.assertIsNone(parse_ocr_reason("OCR|state=x|unknown=1"))


if __name__ == "__main__":
    unittest.main()
