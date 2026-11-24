# -*- coding: utf-8 -*-
"""参数校验矩阵单测（设计 §4.2/§6.3）。

覆盖：--session-id 必填/非空/≤128 字符/仅 [A-Za-z0-9-]（违规 stderr + exit 2）、
--fault-scenario 六枚举合法、未知枚举 exit 2、--data-dir 必填与可创建/可写
校验（TASK-006 §2.5）。argparse 错误亦以退出码 2 终止。
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.main import FAULT_SCENARIOS, SidecarArgs  # noqa: E402

# 测试会话级数据目录替身（--data-dir 必填后各用例的合法值注入点）
_DATA_DIR = tempfile.mkdtemp(prefix="st-args-data-")


def _with_data_dir(argv):
    """argv 未显式给 --data-dir 时注入合法值（保持既有矩阵语义不变）。"""
    if "--data-dir" in argv:
        return argv
    return argv + ["--data-dir", _DATA_DIR]


def _parse(argv):
    """在捕获 stderr 的前提下解析 argv；成功返回 (args, stderr_text)。"""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        result = SidecarArgs.parse(_with_data_dir(argv))
    return result, err.getvalue()


class SessionIdMatrixTest(unittest.TestCase):
    """--session-id 校验矩阵（缺失/空/超长/非法字符/边界合法值）。"""

    def _assert_exit_2(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                SidecarArgs.parse(_with_data_dir(argv))
        self.assertEqual(ctx.exception.code, 2)
        return err.getvalue()

    def test_missing_session_id_exits_2(self):
        stderr_text = self._assert_exit_2([])
        self.assertIn("--session-id", stderr_text)  # argparse 用法/错误信息在 stderr

    def test_empty_session_id_exits_2(self):
        stderr_text = self._assert_exit_2(["--session-id", ""])
        self.assertIn("不能为空", stderr_text)

    def test_overlong_session_id_exits_2(self):
        stderr_text = self._assert_exit_2(["--session-id", "a" * 129])
        self.assertIn("128", stderr_text)

    def test_session_id_with_illegal_chars_exits_2(self):
        for bad in ("session_1", "session/1", "session id", "会话一", "session\x00id", "session.1"):
            stderr_text = self._assert_exit_2(["--session-id", bad])
            self.assertIn("[A-Za-z0-9-]", stderr_text, msg="非法值 %r 的报错不符" % bad)

    def test_valid_session_id_boundaries(self):
        args, _ = _parse(["--session-id", "a" * 128])  # 恰好 128 字符合法
        self.assertEqual(args.session_id, "a" * 128)
        args, _ = _parse(["--session-id", "session-ABC-0123"])  # 字母/数字/连字符
        self.assertEqual(args.session_id, "session-ABC-0123")

    def test_validate_session_id_messages(self):
        self.assertIsNone(SidecarArgs.validate_session_id("session-1"))
        self.assertIsNotNone(SidecarArgs.validate_session_id(""))
        self.assertIsNotNone(SidecarArgs.validate_session_id("a" * 129))
        self.assertIsNotNone(SidecarArgs.validate_session_id("bad_char"))


class FaultScenarioEnumTest(unittest.TestCase):
    """--fault-scenario 枚举合法性（六枚举全收、未知 exit 2、缺省 None）。"""

    def test_enum_list_matches_design(self):
        self.assertEqual(
            set(FAULT_SCENARIOS),
            {
                "crash_on_start",
                "silent_no_ready",
                "stderr_flood",
                "stdout_close_after_ready",
                "exit_after_ready",
                "stdout_garbage",
            },
        )

    def test_all_enums_accepted(self):
        for name in FAULT_SCENARIOS:
            args, _ = _parse(["--session-id", "session-1", "--fault-scenario", name])
            self.assertEqual(args.fault_scenario, name, msg="枚举 %r 应被接受" % name)

    def test_unknown_scenario_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                SidecarArgs.parse(["--session-id", "session-1", "--fault-scenario", "foo"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--fault-scenario", err.getvalue())

    def test_fault_optional_default_none(self):
        args, _ = _parse(["--session-id", "session-1"])
        self.assertIsNone(args.fault_scenario)


class DataDirArgTest(unittest.TestCase):
    """--data-dir 必填与可创建/可写校验（TASK-006 设计 §2.5/§8）。"""

    def _assert_exit_2_raw(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                SidecarArgs.parse(argv)
        self.assertEqual(ctx.exception.code, 2)
        return err.getvalue()

    def test_missing_data_dir_exits_2(self):
        stderr_text = self._assert_exit_2_raw(["--session-id", "session-1"])
        self.assertIn("--data-dir", stderr_text)

    def test_empty_data_dir_exits_2(self):
        stderr_text = self._assert_exit_2_raw(
            ["--session-id", "session-1", "--data-dir", ""]
        )
        self.assertIn("不能为空", stderr_text)

    def test_valid_data_dir_accepted(self):
        target = os.path.join(_DATA_DIR, "nested", "leaf")
        args, _ = _parse(["--session-id", "session-1", "--data-dir", target])
        self.assertEqual(args.data_dir, target)
        self.assertTrue(os.path.isdir(target))  # 校验过程已创建目录

    def test_data_dir_blocked_by_file_exits_2(self):
        occupied = tempfile.mkdtemp(prefix="st-args-file-")
        file_path = os.path.join(occupied, "occupied")
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write("x")
        stderr_text = self._assert_exit_2_raw(
            ["--session-id", "session-1", "--data-dir", file_path]
        )
        self.assertIn("不可创建", stderr_text)

    def test_validate_data_dir_unit(self):
        self.assertIsNotNone(SidecarArgs.validate_data_dir(None))
        self.assertIsNotNone(SidecarArgs.validate_data_dir(""))
        self.assertIsNone(SidecarArgs.validate_data_dir(_DATA_DIR))


if __name__ == "__main__":
    unittest.main()
