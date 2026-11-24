//! ABI v1. Buffers are borrowed only for the duration of a call or callback.
use crate::{commands, ipc::IpcError, sidecar::SupervisorHandle};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, RwLock,
};

type Receiver = extern "C" fn(*const u8, usize);
const ABI_VERSION: u32 = 1;
static OPEN: AtomicBool = AtomicBool::new(false);

pub struct DesktopHost {
    supervisor: SupervisorHandle,
    event: Arc<RwLock<Option<Receiver>>>,
    closing: AtomicBool,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Call {
    Start,
    Stop,
    Status,
    Diagnostics,
    Request {
        session_id: Option<String>,
        id: String,
        command: String,
        params: Value,
    },
    Cancel {
        session_id: Option<String>,
        request_id: String,
    },
}

impl DesktopHost {
    fn dispatch(&self, bytes: &[u8]) -> Result<Value, IpcError> {
        let call = serde_json::from_slice::<crate::ipc::strict_json::StrictValue>(bytes)
            .and_then(|v| serde_json::from_value(v.0))
            .map_err(|e| IpcError::new("INVALID_PARAMS", &e.to_string(), false))?;
        let run = &self.supervisor;
        match call {
            Call::Start => commands::engine_start(run)
                .map(|v| json!(v))
                .map_err(|e| IpcError::new(&e.code, &e.message, e.retryable)),
            Call::Stop => Ok(json!(run.stop("engine_stopping"))),
            Call::Status => Ok(json!(commands::engine_status(run))),
            Call::Diagnostics => Ok(json!(commands::engine_diagnostics(run))),
            Call::Request {
                session_id,
                id,
                command,
                params,
            } => run.session_router(session_id.as_deref())?.request(&id, &command, params),
            Call::Cancel { session_id, request_id } => run.session_router(session_id.as_deref())?.cancel(&request_id),
        }
    }
}

#[no_mangle]
pub extern "C" fn st_host_abi_version() -> u32 {
    ABI_VERSION
}

#[no_mangle]
pub extern "C" fn st_host_open(version: u32, event: Option<Receiver>) -> *mut DesktopHost {
    if version != ABI_VERSION || event.is_none() || OPEN.swap(true, Ordering::AcqRel) {
        return std::ptr::null_mut();
    }
    let supervisor = SupervisorHandle::new();
    let event = Arc::new(RwLock::new(event));
    let sink = Arc::clone(&event);
    supervisor.set_event_emitter(Arc::new(move |_, envelope| {
        let callback = sink.read().unwrap_or_else(|p| p.into_inner());
        if let Some(send) = *callback {
            if let Ok(bytes) = serde_json::to_vec(envelope) {
                send(bytes.as_ptr(), bytes.len());
            }
        }
    }));
    Box::into_raw(Box::new(DesktopHost {
        supervisor,
        event,
        closing: AtomicBool::new(false),
    }))
}

/// The caller keeps the handle and input buffer alive until this call returns.
#[no_mangle]
pub unsafe extern "C" fn st_host_call(
    host: *const DesktopHost,
    data: *const u8,
    len: usize,
    respond: Option<Receiver>,
) -> i32 {
    let (Some(host), Some(respond)) = (host.as_ref(), respond) else {
        return -1;
    };
    if data.is_null() || len == 0 || len > crate::ipc::types::MAX_FRAME_BYTES {
        return -1;
    }
    let result = if host.closing.load(Ordering::Acquire) {
        Err(IpcError::engine_unavailable("桌面宿主正在退出"))
    } else {
        host.dispatch(std::slice::from_raw_parts(data, len))
    };
    let reply = match result {
        Ok(data) => json!({"success": true, "data": data}),
        Err(error) => json!({"success": false, "error": error}),
    };
    match serde_json::to_vec(&reply) {
        Ok(bytes) => {
            respond(bytes.as_ptr(), bytes.len());
            0
        }
        Err(_) => -2,
    }
}

#[no_mangle]
pub unsafe extern "C" fn st_host_begin_close(host: *const DesktopHost) {
    if let Some(host) = host.as_ref() {
        host.closing.store(true, Ordering::Release);
        host.event.write().unwrap_or_else(|p| p.into_inner()).take();
        host.supervisor.shutdown_sync("desktop_exit");
    }
}

/// Called exactly once, after all st_host_call invocations on this handle return.
#[no_mangle]
pub unsafe extern "C" fn st_host_close(host: *mut DesktopHost) {
    if host.is_null() {
        return;
    }
    let host = Box::from_raw(host);
    // Taking the writer lock drains any callback still copying its borrowed bytes.
    host.event.write().unwrap_or_else(|p| p.into_inner()).take();
    host.supervisor.shutdown_sync("desktop_exit");
    drop(host);
    OPEN.store(false, Ordering::Release);
}
