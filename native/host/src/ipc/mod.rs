//! IPC 协议层入口：Router 装配、单线程事件循环与传输解耦窄接口
//! （TASK-005 设计 §3.1/§4.1/§2.2）。
//!
//! 并发模型（§2.2）：全部入站事件（stdout 帧、UI invoke、取消请求）汇入**一条
//! mpsc 命令通道**，由**单一 Router 线程**串行消费；pending map/去重表无锁
//! （见 pending.rs）。UI 侧经 [`RouterHandle`] 投递请求并用 per-request oneshot
//! + 超时 recv 等待。
//!
//! 传输解耦（§2.1/§4.1）：ipc 层经 [`FrameSink`]（Supervisor 单 writer 实现）与
//! [`FrameSource`]（drain 线程实现）两个窄 trait 访问传输，sidecar 与 ipc 之间
//! 只有信封类型依赖——契约测试以内存替身注入，不 spawn 真进程。
//!
//! 批次说明（B2）：本模块以 trait 依赖注入独立编译可测；事件出口
//! [`EventHandler`] 骨架期为 Null 实现，事件总线/hostbridge 与 Supervisor 接线
//! 属批次 B3（drain→Router、直转移除、连续畸形/2MiB 接收侧）。

pub mod dispatcher;
pub mod eventbus;
pub mod hostbridge;
pub mod hostcall;
pub mod pending;
pub mod types;

#[cfg(test)]
mod contract_tests;

use std::sync::mpsc;
use std::time::Duration;

use crate::sidecar::protocol::ProtocolEnvelope;

pub use pending::{PendingMap, SystemClock, Waiter};
pub use types::{
    CommandClass, IpcError, Route, RouterInput, StopGate, NullStopGate, deadline_for,
    request_envelope, route_command,
};

// ---------------------------------------------------------------------------
// host_request 真 handler（M05 接入点，TASK-017）
// ---------------------------------------------------------------------------

/// `host_request` 真 handler trait（M05 接入点）：处理 03 宿主命令白名单内命令
/// （`input_action` / `acquire_input_lease` / `release_input_lease`）。
///
/// `command` 一并传入，使同一 handler 对象可服务多条命令（如 `InputManager`）。
/// `HostBridge` 的 `HostRegistry` 以 `command → handler` 注册；未注册 →
/// `CAPABILITY_DENIED`（诚实降级）。`handle` 在 Router/drain 上下文内同步执行。
pub trait HostHandler: Send {
    fn handle(
        &mut self,
        command: &str,
        params: &serde_json::Value,
    ) -> Result<serde_json::Value, IpcError>;
}

use pending::PendingMap as PendingMapCore;
use types::MAX_FRAME_BYTES;

// ---------------------------------------------------------------------------
// 传输解耦窄接口（设计 §4.1 逐字；sidecar 实现 FrameSink，drain 线程实现 FrameSource）
// ---------------------------------------------------------------------------

/// 传输 sink：写一帧到 sidecar stdin（Supervisor 单 writer；失败=管道断裂 →
/// 上层走清理）。参数为序列化前的信封 JSON（outgoing request 含 command/params，
/// 与 Supervisor::send_envelope(&Value) 直接对接；入站侧仍以 ProtocolEnvelope
/// 经 [`FrameSource`]/[`RouterInput::Frame`] 消费）。
pub trait FrameSink: Send {
    fn send_frame(&mut self, envelope: &serde_json::Value) -> Result<(), String>;
}

/// 传输 source：取一条已解析入站帧；None = EOF（drain 线程结束）。
pub trait FrameSource: Send {
    fn next_frame(&mut self) -> Option<ProtocolEnvelope>;
}

/// 帧/事件出口 Hook（§3.1 eventbus.rs/hostbridge.rs，批次 B3 注入实现）：
/// Router 收到 event / host_request 帧时回调；骨架期 no-op。
pub trait EventHandler: Send {
    fn on_frame(&mut self, envelope: &ProtocolEnvelope);
}

/// 骨架空事件出口（B2：事件只丢弃；B3 换 eventbus/hostbridge 组合）。
pub struct NullEventHandler;

impl EventHandler for NullEventHandler {
    fn on_frame(&mut self, _envelope: &ProtocolEnvelope) {}
}

/// Rust 本地宿主能力 handler（TASK-008 框选）：处理 [`Route::HostHandled`] 命令
/// （`start_selection_mode` / `confirm_selection` / `stop_selection_mode`）。
/// 装配时经 [`Router::with_host_commands`] 注入；未注入时 [`NullHostCommandHandler`]
/// 统一回 CAPABILITY_DENIED（诚实降级）。`dispatch` 在 Router 单线程内同步执行。
pub trait HostCommandHandler: Send {
    fn dispatch(
        &mut self,
        command: &str,
        params: &serde_json::Value,
    ) -> Result<serde_json::Value, IpcError>;
}

/// 骨架期默认实现：宿主能力未提供（06：CAPABILITY_DENIED）。
pub struct NullHostCommandHandler;

impl HostCommandHandler for NullHostCommandHandler {
    fn dispatch(
        &mut self,
        command: &str,
        _params: &serde_json::Value,
    ) -> Result<serde_json::Value, IpcError> {
        Err(IpcError::capability_denied(command))
    }
}

// ---------------------------------------------------------------------------
// Router：单线程事件循环（设计 §4.1/§4.2）
// ---------------------------------------------------------------------------

/// 协议路由器：消费 [`RouterInput`] 四类消息，串行处理请求关联、去重、背压、
/// 超时与转发（具体处理链在 dispatcher.rs 的 `impl Router`）。
pub struct Router {
    /// 当前会话 id（B3 引擎重启时经 `reset_session` 更新）。
    pub(crate) session_id: String,
    /// 单 writer（Supervisor 实现；契约测试用内存替身）。
    pub(crate) sink: Box<dyn FrameSink>,
    /// stop_script 转发前的输入闸门 Hook（M05 前为 NullStopGate）。
    pub(crate) gate: Box<dyn StopGate>,
    /// event/host_request 出口（B3 eventbus/hostbridge）。
    pub(crate) events: Box<dyn EventHandler>,
    /// Rust 本地宿主能力 handler（TASK-008 框选；默认 CapabilityDenied）。
    pub(crate) host_commands: Box<dyn HostCommandHandler>,
    /// 在途台账/去重/背压（单线程无锁）。
    pub(crate) pending: PendingMapCore,
}

impl Router {
    /// 装配 Router（时钟经 PendingMap 注入，契约测试可替换）。
    /// `host_commands` 默认 [`NullHostCommandHandler`]（骨架期 CAPABILITY_DENIED）；
    /// 生产/测试经 [`Router::with_host_commands`] 注入框选 handler。
    pub fn new(
        session_id: &str,
        sink: Box<dyn FrameSink>,
        gate: Box<dyn StopGate>,
        clock: Box<dyn pending::Clock>,
        events: Box<dyn EventHandler>,
    ) -> Self {
        Router {
            session_id: session_id.to_string(),
            sink,
            gate,
            events,
            host_commands: Box::new(NullHostCommandHandler),
            pending: PendingMapCore::with_clock(clock),
        }
    }

    /// 注入 Rust 本地宿主能力 handler（TASK-008 框选）。
    pub fn with_host_commands(mut self, host_commands: Box<dyn HostCommandHandler>) -> Self {
        self.host_commands = host_commands;
        self
    }

    /// 会话重建锚（§6.4；B3 引擎重启时调用）：更新 session_id 并清空去重台账
    /// 与在途（03：启动每次使用新 session_id，旧会话消息全部拒绝）。
    pub fn reset_session(&mut self, session_id: &str) {
        self.session_id = session_id.to_string();
        self.pending.reset();
    }

    /// 单线程事件循环：按最近在途期限计算 recv 超时（超时到期 → expire_due），
    /// 通道断开/Shutdown → 清空 pending（ENGINE_UNAVAILABLE 快速失败）后退出。
    pub fn run(&mut self, rx: mpsc::Receiver<RouterInput>) {
        loop {
            let wait = self
                .pending
                .next_deadline()
                .map(|d| d.saturating_duration_since(self.pending.now()));
            let input = match wait {
                Some(timeout) => match rx.recv_timeout(timeout) {
                    Ok(input) => input,
                    Err(mpsc::RecvTimeoutError::Timeout) => {
                        self.pending.expire_due();
                        continue;
                    }
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                },
                None => match rx.recv() {
                    Ok(input) => input,
                    Err(_) => break,
                },
            };
            match input {
                RouterInput::Frame(envelope) => self.handle_frame(envelope),
                RouterInput::UiRequest { id, command, params, reply } => {
                    self.handle_ui_request(id, command, params, reply);
                }
                RouterInput::UiCancel { request_id, reply } => {
                    self.handle_ui_cancel(request_id, reply);
                }
                RouterInput::Shutdown => {
                    self.pending
                        .fail_all(IpcError::engine_unavailable("引擎会话已关闭"));
                    break;
                }
            }
        }
        // 兜底：无论退出原因，在途等待者一律快速失败（幂等）
        self.pending
            .fail_all(IpcError::engine_unavailable("Router 已停止"));
    }
}

// ---------------------------------------------------------------------------
// RouterHandle：UI/commands 侧入口（命令层与契约测试共用）
// ---------------------------------------------------------------------------

/// Router 的线程安全句柄（Clone；宿主命令层经此投递请求）。
#[derive(Clone)]
pub struct RouterHandle {
    tx: mpsc::Sender<RouterInput>,
}

/// 客户端 recv 余量：服务端期限先到（§5.1 同步命令，§3.3）。
const CLIENT_RECV_MARGIN: Duration = Duration::from_millis(500);

impl RouterHandle {
    pub fn new(tx: mpsc::Sender<RouterInput>) -> Self {
        RouterHandle { tx }
    }

    /// `ipc_request`（§5.1）：投递 UiRequest 后按命令期限 +500ms 余量等待
    /// （服务端超时先达 → UI 得到带 request_id 的 INTERNAL_ERROR）。
    pub fn request(&self, id: &str, command: &str, params: serde_json::Value) -> Result<serde_json::Value, IpcError> {
        let (tx, rx) = mpsc::channel();
        self.tx
            .send(RouterInput::UiRequest {
                id: id.to_string(),
                command: command.to_string(),
                params,
                reply: tx,
            })
            .map_err(|_| IpcError::engine_unavailable("Router 已关闭"))?;
        let wait = deadline_for(command) + CLIENT_RECV_MARGIN;
        rx.recv_timeout(wait).unwrap_or_else(|_| {
            Err(IpcError::timeout(
                deadline_for(command).as_millis() as u64,
                id,
            ))
        })
    }

    /// `ipc_cancel`（§5.1）：幂等；data = {cancelled, reason}（03）。
    pub fn cancel(&self, request_id: &str) -> Result<serde_json::Value, IpcError> {
        let (tx, rx) = mpsc::channel();
        self.tx
            .send(RouterInput::UiCancel {
                request_id: request_id.to_string(),
                reply: tx,
            })
            .map_err(|_| IpcError::engine_unavailable("Router 已关闭"))?;
        let wait = deadline_for("cancel_request") + CLIENT_RECV_MARGIN;
        rx.recv_timeout(wait).unwrap_or_else(|_| {
            Err(IpcError::timeout(
                deadline_for("cancel_request").as_millis() as u64,
                request_id,
            ))
        })
    }

    /// 引擎清理/应用退出：清空 pending（ENGINE_UNAVAILABLE 快速失败）。
    pub fn shutdown(&self) {
        let _ = self.tx.send(RouterInput::Shutdown);
    }
}

/// 装配并启动 Router 线程（B3 接线入口；契约测试亦可直接用）。
pub fn spawn_router(mut router: Router) -> (RouterHandle, std::thread::JoinHandle<()>) {
    let (tx, rx) = mpsc::channel::<RouterInput>();
    let handle = RouterHandle::new(tx);
    let join = std::thread::spawn(move || router.run(rx));
    (handle, join)
}

/// 发送侧 2MiB 护栏（§6.5：send 前测序列化字节 >2MiB → 拒绝发送）。
pub fn frame_over_limit(envelope: &serde_json::Value) -> bool {
    match serde_json::to_string(envelope) {
        Ok(line) => line.len() > MAX_FRAME_BYTES,
        Err(_) => true, // 序列化失败视为不可发送
    }
}
