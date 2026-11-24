# -*- coding: utf-8 -*-
"""TASK-010 存储测试：数据根路径安全与组件包含判定（07 §6）。

覆盖：词法拒绝（绝对/盘符/..）+ realpath 组件包含 + 拒绝符号链接/reparse 逃逸；
``is_contained`` 组件关系（大小写规范化，非 startswith）；四受管目录（Q12）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.storage import DataRoot, PathEscapeError  # noqa: E402
from backend.storage.errors import StorageError  # noqa: E402
from backend.storage.paths import MANAGED_DIRS, is_contained  # noqa: E402


def _code(exc_ctx):
    return getattr(exc_ctx.exception, "code", None)


class DataRootPathTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="st-paths-")
        self.root = DataRoot(self._dir)

    def test_resolve_valid_relative_stays_inside(self):
        resolved = self.root.resolve("images/aa.png")
        self.assertTrue(self.root.contains(resolved))
        self.assertTrue(resolved.startswith(self.root.root))

    def test_reject_absolute_and_drive(self):
        for bad in ("C:\\Windows\\x", "C:/Windows/x", "/etc/passwd", "\\\\srv\\share\\x"):
            with self.subTest(bad=bad):
                with self.assertRaises(StorageError) as ctx:
                    self.root.resolve(bad)
                self.assertEqual(_code(ctx), "INVALID_PARAMS")

    def test_reject_parent_component(self):
        for bad in ("../escape", "images/../../escape", "a/b/../../../x"):
            with self.subTest(bad=bad):
                with self.assertRaises(StorageError) as ctx:
                    self.root.resolve(bad)
                self.assertEqual(_code(ctx), "INVALID_PARAMS")

    def test_relpath_round_trip(self):
        resolved = self.root.resolve("scripts/s1/abc.lua")
        self.assertEqual(self.root.relpath(resolved), "scripts/s1/abc.lua")

    def test_relpath_outside_rejected(self):
        outside = tempfile.mkdtemp(prefix="st-paths-out-")
        with self.assertRaises(PathEscapeError):
            self.root.relpath(os.path.join(outside, "x"))

    def test_symlink_escape_rejected(self):
        if not hasattr(os, "symlink"):
            self.skipTest("platform without symlink")
        outside = tempfile.mkdtemp(prefix="st-paths-target-")
        link = os.path.join(self.root.root, "images", "link")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted")
        if not os.path.islink(link):
            self.skipTest("os.symlink silently copied (no real link)")
        with self.assertRaises(PathEscapeError):
            self.root.resolve("images/link/secret.txt")

    def test_leaf_symlink_rejected(self):
        outside = tempfile.mkdtemp(prefix="st-paths-target2-")
        secret = os.path.join(outside, "secret.png")
        with open(secret, "wb") as handle:
            handle.write(b"x")
        link = os.path.join(self.root.root, "images", "leaf.png")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted")
        if not os.path.islink(link):
            self.skipTest("os.symlink silently copied (no real link)")
        with self.assertRaises(PathEscapeError):
            self.root.resolve("images/leaf.png")

    def test_ensure_dirs_creates_managed_dirs(self):
        self.root.ensure_dirs()
        for name in MANAGED_DIRS:
            self.assertTrue(os.path.isdir(self.root.managed_dir(name)))

    def test_unknown_managed_dir_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            self.root.managed_dir("history")
        self.assertEqual(_code(ctx), "INVALID_PARAMS")

    def test_is_contained_component_relation(self):
        root = self.root.root
        self.assertTrue(is_contained(os.path.join(root, "images", "a.png"), root))
        self.assertTrue(is_contained(root, root))
        # 兄弟前缀不得误判（component-wise，不是 startswith）
        self.assertFalse(is_contained(root + "-evil" + os.sep + "x", root))
        self.assertFalse(is_contained(os.path.dirname(root), root))


if __name__ == "__main__":
    unittest.main()
