# -*- coding: utf-8 -*-
"""存储原语错误类型（TASK-010，06 错误码注册表）。

与 ``backend.capture.errors.CaptureError`` / ``backend.config.service.ConfigError``
**同构**：四字段结构 ``{code,message,details,retryable}``（06），``details.field``
逐字定位到出错字段。命令接线层（TASK-011）再归一为 ``DispatcherError``。

错误码严格取自 06 注册表，本模块只使用任务用例声明过的码：
STORAGE_ERROR / DATA_CORRUPT / SCHEMA_UNSUPPORTED / CONFLICT / IN_USE /
INVALID_PARAMS / RESOURCE_LIMIT。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class StorageError(Exception):
    """存储/持久化错误（06 四字段结构；field 进 details 供 UI 定位）。"""

    def __init__(
        self,
        code: str,
        message: str,
        field: Optional[str] = None,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message
        merged: Dict[str, Any] = dict(details) if details else {}
        if field is not None:
            merged["field"] = field
        self.details: Dict[str, Any] = merged
        self.retryable: bool = retryable

    def to_dict(self) -> Dict[str, Any]:
        """06 四字段错误结构（供命令层/日志使用）。"""
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


# ---------------------------------------------------------------------------
# 具名构造器（code 与 retryable 与 06 注册表默认一致；集中一处避免漂移）
# ---------------------------------------------------------------------------

def storage_error(
    message: str,
    *,
    retryable: bool = True,
    details: Optional[Dict[str, Any]] = None,
) -> StorageError:
    """磁盘不足/被占用/落盘失败（06：STORAGE_ERROR，retryable=true）。"""
    return StorageError("STORAGE_ERROR", message, retryable=retryable, details=details)


def data_corrupt(
    message: str,
    field: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> StorageError:
    """JSON/hash/外键校验失败（06：DATA_CORRUPT，retryable=false，保留原文件）。"""
    return StorageError("DATA_CORRUPT", message, field=field, details=details)


def schema_unsupported(message: str) -> StorageError:
    """数据版本高于当前软件（06：SCHEMA_UNSUPPORTED，不降级覆盖）。"""
    return StorageError("SCHEMA_UNSUPPORTED", message)


def conflict(
    message: str,
    field: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> StorageError:
    """revision 或 ID 内容冲突（06：CONFLICT，重新加载/合并）。"""
    return StorageError("CONFLICT", message, field=field, details=details)


def in_use(message: str, details: Optional[Dict[str, Any]] = None) -> StorageError:
    """删除活动执行引用（06：IN_USE，停止后再操作）。"""
    return StorageError("IN_USE", message, details=details)


def invalid_params(field: str, message: str) -> StorageError:
    """类型、范围、枚举错误（06：INVALID_PARAMS，定位字段）。"""
    return StorageError("INVALID_PARAMS", message, field=field)


def resource_limit(message: str, details: Optional[Dict[str, Any]] = None) -> StorageError:
    """内存/像素/数量超预算（06：RESOURCE_LIMIT，降低规模）。"""
    return StorageError("RESOURCE_LIMIT", message, details=details)
