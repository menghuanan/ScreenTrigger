//! 框选状态机确定性单测（TASK-008）：生命周期、BUSY、10×10、DPI/反向拖动、
//! 事件顺序（终态在恢复之后）、超时/重试、ID 不复用、显示器拔除。
//!
//! 全部副作用用假端口注入；共享 `OrderLog` 记录「隐藏/恢复/开覆盖层/关覆盖层/
//! 事件」的**真实交错顺序**，据此断言 05/03「事件发送须在恢复动作完成之后」。

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use serde_json::Value;

use crate::ipc::IpcError;
use crate::types::region::{DisplayInfo, ImageRef, Rect, Region, RegionSpace};

use super::super::model::{
    ClockPort, DesktopCapturePort, EventSink, HostEnvironmentPort, OverlayHandle, PauseOutcome,
    PortError, PortResult, ScriptControlPort, Sleeper,
};
use super::SelectionController;

// ---------------------------------------------------------------------------
// 测试替身
// ---------------------------------------------------------------------------

type Log = Arc<Mutex<Vec<String>>>;

fn push(log: &Log, line: impl Into<String>) {
    log.lock().unwrap().push(line.into());
}

fn display(id: &str, x: i64, y: i64, w: i64, h: i64, primary: bool) -> DisplayInfo {
    DisplayInfo {
        id: id.to_string(),
        rect: Rect { x, y, width: w, height: h },
        dpi_scale: 1.0,
        primary,
    }
}

fn image(image_id: &str) -> ImageRef {
    ImageRef {
        image_id: image_id.to_string(),
        width: 10,
        height: 10,
        format: "png".to_string(),
        sha256: "a".repeat(64),
    }
}

struct FakeEnv {
    displays: Vec<DisplayInfo>,
    window_rect: Rect,
    visible: Arc<Mutex<bool>>,
    stuck_visible: bool,
    log: Log,
}

impl HostEnvironmentPort for FakeEnv {
    fn list_displays(&self) -> PortResult<Vec<DisplayInfo>> {
        Ok(self.displays.clone())
    }
    fn main_window_rect(&self) -> PortResult<Rect> {
        Ok(self.window_rect.clone())
    }
    fn is_main_window_visible(&self) -> bool {
        *self.visible.lock().unwrap()
    }
    fn set_main_window_visible(&self, visible: bool) -> PortResult<()> {
        push(&self.log, if visible { "restore_main_window" } else { "hide_main_window" });
        if !self.stuck_visible {
            *self.visible.lock().unwrap() = visible;
        }
        Ok(())
    }
    fn open_overlay(&self, display: &DisplayInfo, selection_id: &str) -> PortResult<OverlayHandle> {
        push(&self.log, format!("open_overlay:{}:{selection_id}", display.id));
        Ok(OverlayHandle(format!("ov-{selection_id}")))
    }
    fn close_overlay(&self, handle: &OverlayHandle) -> PortResult<()> {
        push(&self.log, format!("close_overlay:{}", handle.0));
        Ok(())
    }
}

#[derive(Clone, Copy)]
enum FailKind {
    Ok,
    Transient,
    Permanent,
}

struct FakeCapture {
    plan: Arc<Mutex<VecDeque<FailKind>>>,
    log: Log,
    released: Arc<Mutex<Vec<String>>>,
    seq: Arc<Mutex<u32>>,
}

impl DesktopCapturePort for FakeCapture {
    fn capture(&self, display_id: &str, region: &Rect) -> PortResult<ImageRef> {
        let kind = self.plan.lock().unwrap().pop_front().unwrap_or(FailKind::Ok);
        push(
            &self.log,
            format!("capture:{display_id}:{}x{}", region.width, region.height),
        );
        match kind {
            FailKind::Ok => {
                let mut s = self.seq.lock().unwrap();
                *s += 1;
                Ok(image(&format!("img-{s}")))
            }
            FailKind::Transient => Err(PortError::transient("瞬时捕获失败")),
            FailKind::Permanent => Err(PortError::permanent("永久捕获失败")),
        }
    }
    fn release(&self, image_id: &str) -> PortResult<()> {
        self.released.lock().unwrap().push(image_id.to_string());
        push(&self.log, format!("release:{image_id}"));
        Ok(())
    }
}

struct FakeScript {
    outcome: PauseOutcome,
}

impl ScriptControlPort for FakeScript {
    fn request_pause(&self) -> PortResult<PauseOutcome> {
        Ok(self.outcome)
    }
}

struct FakeEvents {
    events: Arc<Mutex<Vec<(String, Value)>>>,
    log: Log,
}

impl EventSink for FakeEvents {
    fn emit(&self, name: &str, payload: Value) {
        push(&self.log, format!("event:{name}"));
        self.events.lock().unwrap().push((name.to_string(), payload));
    }
}

struct FakeClock;

impl ClockPort for FakeClock {
    fn now_rfc3339(&self) -> String {
        "2026-10-11T00:00:00.000Z".to_string()
    }
}

struct FakeSleeper {
    total_ms: Arc<Mutex<u64>>,
}

impl Sleeper for FakeSleeper {
    fn sleep_ms(&self, ms: u64) {
        *self.total_ms.lock().unwrap() += ms;
    }
}

/// 装配控制器 + 句柄。
struct Harness {
    controller: SelectionController,
    log: Log,
    events: Arc<Mutex<Vec<(String, Value)>>>,
    visible: Arc<Mutex<bool>>,
    sleeper_ms: Arc<Mutex<u64>>,
}

fn harness(displays: Vec<DisplayInfo>, stuck_visible: bool) -> Harness {
    let log: Log = Arc::new(Mutex::new(Vec::new()));
    let visible = Arc::new(Mutex::new(true));
    let events = Arc::new(Mutex::new(Vec::new()));
    let sleeper_ms = Arc::new(Mutex::new(0));
    let controller = SelectionController::new(
        Box::new(FakeEnv {
            displays,
            window_rect: Rect { x: 0, y: 0, width: 800, height: 600 },
            visible: Arc::clone(&visible),
            stuck_visible,
            log: Arc::clone(&log),
        }),
        Box::new(FakeCapture {
            plan: Arc::new(Mutex::new(VecDeque::new())),
            log: Arc::clone(&log),
            released: Arc::new(Mutex::new(Vec::new())),
            seq: Arc::new(Mutex::new(0)),
        }),
        Box::new(FakeScript { outcome: PauseOutcome::NoActiveScript }),
        Box::new(FakeEvents { events: Arc::clone(&events), log: Arc::clone(&log) }),
        Box::new(FakeClock),
        Box::new(FakeSleeper { total_ms: Arc::clone(&sleeper_ms) }),
    );
    Harness { controller, log, events, visible, sleeper_ms }
}

impl Harness {
    fn event_names(&self) -> Vec<String> {
        self.events.lock().unwrap().iter().map(|(n, _)| n.clone()).collect()
    }
    fn last_payload(&self, name: &str) -> Value {
        self.events
            .lock()
            .unwrap()
            .iter()
            .rev()
            .find(|(n, _)| n == name)
            .map(|(_, p)| p.clone())
            .expect("事件存在")
    }
    fn index_of(&self, needle: &str) -> usize {
        self.log
            .lock()
            .unwrap()
            .iter()
            .position(|l| l == needle)
            .unwrap_or_else(|| panic!("日志缺少 {needle}：{:?}", self.log.lock().unwrap()))
    }
}

fn single_display() -> Vec<DisplayInfo> {
    vec![display("primary", 0, 0, 1920, 1080, true)]
}

fn region(x: i64, y: i64, w: i64, h: i64) -> Region {
    Region {
        space: RegionSpace::ScreenPhysical,
        x,
        y,
        width: w,
        height: h,
        display_id: Some("primary".to_string()),
        binding_id: None,
        image_id: None,
    }
}

fn code(err: &IpcError) -> &str {
    &err.code
}

// ---------------------------------------------------------------------------
// 生命周期与事件顺序
// ---------------------------------------------------------------------------

#[test]
fn start_then_confirm_emits_completed_after_restore() {
    let mut h = harness(single_display(), false);
    let started = h.controller.start(None).unwrap();
    let sid = started["selection_id"].as_str().unwrap().to_string();
    assert_eq!(h.event_names(), vec!["selection_started"]);

    let out = h.controller.confirm(&sid, &region(10, 10, 100, 100)).unwrap();
    assert_eq!(out["region"]["width"], 100);
    assert!(out["image"]["image_id"].is_string());
    assert_eq!(h.event_names(), vec!["selection_started", "selection_completed"]);

    // 顺序：completed 事件必须在恢复主窗口 + 关覆盖层之后
    let restore = h.index_of("restore_main_window");
    let close = h.index_of("close_overlay:ov-{sid}".replace("{sid}", &sid).as_str());
    let completed = h.index_of("event:selection_completed");
    assert!(restore < completed, "恢复必须先于 completed 事件");
    assert!(close < completed, "关覆盖层必须先于 completed 事件");
    // 主窗口最终可见
    assert!(*h.visible.lock().unwrap());
    assert!(!h.controller.has_active_session());
}

#[test]
fn start_then_stop_emits_cancelled_after_restore() {
    let mut h = harness(single_display(), false);
    let sid = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    let out = h.controller.stop(&sid).unwrap();
    assert_eq!(out["ok"], true);
    assert_eq!(h.event_names(), vec!["selection_started", "selection_cancelled"]);
    assert_eq!(h.last_payload("selection_cancelled")["reason"], "cancelled");
    let restore = h.index_of("restore_main_window");
    let cancelled = h.index_of("event:selection_cancelled");
    assert!(restore < cancelled, "恢复必须先于 cancelled 事件");
    assert!(*h.visible.lock().unwrap());
    assert!(!h.controller.has_active_session());
}

#[test]
fn duplicate_session_returns_busy() {
    let mut h = harness(single_display(), false);
    let _ = h.controller.start(None).unwrap();
    let err = h.controller.start(None).unwrap_err();
    assert_eq!(code(&err), "BUSY");
    assert!(err.retryable, "BUSY retryable=true（06）");
}

#[test]
fn stop_unknown_session_returns_invalid_state() {
    let mut h = harness(single_display(), false);
    let err = h.controller.stop("sel-none").unwrap_err();
    assert_eq!(code(&err), "INVALID_STATE");
}

#[test]
fn selection_ids_are_not_reused_across_sessions() {
    let mut h = harness(single_display(), false);
    let first = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    h.controller.stop(&first).unwrap();
    let second = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert_ne!(first, second);
}

#[test]
fn start_with_unknown_display_returns_display_not_found() {
    let mut h = harness(single_display(), false);
    let err = h.controller.start(Some("ghost")).unwrap_err();
    assert_eq!(code(&err), "DISPLAY_NOT_FOUND");
}

// ---------------------------------------------------------------------------
// 10×10 边界
// ---------------------------------------------------------------------------

#[test]
fn nine_by_ten_is_rejected_and_session_survives() {
    let mut h = harness(single_display(), false);
    let sid = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    let err = h.controller.confirm(&sid, &region(0, 0, 9, 10)).unwrap_err();
    assert_eq!(code(&err), "INVALID_REGION");
    assert!(h.controller.has_active_session(), "拒绝后会话仍在（可重拖）");
    assert_eq!(h.event_names(), vec!["selection_started"]);
}

#[test]
fn ten_by_ten_is_accepted() {
    let mut h = harness(single_display(), false);
    let sid = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    let out = h.controller.confirm(&sid, &region(0, 0, 10, 10)).unwrap();
    assert_eq!(out["region"]["height"], 10);
}

#[test]
fn out_of_bounds_region_rejected_without_clipping() {
    // 显示器 1920 宽，区域越界 1 像素
    let mut h = harness(single_display(), false);
    let sid = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    let err = h
        .controller
        .confirm(&sid, &region(1900, 0, 100, 100))
        .unwrap_err();
    assert_eq!(code(&err), "INVALID_REGION");
}

#[test]
fn space_mismatch_region_rejected() {
    let mut h = harness(single_display(), false);
    let sid = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    let mut r = region(0, 0, 50, 50);
    r.space = RegionSpace::ImagePixel;
    r.image_id = Some("img".to_string());
    let err = h.controller.confirm(&sid, &r).unwrap_err();
    assert_eq!(code(&err), "INVALID_REGION");
}

// ---------------------------------------------------------------------------
// 实时更新：反向拖动 / DPI / 越界
// ---------------------------------------------------------------------------

#[test]
fn update_region_reverse_drag_normalized_and_emits_updated() {
    let mut h = harness(single_display(), false);
    let sid = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    // 反向拖动：从 (200,150) 到 (50,40)，scale=1.0
    let out = h
        .controller
        .update_region(&sid, (200.0, 150.0), (50.0, 40.0), (0, 0), 1.0)
        .unwrap();
    assert_eq!(out["emitted"], true);
    let p = h.last_payload("selection_updated");
    assert_eq!(p["region"]["x"], 50);
    assert_eq!(p["region"]["y"], 40);
    assert_eq!(p["region"]["width"], 150);
    assert_eq!(p["region"]["height"], 110);
    assert!(p["preview"]["image_id"].is_string());
}

#[test]
fn update_region_converts_dip_with_overlay_origin_not_scaled() {
    let mut h = harness(vec![display("second", 1920, 0, 1920, 1080, false)], false);
    // 默认显示器选择：窗口 rect(0,0,800,600) 与 second 交叠面积 0 → 中心(400,300)
    // 不在 second → 无 primary → 取首（second）。故会话显示器为 second。
    let sid = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    // 覆盖层物理原点 = (1920,0)，scale=1.5；A=(10,10) B=(30,30)
    //   left=1920+floor(15)=1935；right=1920+ceil(45)=1965 → w=30
    let out = h
        .controller
        .update_region(&sid, (10.0, 10.0), (30.0, 30.0), (1920, 0), 1.5)
        .unwrap();
    assert_eq!(out["emitted"], true);
    let p = h.last_payload("selection_updated");
    assert_eq!(p["region"]["x"], 1935);
    assert_eq!(p["region"]["width"], 30);
    assert_eq!(p["region"]["display_id"], "second");
}

#[test]
fn update_region_below_minimum_is_not_emitted() {
    let mut h = harness(single_display(), false);
    let sid = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    let out = h
        .controller
        .update_region(&sid, (0.0, 0.0), (9.0, 9.0), (0, 0), 1.0)
        .unwrap();
    assert_eq!(out["emitted"], false);
    assert_eq!(h.event_names(), vec!["selection_started"]);
}

#[test]
fn update_region_out_of_bounds_is_not_emitted() {
    let mut h = harness(single_display(), false);
    let sid = h.controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    let out = h
        .controller
        .update_region(&sid, (1900.0, 0.0), (2000.0, 50.0), (0, 0), 1.0)
        .unwrap();
    assert_eq!(out["emitted"], false);
}

// ---------------------------------------------------------------------------
// 重试 / 超时 / 拔除
// ---------------------------------------------------------------------------

#[test]
fn hide_timeout_returns_error_and_restores() {
    // stuck_visible=true → 主窗口永不隐藏 → 5s 超时
    let mut h = harness(single_display(), true);
    let err = h.controller.start(None).unwrap_err();
    assert_eq!(code(&err), "INTERNAL_ERROR");
    assert_eq!(*h.sleeper_ms.lock().unwrap(), 5_000, "等待上限 5s");
    // finally 恢复（记录中包含 restore）
    assert!(h.index_of("restore_main_window") > 0);
    assert!(!h.controller.has_active_session());
}

#[test]
fn display_removed_before_confirm_ends_session() {
    let log: Log = Arc::new(Mutex::new(Vec::new()));
    let visible = Arc::new(Mutex::new(true));
    let events = Arc::new(Mutex::new(Vec::new()));
    let displays = Arc::new(Mutex::new(single_display()));
    let controller = SelectionController::new(
        Box::new(DynamicEnv {
            displays: Arc::clone(&displays),
            visible: Arc::clone(&visible),
            log: Arc::clone(&log),
        }),
        Box::new(FakeCapture {
            plan: Arc::new(Mutex::new(VecDeque::new())),
            log: Arc::clone(&log),
            released: Arc::new(Mutex::new(Vec::new())),
            seq: Arc::new(Mutex::new(0)),
        }),
        Box::new(FakeScript { outcome: PauseOutcome::NoActiveScript }),
        Box::new(FakeEvents { events: Arc::clone(&events), log: Arc::clone(&log) }),
        Box::new(FakeClock),
        Box::new(FakeSleeper { total_ms: Arc::new(Mutex::new(0)) }),
    );
    let mut controller = controller;
    let sid = controller.start(None).unwrap()["selection_id"]
        .as_str()
        .unwrap()
        .to_string();
    // 拔除显示器
    displays.lock().unwrap().clear();
    let err = controller.confirm(&sid, &region(0, 0, 50, 50)).unwrap_err();
    assert_eq!(code(&err), "DISPLAY_NOT_FOUND");
    let names: Vec<String> = events.lock().unwrap().iter().map(|(n, _)| n.clone()).collect();
    assert_eq!(names, vec!["selection_started", "selection_cancelled"]);
    assert_eq!(
        events.lock().unwrap().last().unwrap().1["reason"],
        "display_removed"
    );
    assert!(!controller.has_active_session());
    *displays.lock().unwrap() = single_display();
    let pick = controller.start_pick(None, None).unwrap()["pick_id"].as_str().unwrap().to_owned();
    displays.lock().unwrap()[0].dpi_scale = 1.5;
    let err = controller.confirm_pick(&pick, crate::types::region::Point { x: 300, y: 400 }).unwrap_err();
    assert_eq!(err.code, "DISPLAY_NOT_FOUND");
    assert_eq!(events.lock().unwrap().last().unwrap().0, "point_pick_cancelled");
    assert!(controller.cancel_pick(&pick, "cancelled").is_err());
    assert!(*visible.lock().unwrap());
}

/// 可动态清空显示器的环境（拔除场景）。
struct DynamicEnv {
    displays: Arc<Mutex<Vec<DisplayInfo>>>,
    visible: Arc<Mutex<bool>>,
    log: Log,
}

impl HostEnvironmentPort for DynamicEnv {
    fn list_displays(&self) -> PortResult<Vec<DisplayInfo>> {
        Ok(self.displays.lock().unwrap().clone())
    }
    fn main_window_rect(&self) -> PortResult<Rect> {
        Ok(Rect { x: 0, y: 0, width: 800, height: 600 })
    }
    fn is_main_window_visible(&self) -> bool {
        *self.visible.lock().unwrap()
    }
    fn set_main_window_visible(&self, visible: bool) -> PortResult<()> {
        push(&self.log, if visible { "restore_main_window" } else { "hide_main_window" });
        *self.visible.lock().unwrap() = visible;
        Ok(())
    }
    fn open_overlay(&self, display: &DisplayInfo, selection_id: &str) -> PortResult<OverlayHandle> {
        push(&self.log, format!("open_overlay:{}:{selection_id}", display.id));
        Ok(OverlayHandle(format!("ov-{selection_id}")))
    }
    fn close_overlay(&self, handle: &OverlayHandle) -> PortResult<()> {
        push(&self.log, format!("close_overlay:{}", handle.0));
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 独立装配（失败注入）：transient / permanent 捕获失败
// ---------------------------------------------------------------------------

struct Rig {
    controller: SelectionController,
    plan: Arc<Mutex<VecDeque<FailKind>>>,
    sleeper_ms: Arc<Mutex<u64>>,
    visible: Arc<Mutex<bool>>,
    events: Arc<Mutex<Vec<(String, Value)>>>,
    log: Log,
}

fn rig(displays: Vec<DisplayInfo>) -> Rig {
    let log: Log = Arc::new(Mutex::new(Vec::new()));
    let visible = Arc::new(Mutex::new(true));
    let events = Arc::new(Mutex::new(Vec::new()));
    let plan = Arc::new(Mutex::new(VecDeque::new()));
    let sleeper_ms = Arc::new(Mutex::new(0));
    let controller = SelectionController::new(
        Box::new(FakeEnv {
            displays,
            window_rect: Rect { x: 0, y: 0, width: 800, height: 600 },
            visible: Arc::clone(&visible),
            stuck_visible: false,
            log: Arc::clone(&log),
        }),
        Box::new(FakeCapture {
            plan: Arc::clone(&plan),
            log: Arc::clone(&log),
            released: Arc::new(Mutex::new(Vec::new())),
            seq: Arc::new(Mutex::new(0)),
        }),
        Box::new(FakeScript { outcome: PauseOutcome::NoActiveScript }),
        Box::new(FakeEvents { events: Arc::clone(&events), log: Arc::clone(&log) }),
        Box::new(FakeClock),
        Box::new(FakeSleeper { total_ms: Arc::clone(&sleeper_ms) }),
    );
    Rig { controller, plan, sleeper_ms, visible, events, log }
}

impl Rig {
    fn names(&self) -> Vec<String> {
        self.events.lock().unwrap().iter().map(|(n, _)| n.clone()).collect()
    }
}

#[test]
fn rig_transient_retry_then_success() {
    let mut r = rig(single_display());
    let sid = r.controller.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    r.plan.lock().unwrap().push_back(FailKind::Transient);
    let out = r.controller.confirm(&sid, &region(0, 0, 50, 50)).unwrap();
    assert!(out["image"]["image_id"].is_string());
    assert_eq!(*r.sleeper_ms.lock().unwrap(), 200);
    assert_eq!(r.names(), vec!["selection_started", "selection_completed"]);
}

#[test]
fn rig_permanent_failure_cancels_and_restores() {
    let mut r = rig(single_display());
    let sid = r.controller.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    r.plan.lock().unwrap().push_back(FailKind::Permanent);
    let err = r.controller.confirm(&sid, &region(0, 0, 50, 50)).unwrap_err();
    assert_eq!(err.code, "CAPTURE_FAILED");
    assert_eq!(r.names(), vec!["selection_started", "selection_cancelled"]);
    // 恢复在 cancelled 事件之前
    let l = r.log.lock().unwrap();
    let restore = l.iter().position(|x| x == "restore_main_window").unwrap();
    let cancel = l.iter().position(|x| x == "event:selection_cancelled").unwrap();
    assert!(restore < cancel);
    assert!(*r.visible.lock().unwrap());
}

#[test]
fn rig_transient_then_permanent_returns_capture_failed() {
    let mut r = rig(single_display());
    let sid = r.controller.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    r.plan.lock().unwrap().push_back(FailKind::Transient);
    r.plan.lock().unwrap().push_back(FailKind::Permanent);
    let err = r.controller.confirm(&sid, &region(0, 0, 50, 50)).unwrap_err();
    assert_eq!(err.code, "CAPTURE_FAILED");
    assert_eq!(*r.sleeper_ms.lock().unwrap(), 200);
    assert_eq!(r.names(), vec!["selection_started", "selection_cancelled"]);
}
#[test]
fn point_pick_single_pixel_negative_origin_and_stale_commands() {
    use crate::types::region::Point;
    let mut h = harness(vec![display("left", -300, 40, 201, 101, true)], false);
    let id = h.controller.start_pick(Some("left"), None).unwrap()["pick_id"].as_str().unwrap().to_owned();
    assert_eq!(h.controller.start(None).unwrap_err().code, "BUSY");
    assert_eq!(h.controller.stop(&id).unwrap_err().code, "INVALID_STATE");
    assert!(h.controller.cancel_pick("another-pick", "escape").is_err());
    let result = h.controller.confirm_pick(&id, Point {x: -100, y: 140}).unwrap();
    assert_eq!(result["point"], serde_json::json!({"x":-100, "y":140}));
    assert_eq!(result["space"], "screen_physical");
    assert_eq!(result["display_id"], "left");
    assert!(result.get("client_point").is_none());
    assert!(h.controller.confirm_pick(&id, Point {x: 0,y:0}).is_err());
    assert_eq!(h.event_names(), ["point_pick_started", "point_pick_completed"]);
    assert!(*h.visible.lock().unwrap());
    assert!(h.index_of("restore_main_window") < h.index_of("event:point_pick_completed"));
}

#[test]
fn point_pick_termination_and_bounds_are_single_use() {
    use crate::types::region::Point;
    let mut h = harness(single_display(), false);
    for reason in ["escape", "closed", "focus_lost", "pointer_lost", "display_changed", "session_ended"] {
        let id = h.controller.start_pick(None, None).unwrap()["pick_id"].as_str().unwrap().to_owned();
        if reason == "session_ended" { h.controller.end_session(); }
        else { h.controller.cancel_pick(&id, reason).unwrap(); }
        assert_eq!(h.last_payload("point_pick_cancelled")["reason"], reason);
        assert!(h.controller.cancel_pick(&id, reason).is_err());
        assert!(*h.visible.lock().unwrap());
    }
    let id = h.controller.start_pick(None, None).unwrap()["pick_id"].as_str().unwrap().to_owned();
    assert_eq!(h.controller.confirm_pick(&id, Point {x: 1920,y:0}).unwrap_err().code, "INVALID_PARAMS");
    assert_eq!(h.event_names().iter().filter(|n| *n=="point_pick_cancelled").count(), 7);
    assert!(!h.controller.has_active_session());
}
