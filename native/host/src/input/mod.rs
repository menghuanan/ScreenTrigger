//! Rust 输入闸门、租约与动作（TASK-017 / M05）。
//!
//! **职责**：唯一物理输入所有者——租约、动作派发（move/click/drag/scroll/key/
//! text/hotkey）、停止闸门、区域/窗口/几何校验、`action_id` 去重、按键账本、
//! Unicode。不保证外部应用业务成功、不做恶意脚本沙箱。
//!
//! **对外接口**（03 宿主命令，走 `host_request`，不进 UI 命令目录）：
//! - [`InputManager::handle`]：`input_action` / `acquire_input_lease` /
//!   `release_input_lease`（`window_snapshot` 归 TASK-018）。
//! - [`InputStopGate`]：实现 [`ipc::StopGate`](crate::ipc::StopGate) +
//!   [`sidecar::InputGate`](crate::sidecar::InputGate)，供 `stop_script`/退出序列先关闸门。
//! - [`install_manager`]/[`shared_manager`]：进程级共享（`lib.rs` setup 装配，
//!   `sidecar::wire_router` 取用）。
//!
//! **模块边界**（01§M05）：实际 OS 输入只有本模块可调用 SendInput；Python 只能经
//! `backend/input_proxy` → host_request。
//!
//! **来源章节**：docs/01 §M05/§M08、docs/03 §全双工与宿主调用、docs/06 §坐标区域与
//! 输入不变量、docs/07 §5。

pub mod action;
pub mod dispatch;
pub mod gate;
pub mod geometry_guard;
pub mod lease;
pub mod ledger;
pub mod model;
#[cfg(test)]
mod tests;
pub mod win32;
pub mod wiring;

use std::sync::{Arc, Mutex};

use serde_json::Value;

use crate::ipc::{HostHandler, IpcError, StopGate};
use crate::sidecar::InputGate;

use dispatch::DispatchPipeline;
use gate::GateTable;
use lease::LeaseTable;
use ledger::Ledger;
use model::{
    ActionAck, ClockPort, InputAction, InputContext, InputPort, LeaseId, PointerPort,
    RegionResolutionPort,
};

/// 输入管理器：包租约表/闸门/账本/派发流水线，向宿主调用暴露 [`HostHandler`]。
pub struct InputManager {
    gate: Arc<GateTable>,
    leases: LeaseTable,
    ledger: Arc<Mutex<Ledger>>,
    pipeline: DispatchPipeline,
}

impl InputManager {
    /// 装配（全部副作用端口注入）。
    pub fn new(
        input: Box<dyn InputPort>,
        pointer: Box<dyn PointerPort>,
        region: Box<dyn RegionResolutionPort>,
        clock: Box<dyn ClockPort>,
    ) -> Self {
        InputManager {
            gate: Arc::new(GateTable::new()),
            leases: LeaseTable::new(),
            ledger: Default::default(),
            pipeline: DispatchPipeline::new(input, pointer, region, clock),
        }
    }

    /// 共享闸门句柄（`InputStopGate`/`wire_router` 用）。
    pub fn gate(&self) -> Arc<GateTable> {
        Arc::clone(&self.gate)
    }

    // -----------------------------------------------------------------------
    // host_request 命令分派（03 宿主命令；参数校验 + 委托）
    // -----------------------------------------------------------------------

    /// 分派一条宿主命令，返回 03 约定的 `data` 形状。
    pub fn handle(&mut self, command: &str, params: &Value) -> Result<Value, IpcError> {
        match command {
            "query_pointer" if params.as_object().is_some_and(|p| p.is_empty()) => self.pipeline.query_pointer(),
            "query_pointer" => Err(IpcError::invalid_params("query_pointer 的参数必须为空对象")),
            "input_action" => self.handle_input_action(params),
            "acquire_input_lease" => self.handle_acquire_lease(params),
            "release_input_lease" => self.handle_release_lease(params),
            other => Err(IpcError::unknown_command(other)),
        }
    }

    /// `input_action`：`{execution_id,lease_id,action_id,geometry_revision,action}` → `{action_id,dispatched:true}`。
    fn handle_input_action(&mut self, params: &Value) -> Result<Value, IpcError> {
        let execution_id = req_str(params, "execution_id")?;
        let lease_id = req_str(params, "lease_id")?;
        let action_id = req_str(params, "action_id")?;
        let geometry_revision = params
            .get("geometry_revision")
            .and_then(Value::as_i64).filter(|r| *r >= 0)
            .ok_or_else(|| IpcError::invalid_params("geometry_revision 必须是非负整数"))?;
        let action_value = params
            .get("action")
            .ok_or_else(|| IpcError::invalid_params("缺少 action（object）"))?;
        let action = action::parse_action(action_value)?;
        let ack = self.dispatch_action(
            &execution_id,
            &lease_id,
            &action_id,
            geometry_revision,
            &action,
        )?;
        Ok(ack.to_value())
    }

    /// `acquire_input_lease`：`{execution_id,context:{...}}` → `{lease_id}`。
    fn handle_acquire_lease(&mut self, params: &Value) -> Result<Value, IpcError> {
        let execution_id = req_str(params, "execution_id")?;
        let context = InputContext::parse(params.get("context"))?;
        let lease_id = self.acquire_lease(&execution_id, context)?;
        Ok(serde_json::json!({ "lease_id": lease_id.as_str() }))
    }

    /// `release_input_lease`：`{execution_id}` → `{ok:true}`。
    fn handle_release_lease(&mut self, params: &Value) -> Result<Value, IpcError> {
        let execution_id = req_str(params, "execution_id")?;
        self.release_input_lease(&execution_id)?;
        Ok(serde_json::json!({ "ok": true }))
    }

    // -----------------------------------------------------------------------
    // 领域操作（供命令面与内部调用）
    // -----------------------------------------------------------------------

    /// 获取租约（闸门已关 → INVALID_STATE；已持有 → INVALID_STATE；>3 → BUSY）。
    pub fn acquire_lease(
        &mut self,
        execution_id: &str,
        context: InputContext,
    ) -> Result<LeaseId, IpcError> {
        if !self.gate.is_open(execution_id) {
            return Err(IpcError::invalid_state("闸门已关闭，拒绝获取输入租约"));
        }
        let binding = context.binding_id.clone();
        let lease=self.leases.acquire(execution_id,context)?;
        if let Err(e)=self.gate.register(execution_id,binding) { self.leases.release(execution_id); return Err(e); }
        Ok(lease)
    }

    /// 派发一个动作（闸门 → 租约 → 去重 → 复核 → 串行 SendInput → 账本）。
    pub fn dispatch_action(
        &mut self,
        execution_id: &str,
        lease_id: &str,
        action_id: &str,
        geometry_revision: i64,
        action: &InputAction,
    ) -> Result<ActionAck, IpcError> {
        self.pipeline.run(
            &self.gate,
            &self.leases,
            &self.ledger,
            execution_id,
            lease_id,
            action_id,
            geometry_revision,
            action,
        )
    }

    /// 释放该执行持有的键/鼠标按钮（账本逆序；只释放本程序持有者）。
    pub fn release_owned_inputs(&mut self, execution_id: &str) -> usize {
        self.ledger.lock().unwrap_or_else(|e|e.into_inner()).release_owned(execution_id, self.pipeline.input_port())
    }

    /// 释放所有执行持有的键/鼠标按钮（退出清理）。
    pub fn release_all_owned(&mut self) -> usize {
        self.ledger.lock().unwrap_or_else(|e|e.into_inner()).release_all(self.pipeline.input_port())
    }

    /// 释放租约（Lua 侧 release；未持有 → INVALID_STATE）。同时尽力释放账本。
    pub fn release_input_lease(&mut self, execution_id: &str) -> Result<(), IpcError> {
        let removed = self.leases.release(execution_id);
        self.gate.unregister(execution_id);
        if let Some(ref lease) = removed { self.pipeline.forget_lease(lease.as_str()); }
        let released = self.release_owned_inputs(execution_id);
        match removed {
            Some(_) => Ok(()),
            None => {
                let _ = released;
                Err(IpcError::invalid_state("该执行未持有输入租约"))
            }
        }
    }

    /// 关闭单个执行的闸门（**立即**拒绝新输入）并尽力释放其账本。
    pub fn close_gate(&mut self, execution_id: &str, reason: &str) {
        self.gate.close(execution_id, reason);
        let released = self.release_owned_inputs(execution_id);
        eprintln!(
            "[input] close_gate execution_id={execution_id} reason={reason} released={released}"
        );
    }

    /// 关闭全局闸门（应用退出/引擎清理）。
    pub fn close_all(&mut self, reason: &str) {
        self.gate.close_all(reason);
        eprintln!("[input] close_all reason={reason}");
    }

    /// 仅关闭该执行的闸门（不触账本；供跨线程快速关闭，避免与派发互斥区争用）。
    pub fn close_gate_only(&self, execution_id: &str, reason: &str) {
        self.gate.close(execution_id, reason);
    }
}

/// 读取必填字符串参数。
fn req_str(params: &Value, key: &str) -> Result<String, IpcError> {
    params
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .ok_or_else(|| IpcError::invalid_params(&format!("缺少非空字符串参数 {key}")))
}

// ---------------------------------------------------------------------------
// 共享包装（HostBridge 真 handler 注册）
// ---------------------------------------------------------------------------

/// 共享输入管理器 handler（`Arc<Mutex<InputManager>>` 的 [`HostHandler`] 适配）。
pub struct SharedInputManager(pub Arc<Mutex<InputManager>>);

impl HostHandler for SharedInputManager {
    fn handle(&mut self, command: &str, params: &Value) -> Result<Value, IpcError> {
        self.0
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .handle(command, params)
    }
}

// ---------------------------------------------------------------------------
// 停止闸门（stop_script / 退出序列）
// ---------------------------------------------------------------------------

/// 输入停止闸门：实现 [`StopGate`] + [`InputGate`]。
///
/// 关闭操作只锁**闸门自身**的小互斥（[`GateTable`]），与派发互斥区解耦，因此即使
/// 有长动作正在进行也能**立即**关闸；账本释放为 best-effort（`try_lock` 失败即跳过，
/// 由派发侧在观察到闸门关闭后自行中止并释放）。
pub struct InputStopGate {
    gate: Arc<GateTable>,
    manager: Option<Arc<Mutex<InputManager>>>,
}

impl InputStopGate {
    /// 以共享闸门（+ 可选共享管理器用于账本释放）构造。
    pub fn new(gate: Arc<GateTable>, manager: Option<Arc<Mutex<InputManager>>>) -> Self {
        InputStopGate { gate, manager }
    }

    fn release_execution(&self, execution_id: Option<&str>) {
        let Some(manager) = &self.manager else {
            return;
        };
        let Ok(mut guard) = manager.try_lock() else {
            release_closed();
            return;
        };
        match execution_id {
            Some(exec) => {
                guard.release_owned_inputs(exec);
            }
            None => {
                guard.release_all_owned();
            }
        }
    }
}

impl StopGate for InputStopGate {
    fn close_gate(&self, reason: &str) {
        self.gate.close_all(reason);
        self.release_execution(None);
        eprintln!("[input] StopGate.close_gate reason={reason}");
    }

    fn close_gate_for(&self, execution_id: &str, reason: &str) {
        self.gate.close(execution_id, reason);
        self.release_execution(Some(execution_id));
        eprintln!("[input] StopGate.close_gate_for execution_id={execution_id} reason={reason}");
    }
}

impl InputGate for InputStopGate {
    fn close_gate(&self, reason: &str) {
        <Self as StopGate>::close_gate(self, reason);
    }
}

/// 延迟解析的输入闸门（`Supervisor` 退出序列用）：调用时读取进程级共享
/// [`InputManager`]；未装配则 no-op。避免 `Supervisor` 构造早于 `lib.rs` setup 装配
/// 的顺序问题。
pub struct LazyInputGate;

impl InputGate for LazyInputGate {
    fn close_gate(&self, reason: &str) {
        match shared_gate() {
            Some(gate) => gate.close_all(reason),
            None => return,
        }
        if let Some(manager) = shared_manager() {
            if let Ok(mut guard) = manager.try_lock() {
                guard.release_all_owned();
            }
        }
        release_closed();
        eprintln!("[input] LazyInputGate.close_gate reason={reason}");
    }
}

// ---------------------------------------------------------------------------
// 进程级共享（宿主初始化 装配；sidecar wire_router 取用）
// ---------------------------------------------------------------------------

struct Installed { manager: Arc<Mutex<InputManager>>, gate: Arc<GateTable>, ledger:Arc<Mutex<Ledger>>, input:Arc<dyn InputPort> }
pub fn release_closed(){
    let held=MANAGER.lock().unwrap_or_else(|e|e.into_inner());
    if let Some(s)=held.as_ref() {
        let mut ledger=s.ledger.lock().unwrap_or_else(|e|e.into_inner());
        for id in s.gate.active(None) {
            if !s.gate.is_open(&id) || s.gate.paused(&id) {ledger.release_owned(&id,s.input.as_ref());}
        }
    }
}
static MANAGER: Mutex<Option<Installed>> = Mutex::new(None);
pub fn install_manager(manager: Arc<Mutex<InputManager>>) -> bool {
    let (gate,ledger,input)={let m=manager.lock().unwrap_or_else(|p|p.into_inner());(m.gate(),m.ledger.clone(),m.pipeline.shared_input())};
    let mut slot=MANAGER.lock().unwrap_or_else(|p|p.into_inner());
    if let Some(old)=slot.as_ref() {old.gate.close_all("engine_session_changed");}
    *slot=Some(Installed{manager,gate,ledger,input}); true
}
pub fn shared_manager() -> Option<Arc<Mutex<InputManager>>> {
    MANAGER.lock().unwrap_or_else(|p|p.into_inner()).as_ref().map(|s|s.manager.clone())
}
pub fn shared_gate() -> Option<Arc<GateTable>> {
    MANAGER.lock().unwrap_or_else(|p|p.into_inner()).as_ref().map(|s|s.gate.clone())
}

/// 构造共享停止闸门（未装配输入管理器 → None，路由层回退 `NullStopGate`）。
pub fn shared_stop_gate() -> Option<InputStopGate> {
    let gate = shared_gate()?;
    Some(InputStopGate::new(gate, shared_manager()))
}
