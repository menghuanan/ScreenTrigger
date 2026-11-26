//! Visible mouse recorder. An opted-in window may send its focused key messages;
//! known foreground controls also support a restricted set of command keys.
use super::sequence::{Key, Pointer, Sequence, Status};
use crate::input::{model::MonitorRect, win32};
use crate::ipc::IpcError;
use std::{
    cell::RefCell,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use uuid::Uuid;

#[repr(C)]
#[derive(Copy, Clone, Default)]
struct Point {
    x: i32,
    y: i32,
}
#[repr(C)]
struct Mouse {
    pt: Point,
    data: u32,
    flags: u32,
    time: u32,
    extra: usize,
}
#[repr(C)]
struct CopyData {
    kind: usize,
    len: u32,
    ptr: *const u8,
}
#[repr(C)]
struct Class {
    style: u32,
    proc: Option<unsafe extern "system" fn(isize, u32, usize, isize) -> isize>,
    extra: i32,
    winextra: i32,
    instance: isize,
    icon: isize,
    cursor: isize,
    brush: isize,
    menu: *const u16,
    name: *const u16,
}
#[link(name = "user32")]
extern "system" {
    fn RegisterClassW(c: *const Class) -> u16;
    fn UnregisterClassW(n: *const u16, m: isize) -> i32;
    fn CreateWindowExW(
        ex: u32,
        c: *const u16,
        title: *const u16,
        style: u32,
        x: i32,
        y: i32,
        w: i32,
        h: i32,
        parent: isize,
        menu: isize,
        instance: isize,
        data: isize,
    ) -> isize;
    fn DestroyWindow(h: isize) -> i32;
    fn DefWindowProcW(h: isize, msg: u32, w: usize, l: isize) -> isize;
    fn SetWindowsHookExW(
        kind: i32,
        proc: Option<unsafe extern "system" fn(i32, usize, isize) -> isize>,
        module: isize,
        tid: u32,
    ) -> isize;
    fn UnhookWindowsHookEx(h: isize) -> i32;
    fn CallNextHookEx(h: isize, code: i32, w: usize, l: isize) -> isize;
    fn GetWindowThreadProcessId(h: isize, p: *mut u32) -> u32;
    fn GetKeyboardLayout(thread: u32) -> isize;
    fn SetWindowTextW(hwnd: isize, text: *const u16) -> i32;
    fn GetAncestor(h: isize, flags: u32) -> isize;
    fn GetWindowLongPtrW(h: isize, index: i32) -> isize;
    fn GetClassNameW(h: isize, name: *mut u16, max: i32) -> i32;
    fn GetPropW(h: isize, name: *const u16) -> isize;
    fn SendMessageTimeoutW(
        h: isize,
        msg: u32,
        w: usize,
        l: isize,
        flags: u32,
        time: u32,
        result: *mut usize,
    ) -> isize;
    fn SetThreadDpiAwarenessContext(context: isize) -> isize;
}
#[link(name = "kernel32")]
extern "system" {
    fn GetModuleHandleW(name: *const u16) -> isize;
}
fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain([0]).collect()
}
const KEY_OFFER: usize = 0x53545231;
const KEY_EDGE: usize = 0x53545232;

struct Context {
    seq: Arc<Mutex<Sequence>>,
    start: Instant,
    nonce: Uuid,
    indicator: isize,
    offered: Option<isize>,
    isolated: Option<Arc<super::isolated::IsolatedDesktop>>,
    geometry: Option<(crate::window::Identity, crate::types::region::Rect, u32)>,
}
impl Context {
    fn geometry_valid(&self) -> bool {
        self.geometry.as_ref().is_none_or(|expected| {
            crate::window::system::recording_geometry(expected.0.hwnd).as_ref() == Some(expected)
        })
    }
}
thread_local! {static CTX:RefCell<Option<Context>>=const{RefCell::new(None)};}
fn root(h: isize) -> Option<isize> {
    let r = unsafe { GetAncestor(h, 2) };
    (r != 0).then_some(r)
}
fn focus() -> Option<(isize, isize)> {
    let foreground = win32::foreground_window() as isize;
    if foreground == 0 {
        return None;
    }
    let thread = unsafe { GetWindowThreadProcessId(foreground, std::ptr::null_mut()) };
    let focused = crate::window::system::thread_focus(thread)?;
    (root(focused) == Some(foreground)).then_some((foreground, focused))
}
fn in_rect(rect: MonitorRect, x: i64, y: i64) -> bool {
    x >= rect.x && y >= rect.y && x - rect.x < rect.width && y - rect.y < rect.height
}
unsafe extern "system" fn mouse(code: i32, w: usize, l: isize) -> isize {
    if code == 0 && l != 0 {
        CTX.with_borrow(|slot| {
            let Some(c) = slot else { return };
            if c.isolated.as_ref().is_some_and(|d| !d.is_input_desktop()) {
                return;
            }
            if !c.geometry_valid() {
                c.seq
                    .lock()
                    .unwrap()
                    .pause(c.start.elapsed().as_millis() as u64, "geometry_changed");
                return;
            }
            let raw = &*(l as *const Mouse);
            if raw.flags & 1 != 0 && raw.extra == crate::recording::SELF_INPUT {
                return;
            }
            let (x, y) = (i64::from(raw.pt.x), i64::from(raw.pt.y));
            let at = c.start.elapsed().as_millis() as u64;
            let hit = crate::window::system::window_at(x, y).filter(|h| {
                let mut pid = 0;
                GetWindowThreadProcessId(*h, &mut pid);
                *h != c.indicator
                    && pid != std::process::id()
                    && c.isolated.as_ref().is_none_or(|d| d.owns_window(*h))
            });
            let foreground = if c.geometry.is_some() {
                let h = win32::foreground_window() as isize;
                (h != 0).then_some(h)
            } else {
                None
            };
            let mut seq = c.seq.lock().unwrap();
            let event = match w {
                0x200 => Pointer::Move,
                0x201 => Pointer::Down("left"),
                0x202 => Pointer::Up("left"),
                0x204 => Pointer::Down("right"),
                0x205 => Pointer::Up("right"),
                0x207 => Pointer::Down("middle"),
                0x208 => Pointer::Up("middle"),
                0x20a => Pointer::Wheel((raw.data >> 16) as i16 as i32),
                0x20b | 0x20e => Pointer::Unsupported,
                _ => return,
            };
            seq.pointer_focus(at, hit, foreground);
            seq.pointer(at, x, y, hit, event);
        });
    }
    CallNextHookEx(0, code, w, l)
}

pub(super) fn named(vk: u16) -> Option<&'static str> {
    const KEYS: &[&str] = &[
        "a",
        "b",
        "c",
        "d",
        "e",
        "f",
        "g",
        "h",
        "i",
        "j",
        "k",
        "l",
        "m",
        "n",
        "o",
        "p",
        "q",
        "r",
        "s",
        "t",
        "u",
        "v",
        "w",
        "x",
        "y",
        "z",
        "0",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "f1",
        "f2",
        "f3",
        "f4",
        "f5",
        "f6",
        "f7",
        "f8",
        "f9",
        "f10",
        "f11",
        "f12",
        "up",
        "down",
        "left",
        "right",
        "enter",
        "space",
        "tab",
        "backspace",
        "delete",
        "esc",
        "ctrl",
        "shift",
        "alt",
        "win",
        "home",
        "end",
        "pageup",
        "pagedown",
        "insert",
        "printscreen",
    ];
    let canonical = match vk {
        0xa0 | 0xa1 => 0x10,
        0xa2 | 0xa3 => 0x11,
        0xa4 | 0xa5 => 0x12,
        0x5c => 0x5b,
        _ => vk,
    };
    KEYS.iter().copied().find(|name| {
        crate::input::action::lookup_vk(name).is_some_and(|(code, _)| code == canonical)
    })
}
fn native_password(h: isize) -> bool {
    let mut class = [0u16; 100];
    let n = unsafe { GetClassNameW(h, class.as_mut_ptr(), class.len() as i32) };
    if n <= 0 {
        return true;
    }
    let class = String::from_utf16_lossy(&class[..n as usize]).to_ascii_lowercase();
    if class != "edit" && !class.starts_with("richedit") {
        return false;
    }
    let mut mask = 0;
    unsafe {
        GetWindowLongPtrW(h, -16) & 0x20 != 0
            || SendMessageTimeoutW(h, 0xd2, 0, 0, 3, 12, &mut mask) == 0
            || mask != 0
    }
}

// Accept only native controls with known password semantics. A custom canvas or a
// browser surface needs a cooperating source that can identify protected child fields.
fn command_control(expected: isize, rect: MonitorRect) -> Option<isize> {
    let (top, child) = focus()?;
    if top != expected || !win32::cursor_pos().is_some_and(|p| in_rect(rect, p.x, p.y)) {
        return None;
    }
    let mut class = [0u16; 64];
    let n = unsafe { GetClassNameW(child, class.as_mut_ptr(), 64) };
    if n <= 0 {
        return None;
    }
    let name = String::from_utf16_lossy(&class[..n as usize]).to_ascii_lowercase();
    if name == "edit" || name.starts_with("richedit") {
        if native_password(child) {
            return None;
        }
    } else if !matches!(
        name.as_str(),
        "button" | "listbox" | "systreeview32" | "syslistview32"
    ) {
        return None;
    }
    if focus() != Some((top, child)) {
        return None;
    }
    Some(child)
}

unsafe extern "system" fn proc(h: isize, msg: u32, w: usize, l: isize) -> isize {
    if msg == 0x201 || (msg == 0xa1 && w == 2) {
        CTX.with_borrow(|slot| {
            if let Some(c) = slot {
                c.seq.lock().unwrap().finish();
            }
        });
        return 0;
    }
    if msg == 0x4a && l != 0 {
        return CTX.with_borrow(|slot| {
            let Some(c) = slot else { return 0 };
            if c.isolated.is_some() {
                return 0;
            }
            if !c.geometry_valid() {
                c.seq
                    .lock()
                    .unwrap()
                    .pause(c.start.elapsed().as_millis() as u64, "geometry_changed");
                return 0;
            }
            let Some((top, focused)) = focus() else {
                return 0;
            };
            // The destination accepted the nonce only after opting in; no inbound payload
            // is inspected before foreground, actual focused child and recording ownership.
            if w as isize != focused || c.offered != Some(top) || native_password(focused) {
                return 0;
            }
            if !c.seq.lock().unwrap().accepts_key(Some(top)) {
                return 0;
            }
            let data = &*(l as *const CopyData);
            if data.kind != KEY_EDGE || data.len != 24 || data.ptr.is_null() {
                return 0;
            }
            let bytes = std::slice::from_raw_parts(data.ptr, 24);
            if &bytes[..16] != c.nonce.as_bytes()
                || bytes[23] != 1
                || bytes[22] != 0
                || bytes[21] > 1
                || bytes[20] > 1
            {
                return 0;
            }
            let vk = u16::from_le_bytes([bytes[16], bytes[17]]);
            let scan = u16::from_le_bytes([bytes[18], bytes[19]]);
            let key = if let Some(key) = named(vk) {
                Key::Named { key }
            } else if (1..=127).contains(&scan) {
                Key::Physical {
                    scan_code: scan,
                    extended: bytes[20] != 0,
                }
            } else {
                c.seq.lock().unwrap().unknown_key();
                return 0;
            };
            if focus() != Some((top, focused)) {
                return 0;
            }
            c.seq.lock().unwrap().message_key(
                c.start.elapsed().as_millis() as u64,
                top,
                focused,
                (1..=127).contains(&scan).then_some((scan, bytes[20] != 0)),
                key,
                bytes[21] != 0,
            );
            1
        });
    }
    DefWindowProcW(h, msg, w, l)
}

pub struct Worker {
    pub receiver: isize,
    pub keyboard_ready: Arc<AtomicBool>,
    pub native_commands: Arc<AtomicBool>,
    done: Arc<AtomicBool>,
    join: Option<thread::JoinHandle<()>>,
}
impl Worker {
    pub fn start(
        seq: Arc<Mutex<Sequence>>,
        rect: MonitorRect,
        bound: Option<isize>,
        start: Instant,
        nonce: Uuid,
        isolated: Option<Arc<super::isolated::IsolatedDesktop>>,
    ) -> Result<Self, IpcError> {
        let done = Arc::new(AtomicBool::new(false));
        let keyboard_ready = Arc::new(AtomicBool::new(false));
        let negotiated = keyboard_ready.clone();
        let native_commands = Arc::new(AtomicBool::new(false));
        let sampling = native_commands.clone();
        let flag = done.clone();
        let (tx, rx) = mpsc::sync_channel(1);
        let join = thread::spawn(move || unsafe {
            super::isolated::trace_lifecycle("worker.begin");
            let _desktop_guard = match isolated.as_ref().map(|d| d.enter_thread()).transpose() {
                Ok(guard) => guard,
                Err(error) => {
                    let _ = tx.send(Err(error));
                    return;
                }
            };
            let _private_claim = match isolated
                .as_ref()
                .map(|_| super::desktop::Claim::acquire(super::desktop::Role::Recording))
                .transpose()
            {
                Ok(claim) => claim,
                Err(error) => {
                    let _ = tx.send(Err(error));
                    return;
                }
            };
            SetThreadDpiAwarenessContext(-4);
            let geometry = bound.and_then(crate::window::system::recording_geometry);
            if bound.is_some() && geometry.is_none() {
                let _ = tx.send(Err(IpcError::window_unavailable("绑定窗口不可见")));
                return;
            }
            let module = GetModuleHandleW(std::ptr::null());
            let class = wide(&format!("ScreenTrigger.Recording.{nonce}"));
            let wc = Class {
                style: 0,
                proc: Some(proc),
                extra: 0,
                winextra: 0,
                instance: module,
                icon: 0,
                cursor: 0,
                brush: 16,
                menu: std::ptr::null(),
                name: class.as_ptr(),
            };
            if RegisterClassW(&wc) == 0 {
                return;
            }
            let indicator = CreateWindowExW(
                0x08000000 | 0x80 | 8,
                class.as_ptr(),
                wide(if isolated.is_some() { "录制 · 等待目标焦点 · 点击停止" } else { "ScreenTrigger · 点击停止录制" }).as_ptr(),
                0x80000000 | 0x10000000 | 0x00c00000,
                (rect.x + rect.width - 240).max(rect.x) as i32,
                rect.y as i32,
                240,
                48,
                0,
                0,
                module,
                0,
            );
            if indicator == 0 {
                UnregisterClassW(class.as_ptr(), module);
                return;
            }
            let mut hook = SetWindowsHookExW(14, Some(mouse), module, 0);
            let mut private_keys = None;
            if hook != 0 {
                let opt_in = wide("ScreenTrigger.RecordingInput.v1");
                if bound.is_some() {
                    seq.lock().unwrap().await_bound();
                }
                CTX.set(Some(Context {
                    seq: seq.clone(),
                    start,
                    nonce,
                    indicator,
                    offered: None,
                    isolated: isolated.clone(),
                    geometry,
                }));
                private_keys = isolated.clone().map(super::isolated_keys::ControlKeys::new);
                if let Some(desktop) = &isolated {
                    let activation = if flag.load(Ordering::Acquire) {
                        Err(IpcError::invalid_state("启动录制的请求已结束"))
                    } else {
                        desktop.activate()
                    };
                    if let Err(error) = activation {
                        let _ = tx.send(Err(error));
                        UnhookWindowsHookEx(hook);
                        CTX.set(None);
                        DestroyWindow(indicator);
                        UnregisterClassW(class.as_ptr(), module);
                        return;
                    }
                }
                if tx.send(Ok(indicator)).is_err() {
                    flag.store(true, Ordering::Release);
                }
                let mut previous = Instant::now();
                let mut commands = super::commands::CommandKeys::default();
                let mut shown_layout = None;
                while !flag.load(Ordering::Acquire) {
                    if isolated
                        .as_ref()
                        .is_some_and(|d| !d.alive() || !d.is_input_desktop())
                    {
                        let mut state = seq.lock().unwrap();
                        if matches!(state.status, Status::Recording | Status::Paused) {
                            state.source_failed("isolated_desktop_closed");
                        }
                        break;
                    }
                    while let Some(message) = crate::winmessages::pop(0, 0) {
                        message.dispatch();
                    }
                    let active_focus = focus().filter(|(top, child)| {
                        isolated
                            .as_ref()
                            .is_none_or(|d| d.owns_window(*top) && d.owns_window(*child))
                    });
                    let current = active_focus.map(|(h, _)| h);
                    let pointer = win32::cursor_pos();
                    let elapsed = start.elapsed().as_millis() as u64;
                    // USER32 can deliver a hook or window message while servicing a call.
                    // The callbacks lock this sequence too. Perform OS queries first and
                    // retain the lock only for pure sequence transitions.
                    let geometry_valid = CTX.with_borrow(|ctx| ctx.as_ref().is_some_and(Context::geometry_valid));
                    let known_message_control = seq.lock().unwrap().message_control().is_some();
                    let message_control = if known_message_control {
                        active_focus.map(|(_, child)| child).filter(|child| !native_password(*child))
                    } else {
                        None
                    };
                    let mut state = seq.lock().unwrap();
                    if matches!(state.status, Status::Finished | Status::Cancelled) {
                        break;
                    }
                    if state.status == Status::Recording && !geometry_valid {
                        state.pause(elapsed, "geometry_changed");
                    }
                    if state.status == Status::Recording
                        && previous.elapsed() > Duration::from_millis(500)
                    {
                        state.source_failed("capture_thread_stalled");
                    }
                    previous = Instant::now();
                    state.focus(
                        elapsed,
                        current,
                        pointer.is_some_and(|p| in_rect(rect, p.x, p.y)),
                    );
                    if known_message_control {
                        state.observe_message_control(message_control);
                    }
                    let status = state.status;
                    let eligible = state.accepts_key(current);
                    drop(state);
                    if matches!(status, Status::Finished | Status::Cancelled) {
                        break;
                    }
                    if status == Status::Paused && hook != 0 {
                        UnhookWindowsHookEx(hook);
                        hook = 0;
                    }
                    if status == Status::Recording && hook == 0 {
                        hook = SetWindowsHookExW(14, Some(mouse), module, 0);
                        if hook == 0 {
                            seq.lock().unwrap().source_failed("capture_unavailable");
                            break;
                        }
                    }
                    let opted_in = isolated.is_none()
                        && current.is_some_and(|h| GetPropW(h, opt_in.as_ptr()) != 0);
                    let mut ordinary = eligible
                        && !opted_in
                        && current.is_some_and(|h| command_control(h, rect).is_some());
                    if let Some(keys) = &mut private_keys {
                        if ordinary {
                            let top = current.unwrap();
                            let desktop = isolated.as_ref().unwrap();
                            ordinary = keys.sample(&seq, elapsed, top, || {
                                if !desktop.is_input_desktop() || !desktop.owns_window(top) {
                                    return None;
                                }
                                command_control(top, rect)
                                    .filter(|child| desktop.owns_window(*child))
                            });
                        } else {
                            keys.reset(&seq);
                        }
                    } else if ordinary {
                        let top = current.unwrap();
                        commands.sample(&seq, elapsed, top, || command_control(top, rect));
                    } else {
                        commands.reset(&mut seq.lock().unwrap());
                    }
                    sampling.store(ordinary, Ordering::Release);
                    if eligible
                        && opted_in
                        && focus().is_some_and(|(_, child)| !native_password(child))
                    {
                        let h = current.unwrap();
                        let send = CTX.with_borrow_mut(|slot| {
                            let c = slot.as_mut().unwrap();
                            let send = c.offered != Some(h);
                            c.offered = Some(h);
                            send
                        });
                        if send {
                            let cd = CopyData {
                                kind: KEY_OFFER,
                                len: 16,
                                ptr: nonce.as_bytes().as_ptr(),
                            };
                            let mut rsp = 0;
                            SendMessageTimeoutW(
                                h,
                                0x4a,
                                indicator as usize,
                                (&cd as *const CopyData) as isize,
                                2,
                                50,
                                &mut rsp,
                            );
                            negotiated.store(rsp == 1, Ordering::Release);
                        }
                    } else {
                        negotiated.store(ordinary, Ordering::Release);
                        CTX.with_borrow_mut(|slot| slot.as_mut().unwrap().offered = None);
                    }
                    if isolated.is_some() {
                        let layout = active_focus.map(|(_, child)| {
                            GetKeyboardLayout(GetWindowThreadProcessId(child, std::ptr::null_mut())) as u16
                        });
                        if shown_layout != Some((layout, ordinary)) {
                            let language = match layout.map(|id| id & 0x03ff) {
                                Some(9) => "英文布局".to_owned(),
                                Some(4) => "中文布局".to_owned(),
                                _ => layout.map_or_else(String::new, |id| format!("布局 {id:04X}")),
                            };
                            let status = if ordinary { "键盘已就绪" } else { "等待目标焦点" };
                            SetWindowTextW(indicator, wide(&format!("{status} {language} · 点击停止")).as_ptr());
                            shown_layout = Some((layout, ordinary));
                        }
                    }
                    thread::sleep(Duration::from_millis(4));
                }
                if hook != 0 {
                    super::isolated::trace_lifecycle("worker.unhook.begin");
                    UnhookWindowsHookEx(hook);
                    super::isolated::trace_lifecycle("worker.unhook.end");
                }
                CTX.set(None);
            }
            if let Some(keys) = &mut private_keys {
                keys.stop();
            }
            super::isolated::trace_lifecycle("worker.destroy-window.begin");
            DestroyWindow(indicator);
            UnregisterClassW(class.as_ptr(), module);
            super::isolated::trace_lifecycle("worker.destroy-window.end");
            if let Some(desktop) = &isolated {
                let _ = desktop.restore();
            }
            drop(private_keys);
            super::isolated::trace_lifecycle("worker.end");
        });
        let receiver = match rx.recv_timeout(Duration::from_secs(2)) {
            Ok(Ok(hwnd)) => hwnd,
            reply => {
                done.store(true, Ordering::Release);
                let _ = join.join();
                return Err(reply.ok().and_then(Result::err).unwrap_or_else(|| {
                    IpcError::new("CAPABILITY_DENIED", "录制指示窗或鼠标采集不可用", false)
                }));
            }
        };
        Ok(Self {
            receiver,
            keyboard_ready,
            native_commands,
            done,
            join: Some(join),
        })
    }
    pub fn stop(&self) {
        self.done.store(true, Ordering::Release);
    }
    pub(super) fn finished(&self) -> bool {
        self.join.as_ref().is_none_or(|thread| thread.is_finished())
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        self.stop();
        if let Some(j) = self.join.take() {
            let _ = j.join();
        }
    }
}

#[cfg(test)]
mod lifecycle_tests {
    use super::*;

    #[test]
    fn reader_shutdown_can_complete_a_synchronous_gui_notification() {
        // A message-only control on this test thread. No desktop switch or input is used.
        let hwnd = unsafe {
            CreateWindowExW(0, wide("STATIC").as_ptr(), wide("").as_ptr(), 0,
                0, 0, 0, 0, -3, 0, GetModuleHandleW(std::ptr::null()), 0)
        };
        assert_ne!(hwnd, 0);
        let (reply, result) = mpsc::channel();
        let reader = thread::spawn(move || {
            let mut value = 0;
            let handled = unsafe { SendMessageTimeoutW(hwnd, 0, 0, 0, 3, 1000, &mut value) };
            reply.send(handled != 0).unwrap();
        });
        crate::winmessages::join_servicing_sent_messages(reader);
        unsafe { DestroyWindow(hwnd); }
        assert!(result.recv().unwrap());
    }

    #[test]
    fn a_terminal_worker_can_retire_while_its_desktop_thread_is_still_cleaning_up() {
        let (release, wait) = mpsc::channel();
        let (exited, reap) = mpsc::channel();
        let done = Arc::new(AtomicBool::new(false));
        let worker = Worker {
            receiver: 0,
            keyboard_ready: Arc::new(AtomicBool::new(false)),
            native_commands: Arc::new(AtomicBool::new(false)),
            done: done.clone(),
            join: Some(thread::spawn(move || {
                let _ = wait.recv();
                let _ = exited.send(());
            })),
        };
        worker.stop();
        let mut retired = vec![super::super::RetiredWorker { worker, desktop: None }];
        retired.retain(|old| !old.finished());
        assert_eq!(retired.len(), 1);
        release.send(()).unwrap();
        drop(retired); // Closing the registry waits for the retained thread to return.
        reap.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(done.load(Ordering::Acquire));
    }
}
