use super::{Identity, Observation};
use crate::types::region::Rect;
use std::mem::{size_of, zeroed};

#[repr(C)]
#[derive(Default, Copy, Clone)]
struct NativeRect {
    left: i32,
    top: i32,
    right: i32,
    bottom: i32,
}
#[repr(C)]
#[derive(Default)]
struct Point {
    x: i32,
    y: i32,
}
#[repr(C)]
struct Monitor {
    size: u32,
    screen: NativeRect,
    work: NativeRect,
    flags: u32,
    name: [u16; 32],
}
#[repr(C)]
#[derive(Default)]
struct FileTime {
    low: u32,
    high: u32,
}
#[repr(C)]
#[derive(Default)]
struct SystemTime {
    year: u16,
    month: u16,
    weekday: u16,
    day: u16,
    hour: u16,
    minute: u16,
    second: u16,
    millis: u16,
}

#[link(name = "user32")]
extern "system" {
    fn EnumWindows(cb: unsafe extern "system" fn(isize, isize) -> i32, param: isize) -> i32;
    fn GetWindowThreadProcessId(hwnd: isize, pid: *mut u32) -> u32;
    fn GetWindowTextW(hwnd: isize, text: *mut u16, len: i32) -> i32;
    fn GetClientRect(hwnd: isize, rect: *mut NativeRect) -> i32;
    fn ClientToScreen(hwnd: isize, point: *mut Point) -> i32;
    fn GetDpiForWindow(hwnd: isize) -> u32;
    fn IsWindowVisible(hwnd: isize) -> i32;
    fn IsIconic(hwnd: isize) -> i32;
    fn GetForegroundWindow() -> *mut std::ffi::c_void;
    fn GetShellWindow() -> isize;
    fn WindowFromPoint(point: Point) -> isize;
    fn GetAncestor(hwnd: isize, flags: u32) -> isize;
    fn MonitorFromPoint(point: Point, flags: u32) -> isize;
    fn GetMonitorInfoW(monitor: *mut std::ffi::c_void, info: *mut std::ffi::c_void) -> i32;
}
#[link(name = "kernel32")]
extern "system" {
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> isize;
    fn QueryFullProcessImageNameW(process: isize, flags: u32, buf: *mut u16, len: *mut u32) -> i32;
    fn GetProcessTimes(
        process: isize,
        creation: *mut FileTime,
        exit: *mut FileTime,
        kernel: *mut FileTime,
        user: *mut FileTime,
    ) -> i32;
    fn FileTimeToSystemTime(time: *const FileTime, out: *mut SystemTime) -> i32;
    fn CloseHandle(handle: isize) -> i32;
}

fn process(hwnd: isize) -> Option<(Identity, String)> {
    unsafe {
        let mut pid = 0;
        if GetWindowThreadProcessId(hwnd, &mut pid) == 0 || pid == std::process::id() {
            return None;
        }
        let handle = OpenProcess(0x1000, 0, pid);
        if handle == 0 {
            return None;
        }
        let mut creation = FileTime::default();
        let mut end = FileTime::default();
        let mut kernel = FileTime::default();
        let mut user = FileTime::default();
        let mut name = vec![0u16; 32768];
        let mut size = name.len() as u32;
        let valid = GetProcessTimes(handle, &mut creation, &mut end, &mut kernel, &mut user) != 0;
        let has_name = QueryFullProcessImageNameW(handle, 0, name.as_mut_ptr(), &mut size) != 0;
        CloseHandle(handle);
        let mut t = SystemTime::default();
        if !valid || FileTimeToSystemTime(&creation, &mut t) == 0 {
            return None;
        }
        let ticks = ((creation.high as u64) << 32) | creation.low as u64;
        let started = format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:07}Z",
            t.year,
            t.month,
            t.day,
            t.hour,
            t.minute,
            t.second,
            ticks % 10_000_000
        );
        let path = if has_name {
            String::from_utf16_lossy(&name[..size as usize])
        } else {
            String::new()
        };
        Some((
            Identity { hwnd, pid, started },
            path.rsplit('\\').next().unwrap_or("").to_owned(),
        ))
    }
}
pub fn observe(hwnd: isize) -> Option<Observation> {
    let (identity, process_name) = process(hwnd)?;
    unsafe {
        let mut rect = NativeRect::default();
        let mut origin = Point::default();
        if GetClientRect(hwnd, &mut rect) == 0 || ClientToScreen(hwnd, &mut origin) == 0 {
            return None;
        }
        let mut title = [0u16; 1024];
        let n = GetWindowTextW(hwnd, title.as_mut_ptr(), title.len() as i32).max(0) as usize;
        Some(Observation {
            identity,
            title: String::from_utf16_lossy(&title[..n]),
            process_name,
            rect: Rect {
                x: origin.x.into(),
                y: origin.y.into(),
                width: i64::from(rect.right) - i64::from(rect.left),
                height: i64::from(rect.bottom) - i64::from(rect.top),
            },
            dpi: GetDpiForWindow(hwnd),
            visible: IsWindowVisible(hwnd) != 0,
            minimized: IsIconic(hwnd) != 0,
            foreground: GetForegroundWindow() as isize == hwnd,
        })
    }
}
pub fn enumerate() -> Vec<Observation> {
    unsafe extern "system" fn collect(hwnd: isize, data: isize) -> i32 {
        if hwnd != GetShellWindow() && IsWindowVisible(hwnd) != 0 {
            if let Some(w) = observe(hwnd) {
                if !w.title.is_empty() {
                    (*(data as *mut Vec<Observation>)).push(w);
                }
            }
        }
        1
    }
    let mut windows = Vec::new();
    unsafe {
        EnumWindows(collect, &mut windows as *mut _ as isize);
    }
    windows
}
pub fn monitor_at(x: i64, y: i64) -> Option<String> {
    let p = Point {
        x: x.try_into().ok()?,
        y: y.try_into().ok()?,
    };
    unsafe {
        let h = MonitorFromPoint(p, 0);
        let mut info: Monitor = zeroed();
        info.size = size_of::<Monitor>() as u32;
        if h == 0 || GetMonitorInfoW(h as *mut _, (&mut info as *mut Monitor).cast()) == 0 {
            return None;
        }
        Some(String::from_utf16_lossy(
            &info.name[..info
                .name
                .iter()
                .position(|v| *v == 0)
                .unwrap_or(info.name.len())],
        ))
    }
}
pub fn window_at(x: i64, y: i64) -> Option<isize> {
    let p = Point {
        x: x.try_into().ok()?,
        y: y.try_into().ok()?,
    };
    unsafe {
        let h = WindowFromPoint(p);
        (h != 0).then(|| GetAncestor(h, 2))
    }
}
