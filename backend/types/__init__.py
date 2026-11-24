# -*- coding: utf-8 -*-
"""backend.types barrel（TASK-006 批 B1）。

三端共享类型的 Python 落点：dataclass(frozen=True) + validate_* 纯函数
（无 IO、无全局状态），与 schema/types.schema.json 同源；
契约用例 schema/contract-cases.json kind=type_validation 三端同跑。
"""

from .capabilities import (
    CAPABILITY_IDS,
    CAPABILITY_STATES,
    CONDITION_TYPES,
    ERROR_CODES,
    REGIONS_SPACES,
    Capability,
    IpcError,
    ModelManifest,
    ProviderManifest,
    TypeValidationError,
    validate_capability,
    validate_error_structure,
    validate_model_manifest,
    validate_provider_manifest,
)
from .conditions import (
    Group,
    Script,
    ScriptParameter,
    Target,
    validate_group_fields,
    validate_script,
    validate_target,
)
from .region import (
    DisplayInfo,
    Frame,
    ImageAsset,
    ImageRef,
    LocalRect,
    Point,
    Rect,
    Region,
    validate_display_info,
    validate_frame,
    validate_image_asset,
    validate_image_ref,
    validate_local_rect,
    validate_point,
    validate_rect,
    validate_region,
)
from .runtime import (
    EXECUTION_STATUSES,
    MonitorConfig,
    RunConfig,
    ExecutionState,
    validate_execution_state,
    validate_monitor_config,
    validate_run_config,
)

# typeName（contract-cases.type_validation.typeName，与 types.schema.json
# definitions 键对应）→ validate 纯函数注册表
VALIDATORS = {
    "rect": validate_rect,
    "point": validate_point,
    "local_rect": validate_local_rect,
    "region": validate_region,
    "display_info": validate_display_info,
    "image_ref": validate_image_ref,
    "image_asset": validate_image_asset,
    "frame": validate_frame,
    "target": validate_target,
    "group": validate_group_fields,
    "script": validate_script,
    "run_config": validate_run_config,
    "monitor_config": validate_monitor_config,
    "execution_state": validate_execution_state,
    "error": validate_error_structure,
    "capability": validate_capability,
    "provider_manifest": validate_provider_manifest,
    "model_manifest": validate_model_manifest,
}


def validate_type(type_name: str, value: object) -> object:
    """按 typeName 分发到对应 validate 纯函数（契约测试与运行共用入口）。"""
    validator = VALIDATORS.get(type_name)
    if validator is None:
        raise TypeValidationError("INVALID_PARAMS", "typeName",
                                  "未知类型名: %s" % type_name)
    return validator(value)


__all__ = [
    # 常量
    "CAPABILITY_IDS", "CAPABILITY_STATES", "CONDITION_TYPES", "ERROR_CODES",
    "REGIONS_SPACES", "EXECUTION_STATUSES", "VALIDATORS",
    # dataclass
    "Capability", "IpcError", "ModelManifest", "ProviderManifest",
    "Group", "Script", "ScriptParameter", "Target",
    "DisplayInfo", "Frame", "ImageAsset", "ImageRef", "LocalRect", "Point",
    "Rect", "Region",
    "MonitorConfig", "RunConfig", "ExecutionState",
    # validate 纯函数
    "validate_capability", "validate_error_structure", "validate_model_manifest",
    "validate_provider_manifest", "validate_group_fields", "validate_script",
    "validate_target", "validate_display_info", "validate_frame",
    "validate_image_asset", "validate_image_ref", "validate_local_rect",
    "validate_point", "validate_rect", "validate_region",
    "validate_execution_state", "validate_monitor_config", "validate_run_config",
    "validate_type",
    # 错误
    "TypeValidationError",
]
