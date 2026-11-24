//! 框选生产适配器（TASK-008）：把 Tauri 窗口/显示器/GDI 捕获接成端口实现。
//!
//! **覆盖层排除（R04）**：覆盖层建为独立顶层窗口，创建后立即
//! `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`（见 [`super::gdi`]），使覆盖层
//! **可见但不出现在任何 GDI 截图中**；捕获侧 BitBlt 亦不带 `CAPTUREBLT`，双重保障。
//! 真机证据见 `reports/TASK-008/`（`reports/raw/TASK-008/`）。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use serde::Serialize;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};

use crate::types::region::{DisplayInfo, ImageRef, Rect};

use super::gdi;
use super::hostframe;
use super::model::{
    ClockPort, DesktopCapturePort, HostEnvironmentPort, OverlayHandle, PauseOutcome, PortError,
    PortResult, ScriptControlPort, Sleeper,
};
use super::png;
use super::SelectionManager;

// ---------------------------------------------------------------------------
// 主窗口 / 显示器 / 覆盖层
// ---------------------------------------------------------------------------

/// Tauri 宿主环境端口（主窗口隐藏/恢复、显示器枚举、覆盖层生命周期）。
pub struct TauriHostEnv {
    app: AppHandle,
}

impl TauriHostEnv {
    pub fn new(app: AppHandle) -> Self {
        TauriHostEnv { app }
    }

    fn main_window(&self) -> PortResult<tauri::WebviewWindow> {
        self.app
            .get_webview_window("main")
            .ok_or_else(|| PortError::permanent("主窗口不存在"))
    }
}

impl HostEnvironmentPort for TauriHostEnv {
    fn list_displays(&self) -> PortResult<Vec<DisplayInfo>> {
        let monitors = self
            .app
            .available_monitors()
            .map_err(|e| PortError::permanent(format!("枚举显示器失败：{e}")))?;
        let primary_name = self
            .app
            .primary_monitor()
            .ok()
            .flatten()
            .and_then(|m| m.name().map(|s| s.to_string()));
        let mut out = Vec::with_capacity(monitors.len());
        for (i, m) in monitors.iter().enumerate() {
            let name = m
                .name()
                .map(|s| s.to_string())
                .unwrap_or_else(|| format!("display-{i}"));
            let pos = m.position();
            let size = m.size();
            let primary = primary_name.as_deref() == Some(name.as_str());
            out.push(DisplayInfo {
                id: name,
                rect: Rect {
                    x: pos.x as i64,
                    y: pos.y as i64,
                    width: size.width as i64,
                    height: size.height as i64,
                },
                dpi_scale: m.scale_factor(),
                primary,
            });
        }
        Ok(out)
    }

    fn main_window_rect(&self) -> PortResult<Rect> {
        let win = self.main_window()?;
        let pos = win
            .outer_position()
            .map_err(|e| PortError::permanent(format!("读取主窗口位置失败：{e}")))?;
        let size = win
            .outer_size()
            .map_err(|e| PortError::permanent(format!("读取主窗口尺寸失败：{e}")))?;
        Ok(Rect {
            x: pos.x as i64,
            y: pos.y as i64,
            width: size.width as i64,
            height: size.height as i64,
        })
    }

    fn is_main_window_visible(&self) -> bool {
        self.main_window()
            .and_then(|w| w.is_visible().map_err(|e| PortError::permanent(e.to_string())))
            .unwrap_or(true)
    }

    fn set_main_window_visible(&self, visible: bool) -> PortResult<()> {
        let win = self.main_window()?;
        let result = if visible { win.show() } else { win.hide() };
        result.map_err(|e| PortError::permanent(format!("设置主窗口可见性失败：{e}")))
    }

    fn open_overlay(&self, display: &DisplayInfo, selection_id: &str) -> PortResult<OverlayHandle> {
        let label = overlay_label(selection_id);
        let builder = WebviewWindowBuilder::new(
            &self.app,
            &label,
            WebviewUrl::App("index.html".into()),
        )
        .title("ScreenTrigger 框选")
        .decorations(false)
        .transparent(true)
        .always_on_top(true)
        .skip_taskbar(true)
        .position(display.rect.x as f64, display.rect.y as f64)
        .inner_size(display.rect.width as f64, display.rect.height as f64);
        let win = builder
            .build()
            .map_err(|e| PortError::permanent(format!("创建覆盖层失败：{e}")))?;
        // 覆盖层排除捕获（R04）：WDA_EXCLUDEFROMCAPTURE
        match win.hwnd() {
            Ok(hwnd) => {
                let ok = gdi::exclude_from_capture(hwnd.0 as *mut std::ffi::c_void);
                if !ok {
                    eprintln!(
                        "[selection] SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) 失败：覆盖层可能进入截图（需 Win10 2004+）"
                    );
                }
            }
            Err(e) => eprintln!("[selection] 获取覆盖层 HWND 失败：{e}"),
        }
        Ok(OverlayHandle(label))
    }

    fn close_overlay(&self, handle: &OverlayHandle) -> PortResult<()> {
        if let Some(win) = self.app.get_webview_window(&handle.0) {
            win.close()
                .map_err(|e| PortError::permanent(format!("关闭覆盖层失败：{e}")))?;
        }
        Ok(())
    }
}

/// 覆盖层窗口标签（含 selection_id；重启后不复用 ID 亦不复用标签）。
pub fn overlay_label(selection_id: &str) -> String {
    format!("selection-overlay-{selection_id}")
}

// ---------------------------------------------------------------------------
// 捕获：GDI → PNG → **引擎登记** → 引擎签发 ImageRef（DEF-TASK008-01 方向 A）
// ---------------------------------------------------------------------------

/// 主机侧框选图像缓存（image_id → PNG 字节）；**仅用于覆盖层本地预览**
/// （`selection_preview`），键为**引擎签发**的 image_id。会话结束由 `release` 释放。
#[derive(Default)]
pub struct HostImageStore {
    png: Mutex<HashMap<String, Vec<u8>>>,
}

impl HostImageStore {
    pub fn get(&self, image_id: &str) -> Option<Vec<u8>> {
        self.png.lock().unwrap_or_else(|p| p.into_inner()).get(image_id).cloned()
    }
}

/// GDI 捕获端口：干净桌面物理矩形 → PNG → **引擎 SessionImageStore** 登记，
/// 返回**引擎签发**的 `ImageRef`（`img-` 前缀，可被公开 `get_image_preview`/
/// `release_image` 解析）。
///
/// DEF-TASK008-01：早期版本在宿主进程内直接造 `sel-img-` 引用（只存本进程），
/// 破坏 03 公开契约。现改由宿主写受控临时区 + 一次性 token + 同步内部调用
/// `register_host_frame` 交引擎登记（见 [`super::hostframe`] 与
/// [`crate::ipc::hostcall`]）。
pub struct HostGdiCapture {
    store: Arc<HostImageStore>,
    data_dir: PathBuf,
}

impl HostGdiCapture {
    pub fn new(store: Arc<HostImageStore>, data_dir: PathBuf) -> Self {
        HostGdiCapture { store, data_dir }
    }

    /// 写受控临时区 + 签发一次性 token + 同步内部调用登记，返回引擎签发 ImageRef。
    fn register_with_engine(
        &self,
        png_bytes: &[u8],
        width: i64,
        height: i64,
        sha256: &str,
    ) -> Result<ImageRef, String> {
        let client = crate::ipc::hostcall::shared()
            .ok_or_else(|| "宿主帧通道未装配（引擎未启动）".to_string())?;
        let secret = hostframe::shared_secret()
            .ok_or_else(|| "host-frame 密钥未装配".to_string())?;
        let ttl = hostframe::shared_ttl_seconds();
        let filename = format!("{}.png", uuid::Uuid::new_v4().simple());
        hostframe::write_temp_frame(&self.data_dir, &filename, png_bytes)
            .map_err(|e| format!("写入受控临时区失败：{e}"))?;
        let nonce = uuid::Uuid::new_v4().simple().to_string();
        let token = hostframe::mint_host_frame_token(
            secret,
            &client.session_id(),
            &filename,
            sha256,
            width,
            height,
            hostframe::now_epoch_seconds(),
            ttl,
            &nonce,
        );
        match client.call("register_host_frame", serde_json::json!({ "token": token })) {
            Ok(data) => {
                let image_value = data
                    .get("image")
                    .cloned()
                    .ok_or_else(|| "register_host_frame 响应缺少 image".to_string())?;
                let image_ref: ImageRef = serde_json::from_value(image_value)
                    .map_err(|e| format!("解析引擎 ImageRef 失败：{e}"))?;
                Ok(image_ref)
            }
            Err(err) => {
                // 登记失败：宿主回收临时文件（引擎未消费）。
                hostframe::remove_temp_frame(&self.data_dir, &filename);
                Err(format!("{}: {}", err.code, err.message))
            }
        }
    }
}

impl DesktopCapturePort for HostGdiCapture {
    fn capture(&self, _display_id: &str, region: &Rect) -> PortResult<ImageRef> {
        if region.width <= 0 || region.height <= 0 {
            return Err(PortError::permanent("区域尺寸必须为正"));
        }
        let frame = gdi::capture_bgr(
            region.x as i32,
            region.y as i32,
            region.width as i32,
            region.height as i32,
        )
        .map_err(PortError::transient)?;
        let png_bytes = png::encode_bgr_png(frame.width, frame.height, &frame.bgr)
            .map_err(PortError::permanent)?;
        let sha256: String = Sha256::digest(&png_bytes)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        // 引擎登记（失败为永久错误：不再自动重试，避免重复登记/双写临时文件）。
        let engine_ref = self
            .register_with_engine(&png_bytes, frame.width as i64, frame.height as i64, &sha256)
            .map_err(PortError::permanent)?;
        // 本地缓存 PNG（键 = 引擎 image_id），供覆盖层 `selection_preview` 本地渲染，
        // 不改变既有 overlay 预览路径。
        self.store
            .png
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .insert(engine_ref.image_id.clone(), png_bytes);
        Ok(engine_ref)
    }

    fn release(&self, image_id: &str) -> PortResult<()> {
        // 仅释放宿主本地预览缓存；引擎侧引用释放由 `selection::release_engine_image`
        // 经同步内部调用通道完成（见 controller.finish_session）。
        self.store
            .png
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .remove(image_id);
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 脚本暂停（无活动脚本）：TASK-022 前恒 NoActiveScript（诚实降级）
// ---------------------------------------------------------------------------

/// 脚本控制端口：执行控制器（TASK-022）落地前无活动脚本管理 → 恒 `NoActiveScript`。
/// 接线后替换为真实"请求暂停并等待输入释放"。
pub struct NoopScriptControl;

impl ScriptControlPort for NoopScriptControl {
    fn request_pause(&self) -> PortResult<PauseOutcome> {
        Ok(PauseOutcome::NoActiveScript)
    }
}

// ---------------------------------------------------------------------------
// 时钟 / 睡眠
// ---------------------------------------------------------------------------

pub struct SystemClock;

impl ClockPort for SystemClock {
    fn now_rfc3339(&self) -> String {
        super::now_rfc3339_utc()
    }
}

pub struct ThreadSleeper;

impl Sleeper for ThreadSleeper {
    fn sleep_ms(&self, ms: u64) {
        std::thread::sleep(std::time::Duration::from_millis(ms));
    }
}

// ---------------------------------------------------------------------------
// 装配
// ---------------------------------------------------------------------------

/// 用生产端口构造共享框选管理器 + 图像缓存（lib.rs setup 调用）。
pub fn build_production_manager(
    app: AppHandle,
    events: Box<dyn super::model::EventSink>,
) -> (Arc<Mutex<SelectionManager>>, Arc<HostImageStore>) {
    let store = Arc::new(HostImageStore::default());
    // 受控临时区根（与 sidecar --data-dir 同源；host-frame 临时帧写在此处）。
    let data_dir = crate::sidecar::resolve_data_dir();
    let manager = SelectionManager::new(
        Box::new(TauriHostEnv::new(app)),
        Box::new(HostGdiCapture::new(Arc::clone(&store), data_dir)),
        Box::new(NoopScriptControl),
        events,
        Box::new(SystemClock),
        Box::new(ThreadSleeper),
    );
    (Arc::new(Mutex::new(manager)), store)
}

// ---------------------------------------------------------------------------
// 覆盖层回传 / 预览 Tauri 命令
// ---------------------------------------------------------------------------

/// 覆盖层报告拖选矩形 → 触发 `selection_updated`。参数经单一 JSON 字符串
/// `payload` 传入（避免 Tauri 命令多词参数大小写歧义），字段：
/// `{selection_id, ax, ay, bx, by, origin_x, origin_y, scale}`（局部 DIP 两对角点 +
/// 覆盖层物理原点/scale）。返回 `{emitted:boolean}`。**内部 UI↔Host 助手**。
#[tauri::command]
pub fn selection_report_region(payload: String) -> Result<serde_json::Value, crate::ipc::IpcError> {
    let v: serde_json::Value = serde_json::from_str(&payload)
        .map_err(|e| crate::ipc::IpcError::invalid_params(&format!("payload 解析失败：{e}")))?;
    let selection_id = v
        .get("selection_id")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| crate::ipc::IpcError::invalid_params("缺少 selection_id"))?
        .to_string();
    let f = |k: &str| -> Result<f64, crate::ipc::IpcError> {
        v.get(k)
            .and_then(serde_json::Value::as_f64)
            .ok_or_else(|| crate::ipc::IpcError::invalid_params(&format!("缺少数值字段 {k}")))
    };
    let i = |k: &str| -> Result<i64, crate::ipc::IpcError> {
        v.get(k)
            .and_then(serde_json::Value::as_i64)
            .ok_or_else(|| crate::ipc::IpcError::invalid_params(&format!("缺少整数字段 {k}")))
    };
    let (ax, ay, bx, by) = (f("ax")?, f("ay")?, f("bx")?, f("by")?);
    let (origin_x, origin_y, scale) = (i("origin_x")?, i("origin_y")?, f("scale")?);
    let manager = super::shared_manager()
        .ok_or_else(|| crate::ipc::IpcError::new("INVALID_STATE", "框选管理器未装配", false))?;
    let result = manager
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .report_region(&selection_id, (ax, ay), (bx, by), (origin_x, origin_y), scale);
    result
}

/// 框选图像预览（03：`{mime, base64}`）——供覆盖层/编辑暂存区显示 ROI（仅展示，
/// 不用于识别）。`payload` = `{image_id, max_edge?}`（单一 JSON 字符串）。
#[derive(Serialize)]
pub struct PreviewData {
    pub mime: String,
    pub base64: String,
}

#[tauri::command]
pub fn selection_preview(
    store: tauri::State<'_, Arc<HostImageStore>>,
    payload: String,
) -> Result<PreviewData, String> {
    let v: serde_json::Value =
        serde_json::from_str(&payload).map_err(|e| format!("payload 解析失败：{e}"))?;
    let image_id = v
        .get("image_id")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "缺少 image_id".to_string())?;
    let bytes = store.get(image_id).ok_or_else(|| "预览不存在或已过期".to_string())?;
    Ok(PreviewData {
        mime: "image/png".to_string(),
        base64: base64_encode(&bytes),
    })
}

/// 标准 base64 编码（预览传输用；不引入依赖）。
fn base64_encode(data: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(TABLE[((n >> 18) & 63) as usize] as char);
        out.push(TABLE[((n >> 12) & 63) as usize] as char);
        out.push(if chunk.len() > 1 {
            TABLE[((n >> 6) & 63) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[(n & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_known_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn overlay_label_contains_selection_id() {
        assert_eq!(overlay_label("sel-123"), "selection-overlay-sel-123");
    }
}
