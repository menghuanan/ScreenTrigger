//! Opt-in Win32 message delivery for one explicitly bound window.
use super::{dispatch::DispatchPipeline, model::InputContext};
use crate::{ipc::IpcError, window::Entry};
use serde_json::{json, Value};

#[cfg(windows)]
#[path = "background_win32.rs"]
mod windows;

const ACTIONS: [&str; 5] = ["click", "drag", "scroll", "key", "hotkey"];

pub fn capabilities(binding: &str, target: &Entry) -> Value {
    #[cfg(windows)]
    let response = windows::declared(target);
    #[cfg(not(windows))]
    let response: Result<Option<u32>, String> = Err("当前平台不支持绑定窗口消息".into());
    let mut states = serde_json::Map::new();
    for (i, name) in ACTIONS.iter().enumerate() {
        let state = match response {
            Ok(Some(mask)) if mask & (1 << i) != 0 => "available",
            Ok(None) => "unknown",
            _ => "unavailable",
        };
        states.insert((*name).into(), json!(state));
    }
    let reason = match response {
        Err(e) => Some(e),
        Ok(None) => Some("该窗口尚未声明对后台消息的支持".to_string()),
        Ok(Some(_)) => None,
    };
    json!({"binding_id":binding,"input":states,"reason":reason})
}

pub fn pipeline(context: &InputContext) -> Result<DispatchPipeline, IpcError> {
    #[cfg(windows)]
    {
        windows::pipeline(context)
    }
    #[cfg(not(windows))]
    {
        let _ = context;
        Err(IpcError::capability_denied("当前平台不支持绑定窗口消息"))
    }
}
