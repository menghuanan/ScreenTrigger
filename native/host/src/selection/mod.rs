//! 框选覆盖层与主窗口恢复（TASK-008，F01）。
//!
//! **职责**：
//! - `SelectionManager`：协议三命令（`start_selection_mode`/`confirm_selection`/
//!   `stop_selection_mode`）的 Rust 本地 handler（[`HostCommandHandler`]）+ 覆盖层
//!   回传助手 `report_region`；
//! - 副作用经端口注入（[`model`]）：主窗口/覆盖层/捕获/脚本/事件/时钟/睡眠；
//! - 事件经 [`RouterEventSink`] 注入 IPC 事件总线（UI seq 由总线统一重分配）。
//!
//! **接线**：桌面入口注入窗口与捕获端口，构造 [`SelectionManager`] 并
//! [`install_manager`]；`sidecar::wire_router` 取 [`shared_manager`] 注入 Router，
//! 并把当前会话的 Router 通道经 [`RouterEventSink::attach`] 交给事件出口。

pub mod controller;
pub mod display;
pub mod gdi;
pub mod geometry;
pub mod hostframe;
pub mod model;
pub mod png;
pub mod wiring;

use std::sync::{Arc, Mutex};

use serde_json::Value;

use crate::ipc::{HostCommandHandler, IpcError, RouterInput};
use crate::sidecar::protocol::{ProtocolEnvelope, SUPPORTED_PROTOCOL_VERSION};
use crate::types::region::Region;

use controller::SelectionController;
use model::{
    ClockPort, DesktopCapturePort, EventSink, HostEnvironmentPort, ScriptControlPort, Sleeper,
};

/// 框选管理器：包 [`SelectionController`]，向 Router 暴露 [`HostCommandHandler`]。
pub struct SelectionManager {
    controller: SelectionController,
}

impl SelectionManager {
    /// 装配（全部副作用端口注入）。
    pub fn new(
        env: Box<dyn HostEnvironmentPort>,
        capture: Box<dyn DesktopCapturePort>,
        script: Box<dyn ScriptControlPort>,
        events: Box<dyn EventSink>,
        clock: Box<dyn ClockPort>,
        sleeper: Box<dyn Sleeper>,
    ) -> Self {
        SelectionManager {
            controller: SelectionController::new(env, capture, script, events, clock, sleeper),
        }
    }

    /// 协议命令分派（参数校验 + 委托控制器）。返回 03 约定的 `data` 形状。
    pub fn dispatch_command(&mut self, command: &str, params: &Value) -> Result<Value, IpcError> {
        match command {
            "start_selection_mode" => match params.get("display_id") {
                None | Some(Value::Null) => self.controller.start(None),
                Some(Value::String(s)) => self.controller.start(Some(s)),
                Some(_) => Err(IpcError::invalid_params("display_id 必须为字符串")),
            },
            "confirm_selection" => {
                let selection_id = params
                    .get("selection_id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| IpcError::invalid_params("缺少 selection_id（string）"))?;
                let region_value = params
                    .get("region")
                    .cloned()
                    .ok_or_else(|| IpcError::invalid_params("缺少 region（object）"))?;
                let region: Region = serde_json::from_value(region_value).map_err(|e| {
                    IpcError::invalid_params(&format!("region 解析失败：{e}"))
                })?;
                self.controller.confirm(selection_id, &region)
            }
            "stop_selection_mode" => {
                let selection_id = params
                    .get("selection_id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| IpcError::invalid_params("缺少 selection_id（string）"))?;
                self.controller.stop(selection_id)
            }
            other => Err(IpcError::unknown_command(other)),
        }
    }

    /// 覆盖层回传选区（内部 UI↔Host 助手）：局部 DIP 两对角点 + 覆盖层物理原点/scale。
    #[allow(clippy::too_many_arguments)]
    pub fn report_region(
        &mut self,
        selection_id: &str,
        a: (f64, f64),
        b: (f64, f64),
        origin: (i64, i64),
        scale: f64,
    ) -> Result<Value, IpcError> {
        self.controller
            .update_region(selection_id, a, b, origin, scale)
    }

    /// 是否有活动框选会话（测试/诊断）。
    pub fn has_active_session(&self) -> bool {
        self.controller.has_active_session()
    }

    pub fn end_session(&mut self) {
        if let Some(id) = self.controller.active_selection_id().map(str::to_owned) {
            let _ = self.controller.stop(&id);
        }
    }
}

impl HostCommandHandler for SelectionManager {
    fn dispatch(&mut self, command: &str, params: &Value) -> Result<Value, IpcError> {
        self.dispatch_command(command, params)
    }
}

/// 共享包装：Router 与桌面入口共享同一控制器（`report_region`）。
pub struct SharedSelectionManager(pub Arc<Mutex<SelectionManager>>);

impl HostCommandHandler for SharedSelectionManager {
    fn dispatch(&mut self, command: &str, params: &Value) -> Result<Value, IpcError> {
        self.0
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .dispatch_command(command, params)
    }
}

// ---------------------------------------------------------------------------
// 进程级共享（宿主初始化 注入；sidecar wire_router 取用）
// ---------------------------------------------------------------------------

static MANAGER: std::sync::OnceLock<Arc<Mutex<SelectionManager>>> = std::sync::OnceLock::new();

/// 安装共享框选管理器（重复安装返回 false）。
pub fn install_manager(m: Arc<Mutex<SelectionManager>>) -> bool {
    MANAGER.set(m).is_ok()
}

/// 读取共享框选管理器（未安装 → None，Router 用默认 CapabilityDenied）。
pub fn shared_manager() -> Option<Arc<Mutex<SelectionManager>>> {
    MANAGER.get().cloned()
}

static EVENT_SINK: std::sync::OnceLock<Arc<RouterEventSink>> = std::sync::OnceLock::new();

/// 安装共享事件出口（宿主初始化；sidecar wire_router 经其 [`RouterEventSink::attach`]
/// 绑定当前会话通道）。
pub fn install_event_sink(sink: Arc<RouterEventSink>) -> bool {
    EVENT_SINK.set(sink).is_ok()
}

/// 读取共享事件出口。
pub fn shared_event_sink() -> Option<Arc<RouterEventSink>> {
    EVENT_SINK.get().cloned()
}

// ---------------------------------------------------------------------------
// 引擎图像引用释放（DEF-TASK008-01）：临时预览引用由引擎 SessionImageStore 拥有，
// 结束会话时经同步内部调用通道 `release_image` 归还（幂等；best-effort）。
// ---------------------------------------------------------------------------

/// 释放一张**引擎拥有**的图像引用（经 [`crate::ipc::hostcall`]，best-effort）。
/// 通道未装配（引擎未启动）时为 no-op。
pub fn release_engine_image(image_id: &str) {
    if image_id.is_empty() {
        return;
    }
    if let Some(client) = crate::ipc::hostcall::shared() {
        let params = serde_json::json!({ "image_id": image_id });
        if let Err(err) = client.call("release_image", params) {
            eprintln!(
                "[selection] 引擎图像释放失败 image_id={image_id}: {}",
                err.code
            );
        }
    }
}

/// `Arc<RouterEventSink>` 的 [`EventSink`] 适配（供管理器持有并后续 attach 目标）。
pub struct SharedEventSink(pub Arc<RouterEventSink>);

impl EventSink for SharedEventSink {
    fn emit(&self, name: &str, payload: Value) {
        self.0.emit(name, payload);
    }
}

// ---------------------------------------------------------------------------
// 事件出口：注入 Router 通道（UI seq 由 eventbus 统一重分配）
// ---------------------------------------------------------------------------

/// 事件出口：把框选事件信封送入当前会话的 Router 通道（`RouterInput::Frame`）。
/// 目标（通道 + 会话 id）由 `sidecar::wire_router` 在装配时 [`attach`]。
pub struct RouterEventSink {
    target: Mutex<Option<(std::sync::mpsc::Sender<RouterInput>, String)>>,
}

impl Default for RouterEventSink {
    fn default() -> Self {
        Self::new()
    }
}

impl RouterEventSink {
    pub fn new() -> Self {
        RouterEventSink { target: Mutex::new(None) }
    }

    /// 绑定当前会话通道（每次引擎启动/会话重建调用）。
    pub fn attach(&self, tx: std::sync::mpsc::Sender<RouterInput>, session_id: String) {
        *self.target.lock().unwrap_or_else(|p| p.into_inner()) = Some((tx, session_id));
    }

    /// 解绑（会话结束/清理）。
    pub fn detach(&self) {
        *self.target.lock().unwrap_or_else(|p| p.into_inner()) = None;
    }
}

impl EventSink for RouterEventSink {
    fn emit(&self, name: &str, payload: Value) {
        let guard = self.target.lock().unwrap_or_else(|p| p.into_inner());
        let Some((tx, session_id)) = guard.as_ref() else {
            eprintln!("[selection] 事件出口未绑定会话，丢弃事件 {name}");
            return;
        };
        let envelope = ProtocolEnvelope {
            protocol_version: SUPPORTED_PROTOCOL_VERSION,
            session_id: session_id.clone(),
            msg_type: "event".to_string(),
            id: None,
            command: None,
            params: None,
            seq: None, // UI seq 由 eventbus 在发送时刻重分配
            name: Some(name.to_string()),
            timestamp: Some(now_rfc3339_utc()),
            payload: Some(payload),
            success: None,
            data: None,
            error: None,
        };
        // 送入 Router 通道（非阻塞；Router 单线程后续消费 → eventbus → UI）
        if let Err(e) = tx.send(RouterInput::Frame(envelope)) {
            eprintln!("[selection] 事件通道已关闭，丢弃事件 {name}: {e}");
        }
    }
}

/// 当前 UTC 时间的 RFC3339（秒精度；用于事件 timestamp）。
fn now_rfc3339_utc() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (h, mi, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    let (y, m, d) = civil_from_days(days);
    format!("{y:04}-{m:02}-{d:02}T{h:02}:{mi:02}:{s:02}Z")
}

/// 天数（1970-01-01 起）→ (year, month, day)（Howard Hinnant civil_from_days）。
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod sink_tests {
    use super::*;
    use std::sync::mpsc;

    #[test]
    fn router_event_sink_sends_event_frame_with_session() {
        let (tx, rx) = mpsc::channel::<RouterInput>();
        let sink = RouterEventSink::new();
        sink.attach(tx, "session-1".to_string());
        sink.emit("selection_started", serde_json::json!({"selection_id": "s1"}));
        match rx.recv().unwrap() {
            RouterInput::Frame(env) => {
                assert_eq!(env.msg_type, "event");
                assert_eq!(env.name.as_deref(), Some("selection_started"));
                assert_eq!(env.session_id, "session-1");
                assert_eq!(env.payload.unwrap()["selection_id"], "s1");
                assert!(env.timestamp.is_some());
            }
            _ => panic!("应为 Frame"),
        }
    }

    #[test]
    fn router_event_sink_without_target_drops_silently() {
        let sink = RouterEventSink::new();
        sink.emit("selection_completed", serde_json::json!({})); // 不 panic
    }

    #[test]
    fn manager_dispatch_validates_params() {
        // 未构造真实控制器：仅验证参数校验路径不 panic 的前置（缺参）
        // 用最小假端口构造
        struct NoEnv;
        impl HostEnvironmentPort for NoEnv {
            fn list_displays(&self) -> model::PortResult<Vec<crate::types::region::DisplayInfo>> { Ok(vec![]) }
            fn main_window_rect(&self) -> model::PortResult<crate::types::region::Rect> { Ok(crate::types::region::Rect { x: 0, y: 0, width: 1, height: 1 }) }
            fn is_main_window_visible(&self) -> bool { true }
            fn set_main_window_visible(&self, _v: bool) -> model::PortResult<()> { Ok(()) }
            fn open_overlay(&self, _d: &crate::types::region::DisplayInfo, _s: &str) -> model::PortResult<model::OverlayHandle> { Ok(model::OverlayHandle("o".into())) }
            fn close_overlay(&self, _h: &model::OverlayHandle) -> model::PortResult<()> { Ok(()) }
        }
        struct NoCap;
        impl DesktopCapturePort for NoCap {
            fn capture(&self, _d: &str, _r: &crate::types::region::Rect) -> model::PortResult<crate::types::region::ImageRef> { Err(model::PortError::permanent("no")) }
            fn release(&self, _i: &str) -> model::PortResult<()> { Ok(()) }
        }
        struct NoScript;
        impl ScriptControlPort for NoScript {
            fn request_pause(&self) -> model::PortResult<model::PauseOutcome> { Ok(model::PauseOutcome::NoActiveScript) }
        }
        struct NoEvents;
        impl EventSink for NoEvents {
            fn emit(&self, _n: &str, _p: Value) {}
        }
        struct SysClock;
        impl ClockPort for SysClock {
            fn now_rfc3339(&self) -> String { "2026-10-11T00:00:00Z".to_string() }
        }
        struct SysSleeper;
        impl Sleeper for SysSleeper {
            fn sleep_ms(&self, _ms: u64) {}
        }
        let mut m = SelectionManager::new(
            Box::new(NoEnv),
            Box::new(NoCap),
            Box::new(NoScript),
            Box::new(NoEvents),
            Box::new(SysClock),
            Box::new(SysSleeper),
        );
        // confirm_selection 缺 region → INVALID_PARAMS
        let err = m
            .dispatch_command("confirm_selection", &serde_json::json!({"selection_id": "s"}))
            .unwrap_err();
        assert_eq!(err.code, "INVALID_PARAMS");
        // 表外命令 → UNKNOWN_COMMAND
        let err2 = m.dispatch_command("bogus", &serde_json::json!({})).unwrap_err();
        assert_eq!(err2.code, "UNKNOWN_COMMAND");
        // 空显示器 → start 返回 DISPLAY_NOT_FOUND
        let err3 = m.dispatch_command("start_selection_mode", &serde_json::json!({})).unwrap_err();
        assert_eq!(err3.code, "DISPLAY_NOT_FOUND");
    }
}
