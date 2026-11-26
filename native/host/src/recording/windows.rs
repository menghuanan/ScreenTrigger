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
    (root(focused)==Some(foreground)).then_some((foreground,focused))
}
fn in_rect(rect: MonitorRect, x: i64, y: i64) -> bool {
    x >= rect.x && y >= rect.y && x - rect.x < rect.width && y - rect.y < rect.height
}
unsafe extern "system" fn mouse(code: i32, w: usize, l: isize) -> isize {
    if code == 0 && l != 0 {
        CTX.with_borrow(|slot| {
            let Some(c) = slot else { return };
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
                *h != c.indicator && pid != std::process::id()
            });
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
            seq.pointer(at, x, y, hit, event);
        });
    }
    CallNextHookEx(0, code, w, l)
}

fn named(vk: u16) -> Option<&'static str> {
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
            let mut seq = c.seq.lock().unwrap();
            if !seq.accepts_key(Some(top)) {
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
                seq.unknown_key();
                return 0;
            };
            if focus() != Some((top, focused)) {
                return 0;
            }
            seq.message_key(
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
    ) -> Result<Self, IpcError> {
        let done = Arc::new(AtomicBool::new(false));
        let keyboard_ready = Arc::new(AtomicBool::new(false));
        let negotiated = keyboard_ready.clone();
        let native_commands = Arc::new(AtomicBool::new(false));
        let sampling = native_commands.clone();
        let flag = done.clone();
        let (tx, rx) = mpsc::sync_channel(1);
        let geometry = bound.and_then(crate::window::system::recording_geometry);
        if bound.is_some() && geometry.is_none() {
            return Err(IpcError::window_unavailable("绑定窗口不可见"));
        }
        let join = thread::spawn(move || unsafe {
            SetThreadDpiAwarenessContext(-4);
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
                wide("ScreenTrigger · 点击停止录制").as_ptr(),
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
                    geometry,
                }));
                let _ = tx.send(indicator);
                let mut previous = Instant::now();
                let mut commands = super::commands::CommandKeys::default();
                while !flag.load(Ordering::Acquire) {
                    while let Some(message) = crate::winmessages::pop(0, 0) {
                        message.dispatch();
                    }
                    let active_focus = focus();
                    let current = active_focus.map(|(h, _)| h);
                    let pointer = win32::cursor_pos();
                    let elapsed = start.elapsed().as_millis() as u64;
                    let mut state = seq.lock().unwrap();
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
                    if state.message_control().is_some() {
                        state.observe_message_control(
                            active_focus
                                .map(|(_, child)| child)
                                .filter(|child| !native_password(*child)),
                        );
                    }
                    let ended = matches!(state.status, Status::Finished | Status::Cancelled);
                    if state.status == Status::Paused && hook != 0 {
                        UnhookWindowsHookEx(hook);
                        hook = 0;
                    }
                    if state.status == Status::Recording && hook == 0 {
                        hook = SetWindowsHookExW(14, Some(mouse), module, 0);
                        if hook == 0 {
                            state.source_failed("capture_unavailable");
                        }
                    }
                    let eligible = state.accepts_key(current);
                    let opted_in = current.is_some_and(|h| GetPropW(h, opt_in.as_ptr()) != 0);
                    let ordinary = eligible
                        && !opted_in
                        && current.is_some_and(|h| command_control(h, rect).is_some());
                    if ordinary {
                        let top = current.unwrap();
                        commands.sample(&mut state, elapsed, top, || command_control(top, rect));
                    } else {
                        commands.reset(&mut state);
                    }
                    sampling.store(ordinary, Ordering::Release);
                    drop(state);
                    if ended {
                        break;
                    }
                    if eligible
                        && focus().is_some_and(|(_, child)| !native_password(child))
                        && current.is_some_and(|h| GetPropW(h, opt_in.as_ptr()) != 0)
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
                    thread::sleep(Duration::from_millis(4));
                }
                if hook != 0 {
                    UnhookWindowsHookEx(hook);
                }
                CTX.set(None);
            }
            DestroyWindow(indicator);
            UnregisterClassW(class.as_ptr(), module);
        });
        let receiver = rx
            .recv_timeout(Duration::from_secs(2))
            .map_err(|_| IpcError::new("CAPABILITY_DENIED", "录制指示窗或鼠标采集不可用", false))?;
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
}
impl Drop for Worker {
    fn drop(&mut self) {
        self.stop();
        if let Some(j) = self.join.take() {
            let _ = j.join();
        }
    }
}
