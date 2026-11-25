//! Trusted window adapter for the in-process WinUI client. Calls never enter the public IPC registry.
use crate::ipc::{hostcall, IpcError};
use crate::selection::{self, hostframe, model::*, wiring, RouterEventSink, SharedEventSink};
use crate::types::region::{DisplayInfo, Rect};
use serde_json::{json, Value};
use std::sync::{Arc, OnceLock, RwLock};

pub type WindowCallback = extern "C" fn(*const u8, usize, *mut u8, usize) -> i32;
static CALLBACK: RwLock<Option<WindowCallback>> = RwLock::new(None);
static IMPORT: OnceLock<crate::import::ImportTokenIssuer> = OnceLock::new();

pub fn attach(callback: WindowCallback) {
    *CALLBACK.write().unwrap_or_else(|p| p.into_inner()) = Some(callback);
    crate::hotkey::start();
    let issuer = IMPORT.get_or_init(crate::import::ImportTokenIssuer::ephemeral);
    crate::import::install_shared_secret_hex(issuer.secret_hex());
    let frames = hostframe::HostFrameIssuer::ephemeral();
    hostframe::install_shared_secret(frames.secret().to_vec(), frames.ttl_seconds());
    if selection::shared_manager().is_none() {
        let sink = Arc::new(RouterEventSink::new());
        selection::install_event_sink(sink.clone());
        let (manager, _) = wiring::build_production_manager(
            Box::new(Windows),
            Box::new(Windows),
            Box::new(SharedEventSink(sink)),
        );
        selection::install_manager(manager);
    }
}

pub fn attached() -> bool {
    CALLBACK.read().unwrap_or_else(|p| p.into_inner()).is_some()
}

pub fn detach() {
    CALLBACK.write().unwrap_or_else(|p| p.into_inner()).take();
}

fn ui(request: Value) -> PortResult<Value> {
    let guard = CALLBACK.read().unwrap_or_else(|p| p.into_inner());
    let callback = guard.ok_or_else(|| PortError::permanent("应用窗口已关闭"))?;
    let data = serde_json::to_vec(&request).map_err(|e| PortError::permanent(&e.to_string()))?;
    let mut buffer = vec![0u8; 128 * 1024]; // A system picker can return a long UTF-8 path.
    let n = callback(data.as_ptr(), data.len(), buffer.as_mut_ptr(), buffer.len());
    if n <= 0 || n as usize > buffer.len() {
        return Err(PortError::permanent("窗口操作未完成"));
    }
    let result: Value = serde_json::from_slice(&buffer[..n as usize])
        .map_err(|e| PortError::permanent(&e.to_string()))?;
    if let Some(message) = result["error"].as_str() {
        return Err(PortError::permanent(message));
    }
    Ok(result["data"].clone())
}

pub fn pick_image(session: &str) -> Result<Value, IpcError> {
    let path = ui(json!({"op":"pick_image"})).map_err(|e| IpcError::internal(&e.message))?;
    let token = match path.as_str() {
        None if path.is_null() => return Ok(json!({"import_token":null})),
        Some(path) => IMPORT
            .get()
            .ok_or_else(|| IpcError::capability_denied("import_image"))?
            .issue(session, path),
        _ => return Err(IpcError::invalid_params("系统文件选择器未返回路径")),
    };
    Ok(json!({"import_token":token}))
}

pub fn prepare_capture(rect: &Rect) -> PortResult<()> {
    ui(json!({"op":"prepare_capture","region":rect})).map(drop)
}
pub fn end_capture() -> PortResult<()> {
    ui(json!({"op":"end_capture"})).map(drop)
}

fn engine(command: &str) -> PortResult<Value> {
    hostcall::shared()
        .ok_or_else(|| PortError::permanent("引擎未连接"))?
        .call(command, json!({}))
        .map_err(|e| PortError::permanent(&e.message))
}

struct Windows;
impl ScriptControlPort for Windows {
    fn request_pause(&self) -> PortResult<PauseOutcome> {
        if let Some(gate) = crate::input::shared_gate() {
            for root in gate.active(None) {
                gate.pause(&root);
            }
            crate::input::release_closed();
        }
        let data = engine("pause_for_selection")?;
        Ok(if data["paused"].as_bool() == Some(true) {
            PauseOutcome::Paused
        } else {
            PauseOutcome::NoActiveScript
        })
    }
}
impl HostEnvironmentPort for Windows {
    fn list_displays(&self) -> PortResult<Vec<DisplayInfo>> {
        serde_json::from_value(engine("get_display_list")?["displays"].clone())
            .map_err(|e| PortError::permanent(&e.to_string()))
    }
    fn main_window_rect(&self) -> PortResult<Rect> {
        serde_json::from_value(ui(json!({"op":"rect"}))?)
            .map_err(|e| PortError::permanent(&e.to_string()))
    }
    fn is_main_window_visible(&self) -> bool {
        ui(json!({"op":"visible"})).ok() == Some(Value::Bool(true))
    }
    fn set_main_window_visible(&self, visible: bool) -> PortResult<()> {
        ui(json!({"op":"set_visible","visible":visible})).map(drop)
    }
    fn open_overlay(&self, display: &DisplayInfo, id: &str) -> PortResult<OverlayHandle> {
        ui(json!({"op":"open_overlay","display":display,"selection_id":id}))?;
        Ok(OverlayHandle(id.to_owned()))
    }
    fn close_overlay(&self, handle: &OverlayHandle) -> PortResult<()> {
        ui(json!({"op":"close_overlay","selection_id":handle.0})).map(drop)
    }
}
