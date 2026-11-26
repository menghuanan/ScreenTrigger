//! 框选状态机（TASK-008）：`start` / `confirm` / `stop` / `update_region` 生命周期。
//!
//! **顺序契约**（05 框选时序 / 03 事件目录）：
//! - `start`：BUSY（重复会话）→ 选默认显示器 → 请求脚本暂停 → 记录可见性并瞬间
//!   隐藏主窗口 → 等待隐藏完成（5s 超时）→ 打开覆盖层 → 发 `selection_started`
//!   （「主窗口已隐藏」）；任一失败 **finally 恢复主窗口**。
//! - `confirm`：校验区域（空间/显示器/10×10/完全落在显示器，越界拒绝不裁剪）→
//!   抓最终帧（瞬时失败重试一次 200ms）→ **先**释放临时图 + 关覆盖层 + 恢复主窗口
//!   → **再**发 `selection_completed`（事件在恢复之后）。
//! - `stop`：等价取消 → 释放临时图 + 关覆盖层 + 恢复 → 发 `selection_cancelled`。
//! - 显示器拔除/捕获异常：结束会话 + 恢复 + 发 `selection_cancelled(reason)`。
//!
//! 结束后不复用 selection_id（02）。全部副作用经端口注入 → 可确定性单测。

use std::collections::HashSet;

use serde_json::{Value, json};

use crate::ipc::IpcError;
use crate::types::region::{DisplayInfo, Point, Rect, Region, RegionSpace};

use super::display::{find_display, select_default_display};
use super::geometry::{
    GeometryError, contains_point, contains_rect, local_dip_rect_to_physical, meets_minimum,
};
use super::model::{
    CAPTURE_RETRY_DELAY_MS, ClockPort, DesktopCapturePort, EventSink, HostEnvironmentPort,
    HIDE_POLL_INTERVAL_MS, OverlayHandle, PREPARE_TIMEOUT_MS, PauseOutcome, PortError,
    ScriptControlPort, SelectionSession, Sleeper,
};

/// 活动会话内部态。
struct Active {
    session: SelectionSession,
    overlay: OverlayHandle,
    /// 覆盖层打开前记录的主窗口可见性（finally 恢复用）。
    was_main_window_visible: bool,
    /// 本次会话产生的临时预览引用（结束统一释放）。
    temp_images: Vec<String>,
    /// 是否暂停过脚本（记录，不自动恢复——F01）。
    paused_script: bool,
    pick: Option<PointPick>,
}

struct PointPick {
    topology: Vec<DisplayInfo>,
    binding: Option<String>,
}

/// 框选控制器（状态机 + 端口）。
pub struct SelectionController {
    env: Box<dyn HostEnvironmentPort>,
    capture: Box<dyn DesktopCapturePort>,
    script: Box<dyn ScriptControlPort>,
    events: Box<dyn EventSink>,
    clock: Box<dyn ClockPort>,
    sleeper: Box<dyn Sleeper>,
    active: Option<Active>,
    used_ids: HashSet<String>,
}

impl SelectionController {
    /// 装配控制器（全部副作用端口注入）。
    pub fn new(
        env: Box<dyn HostEnvironmentPort>,
        capture: Box<dyn DesktopCapturePort>,
        script: Box<dyn ScriptControlPort>,
        events: Box<dyn EventSink>,
        clock: Box<dyn ClockPort>,
        sleeper: Box<dyn Sleeper>,
    ) -> Self {
        SelectionController {
            env,
            capture,
            script,
            events,
            clock,
            sleeper,
            active: None,
            used_ids: HashSet::new(),
        }
    }

    /// 是否有活动会话。
    pub fn has_active_session(&self) -> bool {
        self.active.is_some()
    }

    // ------------------------------------------------------------------
    // 命令：start_selection_mode
    // ------------------------------------------------------------------

    /// 开始框选。`display_id=None` → 默认显示器（F01 顺序）。
    pub fn start(&mut self, display_id: Option<&str>) -> Result<Value, IpcError> {
        self.begin(display_id, false, None)
    }

    pub fn start_pick(&mut self, display_id: Option<&str>, binding: Option<&str>) -> Result<Value, IpcError> {
        if let Some(id) = binding {
            let windows = crate::window::shared().ok_or_else(|| invalid_state("窗口会话已结束"))?;
            windows.lock().unwrap_or_else(|e| e.into_inner()).bound(id)?;
        }
        self.begin(display_id, true, binding)
    }

    fn begin(&mut self, display_id: Option<&str>, picking: bool, binding: Option<&str>) -> Result<Value, IpcError> {
        if self.active.is_some() {
            // 06：重复会话 BUSY（retryable=true）
            return Err(IpcError::busy());
        }
        let displays = self.env.list_displays().map_err(port_to_error)?;
        if displays.is_empty() {
            return Err(display_not_found("未检测到可用显示器"));
        }
        let window = self.env.main_window_rect().map_err(port_to_error)?;
        let display = match display_id {
            Some(id) => find_display(&displays, id)
                .ok_or_else(|| display_not_found(&format!("显示器不存在：{id}")))?
                .clone(),
            None => select_default_display(&displays, &window)
                .ok_or_else(|| display_not_found("无法确定默认显示器"))?
                .clone(),
        };

        // 请求脚本暂停（无活动脚本 → 继续，不阻塞；F01）
        let paused_script = match self.script.request_pause() {
            Ok(PauseOutcome::Paused) => true,
            Ok(PauseOutcome::NoActiveScript) => false,
            Err(e) => return Err(port_to_error(e)),
        };

        // 记录可见性并瞬间隐藏主窗口，等待隐藏完成（5s 超时；失败 finally 恢复）
        let was_visible = self.env.is_main_window_visible();
        if let Err(err) = self.hide_and_wait() {
            let _ = self.env.set_main_window_visible(was_visible);
            return Err(err);
        }

        // 打开覆盖层（WS_EX_LAYERED + WDA_EXCLUDEFROMCAPTURE，见 wiring）
        let selection_id = if picking { format!("pick-{}", uuid::Uuid::new_v4()) } else { self.new_selection_id(&display.id) };
        let overlay = match if picking { self.env.open_point_overlay(&display, &selection_id) }
            else { self.env.open_overlay(&display, &selection_id) } {
            Ok(h) => h,
            Err(e) => {
                let _ = self.env.set_main_window_visible(was_visible);
                return Err(capture_failed(&e.message, e.transient));
            }
        };

        self.active = Some(Active {
            session: SelectionSession::new(
                selection_id.clone(),
                display.id.clone(),
                self.clock.now_rfc3339(),
            ),
            overlay,
            was_main_window_visible: was_visible,
            temp_images: Vec::new(),
            paused_script,
            pick: picking.then(|| PointPick { topology: displays, binding: binding.map(str::to_owned) }),
        });

        if picking {
            self.emit("point_pick_started", json!({"pick_id":selection_id,"display_id":display.id}));
            return Ok(json!({"pick_id":selection_id}));
        }
        // selection_started：主窗口已隐藏（03）
        self.emit(
            "selection_started",
            json!({ "selection_id": selection_id, "display_id": display.id }),
        );
        Ok(json!({ "selection_id": selection_id }))
    }

    // ------------------------------------------------------------------
    // 命令：confirm_selection
    // ------------------------------------------------------------------

    /// 确认选区：返回 `{image, region}`；事件 `selection_completed` 在恢复之后发出。
    pub fn confirm(&mut self, selection_id: &str, region: &Region) -> Result<Value, IpcError> {
        self.require_mode(selection_id, false)?;
        let session_display = self.active_display_id(selection_id)?;

        // 显示器拔除检测（F01：显示器拔除 → 结束会话并恢复）
        let displays = self.env.list_displays().map_err(port_to_error)?;
        let Some(display) = find_display(&displays, &session_display) else {
            self.finish_and_emit_cancelled("display_removed");
            return Err(display_not_found("选定显示器已拔除"));
        };
        let display_rect = display.rect.clone();

        // 区域校验（越界拒绝，不自动裁剪；空间/显示器不符 → INVALID_REGION）
        validate_confirm_region(region, &session_display, &display_rect)?;

        let rect = Rect {
            x: region.x,
            y: region.y,
            width: region.width,
            height: region.height,
        };

        match self.capture_with_retry(&session_display, &rect) {
            Ok(image) => {
                if let Some(id) = image["image_id"].as_str() {
                    // The final engine image belongs to the caller. Its host copy is no longer needed.
                    let _ = self.capture.release(id);
                }
                // 先恢复（关覆盖层 + 恢复主窗口 + 释放临时图），再发终态事件
                self.finish_session();
                self.emit(
                    "selection_completed",
                    json!({
                        "selection_id": selection_id,
                        "region": region,
                        "image": image,
                    }),
                );
                Ok(json!({ "image": image, "region": region }))
            }
            Err(err) => {
                // 捕获异常 → 结束会话 + 恢复 + selection_cancelled(reason)
                self.finish_session();
                self.emit(
                    "selection_cancelled",
                    json!({ "selection_id": selection_id, "reason": "capture_failed" }),
                );
                Err(err)
            }
        }
    }

    // ------------------------------------------------------------------
    // 命令：stop_selection_mode
    // ------------------------------------------------------------------

    /// 停止框选（等价取消，恢复窗口）。无匹配活动会话 → INVALID_STATE。
    pub fn stop(&mut self, selection_id: &str) -> Result<Value, IpcError> {
        self.require_mode(selection_id, false)?;
        self.finish_and_emit_cancelled("cancelled");
        Ok(json!({ "ok": true }))
    }

    // ------------------------------------------------------------------
    // 覆盖层回传：选区实时更新（内部 UI↔Host 助手，非 03 命令目录命令）
    // ------------------------------------------------------------------

    /// 覆盖层报告局部 DIP 矩形（两对角点）+ 覆盖层物理原点/scale；返回是否发出
    /// `selection_updated`。矩形 <10×10 或越界时**不**发事件（保持 Region 合规）。
    pub fn update_region(
        &mut self,
        selection_id: &str,
        a: (f64, f64),
        b: (f64, f64),
        origin: (i64, i64),
        scale: f64,
    ) -> Result<Value, IpcError> {
        self.require_mode(selection_id, false)?;
        let rect = local_dip_rect_to_physical(a, b, origin, scale).map_err(geometry_to_error)?;

        let display_id = self
            .active
            .as_ref()
            .map(|a| a.session.display_id.clone())
            .unwrap_or_default();
        let display = match find_display(&self.env.list_displays().map_err(port_to_error)?, &display_id)
        {
            Some(d) => d.clone(),
            None => {
                self.finish_and_emit_cancelled("display_removed");
                return Err(display_not_found("选定显示器已拔除"));
            }
        };

        // 仅当区域合法（≥10×10 且完全落在显示器内）才发预览事件
        if !meets_minimum(&rect) || !contains_rect(&display.rect, &rect) {
            return Ok(json!({ "emitted": false }));
        }
        let region = Region {
            space: RegionSpace::ScreenPhysical,
            x: rect.x,
            y: rect.y,
            width: rect.width,
            height: rect.height,
            display_id: Some(display_id.clone()),
            binding_id: None,
            image_id: None,
        };
        // 预览捕获失败不结束会话（下一帧重试）
        match self.capture.capture(&display_id, &rect) {
            Ok(preview) => {
                let pid = preview.image_id.clone();
                // DEF-TASK008-01：预览引用现由引擎签发，仅保留**最新**一帧——
                // 释放被替换的旧帧（宿主本地缓存 + 引擎 SessionImageStore），
                // 避免拖选期间引擎字节无界累积。
                let superseded: Vec<String> = self
                    .active
                    .as_mut()
                    .map(|a| std::mem::replace(&mut a.temp_images, vec![pid]))
                    .unwrap_or_default();
                for old in &superseded {
                    let _ = self.capture.release(old);
                    super::release_engine_image(old);
                }
                self.emit(
                    "selection_updated",
                    json!({ "selection_id": selection_id, "region": region, "preview": preview }),
                );
                Ok(json!({ "emitted": true, "preview": preview }))
            }
            Err(err) => Err(capture_failed(&err.message, err.transient)),
        }
    }

    // ------------------------------------------------------------------
    // 内部工具
    // ------------------------------------------------------------------

    /// 隐藏主窗口并等待完成（轮询，5s 超时；06/F01）。
    fn hide_and_wait(&self) -> Result<(), IpcError> {
        self.env.set_main_window_visible(false).map_err(port_to_error)?;
        let mut waited = 0u64;
        while self.env.is_main_window_visible() {
            if waited >= PREPARE_TIMEOUT_MS {
                return Err(IpcError::internal("主窗口隐藏准备超时（5s）"));
            }
            self.sleeper.sleep_ms(HIDE_POLL_INTERVAL_MS);
            waited += HIDE_POLL_INTERVAL_MS;
        }
        Ok(())
    }

    /// 抓最终帧，瞬时失败重试一次（间隔 200ms；06）。
    fn capture_with_retry(&self, display_id: &str, rect: &Rect) -> Result<Value, IpcError> {
        match self.capture.capture(display_id, rect) {
            Ok(image) => Ok(serde_json::to_value(&image).unwrap_or(Value::Null)),
            Err(e) if e.transient => {
                self.sleeper.sleep_ms(CAPTURE_RETRY_DELAY_MS);
                match self.capture.capture(display_id, rect) {
                    Ok(image) => Ok(serde_json::to_value(&image).unwrap_or(Value::Null)),
                    Err(e2) => Err(capture_failed(&e2.message, e2.transient)),
                }
            }
            Err(e) => Err(capture_failed(&e.message, e.transient)),
        }
    }

    /// 释放临时图 + 关覆盖层 + 恢复主窗口（幂等；终态事件之前调用）。
    fn require_mode(&self, id: &str, point: bool) -> Result<(), IpcError> {
        if self.active.as_ref().is_some_and(|a| a.session.selection_id == id && a.pick.is_some() == point) { Ok(()) }
        else { Err(invalid_state("没有匹配的活动选择")) }
    }

    pub fn cancel_pick(&mut self, id: &str, reason: &str) -> Result<Value, IpcError> {
        self.require_mode(id, true)?;
        self.finish_session();
        self.emit("point_pick_cancelled", json!({"pick_id":id,"reason":reason}));
        Ok(json!({"ok":true}))
    }

    pub fn end_session(&mut self) {
        if let Some(active) = self.active.as_ref() {
            let id = active.session.selection_id.clone();
            if active.pick.is_some() { let _ = self.cancel_pick(&id, "session_ended"); }
            else { let _ = self.stop(&id); }
        }
    }

    pub fn confirm_pick(&mut self, id: &str, point: Point) -> Result<Value, IpcError> {
        self.require_mode(id, true)?;
        let result = (|| {
            let active = self.active.as_ref().unwrap();
            let pick = active.pick.as_ref().unwrap();
            let displays = self.env.list_displays().map_err(port_to_error)?;
            if displays.len() != pick.topology.len() || !displays.iter().all(|d| pick.topology.contains(d)) {
                return Err(display_not_found("取点期间显示拓扑已变化"));
            }
            let display = find_display(&displays, &active.session.display_id)
                .ok_or_else(|| display_not_found("取点显示器已移除"))?;
            if i32::try_from(point.x).is_err() || i32::try_from(point.y).is_err() {
                return Err(IpcError::invalid_params("点超出系统物理坐标范围"));
            }
            if !contains_point(&display.rect, point.x, point.y) { return Err(IpcError::invalid_params("点超出取点显示器")); }
            let mut value = json!({"pick_id":id,"point":point,"space":"screen_physical","display_id":display.id});
            if let Some(binding) = pick.binding.as_ref() {
                let windows = crate::window::shared().ok_or_else(|| invalid_state("窗口会话已结束"))?;
                let bound = windows.lock().unwrap_or_else(|e| e.into_inner()).bound(binding)?;
                let rect = &bound.live.rect;
                if bound.live.minimized || !bound.live.visible || !contains_point(rect, point.x, point.y) {
                    return Err(IpcError::invalid_params("取点不在绑定窗口可见客户区内"));
                }
                value["client_point"] = json!({"x": point.x - rect.x, "y": point.y - rect.y});
                value["binding_id"] = json!(binding);
                value["geometry_revision"] = json!(bound.revision);
            }
            Ok(value)
        })();
        match result {
            Ok(payload) => {
                self.finish_session();
                self.emit("point_pick_completed", payload.clone());
                Ok(payload)
            }
            Err(err) => {
                self.cancel_pick(id, &err.code)?;
                Err(err)
            }
        }
    }

    fn finish_session(&mut self) {
        let Some(active) = self.active.take() else {
            return;
        };
        for id in &active.temp_images {
            // 宿主本地预览缓存 + 引擎 SessionImageStore 引用一并归还（幂等）。
            let _ = self.capture.release(id);
            super::release_engine_image(id);
        }
        let _ = self.env.close_overlay(&active.overlay);
        let _ = self
            .env
            .set_main_window_visible(active.was_main_window_visible);
        // paused_script 不自动恢复（F01：框选结束不会自动恢复刚暂停的输入脚本）
        let _ = active.paused_script;
    }

    /// 结束会话并发出 `selection_cancelled {reason}`（事件在恢复之后）。
    fn finish_and_emit_cancelled(&mut self, reason: &str) {
        let Some(active) = self.active.as_ref() else {
            return;
        };
        let selection_id = active.session.selection_id.clone();
        self.finish_session();
        self.emit(
            "selection_cancelled",
            json!({ "selection_id": selection_id, "reason": reason }),
        );
    }

    /// 活动会话的显示器 id（校验 selection_id 匹配）。
    fn active_display_id(&self, selection_id: &str) -> Result<String, IpcError> {
        let Some(active) = self.active.as_ref() else {
            return Err(invalid_state("没有活动框选会话"));
        };
        if active.session.selection_id != selection_id {
            return Err(invalid_state("selection_id 与活动会话不匹配"));
        }
        Ok(active.session.display_id.clone())
    }

    fn emit(&self, name: &str, payload: Value) {
        self.events.emit(name, payload);
    }

    /// 生成不复用的 selection_id（02：结束后不复用 ID）。
    fn new_selection_id(&mut self, _display_id: &str) -> String {
        loop {
            let id = format!("sel-{}", uuid::Uuid::new_v4());
            if self.used_ids.insert(id.clone()) {
                return id;
            }
        }
    }
}

/// 区域确认校验（INVALID_REGION：空间/显示器/10×10/越界）。
fn validate_confirm_region(
    region: &Region,
    session_display: &str,
    display_rect: &Rect,
) -> Result<(), IpcError> {
    if region.space != RegionSpace::ScreenPhysical {
        return Err(invalid_region("区域空间必须为 screen_physical"));
    }
    if region.display_id.as_deref() != Some(session_display) {
        return Err(invalid_region("区域必须属于本次会话的显示器"));
    }
    if region.binding_id.is_some() || region.image_id.is_some() {
        return Err(invalid_region("screen 空间不应携带 binding_id/image_id"));
    }
    let rect = Rect {
        x: region.x,
        y: region.y,
        width: region.width,
        height: region.height,
    };
    if !meets_minimum(&rect) {
        return Err(invalid_region("框选区域小于 10×10 物理像素"));
    }
    if !contains_rect(display_rect, &rect) {
        return Err(invalid_region("区域未完全落在选定显示器内"));
    }
    Ok(())
}

fn port_to_error(e: PortError) -> IpcError {
    IpcError::internal(&format!("宿主环境错误：{}", e.message))
}

fn geometry_to_error(e: GeometryError) -> IpcError {
    IpcError::invalid_params(&format!("坐标/缩放非法：{e:?}"))
}

fn invalid_region(message: &str) -> IpcError {
    IpcError::new("INVALID_REGION", message, false)
}

fn invalid_state(message: &str) -> IpcError {
    IpcError::new("INVALID_STATE", message, false)
}

fn display_not_found(message: &str) -> IpcError {
    IpcError::new("DISPLAY_NOT_FOUND", message, false)
}

/// 06：CAPTURE_FAILED 默认 retryable=true（调用方提示；框架不自动重跑）。
fn capture_failed(message: &str, _transient: bool) -> IpcError {
    IpcError::new("CAPTURE_FAILED", message, true)
}

#[cfg(test)]
mod tests;
