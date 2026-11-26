//! 目标/条件树/分组/脚本类型与校验（TASK-006 设计 §3.2/§6.2；02 权威逐字段）。
//!
//! 校验纯函数（无 IO）：`validate_target`/`validate_group_fields`/`validate_script`。
//! 错误字段路径与 schema/contract-cases.json type_validation 用例逐字对齐：
//! - name 去首尾空白后 1–100 字符 → `name`；
//! - 条件树叶 1–10、嵌套深度 ≤4、叶 ID 唯一 → 树级 `conditions`；
//! - anchor 指向存在叶 → `anchor_condition_id`；
//! - 参数错误定位 `conditions.params.<field>`（hsc → `conditions.params.color_space`、
//!   HSV h 越界 → `conditions.params.color.h`、canny low≥high → `conditions.params.canny_high`）。
//! metadata ≤16KiB；跨实体唯一性（同层同名）归 Repository，本层仅字段校验。

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::region::{validate_image_asset, validate_local_rect};
use super::{as_f64, as_i64, non_empty_str, TypeError};

/// 条件节点（02）：branch{op,children} 或 leaf{id,type,params,roi?}。
/// JSON 形态校验在 `validate_target` 内联完成（跨字段约束需全树上下文），
/// 此处提供 Rust 强类型视图（M 识别任务消费）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ConditionNode {
    /// 分支节点为 "all"/"any"；叶节点为 null（叶以 condition_type 区分）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub op: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub children: Option<Vec<ConditionNode>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub condition_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub roi: Option<super::region::LocalRect>,
}

/// name 校验（Target/Group/Script 共用）：去首尾空白后 1–100 Unicode 字符。
fn validate_name(v: &Value, field: &str) -> Result<(), TypeError> {
    let name = v
        .as_str()
        .ok_or_else(|| TypeError::invalid_params(field))?;
    let trimmed = name.trim();
    let len = trimmed.chars().count();
    if len < 1 || len > 100 {
        return Err(TypeError::invalid_params(field));
    }
    Ok(())
}

/// RFC3339 UTC 时间戳轻校验（schema pattern：YYYY-MM-DDTHH:MM:SS[.mmm]Z）。
fn validate_rfc3339(v: &Value, field: &str) -> Result<(), TypeError> {
    let s = v.as_str().ok_or_else(|| TypeError::invalid_params(field))?;
    let b = s.as_bytes();
    let ok = b.len() >= 20
        && b[4] == b'-'
        && b[7] == b'-'
        && (b[10] == b'T' || b[10] == b't')
        && b[13] == b':'
        && b[16] == b':'
        && *b.last().unwrap() == b'Z';
    if ok {
        Ok(())
    } else {
        Err(TypeError::invalid_params(field))
    }
}

/// Target 校验（§6.2 条件树全规则 + 12 必填字段 + metadata ≤16KiB）。
pub fn validate_target(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("target"))?;
    // 必填字段（02/schema 逐字；缺失与类型错误同报所属字段）
    for key in [
        "id",
        "sample_no",
        "revision",
        "name",
        "group_id",
        "image",
        "thumbnail_path",
        "anchor_condition_id",
        "conditions",
        "created_at",
        "updated_at",
        "metadata",
    ] {
        if !obj.contains_key(key) {
            return Err(TypeError::invalid_params(key));
        }
    }
    non_empty_str(obj.get("id").unwrap()).ok_or_else(|| TypeError::invalid_params("id"))?;
    let sample_no = as_i64(obj.get("sample_no").unwrap())
        .ok_or_else(|| TypeError::invalid_params("sample_no"))?;
    if sample_no < 1 {
        return Err(TypeError::invalid_params("sample_no"));
    }
    let revision = as_i64(obj.get("revision").unwrap())
        .ok_or_else(|| TypeError::invalid_params("revision"))?;
    if revision < 1 {
        return Err(TypeError::invalid_params("revision"));
    }
    validate_name(obj.get("name").unwrap(), "name")?;
    // group_id: string|null
    let group_id = obj.get("group_id").unwrap();
    if !group_id.is_null() && non_empty_str(group_id).is_none() {
        return Err(TypeError::invalid_params("group_id"));
    }
    // thumbnail_path: string|null
    let thumb = obj.get("thumbnail_path").unwrap();
    if !thumb.is_null() && thumb.as_str().is_none() {
        return Err(TypeError::invalid_params("thumbnail_path"));
    }
    validate_image_asset(obj.get("image").unwrap())?;
    // metadata：JSON 对象，序列化 ≤16KiB（02；唯一允许未知字段的例外）
    let metadata = obj.get("metadata").unwrap();
    let meta_obj = metadata
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("metadata"))?;
    let meta_bytes = serde_json::to_vec(meta_obj).unwrap_or_default().len();
    if meta_bytes > 16 * 1024 {
        return Err(TypeError::invalid_params("metadata"));
    }
    validate_rfc3339(obj.get("created_at").unwrap(), "created_at")?;
    validate_rfc3339(obj.get("updated_at").unwrap(), "updated_at")?;
    // 条件树：结构 + 参数 + 叶数/深度/唯一性
    let mut leaf_ids: Vec<String> = Vec::new();
    validate_condition_node(
        obj.get("conditions").unwrap(),
        1,
        &mut leaf_ids,
    )?;
    let leaf_count = leaf_ids.len();
    if !(1..=10).contains(&leaf_count) {
        return Err(TypeError::invalid_params("conditions"));
    }
    // anchor 必须指向存在叶
    let anchor = non_empty_str(obj.get("anchor_condition_id").unwrap())
        .ok_or_else(|| TypeError::invalid_params("anchor_condition_id"))?;
    if !leaf_ids.iter().any(|id| id == anchor) {
        return Err(TypeError::invalid_params("anchor_condition_id"));
    }
    Ok(())
}

/// 条件节点递归校验（depth 从 1 起；≤4 层分支嵌套，02）。
fn validate_condition_node(
    v: &Value,
    depth: usize,
    leaf_ids: &mut Vec<String>,
) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("conditions"))?;
    if let Some(op) = obj.get("op") {
        // 分支节点：op ∈ {all,any}；children 非空数组；深度 ≤4
        let op = op.as_str().ok_or_else(|| TypeError::invalid_params("conditions"))?;
        if op != "all" && op != "any" {
            return Err(TypeError::invalid_params("conditions"));
        }
        if depth > 4 {
            return Err(TypeError::invalid_params("conditions"));
        }
        let children = obj
            .get("children")
            .and_then(Value::as_array)
            .ok_or_else(|| TypeError::invalid_params("conditions"))?;
        if children.is_empty() {
            return Err(TypeError::invalid_params("conditions"));
        }
        for child in children {
            validate_condition_node(child, depth + 1, leaf_ids)?;
        }
        // 分支节点不得携带叶字段混型（schema oneOf 语义）
        if obj.contains_key("id") || obj.contains_key("type") || obj.contains_key("params") {
            return Err(TypeError::invalid_params("conditions"));
        }
        Ok(())
    } else {
        // 叶节点：id 非空且目标内唯一；type 四枚举；params 按类型校验
        let id = non_empty_str(obj.get("id").unwrap_or(&Value::Null))
            .ok_or_else(|| TypeError::invalid_params("conditions"))?;
        if leaf_ids.iter().any(|x| x == id) {
            return Err(TypeError::invalid_params("conditions"));
        }
        leaf_ids.push(id.to_string());
        let ctype = obj
            .get("type")
            .and_then(Value::as_str)
            .ok_or_else(|| TypeError::invalid_params("conditions.type"))?;
        let params = obj
            .get("params")
            .ok_or_else(|| TypeError::invalid_params("conditions.params"))?;
        match ctype {
            "feature_match" => validate_feature_params(params)?,
            "template" => validate_template_params(params)?,
            "contour" => validate_contour_params(params)?,
            "ocr" => validate_ocr_params(params)?,
            "color" => validate_color_params(params)?,
            _ => return Err(TypeError::invalid_params("conditions.type")),
        }
        if let Some(roi) = obj.get("roi") {
            validate_local_rect(roi).map_err(|mut e| {
                e.field = format!("conditions.{}", e.field);
                e
            })?;
        }
        Ok(())
    }
}

fn param_num(params: &Value, key: &str) -> Option<f64> {
    params.get(key).and_then(as_f64)
}

fn validate_feature_params(params: &Value) -> Result<(), TypeError> {
    let object = params.as_object().ok_or_else(|| TypeError::invalid_params("conditions.params"))?;
    for (key, value) in object {
        let valid = match key.as_str() {
            "min_inliers" => as_i64(value).is_some_and(|n| (4..=1500).contains(&n)),
            "min_inlier_ratio" => as_f64(value).is_some_and(|n| (0.0..=1.0).contains(&n)),
            "max_reprojection_error" => as_f64(value).is_some_and(|n| (0.1..=20.0).contains(&n)),
            _ => false,
        };
        if !valid {
            return Err(TypeError::invalid_params(format!("conditions.params.{key}")));
        }
    }
    Ok(())
}

/// template params：threshold 0..1 必填；allow_scale/scale_range/step 可选。
fn validate_template_params(params: &Value) -> Result<(), TypeError> {
    let obj = params
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("conditions.params"))?;
    let threshold =
        param_num(params, "threshold").ok_or_else(|| TypeError::invalid_params("conditions.params.threshold"))?;
    if !(0.0..=1.0).contains(&threshold) {
        return Err(TypeError::invalid_params("conditions.params.threshold"));
    }
    if let Some(allow) = obj.get("allow_scale") {
        if allow.as_bool().is_none() {
            return Err(TypeError::invalid_params("conditions.params.allow_scale"));
        }
    }
    if let Some(range) = obj.get("scale_range") {
        let arr = range
            .as_array()
            .ok_or_else(|| TypeError::invalid_params("conditions.params.scale_range"))?;
        if arr.len() != 2 {
            return Err(TypeError::invalid_params("conditions.params.scale_range"));
        }
        for item in arr {
            let f = as_f64(item)
                .ok_or_else(|| TypeError::invalid_params("conditions.params.scale_range"))?;
            if !(0.0..=1.0).contains(&f) {
                return Err(TypeError::invalid_params("conditions.params.scale_range"));
            }
        }
    }
    if let Some(step) = obj.get("step") {
        let s = as_f64(step)
            .ok_or_else(|| TypeError::invalid_params("conditions.params.step"))?;
        if s <= 0.0 {
            return Err(TypeError::invalid_params("conditions.params.step"));
        }
    }
    Ok(())
}

/// contour params：threshold 0..1；min_area ≥1；canny 0–255 且 low<high（相等即违例）。
fn validate_contour_params(params: &Value) -> Result<(), TypeError> {
    params
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("conditions.params"))?;
    let threshold = param_num(params, "threshold")
        .ok_or_else(|| TypeError::invalid_params("conditions.params.threshold"))?;
    if !(0.0..=1.0).contains(&threshold) {
        return Err(TypeError::invalid_params("conditions.params.threshold"));
    }
    if let Some(min_area) = params.get("min_area") {
        let a = as_i64(min_area)
            .ok_or_else(|| TypeError::invalid_params("conditions.params.min_area"))?;
        if a < 1 {
            return Err(TypeError::invalid_params("conditions.params.min_area"));
        }
    }
    let low = match params.get("canny_low") {
        Some(v) => {
            let n = as_i64(v)
                .ok_or_else(|| TypeError::invalid_params("conditions.params.canny_low"))?;
            if !(0..=255).contains(&n) {
                return Err(TypeError::invalid_params("conditions.params.canny_low"));
            }
            Some(n)
        }
        None => None,
    };
    let high = match params.get("canny_high") {
        Some(v) => {
            let n = as_i64(v)
                .ok_or_else(|| TypeError::invalid_params("conditions.params.canny_high"))?;
            if !(0..=255).contains(&n) {
                return Err(TypeError::invalid_params("conditions.params.canny_high"));
            }
            Some(n)
        }
        None => None,
    };
    // 跨字段：canny_low < canny_high（违例定位 high；02）
    if let (Some(l), Some(h)) = (low, high) {
        if l >= h {
            return Err(TypeError::invalid_params("conditions.params.canny_high"));
        }
    }
    Ok(())
}

/// ocr params：text/model_id 必填非空；match_mode/min_confidence/case_sensitive/
/// fuzzy_threshold 范围与枚举。
fn validate_ocr_params(params: &Value) -> Result<(), TypeError> {
    let obj = params
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("conditions.params"))?;
    non_empty_str(obj.get("text").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("conditions.params.text"))?;
    non_empty_str(obj.get("model_id").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("conditions.params.model_id"))?;
    if let Some(mode) = obj.get("match_mode") {
        let m = mode
            .as_str()
            .ok_or_else(|| TypeError::invalid_params("conditions.params.match_mode"))?;
        if m != "exact" && m != "contains" && m != "fuzzy" {
            return Err(TypeError::invalid_params("conditions.params.match_mode"));
        }
    }
    for key in ["min_confidence", "fuzzy_threshold"] {
        if let Some(v) = obj.get(key) {
            let f = as_f64(v)
                .ok_or_else(|| TypeError::invalid_params(format!("conditions.params.{key}")))?;
            if !(0.0..=1.0).contains(&f) {
                return Err(TypeError::invalid_params(format!("conditions.params.{key}")));
            }
        }
    }
    if let Some(cs) = obj.get("case_sensitive") {
        if cs.as_bool().is_none() {
            return Err(TypeError::invalid_params("conditions.params.case_sensitive"));
        }
    }
    Ok(())
}

/// color params：color_space 仅 rgb|hsv（hsc 生成即 INVALID_PARAMS）；color/tolerance
/// 随空间取色型；HSV h∈[0,360)、容差 h∈[0,180]。
fn validate_color_params(params: &Value) -> Result<(), TypeError> {
    let obj = params
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("conditions.params"))?;
    let space = obj
        .get("color_space")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("conditions.params.color_space"))?;
    if space != "rgb" && space != "hsv" {
        return Err(TypeError::invalid_params("conditions.params.color_space"));
    }
    let color = obj
        .get("color")
        .ok_or_else(|| TypeError::invalid_params("conditions.params.color"))?;
    match space {
        "rgb" => {
            for key in ["r", "g", "b"] {
                let ch = as_i64(color.get(key).unwrap_or(&Value::Null)).ok_or_else(|| {
                    TypeError::invalid_params(format!("conditions.params.color.{key}"))
                })?;
                if !(0..=255).contains(&ch) {
                    return Err(TypeError::invalid_params(format!(
                        "conditions.params.color.{key}"
                    )));
                }
            }
            if let Some(tol) = obj.get("tolerance") {
                for key in ["r", "g", "b"] {
                    let ch = as_i64(tol.get(key).unwrap_or(&Value::Null)).ok_or_else(|| {
                        TypeError::invalid_params(format!("conditions.params.tolerance.{key}"))
                    })?;
                    if !(0..=255).contains(&ch) {
                        return Err(TypeError::invalid_params(format!(
                            "conditions.params.tolerance.{key}"
                        )));
                    }
                }
            }
        }
        _ => {
            let h = as_i64(color.get("h").unwrap_or(&Value::Null)).ok_or_else(|| {
                TypeError::invalid_params("conditions.params.color.h")
            })?;
            // schema h maximum=359：**闭区间 [0,359] 合法**（h=359 通过、360 拒绝）。
            // b3fix：旧写法 0..359 独占区间错误拒绝 h=359（B4 交叉核对发现的
            // off-by-one；契约用例只覆盖 360 拒绝未覆盖 359 合法，故未暴露）。
            if !(0..=359).contains(&h) {
                return Err(TypeError::invalid_params("conditions.params.color.h"));
            }
            for key in ["s", "v"] {
                let ch = as_i64(color.get(key).unwrap_or(&Value::Null)).ok_or_else(|| {
                    TypeError::invalid_params(format!("conditions.params.color.{key}"))
                })?;
                if !(0..=100).contains(&ch) {
                    return Err(TypeError::invalid_params(format!(
                        "conditions.params.color.{key}"
                    )));
                }
            }
            if let Some(tol) = obj.get("tolerance") {
                let th = as_i64(tol.get("h").unwrap_or(&Value::Null)).ok_or_else(|| {
                    TypeError::invalid_params("conditions.params.tolerance.h")
                })?;
                if !(0..=180).contains(&th) {
                    return Err(TypeError::invalid_params("conditions.params.tolerance.h"));
                }
                for key in ["s", "v"] {
                    let ch = as_i64(tol.get(key).unwrap_or(&Value::Null)).ok_or_else(|| {
                        TypeError::invalid_params(format!("conditions.params.tolerance.{key}"))
                    })?;
                    if !(0..=100).contains(&ch) {
                        return Err(TypeError::invalid_params(format!(
                            "conditions.params.tolerance.{key}"
                        )));
                    }
                }
            }
        }
    }
    if let Some(mpc) = obj.get("min_pixel_count") {
        let n = as_i64(mpc)
            .ok_or_else(|| TypeError::invalid_params("conditions.params.min_pixel_count"))?;
        if n < 1 {
            return Err(TypeError::invalid_params("conditions.params.min_pixel_count"));
        }
    }
    if let Some(mp) = obj.get("min_percentage") {
        let f = as_f64(mp)
            .ok_or_else(|| TypeError::invalid_params("conditions.params.min_percentage"))?;
        if !(0.0..=100.0).contains(&f) {
            return Err(TypeError::invalid_params("conditions.params.min_percentage"));
        }
    }
    Ok(())
}

/// Group 字段校验（§3.2：depth 0..2；同层同名唯一性归 Repository）。
pub fn validate_group_fields(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("group"))?;
    for key in ["id", "revision", "name", "parent_group_id", "depth", "created_at", "updated_at"] {
        if !obj.contains_key(key) {
            return Err(TypeError::invalid_params(key));
        }
    }
    non_empty_str(obj.get("id").unwrap()).ok_or_else(|| TypeError::invalid_params("id"))?;
    let revision = as_i64(obj.get("revision").unwrap())
        .ok_or_else(|| TypeError::invalid_params("revision"))?;
    if revision < 1 {
        return Err(TypeError::invalid_params("revision"));
    }
    validate_name(obj.get("name").unwrap(), "name")?;
    let parent = obj.get("parent_group_id").unwrap();
    if !parent.is_null() && non_empty_str(parent).is_none() {
        return Err(TypeError::invalid_params("parent_group_id"));
    }
    let depth =
        as_i64(obj.get("depth").unwrap()).ok_or_else(|| TypeError::invalid_params("depth"))?;
    if !(0..=2).contains(&depth) {
        return Err(TypeError::invalid_params("depth"));
    }
    validate_rfc3339(obj.get("created_at").unwrap(), "created_at")?;
    validate_rfc3339(obj.get("updated_at").unwrap(), "updated_at")?;
    Ok(())
}

/// Script 校验（source_path 形态 + 组件判断；parameters required/default 约束）。
pub fn validate_script(v: &Value) -> Result<(), TypeError> {
    if let Some(policy) = v.get("action_policy") {
        crate::input::policy::ActionPolicy::parse(policy).map_err(TypeError::invalid_params)?;
    }
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("script"))?;
    for key in [
        "id",
        "revision",
        "name",
        "source_path",
        "source_sha256",
        "parameters",
        "created_at",
        "updated_at",
    ] {
        if !obj.contains_key(key) {
            return Err(TypeError::invalid_params(key));
        }
    }
    non_empty_str(obj.get("id").unwrap()).ok_or_else(|| TypeError::invalid_params("id"))?;
    let revision = as_i64(obj.get("revision").unwrap())
        .ok_or_else(|| TypeError::invalid_params("revision"))?;
    if revision < 1 {
        return Err(TypeError::invalid_params("revision"));
    }
    validate_name(obj.get("name").unwrap(), "name")?;
    // source_path：scripts/<id>/<hash>.lua 形态 + 组件判断（.. 拒绝，02/07 §6）
    let source_path = obj
        .get("source_path")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("source_path"))?;
    validate_script_source_path(source_path)?;
    let sha = obj
        .get("source_sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("source_sha256"))?;
    if !super::is_sha256_hex(sha) {
        return Err(TypeError::invalid_params("source_sha256"));
    }
    let params = obj
        .get("parameters")
        .and_then(Value::as_array)
        .ok_or_else(|| TypeError::invalid_params("parameters"))?;
    for p in params {
        let po = p
            .as_object()
            .ok_or_else(|| TypeError::invalid_params("parameters"))?;
        non_empty_str(po.get("name").unwrap_or(&Value::Null))
            .ok_or_else(|| TypeError::invalid_params("parameters"))?;
        let ptype = po
            .get("type")
            .and_then(Value::as_str)
            .ok_or_else(|| TypeError::invalid_params("parameters"))?;
        if !matches!(
            ptype,
            "string" | "number" | "boolean" | "object" | "array"
        ) {
            return Err(TypeError::invalid_params("parameters"));
        }
        let required = po
            .get("required")
            .and_then(Value::as_bool)
            .ok_or_else(|| TypeError::invalid_params("parameters"))?;
        // 02：required=true 时 default 必须 null
        if required && !po.get("default").is_some_and(Value::is_null) {
            return Err(TypeError::invalid_params("parameters"));
        }
        if !po.contains_key("description") || !po.get("description").unwrap().is_string() {
            return Err(TypeError::invalid_params("parameters"));
        }
    }
    validate_rfc3339(obj.get("created_at").unwrap(), "created_at")?;
    validate_rfc3339(obj.get("updated_at").unwrap(), "updated_at")?;
    Ok(())
}

/// source_path 校验：`scripts/<seg>/<hash>.lua`；组件判断拒绝 `..`/`.`/空段/绝对形态。
fn validate_script_source_path(path: &str) -> Result<(), TypeError> {
    let err = || TypeError::invalid_params("source_path");
    if path.starts_with('/') || path.starts_with('\\') {
        return Err(err());
    }
    let normalized = path.replace('\\', "/");
    let segments: Vec<&str> = normalized.split('/').collect();
    // 形态：scripts / <id> / <hash>.lua（恰好 3 段）
    if segments.len() != 3 || segments[0] != "scripts" {
        return Err(err());
    }
    for seg in &segments[1..] {
        if seg.is_empty() || super::path_components_escaped(&[*seg]) {
            return Err(err());
        }
    }
    if !segments[2].ends_with(".lua") || segments[2] == ".lua" {
        return Err(err());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn base_target(name: &str, conditions: Value) -> Value {
        json!({
            "id": "t1", "sample_no": 1, "revision": 1, "name": name,
            "group_id": null,
            "image": {
                "path": "images/example.png",
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "width": 120, "height": 40, "format": "png", "storage_mode": "original"
            },
            "thumbnail_path": null,
            "anchor_condition_id": "c1",
            "conditions": conditions,
            "created_at": "2026-09-21T09:00:00Z",
            "updated_at": "2026-09-21T09:00:00Z",
            "metadata": {}
        })
    }

    fn leaf(id: &str, ctype: &str, params: Value) -> Value {
        json!({ "id": id, "type": ctype, "params": params })
    }

    #[test]
    fn metadata_over_16kib_rejected_locally() {
        // 无共享用例覆盖路径（本地断言）：metadata 序列化 >16KiB → INVALID_PARAMS metadata
        let big = "x".repeat(17 * 1024);
        let mut t = base_target("大元数据", json!({ "op": "all", "children": [leaf("c1", "template", json!({"threshold": 0.85}))] }));
        t["metadata"] = json!({ "blob": big });
        let err = validate_target(&t).unwrap_err();
        assert_eq!((err.code, err.field.as_str()), ("INVALID_PARAMS", "metadata"));
    }

    #[test]
    fn mixed_branch_leaf_rejected_locally() {
        // 分支节点混入叶字段（schema oneOf）：结构级拒绝
        let mixed = json!({
            "op": "all", "id": "c1", "type": "template", "params": {"threshold": 0.85},
            "children": []
        });
        let t = base_target("混型", mixed);
        let err = validate_target(&t).unwrap_err();
        assert_eq!(err.field, "conditions");
    }

    #[test]
    fn single_leaf_target_is_valid_boundary() {
        // 叶 1–10 下边界：单叶合法
        let t = base_target("单叶", json!({ "op": "all", "children": [leaf("c1", "template", json!({"threshold": 0.85}))] }));
        assert!(validate_target(&t).is_ok());
    }

    #[test]
    fn script_path_component_check_not_startswith() {
        // 组合形态："scripts/a/..../x.lua" 中段含 ".." 组件 → 拒绝（非 startswith 判断）
        let mut s = json!({
            "id": "s1", "revision": 1, "name": "脚本",
            "source_path": "scripts/a/../b.lua",
            "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "parameters": [],
            "created_at": "2026-09-21T09:00:00Z",
            "updated_at": "2026-09-21T09:00:00Z"
        });
        assert_eq!(validate_script(&s).unwrap_err().field, "source_path");
        s["source_path"] = json!("scripts/a/b.lua");
        assert!(validate_script(&s).is_ok());
    }

    #[test]
    fn hsv_h_closed_interval_359_boundary() {
        // b3fix-h359 本地边界：schema h maximum=359 为闭区间——h=359 合法、h=360 拒绝
        let color = |h: i64| {
            base_target(
                "HSV上界",
                json!({ "op": "all", "children": [leaf("c1", "color", json!({
                    "color_space": "hsv",
                    "color": { "h": h, "s": 80, "v": 90 },
                    "tolerance": { "h": 18, "s": 20, "v": 20 }
                }))] }),
            )
        };
        assert!(validate_target(&color(359)).is_ok(), "h=359 应合法（闭区间 [0,359]）");
        let err = validate_target(&color(360)).unwrap_err();
        assert_eq!(err.field, "conditions.params.color.h");
    }
}
