# -*- coding: utf-8 -*-
"""数据根路径解析与**规范路径组件包含**判定（TASK-010，07 §6 硬约束）。

07 §6 逐字要求：

> 不要使用 string.startswith 验证路径包含关系，应解析**规范路径（realpath）**并做
> **路径组件关系**判断，额外**拒绝链接（reparse/symlink）逃逸**。

既有的 ``backend.types.capabilities.relative_path_field`` 只做**词法**层拒绝
（绝对路径 / 盘符 / ``..``），运行时**不够**：必须再对**真实文件系统**做
realpath + 组件包含判定并拒 reparse 逃逸。本模块补足该运行时层。

- ``DataRoot``：数据根的解析与安全 path 解析（写入前调用）。
- ``is_contained``：规范路径组件关系判定（大小写规范化后逐段比较）。
- ``PathEscapeError``：越界/链接逃逸（06 INVALID_PARAMS，非静默）。
"""

from __future__ import annotations

import os
from typing import Iterable, List

from .errors import StorageError, invalid_params

# Q12：TASK-010 只管理下列四个目录（config 归 ConfigService、history 后续、
# models 归 TASK-015）；**不创建/不拥有其它**。
MANAGED_DIRS = ("catalog", "images", "scripts", "backups")
# generation 目录：catalog/generations/<generation>/
GENERATIONS_SUBDIR = ("catalog", "generations")


class PathEscapeError(StorageError):
    """路径越界/链接逃逸（06 INVALID_PARAMS；field=path）。"""


def _norm_parts(path: str) -> List[str]:
    """规范路径的**组件**列表（盘符小写 + 逐段小写；供包含判定用）。"""
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    parts = [part.lower() for part in rest.replace("\\", "/").split("/") if part]
    return [drive.lower()] + parts


def is_contained(child: str, parent: str) -> bool:
    """规范路径**组件关系**判定（07 §6：禁止 str.startswith）。

    ``child`` 位于 ``parent`` 之下（或相等）返回 True；先做大小写规范化
    （Windows 大小写不敏感），再按路径组件逐段比较。
    """
    child_parts = _norm_parts(child)
    parent_parts = _norm_parts(parent)
    if len(child_parts) < len(parent_parts):
        return False
    return child_parts[: len(parent_parts)] == parent_parts


def is_remote_volume(path) -> bool:
    """Check the drive type before opening a file on a mapped Windows drive."""
    if os.name != "nt":
        return False
    import ctypes
    drive, _ = os.path.splitdrive(path)
    return bool(drive and ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4)


def _reject_lexical(relative: str) -> List[str]:
    """词法层：拒绝绝对路径/盘符/``..``/空组件，返回规范化后的组件列表。"""
    if not isinstance(relative, str) or not relative:
        raise invalid_params("path", "相对路径必须为非空字符串")
    if os.path.splitdrive(relative)[0]:
        raise invalid_params("path", "数据根相对路径禁止盘符/绝对路径")
    if relative.startswith("/") or relative.startswith("\\"):
        raise invalid_params("path", "数据根相对路径禁止以分隔符开头")
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise invalid_params("path", "数据根相对路径禁止 .. 组件")
    if not parts:
        raise invalid_params("path", "相对路径不得为空")
    return parts


class DataRoot:
    """数据根：解析既有 ``--data-dir``（Q13：只消费，不新增解析）并提供安全路径。"""

    def __init__(self, data_dir: str) -> None:
        if not isinstance(data_dir, str) or not data_dir:
            raise invalid_params("data_dir", "数据根目录必须为非空字符串")
        self._root = os.path.realpath(os.path.abspath(data_dir))

    @property
    def root(self) -> str:
        """数据根的规范绝对路径。"""
        return self._root

    def managed_dir(self, name: str) -> str:
        """四个受管目录之一的绝对路径（Q12）。"""
        if name not in MANAGED_DIRS:
            raise invalid_params("dir", "非受管目录: %s" % name)
        return os.path.join(self._root, name)

    def ensure_dirs(self) -> None:
        """创建数据根与四个受管目录（幂等）。"""
        os.makedirs(self._root, exist_ok=True)
        for name in MANAGED_DIRS:
            os.makedirs(self.managed_dir(name), exist_ok=True)

    def generations_dir(self) -> str:
        """``catalog/generations`` 绝对路径。"""
        return os.path.join(self._root, *GENERATIONS_SUBDIR)

    def resolve(self, relative: str) -> str:
        """把数据根相对路径解析为**安全**的绝对路径。

        做词法拒绝（绝对/盘符/``..``）+ realpath 组件包含判定 + 叶子链接拒绝。
        返回规范绝对路径（调用方据此读写文件）。

        Raises:
            PathEscapeError: 越界/链接逃逸（code=INVALID_PARAMS，field=path）。
        """
        parts = _reject_lexical(relative)
        candidate = os.path.join(self._root, *parts)
        real = os.path.realpath(candidate)
        if not is_contained(real, self._root):
            raise PathEscapeError(
                "INVALID_PARAMS",
                "路径解析越界（拒绝链接/reparse 逃逸）: %s" % relative,
                field="path",
            )
        if os.path.islink(candidate):
            raise PathEscapeError(
                "INVALID_PARAMS",
                "路径不得为符号链接（拒绝链接逃逸）: %s" % relative,
                field="path",
            )
        return real

    def contains(self, absolute: str) -> bool:
        """给定绝对路径是否落在数据根内（规范路径组件判定）。"""
        return is_contained(os.path.realpath(absolute), self._root)

    def relpath(self, absolute: str) -> str:
        """绝对路径 → 数据根相对 posix 路径（必须落在数据根内）。"""
        real = os.path.realpath(absolute)
        if not is_contained(real, self._root):
            raise PathEscapeError(
                "INVALID_PARAMS",
                "路径不在数据根内: %s" % absolute,
                field="path",
            )
        rel = os.path.relpath(real, self._root)
        return rel.replace("\\", "/")


def join_relative(parts: Iterable[str]) -> str:
    """把组件拼成数据根相对 posix 路径（用于生成 ``images/<sha>.<ext>`` 等）。"""
    cleaned = [str(part) for part in parts if str(part) != ""]
    return "/".join(cleaned)
