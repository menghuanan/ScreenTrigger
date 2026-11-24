# -*- coding: utf-8 -*-
"""识别结果缓存（TASK-013 §9；纯键构造 + 进程内 LRU）。

键组成（对齐 01 M02 + 07 §2）：``command`` + ``provider_id`` + 模型版本（``model_id``+
``model_version``）+ 算法版本（``algo_version``）+ ``frame_sha256``（source hash）+
``geometry_revision``（坐标版本）+ ``space`` + ``region`` + ``params``（规范化）+
``condition_revision``（target revision）。

**硬性约束**：
1. **只减少计算，不跳过新帧/新几何**：``frame_sha256`` 与 ``geometry_revision`` 都在键内 →
   新帧/新几何**天然 miss**（旧结果永不用于新帧/新几何）；
2. **负结果也正确失效**：``found=false``/空候选**同样入缓存**（同键）；新帧/新几何/revision
   变化 → 新键 → 旧负结果**不命中**；
3. **容量**：LRU 按**条目上限**（默认 256 起，可注入）；**待 TASK-015 实测冻结**（Q7）。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional, Tuple

#: 识别结果缓存 LRU 条目上限（Q7：256 起，待 TASK-015 实测冻结）。
DEFAULT_CACHE_CAPACITY: int = 256


def _encode_default(value: Any) -> str:
    """非 JSON 可序列化对象（Rect/LocalRect/Region 等 dataclass）→ 稳定字符串。"""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return repr(value)


def _normalize_region(region: Any) -> Any:
    if region is None:
        return None
    if is_dataclass(region) and not isinstance(region, type):
        return asdict(region)
    if isinstance(region, dict):
        return region
    return repr(region)


def cache_key(
    *,
    command: str,
    provider_id: str,
    model_id: Optional[str],
    model_version: str,
    algo_version: str,
    frame_sha256: str,
    geometry_revision: int,
    space: str,
    region: Any,
    params: Dict[str, Any],
    condition_revision: Optional[int],
) -> str:
    """规范化 JSON → sha256（确定性、跨进程稳定）。"""
    payload = {
        "command": command,
        "provider_id": provider_id,
        "model_id": model_id,
        "model_version": model_version,
        "algo_version": algo_version,
        "frame_sha256": frame_sha256,
        "geometry_revision": int(geometry_revision),
        "space": space,
        "region": _normalize_region(region),
        "params": params,
        "condition_revision": None if condition_revision is None else int(condition_revision),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_encode_default
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RecognitionCache:
    """进程内 LRU 结果缓存（``get`` 触碰、``put`` 覆盖并淘汰最旧）。"""

    def __init__(self, capacity: int = DEFAULT_CACHE_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity 必须 ≥1")
        self._capacity = int(capacity)
        self._entries: "OrderedDict[str, Any]" = OrderedDict()

    @property
    def capacity(self) -> int:
        return self._capacity

    def get(self, key: str) -> Optional[Any]:
        """命中返回**缓存值本身**（不可变结果），并标记为最近使用；未命中返回 None。"""
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return self._entries[key]

    def put(self, key: str, value: Any) -> None:
        """写入并淘汰最旧条目（含负结果，Q7）。"""
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def keys(self) -> Tuple[str, ...]:
        return tuple(self._entries.keys())
