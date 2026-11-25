//! 派发流水线（TASK-017 / M05、06§不变量、07§5）。
//!
//! **顺序（03/06）**：闸门 `is_open` → 租约匹配 → `action_id` 去重 → 区域/几何/前台
//! 复核 → **串行** SendInput → 账本更新 → `ActionAck`。任一步失败**整笔拒绝**，
//! 不做 clamp、不自动重试。
//!
//! **长移动/拖拽切片**（07§5）：`move`/`drag` 按 ≤[`MOVE_SLICE_MS`] 毫秒切片，每片前
//! 检查 `is_open(execution_id)`；闸门在途中关闭 → 停止后续切片（**已派发的 OS 事件
//! 不可撤回**，部分派发不自动重试）。`text` 同理逐字符检查闸门。
//!
//! **日志**（06§日志与诊断）：记录 `action_id`/执行 ID/时间与 `dispatched|error`；
//! **不记录输入文本与参数全文**（`text` 只记长度）。

use std::collections::HashMap;
use std::time::Duration;

use crate::ipc::IpcError;

use super::gate::GateTable;
use super::lease::LeaseTable;
use super::ledger::Ledger;
use super::model::{
    ActionAck, ClockPort, InputAction, InputContext, InputPort, Key, KeyState, MouseButton, Point,
    PointerPort, PortError, RegionResolutionPort, MOVE_SLICE_MS,
};
use std::cell::Cell;

/// 把端口错误映射为 06 `INPUT_FAILED`（retryable=false；不自动重试）。
fn input_failed(err: PortError) -> IpcError {
    IpcError::input_failed(&err.message)
}

/// 派发流水线：持有副作用端口与 `action_id` 去重表。
pub struct DispatchPipeline {
    input: std::sync::Arc<dyn InputPort>,
    pointer: Box<dyn PointerPort>,
    region: Box<dyn RegionResolutionPort>,
    clock: Box<dyn ClockPort>,
    /// 同 `action_id` 去重（命中 → 重放同 Ack，不重做副作用）。
    dedup: HashMap<String, HashMap<String, Result<ActionAck, IpcError>>>,
}

impl DispatchPipeline {
    /// 装配（端口全部注入）。
    pub fn new(
        input: Box<dyn InputPort>,
        pointer: Box<dyn PointerPort>,
        region: Box<dyn RegionResolutionPort>,
        clock: Box<dyn ClockPort>,
    ) -> Self {
        DispatchPipeline {
            input: input.into(),
            pointer,
            region,
            clock,
            dedup: HashMap::new(),
        }
    }

    /// 输入端口只读引用（账本释放用）。
    pub fn input_port(&self) -> &dyn InputPort {
        self.input.as_ref()
    }

    pub fn shared_input(&self) -> std::sync::Arc<dyn InputPort> {
        self.input.clone()
    }

    /// 运行一次派发流水线。
    #[allow(clippy::too_many_arguments)]
    pub fn run(
        &mut self,
        gate: &GateTable,
        leases: &LeaseTable,
        ledger: &std::sync::Mutex<Ledger>,
        execution_id: &str,
        lease_id: &str,
        action_id: &str,
        geometry_revision: i64,
        action: &InputAction,
    ) -> Result<ActionAck, IpcError> {
        // 1. 闸门优先（03/06：停止必须先关闸门；关闸后新输入立即被拒，计数不增）。
        if !gate.is_open(execution_id) {
            gate.note_rejected();
            log_outcome(
                execution_id,
                action_id,
                action,
                self.clock.now_ms(),
                "error:INVALID_STATE",
            );
            return Err(IpcError::invalid_state("输入闸门已关闭，拒绝派发"));
        }

        // 2. 租约匹配（lease_id 必须属于该 execution_id）。
        let context: InputContext = match leases.get_by_execution(execution_id) {
            Some(lease) if lease.lease_id.as_str() == lease_id => lease.context.clone(),
            _ => {
                log_outcome(
                    execution_id,
                    action_id,
                    action,
                    self.clock.now_ms(),
                    "error:INVALID_STATE",
                );
                return Err(IpcError::invalid_state("输入租约不存在或与执行不匹配"));
            }
        };

        if let Some(result) = self
            .dedup
            .get(lease_id)
            .and_then(|seen| seen.get(action_id))
        {
            return result.clone();
        }
        let action_context = CheckedAction {
            pipeline: self,
            gate,
            execution: execution_id,
            context: &context,
            revision: geometry_revision,
            submitted: Cell::new(false),
        };
        let result = action_context.execute(action, ledger);
        let submitted = action_context.submitted.get();
        let result = match result {
            Ok(()) => Ok(ActionAck::dispatched(action_id)),
            Err(err) => {
                ledger
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .release_owned(execution_id, self.input.as_ref());
                if submitted && err.code != "INVALID_STATE" {
                    Err(IpcError::input_failed(&err.message)
                        .with_details(serde_json::json!({"partially_dispatched":true})))
                } else {
                    Err(err)
                }
            }
        };
        self.dedup
            .entry(lease_id.to_owned())
            .or_default()
            .insert(action_id.to_owned(), result.clone());
        log_outcome(
            execution_id,
            action_id,
            action,
            self.clock.now_ms(),
            result
                .as_ref()
                .map(|_| "dispatched")
                .unwrap_or_else(|e| &e.code),
        );
        result
    }

    pub fn query_pointer(&self) -> Result<serde_json::Value, IpcError> {
        let p = self.pointer.cursor_pos()?;
        Ok(serde_json::json!({"x":p.x, "y":p.y}))
    }
    pub fn forget_lease(&mut self, lease: &str) {
        self.dedup.remove(lease);
    }
}

struct CheckedAction<'a> {
    pipeline: &'a DispatchPipeline,
    gate: &'a GateTable,
    execution: &'a str,
    context: &'a InputContext,
    revision: i64,
    submitted: Cell<bool>,
}

impl CheckedAction<'_> {
    fn current(&self) -> Result<Point, IpcError> {
        self.pipeline.pointer.cursor_pos()
    }

    fn geometry(&self) -> Result<(), IpcError> {
        let binding = self.context.binding_id.as_deref().unwrap_or("");
        self.pipeline
            .region
            .check_geometry(binding, self.revision)?;
        if !binding.is_empty()
            && !self
                .pipeline
                .region
                .foreground_matches(&self.pipeline.pointer.foreground_window())
        {
            return Err(IpcError::window_unavailable("绑定窗口未在前台"));
        }
        Ok(())
    }

    fn path(&self, start: Point, end: Point) -> Result<(), IpcError> {
        self.pipeline.region.check_path(start, end, self.context)
    }

    fn send(
        &self,
        dest: Option<Point>,
        op: impl FnOnce(&dyn InputPort) -> Result<(), PortError>,
    ) -> Result<(), IpcError> {
        // Check dynamic geometry immediately before each bounded OS operation. Gate close and the send
        // share one critical section. Waiting between slices is always outside that section.
        self.geometry()?;
        let current = self.current()?;
        self.path(current, dest.unwrap_or(current))?;
        self.gate.submit(self.execution, || {
            self.submitted.set(true); // SendInput failure can still indicate a partial dispatch.
            op(self.pipeline.input.as_ref()).map_err(input_failed)
        })
    }

    fn delay(&self, ms: u32) -> Result<(), IpcError> {
        let deadline = std::time::Instant::now() + Duration::from_millis(ms.into());
        while let Some(left) = deadline.checked_duration_since(std::time::Instant::now()) {
            if self.gate.paused(self.execution) {
                return Err(IpcError::window_unavailable("执行已暂停"));
            }
            if !self.gate.is_open(self.execution) {
                return Err(IpcError::invalid_state("输入闸门已关闭"));
            }
            std::thread::sleep(left.min(Duration::from_millis(MOVE_SLICE_MS.into())));
        }
        Ok(())
    }

    fn glide(&self, from: Point, to: Point, duration: u32) -> Result<(), IpcError> {
        let slices = duration.div_ceil(MOVE_SLICE_MS).max(1);
        for i in 1..=slices {
            self.delay(duration * i / slices - duration * (i - 1) / slices)?;
            let t = i as f64 / slices as f64;
            let p = Point {
                x: (from.x as f64 + (to.x as f64 - from.x as f64) * t).round() as i64,
                y: (from.y as f64 + (to.y as f64 - from.y as f64) * t).round() as i64,
            };
            self.send(Some(p), |port| port.move_to(p.x, p.y))?;
        }
        Ok(())
    }

    fn key_down(&self, key: &Key, ledger: &std::sync::Mutex<Ledger>) -> Result<(), IpcError> {
        let mut ledger = ledger.lock().unwrap_or_else(|e| e.into_inner());
        if ledger.owns_key(self.execution, key) {
            return Ok(());
        }
        if ledger.key_in_use(key) || self.pipeline.input.externally_held(key.vk) {
            return Err(IpcError::input_failed("按键已由其他执行或用户持有"));
        }
        self.send(None, |p| p.key_event(key, true))?;
        ledger.record_key_down(self.execution, key.clone());
        Ok(())
    }

    fn key_up(&self, key: &Key, ledger: &std::sync::Mutex<Ledger>) -> Result<(), IpcError> {
        let mut ledger = ledger.lock().unwrap_or_else(|e| e.into_inner());
        if ledger.owns_key(self.execution, key) {
            self.pipeline
                .input
                .key_event(key, false)
                .map_err(input_failed)?;
            ledger.record_key_up(self.execution, key);
        }
        Ok(())
    }

    fn button_free(&self, button: MouseButton, ledger: &Ledger) -> Result<(), IpcError> {
        let vk = match button {
            MouseButton::Left => 1,
            MouseButton::Right => 2,
            MouseButton::Middle => 4,
        };
        if ledger.button_in_use(button) || self.pipeline.input.externally_held(vk) {
            return Err(IpcError::input_failed("鼠标按钮已被持有"));
        }
        Ok(())
    }

    fn button_down(
        &self,
        button: MouseButton,
        ledger: &std::sync::Mutex<Ledger>,
    ) -> Result<(), IpcError> {
        let mut ledger = ledger.lock().unwrap_or_else(|e| e.into_inner());
        self.button_free(button, &ledger)?;
        self.send(None, |p| p.set_button(button, true))?;
        ledger.record_button_down(self.execution, button);
        Ok(())
    }

    fn button_up(
        &self,
        button: MouseButton,
        ledger: &std::sync::Mutex<Ledger>,
    ) -> Result<(), IpcError> {
        let mut ledger = ledger.lock().unwrap_or_else(|e| e.into_inner());
        if !ledger.held_buttons(self.execution).contains(&button) {
            return Ok(());
        }
        self.pipeline
            .input
            .set_button(button, false)
            .map_err(input_failed)?;
        ledger.record_button_up(self.execution, button);
        Ok(())
    }

    fn execute(
        &self,
        action: &InputAction,
        ledger: &std::sync::Mutex<Ledger>,
    ) -> Result<(), IpcError> {
        self.geometry()?;
        let origin = self.current()?;
        self.path(origin, origin)?;
        match action {
            InputAction::Move { x, y, duration_ms } => {
                let dest = Point { x: *x, y: *y };
                self.path(origin, dest)?;
                self.glide(origin, dest, *duration_ms)
            }
            InputAction::Click {
                x,
                y,
                button,
                clicks,
            } => {
                self.button_free(*button, &ledger.lock().unwrap_or_else(|e| e.into_inner()))?;
                self.send(Some(Point { x: *x, y: *y }), |p| p.move_to(*x, *y))?;
                for _ in 0..*clicks {
                    self.button_down(*button, ledger)?;
                    self.button_up(*button, ledger)?;
                }
                Ok(())
            }
            InputAction::Drag {
                from,
                to,
                duration_ms,
            } => {
                self.button_free(
                    MouseButton::Left,
                    &ledger.lock().unwrap_or_else(|e| e.into_inner()),
                )?;
                self.path(origin, *from)?;
                self.path(*from, *to)?;
                self.send(Some(*from), |p| p.move_to(from.x, from.y))?;
                self.button_down(MouseButton::Left, ledger)?;
                self.glide(*from, *to, *duration_ms)?;
                self.button_up(MouseButton::Left, ledger)
            }
            InputAction::Scroll { delta } => {
                if *delta != 0 {
                    self.send(None, |p| p.wheel(*delta))?;
                }
                Ok(())
            }
            InputAction::Key {
                key,
                state: KeyState::Down,
            } => self.key_down(key, ledger),
            InputAction::Key {
                key,
                state: KeyState::Up,
            } => self.key_up(key, ledger),
            InputAction::Key {
                key,
                state: KeyState::Tap,
            } => {
                if ledger
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .owns_key(self.execution, key)
                {
                    return Err(IpcError::invalid_state("按键已由当前执行持有"));
                }
                self.key_down(key, ledger)?;
                self.key_up(key, ledger)
            }
            InputAction::Hotkey { keys } => {
                let mut pressed = Vec::new();
                for key in keys {
                    let held = ledger.lock().unwrap_or_else(|e| e.into_inner());
                    if !held.owns_key(self.execution, key)
                        && (held.key_in_use(key) || self.pipeline.input.externally_held(key.vk))
                    {
                        return Err(IpcError::input_failed(
                            "组合键中含有已被其他执行或用户按住的键",
                        ));
                    }
                }
                for key in keys {
                    if !ledger
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .owns_key(self.execution, key)
                    {
                        self.key_down(key, ledger)?;
                        pressed.push(key);
                    }
                }
                for key in pressed.into_iter().rev() {
                    self.key_up(key, ledger)?;
                }
                Ok(())
            }
            InputAction::Text { text, interval_ms } => {
                let mut buffer = [0u8; 4];
                for (i, ch) in text.chars().enumerate() {
                    if i > 0 {
                        self.delay(*interval_ms)?;
                    }
                    self.send(None, |p| p.unicode_text(ch.encode_utf8(&mut buffer)))?;
                }
                Ok(())
            }
        }
    }
}

/// 结构化日志（不记录参数全文；`text` 只记长度）。
fn log_outcome(
    execution_id: &str,
    action_id: &str,
    action: &InputAction,
    now_ms: u64,
    outcome: &str,
) {
    match action {
        InputAction::Text { text, .. } => eprintln!(
            "[input] action_id={action_id} execution_id={execution_id} kind=text chars={} t={now_ms}ms {outcome}",
            text.chars().count()
        ),
        other => eprintln!(
            "[input] action_id={action_id} execution_id={execution_id} kind={} t={now_ms}ms {outcome}",
            other.kind_str()
        ),
    }
}
