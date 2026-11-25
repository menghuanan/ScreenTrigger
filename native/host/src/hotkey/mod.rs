//! All RegisterHotKey calls and WM_HOTKEY delivery belong to the same native thread.
use crate::ipc::IpcError;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

const ROLES: [&str; 3] = ["start_selection", "pause_script", "stop_script"];
const DEFAULTS: [&str; 3] = ["Ctrl+Alt+S", "Ctrl+Alt+P", "Ctrl+Alt+X"];
type Chord = (u32, u16);
type Bindings = HashMap<Chord, String>;
type Reply = mpsc::Sender<Result<(), IpcError>>;
enum Message {
    Prepare(Bindings, Reply),
    Finish(bool, Reply),
    Clear(Reply),
    Exit,
}
struct Service {
    sender: mpsc::Sender<Message>,
}
static SERVICE: Mutex<Option<Service>> = Mutex::new(None);
static SELECTED: Mutex<Option<(String, String)>> = Mutex::new(None);
pub static TRANSACTION: Mutex<()> = Mutex::new(());

fn chord(spec: &str) -> Result<Chord, IpcError> {
    let parts: Vec<_> = spec
        .split('+')
        .map(|s| s.trim().to_ascii_lowercase())
        .collect();
    let Some((key, modifiers)) = parts.split_last() else {
        return Err(IpcError::invalid_params("热键不能为空"));
    };
    let mut flags = 0;
    for token in modifiers {
        let flag = match token.as_str() {
            "alt" => 1,
            "ctrl" => 2,
            "shift" => 4,
            "win" => 8,
            _ => return Err(IpcError::invalid_params("热键修饰键不受支持")),
        };
        if flags & flag != 0 {
            return Err(IpcError::invalid_params("热键含重复修饰键"));
        }
        flags |= flag;
    }
    let (vk, _) = crate::input::action::lookup_vk(key)
        .ok_or_else(|| IpcError::invalid_params("热键主键不受支持"))?;
    Ok((flags, vk))
}
pub fn defaults() -> Value {
    json!({"start_selection":DEFAULTS[0],"pause_script":DEFAULTS[1],"stop_script":DEFAULTS[2]})
}
pub fn select(session: &str, id: Option<String>) {
    *SELECTED.lock().unwrap_or_else(|e| e.into_inner()) = id.map(|id| (session.to_owned(), id));
}
pub fn prepare(config: &Value) -> Result<(), IpcError> {
    let mut wanted = Bindings::new();
    for role in ROLES {
        let text = config[role]
            .as_str()
            .ok_or_else(|| IpcError::invalid_params("热键配置缺少操作或组合键"))?;
        if wanted.insert(chord(text)?, role.into()).is_some() {
            return Err(conflict("两个操作使用了相同组合键"));
        }
    }
    exchange(|reply| Message::Prepare(wanted, reply))
}
fn conflict(message: &str) -> IpcError {
    IpcError::new("HOTKEY_CONFLICT", message, false)
}
pub fn finish(commit: bool) -> Result<(), IpcError> {
    exchange(|r| Message::Finish(commit, r))
}
fn exchange(build: impl FnOnce(Reply) -> Message) -> Result<(), IpcError> {
    let (tx, rx) = mpsc::channel();
    let sender = SERVICE
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .as_ref()
        .map(|s| s.sender.clone())
        .ok_or_else(|| IpcError::capability_denied("hotkeys"))?;
    sender
        .send(build(tx))
        .map_err(|_| IpcError::engine_unavailable("热键消息线程已退出"))?;
    rx.recv_timeout(Duration::from_secs(2))
        .map_err(|_| IpcError::engine_unavailable("热键消息线程无响应"))?
}
pub fn start() {
    let mut service = SERVICE.lock().unwrap_or_else(|e| e.into_inner());
    if service.is_some() {
        return;
    }
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || message_loop(rx));
    *service = Some(Service { sender: tx });
}
pub fn shutdown() {
    if let Some(s) = SERVICE.lock().unwrap_or_else(|e| e.into_inner()).take() {
        let _ = s.sender.send(Message::Exit);
    }
    *SELECTED.lock().unwrap_or_else(|e| e.into_inner()) = None;
}
pub fn clear() {
    let _ = exchange(Message::Clear);
}
fn message_loop(rx: mpsc::Receiver<Message>) {
    let mut registered: HashMap<Chord, i32> = HashMap::new();
    let mut active = Bindings::new();
    let mut pending = None;
    let mut next = 0;
    loop {
        while let Some(id) = os::pop() {
            if let Some(role) = registered
                .iter()
                .find(|(_, v)| **v == id)
                .and_then(|(c, _)| active.get(c))
            {
                fire(role);
            }
        }
        match rx.recv_timeout(Duration::from_millis(8)) {
            Ok(Message::Prepare(wanted, reply)) => {
                if pending.is_some() {
                    let _ = reply.send(Err(IpcError::busy()));
                    continue;
                }
                let mut added = Vec::new();
                let mut result = Ok(());
                for (&c, role) in &wanted {
                    if registered.contains_key(&c) {
                        continue;
                    }
                    next = next % 0xbfff + 1;
                    while registered.values().any(|&id| id == next) {
                        next = next % 0xbfff + 1;
                    }
                    if !os::register(next, c) {
                        result = Err(conflict(&format!("无法注册 {role} 的快捷键，请更换组合")));
                        break;
                    }
                    registered.insert(c, next);
                    added.push(c);
                }
                if result.is_ok() {
                    pending = Some(wanted);
                } else {
                    for c in added {
                        if let Some(id) = registered.remove(&c) {
                            os::unregister(id);
                        }
                    }
                }
                let _ = reply.send(result);
            }
            Ok(Message::Finish(commit, reply)) => {
                if let Some(new) = pending.take() {
                    if commit {
                        active = new;
                    }
                }
                registered.retain(|c, id| {
                    let keep = active.contains_key(c);
                    if !keep {
                        os::unregister(*id);
                    }
                    keep
                });
                let _ = reply.send(Ok(()));
            }
            Ok(Message::Clear(reply)) => {
                for (_, id) in registered.drain() {
                    os::unregister(id);
                }
                active.clear();
                pending = None;
                let _ = reply.send(Ok(()));
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Ok(Message::Exit) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    for (_, id) in registered {
        os::unregister(id);
    }
}
fn fire(role: &str) {
    let Some(host) = crate::ipc::hostcall::shared() else {
        return;
    };
    let session = host.session_id();
    match role {
        "stop_script" => {
            let Some(gate) = crate::input::shared_gate() else {
                return;
            };
            let epoch = gate.stop_active();
            // The physical gate is already closed, even if Lua or the IPC reader is blocked.
            crate::input::release_closed();
            std::thread::spawn(move || {
                if host.call("stop_active_executions", json!({})).is_ok() {
                    gate.finish_stop(epoch);
                }
            });
        }
        "pause_script" => {
            let selected = SELECTED.lock().unwrap_or_else(|e| e.into_inner()).clone();
            if let Some((_, id)) = selected.filter(|(s, _)| s == &session) {
                std::thread::spawn(move || {
                    let Ok(reply) = host.call("get_script_state", json!({"execution_id":id}))
                    else {
                        return;
                    };
                    match reply["state"]["status"].as_str() {
                        Some("running") => {
                            let _ = control(&host, "pause_script", &id);
                        }
                        Some("paused") => {
                            let _ = control(&host, "resume_script", &id);
                        }
                        _ => {}
                    }
                });
            }
        }
        "start_selection" => {
            std::thread::spawn(move || {
                if let Some(m) = crate::selection::shared_manager() {
                    let _ = m
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .dispatch_command("start_selection_mode", &json!({}));
                }
            });
        }
        _ => {}
    }
}
pub fn control(
    host: &Arc<crate::ipc::hostcall::HostCallClient>,
    cmd: &str,
    id: &str,
) -> Result<Value, IpcError> {
    with_control(cmd, id, || host.call(cmd, json!({"execution_id":id})))
}

pub fn with_control(
    cmd: &str,
    id: &str,
    submit: impl FnOnce() -> Result<Value, IpcError>,
) -> Result<Value, IpcError> {
    let Some(gate) = crate::input::shared_gate() else {
        return submit();
    };
    let known = gate.active(None).iter().any(|e| e == id);
    if !known {
        return submit();
    }
    let previously_paused = gate.paused(id);
    match cmd {
        "stop_script" => {
            gate.close(id, "stop_script");
            crate::input::release_closed();
        }
        "pause_script" => {
            gate.pause(id);
            crate::input::release_closed();
        }
        "resume_script" | "continue_execution" | "step_into" | "step_over" | "step_out" => gate.resume(id),
        _ => {}
    }
    let result = submit();
    if result.is_err() && cmd != "stop_script" {
        if previously_paused {
            gate.pause(id);
        } else {
            gate.resume(id);
        }
    }
    result
}

fn publish_input(
    run: &crate::sidecar::SupervisorHandle,
    host: &crate::ipc::hostcall::HostCallClient,
    error: Option<&IpcError>,
) {
    let enabled = error.is_none();
    let reason = error.map(|e| format!("{} · {}", e.code, e.message));
    if let Some(g) = crate::input::shared_gate() {
        g.enable_input(enabled);
    }
    run.update_input_capability(&host.session_id(), enabled, reason.clone());
    let _ = host.call(
        "set_input_availability",
        json!({"enabled":enabled,"reason":reason}),
    );
}

pub fn initialize_session(run: &crate::sidecar::SupervisorHandle) {
    let _txn = TRANSACTION.lock().unwrap_or_else(|p| p.into_inner());
    let Some(host) = crate::ipc::hostcall::shared() else {
        return;
    };
    let result = host
        .call("get_config", json!({}))
        .and_then(|v| prepare(&v["config"]["hotkeys"]))
        .and_then(|_| finish(true));
    publish_input(run, &host, result.as_ref().err());
}

pub fn configure(
    run: &crate::sidecar::SupervisorHandle,
    cmd: &str,
    params: &Value,
    submit: impl FnOnce() -> Result<Value, IpcError>,
) -> Result<Value, IpcError> {
    let _txn = TRANSACTION.lock().unwrap_or_else(|p| p.into_inner());
    let host =
        crate::ipc::hostcall::shared().ok_or_else(|| IpcError::engine_unavailable("引擎未就绪"))?;
    let old = host.call("get_config", json!({}))?;
    if params["expected_revision"] != old["config"]["revision"] {
        return submit();
    }
    let mut proposed = if cmd == "reset_config" {
        defaults()
    } else {
        old["config"]["hotkeys"].clone()
    };
    if cmd == "update_config" {
        let patch = params["patch"]["hotkeys"]
            .as_object()
            .ok_or_else(|| IpcError::invalid_params("hotkeys 必须是对象"))?;
        for (key, val) in patch {
            proposed[key] = val.clone();
        }
    }
    prepare(&proposed)?;
    let result = submit();
    let persisted = match &result {
        Ok(v) => v["config"]["hotkeys"] == proposed,
        Err(e) if matches!(e.code.as_str(), "ENGINE_UNAVAILABLE" | "INTERNAL_ERROR") => {
            match host.call("get_config", json!({})) {
                Ok(v) => v["config"]["hotkeys"] == proposed,
                Err(_) => {
                    let _ = finish(false);
                    publish_input(run, &host, Some(e));
                    return result;
                }
            }
        }
        Err(_) => false,
    };
    finish(persisted)?;
    if persisted {
        publish_input(run, &host, None);
    }
    result
}

#[cfg(windows)]
mod os {
    use super::Chord;
    #[repr(C)]
    #[derive(Default)]
    struct Msg {
        window: isize,
        message: u32,
        wparam: usize,
        lparam: isize,
        time: u32,
        x: i32,
        y: i32,
        private: u32,
    }
    #[link(name = "user32")]
    extern "system" {
        fn RegisterHotKey(hwnd: isize, id: i32, modifiers: u32, vk: u32) -> i32;
        fn UnregisterHotKey(hwnd: isize, id: i32) -> i32;
        fn PeekMessageW(msg: *mut Msg, hwnd: isize, min: u32, max: u32, remove: u32) -> i32;
    }
    pub fn register(id: i32, key: Chord) -> bool {
        unsafe { RegisterHotKey(0, id, key.0 | 0x4000, key.1.into()) != 0 }
    }
    pub fn unregister(id: i32) {
        unsafe {
            UnregisterHotKey(0, id);
        }
    }
    pub fn pop() -> Option<i32> {
        let mut msg = Msg::default();
        (unsafe { PeekMessageW(&mut msg, 0, 0x312, 0x312, 1) } != 0).then_some(msg.wparam as i32)
    }
}
#[cfg(not(windows))]
mod os {
    pub fn register(_: i32, _: super::Chord) -> bool {
        false
    }
    pub fn unregister(_: i32) {}
    pub fn pop() -> Option<i32> {
        None
    }
}
