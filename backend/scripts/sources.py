# -*- coding: utf-8 -*-
"""脚本源码存储（TASK-012）：内容寻址、不可变、幂等、回读校验。

- 落盘路径 ``scripts/<id>/<source_sha256>.lua``（02 磁盘布局；与 ``images/<sha>.<ext>``
  同构的**不可变资产**，**不进** generation）；
- ``persist`` 幂等：同 hash 文件已存在则跳过（内容不符 → ``DATA_CORRUPT``）；
- ``read``/``verify`` 回读并校验 hash（缺失/hash 不符 → ``DATA_CORRUPT``）；
- **只保留不回收**：本模块不提供删除；``delete_script`` 只删元数据（§8.3）。

路径安全：所有读写经 ``DataRoot.resolve``（词法拒绝绝对/``..`` + realpath 组件包含判定
+ 叶子链接拒绝）。
"""

from __future__ import annotations

import os
from typing import Callable, Optional, Tuple

from ..storage import fsops
from ..storage.errors import data_corrupt
from ..storage.generation import sha256_hex
from ..storage.paths import DataRoot

from . import domain

Logger = Callable[[str], None]


class ScriptSourceStore:
    """脚本源码内容寻址存储（端口可替身；生产实现落盘到数据根 ``scripts/``）。"""

    def __init__(self, data_root: DataRoot, *, logger: Optional[Logger] = None) -> None:
        if data_root is None:
            raise ValueError("data_root 不能为空")
        self._root = data_root
        self._logger = logger

    @property
    def data_root(self) -> DataRoot:
        return self._root

    def source_relative_path(self, script_id: str, source_sha256: str) -> str:
        """数据根相对路径 ``scripts/<id>/<sha>.lua``（形态校验见 domain）。"""
        return domain.source_relative_path(script_id, source_sha256)

    def persist(self, script_id: str, source_bytes: bytes) -> Tuple[str, str]:
        """落盘源码（内容寻址；幂等）。返回 ``(relative_path, source_sha256)``。

        Raises:
            StorageError: DATA_CORRUPT（同名文件内容与 hash 不符）/ INVALID_PARAMS（路径非法）。
        """
        raw = bytes(source_bytes)
        sha = sha256_hex(raw)
        relative = self.source_relative_path(script_id, sha)
        absolute = self._root.resolve(relative)
        if os.path.isfile(absolute):
            existing = fsops.read_bytes(absolute)
            if sha256_hex(existing) != sha:
                raise data_corrupt(
                    "源码内容与文件名 hash 不符: %s" % relative, field="source"
                )
            return relative, sha
        parent = os.path.dirname(absolute)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fsops.write_atomic_bytes(absolute, raw)
        if self._logger is not None:
            self._logger("源码已落盘: %s" % relative)
        return relative, sha

    def read(self, relative_path: str) -> bytes:
        """回读源码字节；文件缺失 → ``DATA_CORRUPT``。"""
        absolute = self._root.resolve(relative_path)
        if not os.path.isfile(absolute):
            raise data_corrupt("源码文件缺失: %s" % relative_path, field="source_path")
        return fsops.read_bytes(absolute)

    def verify(self, relative_path: str, expected_sha256: str) -> bytes:
        """回读并校验 hash 与声明一致（不符 → ``DATA_CORRUPT``）；返回源码字节。"""
        raw = self.read(relative_path)
        actual = sha256_hex(raw)
        if actual != expected_sha256:
            raise data_corrupt(
                "源码 hash 与元数据不符: %s" % relative_path, field="source_sha256"
            )
        return raw
