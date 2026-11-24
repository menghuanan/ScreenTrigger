# -*- coding: utf-8 -*-
"""TASK-013 manifest loader 单测（``backend.vision.manifest``）。

覆盖 design §4.2/Q5：``validate_provider``、``check_api_version``（未知 major → ENGINE_UNAVAILABLE，
retryable=false，field=provider_manifest.api_version）、``load_model_manifest``（缺模型无联网 →
MODEL_UNAVAILABLE、坏档/字段非法定位）。
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

from backend.tests._vision_fakes import make_model_manifest_dict  # noqa: E402
from backend.vision.manifest import (  # noqa: E402
    SUPPORTED_API_MAJOR,
    check_api_version,
    load_model_manifest,
    models_dir_for,
    validate_provider,
)
from backend.vision.types import RecognitionError  # noqa: E402


class ValidateProviderTest(unittest.TestCase):
    def test_valid_provider_manifest(self):
        manifest = validate_provider({
            "provider_id": "p1", "api_version": 1, "provider_version": "1.0.0",
            "supported_condition_types": ["template", "color"], "model_ids": ["m1"],
            "supported_languages": [], "thread_safe": False, "offline": True,
        })
        self.assertEqual(manifest.provider_id, "p1")
        self.assertEqual(manifest.supported_condition_types, ("template", "color"))

    def test_bad_condition_type_rejected(self):
        with self.assertRaises(RecognitionError) as ctx:
            validate_provider({
                "provider_id": "p1", "api_version": 1, "provider_version": "1.0.0",
                "supported_condition_types": ["nope"], "model_ids": ["m1"],
                "supported_languages": [], "thread_safe": False, "offline": True,
            })
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")


class CheckApiVersionTest(unittest.TestCase):
    def test_known_major_ok(self):
        manifest = validate_provider({
            "provider_id": "p1", "api_version": SUPPORTED_API_MAJOR, "provider_version": "1.0.0",
            "supported_condition_types": ["template"], "model_ids": ["m1"],
            "supported_languages": [], "thread_safe": False, "offline": True,
        })
        check_api_version(manifest)  # 不抛

    def test_unknown_major_rejected(self):
        manifest = validate_provider({
            "provider_id": "p1", "api_version": 2, "provider_version": "1.0.0",
            "supported_condition_types": ["template"], "model_ids": ["m1"],
            "supported_languages": [], "thread_safe": False, "offline": True,
        })
        with self.assertRaises(RecognitionError) as ctx:
            check_api_version(manifest)
        self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.details.get("field"), "provider_manifest.api_version")


class LoadModelManifestTest(unittest.TestCase):
    def test_missing_file_model_unavailable(self):
        models_dir = tempfile.mkdtemp(prefix="st-vision-models-")
        path = os.path.join(models_dir, "m1", "manifest.json")
        with self.assertRaises(RecognitionError) as ctx:
            load_model_manifest(path)
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")
        self.assertEqual(ctx.exception.details.get("field"), "model_id")

    def test_valid_model_manifest(self):
        models_dir = tempfile.mkdtemp(prefix="st-vision-models-")
        model_dir = os.path.join(models_dir, "m1")
        os.makedirs(model_dir, exist_ok=True)
        with open(os.path.join(model_dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(make_model_manifest_dict("m1"), handle)
        manifest = load_model_manifest(os.path.join(model_dir, "manifest.json"))
        self.assertEqual(manifest.model_ids, ("m1",))
        self.assertEqual(manifest.license, "Apache-2.0")

    def test_bad_json_model_unavailable(self):
        models_dir = tempfile.mkdtemp(prefix="st-vision-models-")
        model_dir = os.path.join(models_dir, "m1")
        os.makedirs(model_dir, exist_ok=True)
        with open(os.path.join(model_dir, "manifest.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(RecognitionError) as ctx:
            load_model_manifest(os.path.join(model_dir, "manifest.json"))
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")

    def test_bad_sha_field_rejected(self):
        models_dir = tempfile.mkdtemp(prefix="st-vision-models-")
        model_dir = os.path.join(models_dir, "m1")
        os.makedirs(model_dir, exist_ok=True)
        bad = make_model_manifest_dict("m1", sha256="xyz")
        with open(os.path.join(model_dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(bad, handle)
        with self.assertRaises(RecognitionError) as ctx:
            load_model_manifest(os.path.join(model_dir, "manifest.json"))
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")


class ModelsDirTest(unittest.TestCase):
    def test_models_dir_is_absolute(self):
        path = models_dir_for("some/relative/dir")
        self.assertTrue(os.path.isabs(path))
        self.assertTrue(path.endswith("models"))


if __name__ == "__main__":
    unittest.main()
