# -*- coding: utf-8 -*-
"""原子文件原语（TASK-010，02「磁盘布局与一致提交」/07 §6）。

提供**同卷单文件原子写**与 **Windows 占用有限重试**，均为**模块级可替换函数**
（故障注入范式镜像 ``backend/config/service.py`` 的 ``_copy_file``）：

- ``write_temp_bytes``：写 ``<path>.tmp`` + flush + ``fsync``（不替换）；
- ``replace_with_retry``：``os.replace`` + 有界重试 + 短退避（**不覆盖原数据**）；
- ``write_atomic_bytes``：temp 写 + 原子替换（单文件原子可见性）；
- ``fsync_file`` / ``fsync_dir``：文件/目录 fsync（失败可注入）；
- ``copy_file``：备份拷贝封装（模块级，供测试替换）。

02 逐字：原子替换只保证**同卷单文件**可见性，不能保证多文件一起事务化——故
上层用不可变 generation + ``current`` 指针（见 ``catalog``/``repository``）。
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Callable, Optional

from .errors import storage_error

# 07 §6：Windows 文件占用重试**有限且不覆盖原数据**（Q7：3–5 次、20–50ms）。
REPLACE_MAX_ATTEMPTS: int = 5
REPLACE_BACKOFF_BASE_MS: int = 20
REPLACE_BACKOFF_CAP_MS: int = 50
WRITE_TMP_SUFFIX: str = ".tmp"

Logger = Callable[[str], None]


def ensure_parent(path: str) -> None:
    """确保目标文件的父目录存在（幂等）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def fsync_file(path: str) -> None:
    """对文件句柄 fsync（模块级可注入：测试 patch ``fsops.os.fsync``）。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: str) -> None:
    """对目录 fsync；平台不支持目录 fsync（如 Windows）时静默跳过。

    07 §6 只要求「fsync 失败必须可注入」——文件 fsync 为可注入主路径；目录
    fsync 在 Windows 不支持，跳过不视为失败。
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


def write_temp_bytes(path: str, data: bytes) -> str:
    """把 ``data`` 写入 ``<path>.tmp``（同卷）+ flush + fsync，返回临时路径。

    Raises:
        StorageError: STORAGE_ERROR(retryable) 写入/flush/fsync 失败（F1/F2/F4）。
    """
    tmp = path + WRITE_TMP_SUFFIX
    try:
        ensure_parent(path)
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        remove_file(tmp)  # 半写临时件清理，不污染原数据
        raise storage_error("原子写临时文件失败: %s" % exc, retryable=True) from exc
    return tmp


def replace_with_retry(
    src: str,
    dst: str,
    *,
    max_attempts: int = REPLACE_MAX_ATTEMPTS,
    backoff_base_ms: int = REPLACE_BACKOFF_BASE_MS,
    backoff_cap_ms: int = REPLACE_BACKOFF_CAP_MS,
    sleep: Optional[Callable[[float], None]] = None,
    logger: Optional[Logger] = None,
) -> None:
    """``os.replace(src, dst)`` + 有界重试 + 短退避（不覆盖原数据）。

    Raises:
        StorageError: STORAGE_ERROR(retryable) 重试耗尽（占用/权限）。
    """
    if max_attempts < 1:
        raise ValueError("max_attempts 必须 ≥1")
    do_sleep = sleep if sleep is not None else time.sleep
    ensure_parent(dst)
    delay = backoff_base_ms
    last: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            os.replace(src, dst)
            fsync_dir(os.path.dirname(os.path.abspath(dst)))
            return
        except OSError as exc:
            last = exc
            if logger is not None:
                logger("原子替换第 %d/%d 次失败：%s" % (attempt, max_attempts, exc))
            if attempt < max_attempts and delay > 0:
                do_sleep(delay / 1000.0)
                delay = min(delay * 2, backoff_cap_ms)
    raise storage_error(
        "原子替换失败（占用/权限），已重试 %d 次：%s" % (max_attempts, last),
        retryable=True,
    )


def write_atomic_bytes(
    path: str,
    data: bytes,
    *,
    max_attempts: int = REPLACE_MAX_ATTEMPTS,
    sleep: Optional[Callable[[float], None]] = None,
    logger: Optional[Logger] = None,
) -> None:
    """单文件原子写：``<path>.tmp`` + flush + fsync + 原子替换。"""
    tmp = write_temp_bytes(path, data)
    replace_with_retry(tmp, path, max_attempts=max_attempts, sleep=sleep, logger=logger)


def read_bytes(path: str) -> bytes:
    """读取文件全部字节。"""
    with open(path, "rb") as handle:
        return handle.read()


def exists(path: str) -> bool:
    """文件是否存在（且为普通文件）。"""
    return os.path.isfile(path)


def copy_file(src: str, dst: str) -> None:
    """备份拷贝封装（模块级，供测试故障注入替换）。"""
    ensure_parent(dst)
    shutil.copyfile(src, dst)


def remove_file(path: str) -> None:
    """best-effort 删除（不存在/失败不抛）。"""
    try:
        os.remove(path)
    except OSError:
        pass
