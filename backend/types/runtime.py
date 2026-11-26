# -*- coding: utf-8 -*-
"""运行配置类型（TASK-006 批 B1，设计 §3.2/§6.2；schema/types.schema.json 同源）。

02 权威规则：
- RunConfig：mode 四枚举（once/count/forever/until_target）与 iterations/
  stop_target_id 条件必填/必 null；display_id 与 binding_id 互斥（绑定态
  display_id 必 null、未绑定 display_id 必填）；region 空间与绑定一致性；
  frequency_hz 0.1–30；total_timeout_ms ≥0（0=不限时）；
- MonitorConfig：复用 RunConfig 绑定字段；不含 script_id/args/mode/iterations/
  stop_target_id；交叉约束照 02/schema allOf：
  on_match=stop → repeat 必须 false 且 action_script_id 必 null；
  on_match=run_script → action_script_id 必填；
  on_timeout=retry → max_retries 1..100；否则 max_retries 必须 0；
  on_timeout=run_script → timeout_script_id 必填；否则必 null；
- ExecutionState：status 八枚举（starting/running/paused/debug_paused/stopping/
  completed/cancelled/failed，docs/02 权威）；终态不转回 running 属状态机语义；
  纯 monitor 时 script_id=null。

全部 validate_* 为纯函数（无 IO），错误统一 TypeValidationError。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .capabilities import (
    IpcError,
    bool_field,
    check_keys,
    enum_field,
    fail,
    int_field,
    number_field,
    require_object,
    str_field,
    validate_error_structure,
)
from .region import Region, validate_region, validate_rfc3339_utc

RUN_MODES: Tuple[str, ...] = ("once", "count", "forever", "until_target")
ON_MATCH_ACTIONS: Tuple[str, ...] = ("run_script", "stop")
ON_TIMEOUT_ACTIONS: Tuple[str, ...] = ("stop", "retry", "run_script")
TRIGGERS: Tuple[str, ...] = ("rising_edge", "every_match")
EXECUTION_STATUSES: Tuple[str, ...] = (
    "starting", "running", "paused", "debug_paused", "stopping",
    "completed", "cancelled", "failed",
)
EXECUTION_KINDS: Tuple[str, ...] = ("script", "monitor")

_FREQUENCY_MIN = 0.1  # 02：frequency 0.1–30 Hz（configLimits 同源）
_FREQUENCY_MAX = 30.0
_MAX_RETRIES = 100  # 02：max_retries 0–100


def _validate_optional_str(container: Dict[str, Any], name: str, field: str) -> Optional[str]:
    value = container.get(name)
    if value is not None and not isinstance(value, str):
        fail("INVALID_PARAMS", field, "%s 必须为字符串或 null" % field)
    return value


def _validate_scope_fields(data: Dict[str, Any]) -> Tuple[
        Optional[Region], Optional[str], Optional[str]]:
    """RunConfig/MonitorConfig 共用的 region/display_id/binding_id 绑定规则。

    返回 (region, display_id, binding_id)。规则（02 + schema allOf）：
    - display_id 与 binding_id 互斥：同时非 null → 定位 binding_id
      （契约用例口径：run_config/monitor_config 顶层字段不带前缀）；
    - binding_id 为 null → display_id 必填非空；binding_id 非 null → display_id 必 null；
    - region 非 null 时空间与绑定一致：screen→display_id、client→binding_id、
      image_pixel 不可作为运行范围（无绑定主体），定位 region.space。
    """
    display_id = _validate_optional_str(data, "display_id", "display_id")
    binding_id = _validate_optional_str(data, "binding_id", "binding_id")
    mode = data.get("window_mode", "foreground")
    if mode not in ("foreground", "background") or (mode == "background" and not binding_id):
        fail("INVALID_PARAMS", "window_mode", "后台兼容模式仅允许已绑定窗口")
    if display_id is not None and binding_id is not None:
        fail("INVALID_PARAMS", "binding_id",
             "display_id 与 binding_id 互斥，不得同时非 null")
    if binding_id is None and display_id is None:
        fail("INVALID_PARAMS", "display_id", "未绑定时 display_id 必填")
    region_value = data.get("region")
    if region_value is None:
        region: Optional[Region] = None
    else:
        region = validate_region(region_value)
        if region.space == "screen_physical" and binding_id is not None:
            fail("INVALID_PARAMS", "region.space",
                 "region 空间 screen_physical 与 binding_id 绑定不一致")
        if region.space == "window_client_physical" and display_id is not None:
            fail("INVALID_PARAMS", "region.space",
                 "region 空间 window_client_physical 与 display_id 不一致")
        if region.space == "image_pixel":
            fail("INVALID_PARAMS", "region.space",
                 "image_pixel 空间不可作为运行/监控范围")
    return region, display_id, binding_id


# ---------------------------------------------------------------------------
# RunConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunConfig:
    """02：脚本运行配置；mode 互斥字段条件必填/必 null。"""

    script_id: str
    args: Dict[str, Any]
    mode: str
    iterations: Optional[int]
    stop_target_id: Optional[str]
    debug: bool
    region: Optional[Region]
    display_id: Optional[str]
    binding_id: Optional[str]
    allow_exit: bool
    frequency_hz: float
    total_timeout_ms: int
    window_mode: str = "foreground"


def _validate_mode_counters(data: Dict[str, Any], mode: str) -> Tuple[
        Optional[int], Optional[str]]:
    # 02：count → iterations 必填 ≥1；其余模式 iterations 必 null
    iterations_value = data.get("iterations")
    if mode == "count":
        if iterations_value is None:
            fail("INVALID_PARAMS", "iterations", "mode=count 时 iterations 必填")
        iterations: Optional[int] = int_field(data, "iterations", "iterations",
                                              minimum=1)
    else:
        if iterations_value is not None:
            fail("INVALID_PARAMS", "iterations",
                 "iterations 仅 mode=count 允许非 null")
        iterations = None
    # 02：stop_target_id 仅 until_target 非空，其余模式必 null
    stop_value = data.get("stop_target_id")
    if mode == "until_target":
        if stop_value is None:
            fail("INVALID_PARAMS", "stop_target_id",
                 "mode=until_target 时 stop_target_id 必填")
        stop_target_id: Optional[str] = str_field(data, "stop_target_id",
                                                  "stop_target_id")
    else:
        if stop_value is not None:
            fail("INVALID_PARAMS", "stop_target_id",
                 "stop_target_id 仅 mode=until_target 允许非 null")
        stop_target_id = None
    return iterations, stop_target_id


def validate_run_config(value: Any) -> RunConfig:
    data = require_object(value, "run_config")
    check_keys(
        data,
        ("script_id", "args", "mode", "iterations", "stop_target_id", "debug",
         "region", "display_id", "binding_id", "allow_exit", "frequency_hz",
         "total_timeout_ms", "window_mode"),
        "run_config",
    )
    mode = enum_field(data, "mode", "mode", RUN_MODES)
    iterations, stop_target_id = _validate_mode_counters(data, mode)
    args = data.get("args")
    if not isinstance(args, dict):
        fail("INVALID_PARAMS", "args", "args 必须为对象")
    region, display_id, binding_id = _validate_scope_fields(data)
    return RunConfig(
        window_mode=data.get("window_mode", "foreground"),
        script_id=str_field(data, "script_id", "script_id"),
        args=args,
        mode=mode,
        iterations=iterations,
        stop_target_id=stop_target_id,
        debug=bool_field(data, "debug", "debug"),
        region=region,
        display_id=display_id,
        binding_id=binding_id,
        allow_exit=bool_field(data, "allow_exit", "allow_exit"),
        frequency_hz=number_field(data, "frequency_hz", "frequency_hz",
                                  _FREQUENCY_MIN, _FREQUENCY_MAX),
        total_timeout_ms=int_field(data, "total_timeout_ms", "total_timeout_ms",
                                   minimum=0),
    )


# ---------------------------------------------------------------------------
# MonitorConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MonitorConfig:
    """02：目标监控配置；交叉约束见模块 docstring。"""

    target_id: str
    region: Optional[Region]
    display_id: Optional[str]
    binding_id: Optional[str]
    allow_exit: bool
    frequency_hz: float
    total_timeout_ms: int
    debug: bool
    repeat: bool
    wait_timeout_ms: int
    on_match: str
    action_script_id: Optional[str]
    action_args: Dict[str, Any]
    on_timeout: str
    timeout_script_id: Optional[str]
    timeout_args: Dict[str, Any]
    max_retries: int
    retry_delay_ms: int
    trigger: str
    cooldown_ms: int
    window_mode: str = "foreground"


def _validate_args_object(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        fail("INVALID_PARAMS", name, "%s 必须为对象" % name)
    return value


def validate_monitor_config(value: Any) -> MonitorConfig:
    data = require_object(value, "monitor_config")
    check_keys(
        data,
        ("target_id", "region", "display_id", "binding_id", "allow_exit",
         "frequency_hz", "total_timeout_ms", "debug", "repeat", "wait_timeout_ms",
         "on_match", "action_script_id", "action_args", "on_timeout",
         "timeout_script_id", "timeout_args", "max_retries", "retry_delay_ms",
         "trigger", "cooldown_ms", "window_mode"),
        "monitor_config",
    )
    on_match = enum_field(data, "on_match", "on_match", ON_MATCH_ACTIONS)
    on_timeout = enum_field(data, "on_timeout", "on_timeout", ON_TIMEOUT_ACTIONS)
    trigger = enum_field(data, "trigger", "trigger", TRIGGERS)

    # 02：on_match=stop → repeat 必须 false（交叉约束，定位 repeat）
    repeat = bool_field(data, "repeat", "repeat")
    if on_match == "stop" and repeat:
        fail("INVALID_PARAMS", "repeat", "on_match=stop 时 repeat 必须为 false")
    # on_match 互斥字段：run_script 必填 / stop 必 null
    action_script_id = _validate_optional_str(data, "action_script_id",
                                              "action_script_id")
    if on_match == "run_script" and action_script_id is None:
        fail("INVALID_PARAMS", "action_script_id",
             "on_match=run_script 时 action_script_id 必填")
    if on_match == "stop" and action_script_id is not None:
        fail("INVALID_PARAMS", "action_script_id",
             "on_match=stop 时 action_script_id 必须为 null")

    # on_timeout 分支结构（§15.7 裁决：结构缺失先于派生一致性——run_script 必填 /
    # stop|retry 必 null 先于 max_retries 交叉约束检查）
    timeout_script_id = _validate_optional_str(data, "timeout_script_id",
                                               "timeout_script_id")
    if on_timeout == "run_script" and timeout_script_id is None:
        fail("INVALID_PARAMS", "timeout_script_id",
             "on_timeout=run_script 时 timeout_script_id 必填")
    if on_timeout in ("stop", "retry") and timeout_script_id is not None:
        fail("INVALID_PARAMS", "timeout_script_id",
             "on_timeout=%s 时 timeout_script_id 必须为 null" % on_timeout)

    # 02：on_timeout=retry → max_retries 1..100；否则必须 0
    max_retries = int_field(data, "max_retries", "max_retries",
                            minimum=0, maximum=_MAX_RETRIES)
    if on_timeout == "retry" and max_retries < 1:
        fail("INVALID_PARAMS", "max_retries",
             "on_timeout=retry 时 max_retries 必须 ≥1")
    if on_timeout != "retry" and max_retries != 0:
        fail("INVALID_PARAMS", "max_retries",
             "on_timeout≠retry 时 max_retries 必须为 0")

    region, display_id, binding_id = _validate_scope_fields(data)
    return MonitorConfig(
        window_mode=data.get("window_mode", "foreground"),
        target_id=str_field(data, "target_id", "target_id"),
        region=region,
        display_id=display_id,
        binding_id=binding_id,
        allow_exit=bool_field(data, "allow_exit", "allow_exit"),
        frequency_hz=number_field(data, "frequency_hz", "frequency_hz",
                                  _FREQUENCY_MIN, _FREQUENCY_MAX),
        total_timeout_ms=int_field(data, "total_timeout_ms", "total_timeout_ms",
                                   minimum=0),
        debug=bool_field(data, "debug", "debug"),
        repeat=repeat,
        wait_timeout_ms=int_field(data, "wait_timeout_ms", "wait_timeout_ms",
                                  minimum=0),
        on_match=on_match,
        action_script_id=action_script_id,
        action_args=_validate_args_object(data, "action_args"),
        on_timeout=on_timeout,
        timeout_script_id=timeout_script_id,
        timeout_args=_validate_args_object(data, "timeout_args"),
        max_retries=max_retries,
        retry_delay_ms=int_field(data, "retry_delay_ms", "retry_delay_ms",
                                 minimum=0),
        trigger=trigger,
        cooldown_ms=int_field(data, "cooldown_ms", "cooldown_ms", minimum=0),
    )


# ---------------------------------------------------------------------------
# ExecutionState
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionState:
    """02：执行状态快照；status 八枚举；纯 monitor 时 script_id=null。"""

    execution_id: str
    kind: str
    status: str
    reason: Optional[str]
    iteration: int
    script_id: Optional[str]
    current_line: Optional[int]
    result: Any
    error: Optional[IpcError]
    started_at: str
    updated_at: str
    effective_hz: Optional[float]
    throttled: bool


def validate_execution_state(value: Any) -> ExecutionState:
    data = require_object(value, "execution_state")
    check_keys(
        data,
        ("execution_id", "kind", "status", "reason", "iteration", "script_id",
         "current_line", "result", "error", "started_at", "updated_at",
         "effective_hz", "throttled"),
        "execution_state",
    )
    error_value = data.get("error")
    if error_value is None:
        error: Optional[IpcError] = None
    else:
        error = validate_error_structure(error_value)
    reason = _validate_optional_str(data, "reason", "execution_state.reason")
    script_id = _validate_optional_str(data, "script_id",
                                       "execution_state.script_id")
    current_line = data.get("current_line")
    if current_line is not None and (
            isinstance(current_line, bool) or not isinstance(current_line, int)):
        fail("INVALID_PARAMS", "execution_state.current_line",
             "current_line 必须为整数或 null")
    effective_hz = data.get("effective_hz")
    if effective_hz is not None:
        if isinstance(effective_hz, bool) or not isinstance(effective_hz, (int, float)):
            fail("INVALID_PARAMS", "execution_state.effective_hz",
                 "effective_hz 必须为数值或 null")
        effective_hz = float(effective_hz)
    return ExecutionState(
        execution_id=str_field(data, "execution_id", "execution_state.execution_id"),
        kind=enum_field(data, "kind", "execution_state.kind", EXECUTION_KINDS),
        status=enum_field(data, "status", "execution_state.status",
                          EXECUTION_STATUSES),
        reason=reason,
        iteration=int_field(data, "iteration", "execution_state.iteration",
                            minimum=0),
        script_id=script_id,
        current_line=current_line,
        result=data.get("result"),
        error=error,
        started_at=validate_rfc3339_utc(data.get("started_at"),
                                        "execution_state.started_at"),
        updated_at=validate_rfc3339_utc(data.get("updated_at"),
                                        "execution_state.updated_at"),
        effective_hz=effective_hz,
        throttled=bool_field(data, "throttled", "execution_state.throttled"),
    )
