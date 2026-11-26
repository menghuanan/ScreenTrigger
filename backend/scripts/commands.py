# -*- coding: utf-8 -*-
"""脚本命令接线（TASK-012；03 命令目录 5 条：``list_scripts``/``load_script``/
``save_script``/``delete_script``/``validate_script``）。

把 5 条脚本命令注册到 dispatcher（仿 ``backend/targets/commands.py``）：

职责：参数白名单校验（未声明参数 → ``INVALID_PARAMS``，``details.field`` 定位多余字段）、
实体 → dict 编解码、把领域/存储错误（``StorageError``）与类型错误（``TypeValidationError``）
**归一为** ``DispatcherError``（06 四字段）。业务逻辑在 domain/service；本层不发事件
（``catalog_changed`` 由 service 在 commit 成功后发）。

``save_script`` 的 ``script_id`` / ``expected_revision`` 用「键是否存在」区分
「新建（同时省）」与「更新（同时给）」；「只给其一」由 ``ScriptService`` 判为
``INVALID_PARAMS``，``details.field`` = 缺失的伙伴字段（Q7）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from ..ipc.dispatcher import DispatcherError
from ..storage.errors import StorageError
from ..types.capabilities import TypeValidationError
from .domain import UNSET
from .service import (
    PAGE_DEFAULT_LIMIT,
    PAGE_DEFAULT_OFFSET,
    ScriptService,
)

# 各命令允许的 params 键集（白名单外 → INVALID_PARAMS，定位多余字段）
_SCRIPT_COMMAND_PARAMS: Dict[str, Tuple[str, ...]] = {
    "list_scripts": ("offset", "limit"),
    "load_script": ("script_id",),
    "save_script": ("script_id", "expected_revision", "name", "source", "parameters", "action_policy"),
    "delete_script": ("script_id", "expected_revision"),
    "validate_script": ("source",),
}

# list_scripts 分页范围（Q6：limit 1–500；offset ≥0）
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


def _require_present(params: Dict[str, Any], name: str) -> Any:
    """要求键存在（值可为 null）；缺失 → INVALID_PARAMS field=name。"""
    if name not in params:
        raise StorageError("INVALID_PARAMS", "参数 %s 必填" % name, field=name)
    return params.get(name)


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

def register_script_commands(dispatcher: Any, service: ScriptService) -> None:
    """把 5 条脚本命令注册到 dispatcher；错误归一为 DispatcherError（06 四字段）。"""

    def _wrap(command: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]):
        allowed = _SCRIPT_COMMAND_PARAMS[command]

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

    def _cmd_list_scripts(params: Dict[str, Any]) -> Dict[str, Any]:
        offset = _optional_int(
            params, "offset", PAGE_DEFAULT_OFFSET, PAGE_MIN_OFFSET, 2**31
        )
        limit = _optional_int(
            params, "limit", PAGE_DEFAULT_LIMIT, PAGE_MIN_LIMIT, PAGE_MAX_LIMIT
        )
        return service.list_scripts(offset, limit)

    def _cmd_load_script(params: Dict[str, Any]) -> Dict[str, Any]:
        script_id = _require_str(params, "script_id")
        return service.load_script(script_id)

    def _cmd_save_script(params: Dict[str, Any]) -> Dict[str, Any]:
        name = _require_present(params, "name")
        source = _require_present(params, "source")
        parameters = _require_present(params, "parameters")
        # 「键存在才校验」：保持「只给其一 → 缺失伙伴字段」语义（Q7）。
        # 非 int（str/None/float/list/bool）→ INVALID_PARAMS field=expected_revision
        # （与 delete_script 的 _require_int 一致；防 int() 抛 ValueError/TypeError
        # 逃出 dispatcher 击落 sidecar 进程）。
        script_id: Any = UNSET
        if "script_id" in params:
            script_id = _require_str(params, "script_id")
        expected_revision: Any = UNSET
        if "expected_revision" in params:
            expected_revision = _require_int(params, "expected_revision")
        return {
            "script": service.save_script(
                name,
                source,
                parameters,
                script_id=script_id,
                expected_revision=expected_revision,
                **({"action_policy": params["action_policy"]} if "action_policy" in params else {}),
            )
        }

    def _cmd_delete_script(params: Dict[str, Any]) -> Dict[str, Any]:
        script_id = _require_str(params, "script_id")
        expected_revision = _require_int(params, "expected_revision")
        service.delete_script(script_id, expected_revision)
        return {"ok": True}

    def _cmd_validate_script(params: Dict[str, Any]) -> Dict[str, Any]:
        source = _require_present(params, "source")
        return service.validate_script(source)

    dispatcher.register("list_scripts", _wrap("list_scripts", _cmd_list_scripts))
    dispatcher.register("load_script", _wrap("load_script", _cmd_load_script))
    dispatcher.register("save_script", _wrap("save_script", _cmd_save_script))
    dispatcher.register("delete_script", _wrap("delete_script", _cmd_delete_script))
    dispatcher.register("validate_script", _wrap("validate_script", _cmd_validate_script))
