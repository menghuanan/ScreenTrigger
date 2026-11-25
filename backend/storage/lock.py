# -*- coding: utf-8 -*-
"""数据根级写锁（TASK-010，Q3；00「同目录拒绝第二写者」）。

07 §6 逐字：

> 不能每次保存各 new 一个局部 Lock 冒充全局互斥。

因此本模块提供**数据根级**（按规范路径共享）的**进程内单写锁** +
**lockfile 跨进程第二写者拒绝**：

- 进程内：同一数据根的**所有** ``DataRootLock`` 共享**同一** ``threading.RLock``
  （模块级注册表按 realpath 键）；不是每次 new 一个局部锁。
- 跨进程：``catalog/writer.lock`` 以 ``O_CREAT|O_EXCL`` 独占创建；已存在 →
  **拒绝第二写者**，抛 ``STORAGE_ERROR``（06 无 writer-conflict 专用码；
  ``BUSY`` 语义为「根执行/框选/队列占用」，不适用）。
- 数据根级单写锁覆盖整个提交（S0–S11）；**读者不取写锁**（读原子快照指针）。
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Dict, Optional

from .errors import storage_error

LOCKFILE_NAME = "writer.lock"
LOCKFILE_SUBDIR = "catalog"

_REGISTRY_GUARD = threading.Lock()
_ROOT_LOCKS: Dict[str, threading.RLock] = {}


def _canonical(data_dir: str) -> str:
    return os.path.realpath(os.path.abspath(data_dir))


def root_lock(data_dir: str) -> threading.RLock:
    """返回数据根级**共享**的进程内可重入锁（按规范路径键）。"""
    key = _canonical(data_dir)
    with _REGISTRY_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_LOCKS[key] = lock
        return lock


def lockfile_path(data_dir: str) -> str:
    """lockfile 绝对路径：``<data_dir>/catalog/writer.lock``（受管目录内）。"""
    return os.path.join(_canonical(data_dir), LOCKFILE_SUBDIR, LOCKFILE_NAME)


class DataRootLock:
    """数据根级写锁（进程内共享单锁 + lockfile 跨进程第二写者拒绝）。"""

    def __init__(
        self,
        data_dir: str,
        *,
        lockfile: Optional[str] = None,
    ) -> None:
        if not isinstance(data_dir, str) or not data_dir:
            raise ValueError("data_dir 必须为非空字符串")
        self._data_dir = data_dir
        self._canonical = _canonical(data_dir)
        self._inproc = root_lock(data_dir)
        self._lockfile = lockfile or lockfile_path(data_dir)
        self._held = False
        self._descriptor = None

    @property
    def lockfile(self) -> str:
        return self._lockfile

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self, *, timeout: Optional[float] = None) -> "DataRootLock":
        """获取写锁；跨进程第二写者被拒 → STORAGE_ERROR。

        Args:
            timeout: 进程内锁等待上限（秒）；None 表示无限等待。

        Raises:
            StorageError: STORAGE_ERROR（进程内锁超时 / 跨进程第二写者）。
        """
        if self._held:
            raise ValueError("写锁已持有（不可重入 acquire）")
        if timeout is None:
            acquired = self._inproc.acquire()
        else:
            acquired = self._inproc.acquire(timeout=timeout)
        if not acquired:
            raise storage_error("数据根写锁被占用（进程内等待超时）", retryable=False)
        try:
            self._create_lockfile()
        except BaseException:
            self._inproc.release()
            raise
        self._held = True
        return self

    def _create_lockfile(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._lockfile)), exist_ok=True)
        try:
            fd = os.open(self._lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_TEMPORARY", 0))
        except FileExistsError as exc:
            raise storage_error(
                "数据根已被另一写者占用（拒绝第二写者）: %s" % self._lockfile,
                retryable=False,
            ) from exc
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        except BaseException:
            os.close(fd)
            raise
        self._descriptor = fd

    def release(self) -> None:
        """释放写锁（删除 lockfile + 释放进程内锁）；幂等。"""
        if not self._held:
            return
        self._held = False
        os.close(self._descriptor)
        self._descriptor = None
        try:
            if not hasattr(os, "O_TEMPORARY"):
                os.remove(self._lockfile)
        except OSError:
            pass
        self._inproc.release()

    def __enter__(self) -> "DataRootLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@contextmanager
def engine_session(data_dir):
    """One live engine or offline management process. The OS releases this lock on exit."""
    from .paths import DataRoot
    path = DataRoot(str(data_dir)).resolve("catalog/engine-session.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise storage_error("数据目录正在使用，请在设置页操作或退出占用它的引擎。") from exc
        yield
