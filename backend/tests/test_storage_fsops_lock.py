# -*- coding: utf-8 -*-
"""TASK-010 存储测试：原子文件原语与数据根级写锁（02/07 §6，Q3/Q7）。

覆盖：单文件原子写（无残留 tmp）；replace 有界重试（瞬时失败后成功 / 耗尽报
STORAGE_ERROR 且旧档完好）；fsync 失败可注入（F4）；数据根级进程内单写锁互斥；
lockfile 跨进程第二写者拒绝（STORAGE_ERROR 非 BUSY）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.storage import fsops  # noqa: E402
from backend.storage.errors import StorageError  # noqa: E402
from backend.storage.lock import DataRootLock, lockfile_path, root_lock  # noqa: E402


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="st-fs-")

    def test_write_atomic_no_leftover_tmp(self):
        path = os.path.join(self.dir, "a.bin")
        fsops.write_atomic_bytes(path, b"hello", max_attempts=2, sleep=lambda _s: None)
        self.assertEqual(fsops.read_bytes(path), b"hello")
        self.assertFalse(os.path.exists(path + fsops.WRITE_TMP_SUFFIX))

    def test_replace_retries_then_succeeds(self):
        src = os.path.join(self.dir, "src.bin")
        dst = os.path.join(self.dir, "dst.bin")
        with open(src, "wb") as handle:
            handle.write(b"new")
        with open(dst, "wb") as handle:
            handle.write(b"old")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("locked")
            return real_replace(a, b)

        with mock.patch.object(fsops.os, "replace", side_effect=flaky):
            fsops.replace_with_retry(src, dst, max_attempts=5, sleep=lambda _s: None)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(fsops.read_bytes(dst), b"new")

    def test_replace_exhaustion_preserves_original(self):
        src = os.path.join(self.dir, "src2.bin")
        dst = os.path.join(self.dir, "dst2.bin")
        with open(src, "wb") as handle:
            handle.write(b"new")
        with open(dst, "wb") as handle:
            handle.write(b"old")
        with mock.patch.object(fsops.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(StorageError) as ctx:
                fsops.replace_with_retry(src, dst, max_attempts=3, sleep=lambda _s: None)
        self.assertEqual(getattr(ctx.exception, "code", None), "STORAGE_ERROR")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(fsops.read_bytes(dst), b"old")  # 旧档完好

    def test_fsync_failure_is_injectable(self):
        path = os.path.join(self.dir, "f.bin")
        with mock.patch.object(fsops.os, "fsync", side_effect=OSError("boom")):
            with self.assertRaises(StorageError) as ctx:
                fsops.write_atomic_bytes(path, b"x", max_attempts=1, sleep=lambda _s: None)
        self.assertEqual(getattr(ctx.exception, "code", None), "STORAGE_ERROR")
        self.assertFalse(os.path.exists(path))  # 半写未成为正式文件
        self.assertFalse(os.path.exists(path + fsops.WRITE_TMP_SUFFIX))


class DataRootLockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="st-lock-")

    def test_root_lock_shared_per_canonical_path(self):
        self.assertIs(root_lock(self.dir), root_lock(os.path.join(self.dir, ".")))

    def test_release_removes_lockfile(self):
        lock = DataRootLock(self.dir)
        lock.acquire()
        self.assertTrue(os.path.exists(lockfile_path(self.dir)))
        lock.release()
        self.assertFalse(os.path.exists(lockfile_path(self.dir)))

    def test_second_writer_lockfile_rejected(self):
        os.makedirs(os.path.dirname(lockfile_path(self.dir)), exist_ok=True)
        with open(lockfile_path(self.dir), "w") as handle:
            handle.write("99999")  # 模拟另一进程持有
        lock = DataRootLock(self.dir)
        with self.assertRaises(StorageError) as ctx:
            lock.acquire()
        self.assertEqual(getattr(ctx.exception, "code", None), "STORAGE_ERROR")
        self.assertFalse(ctx.exception.retryable)
        self.assertFalse(lock.held)

    def test_in_process_mutual_exclusion_timeout(self):
        holder = DataRootLock(self.dir)
        holder.acquire()
        result = {}

        def contender():
            try:
                DataRootLock(self.dir).acquire(timeout=0.1)
                result["ok"] = True
            except StorageError as exc:
                result["code"] = exc.code

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=5)
        holder.release()
        self.assertNotIn("ok", result)
        self.assertEqual(result.get("code"), "STORAGE_ERROR")


if __name__ == "__main__":
    unittest.main()
