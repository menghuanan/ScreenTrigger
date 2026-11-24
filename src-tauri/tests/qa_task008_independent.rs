//! TASK-008 独立验证（QA：software-qa-engineer-3 / 严过关）。
//!
//! 目的：**不采信** `src/selection/controller/tests.rs` 的结论，用独立硬编码的
//! 数据与独立时序日志重算以下断言：
//!
//! 1. 路由目录：以 docs/03 命令目录**手工抄录**的 62 条为基准（非读取源码数组），
//!    校验每条可路由、无重复、`HostHandled`/`ForwardPython`/`HostDenied` 归属，
//!    并确认总数 62、表外命令 Unknown、`selection_report_region`/`selection_preview`
//!    未混入公开目录；
//! 2. 事件时序：用独立注入端口 + 独立 `OrderLog` 断言 `selection_completed` /
//!    `selection_cancelled` **严格晚于**「关覆盖层 + 恢复主窗口」；
//! 3. DPI 边界：`scale=1.5 → 宽 17`、`origin` 不被二次乘 scale；
//! 4. 跨模块一致性：`confirm_selection` 产出的 image_id 由 Rust 侧生成
//!    （前缀 `sel-img-`），而 `get_image_preview`/`release_image` 路由为
//!    `ForwardPython`（数据源在 Python `SessionImageStore`）——断言此不一致存在，
//!    供报告定性为集成缺陷。

use std::collections::HashSet;
use std::sync::{Arc, Mutex};

use serde_json::Value;

use screen_trigger_lib::ipc::{route_command, Route};
use screen_trigger_lib::selection::controller::SelectionController;
use screen_trigger_lib::selection::geometry::local_dip_rect_to_physical;
use screen_trigger_lib::selection::model::{
    ClockPort, DesktopCapturePort, EventSink, HostEnvironmentPort, OverlayHandle, PauseOutcome,
    PortError, PortResult, ScriptControlPort, Sleeper,
};
use screen_trigger_lib::types::region::{DisplayInfo, ImageRef, Rect, Region, RegionSpace};

// ---------------------------------------------------------------------------
// 1. 路由目录（基准 = docs/03 命令目录，手工抄录，与源码数组解耦）
// ---------------------------------------------------------------------------

/// docs/03「系统、捕获与识别」+「目标、分组和脚本文件」+「运行与调试」+「窗口、输入和配置」
/// 表中的全部命令，共 62 条。
const CATALOG_62: [&str; 62] = [
    // 系统、捕获与识别
    "hello", "ping", "query_request", "cancel_request", "cancel_job", "get_job",
    "get_display_list", "start_selection_mode", "confirm_selection", "stop_selection_mode",
    "import_image", "capture_region", "capture_full_screen", "capture_window",
    "release_image", "get_image_preview", "recognize_text", "match_template", "match_contour",
    "detect_color", "evaluate_target_conditions", "test_recognition",
    // 目标、分组和脚本文件
    "create_target", "get_target", "list_targets", "update_target", "delete_target",
    "create_group", "list_groups", "update_group", "delete_group", "move_target_to_group",
    "list_scripts", "load_script", "save_script", "delete_script", "validate_script",
    // 运行与调试
    "execute_script", "start_monitor", "pause_script", "resume_script", "stop_script",
    "get_script_state", "query_execution", "get_execution_logs", "add_breakpoint",
    "remove_breakpoint", "list_breakpoints", "step_into", "step_over", "step_out",
    "continue_execution", "get_variables", "get_stack",
    // 窗口、输入和配置
    "list_windows", "get_window_info", "bind_window", "unbind_window", "get_bound_window",
    "get_config", "update_config", "reset_config",
];

#[test]
fn catalog_is_62_unique_and_every_entry_is_routable() {
    assert_eq!(CATALOG_62.len(), 62, "docs/03 命令目录应为 62 条");
    let unique: HashSet<&str> = CATALOG_62.iter().copied().collect();
    assert_eq!(unique.len(), 62, "目录内不得有重复命令");
    for cmd in CATALOG_62 {
        assert_ne!(route_command(cmd), Route::Unknown, "{cmd} 必须可路由（在目录内）");
    }
}

#[test]
fn route_partition_matches_docs_03_host_vs_python() {
    // docs/03：UI 发给宿主的 start_selection_mode 类 + 窗口族由 Rust；其余路由 Python。
    // 框选三命令：Rust 本地处理（主窗口恢复语义需宿主掌控）
    for cmd in ["start_selection_mode", "confirm_selection", "stop_selection_mode"] {
        assert_eq!(route_command(cmd), Route::HostHandled, "{cmd} 应为 HostHandled");
    }
    // 窗口族：TASK-018 未实现 → 宿主未提供能力（CAPABILITY_DENIED，不误导可用）
    for cmd in ["list_windows", "get_window_info", "bind_window", "unbind_window", "get_bound_window"] {
        assert_eq!(route_command(cmd), Route::HostDenied, "{cmd} 应为 HostDenied");
    }
    // TASK-007/009 已在 Python 实现的资源/捕获命令 → 转发 Python
    for cmd in [
        "import_image", "get_display_list", "capture_region", "capture_full_screen",
        "capture_window", "release_image", "get_image_preview",
    ] {
        assert_eq!(route_command(cmd), Route::ForwardPython, "{cmd} 应 ForwardPython");
    }
    // 框架 + 引擎业务全部转发 Python
    for cmd in [
        "hello", "ping", "query_request", "cancel_request", "cancel_job", "get_job",
        "get_config", "update_config", "reset_config", "recognize_text", "execute_script",
        "create_target", "delete_script", "get_stack",
    ] {
        assert_eq!(route_command(cmd), Route::ForwardPython, "{cmd} 应 ForwardPython");
    }
}

#[test]
fn internal_selection_commands_are_not_in_public_catalog() {
    // 03「其他字段禁止」/命令目录不含内部助手命令 → 不得进入公开路由。
    // DEF-TASK008-01 新增内部命令 register_host_frame 亦不得混入公开目录。
    for internal in [
        "selection_report_region",
        "selection_preview",
        "register_host_frame",
    ] {
        assert_eq!(route_command(internal), Route::Unknown, "{internal} 不得混入公开目录");
        assert!(!CATALOG_62.contains(&internal), "{internal} 不应出现在 62 条目录");
    }
}

#[test]
fn out_of_catalog_commands_are_unknown() {
    for bogus in ["", "make_coffee", "set_region_limit", "clear_region_limit", "hotkey"] {
        assert_eq!(route_command(bogus), Route::Unknown, "{bogus} 应 Unknown");
    }
}

// ---------------------------------------------------------------------------
// 2/3. 独立端口替身 + 时序日志（不依赖 src/selection/controller/tests.rs）
// ---------------------------------------------------------------------------

type OrderLog = Arc<Mutex<Vec<String>>>;

#[derive(Clone, Copy)]
enum Cap {
    Ok,
    Transient,
    Permanent,
}

struct Env {
    displays: Vec<DisplayInfo>,
    visible: Arc<Mutex<bool>>,
    stuck: bool,
    log: OrderLog,
}

impl HostEnvironmentPort for Env {
    fn list_displays(&self) -> PortResult<Vec<DisplayInfo>> {
        Ok(self.displays.clone())
    }
    fn main_window_rect(&self) -> PortResult<Rect> {
        Ok(Rect { x: 0, y: 0, width: 800, height: 600 })
    }
    fn is_main_window_visible(&self) -> bool {
        *self.visible.lock().unwrap()
    }
    fn set_main_window_visible(&self, visible: bool) -> PortResult<()> {
        self.log.lock().unwrap().push(
            if visible { "restore_main_window".to_string() } else { "hide_main_window".to_string() },
        );
        if !self.stuck {
            *self.visible.lock().unwrap() = visible;
        }
        Ok(())
    }
    fn open_overlay(&self, _d: &DisplayInfo, sid: &str) -> PortResult<OverlayHandle> {
        self.log.lock().unwrap().push(format!("open_overlay:{sid}"));
        Ok(OverlayHandle(format!("ov-{sid}")))
    }
    fn close_overlay(&self, h: &OverlayHandle) -> PortResult<()> {
        self.log.lock().unwrap().push(format!("close_overlay:{}", h.0));
        Ok(())
    }
}

struct CapPort {
    plan: Arc<Mutex<Vec<Cap>>>,
    log: OrderLog,
}

impl DesktopCapturePort for CapPort {
    fn capture(&self, _d: &str, r: &Rect) -> PortResult<ImageRef> {
        let kind = self.plan.lock().unwrap().pop().unwrap_or(Cap::Ok);
        self.log.lock().unwrap().push(format!("capture:{}x{}", r.width, r.height));
        match kind {
            Cap::Ok => Ok(ImageRef {
                // DEF-TASK008-01 修复后：框选最终帧由引擎 SessionImageStore 签发（img-）。
                image_id: "img-independent".to_string(),
                width: r.width,
                height: r.height,
                format: "png".to_string(),
                sha256: "0".repeat(64),
            }),
            Cap::Transient => Err(PortError::transient("transient")),
            Cap::Permanent => Err(PortError::permanent("permanent")),
        }
    }
    fn release(&self, id: &str) -> PortResult<()> {
        self.log.lock().unwrap().push(format!("release:{id}"));
        Ok(())
    }
}

struct Script;
impl ScriptControlPort for Script {
    fn request_pause(&self) -> PortResult<PauseOutcome> {
        Ok(PauseOutcome::NoActiveScript)
    }
}

struct Events {
    names: Arc<Mutex<Vec<String>>>,
    log: OrderLog,
}
impl EventSink for Events {
    fn emit(&self, name: &str, _p: Value) {
        self.log.lock().unwrap().push(format!("event:{name}"));
        self.names.lock().unwrap().push(name.to_string());
    }
}

struct Clk;
impl ClockPort for Clk {
    fn now_rfc3339(&self) -> String {
        "2026-10-11T00:00:00Z".to_string()
    }
}

struct Sleep {
    ms: Arc<Mutex<u64>>,
}
impl Sleeper for Sleep {
    fn sleep_ms(&self, ms: u64) {
        *self.ms.lock().unwrap() += ms;
    }
}

struct Rig {
    ctl: SelectionController,
    log: OrderLog,
    names: Arc<Mutex<Vec<String>>>,
    visible: Arc<Mutex<bool>>,
    slept: Arc<Mutex<u64>>,
    plan: Arc<Mutex<Vec<Cap>>>,
}

fn rig(stuck: bool) -> Rig {
    let log: OrderLog = Arc::new(Mutex::new(Vec::new()));
    let names = Arc::new(Mutex::new(Vec::new()));
    let visible = Arc::new(Mutex::new(true));
    let slept = Arc::new(Mutex::new(0));
    let plan = Arc::new(Mutex::new(Vec::new()));
    let ctl = SelectionController::new(
        Box::new(Env {
            displays: vec![DisplayInfo {
                id: "primary".to_string(),
                rect: Rect { x: 0, y: 0, width: 1920, height: 1080 },
                dpi_scale: 1.0,
                primary: true,
            }],
            visible: Arc::clone(&visible),
            stuck,
            log: Arc::clone(&log),
        }),
        Box::new(CapPort { plan: Arc::clone(&plan), log: Arc::clone(&log) }),
        Box::new(Script),
        Box::new(Events { names: Arc::clone(&names), log: Arc::clone(&log) }),
        Box::new(Clk),
        Box::new(Sleep { ms: Arc::clone(&slept) }),
    );
    Rig { ctl, log, names, visible, slept, plan }
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

fn idx(log: &OrderLog, needle: &str) -> usize {
    log.lock()
        .unwrap()
        .iter()
        .position(|l| l == needle)
        .unwrap_or_else(|| panic!("日志缺少 {needle}"))
}

#[test]
fn completed_event_strictly_after_close_and_restore() {
    let mut r = rig(false);
    let sid = r.ctl.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    r.ctl.confirm(&sid, &region(10, 10, 100, 100)).unwrap();
    assert_eq!(*r.names.lock().unwrap(), vec!["selection_started", "selection_completed"]);
    let close = idx(&r.log, &format!("close_overlay:ov-{sid}"));
    let restore = idx(&r.log, "restore_main_window");
    let ev = idx(&r.log, "event:selection_completed");
    assert!(close < ev, "关覆盖层须先于 completed");
    assert!(restore < ev, "恢复主窗口须先于 completed");
    assert!(*r.visible.lock().unwrap(), "结束后主窗口须可见");
}

#[test]
fn cancelled_event_strictly_after_close_and_restore() {
    let mut r = rig(false);
    let sid = r.ctl.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    r.ctl.stop(&sid).unwrap();
    assert_eq!(*r.names.lock().unwrap(), vec!["selection_started", "selection_cancelled"]);
    let close = idx(&r.log, &format!("close_overlay:ov-{sid}"));
    let restore = idx(&r.log, "restore_main_window");
    let ev = idx(&r.log, "event:selection_cancelled");
    assert!(close < ev && restore < ev, "关覆盖层/恢复须先于 cancelled");
    assert!(*r.visible.lock().unwrap());
}

#[test]
fn transient_retry_once_then_success_sleeps_200ms() {
    let mut r = rig(false);
    let sid = r.ctl.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    r.plan.lock().unwrap().push(Cap::Transient); // 第一次瞬时失败，第二次成功
    r.ctl.confirm(&sid, &region(0, 0, 50, 50)).unwrap();
    assert_eq!(*r.slept.lock().unwrap(), 200, "仅重试一次，等待 200ms");
    assert_eq!(*r.names.lock().unwrap(), vec!["selection_started", "selection_completed"]);
}

#[test]
fn permanent_failure_capture_failed_and_restore() {
    let mut r = rig(false);
    let sid = r.ctl.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    r.plan.lock().unwrap().push(Cap::Permanent);
    let e = r.ctl.confirm(&sid, &region(0, 0, 50, 50)).unwrap_err();
    assert_eq!(e.code, "CAPTURE_FAILED");
    assert_eq!(*r.names.lock().unwrap(), vec!["selection_started", "selection_cancelled"]);
    assert!(idx(&r.log, "restore_main_window") < idx(&r.log, "event:selection_cancelled"));
    assert!(*r.visible.lock().unwrap());
}

#[test]
fn hide_timeout_5s_then_restore() {
    let mut r = rig(true); // stuck visible → 永不隐藏
    let e = r.ctl.start(None).unwrap_err();
    assert_eq!(e.code, "INTERNAL_ERROR");
    assert_eq!(*r.slept.lock().unwrap(), 5_000);
    assert!(r.log.lock().unwrap().contains(&"restore_main_window".to_string()));
}

#[test]
fn nine_by_ten_rejected_session_survives_ten_by_ten_ok() {
    let mut r = rig(false);
    let sid = r.ctl.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    let e = r.ctl.confirm(&sid, &region(0, 0, 9, 10)).unwrap_err();
    assert_eq!(e.code, "INVALID_REGION");
    assert!(r.ctl.has_active_session(), "拒绝后会话仍在");
    r.ctl.confirm(&sid, &region(0, 0, 10, 10)).unwrap();
}

#[test]
fn out_of_bounds_rejected_no_clip_space_and_binding_rejected() {
    let mut r = rig(false);
    let sid = r.ctl.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    // 越界（1920 显示器，x+w=2000）
    assert_eq!(r.ctl.confirm(&sid, &region(1900, 0, 100, 50)).unwrap_err().code, "INVALID_REGION");
    // 空间不符
    let mut bad = region(0, 0, 50, 50);
    bad.space = RegionSpace::ImagePixel;
    bad.image_id = Some("img".to_string());
    assert_eq!(r.ctl.confirm(&sid, &bad).unwrap_err().code, "INVALID_REGION");
    // 携带 binding_id
    let mut bind = region(0, 0, 50, 50);
    bind.binding_id = Some("b".to_string());
    assert_eq!(r.ctl.confirm(&sid, &bind).unwrap_err().code, "INVALID_REGION");
}

#[test]
fn duplicate_start_busy_and_stopped_id_not_reusable() {
    let mut r = rig(false);
    let sid = r.ctl.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    assert_eq!(r.ctl.start(None).unwrap_err().code, "BUSY");
    r.ctl.stop(&sid).unwrap();
    // 已结束 id 不可再用
    assert_eq!(r.ctl.stop(&sid).unwrap_err().code, "INVALID_STATE");
    assert_eq!(r.ctl.confirm(&sid, &region(0, 0, 50, 50)).unwrap_err().code, "INVALID_STATE");
    // 新会话 id 不复用
    let sid2 = r.ctl.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    assert_ne!(sid, sid2);
}

// ---------------------------------------------------------------------------
// 3. DPI 边界（独立构造）
// ---------------------------------------------------------------------------

#[test]
fn dip_scale_1_5_gives_width_17_and_origin_not_scaled() {
    // 07 §1：left=floor(10.3*1.5)=15, right=ceil(20.7*1.5)=32 → w=17
    let r = local_dip_rect_to_physical((10.3, 10.3), (20.7, 20.7), (0, 0), 1.5).unwrap();
    assert_eq!((r.x, r.y, r.width, r.height), (15, 15, 17, 17));
    // 06 第 1 条：origin 不乘 scale
    let r2 = local_dip_rect_to_physical((10.0, 10.0), (30.0, 30.0), (1920, 0), 2.0).unwrap();
    assert_eq!((r2.x, r2.y, r2.width, r2.height), (1940, 20, 40, 40));
    assert!(r2.x < 2000, "origin 不得被二次乘 scale");
}

// ---------------------------------------------------------------------------
// 4. 跨模块一致性（ImageRef 归属）
// ---------------------------------------------------------------------------

#[test]
fn confirm_image_id_is_engine_owned_after_def_task008_01() {
    // DEF-TASK008-01 修复后：confirm_selection 的 image 由**引擎 SessionImageStore**
    // 签发（img- 前缀），与公开 get_image_preview/release_image（ForwardPython）同一数据源。
    let mut r = rig(false);
    let sid = r.ctl.start(None).unwrap()["selection_id"].as_str().unwrap().to_string();
    let out = r.ctl.confirm(&sid, &region(0, 0, 50, 50)).unwrap();
    let image_id = out["image"]["image_id"].as_str().unwrap();
    assert!(image_id.starts_with("img-"), "框选图应由引擎签发（img-），实际 {image_id}");
    assert!(
        !image_id.starts_with("sel-img-"),
        "旧宿主自造 sel-img- 引用（缺陷）不得复现"
    );
    // 公开图片命令仍路由 Python（= 引擎 SessionImageStore，即上者数据源）
    assert_eq!(route_command("get_image_preview"), Route::ForwardPython);
    assert_eq!(route_command("release_image"), Route::ForwardPython);
    // 内部登记命令不得进入公开目录
    assert_eq!(route_command("register_host_frame"), Route::Unknown);
}
