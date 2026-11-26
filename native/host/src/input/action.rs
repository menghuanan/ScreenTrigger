//! `InputAction` 解析与**整笔校验**（TASK-017 / 03 §全双工与宿主调用）。
//!
//! **职责**：
//! - [`parse_action`]：把 03 的带 `kind` 对象解析为 [`InputAction`]（7 种 kind），
//!   逐字段校验范围（时长 0–5000ms、`text` ≤10000 字符、`clicks` 1–2、`keys`
//!   1–5 个不同标准键名、`scroll.delta` -100..100）；越界/未知字段 → INVALID_PARAMS。
//! - [`validation_points`]：给出该动作需要做区域/显示器校验的点集（**整笔校验**，
//!   06§不变量 5：非法端点/路径拒绝，不做 clamp）。非坐标动作返回空集，由派发层
//!   改用当前指针位置同样做校验。
//! - [`lookup_vk`]：标准键名 → 虚拟键码（纯表，跨平台可测；`win32.rs` 只用结果）。
//!
//! **来源章节**：docs/03 §全双工与宿主调用、docs/04 §鼠标与键盘、docs/06
//! §坐标区域与输入不变量、docs/07 §5。

use serde_json::Value;

use crate::ipc::IpcError;

use super::model::{
    InputAction, Key, KeyState, MouseButton, Point, MAX_CLICKS, MAX_DURATION_MS,
    MAX_HOTKEY_KEYS, MAX_TEXT_CHARS, SCROLL_DELTA_MAX, SCROLL_DELTA_MIN,
};

/// 解析并整笔校验一个 03 `InputAction` 对象。
pub fn parse_action(value: &Value) -> Result<InputAction, IpcError> {
    if let Some(raw) = value.get("policy") {
        let field_error = |field: String| IpcError::invalid_params(&field)
            .with_details(serde_json::json!({"field":field}));
        let policy = super::policy::ActionPolicy::parse(raw).map_err(field_error)?;
        let mut inner = value.clone();
        inner.as_object_mut().unwrap().remove("policy");
        let action = parse_action(&inner)?;
        policy.validate_kind(action.kind_str()).map_err(field_error)?;
        return Ok(InputAction::WithPolicy { action: Box::new(action), policy });
    }
    let obj = value
        .as_object()
        .ok_or_else(|| IpcError::invalid_params("action 必须为对象"))?;
    let kind = obj
        .get("kind")
        .and_then(Value::as_str)
        .ok_or_else(|| IpcError::invalid_params("action.kind 必须为字符串"))?;
    match kind {
        "move" => {
            let x = req_i64(obj, "x", "action.x")?;
            let y = req_i64(obj, "y", "action.y")?;
            let duration_ms = opt_duration_ms(obj, "duration_ms", "action.duration_ms", 0)?;
            Ok(InputAction::Move { x, y, duration_ms })
        }
        "click" | "click_anchor" => {
            let button = match obj.get("button") {
                None | Some(Value::Null) => MouseButton::Left,
                Some(Value::String(s)) => MouseButton::parse(s)
                    .ok_or_else(|| IpcError::invalid_params("action.button 必须为 left|right|middle"))?,
                Some(_) => return Err(IpcError::invalid_params("action.button 必须为字符串")),
            };
            let clicks = match obj.get("clicks") {
                None | Some(Value::Null) => 1,
                Some(cv) => {
                    let n = cv
                        .as_i64()
                        .ok_or_else(|| IpcError::invalid_params("action.clicks 必须为整数"))?;
                    if n < 1 || n > MAX_CLICKS as i64 {
                        return Err(IpcError::invalid_params(&format!(
                            "action.clicks 超出范围 1..={MAX_CLICKS}"
                        )));
                    }
                    n as u32
                }
            };
            if kind == "click_anchor" {
                if obj.keys().any(|k| !matches!(k.as_str(), "kind" | "anchor" | "button" | "clicks")) {
                    return Err(IpcError::invalid_params("click_anchor 含未知字段"));
                }
                let anchor = super::anchor::AnchoredPoint::parse(&value["anchor"])?;
                Ok(InputAction::ClickAnchor { anchor, button, clicks })
            } else {
                let x = req_i64(obj, "x", "action.x")?;
                let y = req_i64(obj, "y", "action.y")?;
                Ok(InputAction::Click { x, y, button, clicks })
            }
        }
        "drag" => {
            let from = req_point(obj, "from", "action.from")?;
            let to = req_point(obj, "to", "action.to")?;
            let duration_ms = opt_duration_ms(obj, "duration_ms", "action.duration_ms", 0)?;
            Ok(InputAction::Drag { from, to, duration_ms })
        }
        "scroll" => {
            let delta = obj
                .get("delta")
                .and_then(Value::as_i64)
                .ok_or_else(|| IpcError::invalid_params("action.delta 必须为整数"))?;
            if delta < SCROLL_DELTA_MIN as i64 || delta > SCROLL_DELTA_MAX as i64 {
                return Err(IpcError::invalid_params(&format!(
                    "action.delta 超出范围 {SCROLL_DELTA_MIN}..={SCROLL_DELTA_MAX}"
                )));
            }
            Ok(InputAction::Scroll { delta: delta as i32 })
        }
        "key" => {
            let name = obj
                .get("key")
                .and_then(Value::as_str)
                .ok_or_else(|| IpcError::invalid_params("action.key 必须为字符串"))?;
            let key = parse_key(name)?;
            let state = obj
                .get("state")
                .and_then(Value::as_str)
                .and_then(KeyState::parse)
                .ok_or_else(|| IpcError::invalid_params("action.state 必须为 tap|down|up"))?;
            Ok(InputAction::Key { key, state })
        }
        "text" => {
            let text = obj
                .get("text")
                .and_then(Value::as_str)
                .ok_or_else(|| IpcError::invalid_params("action.text 必须为字符串"))?;
            let chars = text.chars().count();
            if chars > MAX_TEXT_CHARS {
                return Err(IpcError::invalid_params(&format!(
                    "action.text 超过 {MAX_TEXT_CHARS} Unicode 字符（实际 {chars}）"
                )));
            }
            let interval_ms = opt_duration_ms(obj, "interval_ms", "action.interval_ms", 0)?;
            Ok(InputAction::Text { text: text.to_string(), interval_ms })
        }
        "hotkey" => {
            let arr = obj
                .get("keys")
                .and_then(Value::as_array)
                .ok_or_else(|| IpcError::invalid_params("action.keys 必须为数组"))?;
            if arr.is_empty() || arr.len() > MAX_HOTKEY_KEYS {
                return Err(IpcError::invalid_params(&format!(
                    "action.keys 数量必须为 1..={MAX_HOTKEY_KEYS}"
                )));
            }
            let mut keys: Vec<Key> = Vec::with_capacity(arr.len());
            for item in arr {
                let name = item
                    .as_str()
                    .ok_or_else(|| IpcError::invalid_params("action.keys 元素必须为字符串"))?;
                let key = parse_key(name)?;
                if keys.iter().any(|k| k.name == key.name) {
                    return Err(IpcError::invalid_params(&format!(
                        "action.keys 存在重复键：{}",
                        key.name
                    )));
                }
                keys.push(key);
            }
            Ok(InputAction::Hotkey { keys })
        }
        other => Err(IpcError::invalid_params(&format!("action.kind 未知：{other}"))),
    }
}

/// 该动作需要做区域/显示器校验的点集（整笔校验；非坐标动作返回空集）。
///
/// 拖拽返回从起点到终点的采样路径（逐段可发现跨显示器空隙越界）；`move`/`click`
/// 返回其端点。非坐标动作返回空集——派发层改用当前指针位置做同样的区域/身份检查
/// （03：非坐标动作使用当前指针/前台并同样做区域/身份检查）。
pub fn validation_points(action: &InputAction) -> Vec<Point> {
    match action {
        InputAction::WithPolicy { action, .. } => validation_points(action),
        InputAction::Move { x, y, .. } => vec![Point { x: *x, y: *y }],
        InputAction::Click { x, y, .. } => vec![Point { x: *x, y: *y }],
        InputAction::Drag { from, to, .. } => drag_path(from, to),
        InputAction::ClickAnchor { .. } | InputAction::Scroll { .. }
        | InputAction::Key { .. }
        | InputAction::Text { .. }
        | InputAction::Hotkey { .. } => Vec::new(),
    }
}

/// 直线路径采样（含端点；步长按曼哈顿距离，上限 512 采样点）。
pub fn drag_path(from: &Point, to: &Point) -> Vec<Point> {
    let dx = to.x - from.x;
    let dy = to.y - from.y;
    let dist = dx.abs().max(dy.abs());
    let steps = dist.clamp(1, 512) as i64;
    let mut out = Vec::with_capacity((steps + 1) as usize);
    for i in 0..=steps {
        let t = i as f64 / steps as f64;
        out.push(Point {
            x: (from.x as f64 + dx as f64 * t).round() as i64,
            y: (from.y as f64 + dy as f64 * t).round() as i64,
        });
    }
    out
}

/// 解析标准键名（小写归一；不支持 → INVALID_PARAMS）。
pub fn parse_key(name: &str) -> Result<Key, IpcError> {
    let lowered = name.to_ascii_lowercase();
    match lookup_vk(&lowered) {
        Some((vk, extended)) => Ok(Key { name: lowered, vk, extended }),
        None => Err(IpcError::invalid_params(&format!("不支持的键名：{name}"))),
    }
}

/// 标准键名 → (虚拟键码, 是否扩展键)。表来源 docs/04 §鼠标与键盘（键名不区分大小写）。
pub fn lookup_vk(name: &str) -> Option<(u16, bool)> {
    // 单个可打印 ASCII 字母/数字
    let bytes = name.as_bytes();
    if bytes.len() == 1 {
        let c = bytes[0];
        if c.is_ascii_lowercase() {
            return Some((0x41 + (c - b'a') as u16, false));
        }
        if c.is_ascii_digit() {
            return Some((0x30 + (c - b'0') as u16, false));
        }
    }
    match name {
        "up" => Some((0x26, false)),
        "down" => Some((0x28, false)),
        "left" => Some((0x25, false)),
        "right" => Some((0x27, false)),
        "enter" => Some((0x0D, false)),
        "space" => Some((0x20, false)),
        "tab" => Some((0x09, false)),
        "backspace" => Some((0x08, false)),
        "delete" => Some((0x2E, true)),
        "esc" => Some((0x1B, false)),
        "ctrl" => Some((0x11, false)),
        "shift" => Some((0x10, false)),
        "alt" => Some((0x12, false)),
        "win" => Some((0x5B, true)),
        "home" => Some((0x24, true)),
        "end" => Some((0x23, true)),
        "pageup" => Some((0x21, true)),
        "pagedown" => Some((0x22, true)),
        "insert" => Some((0x2D, true)),
        "printscreen" => Some((0x2C, true)),
        _ => lookup_function_key(name),
    }
}

fn lookup_function_key(name: &str) -> Option<(u16, bool)> {
    let rest = name.strip_prefix('f')?;
    let n: u16 = rest.parse().ok()?;
    if (1..=12).contains(&n) {
        Some((0x70 + (n - 1), false))
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// 解析辅助
// ---------------------------------------------------------------------------

fn req_i64(obj: &serde_json::Map<String, Value>, key: &str, field: &str) -> Result<i64, IpcError> {
    obj.get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| IpcError::invalid_params(&format!("{field} 必须为整数")))
}

/// 读取可选时长字段（缺省取 `default`；存在则须为整数且 0..=5000）。
fn opt_duration_ms(
    obj: &serde_json::Map<String, Value>,
    key: &str,
    field: &str,
    default: u32,
) -> Result<u32, IpcError> {
    match obj.get(key) {
        None | Some(Value::Null) => Ok(default),
        Some(v) => {
            let n = v
                .as_i64()
                .ok_or_else(|| IpcError::invalid_params(&format!("{field} 必须为整数")))?;
            if n < 0 || n > MAX_DURATION_MS as i64 {
                return Err(IpcError::invalid_params(&format!(
                    "{field} 超出范围 0..={MAX_DURATION_MS}"
                )));
            }
            Ok(n as u32)
        }
    }
}

fn req_point(
    obj: &serde_json::Map<String, Value>,
    key: &str,
    field: &str,
) -> Result<Point, IpcError> {
    let inner = obj
        .get(key)
        .and_then(Value::as_object)
        .ok_or_else(|| IpcError::invalid_params(&format!("{field} 必须为对象")))?;
    let x = inner
        .get("x")
        .and_then(Value::as_i64)
        .ok_or_else(|| IpcError::invalid_params(&format!("{field}.x 必须为整数")))?;
    let y = inner
        .get("y")
        .and_then(Value::as_i64)
        .ok_or_else(|| IpcError::invalid_params(&format!("{field}.y 必须为整数")))?;
    Ok(Point { x, y })
}
