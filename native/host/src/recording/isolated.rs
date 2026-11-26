//! A disposable interactive desktop for an explicitly selected application.
#[path = "isolated_process.rs"]
mod process;
#[path = "return_protocol.rs"]
mod protocol;
#[path = "return_guard.rs"]
mod return_guard;

use crate::ipc::IpcError;
use process::Target;
use protocol::object_name;
use return_guard::ReturnGuard;
use std::{
    ffi::c_void,
    os::windows::io::OwnedHandle,
    ptr,
    sync::{Arc, Mutex},
    thread,
    time::Duration,
};
use uuid::Uuid;

type Desk = isize;
const READ_OBJECTS: u32 = 0x0001;
const CREATE_WINDOW: u32 = 0x0002;
const CREATE_MENU: u32 = 0x0004;
const HOOK_CONTROL: u32 = 0x0008;
const ENUMERATE: u32 = 0x0040;
const WRITE_OBJECTS: u32 = 0x0080;
const SWITCH: u32 = 0x0100;
#[link(name = "user32")]
extern "system" {
    fn GetProcessWindowStation() -> *mut c_void;
    fn OpenInputDesktop(flags: u32, inherit: i32, access: u32) -> Desk;
    fn CreateDesktopW(
        name: *const u16,
        device: *const u16,
        mode: *mut (),
        flags: u32,
        access: u32,
        security: *const (),
    ) -> Desk;
    fn CloseDesktop(desk: Desk) -> i32;
    fn GetThreadDesktop(thread: u32) -> *mut c_void;
    fn SetThreadDesktop(desk: Desk) -> i32;
    fn SwitchDesktop(desk: Desk) -> i32;
    fn EnumDesktopWindows(
        desk: Desk,
        callback: extern "system" fn(isize, isize) -> i32,
        data: isize,
    ) -> i32;
    fn GetWindowThreadProcessId(hwnd: isize, pid: *mut u32) -> u32;
    fn GetWindow(hwnd: isize, command: u32) -> isize;
    fn GetWindowLongPtrW(hwnd: isize, index: i32) -> isize;
    fn GetAncestor(hwnd: isize, flags: u32) -> isize;
    fn IsWindowVisible(hwnd: isize) -> i32;
}
#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentThreadId() -> u32;
}
#[link(name = "imm32")]
extern "system" {
    fn ImmDisableIME(thread_id: u32) -> i32;
}

/// These recorder threads never edit text. Opt out before USER creates a window or
/// attaches a desktop, so thread detach need not wait for the text service's COM monitor.
/// Only this helper thread is affected, including when the target itself uses a Chinese IME.
pub(super) fn prepare_non_text_thread() {
    if unsafe { ImmDisableIME(GetCurrentThreadId()) } == 0 {
        trace_lifecycle("helper.ime-opt-out-unavailable");
    }
}

pub(super) fn trace_lifecycle(stage: &'static str) {
    static ENABLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    if *ENABLED.get_or_init(|| std::env::var_os("ST_IPC_TRACE").is_some_and(|v| v == "1")) {
        eprintln!("[recording-lifecycle] tid={} {stage}", unsafe { GetCurrentThreadId() });
    }
}

fn unavailable(message: &str) -> IpcError {
    IpcError::new("CAPABILITY_DENIED", message, false)
}
fn os_error(operation: &str) -> IpcError {
    unavailable(&format!("{operation}：{}", std::io::Error::last_os_error()))
}

struct Desktop(Desk);
impl Desktop {
    fn input(access: u32) -> Result<Self, IpcError> {
        let handle = unsafe { OpenInputDesktop(0, 0, access) };
        if handle == 0 {
            Err(os_error("无法打开当前输入桌面"))
        } else {
            Ok(Self(handle))
        }
    }
    fn find_window<F: FnMut(isize) -> bool>(&self, predicate: F) -> Option<isize> {
        struct Search<F> {
            predicate: F,
            found: Option<isize>,
        }
        extern "system" fn visit<F: FnMut(isize) -> bool>(hwnd: isize, raw: isize) -> i32 {
            let search = unsafe { &mut *(raw as *mut Search<F>) };
            if (search.predicate)(hwnd) {
                search.found = Some(hwnd);
                return 0;
            }
            1
        }
        let mut search = Search {
            predicate,
            found: None,
        };
        unsafe {
            EnumDesktopWindows(self.0, visit::<F>, (&mut search as *mut Search<F>) as isize);
        }
        search.found
    }

    fn create(name: &str) -> Result<Self, IpcError> {
        let name: Vec<_> = name.encode_utf16().chain([0]).collect();
        let access = READ_OBJECTS
            | WRITE_OBJECTS
            | CREATE_WINDOW
            | CREATE_MENU
            | HOOK_CONTROL
            | ENUMERATE
            | SWITCH;
        let handle = unsafe {
            CreateDesktopW(
                name.as_ptr(),
                ptr::null(),
                ptr::null_mut(),
                0,
                access,
                ptr::null(),
            )
        };
        if handle == 0 {
            Err(os_error("无法创建隔离录制桌面"))
        } else {
            Ok(Self(handle))
        }
    }
}
impl Drop for Desktop {
    fn drop(&mut self) {
        unsafe {
            CloseDesktop(self.0);
        }
    }
}

struct Presentation {
    active: bool,
    closing: bool,
    job: Option<OwnedHandle>,
}
struct Session {
    private: Desktop,
    original: Desktop,
    name: String,
    original_name: String,
    target: Target,
    guard: ReturnGuard,
    presentation: Mutex<Presentation>,
}
impl Session {
    fn input_name(&self) -> Option<String> {
        object_name(Desktop::input(READ_OBJECTS).ok()?.0)
    }
    fn restore(&self) -> Result<(), IpcError> {
        let mut state = self.presentation.lock().unwrap();
        if state.job.is_none() {
            return Ok(());
        }
        state.closing = true;
        if state.active {
            let current = self
                .input_name()
                .ok_or_else(|| unavailable("暂时无法核验输入桌面，将自动重试"))?;
            // Leave an independently selected desktop alone. When input is inaccessible
            // (e.g. a locked session), retain both handles and let the watcher retry.
            if current == self.name {
                if unsafe { SwitchDesktop(self.original.0) } == 0 {
                    return Err(os_error("未能返回原桌面，将自动重试"));
                }
                if self.input_name().is_none_or(|name| name == self.name) {
                    return Err(unavailable("尚未确认已返回原桌面，将自动重试"));
                }
            }
            state.active = false;
        }
        self.guard
            .disarm()
            .map_err(|e| unavailable(&format!("未能解除桌面返回监护：{e}")))?;
        state.job.take(); // Closing the private job terminates only this application's launch.
        Ok(())
    }
    fn watch(self: Arc<Self>) {
        loop {
            let state = self.presentation.lock().unwrap();
            if state.job.is_none() {
                return;
            }
            let closing = state.closing;
            drop(state);
            if (closing || !self.target.alive() || !self.guard.alive()) && self.restore().is_ok() {
                return;
            }
            thread::park_timeout(Duration::from_millis(75));
        }
    }
}

pub(super) struct IsolatedDesktop {
    session: Arc<Session>,
    watcher: Option<thread::JoinHandle<()>>,
}

impl IsolatedDesktop {
    pub(super) fn launch(executable: &str) -> Result<Self, IpcError> {
        let original = Desktop::input(READ_OBJECTS | SWITCH)?;
        let original_name = object_name(original.0).ok_or_else(|| unavailable("无法验证原桌面"))?;
        if object_name(unsafe { GetThreadDesktop(GetCurrentThreadId()) as Desk }).as_deref()
            != Some(&original_name)
        {
            return Err(unavailable("请在当前可见的用户桌面启动隔离录制"));
        }
        let station = object_name(unsafe { GetProcessWindowStation() as isize })
            .ok_or_else(|| unavailable("无法验证窗口站"))?;
        let name = format!("ScreenTrigger-Recording-{}", Uuid::new_v4());
        let desktop_name = name.clone();
        // The creating thread may be assigned the new desktop. Exit that thread before
        // any launch failure can close its HDESK, and keep the caller on its own desktop.
        let private = thread::Builder::new()
            .name("recording-desktop-create".into())
            .spawn(move || {
                prepare_non_text_thread();
                Desktop::create(&desktop_name)
            })
            .map_err(|_| unavailable("无法创建隔离桌面线程"))?
            .join()
            .map_err(|_| unavailable("隔离桌面创建线程已退出"))??;
        let (target, job) = Target::launch(executable, &format!("{station}\\{name}"))?;
        let guard = ReturnGuard::arm(&station, &original_name, &name)
            .map_err(|e| unavailable(&format!("无法启动隔离桌面返回监护：{e}")))?;
        let session = Arc::new(Session {
            private,
            original,
            name,
            original_name,
            target,
            guard,
            presentation: Mutex::new(Presentation {
                active: false,
                closing: false,
                job: Some(job),
            }),
        });
        let watched = Arc::clone(&session);
        let watcher = thread::Builder::new()
            .name("recording-desktop-return".into())
            .spawn(move || {
                prepare_non_text_thread();
                trace_lifecycle("watcher.begin");
                watched.watch();
                trace_lifecycle("watcher.end");
            })
            .map_err(|_| unavailable("无法启动桌面返回监护线程"))?;
        Ok(Self { session, watcher: Some(watcher) })
    }

    /// Used only on newly spawned, single-purpose threads. The owner retains the desktop
    /// through their joins. USER releases each thread's desktop association at thread exit.
    pub(super) fn enter_dedicated_thread(&self) -> Result<(), IpcError> {
        if unsafe { SetThreadDesktop(self.session.private.0) } == 0 {
            return Err(os_error("录制工作线程无法进入隔离桌面"));
        }
        Ok(())
    }
    pub(super) fn name(&self) -> &str {
        &self.session.name
    }
    pub(super) fn pid(&self) -> u32 {
        self.session.target.pid
    }
    pub(super) fn alive(&self) -> bool {
        self.session.target.alive()
    }
    pub(super) fn is_input_desktop(&self) -> bool {
        self.session.input_name().as_deref() == Some(self.name())
    }
    pub(super) fn owns_window(&self, hwnd: isize) -> bool {
        let mut pid = 0;
        let tid = unsafe { GetWindowThreadProcessId(hwnd, &mut pid) };
        if tid == 0 || pid != self.pid() || !self.alive() {
            return false;
        }
        // An EDIT's thread may deny access to its desktop handle even on our own desktop.
        // Membership in the enumeration on the retained HDESK also works for such controls.
        let root = unsafe { GetAncestor(hwnd, 2) };
        root != 0
            && self
                .session
                .private
                .find_window(|candidate| candidate == root)
                .is_some()
    }
    pub(super) fn main_window(&self) -> Option<isize> {
        if !self.alive() {
            return None;
        }
        let mut fallback = None;
        let primary = self.session.private.find_window(|hwnd| {
            let mut pid = 0;
            let tid = unsafe { GetWindowThreadProcessId(hwnd, &mut pid) };
            // An IME can create visible unowned overlay/tool windows inside the target process.
            // Select an activatable application window, preferring its initial UI thread.
            if pid != self.pid()
                || tid == 0
                || unsafe {
                    IsWindowVisible(hwnd) == 0
                        || GetWindow(hwnd, 4) != 0
                        || GetWindowLongPtrW(hwnd, -20) & (0x80 | 0x08000000) != 0
                }
            {
                return false;
            }
            fallback.get_or_insert(hwnd);
            tid == self.session.target.initial_thread
        });
        primary.or(fallback)
    }
    pub(super) fn activate(&self) -> Result<(), IpcError> {
        let mut state = self.session.presentation.lock().unwrap();
        if !self.session.guard.alive() {
            return Err(unavailable("隔离桌面返回监护已经退出"));
        }
        if state.closing || !self.alive() || self.main_window().is_none() {
            return Err(unavailable("选定应用未在隔离桌面建立独立窗口，或已经退出"));
        }
        if state.active && self.is_input_desktop() {
            return Ok(());
        }
        if self.session.input_name().as_deref() != Some(&self.session.original_name) {
            return Err(unavailable("原输入桌面已切换，启动隔离录制失败"));
        }
        if unsafe { SwitchDesktop(self.session.private.0) } == 0 {
            return Err(os_error("无法显示隔离录制桌面"));
        }
        state.active = true;
        if !self.is_input_desktop() {
            return Err(unavailable("无法确认隔离桌面已取得输入"));
        }
        Ok(())
    }
    pub(super) fn restore(&self) -> Result<(), IpcError> {
        let result = self.session.restore();
        if let Some(thread) = &self.watcher {
            thread.thread().unpark();
        }
        result
    }
    pub(super) fn watcher_finished(&self) -> bool {
        self.watcher.as_ref().is_none_or(crate::winmessages::thread_exited)
    }
}
impl Drop for IsolatedDesktop {
    fn drop(&mut self) {
        let _ = self.restore();
        if let Some(watcher) = self.watcher.take() {
            let _ = watcher.join();
        }
    }
}
