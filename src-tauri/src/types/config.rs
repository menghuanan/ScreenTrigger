//! config 文档视图与 patch 纯校验（TASK-006 设计 §5/§6.3；Rust 侧无 IO）。
//!
//! 配置数据权威在 Python `backend/config/`（§2.3：Rust 仅路由透传）；本模块只
//! 交付契约测试所需的**纯函数**：`apply_config_patch`（深合并 + configLimits
//! 范围 + 未知字段拒绝 + expected_revision 冲突，与 §5.4 算法语义一致）与
//! `default_config_document`（02 默认表字面量，contract 测试与 schema 比对防漂移）。
//! 常量函数 `config_limits()` 与 schema/config.schema.json `configLimits` /
//! contract-cases thresholds.configLimits 三方运行时比对（contract_tests）。

use serde_json::{json, Value};

use super::is_relative_data_root_path;

/// configLimits（三端共享范围常量；与 schema/config.schema.json 逐字一致）。
/// `json!` 非常量可调用，故以 OnceLock 静态初始化（首次访问后不变）。
pub fn config_limits() -> &'static Value {
    static LIMITS: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    LIMITS.get_or_init(|| json!({
        "defaultFrequencyHz": {"min": 0.1, "max": 30},
        "threshold": {"min": 0, "max": 1},
        "ocrMinConfidence": {"min": 0, "max": 1},
        "pngCompressionLevel": {"min": 0, "max": 9},
        "previewHz": {"min": 1, "max": 20},
        "moveDurationMs": {"min": 0, "max": 5000},
        "typeIntervalMs": {"min": 0, "max": 5000},
        "totalTimeoutMs": {"min": 0},
        "rotateBytesMin": 1048576,
        "rotateBytesMax": 104857600,
        "fileCount": {"min": 1, "max": 20},
        "themes": ["system", "light", "dark"],
        "loggingLevels": ["debug", "info", "warning", "error"],
        "languages": ["zh-CN"]
    }))
}

/// patch 应用错误（config_patch 用例口径：CONFLICT 无字段定位；
/// INVALID_PARAMS 定位完整点分路径）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PatchError {
    pub code: &'static str,
    pub field: Option<String>,
}

impl PatchError {
    fn params(field: impl Into<String>) -> Self {
        PatchError { code: "INVALID_PARAMS", field: Some(field.into()) }
    }

    fn conflict() -> Self {
        PatchError { code: "CONFLICT", field: None }
    }
}

/// 02 默认配置文档（§5.1：逐字段照 config.schema.json default / 02 示例；
/// revision 初始 1）。reset_config 内容来源；contract 测试与 schema 比对。
pub fn default_config_document() -> Value {
    json!({
        "schema_version": 2,
        "revision": 1,
        "general": {
            "language": "zh-CN",
            "theme": "system"
        },
        "recognition": {
            "default_frequency_hz": 1,
            "default_threshold": 0.85,
            "rgb_tolerance": {"r": 15, "g": 15, "b": 15},
            "hsv_tolerance": {"h": 10, "s": 20, "v": 20},
            "ocr_model_id": null,
            "ocr_min_confidence": 0.7,
            "model_directory": "models"
        },
        "capture": {
            "preview_hz": 10,
            "png_compression_level": 6
        },
        "input": {
            "allow_exit": false,
            "move_duration_ms": 200,
            "type_interval_ms": 50
        },
        "execution": {
            "total_timeout_ms": 0
        },
        "hotkeys": {
            "start_selection": "Ctrl+Alt+S",
            "stop_script": "Ctrl+Alt+X",
            "pause_script": "Ctrl+Alt+P"
        },
        "logging": {
            "level": "info",
            "rotate_bytes": 10_485_760i64,
            "file_count": 5
        }
    })
}

/// config 七节字段表（§6.3 校验口径；与 config.schema.json definitions 等价）。
#[derive(Debug, Clone, Copy, PartialEq)]
enum FieldKind {
    /// 数值范围（configLimits key；schema "number"：允许小数）
    Limited(&'static str),
    /// 整数范围（configLimits key；schema "integer"：拒绝小数）
    LimitedInt(&'static str),
    /// 整数范围（显式 min/max）
    Int(i64, i64),
    Bool,
    /// 非空字符串
    Text,
    /// 可 null 字符串（ocr_model_id）
    NullableText,
    /// 枚举（themes/loggingLevels/languages → configLimits）
    LimitedEnum(&'static str),
    /// 数据根相对目录（model_directory）
    DataDir,
    /// rgb 容差对象 {r,g,b: 0..=255}
    ToleranceRgb,
    /// hsv 容差对象 {h: 0..=180, s,v: 0..=100}
    ToleranceHsv,
}

const GENERAL_FIELDS: [(&str, FieldKind); 2] = [
    ("language", FieldKind::LimitedEnum("languages")),
    ("theme", FieldKind::LimitedEnum("themes")),
];

const RECOGNITION_FIELDS: [(&str, FieldKind); 7] = [
    ("default_frequency_hz", FieldKind::Limited("defaultFrequencyHz")),
    ("default_threshold", FieldKind::Limited("threshold")),
    ("rgb_tolerance", FieldKind::ToleranceRgb),
    ("hsv_tolerance", FieldKind::ToleranceHsv),
    ("ocr_model_id", FieldKind::NullableText),
    ("ocr_min_confidence", FieldKind::Limited("ocrMinConfidence")),
    ("model_directory", FieldKind::DataDir),
];

const CAPTURE_FIELDS: [(&str, FieldKind); 2] = [
    ("preview_hz", FieldKind::LimitedInt("previewHz")),
    ("png_compression_level", FieldKind::LimitedInt("pngCompressionLevel")),
];

const INPUT_FIELDS: [(&str, FieldKind); 3] = [
    ("allow_exit", FieldKind::Bool),
    ("move_duration_ms", FieldKind::LimitedInt("moveDurationMs")),
    ("type_interval_ms", FieldKind::LimitedInt("typeIntervalMs")),
];

const EXECUTION_FIELDS: [(&str, FieldKind); 1] =
    [("total_timeout_ms", FieldKind::LimitedInt("totalTimeoutMs"))];

const HOTKEYS_FIELDS: [(&str, FieldKind); 3] = [
    ("start_selection", FieldKind::Text),
    ("stop_script", FieldKind::Text),
    ("pause_script", FieldKind::Text),
];

const LOGGING_FIELDS: [(&str, FieldKind); 3] = [
    ("level", FieldKind::LimitedEnum("loggingLevels")),
    ("rotate_bytes", FieldKind::Int(1_048_576, 104_857_600)),
    ("file_count", FieldKind::LimitedInt("fileCount")),
];

fn section_fields(section: &str) -> Option<&'static [(&'static str, FieldKind)]> {
    match section {
        "general" => Some(&GENERAL_FIELDS),
        "recognition" => Some(&RECOGNITION_FIELDS),
        "capture" => Some(&CAPTURE_FIELDS),
        "input" => Some(&INPUT_FIELDS),
        "execution" => Some(&EXECUTION_FIELDS),
        "hotkeys" => Some(&HOTKEYS_FIELDS),
        "logging" => Some(&LOGGING_FIELDS),
        _ => None,
    }
}

fn check_field(kind: &FieldKind, value: &Value, path: &str) -> Result<(), PatchError> {
    let bad = || PatchError::params(path);
    match kind {
        FieldKind::Limited(key) => {
            let limits = &config_limits()[*key];
            let min = limits["min"].as_f64().unwrap_or(f64::MIN);
            let max = limits["max"].as_f64().unwrap_or(f64::MAX);
            let f = value
                .as_f64()
                .filter(|f| f.is_finite())
                .ok_or_else(bad)?;
            if f < min || f > max {
                return Err(bad());
            }
            Ok(())
        }
        FieldKind::LimitedInt(key) => {
            let limits = &config_limits()[*key];
            let min = limits["min"].as_i64().unwrap_or(i64::MIN);
            let max = limits["max"].as_i64().unwrap_or(i64::MAX);
            let n = value.as_i64().ok_or_else(bad)?;
            if n < min || n > max {
                return Err(bad());
            }
            Ok(())
        }
        FieldKind::Int(min, max) => {
            let n = value
                .as_i64()
                .ok_or_else(bad)?;
            if n < *min || n > *max {
                return Err(bad());
            }
            Ok(())
        }
        FieldKind::Bool => {
            if value.is_boolean() {
                Ok(())
            } else {
                Err(bad())
            }
        }
        FieldKind::Text => {
            if matches!(value.as_str(), Some(s) if !s.is_empty()) {
                Ok(())
            } else {
                Err(bad())
            }
        }
        FieldKind::NullableText => {
            if value.is_null() || matches!(value.as_str(), Some(s) if !s.is_empty()) {
                Ok(())
            } else {
                Err(bad())
            }
        }
        FieldKind::LimitedEnum(key) => {
            let allowed = config_limits()[*key].as_array().unwrap();
            let s = value.as_str().ok_or_else(bad)?;
            if allowed.iter().any(|a| a.as_str() == Some(s)) {
                Ok(())
            } else {
                Err(bad())
            }
        }
        FieldKind::DataDir => {
            let s = value.as_str().ok_or_else(bad)?;
            if is_relative_data_root_path(s) {
                Ok(())
            } else {
                Err(bad())
            }
        }
        FieldKind::ToleranceRgb => {
            let obj = value.as_object().ok_or_else(bad)?;
            for key in obj.keys() {
                if !matches!(key.as_str(), "r" | "g" | "b") {
                    return Err(PatchError::params(format!("{path}.{key}")));
                }
                let n = value[key].as_i64().ok_or_else(bad)?;
                if !(0..=255).contains(&n) {
                    return Err(bad());
                }
            }
            Ok(())
        }
        FieldKind::ToleranceHsv => {
            let obj = value.as_object().ok_or_else(bad)?;
            for key in obj.keys() {
                if !matches!(key.as_str(), "h" | "s" | "v") {
                    return Err(PatchError::params(format!("{path}.{key}")));
                }
                let n = value[key].as_i64().ok_or_else(bad)?;
                let max = if key == "h" { 180 } else { 100 };
                if !(0..=max).contains(&n) {
                    return Err(bad());
                }
            }
            Ok(())
        }
    }
}

/// 深合并 patch（§5.4d：对象递归合并、标量替换、数组整体替换）。
/// 全部校验通过后才生成新文档（"非法 patch 不变旧配置"——输入 doc 不被修改）。
pub fn apply_config_patch(
    doc: &Value,
    patch: &Value,
    expected_revision: i64,
    reset: bool,
) -> Result<Value, PatchError> {
    // 步骤 3：expected_revision 不匹配 → CONFLICT（不写盘）
    let current_revision = doc
        .get("revision")
        .and_then(Value::as_i64)
        .ok_or_else(|| PatchError::params("revision"))?;
    if current_revision != expected_revision {
        return Err(PatchError::conflict());
    }
    let mut merged = if reset {
        default_config_document()
    } else {
        doc.clone()
    };
    if reset {
        merged["revision"] = json!(current_revision + 1);
        return Ok(merged);
    }
    let patch_obj = patch
        .as_object()
        .ok_or_else(|| PatchError::params("patch"))?;
    // 步骤 4a：patch 顶层未知字段（含 schema_version/revision 保护）
    for (section, sub) in patch_obj {
        let fields = section_fields(section)
            .ok_or_else(|| PatchError::params(section.clone()))?;
        let sub_obj = sub
            .as_object()
            .ok_or_else(|| PatchError::params(section.clone()))?;
        // 步骤 4：先全量校验（任一失败即拒绝，不产生半合并文档）
        for (field, value) in sub_obj {
            let kind = fields
                .iter()
                .find(|(f, _)| f == field)
                .map(|(_, k)| k)
                .ok_or_else(|| PatchError::params(format!("{section}.{field}")))?;
            check_field(kind, value, &format!("{section}.{field}"))?;
        }
        // 步骤 5：合并（校验全过后逐字段深合并）
        for (field, value) in sub_obj {
            let existing = merged
                .get_mut(section)
                .and_then(|s| s.as_object_mut());
            match existing {
                Some(target) => {
                    // 容差对象递归合并；标量替换
                    if value.is_object() {
                        let entry = target
                            .entry(field.clone())
                            .or_insert_with(|| json!({}));
                        if let Some(t) = entry.as_object_mut() {
                            for (k, v) in value.as_object().unwrap() {
                                t.insert(k.clone(), v.clone());
                            }
                        }
                    } else {
                        target.insert(field.clone(), value.clone());
                    }
                }
                None => {
                    merged[section] = json!({});
                    merged[section][field] = value.clone();
                }
            }
        }
    }
    merged["revision"] = json!(current_revision + 1);
    Ok(merged)
}

/// 从已合并文档读取点分路径（contract 测试断言 expect.field/expect.value 用）。
pub fn doc_value_at<'a>(doc: &'a Value, path: &str) -> Option<&'a Value> {
    let mut cur = doc;
    for seg in path.split('.') {
        cur = cur.get(seg)?;
    }
    Some(cur)
}

/// 单节字段表完备性自检（未知字段用例的镜像：表内键与 schema definitions 一致）。
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn defaults_match_documented_example() {
        // 02 示例逐字段抽查 + revision 初始 1 + schema_version=2
        let doc = default_config_document();
        assert_eq!(doc["schema_version"], 2);
        assert_eq!(doc["revision"], 1);
        assert_eq!(doc["general"]["language"], "zh-CN");
        assert_eq!(doc["recognition"]["default_threshold"], 0.85);
        assert_eq!(doc["recognition"]["ocr_model_id"], Value::Null);
        assert_eq!(doc["recognition"]["hsv_tolerance"]["h"], 10);
        assert_eq!(doc["capture"]["png_compression_level"], 6);
        assert_eq!(doc["input"]["move_duration_ms"], 200);
        assert_eq!(doc["hotkeys"]["start_selection"], "Ctrl+Alt+S");
        assert_eq!(doc["logging"]["rotate_bytes"], 10_485_760);
        assert_eq!(doc["logging"]["file_count"], 5);
    }

    #[test]
    fn unknown_section_and_protected_keys_rejected() {
        let doc = default_config_document();
        // 顶层未知节
        let err = apply_config_patch(&doc, &json!({"nope": {}}), 1, false).unwrap_err();
        assert_eq!(err.code, "INVALID_PARAMS");
        // revision/schema_version 不可经 patch 修改（不在节表内）
        let err2 =
            apply_config_patch(&doc, &json!({"revision": 5}), 1, false).unwrap_err();
        assert_eq!(err2.code, "INVALID_PARAMS");
    }

    #[test]
    fn null_on_non_nullable_field_rejected_locally() {
        // §5.4d 镜像（本地断言）：不可 null 字段置 null → INVALID_PARAMS
        let doc = default_config_document();
        let err = apply_config_patch(
            &doc,
            &json!({"general": {"theme": null}}),
            1,
            false,
        )
        .unwrap_err();
        assert_eq!((err.code, err.field.clone().unwrap().as_str()), ("INVALID_PARAMS", "general.theme"));
    }

    #[test]
    fn tolerance_deep_merge_keeps_other_channels() {
        // 深合并语义：rgb_tolerance 只改 r，g/b 保持
        let doc = default_config_document();
        let out = apply_config_patch(
            &doc,
            &json!({"recognition": {"rgb_tolerance": {"r": 30}}}),
            1,
            false,
        )
        .unwrap();
        assert_eq!(out["recognition"]["rgb_tolerance"]["r"], 30);
        assert_eq!(out["recognition"]["rgb_tolerance"]["g"], 15);
        assert_eq!(out["recognition"]["rgb_tolerance"]["b"], 15);
        assert_eq!(out["revision"], 2);
        // 原文档不被修改（configUnchanged 语义）
        assert_eq!(doc["recognition"]["rgb_tolerance"]["r"], 15);
    }

    #[test]
    fn map_section_merge_inserts_missing_section() {
        // 防御：目标节缺失时也能合并（Map entry or_insert 分支）
        let mut doc = default_config_document();
        doc["general"] = Value::Null; // 人为破坏节
        let out = apply_config_patch(&doc, &json!({"general": {"theme": "dark"}}), 1, false)
            .unwrap();
        assert_eq!(out["general"]["theme"], "dark");
    }
}
