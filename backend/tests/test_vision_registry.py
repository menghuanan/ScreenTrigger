# -*- coding: utf-8 -*-
"""TASK-013 Provider 注册表单测（``backend.vision.registry``；§5 / Q10）。

覆盖：内置工厂注册（重复 → ValueError）、``initialize_all``（就绪/不可用）、``provider_for``、
``capability_reason``（能力诚实置位）、api_version 未知 major 拒绝、缺模型不可用、
**同一条件类型唯一 provider**（重复声明装配期失败）、``installed_model_manifests``、``close_all``。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.tests._vision_fakes import (  # noqa: E402
    RecordingPixelReader,
    make_manifest,
    make_model_manifest_dict,
)
from backend.vision.cancel import NullCancelToken  # noqa: E402
from backend.vision.providers.base import FakeProvider, NoopProvider  # noqa: E402
from backend.vision.registry import (  # noqa: E402
    BUILTIN_PROVIDER_FACTORIES,
    ProviderRegistry,
    register_builtin_provider,
)
from backend.vision.service import VisionService  # noqa: E402
from backend.vision.types import RecognitionError  # noqa: E402


def _models_dir() -> str:
    return tempfile.mkdtemp(prefix="st-vision-reg-")


class FactoryRegistrationTest(unittest.TestCase):
    def test_duplicate_factory_rejected(self):
        reg = ProviderRegistry(RecordingPixelReader(), factories={})
        reg.register_factory("p", lambda pixels: NoopProvider(make_manifest("p")))
        with self.assertRaises(ValueError):
            reg.register_factory("p", lambda pixels: NoopProvider(make_manifest("p")))

    def test_register_builtin_duplicate_rejected(self):
        provider_id = "test-builtin-dup"
        try:
            register_builtin_provider(provider_id, lambda pixels: None)
            with self.assertRaises(ValueError):
                register_builtin_provider(provider_id, lambda pixels: None)
        finally:
            BUILTIN_PROVIDER_FACTORIES.pop(provider_id, None)


class InitializeAllTest(unittest.TestCase):
    def test_ready_provider_available(self):
        def factory(pixels):
            return FakeProvider(make_manifest("p", condition_types=("template",)), pixels)
        reg = ProviderRegistry(RecordingPixelReader(), factories={"p": factory})
        reg.initialize_all(_models_dir())
        self.assertIsNotNone(reg.provider_for("template"))
        self.assertIsNone(reg.provider_for("ocr"))
        self.assertEqual(reg.capability_reason("template"), ("available", None))

    def test_unknown_api_major_unavailable(self):
        def factory(pixels):
            return FakeProvider(make_manifest("p", api_version=2, condition_types=("template",)), pixels)
        reg = ProviderRegistry(RecordingPixelReader(), factories={"p": factory})
        reg.initialize_all(_models_dir())
        self.assertIsNone(reg.provider_for("template"))
        state, reason = reg.capability_reason("template")
        self.assertEqual(state, "unavailable")
        self.assertTrue(reason)

    def test_missing_model_unavailable(self):
        def factory(pixels):
            return FakeProvider(make_manifest("p", condition_types=("ocr",), model_ids=("m1",)), pixels)
        reg = ProviderRegistry(RecordingPixelReader(), factories={"p": factory})
        reg.initialize_all(_models_dir())  # 无 models/m1/manifest.json
        self.assertIsNone(reg.provider_for("ocr"))
        self.assertEqual(reg.capability_reason("ocr")[0], "unavailable")

    def test_model_installed_is_reported(self):
        models_dir = _models_dir()
        model_dir = os.path.join(models_dir, "m1")
        os.makedirs(model_dir, exist_ok=True)
        with open(os.path.join(model_dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(make_model_manifest_dict("m1"), handle)

        def factory(pixels):
            return FakeProvider(make_manifest("p", condition_types=("ocr",), model_ids=("m1",)), pixels)
        reg = ProviderRegistry(RecordingPixelReader(), factories={"p": factory})
        reg.initialize_all(models_dir)
        self.assertIsNotNone(reg.provider_for("ocr"))
        manifests = reg.installed_model_manifests()
        self.assertEqual(len(manifests), 1)
        self.assertEqual(manifests[0].model_ids, ("m1",))

    def test_initialize_failure_marks_unavailable(self):
        def factory(pixels):
            return FakeProvider(
                make_manifest("p", condition_types=("template",)), pixels,
                initialize_error=RuntimeError("boom"),
            )
        reg = ProviderRegistry(RecordingPixelReader(), factories={"p": factory})
        reg.initialize_all(_models_dir())
        self.assertIsNone(reg.provider_for("template"))
        self.assertEqual(reg.capability_reason("template")[0], "unavailable")

    def test_duplicate_condition_type_assembly_fails(self):
        def factory_a(pixels):
            return FakeProvider(make_manifest("a", condition_types=("template",)), pixels)

        def factory_b(pixels):
            return FakeProvider(make_manifest("b", condition_types=("template",)), pixels)

        reg = ProviderRegistry(RecordingPixelReader(), factories={"a": factory_a, "b": factory_b})
        with self.assertRaises(ValueError):
            reg.initialize_all(_models_dir())


class CapabilityHonestyTest(unittest.TestCase):
    def test_empty_builtin_reports_unavailable(self):
        reg = ProviderRegistry(RecordingPixelReader(), factories={})
        reg.initialize_all(_models_dir())
        state, reason = reg.capability_reason("ocr")
        self.assertEqual(state, "unavailable")
        self.assertIn("未装配", reason)

    def test_close_all_idempotent(self):
        def factory(pixels):
            return FakeProvider(make_manifest("p", condition_types=("template",)), pixels)
        reg = ProviderRegistry(RecordingPixelReader(), factories={"p": factory})
        reg.initialize_all(_models_dir())
        reg.close_all()
        reg.close_all()  # 幂等，不抛


class BuiltinOcrRegistrationTest(unittest.TestCase):
    def test_builtin_ocr_factory_registered(self):
        # TASK-015：第 4 个内置 Provider（加法式；不改既有工厂表语义）。
        self.assertIn("builtin.ocr", BUILTIN_PROVIDER_FACTORIES)
        for provider_id in ("builtin.template", "builtin.contour", "builtin.color", "builtin.ocr"):
            self.assertIn(provider_id, BUILTIN_PROVIDER_FACTORIES)


class ErrorCodeClassificationTest(unittest.TestCase):
    """Q2（ISSUE-TASK013-002）三条 unavailable 分支**按原因分类一致** + 独立对抗用例。

    - 缺引擎 → ``ENGINE_UNAVAILABLE``
    - 缺模型 → ``MODEL_UNAVAILABLE``（**非** ``CAPABILITY_DENIED``；Q2 加法式修复后）
    - 未装配 → ``CAPABILITY_DENIED``
    """

    def _service(self, registry):
        return VisionService(registry, pixels=RecordingPixelReader())

    def test_missing_engine_is_engine_unavailable(self):
        def factory(pixels):
            return FakeProvider(
                make_manifest("p", condition_types=("ocr",)), pixels,
                initialize_error=RecognitionError("ENGINE_UNAVAILABLE", "缺 cv2/paddleocr"),
            )
        registry = ProviderRegistry(RecordingPixelReader(), factories={"p": factory})
        registry.initialize_all(_models_dir())
        self.assertIsNone(registry.provider_for("ocr"))
        self.assertEqual(registry.unavailable_code("ocr"), "ENGINE_UNAVAILABLE")
        service = self._service(registry)
        with self.assertRaises(RecognitionError) as ctx:
            service._execute_provider("recognize_text", {"image_id": "x"}, NullCancelToken())  # noqa: SLF001
        self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")

    def test_missing_model_is_model_unavailable(self):
        def factory(pixels):
            return FakeProvider(
                make_manifest("p", condition_types=("ocr",), model_ids=("m-missing",)), pixels
            )
        registry = ProviderRegistry(RecordingPixelReader(), factories={"p": factory})
        registry.initialize_all(_models_dir())  # 无 models/m-missing/manifest.json
        self.assertIsNone(registry.provider_for("ocr"))
        # 关键：Q2 修复后模型缺失分支记 MODEL_UNAVAILABLE（此前落 CAPABILITY_DENIED）
        self.assertEqual(registry.unavailable_code("ocr"), "MODEL_UNAVAILABLE")
        service = self._service(registry)
        with self.assertRaises(RecognitionError) as ctx:
            service._execute_provider("recognize_text", {"image_id": "x"}, NullCancelToken())  # noqa: SLF001
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")

    def test_corrupt_weights_and_initialization_failure_remain_distinct(self):
        from backend.vision.condition_evaluator import ConditionTreeEvaluator
        from types import SimpleNamespace

        for expected in ("DATA_CORRUPT", "RECOGNITION_FAILED"):
            with self.subTest(expected=expected):
                def factory(pixels):
                    return FakeProvider(make_manifest("p", condition_types=("ocr",)), pixels,
                                        initialize_error=RecognitionError(expected, "fixture init failure"))
                registry = ProviderRegistry(RecordingPixelReader(), factories={"p": factory})
                registry.initialize_all(_models_dir())
                with self.assertRaises(RecognitionError) as direct:
                    self._service(registry)._execute_provider("recognize_text", {}, NullCancelToken())
                with self.assertRaises(RecognitionError) as leaf:
                    ConditionTreeEvaluator(registry=registry)._invoke_provider(
                        SimpleNamespace(type="ocr"), None, None, NullCancelToken(), None)
                self.assertEqual(direct.exception.code, expected)
                self.assertEqual(leaf.exception.to_dict(), direct.exception.to_dict())

    def test_unassembled_is_capability_denied(self):
        registry = ProviderRegistry(RecordingPixelReader(), factories={})
        registry.initialize_all(_models_dir())
        self.assertIsNone(registry.provider_for("ocr"))
        self.assertIsNone(registry.unavailable_code("ocr"))
        service = self._service(registry)
        with self.assertRaises(RecognitionError) as ctx:
            service._execute_provider("recognize_text", {"image_id": "x"}, NullCancelToken())  # noqa: SLF001
        self.assertEqual(ctx.exception.code, "CAPABILITY_DENIED")

    def test_ready_provider_returns_none_code(self):
        def factory(pixels):
            return FakeProvider(make_manifest("p", condition_types=("ocr",)), pixels)
        registry = ProviderRegistry(RecordingPixelReader(), factories={"p": factory})
        registry.initialize_all(_models_dir())
        self.assertIsNotNone(registry.provider_for("ocr"))
        self.assertIsNone(registry.unavailable_code("ocr"))


if __name__ == "__main__":
    unittest.main()
