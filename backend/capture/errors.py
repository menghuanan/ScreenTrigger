# -*- coding: utf-8 -*-
"""捕获错误类型（TASK-007，06 错误码注册表）。

`CaptureError` 与 ``backend.config.service.ConfigError`` 同构：四字段结构
``{code,message,details,retryable}``（06），``details.field`` 逐字定位到出错字段。
命令接线层（backend/capture/commands.py）把它映射为 ``DispatcherError``。

错误码严格取自 06 注册表，本模块只使用下列用例声明过的码：
INVALID_PARAMS / INVALID_REGION / INVALID_IMAGE / IMAGE_EXPIRED /
DISPLAY_NOT_FOUND / CAPTURE_FAILED / WINDOW_NOT_FOUND / WINDOW_UNAVAILABLE /
RESOURCE_LIMIT。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class CaptureError(Exception):
    """捕获/图像错误（06 四字段结构；field 进 details 供 UI 定位）。"""

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

def invalid_params(field: str, message: str) -> CaptureError:
    """类型/范围/枚举错误（06：INVALID_PARAMS，retryable=false）。"""
    return CaptureError("INVALID_PARAMS", message, field=field)


def invalid_region(field: str, message: str) -> CaptureError:
    """太小/越界/空间不匹配（06：INVALID_REGION，retryable=false）。"""
    return CaptureError("INVALID_REGION", message, field=field)


def invalid_image(message: str, field: Optional[str] = None) -> CaptureError:
    """解码失败/非法格式（06：INVALID_IMAGE）。"""
    return CaptureError("INVALID_IMAGE", message, field=field)


def image_expired(message: str = "图像引用过期或不属于当前会话") -> CaptureError:
    """引用过期或跨会话（06：IMAGE_EXPIRED）；不是静默成功。"""
    return CaptureError("IMAGE_EXPIRED", message)


def display_not_found(display_id: str) -> CaptureError:
    """显示器消失（06：DISPLAY_NOT_FOUND，retryable=false）。"""
    return CaptureError(
        "DISPLAY_NOT_FOUND",
        "显示器不存在或已拔出",
        details={"display_id": display_id},
    )


def capture_failed(message: str, retryable: bool = True) -> CaptureError:
    """驱动/瞬时捕获失败（06：CAPTURE_FAILED，默认 retryable=true）。

    仅此码允许调用方自动重试一次（06「只自动重试瞬时捕获失败一次（200ms）」）；
    自动重试逻辑在 CaptureService，不在 backend 内。
    """
    return CaptureError("CAPTURE_FAILED", message, retryable=retryable)


def window_not_found(binding_id: str) -> CaptureError:
    """窗口身份失效/已关闭（06：WINDOW_NOT_FOUND，retryable=false）。"""
    return CaptureError(
        "WINDOW_NOT_FOUND",
        "窗口不存在或身份失效",
        details={"binding_id": binding_id},
    )


def window_unavailable(binding_id: str) -> CaptureError:
    """失焦/最小化导致不可操作（06：WINDOW_UNAVAILABLE，retryable=true）。"""
    return CaptureError(
        "WINDOW_UNAVAILABLE",
        "窗口最小化或不可见，无法捕获",
        details={"binding_id": binding_id},
        retryable=True,
    )


def resource_limit(message: str) -> CaptureError:
    """像素/字节超预算（06：RESOURCE_LIMIT，retryable=false）。"""
    return CaptureError("RESOURCE_LIMIT", message)
