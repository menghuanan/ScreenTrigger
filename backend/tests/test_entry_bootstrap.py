# -*- coding: utf-8 -*-
"""入口引导回归（AUDIT-20261012-001）：backend/types 不得遮蔽标准库 types。

缺陷：直接以 ``python backend/main.py`` 执行（Rust 开发态文件入口）时，解释器把
脚本目录 ``<repo>/backend`` 置于 ``sys.path[0]``，其 ``backend/types/`` 即以顶层
模块名 ``types`` 遮蔽标准库，导致 ``import argparse → re → enum → from types ...``
链式失败、进程 exit 1、stdout 零响应。

本文件以**剥离遮蔽型引导变量的干净环境**（不继承 PYTHONPATH 等）复现与验证：

- ``TypesShadowingRegressionTest``：
  a) 负向对照——复刻文件入口 sys.path 条件（脚本目录置首位）后导入 ``types``，
     断言其**确实**被 ``backend/types`` 遮蔽（证明用例有效、能捕获回归）；
  b) 正向验证——同条件下先执行 ``backend/main.py`` 顶层（触发入口引导），再导入
     ``types``，断言其 ``__file__`` 指向 CPython 标准库 ``types.py`` 且
     ``GenericAlias`` 存在，且脚本目录已从 sys.path 移除、仓库根位于首位。
- ``FileEntryHelpTest``：干净环境下 ``python backend/main.py --help`` exit 0
  （--help 必经 argparse→re→enum→types，故 exit 0 即证明标准库 types 可用）。
- ``ModuleEntryRegressionTest``：``python -m backend.main --help`` 仍 exit 0
  （模块入口不受引导影响，幂等）。
- ``BootstrapIdempotencyTest``：``_bootstrap_import_path()`` 重复调用不产生重复
  条目、不误删仓库根。

全部依赖仅标准库；可用 ``python -m unittest discover -s backend/tests`` 运行。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
_MAIN_PY = os.path.join(_BACKEND_DIR, "main.py")

# 会被上游（IDE/工具链）注入、可能预载标准库从而**掩盖**本缺陷的引导变量；
# 复现/验证必须在剥离它们的受控环境中进行（主理人复核要求）。
_STRIPPED_ENV_KEYS = ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME")

if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend import main as backend_main  # noqa: E402


def _clean_env() -> dict:
    """返回不继承遮蔽型引导变量的受控环境（显式构造，非继承）。"""
    env = dict(os.environ)
    for key in _STRIPPED_ENV_KEYS:
        env.pop(key, None)
    return env


def _run(args, timeout: float = 120.0, encoding: str | None = None) -> "subprocess.CompletedProcess[str]":
    """在干净环境、仓库根 cwd 下运行子进程，捕获文本 stdout/stderr。"""
    return subprocess.run(
        args,
        cwd=_REPO_ROOT,
        env=_clean_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=encoding,
        timeout=timeout,
    )


# 负向对照：复刻文件入口 sys.path 条件后导入 types，观测其是否被遮蔽。
_NEGATIVE_PROBE = r"""
import os, sys
backend = {backend!r}
sys.path.insert(0, backend)          # 复刻 `python backend/main.py` 的 sys.path[0]
try:
    import types                     # noqa: F401
except ImportError:
    print("SHADOWED_VIA_IMPORT_ERROR")
    sys.exit(0)
shadowed_dir = os.path.dirname(os.path.abspath(types.__file__ or ""))
if os.path.normcase(os.path.normpath(shadowed_dir)) == os.path.normcase(os.path.normpath(backend)):
    print("SHADOWED_VIA_FILE")
else:
    print("NOT_SHADOWED")
"""

# 正向验证：同条件下先执行 main.py 顶层（入口引导），再导入 types 并检查。
_POSITIVE_PROBE = r"""
import os, sys, importlib.util
backend = {backend!r}
main_py = {main!r}
sys.path.insert(0, backend)          # 复刻 `python backend/main.py` 的 sys.path[0]
spec = importlib.util.spec_from_file_location("st_probe_main", main_py)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)      # 执行 main.py 顶层（含 _bootstrap_import_path）
import types                         # noqa: E402
assert hasattr(types, "GenericAlias"), "标准库 types 仍被遮蔽（缺 GenericAlias）"
types_file = types.__file__ or ""
assert os.path.basename(types_file) == "types.py", "types 未指向标准库 types.py: %r" % types_file
types_dir = os.path.normcase(os.path.normpath(os.path.dirname(os.path.abspath(types_file))))
backend_norm = os.path.normcase(os.path.normpath(backend))
assert types_dir != backend_norm, "types 仍指向 backend/types 包（仍被遮蔽）"
path_norm = [os.path.normcase(os.path.normpath(p)) for p in sys.path if p]
assert backend_norm not in path_norm, "脚本目录 backend/ 未被从 sys.path 移除"
print("STD_LIB_TYPES_FILE=" + types_file)
print("HAS_GENERIC_ALIAS=" + str(hasattr(types, "GenericAlias")))
"""


class TypesShadowingRegressionTest(unittest.TestCase):
    """标准库 types 遮蔽回归：负向对照 + 正向验证（AUDIT-20261012-001）。"""

    def test_negative_control_shadow_is_real(self):
        """负向对照：脚本目录置 sys.path[0] 时 types 确被 backend/types 遮蔽。

        该用例保证正向验证并非空跑——若机制消失（例如 backend/types 被改名），
        此用例会失败以提示“回归用例已失效/前提变更”。
        """
        result = _run([sys.executable, "-c", _NEGATIVE_PROBE.format(backend=_BACKEND_DIR)])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertTrue(
            result.stdout.startswith("SHADOWED_VIA_"),
            msg="未能复现 types 遮蔽机制：%r" % result.stdout,
        )

    def test_standard_library_types_not_shadowed_after_bootstrap(self):
        """正向：执行 main.py 顶层（入口引导）后标准库 types 恢复可见。"""
        result = _run([sys.executable, "-c", _POSITIVE_PROBE.format(
            backend=_BACKEND_DIR, main=_MAIN_PY
        )])
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("HAS_GENERIC_ALIAS=True", result.stdout)
        self.assertIn("STD_LIB_TYPES_FILE=", result.stdout)
        # types.__file__ 不应落在仓库内的 backend/ 目录
        types_line = [
            line for line in result.stdout.splitlines() if line.startswith("STD_LIB_TYPES_FILE=")
        ][0]
        types_file = types_line.split("=", 1)[1]
        self.assertNotIn(
            os.path.normcase(os.path.normpath(_BACKEND_DIR)),
            os.path.normcase(os.path.normpath(types_file)),
        )


class FileEntryHelpTest(unittest.TestCase):
    """文件入口回归：干净环境下 `python backend/main.py --help` 必须 exit 0。"""

    def test_help_exits_zero_in_clean_env(self):
        result = _run([sys.executable, _MAIN_PY, "--help"], encoding="utf-8")
        self.assertEqual(
            result.returncode, 0,
            msg="--help 非 0 退出；stderr=%s" % result.stderr,
        )
        self.assertIn("usage", (result.stdout + result.stderr).lower())
        self.assertNotIn("Traceback (most recent call last)", result.stderr)


class ModuleEntryRegressionTest(unittest.TestCase):
    """模块入口回归：`python -m backend.main --help` 仍 exit 0（引导幂等）。"""

    def test_module_entry_help_exits_zero(self):
        result = _run([sys.executable, "-m", "backend.main", "--help"], encoding="utf-8")
        self.assertEqual(
            result.returncode, 0,
            msg="module 入口 --help 非 0 退出；stderr=%s" % result.stderr,
        )
        self.assertIn("usage", (result.stdout + result.stderr).lower())
        self.assertNotIn("Traceback (most recent call last)", result.stderr)


class BootstrapIdempotencyTest(unittest.TestCase):
    """入口引导幂等性：重复调用不产生重复条目、不误删仓库根。"""

    def test_repeated_bootstrap_no_duplicate_repo_root(self):
        backend_main._bootstrap_import_path()
        backend_main._bootstrap_import_path()
        normalized = [
            os.path.normcase(os.path.normpath(p)) for p in sys.path if p
        ]
        repo_root = os.path.normcase(os.path.normpath(backend_main._REPO_ROOT))
        self.assertEqual(normalized.count(repo_root), 1, "仓库根出现多次：" + repr(sys.path))

    def test_bootstrap_removes_script_dir(self):
        backend_main._bootstrap_import_path()
        normalized = [
            os.path.normcase(os.path.normpath(p)) for p in sys.path if p
        ]
        script_dir = os.path.normcase(os.path.normpath(backend_main._SCRIPT_DIR))
        self.assertNotIn(script_dir, normalized, "脚本目录仍在 sys.path：" + repr(sys.path))

    def test_frozen_entry_is_noop(self):
        """冻结入口（sys.frozen）语义：引导直接返回，不改动 sys.path、不抛异常。"""
        had_frozen = hasattr(sys, "frozen")
        original_frozen = getattr(sys, "frozen", None)
        snapshot = list(sys.path)
        try:
            sys.frozen = True  # 打桩：模拟 PyInstaller 冻结入口
            backend_main._bootstrap_import_path()
            self.assertEqual(sys.path, snapshot, "冻结入口不应改动 sys.path")
        finally:
            if had_frozen:
                sys.frozen = original_frozen
            else:
                del sys.frozen


if __name__ == "__main__":
    unittest.main()
