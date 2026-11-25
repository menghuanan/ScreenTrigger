//! 输入模块共享模型与端口 trait（TASK-017 / M05）。
//!
//! **职责**：定义冻结的 [`InputAction`]（7 种 kind，字段逐字对齐 docs/03
//! §全双工与宿主调用）、宿主调用信封载荷（[`ActionAck`]）、租约与上下文
//! （[`LeaseId`]/[`Lease`]/[`InputContext`]），以及把一切副作用隔离到可注入端口
//! （[`InputPort`]/[`PointerPort`]/[`RegionResolutionPort`]/[`ClockPort`]）。
//!
//! **来源章节**：`docs/01`§M05、`docs/02`「显示器、窗口与运行辅助类型」+
//! 「通用值与单位」、`docs/03`§全双工与宿主调用、`docs/04`§鼠标与键盘、
//! `docs/06`§坐标区域与输入不变量、`docs/07`§5。
//!
//! **安全边界**：本模块只做**校验**与**端口调用编排**；不做坐标钳制（clamp）、
//! 不自动重试动作。所有坐标单位 `screen_physical`（半开矩形，可负）。

use serde_json::Value;

use crate::ipc::IpcError;
use crate::types::region::{validate_region, Region};

// ---------------------------------------------------------------------------
// 常量（全部来自 02/04/06/07 已冻结数值，不发明）
// ---------------------------------------------------------------------------

/// 单次动作并发根租约上限（06：3 活动根执行）。
pub const MAX_ROOT_LEASES: usize = 3;

/// 动作时长上限（02 `input.move_duration_ms` 0..5000；04 move/drag 0–5s）。
pub const MAX_DURATION_MS: u32 = 5_000;

/// 文本 Unicode 字符上限（04：≤10000 Unicode 字符）。
pub const MAX_TEXT_CHARS: usize = 10_000;

/// 组合键键数上限（04：1–5 个不同标准键名）。
pub const MAX_HOTKEY_KEYS: usize = 5;

/// 滚轮增量下界（04：-100..100）。
pub const SCROLL_DELTA_MIN: i32 = -100;

/// 滚轮增量上界（04：-100..100）。
pub const SCROLL_DELTA_MAX: i32 = 100;

/// 一次点击的点击数上限（04：click_at 允许 1–2）。
pub const MAX_CLICKS: u32 = 2;

/// 长移动/拖拽切片上界毫秒（07§5：逐采样检查取消）。
pub const MOVE_SLICE_MS: u32 = 50;

// ---------------------------------------------------------------------------
// 基础几何/输入枚举
// ---------------------------------------------------------------------------

/// 鼠标按键（03 `click.button`：left|right|middle）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MouseButton {
    Left,
    Right,
    Middle,
}

impl MouseButton {
    /// 解析 03 冻结字符串；未知返回 None（由调用方转 INVALID_PARAMS）。
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "left" => Some(MouseButton::Left),
            "right" => Some(MouseButton::Right),
            "middle" => Some(MouseButton::Middle),
            _ => None,
        }
    }

    /// 03 冻结字符串表示。
    pub fn as_str(self) -> &'static str {
        match self {
            MouseButton::Left => "left",
            MouseButton::Right => "right",
            MouseButton::Middle => "middle",
        }
    }
}

/// 键盘状态（03 `key.state`：tap|down|up）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyState {
    Tap,
    Down,
    Up,
}

impl KeyState {
    /// 解析 03 冻结字符串。
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "tap" => Some(KeyState::Tap),
            "down" => Some(KeyState::Down),
            "up" => Some(KeyState::Up),
            _ => None,
        }
    }
}

/// 标准键：名称（小写归一）+ 虚拟键码 + 是否扩展键（07§5 组合键账本）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Key {
    pub name: String,
    pub vk: u16,
    pub extended: bool,
}

/// 物理屏幕点（`screen_physical`，可负）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Point {
    pub x: i64,
    pub y: i64,
}

/// 虚拟屏幕矩形（`SM_X/YVIRTUALSCREEN`/`SM_CX/CYVIRTUALSCREEN`，单位物理像素）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VirtualScreen {
    pub x: i64,
    pub y: i64,
    pub cx: i64,
    pub cy: i64,
}

/// 单台实际显示器矩形（06§不变量 3：点须落在实际显示器集合，非虚拟包围盒）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MonitorRect {
    pub x: i64,
    pub y: i64,
    pub width: i64,
    pub height: i64,
}

/// 窗口身份元组（02：句柄+PID+进程启动时间；TASK-018 权威）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowIdentity {
    pub handle: u64,
    pub pid: u32,
    pub process_started_at: String,
}

// ---------------------------------------------------------------------------
// 动作与信封
// ---------------------------------------------------------------------------

/// 输入动作（03 逐字；带 `kind` 的对象，7 种）。
#[derive(Debug, Clone, PartialEq)]
pub enum InputAction {
    /// `{x,y,duration_ms}`。
    Move { x: i64, y: i64, duration_ms: u32 },
    /// `{x,y,button,clicks}`。
    Click { x: i64, y: i64, button: MouseButton, clicks: u32 },
    /// `{from:{x,y},to:{x,y},duration_ms}`。
    Drag { from: Point, to: Point, duration_ms: u32 },
    /// `{delta}`。
    Scroll { delta: i32 },
    /// `{key,state:tap|down|up}`。
    Key { key: Key, state: KeyState },
    /// `{text,interval_ms}`。
    Text { text: String, interval_ms: u32 },
    /// `{keys:[string]}`。
    Hotkey { keys: Vec<Key> },
}

impl InputAction {
    /// 动作 kind 标签（日志用；不记录参数全文）。
    pub fn kind_str(&self) -> &'static str {
        match self {
            InputAction::Move { .. } => "move",
            InputAction::Click { .. } => "click",
            InputAction::Drag { .. } => "drag",
            InputAction::Scroll { .. } => "scroll",
            InputAction::Key { .. } => "key",
            InputAction::Text { .. } => "text",
            InputAction::Hotkey { .. } => "hotkey",
        }
    }
}

/// 输入响应 data（03：`{action_id,dispatched:true}`；仅表示调用完成）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActionAck {
    pub action_id: String,
    pub dispatched: bool,
}

impl ActionAck {
    /// 派发完成 ack（`dispatched=true`）。
    pub fn dispatched(action_id: &str) -> Self {
        ActionAck { action_id: action_id.to_string(), dispatched: true }
    }

    /// 序列化为 03 data 形状。
    pub fn to_value(&self) -> Value {
        serde_json::json!({ "action_id": self.action_id, "dispatched": self.dispatched })
    }
}

// ---------------------------------------------------------------------------
// 租约与上下文
// ---------------------------------------------------------------------------

/// 租约 id（非空不透明字符串 `"lease-"+uuid4`；02 非持久实体）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LeaseId(pub String);

impl LeaseId {
    /// 生成新租约 id。
    pub fn generate() -> Self {
        LeaseId(format!("lease-{}", uuid::Uuid::new_v4()))
    }

    /// 字符串视图。
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// 租约上下文（03 `acquire_input_lease.context`）。
#[derive(Debug, Clone, PartialEq)]
pub struct InputContext {
    /// 区域（`screen_physical`；null = 选定显示器全屏或绑定客户区）。
    pub region: Option<Region>,
    /// 绑定窗口（string 或 null）。
    pub binding_id: Option<String>,
    /// 显示器（string 或 null）。
    pub display_id: Option<String>,
    /// 是否允许输入离开区域（02：只影响输入，不扩大识别区）。
    pub allow_exit: bool,
}

impl Default for InputContext {
    fn default() -> Self {
        InputContext { region: None, binding_id: None, display_id: None, allow_exit: false }
    }
}

impl InputContext {
    /// 解析 03 `context` 对象（缺省/ null → 默认；未知键 → INVALID_PARAMS）。
    pub fn parse(value: Option<&Value>) -> Result<Self, IpcError> {
        let Some(value) = value else {
            return Ok(InputContext::default());
        };
        if value.is_null() {
            return Ok(InputContext::default());
        }
        let obj = value
            .as_object()
            .ok_or_else(|| IpcError::invalid_params("context 必须为对象"))?;
        for key in obj.keys() {
            if !matches!(key.as_str(), "region" | "binding_id" | "display_id" | "allow_exit") {
                return Err(IpcError::invalid_params(&format!("context.{key}")));
            }
        }
        let region = match obj.get("region") {
            None | Some(Value::Null) => None,
            Some(rv) => {
                validate_region(rv)
                    .map_err(|e| IpcError::invalid_params(&format!("context.region.{}", e.field)))?;
                Some(serde_json::from_value::<Region>(rv.clone()).map_err(|e| {
                    IpcError::invalid_params(&format!("context.region 解析失败：{e}"))
                })?)
            }
        };
        let binding_id = parse_opt_string(obj.get("binding_id"), "context.binding_id")?;
        let display_id = parse_opt_string(obj.get("display_id"), "context.display_id")?;
        let allow_exit = match obj.get("allow_exit") {
            None | Some(Value::Null) => false,
            Some(Value::Bool(b)) => *b,
            Some(_) => return Err(IpcError::invalid_params("context.allow_exit 必须为布尔")),
        };
        Ok(InputContext { region, binding_id, display_id, allow_exit })
    }
}

fn parse_opt_string(value: Option<&Value>, field: &str) -> Result<Option<String>, IpcError> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if !s.is_empty() => Ok(Some(s.clone())),
        Some(_) => Err(IpcError::invalid_params(&format!("{field} 必须为非空字符串或 null"))),
    }
}

/// 活动租约（非 02 持久实体，内存态）。
#[derive(Debug, Clone, PartialEq)]
pub struct Lease {
    pub lease_id: LeaseId,
    pub execution_id: String,
    pub context: InputContext,
}

// ---------------------------------------------------------------------------
// 端口错误
// ---------------------------------------------------------------------------

/// 输入端口错误（SendInput 失败/不完整 → 06 `INPUT_FAILED`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PortError {
    pub message: String,
}

impl PortError {
    /// 构造端口错误。
    pub fn new(message: impl Into<String>) -> Self {
        PortError { message: message.into() }
    }
}

/// 端口错误别名（类图命名 `InputError`）。
pub type InputError = PortError;

// ---------------------------------------------------------------------------
// 端口 trait（可测性核心：副作用全部经注入）
// ---------------------------------------------------------------------------

/// 物理输入端口（唯一可调用 SendInput 的边界；生产实现在 `win32.rs`/`wiring.rs`）。
///
/// 采用**切片级原语**而非“整动作一次派发”，以满足 07§5 的“长移动/拖拽逐片检查
/// 取消”（切片编排在 `dispatch.rs`，端口保持无状态）。
pub trait InputPort: Send + Sync {
    /// 绝对移动光标到 `(x,y)`（`screen_physical`；归一化在端口内）。
    fn move_to(&self, x: i64, y: i64) -> Result<(), PortError>;

    /// 设置鼠标按钮按下/抬起。
    fn set_button(&self, button: MouseButton, down: bool) -> Result<(), PortError>;

    /// 滚轮（正数向上，单位格）。
    fn wheel(&self, delta: i32) -> Result<(), PortError>;

    /// 键盘虚拟键按下/抬起。
    fn key_event(&self, key: &Key, down: bool) -> Result<(), PortError>;

    fn externally_held(&self, _vk: u16) -> bool { false }

    /// Unicode 文本输入（UTF-16 单元逐个，走 SendInput Unicode 路径）。
    fn unicode_text(&self, text: &str) -> Result<(), PortError>;

    /// 释放本程序持有（账本驱动）的键/按钮（逆序；best-effort）。
    fn release_keys(&self, execution_id: &str, keys: &[Key], buttons: &[MouseButton]);
}

/// 指针/虚拟屏/前台查询端口。
pub trait PointerPort: Send {
    /// 虚拟屏幕矩形（`SM_*` 四项）。
    fn virtual_screen(&self) -> VirtualScreen;

    /// 当前前台窗口身份（无绑定会话下 handle 可为 0）。
    fn foreground_window(&self) -> WindowIdentity;

    /// 当前光标位置（`screen_physical`）。
    fn cursor_pos(&self) -> Result<Point, IpcError>;
}

/// 区域/窗口/几何复核端口（TASK-018 提供权威；TASK-017 提供生产实现，见 `wiring.rs`）。
pub trait RegionResolutionPort: Send {
    /// 单点判定：须落在实际显示器集合，且（严格模式）落在区域矩形内。
    fn check_point(&self, x: i64, y: i64, ctx: &InputContext) -> Result<(), IpcError>;

    fn check_path(&self, from: Point, to: Point, ctx: &InputContext) -> Result<(), IpcError> {
        self.check_point(from.x, from.y, ctx)?;
        self.check_point(to.x, to.y, ctx)?;
        super::geometry_guard::walk_segment(from, to, |p| self.check_point(p.x, p.y, ctx))
    }

    /// `geometry_revision` 一致性（旧帧拒绝 → `STALE_FRAME`）。
    fn check_geometry(&self, binding_id: &str, geometry_revision: i64) -> Result<(), IpcError>;

    /// 前台窗口身份是否匹配绑定（TASK-018 权威）。
    fn foreground_matches(&self, identity: &WindowIdentity) -> bool;
}

/// 单调时钟端口（06：所有持续时间用单调时钟，不受系统时间调整影响）。
pub trait ClockPort: Send {
    /// 单调毫秒（进程内起源；仅用于日志/时间关联）。
    fn now_ms(&self) -> u64;
}
