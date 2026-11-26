//! Windows 原始 FFI：SendInput / 光标 / 虚拟屏 / 前台 / 实际显示器集合
//! （TASK-017 / 07§5）。
//!
//! **为何 raw FFI**：与既有 `native/host/src/selection/gdi.rs` 同款先例——用
//! `#[link(name="user32")] extern "system"` 直连，避免 windows crate 逐版本签名
//! 漂移；**零新增依赖**、离线可编译。结构体 `INPUT/MOUSEINPUT/KEYBDINPUT` 手写
//! `#[repr(C)]`（同 gdi.rs 手写 `BitmapInfo`）。
//!
//! **要点**：
//! - 绝对鼠标归一化用 **VIRTUALDESK + 虚拟屏起点/尺寸**（`SM_X/YVIRTUALSCREEN`、
//!   `SM_CX/CYVIRTUALSCREEN`），**不得按主屏宽高单独换算**（07§5）；
//! - Unicode 文本走 `KEYEVENTF_UNICODE`，以 UTF-16 单元（含代理对）逐个成对派发；
//! - 实际显示器集合用 `EnumDisplayMonitors` + `GetMonitorInfoW`（06§不变量 3：
//!   不能只验证虚拟屏幕总包围盒）。
//!
//! 非 Windows 平台提供同签名桩（返回错误/空集），保证跨平台编译。

#![allow(non_snake_case)]

use super::model::{MonitorRect, MouseButton, Point, VirtualScreen};

#[cfg(target_os = "windows")]
mod imp {
    use super::*;
    use std::ffi::c_void;

    const INPUT_MOUSE: u32 = 0;
    const INPUT_KEYBOARD: u32 = 1;

    const MOUSEEVENTF_MOVE: u32 = 0x0001;
    const MOUSEEVENTF_LEFTDOWN: u32 = 0x0002;
    const MOUSEEVENTF_LEFTUP: u32 = 0x0004;
    const MOUSEEVENTF_RIGHTDOWN: u32 = 0x0008;
    const MOUSEEVENTF_RIGHTUP: u32 = 0x0010;
    const MOUSEEVENTF_MIDDLEDOWN: u32 = 0x0020;
    const MOUSEEVENTF_MIDDLEUP: u32 = 0x0040;
    const MOUSEEVENTF_WHEEL: u32 = 0x0800;
    const MOUSEEVENTF_ABSOLUTE: u32 = 0x8000;
    const MOUSEEVENTF_VIRTUALDESK: u32 = 0x4000;

    const KEYEVENTF_EXTENDEDKEY: u32 = 0x0001;
    const KEYEVENTF_KEYUP: u32 = 0x0002;
    const KEYEVENTF_UNICODE: u32 = 0x0004;

    /// 一格滚轮 = 120（WHEEL_DELTA）。
    const WHEEL_DELTA: i32 = 120;

    const SM_XVIRTUALSCREEN: i32 = 76;
    const SM_YVIRTUALSCREEN: i32 = 77;
    const SM_CXVIRTUALSCREEN: i32 = 78;
    const SM_CYVIRTUALSCREEN: i32 = 79;

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct MouseInput {
        dx: i32,
        dy: i32,
        mouse_data: u32,
        flags: u32,
        time: u32,
        extra: usize,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct KeybdInput {
        vk: u16,
        scan: u16,
        flags: u32,
        time: u32,
        extra: usize,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct HardwareInput {
        msg: u32,
        param_l: u16,
        param_h: u16,
    }

    #[repr(C)]
    union InputUnion {
        mi: MouseInput,
        ki: KeybdInput,
        hi: HardwareInput,
    }

    #[repr(C)]
    struct Input {
        kind: u32,
        u: InputUnion,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct PointRaw {
        x: i32,
        y: i32,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct RectRaw {
        left: i32,
        top: i32,
        right: i32,
        bottom: i32,
    }

    #[repr(C)]
    struct MonitorInfo {
        cb_size: u32,
        rc_monitor: RectRaw,
        rc_work: RectRaw,
        dw_flags: u32,
    }

    type MonitorEnumProc =
        unsafe extern "system" fn(*mut c_void, *mut c_void, *mut RectRaw, *mut c_void) -> i32;

    #[link(name = "user32")]
    extern "system" {
        fn SendInput(cinputs: u32, pinputs: *const Input, cb_size: i32) -> u32;
        fn GetSystemMetrics(nindex: i32) -> i32;
        fn SetCursorPos(x: i32, y: i32) -> i32;
        fn GetAsyncKeyState(key: i32) -> i16;
        fn GetCursorPos(lppoint: *mut PointRaw) -> i32;
        fn GetForegroundWindow() -> *mut c_void;
        fn EnumDisplayMonitors(
            hdc: *mut c_void,
            clip: *mut RectRaw,
            callback: MonitorEnumProc,
            data: *mut c_void,
        ) -> i32;
        fn GetMonitorInfoW(monitor: *mut c_void, info: *mut c_void) -> i32;
    }

    /// 单次 SendInput（要求全部输入被接受，否则视为失败 → INPUT_FAILED）。
    fn send(inputs: &[Input]) -> Result<(), String> {
        if inputs.is_empty() {
            return Ok(());
        }
        unsafe {
            let sent = SendInput(
                inputs.len() as u32,
                inputs.as_ptr(),
                std::mem::size_of::<Input>() as i32,
            );
            if sent != inputs.len() as u32 {
                return Err(format!("SendInput 返回 {sent}/{}", inputs.len()));
            }
        }
        Ok(())
    }

    /// 虚拟屏幕矩形（物理像素）。
    pub fn virtual_screen() -> VirtualScreen {
        unsafe {
            VirtualScreen {
                x: GetSystemMetrics(SM_XVIRTUALSCREEN) as i64,
                y: GetSystemMetrics(SM_YVIRTUALSCREEN) as i64,
                cx: GetSystemMetrics(SM_CXVIRTUALSCREEN) as i64,
                cy: GetSystemMetrics(SM_CYVIRTUALSCREEN) as i64,
            }
        }
    }

    pub fn cursor_pos() -> Option<Point> {
        let mut raw = PointRaw { x: 0, y: 0 };
        (unsafe { GetCursorPos(&mut raw) } != 0).then_some(Point { x: raw.x as i64, y: raw.y as i64 })
    }

    pub fn key_is_down(vk: u16) -> bool { (unsafe { GetAsyncKeyState(vk as i32) }) < 0 }

    /// 设置光标位置（绝对，物理像素）。
    pub fn set_cursor_pos(x: i64, y: i64) -> Result<(), String> {
        unsafe {
            if SetCursorPos(x as i32, y as i32) != 0 {
                Ok(())
            } else {
                Err("SetCursorPos 失败".to_string())
            }
        }
    }

    /// 绝对移动（VIRTUALDESK 归一化；dx/dy 映射到 0..65535）。
    pub fn move_absolute(x: i64, y: i64) -> Result<(), String> {
        let vs = virtual_screen();
        if vs.cx <= 1 || vs.cy <= 1 {
            return Err(format!("虚拟屏尺寸非法（cx={} cy={}）", vs.cx, vs.cy));
        }
        // 归一化到 OS 要求的 0..65535（这是 API 坐标映射，不是对输入坐标的 clamp——
        // 输入点是否合法已在上层按实际显示器集合校验）。
        if x < vs.x || y < vs.y || x >= vs.x + vs.cx || y >= vs.y + vs.cy { return Err("显示器布局已变化".into()); }
        let nx = (((x - vs.x) * 65536 + 32768) / vs.cx).min(65535) as i32;
        let ny = (((y - vs.y) * 65536 + 32768) / vs.cy).min(65535) as i32;
        let input = Input {
            kind: INPUT_MOUSE,
            u: InputUnion {
                mi: MouseInput {
                    dx: nx,
                    dy: ny,
                    mouse_data: 0,
                    flags: MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                    time: 0,
                    extra: crate::recording::SELF_INPUT,
                },
            },
        };
        send(&[input])
    }

    /// 鼠标按钮按下/抬起。
    pub fn button(button: MouseButton, down: bool) -> Result<(), String> {
        let flag = match (button, down) {
            (MouseButton::Left, true) => MOUSEEVENTF_LEFTDOWN,
            (MouseButton::Left, false) => MOUSEEVENTF_LEFTUP,
            (MouseButton::Right, true) => MOUSEEVENTF_RIGHTDOWN,
            (MouseButton::Right, false) => MOUSEEVENTF_RIGHTUP,
            (MouseButton::Middle, true) => MOUSEEVENTF_MIDDLEDOWN,
            (MouseButton::Middle, false) => MOUSEEVENTF_MIDDLEUP,
        };
        let input = Input {
            kind: INPUT_MOUSE,
            u: InputUnion {
                mi: MouseInput { dx: 0, dy: 0, mouse_data: 0, flags: flag, time: 0, extra: crate::recording::SELF_INPUT },
            },
        };
        send(&[input])
    }

    /// 滚轮（正数向上，单位格 → ×120）。
    pub fn wheel(delta: i32) -> Result<(), String> {
        let amount = (delta as i64 * WHEEL_DELTA as i64) as u32;
        let input = Input {
            kind: INPUT_MOUSE,
            u: InputUnion {
                mi: MouseInput {
                    dx: 0,
                    dy: 0,
                    mouse_data: amount,
                    flags: MOUSEEVENTF_WHEEL,
                    time: 0,
                    extra: crate::recording::SELF_INPUT,
                },
            },
        };
        send(&[input])
    }

    /// 键盘虚拟键按下/抬起。
    pub fn key(vk: u16, extended: bool, down: bool) -> Result<(), String> {
        let mut flags = 0u32;
        if extended {
            flags |= KEYEVENTF_EXTENDEDKEY;
        }
        if !down {
            flags |= KEYEVENTF_KEYUP;
        }
        let input = Input {
            kind: INPUT_KEYBOARD,
            u: InputUnion {
                ki: KeybdInput { vk, scan: 0, flags, time: 0, extra: crate::recording::SELF_INPUT },
            },
        };
        send(&[input])
    }

    /// Unicode 文本（UTF-16 单元成对派发；自动覆盖代理对/补充平面字符）。
    pub fn unicode_text(text: &str) -> Result<(), String> {
        let mut inputs: Vec<Input> = Vec::new();
        for unit in text.encode_utf16() {
            inputs.push(unicode_input(unit, false));
            inputs.push(unicode_input(unit, true));
        }
        send(&inputs)
    }

    fn unicode_input(scan: u16, keyup: bool) -> Input {
        let mut flags = KEYEVENTF_UNICODE;
        if keyup {
            flags |= KEYEVENTF_KEYUP;
        }
        Input {
            kind: INPUT_KEYBOARD,
            u: InputUnion {
                ki: KeybdInput { vk: 0, scan, flags, time: 0, extra: crate::recording::SELF_INPUT },
            },
        }
    }

    /// 前台窗口句柄（无则 0）。
    pub fn foreground_window() -> u64 {
        unsafe { GetForegroundWindow() as usize as u64 }
    }

    struct EnumCtx {
        rects: Vec<MonitorRect>,
    }

    unsafe extern "system" fn monitor_proc(
        hmon: *mut c_void,
        _hdc: *mut c_void,
        _rect: *mut RectRaw,
        data: *mut c_void,
    ) -> i32 {
        if data.is_null() {
            return 1;
        }
        let ctx = &mut *(data as *mut EnumCtx);
        let mut info: MonitorInfo = std::mem::zeroed();
        info.cb_size = std::mem::size_of::<MonitorInfo>() as u32;
        if GetMonitorInfoW(hmon, (&mut info as *mut MonitorInfo).cast()) != 0 {
            let m = info.rc_monitor;
            ctx.rects.push(MonitorRect {
                x: m.left as i64,
                y: m.top as i64,
                width: (m.right - m.left) as i64,
                height: (m.bottom - m.top) as i64,
            });
        }
        1 // 继续枚举
    }

    /// 实际显示器集合（各显示器矩形；失败兜底主屏）。
    pub fn enum_monitors() -> Vec<MonitorRect> {
        let mut ctx = EnumCtx { rects: Vec::new() };
        unsafe {
            EnumDisplayMonitors(
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                monitor_proc,
                &mut ctx as *mut EnumCtx as *mut c_void,
            );
        }
        ctx.rects
    }
}

#[cfg(not(target_os = "windows"))]
mod imp {
    use super::*;

    pub fn virtual_screen() -> VirtualScreen {
        VirtualScreen { x: 0, y: 0, cx: 0, cy: 0 }
    }
    pub fn cursor_pos() -> Option<Point> { None }
    pub fn key_is_down(_vk: u16) -> bool { false }
    pub fn set_cursor_pos(_x: i64, _y: i64) -> Result<(), String> {
        Err("非 Windows 平台不支持输入".to_string())
    }
    pub fn move_absolute(_x: i64, _y: i64) -> Result<(), String> {
        Err("非 Windows 平台不支持输入".to_string())
    }
    pub fn button(_button: MouseButton, _down: bool) -> Result<(), String> {
        Err("非 Windows 平台不支持输入".to_string())
    }
    pub fn wheel(_delta: i32) -> Result<(), String> {
        Err("非 Windows 平台不支持输入".to_string())
    }
    pub fn key(_vk: u16, _extended: bool, _down: bool) -> Result<(), String> {
        Err("非 Windows 平台不支持输入".to_string())
    }
    pub fn unicode_text(_text: &str) -> Result<(), String> {
        Err("非 Windows 平台不支持输入".to_string())
    }
    pub fn foreground_window() -> u64 {
        0
    }
    pub fn enum_monitors() -> Vec<MonitorRect> {
        Vec::new()
    }
}

pub use imp::{
    button, cursor_pos, key_is_down, enum_monitors, foreground_window, key, move_absolute, set_cursor_pos,
    unicode_text, virtual_screen, wheel,
};
