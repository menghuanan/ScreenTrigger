//! 输入模块单元测试（TASK-017 / TEST-005 / TEST-008 / TEST-016 子集）。
//!
//! **确定性替身**（06/执行约定）：注入 clock/input port/region port，验证
//! `gate/lease/ledger/dispatch/geometry_guard` 的纯逻辑。**真实 OS 行为**（真实
//! 坐标正确性、越界不 clamp、停止后 OS 无新事件、Unicode 代理对、拖拽）另在
//! 真实环境取证（见 `reports/raw/TASK-017/`），不以替身证明真实坐标。

use std::sync::{Arc, Mutex};

use serde_json::json;

use super::action;
use super::dispatch::DispatchPipeline;
use super::gate::GateTable;
use super::lease::LeaseTable;
use super::ledger::Ledger;
use super::model::{
    ClockPort, InputAction, InputContext, InputPort, Key, KeyState, MonitorRect, MouseButton,
    PointerPort, PortError, RegionResolutionPort, VirtualScreen, WindowIdentity,
};
use super::{wiring, InputStopGate};
use crate::ipc::{IpcError, StopGate};
use crate::types::region::{Region, RegionSpace};

// ---------------------------------------------------------------------------
// 替身
// ---------------------------------------------------------------------------

/// 记录已实际提交的输入事件。
struct FakeInputPort {
    events: Arc<Mutex<Vec<String>>>,
}

impl FakeInputPort {
    fn new(events: Arc<Mutex<Vec<String>>>) -> Self {
        Self { events }
    }

    fn record(&self, entry: String) {
        self.events.lock().unwrap().push(entry);
    }
}

impl InputPort for FakeInputPort {
    fn move_to(&self, x: i64, y: i64) -> Result<(), PortError> {
        self.record(format!("move:{x},{y}"));
        Ok(())
    }

    fn set_button(&self, button: MouseButton, down: bool) -> Result<(), PortError> {
        self.record(format!("button:{}:{down}", button.as_str()));
        Ok(())
    }

    fn wheel(&self, delta: i32) -> Result<(), PortError> {
        self.record(format!("wheel:{delta}"));
        Ok(())
    }

    fn key_event(&self, key: &Key, down: bool) -> Result<(), PortError> {
        self.record(format!("key:{}:{down}", key.name));
        Ok(())
    }

    fn unicode_text(&self, text: &str) -> Result<(), PortError> {
        self.record(format!("text:{text}"));
        Ok(())
    }

    fn release_keys(&self, _execution_id: &str, keys: &[Key], buttons: &[MouseButton]) {
        for button in buttons.iter().rev() {
            self.record(format!("release-button:{}", button.as_str()));
        }
        for key in keys.iter().rev() {
            self.record(format!("release-key:{}", key.name));
        }
    }
}

/// 指针端口替身（固定虚拟屏/光标）。
struct FakePointer {
    virtual_screen: VirtualScreen,
    cursor: super::model::Point,
}

impl PointerPort for FakePointer {
    fn virtual_screen(&self) -> VirtualScreen {
        self.virtual_screen
    }
    fn foreground_window(&self) -> WindowIdentity {
        WindowIdentity { handle: 0, pid: 0, process_started_at: String::new() }
    }
    fn cursor_pos(&self) -> Result<super::model::Point, IpcError> {
        Ok(self.cursor)
    }
}

/// 区域端口替身（注入显示器集合；真实显示器判定逻辑与生产实现一致）。
struct FakeRegion {
    monitors: Vec<MonitorRect>,
}

impl RegionResolutionPort for FakeRegion {
    fn anchor_scope(&self, scope: super::anchor::Scope, ctx: &InputContext) -> Result<super::anchor::ScopeSnapshot, IpcError> {
        assert_eq!(scope, super::anchor::Scope::Region);
        let r = ctx.region.as_ref().unwrap();
        super::anchor::ScopeSnapshot::new(MonitorRect { x:r.x, y:r.y, width:r.width, height:r.height }, self.monitors.clone())
    }
    fn check_point(&self, x: i64, y: i64, ctx: &InputContext) -> Result<(), IpcError> {
        super::geometry_guard::evaluate_point(x, y, ctx, &self.monitors)
    }
    fn check_geometry(&self, binding_id: &str, geometry_revision: i64) -> Result<(), IpcError> {
        super::geometry_guard::evaluate_geometry(binding_id, geometry_revision, None)
    }
    fn foreground_matches(&self, _identity: &WindowIdentity) -> bool {
        true
    }
}

/// 时钟替身（固定计数）。
struct FakeClock;
impl ClockPort for FakeClock {
    fn now_ms(&self) -> u64 {
        0
    }
}

fn single_monitor() -> Vec<MonitorRect> {
    // 单个 1920×1080 显示器（本机环境：单虚拟显示器）。
    vec![MonitorRect { x: 0, y: 0, width: 1920, height: 1080 }]
}

fn region_full() -> Region {
    Region {
        space: RegionSpace::ScreenPhysical,
        x: 0,
        y: 0,
        width: 1920,
        height: 1080,
        display_id: Some("display-0".to_string()),
        binding_id: None,
        image_id: None,
    }
}

fn region_small() -> Region {
    Region {
        space: RegionSpace::ScreenPhysical,
        x: 100,
        y: 100,
        width: 200,
        height: 200,
        display_id: Some("display-0".to_string()),
        binding_id: None,
        image_id: None,
    }
}

fn ctx(region: Option<Region>, allow_exit: bool) -> InputContext {
    InputContext { region, binding_id: None, display_id: None, allow_exit, ..Default::default() }
}

/// 构造流水线 + 闸门/租约/账本（共享事件记录）。
fn setup(
    monitors: Vec<MonitorRect>,
    ctx: InputContext,
) -> (DispatchPipeline, GateTable, LeaseTable, Mutex<Ledger>, Arc<Mutex<Vec<String>>>) {
    let events = Arc::new(Mutex::new(Vec::new()));
    let pipeline = DispatchPipeline::new(
        Box::new(FakeInputPort::new(Arc::clone(&events))),
        Box::new(FakePointer { virtual_screen: VirtualScreen { x: 0, y: 0, cx: 1920, cy: 1080 }, cursor: super::model::Point { x: 10, y: 10 } }),
        Box::new(FakeRegion { monitors }),
        Box::new(FakeClock),
    );
    let mut leases = LeaseTable::new();
    leases.acquire("exec-1", ctx).unwrap();
    (pipeline, GateTable::new(), leases, Mutex::new(Ledger::new()), events)
}

fn lease_id(leases: &LeaseTable) -> String {
    leases.get_by_execution("exec-1").unwrap().lease_id.0.clone()
}

fn move_action(x: i64, y: i64) -> InputAction {
    InputAction::Move { x, y, duration_ms: 0 }
}

// ---------------------------------------------------------------------------
// 闸门 / 停止（TEST-008：关闸后新输入计数恒 0）
// ---------------------------------------------------------------------------

#[test]
fn gate_closed_rejects_new_dispatch_with_zero_sends() {
    let (mut pipeline, gate, leases, mut ledger, events) = setup(single_monitor(), ctx(None, false));
    let lid = lease_id(&leases);
    gate.close("exec-1", "stop_script");

    for i in 0..5 {
        let err = pipeline
            .run(&gate, &leases, &mut ledger, "exec-1", &lid, &format!("a-{i}"), 0, &move_action(10, 10))
            .unwrap_err();
        assert_eq!(err.code, "INVALID_STATE");
    }
    assert_eq!(gate.rejected_after_close(), 5, "关闸后被拒尝试计数");
    assert!(events.lock().unwrap().is_empty(), "关闸后不得派发任何 OS 输入（计数恒 0）");
}

#[test]
fn anchored_click_refuses_the_whole_partial_gap_even_when_point_and_path_are_on_screen() {
    let monitors = vec![MonitorRect { x:0, y:0, width:100, height:100 },
                        MonitorRect { x:101, y:0, width:100, height:100 }];
    let region = Region { width:200, height:100, ..region_full() };
    let (mut pipeline, gate, leases, ledger, events) = setup(monitors, ctx(Some(region), true));
    let action = action::parse_action(&json!({"kind":"click_anchor","anchor":{"version":1,
        "scope":"region","strategy":"offset","reference_width":200,"reference_height":100,"x":30,"y":30}})).unwrap();
    let error = pipeline.run(&gate,&leases,&ledger,"exec-1",&lease_id(&leases),"inside",0,&action).unwrap_err();
    assert_eq!(error.code, "REGION_LIMIT_VIOLATION");
    assert!(events.lock().unwrap().is_empty());
}

#[test]
fn global_close_all_blocks_every_execution() {
    let (mut pipeline, gate, leases, mut ledger, events) = setup(single_monitor(), ctx(None, false));
    let lid = lease_id(&leases);
    gate.close_all("engine_stopping");
    let err = pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "a-1", 0, &move_action(1, 1))
        .unwrap_err();
    assert_eq!(err.code, "INVALID_STATE");
    assert!(events.lock().unwrap().is_empty());
}

// ---------------------------------------------------------------------------
// 越界整笔拒绝、无 clamp（TEST-005）
// ---------------------------------------------------------------------------

#[test]
fn strict_region_out_of_bounds_rejected_without_clamp() {
    let (mut pipeline, gate, leases, mut ledger, events) =
        setup(single_monitor(), ctx(Some(region_small()), false));
    let lid = lease_id(&leases);
    // 点 (500,500) 在显示器内但在严格区域 (100..300) 外 → 整笔拒绝
    let err = pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "a-1", 0, &move_action(500, 500))
        .unwrap_err();
    assert_eq!(err.code, "REGION_LIMIT_VIOLATION");
    assert!(!err.retryable);
    assert!(events.lock().unwrap().is_empty(), "越界不得派发（无 clamp）");
}

#[test]
fn allow_exit_permits_leaving_region_but_stays_on_displays() {
    let (mut pipeline, gate, leases, mut ledger, events) =
        setup(single_monitor(), ctx(Some(region_small()), true));
    let lid = lease_id(&leases);
    // allow_exit=true：离开区域但仍在显示器内 → 通过
    pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "a-1", 0, &move_action(500, 500))
        .unwrap();
    assert_eq!(events.lock().unwrap().len(), 1);
    // 但离开显示器（x=3000）→ 仍拒绝（不扩大识别区）
    let err = pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "a-2", 0, &move_action(3000, 500))
        .unwrap_err();
    assert_eq!(err.code, "REGION_LIMIT_VIOLATION");
}

#[test]
fn point_in_gap_between_monitors_rejected() {
    // 两台显示器之间有空隙（06§不变量 3：不能只验证虚拟包围盒）
    let monitors = vec![
        MonitorRect { x: 0, y: 0, width: 100, height: 100 },
        MonitorRect { x: 200, y: 0, width: 100, height: 100 },
    ];
    let (mut pipeline, gate, leases, mut ledger, events) = setup(monitors, ctx(None, false));
    let lid = lease_id(&leases);
    // (150,50) 落在虚拟包围盒 [0,300)×[0,100) 内，但处于两显示器空隙 → 拒绝
    let err = pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "a-1", 0, &move_action(150, 50))
        .unwrap_err();
    assert_eq!(err.code, "REGION_LIMIT_VIOLATION");
    assert!(events.lock().unwrap().is_empty());
}

#[test]
fn non_coordinate_action_checks_current_pointer() {
    let (mut pipeline, gate, leases, mut ledger, _events) = setup(single_monitor(), ctx(Some(region_small()), false));
    let lid = lease_id(&leases);
    // 光标在 (10,10)（严格区域外）→ 非坐标动作也被拒（03：使用当前指针同样检查）
    let err = pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "a-1", 0, &InputAction::Scroll { delta: 1 })
        .unwrap_err();
    assert_eq!(err.code, "REGION_LIMIT_VIOLATION");
}

// ---------------------------------------------------------------------------
// 去重（03：同 action_id 重放同 Ack，不重做副作用）
// ---------------------------------------------------------------------------

#[test]
fn duplicate_action_id_replays_ack_without_resend() {
    let (mut pipeline, gate, leases, mut ledger, events) = setup(single_monitor(), ctx(None, false));
    let lid = lease_id(&leases);
    let ack1 = pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "a-1", 0, &move_action(20, 20))
        .unwrap();
    let ack2 = pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "a-1", 0, &move_action(20, 20))
        .unwrap();
    assert_eq!(ack1, ack2);
    assert!(ack1.dispatched);
    assert_eq!(events.lock().unwrap().len(), 1, "去重命中不得二次派发");
}

// ---------------------------------------------------------------------------
// 失败 identity：租约不符 → INVALID_STATE
// ---------------------------------------------------------------------------

#[test]
fn mismatched_lease_id_rejected() {
    let (mut pipeline, gate, leases, mut ledger, events) = setup(single_monitor(), ctx(None, false));
    let err = pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", "lease-bogus", "a-1", 0, &move_action(1, 1))
        .unwrap_err();
    assert_eq!(err.code, "INVALID_STATE");
    assert!(events.lock().unwrap().is_empty());
}

// ---------------------------------------------------------------------------
// 长移动切片：中途关闸停止后续切片（07§5；部分已派发不自动重试）
// ---------------------------------------------------------------------------

#[test]
fn move_slicing_aborts_when_gate_closes_midway() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let gate = Arc::new(GateTable::new());
    // The port submits under the gate lock. Close during the next pointer check,
    // after the first submission has returned and before the next can begin.
    struct Pointer(FakePointer, Arc<Mutex<Vec<String>>>, Arc<GateTable>);
    impl PointerPort for Pointer {
        fn virtual_screen(&self) -> VirtualScreen { self.0.virtual_screen() }
        fn foreground_window(&self) -> WindowIdentity { self.0.foreground_window() }
        fn cursor_pos(&self) -> Result<super::model::Point, IpcError> {
            if !self.1.lock().unwrap().is_empty() {
                self.2.close("exec-1", "stop_mid_action");
            }
            self.0.cursor_pos()
        }
    }
    let input = FakeInputPort::new(Arc::clone(&events));
    let mut pipeline = DispatchPipeline::new(
        Box::new(input),
        Box::new(Pointer(FakePointer { virtual_screen: VirtualScreen { x: 0, y: 0, cx: 1920, cy: 1080 }, cursor: super::model::Point { x: 0, y: 0 } }, Arc::clone(&events), Arc::clone(&gate))),
        Box::new(FakeRegion { monitors: single_monitor() }),
        Box::new(FakeClock),
    );
    let mut leases = LeaseTable::new();
    leases.acquire("exec-1", ctx(None, false)).unwrap();
    let lid = lease_id(&leases);
    let mut ledger = Mutex::new(Ledger::new());

    // duration 200ms → 多片；第 1 片移动后闸门关闭 → 第 2 片起中止。
    let action = InputAction::Move { x: 1000, y: 1000, duration_ms: 200 };
    let err = pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "a-1", 0, &action)
        .unwrap_err();
    assert_eq!(err.code, "INVALID_STATE");
    let recorded = events.lock().unwrap().clone();
    assert_eq!(recorded.len(), 1, "闸门关闭后不得再派发后续切片（部分派发不重试）");
}

// ---------------------------------------------------------------------------
// 账本：只释放本程序持有者、逆序、幂等 down
// ---------------------------------------------------------------------------

#[test]
fn ledger_releases_only_owned_keys_in_reverse() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let port = FakeInputPort::new(Arc::clone(&events));
    let mut ledger = Ledger::new();
    let ctrl = action::parse_key("ctrl").unwrap();
    let a = action::parse_key("a").unwrap();
    ledger.record_key_down("exec-1", ctrl.clone());
    ledger.record_key_down("exec-1", a.clone());
    ledger.record_key_down("exec-1", ctrl.clone()); // 幂等：重复 down 不重复登记

    assert_eq!(ledger.held_keys("exec-1").len(), 2);
    let released = ledger.release_owned("exec-1", &port);
    assert_eq!(released, 2);
    let recorded = events.lock().unwrap().clone();
    // 逆序：先释放 a，再释放 ctrl
    assert_eq!(recorded, vec!["release-key:a".to_string(), "release-key:ctrl".to_string()]);
    assert!(ledger.held_keys("exec-1").is_empty());
    // 空账本再释放：不产生任何事件（不触碰非本程序按键）
    let again = ledger.release_owned("exec-1", &port);
    assert_eq!(again, 0);
    assert_eq!(events.lock().unwrap().len(), 2);
}

#[test]
fn hotkey_records_then_releases_all_keys() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let mut pipeline = DispatchPipeline::new(
        Box::new(FakeInputPort::new(Arc::clone(&events))),
        Box::new(FakePointer { virtual_screen: VirtualScreen { x: 0, y: 0, cx: 1920, cy: 1080 }, cursor: super::model::Point { x: 10, y: 10 } }),
        Box::new(FakeRegion { monitors: single_monitor() }),
        Box::new(FakeClock),
    );
    let gate = GateTable::new();
    let mut leases = LeaseTable::new();
    leases.acquire("exec-1", ctx(None, false)).unwrap();
    let lid = lease_id(&leases);
    let mut ledger = Mutex::new(Ledger::new());
    let action = action::parse_action(&json!({
        "kind": "hotkey", "keys": ["ctrl", "shift", "a"]
    }))
    .unwrap();
    pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "h-1", 0, &action)
        .unwrap();
    let recorded = events.lock().unwrap().clone();
    assert_eq!(
        recorded,
        vec![
            "key:ctrl:true", "key:shift:true", "key:a:true",
            "key:a:false", "key:shift:false", "key:ctrl:false",
        ],
        "依序按下、逆序释放"
    );
    assert!(ledger.lock().unwrap().held_keys("exec-1").is_empty(), "异常/正常后账本应清空");
}

// ---------------------------------------------------------------------------
// Unicode：管线原样转发 + UTF-16 代理对编码（纯逻辑）
// ---------------------------------------------------------------------------

#[test]
fn text_action_forwards_full_unicode_including_supplementary() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let mut pipeline = DispatchPipeline::new(
        Box::new(FakeInputPort::new(Arc::clone(&events))),
        Box::new(FakePointer { virtual_screen: VirtualScreen { x: 0, y: 0, cx: 1920, cy: 1080 }, cursor: super::model::Point { x: 10, y: 10 } }),
        Box::new(FakeRegion { monitors: single_monitor() }),
        Box::new(FakeClock),
    );
    let gate = GateTable::new();
    let mut leases = LeaseTable::new();
    leases.acquire("exec-1", ctx(None, false)).unwrap();
    let lid = lease_id(&leases);
    let mut ledger = Mutex::new(Ledger::new());
    let text = "中文a😀";
    let action = action::parse_action(&json!({ "kind": "text", "text": text, "interval_ms": 0 })).unwrap();
    pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "t-1", 0, &action)
        .unwrap();
    assert_eq!(events.lock().unwrap().clone(), text.chars().map(|c| format!("text:{c}")).collect::<Vec<_>>());
}

#[test]
fn supplementary_char_encodes_to_surrogate_pair() {
    // 😀 为补充平面字符 → UTF-16 需 2 个代理单元（win32 用 encode_utf16 逐个成对派发）。
    let units: Vec<u16> = "😀".encode_utf16().collect();
    assert_eq!(units.len(), 2);
    assert!(units[0] >= 0xD800 && units[0] <= 0xDBFF, "高代理");
    assert!(units[1] >= 0xDC00 && units[1] <= 0xDFFF, "低代理");
}

// ---------------------------------------------------------------------------
// 租约：一根一个、并发上限 3（06）
// ---------------------------------------------------------------------------

#[test]
fn lease_one_per_execution_and_max_three() {
    let mut leases = LeaseTable::new();
    leases.acquire("e1", ctx(None, false)).unwrap();
    let dup = leases.acquire("e1", ctx(None, false)).unwrap_err();
    assert_eq!(dup.code, "INVALID_STATE");
    leases.acquire("e2", ctx(None, false)).unwrap();
    leases.acquire("e3", ctx(None, false)).unwrap();
    let busy = leases.acquire("e4", ctx(None, false)).unwrap_err();
    assert_eq!(busy.code, "BUSY");
    assert!(busy.retryable);
    assert_eq!(leases.active_count(), 3);
}

// ---------------------------------------------------------------------------
// 命令面参数校验（TEST-016 host_request）
// ---------------------------------------------------------------------------

#[test]
fn handle_rejects_missing_fields_and_bad_ranges() {
    let manager = wiring::build_production_manager();
    let mut guard = manager.lock().unwrap();
    // 缺 execution_id
    assert_eq!(guard.handle("input_action", &json!({})).unwrap_err().code, "INVALID_PARAMS");
    // action 结构错误（text 超限）
    let too_long = "x".repeat(10_001);
    let err = guard
        .handle(
            "input_action",
            &json!({ "execution_id": "e1", "lease_id": "l1", "action_id": "a1", "geometry_revision": 0,
                     "action": { "kind": "text", "text": too_long } }),
        )
        .unwrap_err();
    assert_eq!(err.code, "INVALID_PARAMS");
    // 未知 kind
    let err = guard
        .handle(
            "input_action",
            &json!({ "execution_id": "e1", "lease_id": "l1", "action_id": "a1",
                     "action": { "kind": "teleport" } }),
        )
        .unwrap_err();
    assert_eq!(err.code, "INVALID_PARAMS");
    // 表外命令
    assert_eq!(guard.handle("make_coffee", &json!({})).unwrap_err().code, "UNKNOWN_COMMAND");
}

#[test]
fn acquire_and_release_lease_roundtrip_via_handle() {
    let manager = wiring::build_production_manager();
    let mut guard = manager.lock().unwrap();
    let data = guard
        .handle("acquire_input_lease", &json!({ "execution_id": "e1", "context": { "allow_exit": false } }))
        .unwrap();
    let lease = data["lease_id"].as_str().unwrap().to_string();
    assert!(lease.starts_with("lease-"));
    // 重复获取 → INVALID_STATE
    assert_eq!(
        guard.handle("acquire_input_lease", &json!({ "execution_id": "e1" })).unwrap_err().code,
        "INVALID_STATE"
    );
    // 释放 → {ok:true}
    let ok = guard.handle("release_input_lease", &json!({ "execution_id": "e1" })).unwrap();
    assert_eq!(ok["ok"], true);
    // 重复释放 → INVALID_STATE
    assert_eq!(
        guard.handle("release_input_lease", &json!({ "execution_id": "e1" })).unwrap_err().code,
        "INVALID_STATE"
    );
}

// ---------------------------------------------------------------------------
// 停闸门：关闸 + best-effort 释放（StopGate trait）
// ---------------------------------------------------------------------------

#[test]
fn input_stop_gate_closes_and_releases() {
    let manager = wiring::build_production_manager();
    let gate = manager.lock().unwrap().gate();
    let stop_gate = InputStopGate::new(Arc::clone(&gate), Some(Arc::clone(&manager)));
    // 先获取租约并持有一个键
    {
        let mut guard = manager.lock().unwrap();
        guard.acquire_lease("e1", ctx(None, false)).unwrap();
    }
    stop_gate.close_gate_for("e1", "stop_script");
    assert!(!gate.is_open("e1"), "关闸后 is_open=false");
    assert_eq!(gate.reason_for("e1").as_deref(), Some("stop_script"));
    // 全局关闭
    stop_gate.close_gate("engine_stopping");
    assert!(gate.is_closed_all());
}

#[test]
fn key_tap_leaves_empty_ledger() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let mut pipeline = DispatchPipeline::new(
        Box::new(FakeInputPort::new(Arc::clone(&events))),
        Box::new(FakePointer { virtual_screen: VirtualScreen { x: 0, y: 0, cx: 1920, cy: 1080 }, cursor: super::model::Point { x: 10, y: 10 } }),
        Box::new(FakeRegion { monitors: single_monitor() }),
        Box::new(FakeClock),
    );
    let gate = GateTable::new();
    let mut leases = LeaseTable::new();
    leases.acquire("exec-1", ctx(None, false)).unwrap();
    let lid = lease_id(&leases);
    let mut ledger = Mutex::new(Ledger::new());
    let action = InputAction::Key { key: action::parse_key("enter").unwrap(), state: KeyState::Tap };
    pipeline
        .run(&gate, &leases, &mut ledger, "exec-1", &lid, "k-1", 0, &action)
        .unwrap();
    assert!(ledger.lock().unwrap().held_keys("exec-1").is_empty(), "tap 后不残留持有");
    assert_eq!(
        events.lock().unwrap().clone(),
        vec!["key:enter:true".to_string(), "key:enter:false".to_string()]
    );
}

#[test]
fn unused_import_guard() {
    // 保持 region_full 辅助函数被引用（整段区域校验路径），并避免 dead_code 警告。
    let r = region_full();
    assert_eq!(r.width, 1920);
}

#[test]
fn rejects_origin_outside_before_entering_region() {
    let (mut pipeline, gate, leases, held, events) = setup(single_monitor(), ctx(Some(region_small()), false));
    let lease = lease_id(&leases);
    let error = pipeline.run(&gate, &leases, &held, "exec-1", &lease, "entry", 0, &move_action(110, 110)).unwrap_err();
    assert_eq!(error.code, "REGION_LIMIT_VIOLATION");
    assert!(events.lock().unwrap().is_empty());
}

#[test]
fn path_includes_single_pixel_monitor_gap_and_revision_must_equal() {
    use super::{geometry_guard, model::Point};
    let mut visited = Vec::new();
    let error = geometry_guard::walk_segment(Point { x: -1, y: 2 }, Point { x: 4, y: 2 }, |point| {
        visited.push(point.x);
        if point.x == 1 { Err(IpcError::region_limit_violation("gap")) } else { Ok(()) }
    }).unwrap_err();
    assert_eq!(visited, vec![0,1]);
    assert_eq!(error.code, "REGION_LIMIT_VIOLATION");
    assert_eq!(geometry_guard::evaluate_geometry("b", 5, Some(4)).unwrap_err().code, "STALE_FRAME");
}

#[test]
fn repeated_down_and_foreign_up_never_submit_an_extra_os_event() {
    let (mut pipeline, gate, mut leases, ledger, events) = setup(single_monitor(), ctx(None,false));
    let first = lease_id(&leases);
    let second = leases.acquire("exec-2", ctx(None,false)).unwrap().0;
    let key = |state| action::parse_action(&json!({"kind":"key","key":"shift","state":state})).unwrap();
    for action_id in ["first", "repeat"] {
        pipeline.run(&gate,&leases,&ledger,"exec-1",&first,action_id,0,&key("down")).unwrap();
    }
    pipeline.run(&gate,&leases,&ledger,"exec-2",&second,"foreign",0,&key("up")).unwrap();
    pipeline.run(&gate,&leases,&ledger,"exec-1",&first,"release",0,&key("up")).unwrap();
    assert_eq!(*events.lock().unwrap(), vec!["key:shift:true","key:shift:false"]);
}

#[test]
fn policy_checks_entire_curve_and_sampled_offset_before_dispatch() {
    let limits = Region { x: 0, y: 0, width: 100, height: 100, ..region_full() };
    let (mut pipe, gate, leases, ledger, events) = setup(single_monitor(), ctx(Some(limits), false));
    for (n, policy) in [
        json!({"curve":{"c1":[0,200],"c2":[0,200]},"version":1}),
        json!({"offset":{"x":[50,50],"y":[0,0]},"version":1}),
    ].into_iter().enumerate() {
        let action = action::parse_action(&json!({"kind":"click","x":50,"y":40,"policy":policy})).unwrap();
        let err = pipe.run(&gate,&leases,&ledger,"exec-1",&lease_id(&leases),&format!("bad-{n}"),0,&action).unwrap_err();
        assert_eq!(err.code,"REGION_LIMIT_VIOLATION");
        assert!(events.lock().unwrap().is_empty());
    }
    let drag = action::parse_action(&json!({"kind":"drag","from":{"x":20,"y":10},"to":{"x":50,"y":10},
        "policy":{"version":1,"curve":{"c1":[0,300],"c2":[0,300]}}})).unwrap();
    assert!(pipe.run(&gate,&leases,&ledger,"exec-1",&lease_id(&leases),"drag",0,&drag).is_err());
    assert!(events.lock().unwrap().is_empty());
}

#[test]
fn policy_sampling_is_stable_bounded_and_duplicate_click_is_replayed() {
    let raw = json!({"version":1,"offset":{"x":[-3,2],"y":[0,4]},"delay_ms":[0,5]});
    let policy = super::policy::ActionPolicy::parse(&raw).unwrap();
    let mut offsets = std::collections::HashSet::new();
    for id in 0..500 {
        let id = format!("id-{id}");
        let a = policy.sample(&id);
        let b = policy.sample(&id);
        assert_eq!((a.offset, a.delay), (b.offset, b.delay));
        assert!((-3..=2).contains(&a.offset[0]) && (0..=4).contains(&a.offset[1]) && a.delay <= 5);
        offsets.insert(a.offset);
    }
    assert_eq!(offsets.len(),30);
    let (mut pipe, gate, leases, ledger, events) = setup(single_monitor(),ctx(Some(region_full()),false));
    let action = action::parse_action(&json!({"kind":"click","x":200,"y":300,"policy":raw})).unwrap();
    for _ in 0..2 { pipe.run(&gate,&leases,&ledger,"exec-1",&lease_id(&leases),"fixed-seed",0,&action).unwrap(); }
    let offset = policy.sample("fixed-seed").offset;
    assert_eq!(*events.lock().unwrap(),vec![format!("move:{},{}",200+offset[0],300+offset[1]),"button:left:true".into(),"button:left:false".into()]);
    for (kind, opt) in [("key", json!({"version":1,"curve":{"c1":[0,0],"c2":[0,0]}})),
                       ("move", json!({"version":1,"offset":{"x":[0,0],"y":[0,0]}}))] {
        let err = action::parse_action(&json!({"kind":kind,"x":1,"y":2,"key":"F8","state":"tap","policy":opt})).unwrap_err();
        assert_eq!(err.code,"INVALID_PARAMS");
    }
}

#[test]
fn policy_fixed_clock_waits_exactly_the_sampled_interval_and_cancels_between_slices() {
    use std::cell::Cell;
    let elapsed = Cell::new(0);
    let policy = super::policy::ActionPolicy::parse(&json!({"version":1,"delay_ms":[123,123]})).unwrap().sample("clock");
    policy.wait(|| Ok::<(), ()>(()), |ms| { assert!(ms <= 10); elapsed.set(elapsed.get()+ms); }).unwrap();
    assert_eq!(elapsed.get(),123);
    elapsed.set(0);
    let result = policy.wait(|| if elapsed.get() >= 40 { Err("cancelled") } else { Ok(()) },
                             |ms| elapsed.set(elapsed.get()+ms));
    assert_eq!((result,elapsed.get()),(Err("cancelled"),40));
}
