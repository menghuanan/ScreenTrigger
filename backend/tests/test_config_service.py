# -*- coding: utf-8 -*-
"""ConfigService 单测（TASK-006 批 B2，设计 §9.2）。

覆盖：
- 三端共享 config_patch 用例（schema/contract-cases.json 11 条，B1 扩展）：
  patch/expectedRevision/expect 三元组逐字执行，失败断言 configUnchanged
  （文件字节不变 + 内存 revision 不变）；
- 状态机：首次初始化/load 幂等/update/reset 循环/revision 单调/内容=默认值；
- 坏档恢复：备份恢复（revision 以备份为准）、无备份只读失败（不建空库覆盖、
  坏档不被覆盖）、人工处置（删除坏档）后恢复、SCHEMA_UNSUPPORTED 拒写、
  同版本未知字段按 DATA_CORRUPT、文件含 NaN 字面量拒绝；
- 原子保存故障注入（os.fsync/os.replace/备份拷贝）→ STORAGE_ERROR retryable
  且旧档完好；并发 update 写锁串行性；默认值与 schema default 防漂移比对；
  dispatcher 三命令接线与错误映射；日志不记 patch 参数全文。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.config import (  # noqa: E402
    CONFIG_LIMITS,
    ConfigError,
    ConfigService,
    default_config,
    load_schema_limits,
    register_config_commands,
    schema_defaults_path,
)
from backend.ipc.dispatcher import Dispatcher  # noqa: E402

_CASES_PATH = os.path.join(_REPO_ROOT, "schema", "contract-cases.json")


def _read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def _write_bytes(path, raw):
    with open(path, "wb") as handle:
        handle.write(raw)


def _make_service(logger=None):
    data_dir = tempfile.mkdtemp(prefix="st-config-")
    service = ConfigService(data_dir, logger=logger)
    return service, data_dir


def _field_value(doc, field):
    current = doc
    for part in field.split("."):
        current = current[part]
    return current


class _CaptureTransport:
    """内存传输替身：send 捕获信封，read_line 立即 EOF。"""

    def __init__(self):
        self.sent = []

    def send(self, envelope):
        self.sent.append(envelope)
        return True

    def read_line(self):
        return None


class _SessionStub:
    session_id = "s-test"


class _QuietLogger:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)


# ---------------------------------------------------------------------------
# 三端共享 config_patch 用例（同一份 JSON 逐字执行）
# ---------------------------------------------------------------------------

class SharedConfigPatchCasesTest(unittest.TestCase):
    def setUp(self):
        with open(_CASES_PATH, "r", encoding="utf-8") as handle:
            self.document = json.load(handle)
        self._dirs = []

    def tearDown(self):
        for data_dir in self._dirs:
            shutil.rmtree(data_dir, ignore_errors=True)

    def _make_service(self, logger):
        service, data_dir = _make_service(logger)
        self._dirs.append(data_dir)
        return service, data_dir

    def _shared_cases(self):
        return [
            case for case in self.document["cases"]
            if case.get("kind") == "config_patch"
            and "python" in case.get("applies_to", [])
        ]

    def test_shared_config_patch_cases(self):
        cases = self._shared_cases()
        self.assertGreaterEqual(len(cases), 11, "config_patch 共享用例不得回退")
        for case in cases:
            with self.subTest(case=case["id"]):
                logger = _QuietLogger()
                service, data_dir = self._make_service(logger)
                service.load()  # 首次初始化 revision=1
                before_revision = service.get_config()["revision"]
                before = _read_bytes(os.path.join(data_dir, "config.json"))
                if case.get("reset"):
                    operation = lambda: service.reset_config(case["expectedRevision"])
                else:
                    operation = lambda: service.update_config(
                        case["patch"], case["expectedRevision"]
                    )
                expect = case["expect"]
                try:
                    doc = operation()
                    actual_success, actual_error = True, None
                except ConfigError as exc:
                    actual_success, actual_error, doc = False, exc, None
                self.assertEqual(actual_success, expect["success"],
                                 "用例 %s success 判定不一致" % case["id"])
                if expect["success"]:
                    self.assertEqual(doc["revision"], expect["revision"],
                                     "用例 %s revision 不一致" % case["id"])
                    if "field" in expect:
                        self.assertEqual(_field_value(doc, expect["field"]),
                                         expect["value"],
                                         "用例 %s 字段值不一致" % case["id"])
                else:
                    self.assertEqual(actual_error.code, expect["errorCode"],
                                     "用例 %s 错误码不一致" % case["id"])
                    if "field" in expect:
                        self.assertEqual(actual_error.details.get("field"),
                                         expect["field"],
                                         "用例 %s 错误字段不一致" % case["id"])
                    if expect.get("configUnchanged"):
                        after = _read_bytes(os.path.join(data_dir, "config.json"))
                        self.assertEqual(after, before,
                                         "用例 %s 非法操作不得改写文件" % case["id"])
                        current = service.get_config()
                        self.assertEqual(current["revision"], before_revision,
                                         "用例 %s 失败后 revision 不得推进" % case["id"])


# ---------------------------------------------------------------------------
# 状态机与基础循环（§9.2）
# ---------------------------------------------------------------------------

class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.logger = _QuietLogger()
        self.service, self.data_dir = _make_service(self.logger)

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_first_init_writes_defaults(self):
        doc = self.service.load()
        self.assertEqual(self.service.state, ConfigService.STATE_READY)
        self.assertTrue(os.path.exists(self.service.config_path))
        self.assertEqual(doc["revision"], 1)
        self.assertEqual(doc["schema_version"], 2)
        self.assertEqual(doc, default_config())

    def test_load_idempotent(self):
        first = self.service.load()
        second = self.service.load()
        self.assertEqual(first, second)

    def test_update_reset_cycle_revision_monotonic(self):
        doc1 = self.service.load()
        doc2 = self.service.update_config(
            {"recognition": {"default_threshold": 0.9}}, 1
        )
        self.assertEqual(doc2["revision"], 2)
        self.assertEqual(doc2["recognition"]["default_threshold"], 0.9)
        self.assertEqual(doc2["general"], doc1["general"])  # 深合并不动其他字段
        doc3 = self.service.reset_config(2)
        self.assertEqual(doc3["revision"], 3)
        self.assertEqual(doc3, dict(default_config(), revision=3))
        # 磁盘与内存一致
        on_disk = json.loads(_read_bytes(self.service.config_path).decode("utf-8"))
        self.assertEqual(on_disk, doc3)

    def test_failed_patch_leaves_untouched(self):
        self.service.load()
        before = _read_bytes(self.service.config_path)
        with self.assertRaises(ConfigError) as ctx:
            self.service.update_config({"foo": 1}, 1)
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(ctx.exception.details.get("field"), "foo")
        self.assertEqual(_read_bytes(self.service.config_path), before)
        self.assertEqual(self.service.get_config()["revision"], 1)

    def test_log_does_not_contain_param_values(self):
        self.service.load()
        self.service.update_config({"recognition": {"default_threshold": 0.9}}, 1)
        joined = "\n".join(self.logger.lines)
        self.assertIn("recognition.default_threshold", joined)  # 记路径
        self.assertNotIn("0.9", joined)  # 不记值（06 不记录参数全文）


# ---------------------------------------------------------------------------
# 坏档恢复状态机（§5.3/§8）
# ---------------------------------------------------------------------------

class CorruptedRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.logger = _QuietLogger()
        self.service, self.data_dir = _make_service(self.logger)
        self.service.load()
        # 产生一份 revision=1 的备份
        self.service.update_config({"general": {"theme": "dark"}}, 1)

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _fresh(self):
        service = ConfigService(self.data_dir, logger=self.logger)
        return service

    def test_backup_recovery_on_corruption(self):
        _write_bytes(self.service.config_path, b"this is { not json")
        recovered = self._fresh().load()
        # 备份 = 上一次成功替换前的旧档（revision=1、theme=system），revision 以备份为准
        self.assertEqual(recovered["revision"], 1)
        self.assertEqual(recovered["general"]["theme"], "system")
        self.assertIn("备份恢复", "\n".join(self.logger.lines))
        on_disk = json.loads(_read_bytes(self.service.config_path).decode("utf-8"))
        self.assertEqual(on_disk, recovered)  # 原子拷贝回 config.json

    def test_unknown_field_same_version_uses_backup(self):
        doc = json.loads(_read_bytes(self.service.config_path).decode("utf-8"))
        doc["mystery"] = 1
        _write_bytes(self.service.config_path,
                     json.dumps(doc, ensure_ascii=False).encode("utf-8"))
        recovered = self._fresh().load()
        self.assertEqual(recovered["revision"], 1)  # 备份 = 更新前旧档
        self.assertEqual(recovered["general"]["theme"], "system")
        self.assertNotIn("mystery", recovered)

    def test_no_backup_read_only_failed(self):
        _write_bytes(self.service.config_path, b"{truncated")
        shutil.rmtree(os.path.join(self.data_dir, "backups"))
        service = self._fresh()
        with self.assertRaises(ConfigError) as ctx:
            service.get_config()
        self.assertEqual(ctx.exception.code, "DATA_CORRUPT")
        self.assertEqual(service.state, ConfigService.STATE_READ_ONLY_FAILED)
        with self.assertRaises(ConfigError) as ctx:
            service.update_config({}, 1)
        self.assertEqual(ctx.exception.code, "DATA_CORRUPT")
        with self.assertRaises(ConfigError):
            service.reset_config(1)
        # 不建空库覆盖：坏档原样保留
        self.assertEqual(_read_bytes(self.service.config_path), b"{truncated")

    def test_manual_disposal_restores_ready(self):
        _write_bytes(self.service.config_path, b"{truncated")
        shutil.rmtree(os.path.join(self.data_dir, "backups"))
        service = self._fresh()
        with self.assertRaises(ConfigError):
            service.get_config()
        # 人工处置：删除坏档 → 下一次 load 走首次初始化
        os.remove(self.service.config_path)
        doc = service.load()
        self.assertEqual(service.state, ConfigService.STATE_READY)
        self.assertEqual(doc["revision"], 1)

    def test_future_schema_rejected_and_preserved(self):
        doc = json.loads(_read_bytes(self.service.config_path).decode("utf-8"))
        doc["schema_version"] = 3
        doc["brand_new_field"] = {"keep": True}
        raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        _write_bytes(self.service.config_path, raw)
        service = self._fresh()
        with self.assertRaises(ConfigError) as ctx:
            service.get_config()
        self.assertEqual(ctx.exception.code, "SCHEMA_UNSUPPORTED")
        self.assertEqual(service.state, ConfigService.STATE_READ_ONLY_FAILED)
        with self.assertRaises(ConfigError):
            service.update_config({}, 3)
        # 未知字段不被静默删除：文件字节不变
        self.assertEqual(_read_bytes(self.service.config_path), raw)

    def test_nan_literal_in_file_rejected(self):
        # 先移除备份：否则坏档会走备份恢复成功路径而非 DATA_CORRUPT
        shutil.rmtree(os.path.join(self.data_dir, "backups"))
        _write_bytes(self.service.config_path,
                     b'{"schema_version":2,"revision":1,"recognition":'
                     b'{"default_threshold":NaN}}')
        service = self._fresh()
        with self.assertRaises(ConfigError) as ctx:
            service.load()
        self.assertEqual(ctx.exception.code, "DATA_CORRUPT")


# ---------------------------------------------------------------------------
# 原子保存故障注入（§5.4 步骤 6 → STORAGE_ERROR）
# ---------------------------------------------------------------------------

class AtomicSaveFaultTest(unittest.TestCase):
    def setUp(self):
        self.service, self.data_dir = _make_service(_QuietLogger())
        self.service.load()
        self.before = _read_bytes(self.service.config_path)

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _assert_storage_error_and_intact(self):
        with self.assertRaises(ConfigError) as ctx:
            self.service.update_config({"general": {"theme": "dark"}}, 1)
        self.assertEqual(ctx.exception.code, "STORAGE_ERROR")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(_read_bytes(self.service.config_path), self.before)
        self.assertEqual(self.service.get_config()["revision"], 1)

    def test_fsync_failure_storage_error(self):
        with mock.patch("backend.config.service.os.fsync", side_effect=OSError("boom")):
            self._assert_storage_error_and_intact()

    def test_replace_failure_storage_error(self):
        with mock.patch("backend.config.service.os.replace", side_effect=OSError("boom")):
            self._assert_storage_error_and_intact()

    def test_backup_copy_failure_storage_error(self):
        with mock.patch("backend.config.service._copy_file", side_effect=OSError("boom")):
            self._assert_storage_error_and_intact()

    def test_concurrent_updates_serialized(self):
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                results.append(self.service.update_config(
                    {"general": {"theme": "light"}}, 1
                ))
            except ConfigError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "CONFLICT")
        self.assertEqual(results[0]["revision"], 2)


# ---------------------------------------------------------------------------
# 默认值与 schema 防漂移（设计 §4.2"运行时读取比对"）
# ---------------------------------------------------------------------------

class DefaultsSchemaDriftTest(unittest.TestCase):
    def setUp(self):
        with open(schema_defaults_path(), "r", encoding="utf-8") as handle:
            self.schema = json.load(handle)
        self.config = default_config()

    def test_section_defaults_match_schema(self):
        for section in ("general", "recognition", "capture", "input",
                        "execution", "hotkeys", "logging"):
            properties = self.schema["definitions"][section]["properties"]
            for key, spec in properties.items():
                if "default" in spec:
                    self.assertEqual(
                        self.config[section][key], spec["default"],
                        "默认值漂移：%s.%s" % (section, key),
                    )

    def test_tolerance_defaults_match_schema(self):
        for definition, section_key in (
            ("toleranceRgb", "rgb_tolerance"), ("toleranceHsv", "hsv_tolerance"),
        ):
            properties = self.schema["definitions"][definition]["properties"]
            for key, spec in properties.items():
                self.assertEqual(self.config["recognition"][section_key][key],
                                 spec["default"],
                                 "默认值漂移：%s.%s" % (section_key, key))

    def test_top_level_defaults(self):
        self.assertEqual(self.config["schema_version"],
                         self.schema["properties"]["schema_version"]["const"])
        self.assertEqual(self.config["revision"],
                         self.schema["properties"]["revision"]["default"])

    def test_config_limits_match_schema(self):
        self.assertEqual(CONFIG_LIMITS, self.schema["configLimits"])
        self.assertEqual(CONFIG_LIMITS, load_schema_limits(schema_defaults_path()))


# ---------------------------------------------------------------------------
# dispatcher 三命令接线（03 命令目录：data={config:...}）
# ---------------------------------------------------------------------------

class DispatcherWiringTest(unittest.TestCase):
    def setUp(self):
        self.logger = _QuietLogger()
        self.service, self.data_dir = _make_service(self.logger)
        self.transport = _CaptureTransport()
        self.dispatcher = Dispatcher(
            transport=self.transport, session=_SessionStub(), logger=self.logger
        )
        register_config_commands(self.dispatcher, self.service)
        self.service.load()  # 启动期首次初始化（与 main.py 装配时序一致）

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _request(self, command, params, request_id):
        envelope = {
            "protocol_version": 1,
            "session_id": _SessionStub.session_id,
            "type": "request",
            "id": request_id,
            "command": command,
            "params": params,
        }
        self.dispatcher.dispatch_command(envelope)
        return self.transport.sent[-1]

    def test_get_config_returns_wrapped_document(self):
        response = self._request("get_config", {}, "r1")
        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["config"]["revision"], 1)

    def test_update_config_legal_and_illegal(self):
        ok = self._request(
            "update_config",
            {"expected_revision": 1, "patch": {"general": {"theme": "dark"}}},
            "r2",
        )
        self.assertTrue(ok["success"])
        self.assertEqual(ok["data"]["config"]["revision"], 2)
        bad = self._request(
            "update_config",
            {"expected_revision": 2, "patch": {"recognition": {"foo": 1}}},
            "r3",
        )
        self.assertFalse(bad["success"])
        self.assertEqual(bad["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(bad["error"]["details"]["field"], "recognition.foo")
        conflict = self._request(
            "update_config", {"expected_revision": 99, "patch": {}}, "r4"
        )
        self.assertFalse(conflict["success"])
        self.assertEqual(conflict["error"]["code"], "CONFLICT")

    def test_reset_config_wrapped(self):
        response = self._request("reset_config", {"expected_revision": 1}, "r5")
        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["config"]["revision"], 2)

    def test_unknown_command_still_unknown(self):
        response = self._request("no_such_command", {}, "r6")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "UNKNOWN_COMMAND")

    # -- 全命令统一"未知参数即拒"（架构师裁决 TASK-006 设计说明 §15.5） --------

    def test_get_config_rejects_extra_params_and_config_unchanged(self):
        before = _read_bytes(self.service.config_path)
        response = self._request("get_config", {"foo": 1}, "r7")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "foo")
        # 拒绝不产生任何写副作用
        self.assertEqual(_read_bytes(self.service.config_path), before)
        self.assertEqual(self.service.get_config()["revision"], 1)

    def test_get_config_empty_and_missing_params_ok(self):
        ok = self._request("get_config", {}, "r8")
        self.assertTrue(ok["success"])
        self.assertEqual(ok["data"]["config"]["revision"], 1)
        # 信封无 params 键（03：`?` 可省略）同样放行
        envelope = {
            "protocol_version": 1,
            "session_id": _SessionStub.session_id,
            "type": "request",
            "id": "r9",
            "command": "get_config",
        }
        self.dispatcher.dispatch_command(envelope)
        self.assertTrue(self.transport.sent[-1]["success"])

    def test_config_commands_reject_unknown_params_uniformly(self):
        """三命令未知参数错误口径一致：INVALID_PARAMS + details.field 定位。"""
        cases = (
            ("update_config", {"expected_revision": 1, "patch": {}, "junk": 2}),
            ("reset_config", {"expected_revision": 1, "junk": 2}),
        )
        for command, params in cases:
            with self.subTest(command=command):
                response = self._request(command, params, "r10-%s" % command)
                self.assertFalse(response["success"])
                self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
                self.assertEqual(response["error"]["details"]["field"], "junk")
        # 白名单内参数仍正常
        ok = self._request(
            "update_config",
            {"expected_revision": 1, "patch": {"general": {"theme": "dark"}}},
            "r11",
        )
        self.assertTrue(ok["success"])


if __name__ == "__main__":
    unittest.main()
