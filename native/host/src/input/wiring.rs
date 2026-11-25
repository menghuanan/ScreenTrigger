//! 生产端口装配（TASK-017）。
//!
//! 把 [`win32`](super::win32) 原始 FFI 接成 [`InputManager`](super::InputManager) 所需的
//! 端口实现，并提供 [`build_production_manager`] 供 `lib.rs` setup 装配：
//! - [`SendInputPort`]：SendInput 绝对移动/按键/滚轮/Unicode/按钮；
//! - [`SystemPointer`]：虚拟屏/光标/前台窗口；
//! - [`SystemClock`]：单调时钟（进程内起源）；
//! - [`SystemRegionResolution`]：实际显示器、绑定客户区、前台身份和几何修订校验。

use super::model::{InputContext, Point};
use crate::window::{self, Entry, Windows};
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

    fn externally_held(&self, vk: u16) -> bool {
        win32::key_is_down(vk)
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

    fn release_keys(
        &self,
        _execution_id: &str,
        keys: &[super::model::Key],
        buttons: &[MouseButton],
    ) {
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
        let hwnd = win32::foreground_window();
        let actual = window::system::observe(hwnd as isize);
        WindowIdentity {
            handle: hwnd,
            pid: actual.as_ref().map_or(0, |w| w.identity.pid),
            process_started_at: actual.map(|w| w.identity.started).unwrap_or_default(),
        }
    }

    fn cursor_pos(&self) -> Result<super::model::Point, IpcError> {
        win32::cursor_pos().ok_or_else(|| IpcError::window_unavailable("Windows 无法读取光标位置"))
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
        SystemClock {
            origin: std::time::Instant::now(),
        }
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

/// Snapshot client geometry and display membership for a whole motion segment.
pub struct SystemRegionResolution {
    monitors: Box<dyn Fn() -> Vec<MonitorRect> + Send + Sync>,
    windows: Option<Windows>,
    physical: bool,
}
impl Default for SystemRegionResolution {
    fn default() -> Self {
        Self::new()
    }
}
impl SystemRegionResolution {
    pub fn new() -> Self {
        Self {
            monitors: Box::new(win32::enum_monitors),
            windows: window::shared(),
            physical: true,
        }
    }
    pub fn with_monitors(monitors: Box<dyn Fn() -> Vec<MonitorRect> + Send + Sync>) -> Self {
        Self {
            monitors,
            windows: None,
            physical: false,
        }
    }
    fn binding(&self, id: &str) -> Result<Entry, IpcError> {
        let entry = self
            .windows
            .as_ref()
            .ok_or_else(|| IpcError::window_not_found("窗口服务不可用"))?
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .bound(id)?;
        if !entry.live.visible || !entry.live.foreground || entry.live.minimized {
            return Err(IpcError::window_unavailable(
                "请手动恢复绑定窗口并使其位于前台",
            ));
        }
        Ok(entry)
    }
}
impl RegionResolutionPort for SystemRegionResolution {
    fn check_point(&self, x: i64, y: i64, ctx: &InputContext) -> Result<(), IpcError> {
        self.check_path(Point { x, y }, Point { x, y }, ctx)
    }
    fn check_path(&self, from: Point, to: Point, ctx: &InputContext) -> Result<(), IpcError> {
        use crate::types::region::RegionSpace;
        let monitors = (self.monitors)();
        let mut effective = ctx.clone();
        let bound = ctx
            .binding_id
            .as_deref()
            .map(|b| self.binding(b))
            .transpose()?;
        if let Some(r) = effective.region.as_mut() {
            match r.space {
                RegionSpace::WindowClientPhysical
                    if r.binding_id == ctx.binding_id && bound.is_some() =>
                {
                    let rect = &bound.as_ref().unwrap().live.rect;
                    if r.x < 0
                        || r.y < 0
                        || r.x.saturating_add(r.width) > rect.width
                        || r.y.saturating_add(r.height) > rect.height
                    {
                        return Err(IpcError::region_limit_violation(
                            "输入区域超出绑定窗口客户区",
                        ));
                    }
                    r.x += rect.x;
                    r.y += rect.y;
                }
                RegionSpace::ScreenPhysical if r.display_id == ctx.display_id || !self.physical => {
                }
                _ => {
                    return Err(IpcError::invalid_params(
                        "输入区域与执行的窗口或显示器不一致",
                    ))
                }
            }
        }
        let check = |p: Point| -> Result<(), IpcError> {
            geometry_guard::evaluate_point(p.x, p.y, &effective, &monitors)?;
            if let Some(w) = bound.as_ref() {
                let r = &w.live.rect;
                if p.x < r.x || p.y < r.y || p.x >= r.x + r.width || p.y >= r.y + r.height {
                    return Err(IpcError::region_limit_violation("输入越出绑定窗口客户区"));
                }
            }
            Ok(())
        };
        for p in [from, to] {
            check(p)?;
            if let Some(w) = bound.as_ref() {
                if window::system::window_at(p.x, p.y) != Some(w.live.identity.hwnd) {
                    return Err(IpcError::window_unavailable("绑定窗口的操作点被遮挡"));
                }
            } else if self.physical
                && window::system::monitor_at(p.x, p.y).as_deref() != ctx.display_id.as_deref()
            {
                return Err(IpcError::region_limit_violation("输入点不在选定显示器"));
            }
        }
        // A single display and a client/ROI rectangle are convex. The monitor union can contain holes.
        geometry_guard::walk_segment(from, to, check)
    }
    fn check_geometry(&self, id: &str, revision: i64) -> Result<(), IpcError> {
        let current = if id.is_empty() {
            0
        } else {
            self.binding(id)?.revision
        };
        geometry_guard::evaluate_geometry(id, revision, Some(current))
    }
    fn foreground_matches(&self, id: &WindowIdentity) -> bool {
        self.windows.as_ref().is_some_and(|windows| {
            windows
                .lock()
                .unwrap_or_else(|p| p.into_inner())
                .matches_foreground(id)
        })
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
