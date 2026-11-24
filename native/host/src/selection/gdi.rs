//! 框选用 GDI/BitBlt 进程内捕获（TASK-008）。
//!
//! **为何进程内**：框选命令经单线程 Router 处理（`Route::HostHandled`）；若在
//! Router 线程内再经 IPC 请求 Python 捕获，会自锁（Router 被占）。故框选捕获在
//! Rust 进程内完成，映射后端 `backend/capture/gdi.py` 已验证手法（GetDC(NULL) +
//! CreateCompatibleBitmap + BitBlt + GetDIBits(32bpp top-down) → BGR）。
//!
//! **覆盖层排除（R04）**：BitBlt **不带** `CAPTUREBLT`；配合覆盖层窗口
//! `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`（见 wiring），使覆盖层可见时
//! 捕获帧不含覆盖层像素（真机证据见 reports/TASK-008/）。
//!
//! 通过**原始 FFI**（`extern "system"`）直连 user32/gdi32，避免 windows crate
//! 逐版本签名漂移；句柄按 `*mut c_void` 处理。仅 Windows 编译。

#![allow(non_snake_case)]

use std::ffi::c_void;

/// 原始 BGR 帧（去 Alpha；长度 = width*height*3）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawFrame {
    pub width: i32,
    pub height: i32,
    pub bgr: Vec<u8>,
}

#[cfg(target_os = "windows")]
mod imp {
    use super::*;

    pub const SRCCOPY: u32 = 0x00CC_0020;
    pub const DIB_RGB_COLORS: u32 = 0;
    pub const BI_RGB: u32 = 0;
    /// 覆盖层排除：WDA_EXCLUDEFROMCAPTURE（Windows 10 2004+）。
    pub const WDA_EXCLUDEFROMCAPTURE: u32 = 0x0000_0011;

    #[repr(C)]
    struct BitmapInfoHeader {
        bi_size: u32,
        bi_width: i32,
        bi_height: i32,
        bi_planes: u16,
        bi_bit_count: u16,
        bi_compression: u32,
        bi_size_image: u32,
        bi_x_pels: i32,
        bi_y_pels: i32,
        bi_clr_used: u32,
        bi_clr_important: u32,
    }

    #[repr(C)]
    struct BitmapInfo {
        header: BitmapInfoHeader,
        colors: [u32; 3],
    }

    #[link(name = "user32")]
    extern "system" {
        fn GetDC(hwnd: *mut c_void) -> *mut c_void;
        fn ReleaseDC(hwnd: *mut c_void, hdc: *mut c_void) -> i32;
        fn SetWindowDisplayAffinity(hwnd: *mut c_void, affinity: u32) -> i32;
    }

    #[link(name = "gdi32")]
    extern "system" {
        fn CreateCompatibleDC(hdc: *mut c_void) -> *mut c_void;
        fn CreateCompatibleBitmap(hdc: *mut c_void, w: i32, h: i32) -> *mut c_void;
        fn SelectObject(hdc: *mut c_void, obj: *mut c_void) -> *mut c_void;
        fn BitBlt(
            dst: *mut c_void,
            x: i32,
            y: i32,
            w: i32,
            h: i32,
            src: *mut c_void,
            x1: i32,
            y1: i32,
            rop: u32,
        ) -> i32;
        fn GetDIBits(
            hdc: *mut c_void,
            hbm: *mut c_void,
            start: u32,
            lines: u32,
            bits: *mut c_void,
            bmi: *mut BitmapInfo,
            usage: u32,
        ) -> i32;
        fn DeleteObject(obj: *mut c_void) -> i32;
        fn DeleteDC(hdc: *mut c_void) -> i32;
    }

    /// 抓取物理矩形 `[x,x+w)×[y,y+h)`（支持负坐标）；覆盖层因 WDA 排除而不入帧。
    pub fn capture_bgr(x: i32, y: i32, w: i32, h: i32) -> Result<RawFrame, String> {
        if w <= 0 || h <= 0 {
            return Err("捕获尺寸必须为正".to_string());
        }
        unsafe {
            let screen = GetDC(std::ptr::null_mut());
            if screen.is_null() {
                return Err("GetDC 失败".to_string());
            }
            let mem = CreateCompatibleDC(screen);
            let bmp = CreateCompatibleBitmap(screen, w, h);
            if mem.is_null() || bmp.is_null() {
                if !bmp.is_null() {
                    DeleteObject(bmp);
                }
                if !mem.is_null() {
                    DeleteDC(mem);
                }
                ReleaseDC(std::ptr::null_mut(), screen);
                return Err("创建兼容 DC/位图失败".to_string());
            }
            let previous = SelectObject(mem, bmp);
            // 不带 CAPTUREBLT：分层/已排除捕获的覆盖层不入帧
            let ok = BitBlt(mem, 0, 0, w, h, screen, x, y, SRCCOPY);
            if ok == 0 {
                SelectObject(mem, previous);
                DeleteObject(bmp);
                DeleteDC(mem);
                ReleaseDC(std::ptr::null_mut(), screen);
                return Err("BitBlt 失败（受保护内容或显示器变化）".to_string());
            }
            let mut bmi: BitmapInfo = std::mem::zeroed();
            bmi.header.bi_size = std::mem::size_of::<BitmapInfoHeader>() as u32;
            bmi.header.bi_width = w;
            bmi.header.bi_height = -h; // 负值 → 自上而下
            bmi.header.bi_planes = 1;
            bmi.header.bi_bit_count = 32;
            bmi.header.bi_compression = BI_RGB;

            let mut buffer = vec![0u8; (w as usize) * (h as usize) * 4];
            let scanned = GetDIBits(
                mem,
                bmp,
                0,
                h as u32,
                buffer.as_mut_ptr() as *mut c_void,
                &mut bmi,
                DIB_RGB_COLORS,
            );
            SelectObject(mem, previous);
            DeleteObject(bmp);
            DeleteDC(mem);
            ReleaseDC(std::ptr::null_mut(), screen);
            if scanned != h {
                return Err("GetDIBits 行数不完整".to_string());
            }
            let mut bgr = vec![0u8; (w as usize) * (h as usize) * 3];
            for i in 0..(w as usize) * (h as usize) {
                bgr[i * 3] = buffer[i * 4];
                bgr[i * 3 + 1] = buffer[i * 4 + 1];
                bgr[i * 3 + 2] = buffer[i * 4 + 2];
            }
            Ok(RawFrame { width: w, height: h, bgr })
        }
    }

    /// 对窗口设置捕获排除（覆盖层可见但不出现在截图中）。返回是否成功。
    ///
    /// `hwnd` 为窗口句柄（`*mut c_void`）。`WDA_EXCLUDEFROMCAPTURE` 需 Win10 2004+；
    /// 失败由调用方记录并回退（回退策略见 wiring）。
    pub fn exclude_from_capture(hwnd: *mut c_void) -> bool {
        unsafe { SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE) != 0 }
    }
}

#[cfg(target_os = "windows")]
pub use imp::{capture_bgr, exclude_from_capture, WDA_EXCLUDEFROMCAPTURE};

#[cfg(not(target_os = "windows"))]
pub fn capture_bgr(_x: i32, _y: i32, _w: i32, _h: i32) -> Result<RawFrame, String> {
    Err("非 Windows 平台不支持 GDI 捕获".to_string())
}

#[cfg(not(target_os = "windows"))]
pub fn exclude_from_capture(_hwnd: *mut std::ffi::c_void) -> bool {
    false
}
