# -*- coding: utf-8 -*-
"""识别命令接线（TASK-013 §6.1；03 命令目录 6 条识别命令，**全部异步 job 化**）。

把 6 条识别命令注册到 dispatcher（仿 ``backend/targets/commands.py``/``backend/scripts/commands.py``）：

- **同步**（``response.error``）：仅**参数白名单/类型/枚举**错误 → ``INVALID_PARAMS``（**不建 job**）；
- **异步**（job error + 事件）：图像/模型/能力/超时/内部故障 —— 全部在 job 内发生，经 ``get_job``
  与 ``recognition_error`` 双路可查；
- 命令 handler **只**做白名单 + 类型校验 → ``service.submit`` → **立即**（≤5s）返回 ``{job_id}``；
- 本层**不发事件**（由 service 在终态发）；错误归一为 ``DispatcherError``（06 四字段）。

**不新增命令**（6 条已在 03 命令目录，对外总数仍 62）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from ..ipc.dispatcher import DispatcherError
from ..storage.errors import StorageError
from ..types.capabilities import TypeValidationError
from ..types.conditions import _validate_color_params
from .service import VisionService
from .types import RecognitionError

#: 各命令允许的 params 键集（白名单外 → INVALID_PARAMS，定位多余字段）。
_VISION_COMMAND_PARAMS: Dict[str, Tuple[str, ...]] = {
    "recognize_text": ("image_id", "model_id"),
    "match_template": ("image_id", "template_image_id", "threshold"),
    "match_contour": ("image_id", "template_image_id", "threshold"),
    "detect_color": ("image_id", "params"),
    "evaluate_target_conditions": ("image_id", "target_id"),
    "test_recognition": ("image_id", "draft"),
}

#: 匹配类命令 threshold 缺省（03：threshold?:number=.85）。
MATCH_DEFAULT_THRESHOLD: float = 0.85


def _require_str(params: Dict[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise StorageError("INVALID_PARAMS", "参数 %s 必填且为非空字符串" % name, field=name)
    return value


def _require_object(params: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = params.get(name)
    if not isinstance(value, dict):
        raise StorageError("INVALID_PARAMS", "参数 %s 必须为对象" % name, field=name)
    return value


def _optional_threshold(params: Dict[str, Any]) -> float:
    if "threshold" not in params:
        return MATCH_DEFAULT_THRESHOLD
    value = params.get("threshold")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageError("INVALID_PARAMS", "参数 threshold 必须为数值", field="threshold")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise StorageError("INVALID_PARAMS", "参数 threshold 必须为有限数", field="threshold")
    if number < 0.0 or number > 1.0:
        raise StorageError("INVALID_PARAMS", "参数 threshold 须在 [0,1]", field="threshold")
    return number


def register_vision_commands(dispatcher: Any, service: VisionService) -> None:
    """把 6 条识别命令注册到 dispatcher；错误归一为 ``DispatcherError``（06 四字段）。"""

    def _wrap(command: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]):
        allowed = _VISION_COMMAND_PARAMS[command]

        def _handler(envelope: Dict[str, Any]) -> Dict[str, Any]:
            params = envelope.get("params")
            if not isinstance(params, dict):
                params = {}
            params = dict(params)
            try:
                for key in params:
                    if key not in allowed:
                        raise StorageError(
                            "INVALID_PARAMS", "%s 未声明参数: %s" % (command, key), field=key
                        )
                return handler(params)
            except TypeValidationError as exc:
                raise DispatcherError(exc.code, exc.message, details={"field": exc.field},
                                      retryable=exc.retryable) from exc
            except RecognitionError as exc:
                raise DispatcherError(exc.code, exc.message, details=dict(exc.details),
                                      retryable=exc.retryable) from exc
            except StorageError as exc:
                raise DispatcherError(exc.code, exc.message, details=dict(exc.details),
                                      retryable=exc.retryable) from exc

        return _handler

    def _cmd_recognize_text(params: Dict[str, Any]) -> Dict[str, Any]:
        params["image_id"] = _require_str(params, "image_id")
        params["model_id"] = _require_str(params, "model_id")
        return {"job_id": service.submit("recognize_text", params)}

    def _cmd_match_template(params: Dict[str, Any]) -> Dict[str, Any]:
        params["image_id"] = _require_str(params, "image_id")
        params["template_image_id"] = _require_str(params, "template_image_id")
        params["threshold"] = _optional_threshold(params)
        return {"job_id": service.submit("match_template", params)}

    def _cmd_match_contour(params: Dict[str, Any]) -> Dict[str, Any]:
        params["image_id"] = _require_str(params, "image_id")
        params["template_image_id"] = _require_str(params, "template_image_id")
        params["threshold"] = _optional_threshold(params)
        return {"job_id": service.submit("match_contour", params)}

    def _cmd_detect_color(params: Dict[str, Any]) -> Dict[str, Any]:
        params["image_id"] = _require_str(params, "image_id")
        params["params"] = _require_object(params, "params")
        # TASK-014 Q6：命令层**同步**复用 ColorParams 叶校验器（fail-fast；
        # field 逐字 conditions.params.*）；provider 侧保留异步兜底。
        _validate_color_params(params["params"])
        return {"job_id": service.submit("detect_color", params)}

    def _cmd_evaluate_target_conditions(params: Dict[str, Any]) -> Dict[str, Any]:
        params["image_id"] = _require_str(params, "image_id")
        params["target_id"] = _require_str(params, "target_id")
        return {"job_id": service.submit("evaluate_target_conditions", params)}

    def _cmd_test_recognition(params: Dict[str, Any]) -> Dict[str, Any]:
        params["image_id"] = _require_str(params, "image_id")
        params["draft"] = _require_object(params, "draft")
        return {"job_id": service.submit("test_recognition", params)}

    dispatcher.register("recognize_text", _wrap("recognize_text", _cmd_recognize_text))
    dispatcher.register("match_template", _wrap("match_template", _cmd_match_template))
    dispatcher.register("match_contour", _wrap("match_contour", _cmd_match_contour))
    dispatcher.register("detect_color", _wrap("detect_color", _cmd_detect_color))
    dispatcher.register(
        "evaluate_target_conditions", _wrap("evaluate_target_conditions", _cmd_evaluate_target_conditions)
    )
    dispatcher.register("test_recognition", _wrap("test_recognition", _cmd_test_recognition))
