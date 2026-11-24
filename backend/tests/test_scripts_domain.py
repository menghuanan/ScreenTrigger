# -*- coding: utf-8 -*-
"""TASK-012 领域层单测（backend/scripts/domain.py）：纯函数，无 IO/落盘/事件。

覆盖设计 §7 规则与 §15 裁决：

- 源码：``str`` + 严格 UTF-8（孤立代理对拒绝）+ 编码后**字节** ≤ 1MiB（Q8）；
- 名称：去首尾空白后 1–100 Unicode 码点；
- 参数：五字段（复用 ``types.conditions``）+ **名称唯一性**（Q9）+ ``default`` 合法
  JsonValue 且一级类型与 ``type`` 一致（Q9；``null`` 跳过）；
- ``source_relative_path``：``scripts/<id>/<hash>.lua`` 形态 + ``<id>`` 组件安全；
- script 定位 / revision 判定 / 排序 / 增删改（纯 dict/tuple 变换）；
- ``build_script_dict`` / ``is_noop_save``（Q3）。

运行：python -m unittest backend.tests.test_scripts_domain -v
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.scripts import domain  # noqa: E402
from backend.storage.errors import StorageError  # noqa: E402

_TS = "2026-09-21T09:00:00Z"
_HEX64 = "a" * 64


def _code(ctx) -> str:
    return getattr(ctx.exception, "code", None)


def _field(ctx):
    return getattr(ctx.exception, "details", {}).get("field")


def _param(name="count", type_="number", required=False, default=3, description=""):
    return {
        "name": name,
        "type": type_,
        "required": required,
        "default": default,
        "description": description,
    }


class ValidateSourceTest(unittest.TestCase):
    """源码 UTF-8 + 字节预算（Q8）。"""

    def test_accepts_str_and_returns_bytes(self):
        raw = domain.validate_source("return 1")
        self.assertEqual(raw, b"return 1")

    def test_non_str_rejected_field_source(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_source(123)
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx), "source")

    def test_lone_surrogate_rejected_field_source(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_source("a\ud800b")
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx), "source")

    def test_exactly_one_mib_ok(self):
        source = "a" * domain.SOURCE_MAX_BYTES
        self.assertEqual(len(domain.validate_source(source)), domain.SOURCE_MAX_BYTES)

    def test_one_byte_over_budget_resource_limit(self):
        source = "a" * (domain.SOURCE_MAX_BYTES + 1)
        with self.assertRaises(StorageError) as ctx:
            domain.validate_source(source)
        self.assertEqual(_code(ctx), "RESOURCE_LIMIT")
        self.assertEqual(_field(ctx), "source")

    def test_budget_counted_in_bytes_not_codepoints(self):
        # "é" 为 2 字节：524288 个字符 = 1048576 字节 → 恰好通过
        ok = "é" * (domain.SOURCE_MAX_BYTES // 2)
        self.assertEqual(len(domain.validate_source(ok)), domain.SOURCE_MAX_BYTES)
        # 多一个字符（+2 字节）→ 超限
        over = ok + "é"
        with self.assertRaises(StorageError) as ctx:
            domain.validate_source(over)
        self.assertEqual(_code(ctx), "RESOURCE_LIMIT")
        self.assertEqual(_field(ctx), "source")


class NormalizeNameTest(unittest.TestCase):
    """名称：去首尾空白后 1–100 码点。"""

    def test_strips_whitespace(self):
        self.assertEqual(domain.normalize_name("  脚本  "), "脚本")

    def test_empty_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.normalize_name("   ")
        self.assertEqual(_field(ctx), "name")

    def test_non_str_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.normalize_name(None)
        self.assertEqual(_field(ctx), "name")

    def test_boundary_100_ok_101_invalid(self):
        self.assertEqual(len(domain.normalize_name("a" * 100)), 100)
        with self.assertRaises(StorageError) as ctx:
            domain.normalize_name("a" * 101)
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx), "name")


class ValidateParametersTest(unittest.TestCase):
    """参数五字段 + 唯一性 + default JsonValue/类型（Q9）。"""

    def test_valid_parameters_roundtrip(self):
        params = domain.validate_parameters([_param()])
        self.assertEqual(len(params), 1)
        self.assertEqual(set(params[0].keys()), {"name", "type", "required", "default", "description"})

    def test_non_array_rejected_field_parameters(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_parameters({"name": "x"})
        self.assertEqual(_field(ctx), "parameters")

    def test_duplicate_names_rejected_field_parameters(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_parameters([_param(name="a"), _param(name="a")])
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx), "parameters")

    def test_required_with_default_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_parameters([_param(required=True, default=3)])
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx), "parameters")

    def test_required_with_null_default_ok(self):
        params = domain.validate_parameters([_param(required=True, default=None)])
        self.assertIsNone(params[0]["default"])

    def test_bad_type_enum_field_parameters_type(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_parameters([_param(type_="integer")])
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx), "parameters.type")

    def test_default_type_mismatch_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_parameters([_param(type_="number", default="x")])
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx), "parameters")

    def test_default_null_skips_type_check(self):
        params = domain.validate_parameters([_param(type_="number", default=None)])
        self.assertIsNone(params[0]["default"])

    def test_default_nan_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_parameters([_param(type_="number", default=float("nan"))])
        self.assertEqual(_field(ctx), "parameters")

    def test_default_object_array_accepts_nested_jsonvalue(self):
        params = domain.validate_parameters(
            [_param(type_="object", default={"a": [1, 2, None, True, "x"]})]
        )
        self.assertEqual(params[0]["default"], {"a": [1, 2, None, True, "x"]})

    def test_extra_param_field_rejected(self):
        bad = _param()
        bad["extra"] = 1
        with self.assertRaises(StorageError) as ctx:
            domain.validate_parameters([bad])
        # 复用 _validate_script_parameter 的固定前缀定位（parameters.<多余键>）
        self.assertTrue(str(_field(ctx)).startswith("parameters"))


class SourceRelativePathTest(unittest.TestCase):
    """scripts/<id>/<hash>.lua 形态 + 组件安全。"""

    def test_valid_form(self):
        self.assertEqual(
            domain.source_relative_path("s1", _HEX64),
            "scripts/s1/%s.lua" % _HEX64,
        )

    def test_rejects_separator_in_id(self):
        for bad in ("a/b", "a\\b", "..", ".", ""):
            with self.subTest(id=bad):
                with self.assertRaises(StorageError) as ctx:
                    domain.source_relative_path(bad, _HEX64)
                self.assertEqual(_code(ctx), "INVALID_PARAMS")
                self.assertEqual(_field(ctx), "source_path")


class ScriptMetadataTest(unittest.TestCase):
    """元数据文档读写 / 定位 / revision / 排序 / 增删改（纯 dict/tuple 变换）。"""

    def _script(self, sid="s1", revision=1, created_at=_TS, name="脚本"):
        return domain.build_script_dict(
            script_id=sid,
            revision=revision,
            name=name,
            source_path=domain.source_relative_path(sid, _HEX64),
            source_sha256=_HEX64,
            parameters=domain.validate_parameters([_param()]),
            created_at=created_at,
            updated_at=_TS,
        )

    def test_build_script_dict_keys(self):
        script = self._script()
        self.assertEqual(
            set(script.keys()),
            {"id", "revision", "name", "source_path", "source_sha256",
             "parameters", "created_at", "updated_at"},
        )

    def test_scripts_from_snapshot_missing_returns_empty(self):
        class _Snap:
            script_metadata = None

        self.assertEqual(domain.scripts_from_snapshot(_Snap()), [])

    def test_scripts_from_snapshot_reads_list(self):
        class _Snap:
            script_metadata = {"schema_version": 2, "scripts": [{"id": "s1"}]}

        self.assertEqual(domain.scripts_from_snapshot(_Snap()), [{"id": "s1"}])

    def test_metadata_document_wraps_scripts(self):
        doc = domain.script_metadata_document([self._script()])
        self.assertEqual(set(doc.keys()), {"schema_version", "scripts"})
        self.assertEqual(len(doc["scripts"]), 1)

    def test_find_and_ensure_not_found(self):
        scripts = [self._script()]
        self.assertIsNotNone(domain.find_script(scripts, "s1"))
        with self.assertRaises(StorageError) as ctx:
            domain.ensure_script_found(domain.find_script(scripts, "nope"), "nope")
        self.assertEqual(_code(ctx), "SCRIPT_NOT_FOUND")
        self.assertEqual(_field(ctx), "script_id")

    def test_revision_check_conflict_field(self):
        script = self._script(revision=3)
        domain.ensure_script_revision(script, 3)  # 通过
        with self.assertRaises(StorageError) as ctx:
            domain.ensure_script_revision(script, 2)
        self.assertEqual(_code(ctx), "CONFLICT")
        self.assertEqual(_field(ctx), "expected_revision")

    def test_sort_created_at_then_id(self):
        a = self._script(sid="b", created_at="2026-09-21T09:00:00Z")
        b = self._script(sid="a", created_at="2026-09-21T09:00:00Z")
        c = self._script(sid="c", created_at="2026-09-20T09:00:00Z")
        ordered = domain.sort_scripts([a, b, c])
        self.assertEqual([s["id"] for s in ordered], ["c", "a", "b"])

    def test_upsert_and_remove(self):
        scripts = [self._script(sid="s1", revision=1)]
        updated = self._script(sid="s1", revision=2)
        merged = domain.upsert_script(scripts, updated)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["revision"], 2)
        added = domain.upsert_script(merged, self._script(sid="s2"))
        self.assertEqual(len(added), 2)
        removed = domain.remove_script(added, "s1")
        self.assertEqual([s["id"] for s in removed], ["s2"])

    def test_is_noop_save(self):
        script = self._script(name="登录", revision=1)
        params = domain.validate_parameters([_param()])
        self.assertTrue(domain.is_noop_save(script, "登录", params, _HEX64))
        self.assertFalse(domain.is_noop_save(script, "改名", params, _HEX64))
        self.assertFalse(domain.is_noop_save(script, "登录", params, "b" * 64))
        other = domain.validate_parameters([_param(name="other")])
        self.assertFalse(domain.is_noop_save(script, "登录", other, _HEX64))


if __name__ == "__main__":
    unittest.main()
