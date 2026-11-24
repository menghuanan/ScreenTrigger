# -*- coding: utf-8 -*-
"""TASK-010 契约测试：冻结的 manifest schema（Q4）与生成器一致。

Q4 授权 TASK-010 冻结最小 manifest schema（为 02「schema、文件 hashes、计数」的
超集且不冲突），要求**机器声明新增 schema/ + 契约测试**。本用例用**内置最小
JSON-Schema 子集校验器**（无第三方依赖）比对 ``generation.build_manifest`` 的
产出与 ``schema/manifest.schema.json``，并覆盖正/负例。
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.storage import generation  # noqa: E402
from backend.storage.errors import StorageError  # noqa: E402

_SCHEMA_PATH = os.path.join(_REPO_ROOT, "schema", "manifest.schema.json")


def validate_schema(instance, schema, path="$"):
    """最小 JSON-Schema 子集校验器（type/required/properties/additionalProperties/items/enum/pattern/minLength/minimum）。"""
    errors = []
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(instance, dict):
            return ["%s: 期望 object" % path]
        for key in schema.get("required", []):
            if key not in instance:
                errors.append("%s: 缺少必填键 %s" % (path, key))
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    errors.append("%s: 多余键 %s" % (path, key))
        for key, value in instance.items():
            if key in properties:
                errors += validate_schema(value, properties[key], "%s.%s" % (path, key))
    elif expected_type == "array":
        if not isinstance(instance, list):
            return ["%s: 期望 array" % path]
        items = schema.get("items")
        if items:
            for index, value in enumerate(instance):
                errors += validate_schema(value, items, "%s[%d]" % (path, index))
    elif expected_type == "integer":
        if isinstance(instance, bool) or not isinstance(instance, int):
            return ["%s: 期望 integer" % path]
    elif expected_type == "string":
        if not isinstance(instance, str):
            return ["%s: 期望 string" % path]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("%s: 不在枚举 %s" % (path, schema["enum"]))
    if "pattern" in schema and isinstance(instance, str):
        if re.search(schema["pattern"], instance) is None:
            errors.append("%s: 不匹配 pattern %s" % (path, schema["pattern"]))
    if "minLength" in schema and isinstance(instance, str) and len(instance) < schema["minLength"]:
        errors.append("%s: 长度小于 %d" % (path, schema["minLength"]))
    if "minimum" in schema and isinstance(instance, int) and instance < schema["minimum"]:
        errors.append("%s: 小于最小值 %d" % (path, schema["minimum"]))
    return errors


class ManifestContractTest(unittest.TestCase):
    def setUp(self):
        with open(_SCHEMA_PATH, encoding="utf-8") as handle:
            self.schema = json.load(handle)

    def _manifest(self):
        raw_targets = generation.encode_json({"schema_version": 2, "next_sample_no": 3, "targets": []})
        raw_groups = generation.encode_json({"schema_version": 2, "groups": []})
        raw_script = generation.encode_json(generation.minimal_script_metadata())
        return generation.build_manifest(
            "g-abc",
            (
                ("targets.json", raw_targets),
                ("groups.json", raw_groups),
                ("script_metadata.json", raw_script),
            ),
            {"targets": 2, "groups": 0, "scripts": 0, "next_sample_no": 3},
            "2026-09-21T09:00:00Z",
        )

    def test_produced_manifest_validates(self):
        manifest = self._manifest()
        self.assertEqual(validate_schema(manifest, self.schema), [])
        self.assertEqual(set(manifest), set(generation.MANIFEST_KEYS))
        self.assertLessEqual(set(generation.MANIFEST_KEYS), set(self.schema["properties"]))

    def test_round_trip_through_parse_manifest(self):
        manifest = self._manifest()
        parsed = generation.parse_manifest(generation.encode_manifest(manifest))
        self.assertEqual(parsed, manifest)

    def test_schema_rejects_wrong_type_and_extra_key(self):
        manifest = self._manifest()
        manifest["counts"]["targets"] = -1
        self.assertTrue(validate_schema(manifest, self.schema))
        manifest = self._manifest()
        manifest["extra"] = 1
        self.assertTrue(validate_schema(manifest, self.schema))

    def test_future_schema_version_rejected(self):
        manifest = self._manifest()
        manifest["schema_version"] = 3
        with self.assertRaises(StorageError) as ctx:
            generation.parse_manifest(generation.encode_manifest(manifest))
        self.assertEqual(getattr(ctx.exception, "code", None), "SCHEMA_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
