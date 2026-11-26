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
    pub(super) background: bool,
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
            background: false,
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

        if context.background != self.background {
            return Err(IpcError::capability_denied("输入端口与租约的窗口模式不符"));
        }
        if let Some(result) = self
            .dedup
            .get(lease_id)
            .and_then(|seen| seen.get(action_id))
        {
            return result.clone();
        }
        let (action, policy) = match action {
            InputAction::WithPolicy { action, policy } => (action.as_ref(), policy.sample(action_id)),
            bare => (bare, Default::default()),
        };
        let action_context = CheckedAction {
            pipeline: self,
            gate,
            execution: execution_id,
            context: &context,
            revision: geometry_revision,
            submitted: Cell::new(false),
            anchor: Default::default(),
            policy,
        };
        let result = self.input.authorize(action).and_then(|_| action_context.execute(action, ledger));
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
                        .with_details(if context.background {
                            serde_json::json!({"partially_dispatched":true,"delivery_unknown":true})
                        } else {serde_json::json!({"partially_dispatched":true})}))
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
    anchor: std::cell::RefCell<Option<(super::anchor::Scope, super::anchor::ScopeSnapshot)>>,
    policy: super::policy::SampledPolicy,
}

impl CheckedAction<'_> {
    fn current(&self) -> Result<Point, IpcError> {
        self.pipeline.pointer.cursor_pos()
    }

    fn geometry(&self) -> Result<(), IpcError> {
        if let Some((scope, snapshot)) = self.anchor.borrow().as_ref() {
            if self.pipeline.region.anchor_scope(*scope, self.context)? != *snapshot {
                return Err(IpcError::stale_frame("定位期间窗口或显示器几何发生变化"));
            }
        }
        let binding = self.context.binding_id.as_deref().unwrap_or("");
        self.pipeline
            .region
            .check_geometry(binding, self.revision)?;
        if !self.context.background && !binding.is_empty()
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
        if let Some((_, snapshot)) = self.anchor.borrow().as_ref() {
            let rect = &snapshot.rect;
            for p in [start, end] {
                if p.x < rect.x || p.y < rect.y || p.x >= rect.x + rect.width || p.y >= rect.y + rect.height {
                    return Err(IpcError::region_limit_violation("定位动作路径越出当前范围"));
                }
            }
        }
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

    fn plan(&self, from: Point, to: Point, duration: u32) -> Result<Vec<Point>, IpcError> {
        let points = self.policy.trajectory(from, to, duration.div_ceil(MOVE_SLICE_MS).max(1));
        let mut previous = from;
        for point in &points {
            self.path(previous, *point)?;
            previous = *point;
        }
        Ok(points)
    }

    fn glide(&self, points: &[Point], duration: u32) -> Result<(), IpcError> {
        let count = points.len() as u32;
        for (index, &p) in points.iter().enumerate() {
            let i = index as u32;
            let at = |n| (u64::from(duration) * u64::from(n) / u64::from(count)) as u32;
            self.delay(at(i + 1) - at(i))?;
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

    fn click(&self, dest: Point, button: MouseButton, clicks: u32, hold_ms: u32, ledger: &std::sync::Mutex<Ledger>) -> Result<(), IpcError> {
        self.button_free(button, &ledger.lock().unwrap_or_else(|e| e.into_inner()))?;
        let [dx, dy] = self.policy.offset;
        let overflow = || IpcError::region_limit_violation("偏移坐标溢出");
        let dest = Point { x: dest.x.checked_add(dx).ok_or_else(overflow)?, y: dest.y.checked_add(dy).ok_or_else(overflow)? };
        let duration = if self.policy.curve.is_some() { 120 } else { 0 };
        let points = self.plan(self.current()?, dest, duration)?;
        self.glide(&points, duration)?;
        for _ in 0..clicks {
            self.button_down(button, ledger)?;
            if hold_ms != 0 { self.delay(hold_ms)?; }
            self.button_up(button, ledger)?;
        }
        Ok(())
    }

    fn recorded_keys(&self, events: &[super::model::RecordedEdge], ledger: &std::sync::Mutex<Ledger>) -> Result<(), IpcError> {
        use super::model::RecordedKey;
        let port = &self.pipeline.input;
        let target = port.key_target().map_err(|e| IpcError::window_unavailable(&e.message))?;
        let mut aliases = HashMap::new();
        let mut keys = Vec::with_capacity(events.len());
        for event in events {
            let key = match &event.key {
                RecordedKey::Named(k) => {
                    let mut key = k.clone();
                    if let Some((scan, extended)) = super::win32::vk_scan(target.1, key.vk) {
                        key.scan_code = Some(scan);
                        key.extended |= extended;
                    }
                    key
                }
                RecordedKey::Physical { scan_code, extended } => {
                    let vk = super::win32::scan_vk(target.1, *scan_code, *extended)
                        .ok_or_else(|| IpcError::invalid_params("当前目标键盘布局无法映射扫描码"))?;
                    Key { name: format!("scan-{scan_code}-{extended}"), vk, extended: *extended, scan_code: Some(*scan_code) }
                }
            };
            if let Some(previous) = aliases.insert(key.virtual_group(), &event.key) {
                if previous != &event.key { return Err(IpcError::invalid_params("键序包含相互冲突的键标识")); }
            }
            keys.push(key);
        }
        {
            let held = ledger.lock().unwrap_or_else(|e| e.into_inner());
            if keys.iter().any(|k| held.key_in_use(k) || port.externally_held(k.vk)) {
                return Err(IpcError::input_failed("键序含已被持有的键"));
            }
        }
        let start = std::time::Instant::now();
        for (event, key) in events.iter().zip(&keys) {
            let elapsed = start.elapsed().as_millis().min(u128::from(u32::MAX)) as u32;
            self.delay(event.offset_ms.saturating_sub(elapsed))?;
            self.geometry()?;
            let position = self.current()?;
            self.path(position, position)?;
            if port.key_target().ok() != Some(target) { return Err(IpcError::window_unavailable("接收键序的焦点已改变")); }
            if event.down {
                if ledger.lock().unwrap_or_else(|e| e.into_inner()).owns_key(self.execution, key) {
                    self.send(None, |p| p.key_event(key, true))?;
                } else {
                    self.key_down(key, ledger)?;
                }
            } else {
                self.key_up(key, ledger)?;
            }
        }
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
        if self.policy.delay > 0 {
            self.policy.wait(|| {
                if self.gate.paused(self.execution) {
                    return Err(IpcError::window_unavailable("执行已暂停"));
                }
                if !self.gate.is_open(self.execution) {
                    return Err(IpcError::invalid_state("输入闸门已关闭"));
                }
                Ok(())
            }, |ms| std::thread::sleep(Duration::from_millis(ms.into())))?;
            self.geometry()?;
        }
        match action {
            InputAction::WithPolicy { .. } => Err(IpcError::invalid_params("嵌套动作策略")),
            InputAction::ClickAnchor { anchor, button, clicks, hold_ms } => {
                let scope = self.pipeline.region.anchor_scope(anchor.scope, self.context)?;
                let p = anchor.resolve(scope.rect)?;
                self.anchor.replace(Some((anchor.scope, scope)));
                self.click(p, *button, *clicks, *hold_ms, ledger)
            }
            InputAction::ScrollAnchor { anchor, delta_raw } => {
                let snapshot = self.pipeline.region.anchor_scope(anchor.scope, self.context)?;
                let dest = anchor.resolve(snapshot.rect)?;
                self.anchor.replace(Some((anchor.scope, snapshot)));
                self.path(origin, dest)?;
                self.send(Some(dest), |p| p.move_to(dest.x, dest.y))?;
                self.send(None, |p| p.raw_wheel(*delta_raw))
            }
            InputAction::DragAnchor { points, duration_ms } => {
                self.button_free(MouseButton::Left, &ledger.lock().unwrap_or_else(|e| e.into_inner()))?;
                let scope = points[0].scope;
                let snapshot = self.pipeline.region.anchor_scope(scope, self.context)?;
                let mapped = points.iter().map(|p| p.resolve(snapshot.rect)).collect::<Result<Vec<_>,_>>()?;
                self.anchor.replace(Some((scope, snapshot)));
                let first = mapped[0];
                self.path(origin, first)?;
                let n = (mapped.len()-1) as u64;
                let at = |i: usize| (u64::from(*duration_ms) * i as u64 / n) as u32;
                let mut segments = Vec::with_capacity(mapped.len()-1);
                for (i, pair) in mapped.windows(2).enumerate() {
                    let duration = at(i+1) - at(i);
                    segments.push((self.plan(pair[0], pair[1], duration)?, duration));
                }
                self.send(Some(first), |p| p.move_to(first.x, first.y))?;
                self.button_down(MouseButton::Left, ledger)?;
                for (path, ms) in segments { self.glide(&path, ms)?; }
                self.button_up(MouseButton::Left, ledger)
            }
            InputAction::RecordedKeys { scope, events } => {
                let snapshot = self.pipeline.region.anchor_scope(*scope, self.context)?;
                self.anchor.replace(Some((*scope, snapshot)));
                self.path(origin, origin)?;
                self.recorded_keys(events, ledger)
            }
            InputAction::Move { x, y, duration_ms } => {
                let dest = Point { x: *x, y: *y };
                let points = self.plan(origin, dest, *duration_ms)?;
                self.glide(&points, *duration_ms)
            }
            InputAction::Click {
                x,
                y,
                button,
                clicks,
            } => self.click(Point { x: *x, y: *y }, *button, *clicks, 0, ledger),
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
                let points = self.plan(*from, *to, *duration_ms)?;
                self.send(Some(*from), |p| p.move_to(from.x, from.y))?;
                self.button_down(MouseButton::Left, ledger)?;
                self.glide(&points, *duration_ms)?;
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
