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

use super::action;
use super::gate::GateTable;
use super::lease::LeaseTable;
use super::ledger::Ledger;
use super::model::{
    ActionAck, ClockPort, InputAction, InputContext, InputPort, KeyState, MouseButton, Point,
    PointerPort, PortError, RegionResolutionPort, MOVE_SLICE_MS,
};

/// 把端口错误映射为 06 `INPUT_FAILED`（retryable=false；不自动重试）。
fn input_failed(err: PortError) -> IpcError {
    IpcError::input_failed(&err.message)
}

/// 派发流水线：持有副作用端口与 `action_id` 去重表。
pub struct DispatchPipeline {
    input: Box<dyn InputPort>,
    pointer: Box<dyn PointerPort>,
    region: Box<dyn RegionResolutionPort>,
    clock: Box<dyn ClockPort>,
    /// 同 `action_id` 去重（命中 → 重放同 Ack，不重做副作用）。
    dedup: HashMap<String, ActionAck>,
}

impl DispatchPipeline {
    /// 装配（端口全部注入）。
    pub fn new(
        input: Box<dyn InputPort>,
        pointer: Box<dyn PointerPort>,
        region: Box<dyn RegionResolutionPort>,
        clock: Box<dyn ClockPort>,
    ) -> Self {
        DispatchPipeline { input, pointer, region, clock, dedup: HashMap::new() }
    }

    /// 输入端口只读引用（账本释放用）。
    pub fn input_port(&self) -> &dyn InputPort {
        self.input.as_ref()
    }

    /// 运行一次派发流水线。
    #[allow(clippy::too_many_arguments)]
    pub fn run(
        &mut self,
        gate: &GateTable,
        leases: &LeaseTable,
        ledger: &mut Ledger,
        execution_id: &str,
        lease_id: &str,
        action_id: &str,
        geometry_revision: i64,
        action: &InputAction,
    ) -> Result<ActionAck, IpcError> {
        // 1. 闸门优先（03/06：停止必须先关闸门；关闸后新输入立即被拒，计数不增）。
        if !gate.is_open(execution_id) {
            gate.note_rejected();
            log_outcome(execution_id, action_id, action, self.clock.now_ms(), "error:INVALID_STATE");
            return Err(IpcError::invalid_state("输入闸门已关闭，拒绝派发"));
        }

        // 2. 租约匹配（lease_id 必须属于该 execution_id）。
        let context: InputContext = match leases.get_by_execution(execution_id) {
            Some(lease) if lease.lease_id.as_str() == lease_id => lease.context.clone(),
            _ => {
                log_outcome(execution_id, action_id, action, self.clock.now_ms(), "error:INVALID_STATE");
                return Err(IpcError::invalid_state("输入租约不存在或与执行不匹配"));
            }
        };

        // 3. action_id 去重（命中 → 重放同 Ack，不重做副作用）。
        if let Some(ack) = self.dedup.get(action_id) {
            return Ok(ack.clone());
        }

        // 4. 区域/窗口/几何/前台身份复核（权威在 Rust）。
        let binding_id = context.binding_id.clone().unwrap_or_default();
        self.region.check_geometry(&binding_id, geometry_revision)?;
        let foreground = self.pointer.foreground_window();
        if !self.region.foreground_matches(&foreground) {
            log_outcome(execution_id, action_id, action, self.clock.now_ms(), "error:WINDOW_NOT_FOUND");
            return Err(IpcError::window_not_found("前台窗口身份与绑定不匹配"));
        }
        let points = action::validation_points(action);
        let check_points: Vec<Point> = if points.is_empty() {
            // 非坐标动作：使用当前指针位置同样做区域/身份检查（03）。
            vec![self.pointer.cursor_pos()]
        } else {
            points
        };
        for point in &check_points {
            self.region.check_point(point.x, point.y, &context).map_err(|err| {
                log_outcome(
                    execution_id,
                    action_id,
                    action,
                    self.clock.now_ms(),
                    &format!("error:{}", err.code),
                );
                err
            })?;
        }

        // 5. 串行 SendInput（调用方持全局输入互斥锁 → 单点派发）。
        if let Err(err) = self.execute(execution_id, action, ledger, gate) {
            log_outcome(execution_id, action_id, action, self.clock.now_ms(), &format!("error:{}", err.code));
            return Err(err);
        }

        // 6. 账本更新已在 execute 内完成；登记去重并回 Ack。
        let ack = ActionAck::dispatched(action_id);
        self.dedup.insert(action_id.to_string(), ack.clone());
        log_outcome(execution_id, action_id, action, self.clock.now_ms(), "dispatched");
        Ok(ack)
    }

    /// 执行动作原语（串行临界区内调用）。
    fn execute(
        &self,
        execution_id: &str,
        action: &InputAction,
        ledger: &mut Ledger,
        gate: &GateTable,
    ) -> Result<(), IpcError> {
        let input = self.input.as_ref();
        match action {
            InputAction::Move { x, y, duration_ms } => {
                self.glide_move(execution_id, *x, *y, *duration_ms, gate)
            }
            InputAction::Click { x, y, button, clicks } => {
                input.move_to(*x, *y).map_err(input_failed)?;
                for _ in 0..*clicks {
                    input.set_button(*button, true).map_err(input_failed)?;
                    input.set_button(*button, false).map_err(input_failed)?;
                }
                Ok(())
            }
            InputAction::Drag { from, to, duration_ms } => {
                input.move_to(from.x, from.y).map_err(input_failed)?;
                input.set_button(MouseButton::Left, true).map_err(input_failed)?;
                let glide = self.glide_move(execution_id, to.x, to.y, *duration_ms, gate);
                // finally 语义：无论移动是否中止，都抬起左键。
                let release = input.set_button(MouseButton::Left, false).map_err(input_failed);
                glide?;
                release
            }
            InputAction::Scroll { delta } => {
                if *delta != 0 {
                    input.wheel(*delta).map_err(input_failed)?;
                }
                Ok(())
            }
            InputAction::Key { key, state } => match state {
                KeyState::Tap => {
                    input.key_event(key, true).map_err(input_failed)?;
                    ledger.record_key_down(execution_id, key.clone());
                    if input.key_event(key, false).is_ok() {
                        ledger.record_key_up(execution_id, key);
                    }
                    Ok(())
                }
                KeyState::Down => {
                    input.key_event(key, true).map_err(input_failed)?;
                    ledger.record_key_down(execution_id, key.clone());
                    Ok(())
                }
                KeyState::Up => {
                    input.key_event(key, false).map_err(input_failed)?;
                    ledger.record_key_up(execution_id, key);
                    Ok(())
                }
            },
            InputAction::Text { text, interval_ms } => {
                self.type_text(execution_id, text, *interval_ms, gate)
            }
            InputAction::Hotkey { keys } => self.hotkey(execution_id, keys, ledger),
        }
    }

    /// 长移动切片（≤50ms/片，逐片查闸门；已派发不可撤回，不重试）。
    fn glide_move(
        &self,
        execution_id: &str,
        x: i64,
        y: i64,
        duration_ms: u32,
        gate: &GateTable,
    ) -> Result<(), IpcError> {
        let input = self.input.as_ref();
        if duration_ms == 0 {
            return input.move_to(x, y).map_err(input_failed);
        }
        let start = self.pointer.cursor_pos();
        let slices = duration_ms.div_ceil(MOVE_SLICE_MS).max(1);
        for i in 1..=slices {
            if !gate.is_open(execution_id) {
                // 闸门在移动中途关闭：停止后续切片（部分已派发不自动重试）。
                return Err(IpcError::invalid_state("移动中途闸门关闭，中止后续切片"));
            }
            let t = i as f64 / slices as f64;
            let nx = (start.x as f64 + (x - start.x) as f64 * t).round() as i64;
            let ny = (start.y as f64 + (y - start.y) as f64 * t).round() as i64;
            input.move_to(nx, ny).map_err(input_failed)?;
            if i < slices {
                std::thread::sleep(Duration::from_millis(MOVE_SLICE_MS as u64));
            }
        }
        Ok(())
    }

    /// Unicode 文本输入（按 interval 逐字符；逐字符查闸门）。
    fn type_text(
        &self,
        execution_id: &str,
        text: &str,
        interval_ms: u32,
        gate: &GateTable,
    ) -> Result<(), IpcError> {
        let input = self.input.as_ref();
        if interval_ms == 0 {
            return input.unicode_text(text).map_err(input_failed);
        }
        let mut buffer = [0u8; 4];
        for ch in text.chars() {
            if !gate.is_open(execution_id) {
                return Err(IpcError::invalid_state("文本输入中途闸门关闭，中止后续字符"));
            }
            input
                .unicode_text(ch.encode_utf8(&mut buffer))
                .map_err(input_failed)?;
            std::thread::sleep(Duration::from_millis(interval_ms as u64));
        }
        Ok(())
    }

    /// 组合键：依序按下、逆序释放（异常亦清理，07§5）。
    fn hotkey(
        &self,
        execution_id: &str,
        keys: &[super::model::Key],
        ledger: &mut Ledger,
    ) -> Result<(), IpcError> {
        let input = self.input.as_ref();
        for (index, key) in keys.iter().enumerate() {
            if let Err(err) = input.key_event(key, true) {
                // 已按下的键逆序释放后报错（不重试）。
                for pressed in keys[..index].iter().rev() {
                    let _ = input.key_event(pressed, false);
                    ledger.record_key_up(execution_id, pressed);
                }
                return Err(input_failed(err));
            }
            ledger.record_key_down(execution_id, key.clone());
        }
        for key in keys.iter().rev() {
            let _ = input.key_event(key, false);
            ledger.record_key_up(execution_id, key);
        }
        Ok(())
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
