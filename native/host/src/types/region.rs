//! 空间与通用类型（TASK-006 设计 §3.1/§6.1；02 权威逐字段）。
//!
//! 校验为纯函数（`validate_region`/`validate_point`/`validate_local_rect`），
//! 输入 `serde_json::Value`（契约用例三端同源），错误按 §6.1 表定位字段：
//! - space 枚举错 → `region.space`；坐标负值（client/image）→ `region.x`/`region.y`；
//! - 宽高 <10 → INVALID_REGION（06"太小"分支）→ `region.width`/`region.height`；
//! - 空间互斥：display_id 仅 screen、binding_id 仅 client、image_id 仅 image；
//!   缺失与错配均报所属字段路径。
//! 与运行 binding 一致性/显示器集合归属属运行时校验（M01），本层不做。

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{as_i64, non_empty_str, TypeError};

/// Rect（02）：左闭右开 [x,x+w)×[y,y+h)，宽高正整数；不携带空间。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct Rect {
    pub x: i64,
    pub y: i64,
    pub width: i64,
    pub height: i64,
}

/// Point（02）：单像素读取，不受 Region ≥10 限制，坐标可负。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct Point {
    pub x: i64,
    pub y: i64,
}

/// LocalRect（02）：条件局部 ROI，相对候选 bbox，宽高 ≥1（不受 10×10 约束）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct LocalRect {
    pub x: i64,
    pub y: i64,
    pub width: i64,
    pub height: i64,
}

/// Region 空间枚举（02 三枚举；图像/窗口/屏幕物理坐标）。
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RegionSpace {
    ScreenPhysical,
    WindowClientPhysical,
    ImagePixel,
}

impl RegionSpace {
    pub fn as_str(self) -> &'static str {
        match self {
            RegionSpace::ScreenPhysical => "screen_physical",
            RegionSpace::WindowClientPhysical => "window_client_physical",
            RegionSpace::ImagePixel => "image_pixel",
        }
    }

    fn from_str(s: &str) -> Option<Self> {
        match s {
            "screen_physical" => Some(RegionSpace::ScreenPhysical),
            "window_client_physical" => Some(RegionSpace::WindowClientPhysical),
            "image_pixel" => Some(RegionSpace::ImagePixel),
            _ => None,
        }
    }
}

/// Region（02）：空间互斥——display_id 仅 screen、binding_id 仅 client、
/// image_id 仅 image；screen x/y 可负，client/image 不可负；宽高 ≥10。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct Region {
    pub space: RegionSpace,
    pub x: i64,
    pub y: i64,
    pub width: i64,
    pub height: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub display_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub image_id: Option<String>,
}

/// DisplayInfo（02）：rect 为 screen 物理坐标；dpi_scale > 0。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct DisplayInfo {
    pub id: String,
    pub rect: Rect,
    pub dpi_scale: f64,
    pub primary: bool,
}

/// ImageRef（02）：引用只在当前引擎会话有效；持久化转 ImageAsset。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ImageRef {
    pub image_id: String,
    pub width: i64,
    pub height: i64,
    pub format: String,
    pub sha256: String,
}

/// ImageAsset（02）：数据根相对 path（禁绝对/盘符/..）、storage_mode 两枚举。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct ImageAsset {
    pub path: String,
    pub sha256: String,
    pub width: i64,
    pub height: i64,
    pub format: String,
    pub storage_mode: String,
}

/// Frame（02）：未绑定 geometry_revision=0、binding_id=null；图像导入 origin=(0,0)。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct Frame {
    pub frame_id: String,
    pub image_ref: ImageRef,
    pub space: RegionSpace,
    pub origin: Point,
    pub captured_at: String,
    pub geometry_revision: i64,
    pub binding_id: Option<String>,
}

/// Region 纯校验（§6.1 全规则；与 contract-cases type_validation 用例逐字对齐）。
pub fn validate_region(v: &Value) -> Result<(), TypeError> {
    let obj = v.as_object().ok_or_else(|| TypeError::invalid_params("region"))?;
    // 允许键集（schema additionalProperties:false）
    for key in obj.keys() {
        if !matches!(
            key.as_str(),
            "space" | "x" | "y" | "width" | "height" | "display_id" | "binding_id" | "image_id"
        ) {
            return Err(TypeError::invalid_params(format!("region.{key}")));
        }
    }
    // 1. space 三枚举（未知空间 → region.space）
    let space_raw = obj
        .get("space")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("region.space"))?;
    let space = RegionSpace::from_str(space_raw)
        .ok_or_else(|| TypeError::invalid_params("region.space"))?;
    // 2. x/y 整数；screen 可负，client/image 不可负
    let x = as_i64(obj.get("x").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("region.x"))?;
    let y = as_i64(obj.get("y").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("region.y"))?;
    if space != RegionSpace::ScreenPhysical && (x < 0 || y < 0) {
        // 负坐标规则：先 x 后 y（与用例预期一致）
        return Err(TypeError::invalid_params(if x < 0 { "region.x" } else { "region.y" }));
    }
    // 3. 宽高整数 ≥10（10×10 下限；太小 → INVALID_REGION）
    let width = as_i64(obj.get("width").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("region.width"))?;
    let height = as_i64(obj.get("height").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("region.height"))?;
    if width < 10 {
        return Err(TypeError::invalid_region("region.width"));
    }
    if height < 10 {
        return Err(TypeError::invalid_region("region.height"));
    }
    // 4. 空间互斥：所属 id 必填 + 交叉字段禁止（先必填后错配，与用例对齐）
    let display_id = obj.get("display_id");
    let binding_id = obj.get("binding_id");
    let image_id = obj.get("image_id");
    let present = |f: Option<&Value>| matches!(f, Some(Value::String(s)) if !s.is_empty());
    match space {
        RegionSpace::ScreenPhysical => {
            if !present(display_id) {
                return Err(TypeError::invalid_params("region.display_id"));
            }
            if binding_id.is_some() {
                return Err(TypeError::invalid_params("region.binding_id"));
            }
            if image_id.is_some() {
                return Err(TypeError::invalid_params("region.image_id"));
            }
        }
        RegionSpace::WindowClientPhysical => {
            if !present(binding_id) {
                return Err(TypeError::invalid_params("region.binding_id"));
            }
            if display_id.is_some() {
                return Err(TypeError::invalid_params("region.display_id"));
            }
            if image_id.is_some() {
                return Err(TypeError::invalid_params("region.image_id"));
            }
        }
        RegionSpace::ImagePixel => {
            if !present(image_id) {
                return Err(TypeError::invalid_params("region.image_id"));
            }
            if display_id.is_some() {
                return Err(TypeError::invalid_params("region.display_id"));
            }
            if binding_id.is_some() {
                return Err(TypeError::invalid_params("region.binding_id"));
            }
        }
    }
    Ok(())
}

/// Point 纯校验（单像素，坐标可负，不受 ≥10 限制）。
pub fn validate_point(v: &Value) -> Result<(), TypeError> {
    let obj = v.as_object().ok_or_else(|| TypeError::invalid_params("point"))?;
    for key in obj.keys() {
        if key != "x" && key != "y" {
            return Err(TypeError::invalid_params(format!("point.{key}")));
        }
    }
    as_i64(obj.get("x").unwrap_or(&Value::Null)).ok_or_else(|| TypeError::invalid_params("point.x"))?;
    as_i64(obj.get("y").unwrap_or(&Value::Null)).ok_or_else(|| TypeError::invalid_params("point.y"))?;
    Ok(())
}

/// LocalRect 纯校验（宽高 ≥1；0 宽 → local_rect.width）。
pub fn validate_local_rect(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("local_rect"))?;
    for key in obj.keys() {
        if !matches!(key.as_str(), "x" | "y" | "width" | "height") {
            return Err(TypeError::invalid_params(format!("local_rect.{key}")));
        }
    }
    as_i64(obj.get("x").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("local_rect.x"))?;
    as_i64(obj.get("y").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("local_rect.y"))?;
    let width = as_i64(obj.get("width").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("local_rect.width"))?;
    if width < 1 {
        return Err(TypeError::invalid_params("local_rect.width"));
    }
    let height = as_i64(obj.get("height").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("local_rect.height"))?;
    if height < 1 {
        return Err(TypeError::invalid_params("local_rect.height"));
    }
    Ok(())
}

/// DisplayInfo 轻校验（id 非空、dpi_scale>0；rect 结构交给 Rect 语义）。
pub fn validate_display_info(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("display_info"))?;
    non_empty_str(obj.get("id").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("display_info.id"))?;
    let dpi = obj
        .get("dpi_scale")
        .and_then(Value::as_f64)
        .ok_or_else(|| TypeError::invalid_params("display_info.dpi_scale"))?;
    if !(dpi > 0.0) {
        return Err(TypeError::invalid_params("display_info.dpi_scale"));
    }
    obj.get("primary")
        .and_then(Value::as_bool)
        .ok_or_else(|| TypeError::invalid_params("display_info.primary"))?;
    validate_rect_value(obj.get("rect").unwrap_or(&Value::Null), "display_info.rect")
}

fn validate_rect_value(v: &Value, prefix: &str) -> Result<(), TypeError> {
    let obj = v.as_object().ok_or_else(|| TypeError::invalid_params(prefix.to_string()))?;
    for key in ["x", "y", "width", "height"] {
        let n = as_i64(obj.get(key).unwrap_or(&Value::Null))
            .ok_or_else(|| TypeError::invalid_params(format!("{prefix}.{key}")))?;
        if matches!(key, "width" | "height") && n < 1 {
            return Err(TypeError::invalid_params(format!("{prefix}.{key}")));
        }
    }
    Ok(())
}

/// ImageRef 轻校验（id 非空、format 两枚举、sha256 hex64）。
pub fn validate_image_ref(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("image_ref"))?;
    non_empty_str(obj.get("image_id").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("image_ref.image_id"))?;
    as_i64(obj.get("width").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("image_ref.width"))?;
    as_i64(obj.get("height").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("image_ref.height"))?;
    let format = obj
        .get("format")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("image_ref.format"))?;
    if format != "png" && format != "jpeg" {
        return Err(TypeError::invalid_params("image_ref.format"));
    }
    let sha = obj
        .get("sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("image_ref.sha256"))?;
    if !super::is_sha256_hex(sha) {
        return Err(TypeError::invalid_params("image_ref.sha256"));
    }
    Ok(())
}

/// ImageAsset 轻校验（path 数据根相对组件判断、storage_mode 两枚举）。
pub fn validate_image_asset(v: &Value) -> Result<(), TypeError> {
    let obj = v
        .as_object()
        .ok_or_else(|| TypeError::invalid_params("image"))?;
    let path = obj
        .get("path")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("image.path"))?;
    if !super::is_relative_data_root_path(path) {
        return Err(TypeError::invalid_params("image.path"));
    }
    let sha = obj
        .get("sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("image.sha256"))?;
    if !super::is_sha256_hex(sha) {
        return Err(TypeError::invalid_params("image.sha256"));
    }
    as_i64(obj.get("width").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("image.width"))?;
    as_i64(obj.get("height").unwrap_or(&Value::Null))
        .ok_or_else(|| TypeError::invalid_params("image.height"))?;
    let format = obj
        .get("format")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("image.format"))?;
    if format != "png" && format != "jpeg" {
        return Err(TypeError::invalid_params("image.format"));
    }
    let storage = obj
        .get("storage_mode")
        .and_then(Value::as_str)
        .ok_or_else(|| TypeError::invalid_params("image.storage_mode"))?;
    if storage != "original" && storage != "png_lossless" {
        return Err(TypeError::invalid_params("image.storage_mode"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // ---- 无共享用例覆盖的本地断言（团队口径：每端补 1-2 条）----

    #[test]
    fn region_unknown_key_rejected() {
        // schema additionalProperties:false：未知键定位到 region.<key>
        let v = json!({
            "space": "screen_physical", "x": 0, "y": 0, "width": 20, "height": 20,
            "display_id": "d1", "extra": 1
        });
        let err = validate_region(&v).unwrap_err();
        assert_eq!((err.code, err.field.as_str()), ("INVALID_PARAMS", "region.extra"));
    }

    #[test]
    fn region_nested_parent_escape_for_image_asset() {
        // ImageAsset.path 组件判断：中部 .. 拒绝（07 §6，非 startswith）
        let v = json!({
            "path": "images/../secret.png",
            "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "width": 10, "height": 10, "format": "png", "storage_mode": "original"
        });
        let err = validate_image_asset(&v).unwrap_err();
        assert_eq!(err.field, "image.path");
        let ok = json!({
            "path": "images/sub/example.png",
            "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "width": 10, "height": 10, "format": "png", "storage_mode": "png_lossless"
        });
        assert!(validate_image_asset(&ok).is_ok());
    }

    #[test]
    fn region_screen_zero_size_and_floats_rejected() {
        // 浮点冒充整数坐标：类型不符（不是"太小"）
        let v = json!({
            "space": "screen_physical", "x": 0.5, "y": 0, "width": 20, "height": 20,
            "display_id": "d1"
        });
        let err = validate_region(&v).unwrap_err();
        assert_eq!(err.field, "region.x");
        // 宽 10 高 10 合法边界（已在契约用例），9 宽走 INVALID_REGION（契约覆盖，
        // 这里补 height=9.5 浮点 → 参数错误而非 INVALID_REGION）
        let v2 = json!({
            "space": "screen_physical", "x": 0, "y": 0, "width": 10, "height": 9.5,
            "display_id": "d1"
        });
        let err2 = validate_region(&v2).unwrap_err();
        assert_eq!((err2.code, err2.field.as_str()), ("INVALID_PARAMS", "region.height"));
    }

    #[test]
    fn serde_roundtrip_region_snake_case() {
        let r = Region {
            space: RegionSpace::WindowClientPhysical,
            x: 0,
            y: 0,
            width: 120,
            height: 40,
            display_id: None,
            binding_id: Some("binding-1".to_string()),
            image_id: None,
        };
        let s = serde_json::to_string(&r).unwrap();
        assert!(s.contains("\"window_client_physical\"") && s.contains("\"binding_id\""));
        let back: Region = serde_json::from_str(&s).unwrap();
        assert_eq!(back, r);
    }
}
