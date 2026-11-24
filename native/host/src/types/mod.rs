//! 共享类型与纯校验函数（TASK-006 设计 §3/§6；Rust 侧落点）。
//!
//! 设计要点（§2.1/§2.2）：
//! - 手写类型 + `schema/types.schema.json`/`schema/config.schema.json` 单一事实源
//!   + `schema/contract-cases.json` 三端同源用例（`type_validation`/`config_patch`）；
//! - 校验全部为**纯函数、无 IO**（配置 IO 归 Python `backend/config/`，Rust 仅
//!   路由透传——§2.3）；错误定位到字段路径（`region.x`/`recognition.foo` 等），
//!   与 06 错误码注册表（INVALID_PARAMS/INVALID_REGION/CONFLICT）逐字对齐；
//! - snake_case 三端统一，禁止同义字段（Match 用 bbox/center、Region 用 space）。

pub mod capabilities;
pub mod conditions;
pub mod config;
pub mod region;
pub mod runtime;

#[cfg(test)]
mod contract_tests;

pub use capabilities::{
    is_valid_capability_id, is_valid_error_code, Capability, ProviderManifest, ModelManifest,
    CAPABILITY_IDS, ERROR_CODES,
};
pub use config::{
    apply_config_patch, config_limits, default_config_document, doc_value_at, PatchError,
};
pub use conditions::{
    validate_group_fields, validate_script, validate_target, ConditionNode,
};
pub use region::{
    validate_local_rect, validate_point, validate_region, DisplayInfo, Frame, ImageAsset, ImageRef,
    LocalRect, Point, Rect, Region, RegionSpace,
};
pub use runtime::{
    validate_execution_state, validate_monitor_config, validate_run_config, ExecutionState,
    ExecutionStatus,
};

use serde_json::Value;

/// 类型层校验错误（TASK-006 §6/§8）：06 错误码 + 字段路径定位。
///
/// `field` 为完整点分路径（如 `region.x`、`conditions.params.threshold`、
/// `recognition.default_frequency_hz`）；与 schema/contract-cases.json 用例的
/// `expect.field` 逐字对齐。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TypeError {
    /// 06 错误码（类型层只产生 INVALID_PARAMS / INVALID_REGION / CONFLICT）。
    pub code: &'static str,
    /// 字段路径（无条件定位；树级约束用 `conditions` 等父路径）。
    pub field: String,
}

impl TypeError {
    /// INVALID_PARAMS（参数类型/范围/枚举/未知字段错误）。
    pub fn invalid_params(field: impl Into<String>) -> Self {
        TypeError { code: "INVALID_PARAMS", field: field.into() }
    }

    /// INVALID_REGION（区域尺寸"太小"分支，06）。
    pub fn invalid_region(field: impl Into<String>) -> Self {
        TypeError { code: "INVALID_REGION", field: field.into() }
    }
}

/// JSON 数值 → i64（严格整数：拒绝 bool 与浮点；schema "type":"integer" 语义）。
pub(crate) fn as_i64(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => {
            if n.is_i64() {
                n.as_i64()
            } else if n.is_u64() {
                // u64 溢出 i64 的极端值按越界处理（类型层无 >i64::MAX 合法样本）
                n.as_u64().and_then(|u| i64::try_from(u).ok())
            } else {
                None
            }
        }
        _ => None,
    }
}

/// JSON 数值 → f64（拒绝 bool；NaN/Infinity 由 serde_json 解析层天然拒绝，
/// 此处对手工构造的 Value 双重保险）。
pub(crate) fn as_f64(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64().filter(|f| f.is_finite()),
        _ => None,
    }
}

/// 非空字符串提取。
pub(crate) fn non_empty_str<'a>(v: &'a Value) -> Option<&'a str> {
    v.as_str().filter(|s| !s.is_empty())
}

/// sha256 十六进制串校验（64 位 hex，schema pattern 语义）。
pub(crate) fn is_sha256_hex(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

/// 路径组件判断（02/07 §6：拒绝 `..` 组件、绝对路径、盘符——**组件判断，
/// 非 startswith**）。`segments` 为按 `/`（或 `\`）切分后的组件序列。
pub(crate) fn path_components_escaped(segments: &[&str]) -> bool {
    segments.iter().any(|s| *s == ".." || *s == ".")
}

/// 数据根相对路径校验（config.model_directory / ImageAsset.path 共用口径）：
/// 拒绝空串、绝对路径（`/`、`\` 开头）、盘符（`X:` 前缀）、任意 `..`/`.` 组件。
pub(crate) fn is_relative_data_root_path(s: &str) -> bool {
    if s.is_empty() {
        return false;
    }
    if s.starts_with('/') || s.starts_with('\\') {
        return false;
    }
    // 盘符：首组件含 ':'（如 C:/models）
    let normalized = s.replace('\\', "/");
    if normalized.split('/').next().is_some_and(|first| first.contains(':')) {
        return false;
    }
    let segments: Vec<&str> = normalized.split('/').collect();
    !path_components_escaped(&segments)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn as_i64_rejects_bool_and_float() {
        assert_eq!(as_i64(&json!(3)), Some(3));
        assert_eq!(as_i64(&json!(-3)), Some(-3));
        assert_eq!(as_i64(&json!(true)), None, "bool 不得冒充整数");
        assert_eq!(as_i64(&json!(1.5)), None, "浮点不是 integer");
        assert_eq!(as_i64(&json!("3")), None);
    }

    #[test]
    fn as_f64_rejects_non_finite_and_bool() {
        assert_eq!(as_f64(&json!(0.85)), Some(0.85));
        assert_eq!(as_f64(&json!(1)), Some(1.0));
        assert_eq!(as_f64(&json!(false)), None);
    }

    #[test]
    fn relative_path_component_checks() {
        assert!(is_relative_data_root_path("models"));
        assert!(is_relative_data_root_path("images/example.png"));
        assert!(!is_relative_data_root_path("../models"), ".. 组件拒绝");
        assert!(!is_relative_data_root_path("models/../x"), "中部 .. 拒绝");
        assert!(!is_relative_data_root_path("/abs"), "绝对路径拒绝");
        assert!(!is_relative_data_root_path("C:/models"), "盘符拒绝");
        assert!(!is_relative_data_root_path("C:\\models"), "盘符（反斜杠）拒绝");
        assert!(!is_relative_data_root_path(""), "空串拒绝");
        // 组件判断而非 startswith：嵌套合法相对路径不误伤
        assert!(is_relative_data_root_path("a/b/c"));
    }

    #[test]
    fn sha256_and_type_error_fields() {
        assert!(is_sha256_hex(&"a".repeat(64)));
        assert!(!is_sha256_hex(&"a".repeat(63)));
        assert!(!is_sha256_hex(&"g".repeat(64)), "非 hex 拒绝");
        let e = TypeError::invalid_params("region.x");
        assert_eq!(e.code, "INVALID_PARAMS");
        assert_eq!(e.field, "region.x");
        assert_eq!(TypeError::invalid_region("region.width").code, "INVALID_REGION");
    }
}
