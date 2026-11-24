# -*- coding: utf-8 -*-
"""图像命令接线：``import_image``（TASK-009）与 ``register_host_frame``（DEF-TASK008-01）。

- ``import_image {import_token:string}`` → ``{image:ImageRef}``（03 公开命令目录）；
- ``register_host_frame {token:string}`` → ``{image:ImageRef}``（**内部命令**，
  仅宿主↔引擎内部约定；不进 03 公开命令目录/不影响对外 62 条统计）；
- 未声明参数一律 INVALID_PARAMS 并 details.field 定位；
- 错误映射：TypeValidationError / CaptureError → DispatcherError（06 四字段）；
- 复用已完成命令：``release_image`` / ``get_image_preview`` 由
  ``register_capture_commands`` 注册，操作**同一个** SessionImageStore。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from ..types.capabilities import TypeValidationError

from .host_frame import HostFrameRegistrationService
from .import_service import ImageImportService

_IMAGE_COMMAND_PARAMS: Dict[str, Tuple[str, ...]] = {
    "import_image": ("import_token",),
    "register_host_frame": ("token",),
}


def register_image_commands(dispatcher: Any, import_service: ImageImportService) -> None:
    """把 ``import_image`` 注册到 dispatcher（仿 register_capture_commands）。"""
    from backend.ipc.dispatcher import DispatcherError

    from ..capture.commands import image_ref_to_dict
    from ..capture.errors import CaptureError, invalid_params

    allowed = _IMAGE_COMMAND_PARAMS["import_image"]

    def _handler(envelope: Dict[str, Any]) -> Dict[str, Any]:
        params = envelope.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            for key in params:
                if key not in allowed:
                    raise invalid_params(key, "import_image 未声明参数: %s" % key)
            token = params.get("import_token")
            if not isinstance(token, str) or not token:
                raise invalid_params("import_token", "参数 import_token 必填且为非空字符串")
            image_ref, _asset = import_service.import_image(token)
            return {"image": image_ref_to_dict(image_ref)}
        except TypeValidationError as exc:
            raise DispatcherError(
                exc.code, exc.message, details={"field": exc.field}, retryable=exc.retryable
            ) from exc
        except CaptureError as exc:
            raise DispatcherError(
                exc.code, exc.message, details=exc.details, retryable=exc.retryable
            ) from exc

    dispatcher.register("import_image", _handler)


def register_host_frame_command(
    dispatcher: Any, registration_service: HostFrameRegistrationService
) -> None:
    """把**内部命令** ``register_host_frame`` 注册到 dispatcher（DEF-TASK008-01）。

    宿主捕获帧后经此内部命令把一次性 host-frame token 交引擎，引擎校验后
    由 ``SessionImageStore`` **签发** ImageRef 返回。会话上下文取自
    ``dispatcher.session.session_id``（与 import_image 同一会话归属）。
    """
    from backend.ipc.dispatcher import DispatcherError

    from ..capture.commands import image_ref_to_dict
    from ..capture.errors import CaptureError, invalid_params

    allowed = _IMAGE_COMMAND_PARAMS["register_host_frame"]

    def _handler(envelope: Dict[str, Any]) -> Dict[str, Any]:
        params = envelope.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            for key in params:
                if key not in allowed:
                    raise invalid_params(key, "register_host_frame 未声明参数: %s" % key)
            token = params.get("token")
            if not isinstance(token, str) or not token:
                raise invalid_params("token", "参数 token 必填且为非空字符串")
            session_id = dispatcher.session.session_id
            image_ref = registration_service.register(token, session_id)
            return {"image": image_ref_to_dict(image_ref)}
        except TypeValidationError as exc:
            raise DispatcherError(
                exc.code, exc.message, details={"field": exc.field}, retryable=exc.retryable
            ) from exc
        except CaptureError as exc:
            raise DispatcherError(
                exc.code, exc.message, details=exc.details, retryable=exc.retryable
            ) from exc

    dispatcher.register("register_host_frame", _handler)
