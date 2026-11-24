//! 框选控制器的 GDI 捕获、图像缓存和计时端口。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use sha2::{Digest, Sha256};

use super::model::{
    ClockPort, DesktopCapturePort, EventSink, HostEnvironmentPort, PortError, PortResult,
    ScriptControlPort, Sleeper,
};
use super::{gdi, hostframe, png, SelectionManager};
use crate::types::region::{ImageRef, Rect};

/// 主机侧框选图像缓存（image_id → PNG 字节）；**仅用于覆盖层本地预览**，键为**引擎签发**的 image_id。会话结束由 `release` 释放。
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
        // 本地缓存 PNG（键 = 引擎 image_id），供窗口渲染预览。
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

/// 窗口管理与脚本暂停由调用端注入，捕获和引擎共享数据目录。
pub fn build_production_manager(
    environment: Box<dyn HostEnvironmentPort>,
    script: Box<dyn ScriptControlPort>,
    events: Box<dyn EventSink>,
) -> (Arc<Mutex<SelectionManager>>, Arc<HostImageStore>) {
    let store = Arc::new(HostImageStore::default());
    let capture = HostGdiCapture::new(Arc::clone(&store), crate::sidecar::resolve_data_dir());
    let manager = SelectionManager::new(
        environment,
        Box::new(capture),
        script,
        events,
        Box::new(SystemClock),
        Box::new(ThreadSleeper),
    );
    (Arc::new(Mutex::new(manager)), store)
}
