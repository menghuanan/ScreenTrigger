//! 生产端口装配（TASK-017）。
//!
//! 把 [`win32`](super::win32) 原始 FFI 接成 [`InputManager`](super::InputManager) 所需的
//! 端口实现，并提供 [`build_production_manager`] 供 `lib.rs` setup 装配：
//! - [`SendInputPort`]：SendInput 绝对移动/按键/滚轮/Unicode/按钮；
//! - [`SystemPointer`]：虚拟屏/光标/前台窗口；
//! - [`SystemClock`]：单调时钟（进程内起源）；
//! - [`SystemRegionResolution`]：**TASK-017 自带的生产实现**——用 `EnumDisplayMonitors`
//!   得到**实际显示器集合**判定点是否合法（06§不变量 3）；
//!   `binding_id`/`geometry_revision` 与前台身份校验在本任务中经端口抽象**留待
//!   TASK-018 丰富**（未绑定/无 binding_id 时按“无绑定会话”通过），**本任务不实现
//!   窗口枚举**。
//!
//! **来源章节**：docs/01 §M05、docs/06 §坐标区域与输入不变量、docs/07 §5。

use std::collections::HashMap;
use std::sync::Mutex;

use crate::ipc::IpcError;

use super::geometry_guard;
use super::model::{
    ClockPort, InputPort, MonitorRect, MouseButton, PointerPort, PortError, RegionResolutionPort,
    VirtualScreen, WindowIdentity,
};
use super::win32;
use super::InputManager;

// ---------------------------------------------------------------------------
// 物理输入端口（唯一调用 SendInput 的边界）
// ---------------------------------------------------------------------------

/// SendInput 输入端口（生产）。
pub struct SendInputPort;

impl InputPort for SendInputPort {
    fn move_to(&self, x: i64, y: i64) -> Result<(), PortError> {
        win32::move_absolute(x, y).map_err(PortError::new)
    }

    fn set_button(&self, button: MouseButton, down: bool) -> Result<(), PortError> {
        win32::button(button, down).map_err(PortError::new)
    }

    fn wheel(&self, delta: i32) -> Result<(), PortError> {
        win32::wheel(delta).map_err(PortError::new)
    }

    fn key_event(&self, key: &super::model::Key, down: bool) -> Result<(), PortError> {
        win32::key(key.vk, key.extended, down).map_err(PortError::new)
    }

    fn unicode_text(&self, text: &str) -> Result<(), PortError> {
        win32::unicode_text(text).map_err(PortError::new)
    }

    fn release_keys(&self, _execution_id: &str, keys: &[super::model::Key], buttons: &[MouseButton]) {
        for button in buttons.iter().rev() {
            let _ = win32::button(*button, false);
        }
        for key in keys.iter().rev() {
            let _ = win32::key(key.vk, key.extended, false);
        }
    }
}

// ---------------------------------------------------------------------------
// 指针端口
// ---------------------------------------------------------------------------

/// 系统指针端口（生产）。
pub struct SystemPointer;

impl PointerPort for SystemPointer {
    fn virtual_screen(&self) -> VirtualScreen {
        win32::virtual_screen()
    }

    fn foreground_window(&self) -> WindowIdentity {
        WindowIdentity {
            handle: win32::foreground_window(),
            pid: 0,
            process_started_at: String::new(),
        }
    }

    fn cursor_pos(&self) -> super::model::Point {
        win32::cursor_pos()
    }
}

// ---------------------------------------------------------------------------
// 时钟端口
// ---------------------------------------------------------------------------

/// 单调时钟端口（进程内起源；不受系统时间调整影响）。
pub struct SystemClock {
    origin: std::time::Instant,
}

impl SystemClock {
    /// 以当前时刻为起源构造。
    pub fn new() -> Self {
        SystemClock { origin: std::time::Instant::now() }
    }
}

impl Default for SystemClock {
    fn default() -> Self {
        Self::new()
    }
}

impl ClockPort for SystemClock {
    fn now_ms(&self) -> u64 {
        self.origin.elapsed().as_millis() as u64
    }
}

// ---------------------------------------------------------------------------
// 区域/几何/前台复核端口（TASK-017 生产实现；TASK-018 将替换/扩展）
// ---------------------------------------------------------------------------

/// 系统区域复核端口（生产）。
///
/// **TASK-017 自带、可独立工作**：`check_point` 基于 `EnumDisplayMonitors` 的**实际
/// 显示器集合**判定点是否合法（06§不变量 3）。
///
/// **TASK-018 将替换/扩展该端口实现**：本任务不实现窗口枚举，故 `check_geometry`
/// 与 `foreground_matches` 在“无绑定会话”（binding_id 空）下按通过处理；已知
/// geometry_revision 的旧帧拒绝逻辑由 TASK-018 经 `latest_revision` 注入。
pub struct SystemRegionResolution {
    monitors: Box<dyn Fn() -> Vec<MonitorRect> + Send + Sync>,
    latest_revision: Mutex<HashMap<String, i64>>,
}

impl SystemRegionResolution {
    /// 生产构造：真实显示器枚举。
    pub fn new() -> Self {
        SystemRegionResolution {
            monitors: Box::new(win32::enum_monitors),
            latest_revision: Mutex::new(HashMap::new()),
        }
    }

    /// 测试构造：注入显示器集合提供器（确定性）。
    pub fn with_monitors(
        monitors: Box<dyn Fn() -> Vec<MonitorRect> + Send + Sync>,
    ) -> Self {
        SystemRegionResolution { monitors, latest_revision: Mutex::new(HashMap::new()) }
    }
}

impl Default for SystemRegionResolution {
    fn default() -> Self {
        Self::new()
    }
}

impl RegionResolutionPort for SystemRegionResolution {
    fn check_point(
        &self,
        x: i64,
        y: i64,
        ctx: &super::model::InputContext,
    ) -> Result<(), IpcError> {
        let monitors = (self.monitors)();
        geometry_guard::evaluate_point(x, y, ctx, &monitors)
    }

    fn check_geometry(&self, binding_id: &str, geometry_revision: i64) -> Result<(), IpcError> {
        // 未绑定/无 binding_id → 无绑定会话，通过（不提前实现 TASK-018 窗口枚举）。
        if binding_id.is_empty() {
            return Ok(());
        }
        let known = self
            .latest_revision
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .get(binding_id)
            .copied();
        geometry_guard::evaluate_geometry(binding_id, geometry_revision, known)
    }

    fn foreground_matches(&self, _identity: &WindowIdentity) -> bool {
        // TASK-018 将替换/扩展：本任务无窗口枚举，无法核对前台身份 → 视为匹配。
        true
    }
}

// ---------------------------------------------------------------------------
// 装配
// ---------------------------------------------------------------------------

/// 用生产端口构造共享输入管理器（`lib.rs` setup 调用）。
pub fn build_production_manager() -> std::sync::Arc<Mutex<InputManager>> {
    let manager = InputManager::new(
        Box::new(SendInputPort),
        Box::new(SystemPointer),
        Box::new(SystemRegionResolution::new()),
        Box::new(SystemClock::new()),
    );
    std::sync::Arc::new(Mutex::new(manager))
}
