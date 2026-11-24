//! 契约测试（TASK-006 设计 §2.2/§9.1）：读 schema/contract-cases.json 的
//! `type_validation`/`config_patch` 用例（applies_to 含 "rust"），与 Python
//! `test_types_contract.py` 跑同一份文件——三端同源。
//!
//! 另含 configLimits 三方运行时比对（schema/config.schema.json ↔
//! contract-cases thresholds.configLimits ↔ 本模块 config_limits() 常量），
//! 沿用 TASK-005 `shared_thresholds_match_contract_cases` 先例。

use serde_json::Value;

use super::capabilities::validate_capability;
use super::conditions::{validate_group_fields, validate_script, validate_target};
use super::config::{apply_config_patch, config_limits, default_config_document, doc_value_at};
use super::region::{validate_local_rect, validate_point, validate_region};
use super::runtime::{
    validate_execution_state, validate_monitor_config, validate_run_config,
};

fn contract_cases() -> Vec<Value> {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../schema")
        .join("contract-cases.json");
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("读取契约用例失败（{}）: {e}", path.display()));
    let parsed: Value = serde_json::from_str(&text).expect("contract-cases.json 必须是合法 JSON");
    parsed
        .get("cases")
        .and_then(Value::as_array)
        .cloned()
        .expect("cases 数组缺失")
}

fn case_applies_to_rust(case: &Value) -> bool {
    case["applies_to"]
        .as_array()
        .map(|a| a.iter().any(|x| x.as_str() == Some("rust")))
        .unwrap_or(false)
}

/// type_name → 校验纯函数分发（typeName 语义与用例集逐字对齐）。
fn dispatch_type_validation(case: &Value) -> Result<(), super::TypeError> {
    let type_name = case["typeName"].as_str().unwrap_or("");
    // nameOverrideLength：name-101 用例按提示覆写字段长度（共享样本不内联 101 字符串）
    let mut value = case["value"].clone();
    if let Some(len) = case.get("nameOverrideLength").and_then(Value::as_i64) {
        let long = "名".repeat(len as usize);
        value["name"] = Value::String(long);
    }
    match type_name {
        "region" => validate_region(&value),
        "point" => validate_point(&value),
        "local_rect" => validate_local_rect(&value),
        "target" => validate_target(&value),
        "run_config" => validate_run_config(&value),
        "monitor_config" => validate_monitor_config(&value),
        "script" => validate_script(&value),
        "group" => validate_group_fields(&value),
        other => panic!("未接入的 typeName: {other}（case={}）", case["id"]),
    }
}

#[test]
fn type_validation_cases_match_python_source() {
    let cases = contract_cases();
    let mut ran = 0usize;
    for case in &cases {
        if case["kind"].as_str() != Some("type_validation") || !case_applies_to_rust(case) {
            continue;
        }
        ran += 1;
        let id = case["id"].as_str().unwrap_or("?");
        let expect = &case["expect"];
        let result = dispatch_type_validation(case);
        match (expect["valid"].as_bool(), result) {
            (Some(true), Ok(())) => {}
            (Some(false), Err(err)) => {
                let expected_code = expect["code"].as_str().unwrap_or("INVALID_PARAMS");
                let expected_field = expect["field"].as_str().unwrap_or("");
                assert_eq!(
                    err.code, expected_code,
                    "case {id}: 错误码不符（实际 field={}）",
                    err.field
                );
                assert_eq!(err.field, expected_field, "case {id}: 字段路径不符");
            }
            (Some(true), Err(err)) => {
                panic!("case {id}: 应合法却报 {err:?}");
            }
            (Some(false), Ok(())) => {
                panic!("case {id}: 应拒绝却通过（expect.field={:?}）", expect["field"]);
            }
            _ => panic!("case {id}: expect.valid 缺失"),
        }
    }
    // b3fix-h359：B1 新增 h=359 共享用例后 type_validation 37→39
    //（新增 2 条：type-target-hsv-h-359-valid、type-monitor-run-script-double-violation-timeout-script-id-invalid）
    // TASK-011：新增 2 条 group 结构共享用例（type-group-top-level-valid、
    // type-group-nested-depth2-valid）→ type_validation 39→41
    // TASK-012：新增 3 条 script 结构共享用例（type-script-name-101-invalid、
    // type-script-parameters-not-array-invalid、type-script-minimal-valid）→ 41→44
    assert_eq!(ran, 44, "rust 侧 type_validation 用例数应与 B1+group+script 用例集一致");
}

#[test]
fn config_patch_cases_match_python_source() {
    let cases = contract_cases();
    let mut ran = 0usize;
    let doc = default_config_document(); // revision=1；纯函数不落盘
    for case in &cases {
        if case["kind"].as_str() != Some("config_patch") || !case_applies_to_rust(case) {
            continue;
        }
        ran += 1;
        let id = case["id"].as_str().unwrap_or("?");
        let patch = case.get("patch").cloned().unwrap_or(Value::Null);
        let reset = case.get("reset").and_then(Value::as_bool).unwrap_or(false);
        let expected_revision = case
            .get("expectedRevision")
            .and_then(Value::as_i64)
            .expect("config_patch 用例必须带 expectedRevision");
        let expect = &case["expect"];
        let before = doc.clone();
        let result = apply_config_patch(&doc, &patch, expected_revision, reset);
        match (expect["success"].as_bool(), result) {
            (Some(true), Ok(new_doc)) => {
                let rev = expect["revision"].as_i64().expect("成功用例须带期望 revision");
                assert_eq!(new_doc["revision"], rev, "case {id}: revision 应递增");
                if let Some(field) = expect["field"].as_str() {
                    let got = doc_value_at(&new_doc, field)
                        .unwrap_or_else(|| panic!("case {id}: 字段 {field} 缺失"));
                    assert_eq!(
                        got, &expect["value"],
                        "case {id}: 合并结果不符"
                    );
                }
            }
            (Some(false), Err(err)) => {
                let expected_code = expect["errorCode"].as_str().unwrap_or("INVALID_PARAMS");
                assert_eq!(err.code, expected_code, "case {id}: 错误码不符");
                if let Some(field) = expect["field"].as_str() {
                    assert_eq!(
                        err.field.as_deref(),
                        Some(field),
                        "case {id}: 字段路径不符"
                    );
                }
                // configUnchanged：失败路径不得改动文档（apply 为纯函数，克隆语义）
                assert!(
                    expect["configUnchanged"].as_bool().unwrap_or(false),
                    "case {id}: 失败用例应声明 configUnchanged"
                );
            }
            (Some(true), Err(err)) => panic!("case {id}: 应成功却报 {err:?}"),
            (Some(false), Ok(_)) => panic!("case {id}: 应拒绝却成功"),
            _ => panic!("case {id}: expect.success 缺失"),
        }
        assert_eq!(before, doc, "case {id}: 输入文档必须原样（configUnchanged）");
    }
    // B1 交付 11 条 config_patch（全部 applies_to 含 rust）
    assert_eq!(ran, 11, "rust 侧 config_patch 用例数应与 B1 用例集一致");
}

#[test]
fn config_limits_match_both_schema_sources() {
    // 三方比对：schema/config.schema.json configLimits ↔
    // contract-cases thresholds.configLimits ↔ config_limits() 常量
    let schema_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../schema")
        .join("config.schema.json");
    let schema: Value = serde_json::from_str(
        &std::fs::read_to_string(&schema_path).expect("config.schema.json 读取失败"),
    )
    .expect("config.schema.json 必须是合法 JSON");
    let cases_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../schema")
        .join("contract-cases.json");
    let cases: Value = serde_json::from_str(
        &std::fs::read_to_string(&cases_path).expect("contract-cases.json 读取失败"),
    )
    .expect("contract-cases.json 必须是合法 JSON");
    let from_schema = schema
        .get("configLimits")
        .expect("config.schema.json 缺 configLimits");
    let from_cases = cases
        .get("thresholds")
        .and_then(|t| t.get("configLimits"))
        .expect("contract-cases.json 缺 thresholds.configLimits");
    for key in [
        "defaultFrequencyHz",
        "threshold",
        "ocrMinConfidence",
        "pngCompressionLevel",
        "previewHz",
        "moveDurationMs",
        "typeIntervalMs",
        "totalTimeoutMs",
        "rotateBytesMin",
        "rotateBytesMax",
        "fileCount",
        "themes",
        "loggingLevels",
        "languages",
    ] {
        assert_eq!(
            &config_limits()[key], &from_schema[key],
            "configLimits.{key}: 常量与 config.schema.json 漂移"
        );
        assert_eq!(
            &from_cases[key], &from_schema[key],
            "configLimits.{key}: 两处 schema 事实源不一致"
        );
    }
}

/// Rust 侧本地能力/错误结构轻校验接入契约常量（无共享用例，保底断言）。
#[test]
fn capability_and_error_structure_validators_live() {
    let cap = serde_json::json!({"id": "capture", "state": "pending", "reason": null});
    assert!(validate_capability(&cap).is_ok());
    let err = serde_json::json!({
        "code": "DATA_CORRUPT", "message": "配置损坏",
        "details": {}, "retryable": false
    });
    assert!(super::capabilities::validate_error_structure(&err).is_ok());
}
