//! 统一事件总线（TASK-005 设计 §3.1 eventbus.rs / §4.4 / §6.4；用户契约④）。
//!
//! 职责：
//! - **UI seq 重分配**：Python event → 每 session 从 1 单调重分配（03：Python
//!   序号不冒充 UI seq）；seq 在**发送时刻**分配（可合并事件被覆盖的中间态
//!   不占号，可能跳号——03 的缺口恢复义务在 UI 侧 onGap）；
//! - **可合并事件槽**（03 事件目录标注"可合并"的 selection_updated /
//!   recognition_stats）：同 name 在合并窗口（50ms，内部实现参数不入契约）
//!   内到达 → 覆盖槽内最新值（积压时合并到最新），窗口期满由 flusher 线程
//!   补发最新一帧；状态变更/终态事件**永不合并、永不丢弃**、按序可靠发送；
//! - **事件名 = 协议 name**，payload = 重分配 seq 后的完整信封（§4.4）；
//! - system_ready 经本总线到达 UI（契约④：移除 TASK-004 的同名直转）——
//!   它是会话重建锚（§6.4：UI 收到后归零 last_seq、清空旧 pending 视图），
//!   本身作为普通事件参与 seq 分配（新会话首事件 seq=1）。
//!
//! 可测性：emit 回调与时钟经构造注入（桌面入口提供窗口事件适配
//! 闭包；契约测试 = 捕获向量 + 假时钟），flusher 线程为可选（测试可
//! `without_flusher` + 手动 `flush_due` 驱动，避免线程竞态）。

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::sidecar::protocol::ProtocolEnvelope;

/// 可合并事件（03 事件目录逐字标注"可合并"的两个；其余一律不合并）。
const MERGEABLE_EVENTS: [&str; 2] = ["selection_updated", "recognition_stats"];

/// 可合并事件窗口（内部实现参数，§13-4：不入契约）。
const MERGE_WINDOW: Duration = Duration::from_millis(50);

/// 事件出口回调（桌面入口 = 事件订阅端；测试 = 捕获）。参数（事件名, 完整信封）。
/// Arc 包装（+Sync）以便投递线程与 flusher 线程共享同一底层出口。
pub type EmitFn = Arc<dyn Fn(&str, &Value) + Send + Sync>;
/// 时钟回调（测试注入假时钟；Arc 同上）。
pub type ClockFn = Arc<dyn Fn() -> Instant + Send + Sync>;

/// 总线共享状态（flusher 线程与投递线程共享；单锁，临界区无 IO）。
struct BusState {
    /// 下一个 UI seq（每 session 从 1 单调；发送时刻分配）。
    next_seq: u64,
    /// 待发合并槽：name → 最新信封（序列化后）。
    merge_slots: HashMap<String, Value>,
    /// 各可合并事件的窗口起点（窗口内后续同名事件只覆盖槽）。
    windows: HashMap<String, Instant>,
}

/// 事件总线（契约④）。
pub struct EventBus {
    session_id: String,
    state: Arc<Mutex<BusState>>,
    emit: EmitFn,
    clock: ClockFn,
    /// flusher 线程停止位（Drop 时置位；0=运行）。
    stop_flusher: Arc<AtomicU64>,
}

impl EventBus {
    /// 构造并启动 flusher 线程（每 MERGE_WINDOW 扫描一次到期合并槽）。
    pub fn new(session_id: &str, emit: EmitFn, clock: ClockFn) -> Self {
        let bus = EventBus::without_flusher(session_id, emit, clock);
        bus.start_flusher();
        bus
    }

    /// 无 flusher 构造（契约测试手动驱动 `flush_due`）。
    pub fn without_flusher(session_id: &str, emit: EmitFn, clock: ClockFn) -> Self {
        EventBus {
            session_id: session_id.to_string(),
            state: Arc::new(Mutex::new(BusState {
                next_seq: 1,
                merge_slots: HashMap::new(),
                windows: HashMap::new(),
            })),
            emit,
            clock,
            stop_flusher: Arc::new(AtomicU64::new(0)),
        }
    }

    fn start_flusher(&self) {
        let state = Arc::clone(&self.state);
        let stop = Arc::clone(&self.stop_flusher);
        let emit = Arc::clone(&self.emit);
        let clock = Arc::clone(&self.clock);
        std::thread::spawn(move || loop {
            if stop.load(Ordering::Relaxed) != 0 {
                return;
            }
            std::thread::sleep(MERGE_WINDOW);
            flush_due_locked(&state, &emit, &clock);
        });
    }

    /// 事件投递入口（Router 经 `EventHandler::on_frame` 调用；仅 event 帧）。
    pub fn on_event(&mut self, envelope: &ProtocolEnvelope) {
        let Some(name) = envelope.name.as_deref() else {
            return; // 解析层保证 event 必有 name；防御性忽略
        };
        if MERGEABLE_EVENTS.contains(&name) {
            self.on_mergeable_event(name, envelope);
        } else {
            // 状态变更/终态：不合并、不丢弃，按序发送（seq 发送时刻分配）
            let seq = self.alloc_seq();
            let mut envelope = envelope.clone();
            envelope.seq = Some(seq);
            let value = serde_json::to_value(&envelope).unwrap_or(Value::Null);
            (self.emit)(name, &value);
        }
    }

    /// 可合并事件：窗口内覆盖槽（合并到最新），窗口外立即发送并开新窗口。
    fn on_mergeable_event(&mut self, name: &str, envelope: &ProtocolEnvelope) {
        let now = (self.clock)();
        {
            let mut state = self.state.lock().unwrap_or_else(|p| p.into_inner());
            if let Some(&start) = state.windows.get(name) {
                if now.duration_since(start) < MERGE_WINDOW {
                    // 窗口内：覆盖槽内最新值（积压时合并到最新，03）；不占 UI seq
                    let value = serde_json::to_value(envelope).unwrap_or(Value::Null);
                    state.merge_slots.insert(name.to_string(), value);
                    return;
                }
            }
            // 窗口外：开新窗口（发送在锁外进行——emit 回调不得持锁）
            state.windows.insert(name.to_string(), now);
        }
        let seq = self.alloc_seq();
        let mut envelope = envelope.clone();
        envelope.seq = Some(seq);
        let value = serde_json::to_value(&envelope).unwrap_or(Value::Null);
        (self.emit)(name, &value);
    }

    /// flusher / 测试驱动：补发窗口期满的合并槽（槽内最新值；seq 发送时刻分配）。
    pub fn flush_due(&mut self) {
        flush_due_locked(&self.state, &self.emit, &self.clock);
    }

    fn alloc_seq(&self) -> u64 {
        let mut state = self.state.lock().unwrap_or_else(|p| p.into_inner());
        let seq = state.next_seq;
        state.next_seq += 1;
        seq
    }

    /// 会话重建锚（§6.4：引擎重启 = 新会话）：seq 归零（下一事件 seq=1）、
    /// 清空合并槽与窗口（旧会话事件不得泄漏进新会话）。
    pub fn reset_session(&mut self, session_id: &str) {
        self.session_id = session_id.to_string();
        let mut state = self.state.lock().unwrap_or_else(|p| p.into_inner());
        state.next_seq = 1;
        state.merge_slots.clear();
        state.windows.clear();
    }

    /// 当前会话 id（诊断/测试用）。
    pub fn session_id(&self) -> &str {
        &self.session_id
    }
}

impl Drop for EventBus {
    fn drop(&mut self) {
        self.stop_flusher.store(1, Ordering::Relaxed);
    }
}

/// 窗口期满的合并槽补发（flusher 线程与 flush_due 共用）。
/// emit 回调在锁外执行（回调可能同步消费事件，不得持锁回调）。
fn flush_due_locked(state: &Mutex<BusState>, emit: &EmitFn, clock: &ClockFn) {
    let now = clock();
    // 第一阶段（持锁）：收集到期槽并摘除
    let due: Vec<(String, Value)> = {
        let mut state = state.lock().unwrap_or_else(|p| p.into_inner());
        let mut due = Vec::new();
        let expired: Vec<String> = state
            .windows
            .iter()
            .filter(|(_, start)| now.duration_since(**start) >= MERGE_WINDOW)
            .map(|(name, _)| name.clone())
            .collect();
        for name in expired {
            state.windows.remove(&name);
            if let Some(value) = state.merge_slots.remove(&name) {
                due.push((name, value));
            }
        }
        due
    };
    // 第二阶段（锁外）：分配 seq 并发送
    for (name, mut value) in due {
        let seq = {
            let mut state = state.lock().unwrap_or_else(|p| p.into_inner());
            let seq = state.next_seq;
            state.next_seq += 1;
            seq
        };
        if let Some(obj) = value.as_object_mut() {
            obj.insert("seq".to_string(), serde_json::json!(seq));
        }
        emit(&name, &value);
    }
}

// ---------------------------------------------------------------------------
// 单元测试
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    /// 捕获型 emit（测试断言事件名/seq/payload）。
    #[derive(Clone, Default)]
    struct Capture {
        events: Arc<Mutex<Vec<(String, Value)>>>,
    }

    impl Capture {
        fn emit_fn(&self) -> EmitFn {
            let events = Arc::clone(&self.events);
            Arc::new(move |name: &str, value: &Value| {
                events.lock().unwrap().push((name.to_string(), value.clone()));
            })
        }
        fn len(&self) -> usize {
            self.events.lock().unwrap().len()
        }
        fn get(&self, i: usize) -> (String, Value) {
            self.events.lock().unwrap()[i].clone()
        }
    }

    fn fake_clock() -> (ClockFn, Arc<Mutex<Instant>>) {
        let t = Arc::new(Mutex::new(Instant::now()));
        let t2 = Arc::clone(&t);
        (Arc::new(move || *t2.lock().unwrap()), t)
    }

    fn event_envelope(name: &str, py_seq: u64) -> ProtocolEnvelope {
        ProtocolEnvelope {
            protocol_version: 1,
            session_id: "session-bus".into(),
            msg_type: "event".into(),
            id: None,
            command: None,
            params: None,
            seq: Some(py_seq),
            name: Some(name.to_string()),
            timestamp: Some("2026-10-09T12:00:00.000Z".into()),
            payload: Some(serde_json::json!({ "py_seq": py_seq })),
            success: None,
            data: None,
            error: None,
        }
    }

    #[test]
    fn ui_seq_is_reassigned_monotonic_from_1() {
        let cap = Capture::default();
        let (clock, _t) = fake_clock();
        let mut bus = EventBus::without_flusher("session-bus", cap.emit_fn(), clock);
        // Python 侧 seq 从 7 开始（Python seq ≠ UI seq，03）
        bus.on_event(&event_envelope("script_state_changed", 7));
        bus.on_event(&event_envelope("script_state_changed", 8));
        bus.on_event(&event_envelope("engine_status", 9));
        assert_eq!(cap.len(), 3);
        let (n0, v0) = cap.get(0);
        assert_eq!(n0, "script_state_changed");
        assert_eq!(v0["seq"], 1, "UI seq 每 session 从 1 单调（Python seq 不冒充）");
        assert_eq!(cap.get(1).1["seq"], 2);
        assert_eq!(cap.get(2).1["seq"], 3);
        assert_eq!(cap.get(2).0, "engine_status");
    }

    #[test]
    fn mergeable_events_coalesce_within_window() {
        let cap = Capture::default();
        let (clock, t) = fake_clock();
        let mut bus = EventBus::without_flusher("session-bus", cap.emit_fn(), clock);
        // 第一帧：窗口外 → 立即发送
        bus.on_event(&event_envelope("selection_updated", 1));
        assert_eq!(cap.len(), 1);
        // 第二帧（窗口内 10ms）：覆盖槽，不发送、不占号
        *t.lock().unwrap() += Duration::from_millis(10);
        bus.on_event(&event_envelope("selection_updated", 2));
        assert_eq!(cap.len(), 1, "窗口内同名事件合并，不立即发送");
        // 第三帧（仍在窗口内）：覆盖为最新
        *t.lock().unwrap() += Duration::from_millis(10);
        bus.on_event(&event_envelope("selection_updated", 3));
        assert_eq!(cap.len(), 1);
        // 窗口期满：flush 补发最新一帧（合并到最新，非逐帧重放）
        *t.lock().unwrap() += Duration::from_millis(60);
        bus.flush_due();
        assert_eq!(cap.len(), 2);
        let (name, value) = cap.get(1);
        assert_eq!(name, "selection_updated");
        assert_eq!(value["payload"]["py_seq"], 3, "补发的是最新状态");
        assert_eq!(value["seq"], 2, "补发帧的 UI seq 在发送时刻分配（单调递增）");
        // 再次 flush：无到期槽，不重复发送
        bus.flush_due();
        assert_eq!(cap.len(), 2);
    }

    #[test]
    fn non_mergeable_events_never_coalesce() {
        let cap = Capture::default();
        let (clock, _t) = fake_clock();
        let mut bus = EventBus::without_flusher("session-bus", cap.emit_fn(), clock);
        // 状态变更类事件即使同 name 连发也不合并（永不丢弃）
        for i in 1..=4 {
            bus.on_event(&event_envelope("script_state_changed", i));
        }
        assert_eq!(cap.len(), 4, "非可合并事件逐帧发送");
    }

    #[test]
    fn reset_session_restarts_seq_and_clears_slots() {
        let cap = Capture::default();
        let (clock, t) = fake_clock();
        let mut bus = EventBus::without_flusher("session-bus", cap.emit_fn(), clock);
        bus.on_event(&event_envelope("script_state_changed", 1)); // seq=1，立即发送
        bus.on_event(&event_envelope("selection_updated", 2)); // 窗口外 → 立即发送
        assert_eq!(cap.len(), 2);
        // 窗口内再挂一个未发送的合并槽
        *t.lock().unwrap() += Duration::from_millis(10);
        bus.on_event(&event_envelope("selection_updated", 3));
        assert_eq!(cap.len(), 2);
        // 会话重建：seq 归零、槽清空（旧会话事件不得泄漏进新会话）
        bus.reset_session("session-bus2");
        *t.lock().unwrap() += Duration::from_millis(60);
        bus.flush_due();
        assert_eq!(cap.len(), 2, "重建后旧合并槽不得补发");
        bus.on_event(&event_envelope("script_state_changed", 9));
        let (_, value) = cap.get(2);
        assert_eq!(value["seq"], 1, "新会话 UI seq 重新从 1 开始");
    }
}
