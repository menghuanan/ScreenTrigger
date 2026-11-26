//! 运行/监控/执行状态类型与校验（TASK-006 设计 §3.2/§6.2；02 权威逐字段）。
//!
//! 校验纯函数（无 IO）：`validate_run_config`/`validate_monitor_config`/
//! `validate_execution_state`。交叉约束（§6.2）：
//! - mode 四枚举与 iterations/stop_target_id 条件必填/必 null；
//! - display_id/binding_id 互斥（未绑定必填 display_id、绑定时 display=null）；
//! - region 空间与绑定一致性；**image_pixel region 作为运行/监控范围 → 拒绝
//!   INVALID_PARAMS{field:"region.space"}**（架构师裁决口径，与 Python 一致）；
//! - MonitorConfig：on_match=stop→repeat 必须 false；on_timeout=retry→max_retries
//!   ≥1（否则 0）；run_script 分支 script_id 必填。
//! 错误字段路径与 contract-cases type_validation 用例逐字对齐。

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::region::{validate_region, RegionSpace};
use super::{as_f64, as_i64, non_empty_str, TypeError};

/// ExecutionState.status 八枚举（02 权威；终态不转回 running）。
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionStatus {
    Starting,
    Running,
    Paused,
    DebugPaused,
    Stopping,
    Completed,
    Cancelled,
    Failed,
}

impl ExecutionStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            ExecutionStatus::Starting => "starting",
            ExecutionStatus::Running => "running",
            ExecutionStatus::Paused => "paused",
            ExecutionStatus::DebugPaused => "debug_paused",
            ExecutionStatus::Stopping => "stopping",
            ExecutionStatus::Completed => "completed",
            ExecutionStatus::Cancelled => "cancelled",
            ExecutionStatus::Failed => "failed",
        }
    }

    fn from_str(s: &str) -> Option<Self> {
        Some(match s {
            "starting" => ExecutionStatus::Starting,
            "running" => ExecutionStatus::Running,
            "paused" => ExecutionStatus::Paused,
            "debug_paused" => ExecutionStatus::DebugPaused,
            "stopping" => ExecutionStatus::Stopping,
            "completed" => ExecutionStatus::Completed,
            "cancelled" => ExecutionStatus::Cancelled,
            "failed" => ExecutionStatus::Failed,
            _ => return None,
        })
    }
}

/// ExecutionState（02）：纯 monitor 时 script_id=null；result 只保留最后一次。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ExecutionState {
    pub execution_id: String,
    /// "script" | "monitor"
    pub kind: String,
    pub status: ExecutionStatus,
    pub reason: Option<String>,
    pub iteration: i64,
    pub script_id: Option<String>,
    pub current_line: Option<i64>,
    pub result: Option<Value>,
    pub error: Option<Value>,
    pub started_at: String,
    pub updated_at: String,
    pub effective_hz: Option<f64>,
    pub throttled: bool,
}

/// RunConfig / MonitorConfig 共用：display_id 与 binding_id 互斥
/// （§3.2：未绑定时 display_id 必填；绑定时 display_id 必须 null）。
/// 双设 → `binding_id`（用例口径）；其余违例报 `display_id`。
fn validate_display_binding(doc: &serde_json::Map<String, Value>) -> Result<(), TypeError> {
    let display = doc.get("display_id").unwrap_or(&Value::Null);
    let binding = doc.get("binding_id").unwrap_or(&Value::Null);
    let display_set = non_empty_str(display).is_some();
    let binding_set = non_empty_str(binding).is_some();
    if let Some(mode) = doc.get("window_mode") {
        if mode.as_str() != Some("foreground") && !(mode.as_str() == Some("background") && binding_set) {
            return Err(TypeError::invalid_params("window_mode"));
        }
    }
    if display_set && binding_set {
        return Err(TypeError::invalid_params("binding_id"));
    }
    if !binding_set && !display_set {
        return Err(TypeError::invalid_params("display_id"));
    }
    if binding_set && !display.is_null() {
        return Err(TypeError::invalid_params("display_id"));
    }
    Ok(())
}

/// RunConfig / MonitorConfig 共用：frequency 0.1–30、total_timeout_ms ≥0。
fn validate_frequency_and_timeout(doc: &serde_json::Map<String, Value>) -> Result<(), TypeError> {
    let freq = as_f64(doc.get("frequency_hz").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("frequency_hz"))?;
    if !(0.1..=30.0).contains(&freq) {
        return Err(TypeError::invalid_params("frequency_hz"));
    }
    let timeout = as_i64(doc.get("total_timeout_ms").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("total_timeout_ms"))?;
    if timeout < 0 {
        return Err(TypeError::invalid_params("total_timeout_ms"));
    }
    Ok(())
}

/// RunConfig / MonitorConfig 共用：region 校验 + 空间/绑定一致性 +
/// image_pixel 作为运行范围拒绝（架构师裁决，随 B5 如有变对齐）。
fn validate_scope_region(doc: &serde_json::Map<String, Value>) -> Result<(), TypeError> {
    let region = doc.get("region").unwrap_or(&Value::Null);
    if region.is_null() {
        return Ok(());
    }
    validate_region(region)?;
    let space = region.get("space").and_then(Value::as_str).unwrap_or("");
    if space == RegionSpace::ImagePixel.as_str() {
        // 运行/监控范围不接受图像像素空间（识别测试 job 专用）
        return Err(TypeError::invalid_params("region.space"));
    }
    let display_set = non_empty_str(doc.get("display_id").unwrap_or(&Value::Null)).is_some();
    let binding_set = non_empty_str(doc.get("binding_id").unwrap_or(&Value::Null)).is_some();
    let consistent = match space {
        "screen_physical" => display_set && !binding_set,
        "window_client_physical" => binding_set && !display_set,
        _ => false,
    };
    if !consistent {
        return Err(TypeError::invalid_params("region.space"));
    }
    Ok(())
}

/// RunConfig 校验（§3.2：mode 互斥字段条件必填/必 null + scope 交叉约束）。
pub fn validate_run_config(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("run_config"))?;
    for key in [
        "script_id",
        "args",
        "mode",
        "iterations",
        "stop_target_id",
        "debug",
        "region",
        "display_id",
        "binding_id",
        "allow_exit",
        "frequency_hz",
        "total_timeout_ms",
    ] {
        if !obj.contains_key(key) {
            return Err(TypeError::invalid_params(key));
        }
    }
    non_empty_str(obj.get("script_id").unwrap())
        .ok_or_else(|| TypeError::invalid_params("script_id"))?;
    obj.get("args")
        .unwrap()
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("args"))?;
    obj.get("debug")
        .unwrap()
        .as_bool()
        .ok_or_else(|| TypeError::invalid_params("debug"))?;
    obj.get("allow_exit")
        .unwrap()
        .as_bool()
        .ok_or_else(|| TypeError::invalid_params("allow_exit"))?;
    let mode = obj
        .get("mode")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("mode"))?;
    if !matches!(mode, "once" | "count" | "forever" | "until_target") {
        return Err(TypeError::invalid_params("mode"));
    }
    // mode 互斥字段：count → iterations ≥1；其余 → 必须 null
    let iterations = obj.get("iterations").unwrap();
    if mode == "count" {
        let it = as_i64(iterations).ok_or_else(|| TypeError::invalid_params("iterations"))?;
        if it < 1 {
            return Err(TypeError::invalid_params("iterations"));
        }
    } else if !iterations.is_null() {
        return Err(TypeError::invalid_params("iterations"));
    }
    // stop_target_id：仅 until_target 非空
    let stop = obj.get("stop_target_id").unwrap();
    if mode == "until_target" {
        non_empty_str(stop)
            .ok_or_else(|| TypeError::invalid_params("stop_target_id"))?;
    } else if !stop.is_null() {
        return Err(TypeError::invalid_params("stop_target_id"));
    }
    validate_display_binding(obj)?;
    validate_frequency_and_timeout(obj)?;
    validate_scope_region(obj)?;
    Ok(())
}

/// MonitorConfig 校验（§3.2：on_match/on_timeout 交叉约束 + scope 交叉约束）。
pub fn validate_monitor_config(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("monitor_config"))?;
    for key in [
        "target_id",
        "region",
        "display_id",
        "binding_id",
        "allow_exit",
        "frequency_hz",
        "total_timeout_ms",
        "debug",
        "repeat",
        "wait_timeout_ms",
        "on_match",
        "action_script_id",
        "action_args",
        "on_timeout",
        "timeout_script_id",
        "timeout_args",
        "max_retries",
        "retry_delay_ms",
        "trigger",
        "cooldown_ms",
    ] {
        if !obj.contains_key(key) {
            return Err(TypeError::invalid_params(key));
        }
    }
    non_empty_str(obj.get("target_id").unwrap())
        .ok_or_else(|| TypeError::invalid_params("target_id"))?;
    for key in ["debug", "allow_exit", "repeat"] {
        obj.get(key)
            .unwrap()
            .as_bool()
            .ok_or_else(|| TypeError::invalid_params(key))?;
    }
    obj.get("action_args")
        .unwrap()
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("action_args"))?;
    obj.get("timeout_args")
        .unwrap()
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("timeout_args"))?;
    let wait = as_i64(obj.get("wait_timeout_ms").unwrap())
        .ok_or_else(|| TypeError::invalid_params("wait_timeout_ms"))?;
    if wait < 0 {
        return Err(TypeError::invalid_params("wait_timeout_ms"));
    }
    let retry_delay = as_i64(obj.get("retry_delay_ms").unwrap())
        .ok_or_else(|| TypeError::invalid_params("retry_delay_ms"))?;
    if retry_delay < 0 {
        return Err(TypeError::invalid_params("retry_delay_ms"));
    }
    let cooldown = as_i64(obj.get("cooldown_ms").unwrap())
        .ok_or_else(|| TypeError::invalid_params("cooldown_ms"))?;
    if cooldown < 0 {
        return Err(TypeError::invalid_params("cooldown_ms"));
    }
    let max_retries = as_i64(obj.get("max_retries").unwrap())
        .ok_or_else(|| TypeError::invalid_params("max_retries"))?;
    // 全局范围 0–100（02；on_timeout 分支的 ≥1 约束在其后叠加）
    if !(0..=100).contains(&max_retries) {
        return Err(TypeError::invalid_params("max_retries"));
    }
    // on_match 交叉约束
    let on_match = obj
        .get("on_match")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("on_match"))?;
    if !matches!(on_match, "run_script" | "stop") {
        return Err(TypeError::invalid_params("on_match"));
    }
    let action_script_id = obj.get("action_script_id").unwrap();
    if on_match == "stop" {
        // 02：on_match=stop → repeat 必须 false；action_script_id 必须 null
        if obj.get("repeat").unwrap().as_bool() == Some(true) {
            return Err(TypeError::invalid_params("repeat"));
        }
        if !action_script_id.is_null() {
            return Err(TypeError::invalid_params("action_script_id"));
        }
    } else {
        // run_script → action_script_id 必填非空
        non_empty_str(action_script_id)
            .ok_or_else(|| TypeError::invalid_params("action_script_id"))?;
    }
    // on_timeout 交叉约束
    let on_timeout = obj
        .get("on_timeout")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("on_timeout"))?;
    if !matches!(on_timeout, "stop" | "retry" | "run_script") {
        return Err(TypeError::invalid_params("on_timeout"));
    }
    let timeout_script_id = obj.get("timeout_script_id").unwrap();
    match on_timeout {
        "retry" => {
            // 02：retry → max_retries ≥1（0 违例，用例口径）
            if max_retries < 1 {
                return Err(TypeError::invalid_params("max_retries"));
            }
            if !timeout_script_id.is_null() {
                return Err(TypeError::invalid_params("timeout_script_id"));
            }
        }
        "stop" => {
            if max_retries != 0 {
                return Err(TypeError::invalid_params("max_retries"));
            }
            if !timeout_script_id.is_null() {
                return Err(TypeError::invalid_params("timeout_script_id"));
            }
        }
        _ => {
            // run_script → timeout_script_id 必填非空；max_retries 必须 0
            non_empty_str(timeout_script_id)
                .ok_or_else(|| TypeError::invalid_params("timeout_script_id"))?;
            if max_retries != 0 {
                return Err(TypeError::invalid_params("max_retries"));
            }
        }
    }
    let trigger = obj
        .get("trigger")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("trigger"))?;
    if !matches!(trigger, "rising_edge" | "every_match") {
        return Err(TypeError::invalid_params("trigger"));
    }
    validate_display_binding(obj)?;
    validate_frequency_and_timeout(obj)?;
    validate_scope_region(obj)?;
    Ok(())
}

/// ExecutionState 校验（status 八枚举；kind 两枚举；终态字段可 null）。
pub fn validate_execution_state(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("execution_state"))?;
    for key in [
        "execution_id",
        "kind",
        "status",
        "reason",
        "iteration",
        "script_id",
        "current_line",
        "result",
        "error",
        "started_at",
        "updated_at",
        "effective_hz",
        "throttled",
    ] {
        if !obj.contains_key(key) {
            return Err(TypeError::invalid_params(key));
        }
    }
    non_empty_str(obj.get("execution_id").unwrap())
        .ok_or_else(|| TypeError::invalid_params("execution_id"))?;
    let kind = obj
        .get("kind")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("kind"))?;
    if kind != "script" && kind != "monitor" {
        return Err(TypeError::invalid_params("kind"));
    }
    let status = obj
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("status"))?;
    if ExecutionStatus::from_str(status).is_none() {
        return Err(TypeError::invalid_params("status"));
    }
    let iteration = as_i64(obj.get("iteration").unwrap())
        .ok_or_else(|| TypeError::invalid_params("iteration"))?;
    if iteration < 0 {
        return Err(TypeError::invalid_params("iteration"));
    }
    obj.get("throttled")
        .unwrap()
        .as_bool()
        .ok_or_else(|| TypeError::invalid_params("throttled"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn base_run(mode: &str, iterations: Value, stop: Value) -> Value {
        json!({
            "script_id": "s1", "args": {}, "mode": mode,
            "iterations": iterations, "stop_target_id": stop,
            "debug": false, "region": null,
            "display_id": "display-1", "binding_id": null,
            "allow_exit": false, "frequency_hz": 1, "total_timeout_ms": 0
        })
    }

    fn base_monitor() -> Value {
        json!({
            "target_id": "t1", "region": null,
            "display_id": "display-1", "binding_id": null,
            "allow_exit": false, "frequency_hz": 1, "total_timeout_ms": 0,
            "debug": false, "repeat": false, "wait_timeout_ms": 30000,
            "on_match": "stop", "action_script_id": null, "action_args": {},
            "on_timeout": "stop", "timeout_script_id": null, "timeout_args": {},
            "max_retries": 0, "retry_delay_ms": 1000,
            "trigger": "rising_edge", "cooldown_ms": 1000
        })
    }

    #[test]
    fn image_pixel_region_rejected_as_run_scope() {
        // 团队口径（架构师裁决中）：image_pixel region 作为运行范围 → INVALID_PARAMS
        // field=region.space，与 Python 一致；如有变随 B5 对齐
        // （display_id 保持设置以满足"未绑定必填 display_id"，使失败定位在 region.space）
        let mut rc = base_run("once", Value::Null, Value::Null);
        rc["region"] = json!({
            "space": "image_pixel", "x": 0, "y": 0, "width": 100, "height": 40,
            "image_id": "img-1"
        });
        let err = validate_run_config(&rc).unwrap_err();
        assert_eq!((err.code, err.field.as_str()), ("INVALID_PARAMS", "region.space"));
    }

    #[test]
    fn region_space_binding_consistency_locally() {
        // 无共享用例路径（本地断言）：screen region + 绑定 scope → 空间不一致拒绝
        let mut rc = base_run("once", Value::Null, Value::Null);
        rc["region"] = json!({
            "space": "screen_physical", "x": -900, "y": 0, "width": 120, "height": 40,
            "display_id": "display-1"
        });
        rc["binding_id"] = json!("binding-1");
        rc["display_id"] = Value::Null;
        let err = validate_run_config(&rc).unwrap_err();
        assert_eq!(err.field, "region.space");
        // client region + binding scope 一致 → 通过
        rc["region"] = json!({
            "space": "window_client_physical", "x": 0, "y": 0, "width": 120, "height": 40,
            "binding_id": "binding-1"
        });
        assert!(validate_run_config(&rc).is_ok());
    }

    #[test]
    fn runconfig_until_target_requires_stop_target_locally() {
        // 无共享用例路径（本地断言）：until_target 缺 stop_target_id → stop_target_id
        let rc = base_run("until_target", Value::Null, Value::Null);
        let err = validate_run_config(&rc).unwrap_err();
        assert_eq!(err.field, "stop_target_id");
        // 合法 until_target
        let ok = base_run("until_target", Value::Null, json!("t1"));
        assert!(validate_run_config(&ok).is_ok());
    }

    #[test]
    fn monitor_run_script_branch_requires_script_ids_locally() {
        // 无共享用例路径（本地断言）：on_match=run_script 缺 action_script_id
        let mut m = base_monitor();
        m["on_match"] = json!("run_script");
        let err = validate_monitor_config(&m).unwrap_err();
        assert_eq!(err.field, "action_script_id");
        // on_timeout=run_script 缺 timeout_script_id → 先报 script_id（必填先于
        // max_retries 清零校验，02 allOf 分支内 required 优先）
        m["action_script_id"] = json!("s1");
        m["on_timeout"] = json!("run_script");
        m["max_retries"] = json!(3);
        let err2 = validate_monitor_config(&m).unwrap_err();
        assert_eq!(err2.field, "timeout_script_id");
        // script_id 补齐后 max_retries≠0 仍拒绝
        m["timeout_script_id"] = json!("s2");
        let err3 = validate_monitor_config(&m).unwrap_err();
        assert_eq!(err3.field, "max_retries");
        // 完整合法
        m["max_retries"] = json!(0);
        assert!(validate_monitor_config(&m).is_ok());
    }

    #[test]
    fn execution_state_eight_status_enum() {
        // 八枚举逐一接受（02 权威）；未知 status 拒绝
        for s in [
            "starting", "running", "paused", "debug_paused", "stopping", "completed",
            "cancelled", "failed",
        ] {
            let st = json!({
                "execution_id": "e1", "kind": "script", "status": s, "reason": null,
                "iteration": 0, "script_id": "s1", "current_line": null, "result": null,
                "error": null, "started_at": "2026-09-21T09:00:00Z",
                "updated_at": "2026-09-21T09:00:00Z", "effective_hz": null, "throttled": false
            });
            assert!(validate_execution_state(&st).is_ok(), "{s} 应合法");
        }
        let bad = json!({
            "execution_id": "e1", "kind": "script", "status": "zombie", "reason": null,
            "iteration": 0, "script_id": null, "current_line": null, "result": null,
            "error": null, "started_at": "2026-09-21T09:00:00Z",
            "updated_at": "2026-09-21T09:00:00Z", "effective_hz": null, "throttled": false
        });
        let err = validate_execution_state(&bad).unwrap_err();
        assert_eq!(err.field, "status");
    }

    #[test]
    fn serde_snake_case_roundtrip_status() {
        let st: ExecutionStatus = serde_json::from_str("\"debug_paused\"").unwrap();
        assert_eq!(st, ExecutionStatus::DebugPaused);
        assert_eq!(serde_json::to_string(&st).unwrap(), "\"debug_paused\"");
    }
}
