//! 能力/错误码/模型 manifest 类型（TASK-006 设计 §3.3/§7.1；06/00 权威）。
//!
//! 常量与 schema/types.schema.json 同源（capability 枚举、errorCode 40 码全量）；
//! 类型冻结面沿用 TASK-005（Capability = {id,state,reason}）。骨架期
//! hello.model_manifests = []（类型就位，Provider 实现属识别任务）。

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::TypeError;

/// 能力 id 枚举（§7.1 冻结面；schema/types.schema.json capability.id 同源）。
pub const CAPABILITY_IDS: [&str; 7] = [
    "ocr",
    "lua_script",
    "template_match",
    "contour_match",
    "color_match",
    "input",
    "capture",
];

/// 能力 state 三枚举（TASK-005 冻结）。
pub const CAPABILITY_STATES: [&str; 3] = ["available", "pending", "unavailable"];

/// 06 错误码注册表全量 40 码（schema/types.schema.json errorCode 枚举同源）。
#[allow(clippy::enum_variant_names)]
pub const ERROR_CODES: [&str; 40] = [
    "INVALID_PARAMS",
    "INVALID_STATE",
    "BUSY",
    "CONFLICT",
    "IN_USE",
    "NOT_FOUND",
    "PROTOCOL_ERROR",
    "UNKNOWN_COMMAND",
    "DUPLICATE_EXPIRED",
    "ENGINE_UNAVAILABLE",
    "INTERNAL_ERROR",
    "DISPLAY_NOT_FOUND",
    "INVALID_REGION",
    "INVALID_IMAGE",
    "IMAGE_EXPIRED",
    "CAPTURE_FAILED",
    "TARGET_NOT_FOUND",
    "GROUP_NOT_FOUND",
    "MODEL_UNAVAILABLE",
    "LANGUAGE_UNSUPPORTED",
    "RECOGNITION_TIMEOUT",
    "RECOGNITION_FAILED",
    "SCRIPT_NOT_FOUND",
    "SCRIPT_SYNTAX_ERROR",
    "SCRIPT_RUNTIME_ERROR",
    "SCRIPT_TIMEOUT",
    "WAIT_TIMEOUT",
    "CALL_DEPTH_EXCEEDED",
    "SERIALIZATION_ERROR",
    "CAPABILITY_DENIED",
    "INPUT_FAILED",
    "REGION_LIMIT_VIOLATION",
    "STALE_FRAME",
    "WINDOW_NOT_FOUND",
    "WINDOW_UNAVAILABLE",
    "HOTKEY_CONFLICT",
    "STORAGE_ERROR",
    "DATA_CORRUPT",
    "SCHEMA_UNSUPPORTED",
    "RESOURCE_LIMIT",
];

/// 合法能力 id。
pub fn is_valid_capability_id(id: &str) -> bool {
    CAPABILITY_IDS.contains(&id)
}

/// 合法 06 错误码。
pub fn is_valid_error_code(code: &str) -> bool {
    ERROR_CODES.contains(&code)
}

/// Capability（TASK-005 冻结 {id,state,reason}；reason 可 null）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct Capability {
    pub id: String,
    pub state: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

/// 06 错误结构（协议伴生；IpcError 的数据视图）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct IpcErrorObj {
    pub code: String,
    pub message: String,
    pub details: Value,
    pub retryable: bool,
}

/// Provider manifest（00 接口段字段表；TASK-006 只定义类型）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ProviderManifest {
    pub provider_id: String,
    pub api_version: i64,
    pub provider_version: String,
    pub supported_condition_types: Vec<String>,
    pub model_ids: Vec<String>,
    pub supported_languages: Vec<String>,
    pub thread_safe: bool,
    pub offline: bool,
}

/// Model manifest（00：provider_manifest 全字段 + license/source/sha256/
/// supported_charsets）；骨架期 hello.model_manifests=[]。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ModelManifest {
    pub provider_id: String,
    pub api_version: i64,
    pub provider_version: String,
    pub supported_condition_types: Vec<String>,
    pub model_ids: Vec<String>,
    pub supported_languages: Vec<String>,
    pub thread_safe: bool,
    pub offline: bool,
    pub license: String,
    pub source: String,
    pub sha256: String,
    pub supported_charsets: Vec<String>,
}

/// Capability 纯校验（id/state 枚举；reason string|null）。
pub fn validate_capability(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("capability"))?;
    let id = obj
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("capability.id"))?;
    if !is_valid_capability_id(id) {
        return Err(TypeError::invalid_params("capability.id"));
    }
    let state = obj
        .get("state")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("capability.state"))?;
    if !CAPABILITY_STATES.contains(&state) {
        return Err(TypeError::invalid_params("capability.state"));
    }
    if let Some(reason) = obj.get("reason") {
        if !reason.is_null() && reason.as_str().is_none() {
            return Err(TypeError::invalid_params("capability.reason"));
        }
    } else {
        return Err(TypeError::invalid_params("capability.reason"));
    }
    Ok(())
}

/// 错误结构纯校验（code ∈ 06 注册表；details 对象；retryable bool）。
pub fn validate_error_structure(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("error"))?;
    let code = obj
        .get("code")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("error.code"))?;
    if !is_valid_error_code(code) {
        return Err(TypeError::invalid_params("error.code"));
    }
    obj.get("message")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("error.message"))?;
    obj.get("details")
        .and_then(Value::as_object)
        .ok_or_else(|| TypeError::invalid_params("error.details"))?;
    obj.get("retryable")
        .and_then(Value::as_bool)
        .ok_or_else(|| TypeError::invalid_params("error.retryable"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn error_code_table_is_forty_and_matches_schema_shape() {
        // 40 码全量（schema errorCode 枚举逐一存在；常量列表无重复）
        assert_eq!(ERROR_CODES.len(), 40);
        let mut sorted = ERROR_CODES.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(sorted.len(), 40, "错误码常量不得重复");
        for code in ERROR_CODES {
            assert!(is_valid_error_code(code));
        }
        assert!(!is_valid_error_code("NOT_AN_CODE"));
    }

    #[test]
    fn capability_ids_and_states_frozen() {
        assert_eq!(CAPABILITY_IDS.len(), 7);
        assert!(is_valid_capability_id("lua_script"));
        assert!(!is_valid_capability_id("hotkey"));
        let cap = json!({"id": "ocr", "state": "unavailable", "reason": "骨架未接入"});
        assert!(validate_capability(&cap).is_ok());
        let bad = json!({"id": "nope", "state": "available", "reason": null});
        assert_eq!(validate_capability(&bad).unwrap_err().field, "capability.id");
        let bad_state = json!({"id": "ocr", "state": "disabled", "reason": null});
        assert_eq!(validate_capability(&bad_state).unwrap_err().field, "capability.state");
    }

    #[test]
    fn model_manifest_roundtrip_with_charsets() {
        // supported_charsets 字段名（架构师将同步 02；三端统一此名）
        let m: ModelManifest = serde_json::from_str(
            r#"{
                "provider_id": "p1", "api_version": 1, "provider_version": "0.1.0",
                "supported_condition_types": ["ocr"], "model_ids": ["m1"],
                "supported_languages": ["zh", "en"], "thread_safe": true, "offline": true,
                "license": "Apache-2.0", "source": "models/m1",
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "supported_charsets": ["utf-8"]
            }"#,
        )
        .unwrap();
        assert_eq!(m.supported_charsets, vec!["utf-8".to_string()]);
        let s = serde_json::to_string(&m).unwrap();
        assert!(s.contains("supported_charsets"), "字段名必须 snake_case 原样");
    }
}
