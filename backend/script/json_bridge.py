# -*- coding: utf-8 -*-
"""Lua ↔ JSON 值桥（TASK-020 §4.2 json_bridge.py；设计 §2.3 三态算法）。

依据 ``docs/02`` §通用值与单位（JsonValue 契约）：

- JsonValue = null/boolean/string/有限 number/JsonValue 数组/字符串键对象；
- 整数限制在 ±(2^53−1)（超过用字符串）；深度最多 32；编码后最多 1MiB；
- 一个脚本返回零或一个值，无返回映射 null，多个返回拒绝；
- 循环 table、混合数组/对象键、稀疏数组、函数、userdata 均拒绝；
- 空 Lua table 默认对象；``json_array({})`` 标为空数组；``json_null`` 保留 null（数组
  空位 / 对象 null 字段）。

硬性约束：

- 身份/循环/哨兵判定**一律用** ``rawequal``（禁 ``id()``/``==``；§2.4 ②）；
- runtime type **一律用** ``type_fn``（禁 ``isinstance(v, lua_type('table'))``；§2.4 ①）；
- **值复制、不共享 table**：只读遍历，不改 Lua 状态、不写盘、不发事件；
- **拒绝在产生副作用前**（桥只读，任一拒绝先于任何宿主动作返回）。

**归因裁决（设计 §6 Q2，主理人已采纳）**：JSON 嵌套深度 >32 抛 ``SERIALIZATION_ERROR``
（**不是** ``CALL_DEPTH_EXCEEDED``；后者 06 冻结为「子调用第 6 层」，属 TASK-022）。
编码后 >1MiB 亦抛 ``SERIALIZATION_ERROR``（``details.field="value"``，附实际字节数，Q3）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .errors import serialization_error
from .interop import LuaInterop

#: JSON 嵌套深度上限（02：深度最多 32）。
MAX_JSON_DEPTH: int = 32
#: 编码后字节上限（02：1 MiB）。
MAX_ENCODED_BYTES: int = 1048576
#: 跨 Lua 整数上限（02：±(2^53−1)）。
MAX_SAFE_INT: int = 2 ** 53 - 1


@dataclass(frozen=True)
class Sentinels:
    """宿主私有哨兵（``json_null`` 值哨兵 / ``json_array`` 数组标记 metatable）。"""

    null: Any
    array_mt: Any


def make_sentinels(interop: LuaInterop) -> Sentinels:
    """创建宿主私有哨兵：``json_null`` = 唯一 table；``array_mt`` = 唯一 metatable。"""
    return Sentinels(null=interop.new_table(), array_mt=interop.new_table())


def measure_encoded_size(py_value: Any) -> int:
    """JSON（UTF-8、无冗余空白）编码后的字节数（仅用于度量，不用于转换语义）。"""
    text = json.dumps(py_value, ensure_ascii=False, separators=(",", ":"))
    return len(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# JSON 桥
# ---------------------------------------------------------------------------


class JsonBridge:
    """Lua ↔ JSON 值复制桥（显式区分数组/对象/null；拒绝非 JsonValue）。"""

    def __init__(
        self,
        interop: LuaInterop,
        sentinels: Sentinels,
        *,
        max_depth: int = MAX_JSON_DEPTH,
        max_bytes: int = MAX_ENCODED_BYTES,
    ) -> None:
        self.interop = interop
        self.sentinels = sentinels
        self.max_depth = int(max_depth)
        self.max_bytes = int(max_bytes)

    # -- Lua → JSON ----------------------------------------------------------

    def lua_to_json(self, value: Any, *, context: str = "return") -> Any:
        """转换单个 Lua 值为 JSON 值（并校验深度 / 编码大小）。

        Args:
            value: Lua 值（lupa 包装对象或替身）。
            context: 出错时 ``details.field`` 的定位（``"return"`` / ``"argument"`` / ``"value"``）。
        """
        result = self._convert(value, 1, (), context)
        size = measure_encoded_size(result)
        if size > self.max_bytes:
            raise serialization_error(
                "值编码后超过 %d 字节（实际 %d，JSON 契约限制）" % (self.max_bytes, size),
                field="value",
                details={"bytes": size, "limit": self.max_bytes},
            )
        return result

    def bridge_return(self, values: list) -> Any:
        """返回值语义（02）：0 → null；1 → 转换；≥2 → ``SERIALIZATION_ERROR``。"""
        count = len(values)
        if count == 0:
            return None
        if count == 1:
            return self.lua_to_json(values[0], context="return")
        raise serialization_error(
            "脚本返回 %d 个值，仅允许零或一个" % count, field="return"
        )

    # -- JSON → Lua ----------------------------------------------------------

    def json_to_lua(self, value: Any) -> Any:
        """把 JSON 值复制为**全新** Lua 结构（不共享；null → ``json_null`` 哨兵）。"""
        return self._to_lua(value, 1)

    # -- 内部：转换 ----------------------------------------------------------

    def _to_lua(self, value: Any, depth: int) -> Any:
        """JSON → Lua（值复制）。``None`` → ``json_null`` 哨兵（保留数组空位/对象 null）。"""
        if value is None:
            return self.sentinels.null
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value
        if isinstance(value, list):
            if depth > self.max_depth:
                raise serialization_error("参数嵌套超过 %d 层" % self.max_depth, field="argument")
            table = self.interop.new_table()
            for index, item in enumerate(value, 1):
                self.interop.table_set(table, index, self._to_lua(item, depth + 1))
            return table
        if isinstance(value, dict):
            if depth > self.max_depth:
                raise serialization_error("参数嵌套超过 %d 层" % self.max_depth, field="argument")
            table = self.interop.new_table()
            for key, item in value.items():
                if not isinstance(key, str):
                    raise serialization_error("对象键必须为字符串", field="argument")
                self.interop.table_set(table, key, self._to_lua(item, depth + 1))
            return table
        raise serialization_error(
            "无法转换为 Lua 值：%s" % type(value).__name__, field="argument"
        )

    def _convert(self, value: Any, depth: int, ancestors: tuple, context: str) -> Any:
        """Lua 值 → JSON 值（§2.3 算法；拒绝在副作用前）。"""
        value_type = self.interop.lua_type(value)
        if value_type == "nil":
            return None
        if value_type == "boolean":
            return bool(value)
        if value_type == "string":
            return str(value)
        if value_type == "number":
            return self._convert_number(value, context)
        if value_type == "table":
            return self._table_to_json(value, depth, ancestors, context)
        # function / userdata / thread / 其他 → 一律拒绝
        raise serialization_error(
            "不可序列化类型：%s" % value_type, field=context
        )

    def _convert_number(self, value: Any, context: str) -> Any:
        """数值校验（02/§2.4 ③）：NaN/Inf 拒绝；整数 |v|>2^53−1 拒绝；其余原样。"""
        if isinstance(value, bool):  # 防御：bool 应已由 'boolean' 分支处理
            raise serialization_error("非法数值类型", field=context)
        if isinstance(value, int):
            if abs(value) > MAX_SAFE_INT:
                raise serialization_error(
                    "整数超出 ±(2^53−1) 限制：%d" % value, field=context
                )
            return value
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                raise serialization_error(
                    "数值非法（NaN/Infinity 不允许）", field=context
                )
            return value
        raise serialization_error("未知数值类型", field=context)

    def _table_to_json(self, table: Any, depth: int, ancestors: tuple, context: str) -> Any:
        """table → JSON（数组/对象/空位/null 三态；§2.3）。"""
        interop = self.interop
        # a) json_null 哨兵（仅值位置有效）
        if self._is_null_mark(table):
            return None
        # b) json_array 标记（仅影响空表：空标记表 → []）
        is_marked_array = self._is_array_mark(table)
        # c) 深度（Q2：JSON 嵌套 >32 → SERIALIZATION_ERROR，非 CALL_DEPTH_EXCEEDED）
        if depth > self.max_depth:
            raise serialization_error(
                "JSON 嵌套超过 %d 层限制" % self.max_depth, field=context
            )
        # d) 循环引用（必须用 rawequal）
        for ancestor in ancestors:
            if interop.rawequal(ancestor, table):
                raise serialization_error("检测到循环 table 引用", field=context)
        # e) 键集合
        keys = list(interop.table_keys(table))
        # f) 空表：默认对象；仅 json_array({}) 表达空数组
        if not keys:
            return [] if is_marked_array else {}
        # g) 分类：整数键 / 字符串键
        int_keys: List[int] = []
        str_keys: List[str] = []
        for key in keys:
            key_type = interop.lua_type(key)
            if key_type == "string":
                str_keys.append(key)
            elif key_type == "number" and not isinstance(key, bool) and isinstance(key, int):
                int_keys.append(int(key))
            else:  # 布尔键 / 浮点键 / table/function 键 → 拒绝
                raise serialization_error(
                    "table 含非字符串/非整数键：%s" % key_type, field=context
                )
        if int_keys and str_keys:
            raise serialization_error("table 混合数组/对象键", field=context)
        child_ancestors = ancestors + (table,)
        if int_keys:
            count = len(int_keys)
            if set(int_keys) != set(range(1, count + 1)):
                raise serialization_error("稀疏数组被拒绝（键须连续 1..n）", field=context)
            return [
                self._convert(interop.table_get(table, index), depth + 1, child_ancestors, context)
                for index in range(1, count + 1)
            ]
        return {
            key: self._convert(interop.table_get(table, key), depth + 1, child_ancestors, context)
            for key in str_keys
        }

    # -- 内部：哨兵判定 ------------------------------------------------------

    def _is_null_mark(self, value: Any) -> bool:
        """``rawequal(sentinels.null, value)``（值哨兵；仅身份判定）。"""
        return bool(self.interop.rawequal(self.sentinels.null, value))

    def _is_array_mark(self, table: Any) -> bool:
        """``rawequal(sentinels.array_mt, getmetatable(table))``（数组标记）。"""
        meta = self.interop.getmetatable(table)
        return bool(self.interop.rawequal(self.sentinels.array_mt, meta))
