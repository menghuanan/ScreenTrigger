//! Session-scoped window identities and client geometry shared by capture and input.
#[cfg(windows)]
pub mod system;
#[cfg(not(windows))]
pub mod system {
    pub fn observe(_: isize) -> Option<super::Observation> {
        None
    }
    pub fn enumerate() -> Vec<super::Observation> {
        Vec::new()
    }
    pub fn monitor_at(_: i64, _: i64) -> Option<String> {
        None
    }
    pub fn window_at(_: i64, _: i64) -> Option<isize> {
        None
    }
}
use crate::ipc::{HostHandler, IpcError};
use crate::types::region::Rect;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use uuid::Uuid;

#[derive(Clone, PartialEq, Eq)]
pub struct Identity {
    pub hwnd: isize,
    pub pid: u32,
    pub started: String,
}
#[derive(Clone)]
pub struct Observation {
    pub identity: Identity,
    pub title: String,
    pub process_name: String,
    pub rect: Rect,
    pub dpi: u32,
    pub visible: bool,
    pub minimized: bool,
    pub foreground: bool,
}
#[derive(Clone)]
pub struct Entry {
    pub id: String,
    pub live: Observation,
    pub revision: i64,
}
impl Entry {
    fn binding(&self, id: &str) -> Value {
        let identity = &self.live.identity;
        json!({"binding_id":id,"window_id":self.id,"handle":format!("0x{:X}",identity.hwnd),"pid":identity.pid,"process_started_at":identity.started})
    }
    fn info(&self) -> Value {
        let mut v = self.binding("");
        v.as_object_mut().unwrap().remove("binding_id");
        v.as_object_mut()
            .unwrap()
            .extend(self.geometry().as_object().unwrap().clone());
        v["title"] = json!(self.live.title);
        v["process_name"] = json!(self.live.process_name);
        v
    }
    fn geometry(&self) -> Value {
        json!({"client_rect":self.live.rect,"geometry_revision":self.revision,"visible":self.live.visible,"minimized":self.live.minimized,"foreground":self.live.foreground})
    }
}
#[derive(Default)]
pub struct Catalog {
    windows: HashMap<String, Entry>,
    binding: Option<(String, String)>,
    last_event: Option<Entry>,
}
pub type Windows = Arc<Mutex<Catalog>>;
pub static SESSION: Mutex<Option<Windows>> = Mutex::new(None);
pub fn shared() -> Option<Windows> {
    SESSION.lock().unwrap_or_else(|e| e.into_inner()).clone()
}
pub fn new_session() -> Windows {
    let w = Windows::default();
    *SESSION.lock().unwrap_or_else(|e| e.into_inner()) = Some(w.clone());
    w
}
pub fn end_session() {
    SESSION.lock().unwrap_or_else(|e| e.into_inner()).take();
}
pub fn watch(
    windows: Windows,
    session: String,
    tx: std::sync::mpsc::Sender<crate::ipc::RouterInput>,
) {
    use crate::selection::model::EventSink;
    let events = crate::selection::RouterEventSink::new();
    events.attach(tx, session.clone());
    std::thread::spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_millis(50));
        if !shared().is_some_and(|w| Arc::ptr_eq(&w, &windows)) {
            break;
        }
        let change = windows
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .poll_change();
        let Some(change) = change else {
            continue;
        };
        events.emit("window_changed", change.clone());
        let Some(state) = change["state"].as_str() else {
            continue;
        };
        if !matches!(state, "closed" | "foreground_lost" | "minimized") {
            continue;
        }
        let Some(gate) = crate::input::shared_gate() else {
            continue;
        };
        let roots = gate.window_roots(change["binding_id"].as_str(), state == "foreground_lost");
        for root in &roots {
            if state == "closed" {
                gate.close(root, "window_closed");
            } else {
                gate.pause(root);
            }
        }
        if !roots.is_empty() {
            // The gate closes immediately, even when the Lua body is waiting and sends no further input.
            crate::input::release_closed();
            if let Some(host) = crate::ipc::hostcall::shared().filter(|h| h.session_id() == session)
            {
                let _ = host.call("binding_state_changed", change);
            }
        }
    });
}
fn missing() -> IpcError {
    IpcError::window_not_found("窗口已关闭或身份已变化，请重新选择窗口")
}
impl Catalog {
    fn update(&mut self, live: Observation) -> Entry {
        if let Some(entry) = self
            .windows
            .values_mut()
            .find(|e| e.live.identity == live.identity)
        {
            if entry.live.rect != live.rect || entry.live.dpi != live.dpi {
                entry.revision += 1;
            }
            entry.live = live;
            return entry.clone();
        }
        let entry = Entry {
            id: Uuid::new_v4().to_string(),
            live,
            revision: 1,
        };
        self.windows.insert(entry.id.clone(), entry.clone());
        entry
    }
    fn by_id(&mut self, id: &str) -> Result<Entry, IpcError> {
        let stored = self.windows.get(id).ok_or_else(missing)?;
        let actual = system::observe(stored.live.identity.hwnd);
        match actual {
            Some(w) if w.identity == stored.live.identity => Ok(self.update(w)),
            _ => {
                self.windows.remove(id);
                Err(missing())
            }
        }
    }
    pub fn bound(&mut self, binding: &str) -> Result<Entry, IpcError> {
        let id = self
            .binding
            .as_ref()
            .filter(|(b, _)| b == binding)
            .map(|(_, id)| id.clone())
            .ok_or_else(missing)?;
        self.by_id(&id)
    }
    pub fn matches_foreground(&mut self, identity: &crate::input::model::WindowIdentity) -> bool {
        let Some((binding, _)) = self.binding.clone() else {
            return false;
        };
        self.bound(&binding).is_ok_and(|w| {
            w.live.foreground
                && w.live.identity.hwnd as u64 == identity.handle
                && w.live.identity.pid == identity.pid
                && w.live.identity.started == identity.process_started_at
        })
    }
    pub fn poll_change(&mut self) -> Option<Value> {
        let (binding, _) = self.binding.clone()?;
        let current = match self.bound(&binding) {
            Ok(w) => w,
            Err(_) => {
                let previous = self.last_event.take()?;
                self.binding = None;
                return Some(
                    json!({"binding_id":binding,"geometry_revision":previous.revision,"state":"closed"}),
                );
            }
        };
        let previous = self.last_event.replace(current.clone())?;
        let (old, new) = (&previous.live, &current.live);
        let state = if new.minimized && !old.minimized {
            "minimized"
        } else if (!new.foreground && old.foreground) || (!new.visible && old.visible) {
            "foreground_lost"
        } else if (!new.minimized && old.minimized) || (new.foreground && !old.foreground) {
            "restored"
        } else if new.dpi != old.dpi {
            "dpi"
        } else if new.rect.width != old.rect.width || new.rect.height != old.rect.height {
            "resized"
        } else if new.rect != old.rect {
            "moved"
        } else {
            return None;
        };
        Some(json!({"binding_id":binding,"geometry_revision":current.revision,"state":state}))
    }
    pub fn command(
        &mut self,
        command: &str,
        params: &Value,
        active: bool,
    ) -> Result<Value, IpcError> {
        let id = || {
            params["window_id"]
                .as_str()
                .ok_or_else(|| IpcError::invalid_params("缺少 window_id"))
        };
        match command {
            "get_window_input_capabilities" => {
                if params.as_object().map_or(0,|p|p.len()) != 1 {
                    return Err(IpcError::invalid_params("binding_id"));
                }
                let binding = params.get("binding_id").and_then(Value::as_str)
                    .ok_or_else(|| IpcError::invalid_params("binding_id"))?;
                let target = self.bound(binding)?;
                Ok(crate::input::background::capabilities(binding,&target))
            }
            "list_windows" => {
                let windows = system::enumerate();
                let bound = self.binding.as_ref().map(|(_, id)| id.clone());
                self.windows.retain(|id, entry| {
                    bound.as_ref() == Some(id)
                        || windows.iter().any(|w| w.identity == entry.live.identity)
                });
                Ok(
                    json!({"windows":windows.into_iter().map(|w|self.update(w).info()).collect::<Vec<_>>()}),
                )
            }
            "get_window_info" => Ok(json!({"window":self.by_id(id()?)?.info()})),
            "bind_window" | "unbind_window" if active => {
                Err(IpcError::invalid_state("执行期间不能更改窗口绑定"))
            }
            "bind_window" => {
                let w = self.by_id(id()?)?;
                let binding = Uuid::new_v4().to_string();
                self.binding = Some((binding.clone(), w.id.clone()));
                self.last_event = Some(w.clone());
                Ok(json!({"binding":w.binding(&binding)}))
            }
            "unbind_window" => {
                self.binding = None;
                self.last_event = None;
                Ok(json!({"ok":true}))
            }
            "get_bound_window" => {
                let result = match self.binding.clone() {
                    Some((b, _)) => self.bound(&b).ok().map(|w| w.binding(&b)),
                    None => None,
                };
                Ok(json!({"binding":result}))
            }
            "window_snapshot" => {
                let b = params["binding_id"]
                    .as_str()
                    .ok_or_else(|| IpcError::invalid_params("缺少 binding_id"))?;
                let w = self.bound(b)?;
                let mut v = w.geometry();
                v["binding"] = w.binding(b);
                Ok(v)
            }
            _ => Err(IpcError::unknown_command(command)),
        }
    }
}
pub struct SnapshotHandler(pub Windows);
impl HostHandler for SnapshotHandler {
    fn handle(&mut self, cmd: &str, params: &Value) -> Result<Value, IpcError> {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .command(cmd, params, false)
    }
}

pub struct DesktopCommands {
    pub session: String,
    pub windows: Option<Windows>,
    pub gate: Option<Arc<crate::input::gate::GateTable>>,
    pub selection: Option<crate::selection::SharedSelectionManager>,
}
impl crate::ipc::HostCommandHandler for DesktopCommands {
    fn dispatch(&mut self, cmd: &str, params: &Value) -> Result<Value, IpcError> {
        if crate::recording::COMMANDS.contains(&cmd){return crate::recording::command(&self.session,cmd,params);}
        if matches!(
            cmd,
            "start_selection_mode" | "confirm_selection" | "stop_selection_mode"
                | "start_point_pick" | "confirm_point_pick" | "cancel_point_pick"
        ) {
            return self
                .selection
                .as_mut()
                .ok_or_else(|| IpcError::capability_denied(cmd))?
                .dispatch(cmd, params);
        }
        let active = self
            .gate
            .as_ref()
            .is_some_and(|g| !g.active(None).is_empty()) || crate::recording::active();
        self.windows
            .as_ref()
            .ok_or_else(|| IpcError::capability_denied(cmd))?
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .command(cmd, params, active)
    }
}
