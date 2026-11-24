# -*- coding: utf-8 -*-
"""目标/分组命令接线（TASK-011；03 命令目录「目标、分组和脚本文件」10 条命令）。

把 10 条目标/分组命令注册到 dispatcher（仿 ``backend/capture/commands.py``）：

``create_target`` / ``get_target`` / ``list_targets`` / ``update_target`` /
``delete_target`` / ``create_group`` / ``list_groups`` / ``update_group`` /
``delete_group`` / ``move_target_to_group``。

职责：参数白名单校验（未声明参数 → INVALID_PARAMS，``details.field`` 定位）、
把领域/存储错误（``StorageError``）与类型错误（``TypeValidationError``）**归一为**
``DispatcherError``（06 四字段）。业务逻辑在 domain/service；本层不发事件
（``catalog_changed`` 由 service 在 commit 成功后发）。

``update_group`` 的 ``name`` / ``parent_group_id`` 用「键是否存在」区分
「不改名/不搬移」（键缺省）与「显式 null 移至顶层」（键存在且为 null）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from ..ipc.dispatcher import DispatcherError
from ..storage.errors import StorageError
from ..types.capabilities import TypeValidationError
from .domain import UNSET
from .service import PAGE_DEFAULT_LIMIT, PAGE_DEFAULT_OFFSET, TargetService

# 各命令允许的 params 键集（白名单外 → INVALID_PARAMS，定位多余字段）
_TARGET_COMMAND_PARAMS: Dict[str, Tuple[str, ...]] = {
    "create_target": ("draft",),
    "get_target": ("target_id",),
    "list_targets": ("group_id", "query", "offset", "limit"),
    "update_target": ("target_id", "expected_revision", "patch"),
    "delete_target": ("target_id", "expected_revision"),
    "create_group": ("name", "parent_group_id"),
    "list_groups": (),
    "update_group": ("group_id", "name", "parent_group_id", "expected_revision"),
    "delete_group": ("group_id", "expected_revision"),
    "move_target_to_group": ("target_id", "group_id", "expected_revision"),
}

# list_targets 分页范围（03：limit 1–500；offset ≥0）
PAGE_MIN_LIMIT = 1
PAGE_MAX_LIMIT = 500
PAGE_MIN_OFFSET = 0


# ---------------------------------------------------------------------------
# 参数取值辅助
# ---------------------------------------------------------------------------

def _require_str(params: Dict[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise StorageError(
            "INVALID_PARAMS", "参数 %s 必填且为非空字符串" % name, field=name
        )
    return value


def _require_int(params: Dict[str, Any], name: str, minimum: int = 1) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError("INVALID_PARAMS", "参数 %s 必须为整数" % name, field=name)
    if value < minimum:
        raise StorageError(
            "INVALID_PARAMS", "参数 %s 不得小于 %d" % (name, minimum), field=name
        )
    return value


def _optional_int(
    params: Dict[str, Any], name: str, default: int, minimum: int, maximum: int
) -> int:
    if name not in params:
        return default
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError("INVALID_PARAMS", "参数 %s 必须为整数" % name, field=name)
    if value < minimum or value > maximum:
        raise StorageError(
            "INVALID_PARAMS",
            "参数 %s 超出范围 [%d, %d]" % (name, minimum, maximum),
            field=name,
        )
    return value


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def register_target_commands(dispatcher: Any, service: TargetService) -> None:
    """把 10 条目标/分组命令注册到 dispatcher；错误归一为 DispatcherError（06 四字段）。"""

    def _wrap(command: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]):
        allowed = _TARGET_COMMAND_PARAMS[command]

        def _handler(envelope: Dict[str, Any]) -> Dict[str, Any]:
            params = envelope.get("params")
            if not isinstance(params, dict):
                params = {}
            try:
                for key in params:
                    if key not in allowed:
                        raise StorageError(
                            "INVALID_PARAMS",
                            "%s 未声明参数: %s" % (command, key),
                            field=key,
                        )
                return handler(params)
            except TypeValidationError as exc:
                raise DispatcherError(
                    exc.code,
                    exc.message,
                    details={"field": exc.field},
                    retryable=exc.retryable,
                ) from exc
            except StorageError as exc:
                raise DispatcherError(
                    exc.code, exc.message, details=exc.details, retryable=exc.retryable
                ) from exc

        return _handler

    def _cmd_create_target(params: Dict[str, Any]) -> Dict[str, Any]:
        return {"target": service.create_target(params.get("draft"))}

    def _cmd_get_target(params: Dict[str, Any]) -> Dict[str, Any]:
        target_id = _require_str(params, "target_id")
        return {"target": service.get_target(target_id)}

    def _cmd_list_targets(params: Dict[str, Any]) -> Dict[str, Any]:
        group_id: Any = params["group_id"] if "group_id" in params else UNSET
        query = params.get("query")
        if query is not None and not isinstance(query, str):
            raise StorageError("INVALID_PARAMS", "参数 query 必须为字符串", field="query")
        offset = _optional_int(params, "offset", PAGE_DEFAULT_OFFSET, PAGE_MIN_OFFSET, 2**31)
        limit = _optional_int(params, "limit", PAGE_DEFAULT_LIMIT, PAGE_MIN_LIMIT, PAGE_MAX_LIMIT)
        return service.list_targets(group_id, query, offset, limit)

    def _cmd_update_target(params: Dict[str, Any]) -> Dict[str, Any]:
        target_id = _require_str(params, "target_id")
        expected_revision = _require_int(params, "expected_revision")
        return {"target": service.update_target(target_id, params.get("patch"), expected_revision)}

    def _cmd_delete_target(params: Dict[str, Any]) -> Dict[str, Any]:
        target_id = _require_str(params, "target_id")
        expected_revision = _require_int(params, "expected_revision")
        service.delete_target(target_id, expected_revision)
        return {"ok": True}

    def _cmd_create_group(params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise StorageError("INVALID_PARAMS", "参数 name 必填且为字符串", field="name")
        parent = params.get("parent_group_id")
        return {"group": service.create_group(name, parent)}

    def _cmd_list_groups(_params: Dict[str, Any]) -> Dict[str, Any]:
        return {"groups": service.list_groups()}

    def _cmd_update_group(params: Dict[str, Any]) -> Dict[str, Any]:
        group_id = _require_str(params, "group_id")
        expected_revision = _require_int(params, "expected_revision")
        name: Any = params["name"] if "name" in params else UNSET
        parent: Any = params["parent_group_id"] if "parent_group_id" in params else UNSET
        return {
            "group": service.update_group(
                group_id, expected_revision, name=name, parent_group_id=parent
            )
        }

    def _cmd_delete_group(params: Dict[str, Any]) -> Dict[str, Any]:
        group_id = _require_str(params, "group_id")
        expected_revision = _require_int(params, "expected_revision")
        service.delete_group(group_id, expected_revision)
        return {"ok": True}

    def _cmd_move_target_to_group(params: Dict[str, Any]) -> Dict[str, Any]:
        target_id = _require_str(params, "target_id")
        group_id = params.get("group_id")
        if group_id is not None and not isinstance(group_id, str):
            raise StorageError(
                "INVALID_PARAMS", "参数 group_id 必须为字符串或 null", field="group_id"
            )
        expected_revision = _require_int(params, "expected_revision")
        return {
            "target": service.move_target_to_group(target_id, group_id, expected_revision)
        }

    dispatcher.register("create_target", _wrap("create_target", _cmd_create_target))
    dispatcher.register("get_target", _wrap("get_target", _cmd_get_target))
    dispatcher.register("list_targets", _wrap("list_targets", _cmd_list_targets))
    dispatcher.register("update_target", _wrap("update_target", _cmd_update_target))
    dispatcher.register("delete_target", _wrap("delete_target", _cmd_delete_target))
    dispatcher.register("create_group", _wrap("create_group", _cmd_create_group))
    dispatcher.register("list_groups", _wrap("list_groups", _cmd_list_groups))
    dispatcher.register("update_group", _wrap("update_group", _cmd_update_group))
    dispatcher.register("delete_group", _wrap("delete_group", _cmd_delete_group))
    dispatcher.register(
        "move_target_to_group", _wrap("move_target_to_group", _cmd_move_target_to_group)
    )
