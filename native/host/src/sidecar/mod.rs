//! Python sidecar 唯一所有者：Supervisor（设计说明 §2.2–§2.6、§4.1、§5.1–§5.3）。
//!
//! 架构分层：`commands.rs`（invoke 面）→ 本模块 `Supervisor` → `job`/`protocol`
//! （OS / 解析细节）。Supervisor 是 Python 进程树的唯一所有者。
//!
//! 关键行为：
//! - 状态机（§2.4）：Idle → Starting → Ready → Stopping → Exited；
//!   终态不复活（06），重启总是新 session_id（`session-` + uuid-v4）；
//! - spawn（§2.3）：参数数组（无 shell，天然防注入）、stdin/stdout/stderr 全
//!   piped、CREATE_NO_WINDOW；spawn 后立即 AssignProcessToJobObject；
//! - 3 线程：stdout drain / stderr drain（500 行环形缓冲）/ 30s 启动看门狗；
//! - hello 握手（§2.3）：system_ready → hello 请求 → 响应成功才 Ready（§2.4）；
//! - 退出序列（§2.5）：close_gate → 拒绝新请求 → stdin EOF → 2s 优雅期 →
//!   TerminateProcess → Job Object CloseHandle（KILL_ON_JOB_CLOSE 兜底）→ join 线程；
//! - 资源路径（§2.6）：`resolve_sidecar_command()` 单一函数 + 编译期分支；
//!   打包态只解析 + 明确报错，**不回退系统 python**（07 §4）。

pub mod job;
pub mod protocol;
pub mod transport;
mod writer;

use std::collections::VecDeque;
use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use uuid::Uuid;

use crate::ipc::eventbus::EventBus;
use crate::ipc::hostbridge::{HostBridge, IpcOutlet};
use crate::ipc::types::{RouterInput, MAX_FRAME_BYTES};
use crate::ipc::{FrameSink, NullStopGate, Router, RouterHandle, StopGate};
use crate::ipc::pending::SystemClock;

use protocol::{ProtocolEnvelope, ProtocolParseError};

// ---------------------------------------------------------------------------
// 常量（设计 §2.3/§2.5/§4.1/§4.3；docs/06 表：启动 30s、优雅期 2s）
// ---------------------------------------------------------------------------

/// 启动看门狗：30s 内未完成 system_ready + hello 往返 → 判启动失败（06：启动 30s）。
pub const STARTUP_WATCHDOG_SECS: u64 = 30;
/// 优雅退出期：stdin EOF 后最多 2s，未退出则 TerminateProcess（06：停止清理）。
pub const GRACEFUL_EXIT_SECS: u64 = 2;
/// stderr 环形缓冲容量（设计 §2.3：500 行，内存有界）。
pub const STDERR_TAIL_CAPACITY: usize = 500;
/// `engine_diagnostics.stderr_tail` 视图行数（§4.1：最近 ≤50 行）。
pub const DIAGNOSTICS_TAIL_LINES: usize = 50;
/// 连续畸形帧关会话阈值（TASK-005 设计 §6.5：03 只有规则无数值，取 10；
/// 中间插入任何合法帧即清零；§9-D1 随文档同步冻结）。
pub const CONSECUTIVE_MALFORMED_LIMIT: usize = 10;
/// 打包态 sidecar 可执行名（PyInstaller 产物，TASK-014 接入）。
/// 仅 release 构建使用（debug 下允许未使用告警豁免）。
#[cfg_attr(debug_assertions, allow(dead_code))]
const PACKAGED_SIDECAR_NAME: &str = "screentrigger-engine-x86_64-pc-windows-msvc.exe";

// ---------------------------------------------------------------------------
// 视图结构（§4.1，serde snake_case，与 IPC/数据契约风格一致）
// ---------------------------------------------------------------------------

/// 引擎健康状态机（Rust Host 视角，§2.4）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum EngineState {
    Idle,
    Starting,
    Ready,
    Stopping,
    Exited,
}

/// 能力徽标条目（最小子集 {id,state,reason}，capability-manifest 模板措辞）。
#[derive(Debug, Clone, Serialize)]
pub struct CapabilityView {
    pub id: String,
    pub state: String, // "available" | "pending" | "unavailable"
    pub reason: Option<String>,
}

/// 版本信息（hello 缓存；未 ready 时 status 中为 null）。
#[derive(Debug, Clone, Serialize)]
pub struct VersionsView {
    pub protocol_version: u32,
    pub engine_version: String,
    pub lua_version: Option<String>,
    pub schema_version: u32,
}

/// `engine_start` / `engine_stop` 返回结构与 `engine_status` 相同（§4.1）。
#[derive(Debug, Clone, Serialize)]
pub struct EngineStatusView {
    pub state: EngineState,
    pub session_id: Option<String>,
    pub pid: Option<u32>,
    pub started_at: Option<String>, // RFC3339 UTC
    pub ready_at: Option<String>,
    pub uptime_ms: Option<u64>,
    pub last_error: Option<String>,
    pub capabilities: Vec<CapabilityView>, // 未 ready 时为空数组
    pub versions: Option<VersionsView>,
}

/// 诊断视图（`engine_diagnostics` 返回，§4.1）。
#[derive(Debug, Clone, Serialize)]
pub struct EngineDiagnostics {
    pub state: EngineState,
    pub session_id: Option<String>,
    pub pid: Option<u32>,
    pub exit_code: Option<i32>,
    pub stderr_tail: Vec<String>, // 环形缓冲 500 行的最近 ≤50 行视图
    pub stdout_event_count: u64,
    pub malformed_line_count: u64,
    pub job_object_active: bool,
    pub started_at: Option<String>,
    pub exited_at: Option<String>,
}

// ---------------------------------------------------------------------------
// 输入闸门占位（§2.5：退出序列第一步 close_gate；M05 落地替换实现，不改序列）
// ---------------------------------------------------------------------------

/// 闸门 Hook trait（M05 预留位）：退出序列的第一步永远是关闸门，
/// 保证"先关闸门再清理"从骨架起就是结构性的（§2.5 注）。
pub trait InputGate: Send {
    fn close_gate(&self, reason: &str);
}

/// 骨架空实现：永不派发任何输入，close_gate 为 no-op。
pub struct NullGate;

impl InputGate for NullGate {
    fn close_gate(&self, _reason: &str) {
        // no-op：骨架阶段无输入派发能力（§2.5 步骤 2 调用点已存在）
    }
}

// ---------------------------------------------------------------------------
// 状态机转换表（§2.4/§5.1）——纯函数，便于单测覆盖全路径
// ---------------------------------------------------------------------------

/// 状态机迁移事件。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransitionEvent {
    /// engine_start 受理（Idle/Exited → Starting；重启新 session_id）。
    StartRequested,
    /// system_ready 收到且 hello 往返成功（Starting → Ready，§2.4）。
    HandshakeCompleted,
    /// engine_stop 受理或应用退出（Starting/Ready → Stopping）。
    StopRequested,
    /// 清理序列（§2.5）完成（Starting/Ready/Stopping → Exited；终态不复活）。
    CleanupCompleted,
}

impl EngineState {
    /// §2.4 转换表的唯一权威实现；非法迁移返回 None（调用方记日志并忽略，
    /// 例：Exited 后迟到的 hello 响应不得复活状态，06"终态不可复活"）。
    pub fn transition(current: EngineState, event: TransitionEvent) -> Option<EngineState> {
        match (current, event) {
            (EngineState::Idle, TransitionEvent::StartRequested)
            | (EngineState::Exited, TransitionEvent::StartRequested) => Some(EngineState::Starting),
            (EngineState::Starting, TransitionEvent::HandshakeCompleted) => Some(EngineState::Ready),
            (EngineState::Starting, TransitionEvent::StopRequested)
            | (EngineState::Ready, TransitionEvent::StopRequested) => Some(EngineState::Stopping),
            (EngineState::Starting, TransitionEvent::CleanupCompleted)
            | (EngineState::Ready, TransitionEvent::CleanupCompleted)
            | (EngineState::Stopping, TransitionEvent::CleanupCompleted) => Some(EngineState::Exited),
            // 其余组合全部非法：Idle 从未启动、终态不复活、Ready 不重复握手等
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// Supervisor 与 SupervisorHandle
// ---------------------------------------------------------------------------

/// stdout drain 线程 → 握手/泵线程的消息（握手期间由 start 循环消费，
/// ready 之后由 frame bridge 线程消费；MalformedOverflow = 连续畸形达阈值
/// （§6.5 关会话触发信号，drain 线程停止读取后发出）。
#[derive(Debug)]
enum StdoutMessage {
    Line(ProtocolEnvelope),
    Eof,
    MalformedOverflow,
}

/// stdout 行处置分类（`classify_stdout_line` 产出；drain 线程消费，纯函数可测）。
#[derive(Debug)]
enum StdoutLineVerdict {
    /// 结构合法帧（session 匹配与否由下游握手/Router 丢弃，不计畸形）。
    Frame(ProtocolEnvelope),
    /// session_id 缺失/类型非法/格式非法：丢弃+日志，**不计畸形**且清零连续
    /// 计数（用户契约③；schema 用例 drop-missing-session-id 双端 counted=false；
    /// 与 Python"parse 不过 session_id → verify 丢弃前已清零"观测行为对称）。
    DropSessionLine,
    /// 畸形帧：计数+日志（载荷为 eprintln 日志文本）。
    Malformed(String),
}

/// stdout 单行分类（drain 线程用；把计数语义收敛为纯函数便于单测覆盖）。
fn classify_stdout_line(raw: &str) -> StdoutLineVerdict {
    match protocol::parse_line(raw) {
        Ok(envelope) => StdoutLineVerdict::Frame(envelope),
        Err(
            ProtocolParseError::MissingField("session_id")
            | ProtocolParseError::InvalidFieldType("session_id")
            | ProtocolParseError::InvalidSessionId(_),
        ) => StdoutLineVerdict::DropSessionLine,
        Err(err) => StdoutLineVerdict::Malformed(err.to_string()),
    }
}

/// 容错逐行读取（BUG-6 修复，§5.2/§6.2 畸形数据语义）：
///
/// - 正常行（UTF-8 合法，已去除行尾 \r\n）→ `on_line(Ok(行))`；
/// - **UTF-8 解码失败 → `on_line(Err(字节数))`：调用方计数+日志后继续读，
///   不得中断数据流**（旧实现 `reader.lines()` 把 InvalidData 当 EOF，
///   非 ASCII 畸形字节会造成假性断流）；
/// - EOF（读到 0 字节）→ 正常返回；
/// - 真实 IO 错误（进程死亡/管道断裂等）→ 原样返回 Err，由调用方按原
///   EOF/错误路径处理（EOF 判定语义不变）。
///
/// 字节级策略：以 \n 分帧逐段读取（read_until），段内非法 UTF-8 计 1 次
/// 畸形——与 protocol.rs 的按行畸形计数口径对齐。
///
/// `on_line` 返回 false 表示提前终止读取（如 stdout 接收端已消失）。
fn for_each_line_lossy<R: BufRead>(
    reader: &mut R,
    mut on_line: impl FnMut(Result<&str, usize>) -> bool,
) -> std::io::Result<()> {
    let mut buf: Vec<u8> = Vec::new();
    loop {
        buf.clear();
        // Bound memory before a newline arrives. Drain the remainder without retaining it.
        match reader.by_ref().take((MAX_FRAME_BYTES + 2) as u64).read_until(b'\n', &mut buf) {
            Ok(0) => return Ok(()), // EOF
            Ok(_) => {
                if buf.len() == MAX_FRAME_BYTES + 2 && !buf.ends_with(b"\n") {
                    let remaining = reader.skip_until(b'\n')?;
                    if !on_line(Err(buf.len().saturating_add(remaining))) { return Ok(()); }
                    continue;
                }
                // 去除行尾 \r\n（先判 \n 再剥连续 \r，兼容 \r\n 与 \r）
                while matches!(buf.last(), Some(b'\n') | Some(b'\r')) {
                    buf.pop();
                }
                match std::str::from_utf8(&buf) {
                    Ok(line) => {
                        if !on_line(Ok(line)) {
                            return Ok(());
                        }
                    }
                    Err(_) => {
                        if !on_line(Err(buf.len())) {
                            return Ok(());
                        }
                    }
                }
            }
            Err(e) => return Err(e),
        }
    }
}

/// begin_start 的结果。
enum StartOutcome {
    /// 本次调用发起新启动，返回 stdout 接收端（启动代数经 Supervisor 状态读取）。
    Initiated { generation: u64, rx: Receiver<StdoutMessage> },
    /// 幂等：Starting/Ready/Stopping 直接返回当前状态。
    AlreadyRunning,
    /// 解析/创建失败：状态已落 Exited(last_error=spawn_failed: ...)。
    Failed(String),
}

/// 握手循环对单条 stdout 消息的处理结果。
enum HandshakeStep {
    /// 继续等待（已发 hello 请求等）。
    Continue,
    /// 与握手无关（跨 session / 迟到消息）：只记日志（03：旧会话消息全部拒绝）。
    Irrelevant,
    /// 握手成功，携带 system_ready 完整信封（契约④：经事件总线投递 UI，
    /// 不再直转；§2.3 步骤 11 的转发职责移交 Router）。
    Ready(ProtocolEnvelope),
}

/// Supervisor：进程树唯一所有者。全部字段仅经 `SupervisorHandle` 的 Mutex 访问。
struct Supervisor {
    state: EngineState,
    session_id: Option<String>,
    child: Option<Child>,
    job: Option<job::JobObject>,
    /// 父进程 stdin 写端（命名管道 client；关闭即子进程 EOF——修订-01）
    stdin_client: Option<writer::SessionWriter>,
    /// 父进程 stdout 读端（命名管道 server；移交 drain 线程）
    stdout_pipe: Option<File>,
    /// 父进程 stderr 读端（命名管道 server；移交 drain 线程）
    stderr_pipe: Option<File>,
    gate: Box<dyn InputGate>,
    capabilities: Vec<CapabilityView>,
    versions: Option<VersionsView>,
    // 与 stderr drain 线程共享（线程不持有 Supervisor 锁，避免握手期间死锁）
    stderr_tail: Arc<Mutex<VecDeque<String>>>,
    stdout_event_count: Arc<AtomicU64>,
    malformed_line_count: Arc<AtomicU64>,
    last_error: Option<String>,
    exit_code: Option<i32>,
    started_at_wall: Option<String>,
    ready_at_wall: Option<String>,
    exited_at_wall: Option<String>,
    started_instant: Option<Instant>,
    pid: Option<u32>,
    request_seq: u64,
    /// 启动代数：区分看门狗实例，防止旧看门狗误杀新一轮启动。
    generation: u64,
    cleanup_done: bool,
    stdout_join: Option<JoinHandle<()>>,
    stderr_join: Option<JoinHandle<()>>,
    // 握手中间态
    hello_request_id: Option<String>,
    system_ready_seen: bool,
    /// system_ready 完整信封（hello 成功后经 Router 事件总线投递 UI，契约④）。
    pending_ready_event: Option<ProtocolEnvelope>,
    /// 本会话 Router 句柄（cleanup 时 Shutdown → pending 快速失败）。
    router: Option<RouterHandle>,
    /// Router 命令通道原始发送端（system_ready 首事件注入，契约④）。
    router_tx: Option<mpsc::Sender<RouterInput>>,
    /// UI 事件出口回调（桌面入口注入一次；
    /// 未注入时事件仅记 stderr 日志——契约测试/无窗口环境兜底）。
    event_emitter: Option<crate::ipc::eventbus::EmitFn>,
}

/// Supervisor 的 [`FrameSink`] 实现：Router/HostBridge 经此写 sidecar stdin
/// （单 writer 语义由 Supervisor 锁保证；写失败 = 管道断裂 → 上层清理）。
struct SupervisorFrameSink {
    inner: Arc<Mutex<Supervisor>>,
}

impl FrameSink for SupervisorFrameSink {
    fn send_frame(&mut self, envelope: &serde_json::Value) -> Result<(), String> {
        lock_sup(&self.inner).send_envelope(envelope)
    }
}

impl Supervisor {
    fn new() -> Self {
        Supervisor {
            state: EngineState::Idle,
            session_id: None,
            child: None,
            job: None,
            stdin_client: None,
            stdout_pipe: None,
            stderr_pipe: None,
            gate: Box::new(crate::input::LazyInputGate),
            capabilities: Vec::new(),
            versions: None,
            stderr_tail: Arc::new(Mutex::new(VecDeque::with_capacity(STDERR_TAIL_CAPACITY))),
            stdout_event_count: Arc::new(AtomicU64::new(0)),
            malformed_line_count: Arc::new(AtomicU64::new(0)),
            last_error: None,
            exit_code: None,
            started_at_wall: None,
            ready_at_wall: None,
            exited_at_wall: None,
            started_instant: None,
            pid: None,
            request_seq: 0,
            generation: 0,
            cleanup_done: false,
            stdout_join: None,
            stderr_join: None,
            hello_request_id: None,
            system_ready_seen: false,
            pending_ready_event: None,
            router: None,
            router_tx: None,
            event_emitter: None,
        }
    }

    /// `engine_status` / `engine_start` / `engine_stop` 返回视图（§4.1）。
    fn start_result(&self, generation: u64) -> Result<EngineStatusView, String> {
        if self.generation != generation {
            return Err("启动请求已被新会话取代".into());
        }
        // The watchdog can finish cleanup before the handshake thread wakes up.
        if self.state == EngineState::Exited {
            if let Some(reason) = &self.last_error {
                return Err(reason.clone());
            }
        }
        Ok(self.status())
    }

    fn status(&self) -> EngineStatusView {
        let running = matches!(self.state, EngineState::Starting | EngineState::Ready | EngineState::Stopping);
        EngineStatusView {
            state: self.state,
            session_id: self.session_id.clone(),
            pid: self.pid,
            started_at: self.started_at_wall.clone(),
            ready_at: self.ready_at_wall.clone(),
            uptime_ms: if running {
                self.started_instant.map(|t| t.elapsed().as_millis() as u64)
            } else {
                None
            },
            last_error: self.last_error.clone(),
            capabilities: if self.state == EngineState::Ready {
                self.capabilities.clone()
            } else {
                Vec::new()
            },
            versions: if self.state == EngineState::Ready {
                self.versions.clone()
            } else {
                None
            },
        }
    }

    /// `engine_diagnostics` 返回视图（§4.1）。
    fn diagnostics(&self) -> EngineDiagnostics {
        let tail: Vec<String> = {
            let q = self
                .stderr_tail
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let len = q.len();
            let skip = len.saturating_sub(DIAGNOSTICS_TAIL_LINES);
            q.iter().skip(skip).cloned().collect()
        };
        EngineDiagnostics {
            state: self.state,
            session_id: self.session_id.clone(),
            pid: self.pid,
            exit_code: self.exit_code,
            stderr_tail: tail,
            stdout_event_count: self.stdout_event_count.load(Ordering::Relaxed),
            malformed_line_count: self.malformed_line_count.load(Ordering::Relaxed),
            job_object_active: self.job.as_ref().map(|j| j.is_active()).unwrap_or(false),
            started_at: self.started_at_wall.clone(),
            exited_at: self.exited_at_wall.clone(),
        }
    }

    /// 生成新 session_id（§2.3：每次启动/重启都生成新值）。
    fn generate_session_id() -> String {
        format!("session-{}", Uuid::new_v4())
    }

    /// 写一行协议帧到子进程 stdin（单 writer = Supervisor 本身，保证行完整）。
    /// 写失败（管道断裂）由调用方按 §5.2 走 TerminateProcess 兜底。
    fn send_envelope(&mut self, envelope: &serde_json::Value) -> Result<(), String> {
        let stdin = self
            .stdin_client
            .as_mut()
            .ok_or_else(|| "stdin 已关闭（进程不在运行）".to_string())?;
        if envelope.get("session_id").and_then(|v| v.as_str()) != self.session_id.as_deref() {
            return Err("会话已结束".into());
        }
        let mut line = serde_json::to_vec(envelope).map_err(|e| e.to_string())?;
        if line.len() > MAX_FRAME_BYTES { return Err("PROTOCOL_ERROR: 消息超过 2 MiB".into()); }
        line.push(b'\n');
        // Cancel must stay behind the request it cancels. Stop closes the local gate before enqueue.
        let priority = matches!(envelope["command"].as_str(), Some("stop_script" | "ping"))
            || envelope["type"] == "host_response";
        stdin.send(line, priority)
    }

    /// 发送 hello 请求（§2.3 步骤 9，03：第一请求 hello）。
    fn send_hello_request(&mut self) -> Result<(), String> {
        self.request_seq += 1;
        let session_id = self
            .session_id
            .clone()
            .ok_or_else(|| "session_id 未初始化".to_string())?;
        let id = format!("req-{}", self.request_seq);
        let envelope = serde_json::json!({
            "protocol_version": protocol::SUPPORTED_PROTOCOL_VERSION,
            "session_id": session_id,
            "type": "request",
            "id": id,
            "command": "hello",
            "params": {}
        });
        self.send_envelope(&envelope)?;
        self.hello_request_id = Some(id);
        Ok(())
    }

    /// 握手期间处理一条已通过协议解析的 stdout 行（§2.3 步骤 8–10）。
    fn on_handshake_line(&mut self, envelope: ProtocolEnvelope) -> HandshakeStep {
        // 03：session_id 不匹配或缺失 → 记日志并丢弃该行（旧会话消息全部拒绝）
        if Some(envelope.session_id.as_str()) != self.session_id.as_deref() {
            eprintln!(
                "[supervisor] 丢弃跨 session 消息（got={}，want={:?}）",
                envelope.session_id, self.session_id
            );
            return HandshakeStep::Irrelevant;
        }

        if envelope.msg_type == "event" && envelope.name.as_deref() == Some("system_ready") {
            if self.system_ready_seen {
                return HandshakeStep::Irrelevant; // 重复 system_ready：忽略
            }
            self.system_ready_seen = true;
            // 看门狗撤销语义：以 hello 往返仍受同一 30s 启动期限约束（06：启动 30s）
            if let Some(payload) = envelope.payload.as_ref() {
                self.capabilities = protocol::parse_capabilities(payload);
                if let Some(versions) = payload.get("versions").and_then(protocol::parse_versions) {
                    self.versions = Some(versions);
                }
            }
            // 缓存 system_ready 完整信封（契约④：hello 成功后经 Router 事件
            // 总线投递 UI——Router 会重分配 UI seq，不直转）
            self.pending_ready_event = Some(envelope.clone());
            match self.send_hello_request() {
                Ok(()) => HandshakeStep::Continue,
                Err(e) => {
                    // stdin 写失败（管道断裂，§5.2）：走清理兜底
                    let reason = self.eof_failure_reason();
                    eprintln!("[supervisor] hello 请求写入失败（{e}），原因按 {reason} 处理");
                    self.run_cleanup_locked(Some(reason.clone()));
                    self.last_error.get_or_insert(reason.clone());
                    HandshakeStep::Irrelevant
                }
            }
        } else if envelope.msg_type == "response" && self.hello_request_id.as_deref() == envelope.id.as_deref() {
            // hello 响应：成功且数据完整 → Ready（§2.4：system_ready 且 hello 往返成功）
            let data_ok = envelope.success == Some(true)
                && envelope
                    .data
                    .as_ref()
                    .and_then(|d| protocol::parse_versions(d).map(|v| self.versions = Some(v)))
                    .is_some();
            if data_ok {
                if let Some(data) = envelope.data.as_ref() {
                    let caps = protocol::parse_capabilities(data);
                    if !caps.is_empty() {
                        self.capabilities = caps;
                    }
                }
                self.ready_at_wall = Some(rfc3339_now());
                match EngineState::transition(self.state, TransitionEvent::HandshakeCompleted) {
                    Some(next) => self.state = next,
                    None => eprintln!("[supervisor] 非法迁移：HandshakeCompleted 于 {:?}", self.state),
                }
                let ready_event = self.pending_ready_event.take().expect("Ready 必有缓存信封");
                HandshakeStep::Ready(ready_event)
            } else {
                eprintln!("[supervisor] hello 响应非法（success/data 不完整）");
                self.run_cleanup_locked(Some("invalid_hello_response".to_string()));
                HandshakeStep::Irrelevant
            }
        } else {
            // 握手期间的其余消息：只记日志
            eprintln!("[supervisor] 握手期间收到无关消息（type={}）", envelope.msg_type);
            HandshakeStep::Irrelevant
        }
    }

    /// stdout EOF 时的失败原因（§5.2）：进程已退出 → "process_exited: code N"；
    /// 进程仍在（stdout 被关闭）→ "stdout_closed"。
    /// R3.6 竞态微修（TASK-005 设计 §7）：EOF 与 reap 同拍时 try_wait 可能仍返回
    /// None——等待 50ms 重试一次再分类，避免 last_error 瞬时偏差。
    fn eof_failure_reason(&mut self) -> String {
        if let Some(child) = self.child.as_mut() {
            match child.try_wait() {
                Ok(Some(status)) => {
                    return match status.code() {
                        Some(c) => format!("process_exited: code {c}"),
                        None => "process_exited: abnormal termination".to_string(),
                    };
                }
                Ok(None) => {
                    std::thread::sleep(Duration::from_millis(50));
                    return match child.try_wait() {
                        Ok(Some(status)) => match status.code() {
                            Some(c) => format!("process_exited: code {c}"),
                            None => "process_exited: abnormal termination".to_string(),
                        },
                        _ => "stdout_closed".to_string(),
                    };
                }
                Err(e) => return format!("process_exited: io err {e}"),
            }
        }
        "stdout_closed".to_string()
    }

    /// 清理序列（§2.5，状态机 CleanupCompleted 迁移；幂等：cleanup_done 守卫）。
    ///
    /// 调用方必须已持有 Supervisor 锁。顺序固定：
    /// 1. close_gate（骨架 no-op 调用点，M05 实现后在此关闸门并 release_owned_inputs）
    /// 2. 状态 → stopping（此后拒绝新请求，UI 全按钮禁用）
    /// 3. 关闭 stdin（EOF 信号，Python 读循环退出 → 优雅退出）
    /// 4. 2s 优雅期（06：worker 最多 2s 优雅退出后终止）
    /// 5. 未退出 → TerminateProcess（exit code 非 0）
    /// 6. Job Object CloseHandle → KILL_ON_JOB_CLOSE 兜底终止整树
    /// 7. join stdout/stderr drain 线程；记录 exit_code
    fn run_cleanup_locked(&mut self, last_error: Option<String>) {
        if self.cleanup_done {
            // 幂等：应用退出钩子与 engine_stop 可能先后触发同一清理
            if let Some(err) = last_error {
                self.last_error.get_or_insert(err);
            }
            return;
        }
        // 步骤 1：先关闸门（结构性的第一步，§2.5 步骤 2）
        self.gate.close_gate("engine_stopping");
        crate::window::end_session();

        // 步骤 1.5（TASK-005 接线）：Router Shutdown → 在途 ipc 请求全部以
        // ENGINE_UNAVAILABLE 快速失败（§7：sidecar 崩溃/stdout EOF 路径），
        // 且此后 Router 不再向 stdin 写帧（单 writer 让位给 EOF 语义）。
        if let Some(router) = self.router.take() {
            router.shutdown();
        }
        self.router_tx.take();

        // 步骤 2：状态 → stopping（经转换表；非法迁移只记日志不恐慌）
        if let Some(next) = EngineState::transition(self.state, TransitionEvent::StopRequested) {
            self.state = next;
        } else if self.state != EngineState::Stopping {
            eprintln!("[supervisor] 清理：StopRequested 迁移于 {:?} 非法，继续清理", self.state);
        }

        // 步骤 3：关闭 stdin 父端写句柄 → 子进程读端 EOF（修订-01：关闭命名管道
        // client 与原匿名管道关闭语义一致；忽略写路径错误，§5.2 最后一行）
        self.stdin_client.take(); // drop File 即关闭

        // 步骤 4：2s 优雅期，轮询等待进程退出
        let deadline = Instant::now() + Duration::from_secs(GRACEFUL_EXIT_SECS);
        let mut exit_code: Option<i32> = None;
        if let Some(child) = self.child.as_mut() {
            loop {
                match child.try_wait() {
                    Ok(Some(status)) => {
                        exit_code = status.code();
                        break;
                    }
                    Ok(None) => {}
                    Err(_) => break,
                }
                if Instant::now() >= deadline {
                    break;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            // 步骤 5：优雅期超时 → TerminateProcess 兜底（job.rs 集中 unsafe）
            if exit_code.is_none() {
                if job::terminate_process(child, 1).is_ok() {
                    if let Ok(status) = child.wait() {
                        exit_code = status.code();
                    }
                }
            }
        }
        if self.exit_code.is_none() {
            self.exit_code = exit_code;
        }

        // 步骤 6：Job 句柄关闭 → KILL_ON_JOB_CLOSE 整树兜底（含未来 worker）
        if let Some(job) = self.job.take() {
            job.close_kill_tree();
        }

        // 步骤 7：join drain 线程（子进程已终止 → 管道 EOF → 线程自然退出；
        // 发送端用 unbounded mpsc，join 不会死锁）
        if let Some(handle) = self.stdout_join.take() {
            let _ = handle.join();
        }
        if let Some(handle) = self.stderr_join.take() {
            let _ = handle.join();
        }

        // 终态：Exited（终态不复活；迟到消息不改变状态，§5.3）
        self.child.take(); // 已 reap，释放句柄
        match EngineState::transition(self.state, TransitionEvent::CleanupCompleted) {
            Some(EngineState::Exited) => self.state = EngineState::Exited,
            Some(other) => {
                eprintln!("[supervisor] 清理后异常状态 {other:?}，强制 Exited");
                self.state = EngineState::Exited;
            }
            None => self.state = EngineState::Exited,
        }
        self.exited_at_wall = Some(rfc3339_now());
        if let Some(err) = last_error {
            self.last_error = Some(err);
        }
        self.cleanup_done = true;
    }
}

/// 托管句柄：内部 Arc<Mutex<Supervisor>> 串行化全部访问。
/// Clone 为浅拷贝（共享同一 Supervisor；全链自证并发 ipc_request 用）。
#[derive(Clone)]
pub struct SupervisorHandle {
    inner: Arc<Mutex<Supervisor>>,
}

/// 毒锁容忍读取：Mutex 中毒（某次清理 panic）后仍取回数据，保证清理可继续。
fn lock_sup(m: &Mutex<Supervisor>) -> std::sync::MutexGuard<'_, Supervisor> {
    m.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl SupervisorHandle {
    pub fn new() -> Self {
        SupervisorHandle {
            inner: Arc::new(Mutex::new(Supervisor::new())),
        }
    }

    /// `engine_start`（§4.1 幂等语义 + §2.3 启动时序 + §2.4 握手）。
    ///
    /// 契约④：system_ready **不经本方法直转 UI**——握手成功后它作为 Router
    /// 首事件经事件总线投递（UI seq 重分配，见 start 阶段三装配）。
    pub fn start(&self) -> Result<EngineStatusView, String> {
        // 阶段一（持锁）：幂等检查 → 解析命令 → Job Object → spawn → 3 线程
        let (generation, rx) = match self.begin_start() {
            StartOutcome::AlreadyRunning => return Ok(lock_sup(&self.inner).status()),
            StartOutcome::Failed(reason) => return Err(reason),
            StartOutcome::Initiated { generation, rx } => (generation, rx),
        };

        // 阶段二（不持锁）：握手循环。30s 启动期限 = 看门狗 deadline（06：启动 30s）；
        // 独立看门狗线程兜底（begin_start 中启动），此处 deadline 为主动判定。
        let deadline = Instant::now() + Duration::from_secs(STARTUP_WATCHDOG_SECS);
        let mut outcome: Result<(), String> = Ok(());
        let mut ready_envelope: Option<ProtocolEnvelope> = None;
        loop {
            {
                let sup = lock_sup(&self.inner);
                if sup.state != EngineState::Starting || sup.generation != generation {
                    // 被用户 stop / 看门狗清理：状态已非 Starting
                    break;
                }
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                let mut sup = lock_sup(&self.inner);
                if sup.state == EngineState::Starting && sup.generation == generation {
                    sup.run_cleanup_locked(Some("startup_timeout".to_string()));
                    outcome = Err("startup_timeout".to_string());
                }
                break;
            }
            match rx.recv_timeout(remaining) {
                Ok(StdoutMessage::Line(envelope)) => {
                    let mut sup = lock_sup(&self.inner);
                    if sup.state != EngineState::Starting || sup.generation != generation {
                        break;
                    }
                    match sup.on_handshake_line(envelope) {
                        HandshakeStep::Continue | HandshakeStep::Irrelevant => {}
                        HandshakeStep::Ready(envelope) => {
                            // §2.3 步骤 11：hello 往返成功（转发职责移交 Router）
                            ready_envelope = Some(envelope);
                            break;
                        }
                    }
                }
                Ok(StdoutMessage::MalformedOverflow) => {
                    let mut sup = lock_sup(&self.inner);
                    if sup.state == EngineState::Starting && sup.generation == generation {
                        sup.run_cleanup_locked(Some(
                            "protocol_error: 连续畸形帧达阈值".to_string(),
                        ));
                        outcome = Err("protocol_error".to_string());
                    }
                    break;
                }
                Ok(StdoutMessage::Eof) => {
                    let mut sup = lock_sup(&self.inner);
                    if sup.state == EngineState::Starting && sup.generation == generation {
                        let reason = sup.eof_failure_reason();
                        sup.run_cleanup_locked(Some(reason.clone()));
                        outcome = Err(reason);
                    }
                    break;
                }
                Err(RecvTimeoutError::Timeout) => {
                    let mut sup = lock_sup(&self.inner);
                    if sup.state == EngineState::Starting && sup.generation == generation {
                        sup.run_cleanup_locked(Some("startup_timeout".to_string()));
                        outcome = Err("startup_timeout".to_string());
                    }
                    break;
                }
                Err(RecvTimeoutError::Disconnected) => {
                    // 全部发送端消失（drain 线程异常结束）
                    let mut sup = lock_sup(&self.inner);
                    if sup.state == EngineState::Starting && sup.generation == generation {
                        sup.run_cleanup_locked(Some("stdout_closed".to_string()));
                        outcome = Err("stdout_closed".to_string());
                    }
                    break;
                }
            }
        }

        // 阶段三：成功 → 装配 Router（per-session；TASK-005 §2.2/§4.1）+ frame
        // bridge 线程（持续 drain stdout 直到 EOF，R10：drain 不停歇）；
        // 失败/被中止 → 返回当前状态与原因。
        if self.wire_router(generation, ready_envelope) {
            let inner = self.inner.clone();
            std::thread::spawn(move || frame_bridge_loop(inner, generation, rx));
            Ok(lock_sup(&self.inner).status())
        } else {
            outcome?;
            lock_sup(&self.inner).start_result(generation)
        }
    }

    /// Router 装配（阶段三，Ready 后调用）：
    /// - sink = SupervisorFrameSink（单 writer 经 Supervisor 锁；与 HostBridge
    ///   共享同一底层，写入天然串行）；
    /// - gate = NullStopGate（M05 替换位）；
    /// - events = IpcOutlet{EventBus, HostBridge}（契约④：system_ready 经总线
    ///   重分配 UI seq 后到达 UI；host_request 骨架期 CAPABILITY_DENIED）。
    /// 装配后注入 system_ready 为 Router 首事件。
    fn wire_router(&self, generation: u64, ready_envelope: Option<ProtocolEnvelope>) -> bool {
        let mut sup = lock_sup(&self.inner);
        if sup.generation != generation || sup.state != EngineState::Ready {
            return false;
        }
        let session_id = sup.session_id.clone().unwrap_or_default();
        let emit = sup.event_emitter.clone();
        if let Some(old) = sup.router.take() {
            old.shutdown();
        }
        sup.router_tx.take();
        let emit = emit.unwrap_or_else(|| {
            eprintln!(
                "[supervisor] 事件出口未注入（宿主初始化缺 set_event_emitter），事件仅记日志"
            );
            Arc::new(|name: &str, value: &serde_json::Value| {
                eprintln!("[supervisor] [事件未投递] {name}: {value}");
            })
        });
        let now = Instant::now;
        let bus = EventBus::new(
            &session_id,
            emit,
            Arc::new(move || now()),
        );
        let mut bridge = HostBridge::new(Arc::new(Mutex::new(Box::new(SupervisorFrameSink {
            inner: Arc::clone(&self.inner),
        }) as Box<dyn FrameSink>)));
        // TASK-017：把输入宿主命令的真 handler 注册进 HostBridge（M05 接入点）。
        // 未装配 InputManager（如无窗口/测试）→ 不注册 → 命令回 CAPABILITY_DENIED（诚实降级）。
        if let Some(manager) = crate::input::shared_manager() {
            bridge.attach_input(manager);
        }
        if let Some(windows) = crate::window::shared() {
            bridge.register_handler("window_snapshot", Box::new(crate::window::SnapshotHandler(windows)));
        }
        // TASK-017：stop_script 转发前的输入闸门由 NullStopGate 换成共享 InputStopGate
        // （M05：停止必须先由 Rust 关输入闸门，不等待 Python）；未装配则维持 NullStopGate。
        let stop_gate: Box<dyn StopGate> = match crate::input::shared_stop_gate() {
            Some(gate) => Box::new(gate),
            None => Box::new(NullStopGate),
        };
        let router = Router::new(
            &session_id,
            Box::new(SupervisorFrameSink {
                inner: Arc::clone(&self.inner),
            }),
            stop_gate,
            Box::new(SystemClock),
            Box::new(IpcOutlet::new(bus, bridge)),
        );
        // TASK-008：注入 Rust 本地宿主能力 handler（框选三命令；未装配则默认
        // CapabilityDenied 诚实降级）。
        let router = router.with_host_commands(Box::new(crate::window::DesktopCommands {
            selection: crate::selection::shared_manager().map(crate::selection::SharedSelectionManager),
            windows: crate::window::shared(), gate: crate::input::shared_gate(),
        }));
        // 手工装配 spawn_router 等价流程：需要 tx 副本注入 system_ready 首事件
        // （契约④：system_ready 经事件总线 → UI seq=1，不直转）。
        let (tx, rx) = mpsc::channel::<RouterInput>();
        // TASK-008：把当前会话通道交给框选事件出口（selection_* 事件经 eventbus
        // 统一重分配 UI seq）。
        if let Some(sink) = crate::selection::shared_event_sink() {
            sink.attach(tx.clone(), session_id.clone());
        }
        let handle = RouterHandle::new(tx.clone());
        std::thread::spawn(move || {
            let mut router = router;
            router.run(rx)
        });
        // DEF-TASK008-01：装配同步宿主→引擎内部调用通道（框选最终帧登记用）；
        // drain 线程（frame_bridge_loop）在转发 Router 前先行投递其响应，避免
        // Router 线程自锁。会话重建时替换为新会话客户端。
        crate::ipc::hostcall::install(std::sync::Arc::new(
            crate::ipc::hostcall::HostCallClient::new(
                Box::new(SupervisorFrameSink {
                    inner: Arc::clone(&self.inner),
                }),
                session_id.clone(),
            ),
        ));
        sup.router = Some(handle);
        if let Some(windows)=crate::window::shared() {
            crate::window::watch(windows,session_id.clone(),tx.clone());
        }
        sup.router_tx = Some(tx.clone());
        if let Some(envelope) = ready_envelope {
            let _ = tx.send(RouterInput::Frame(envelope));
        }
        true
    }

    /// 阶段一实现（持锁调用）。失败路径统一落 `Exited(last_error=spawn_failed: ...)`。
    fn begin_start(&self) -> StartOutcome {
        let mut sup = lock_sup(&self.inner);
        match sup.state {
            EngineState::Starting | EngineState::Ready | EngineState::Stopping => {
                return StartOutcome::AlreadyRunning
            }
            EngineState::Idle | EngineState::Exited => {}
        }

        // 重置运行期状态（重启 = 全新会话；06：启动每次使用新 session_id）
        sup.capabilities.clear();
        sup.versions = None;
        sup.last_error = None;
        sup.exit_code = None;
        sup.started_at_wall = None;
        sup.ready_at_wall = None;
        sup.exited_at_wall = None;
        sup.started_instant = None;
        sup.pid = None;
        sup.request_seq = 0;
        sup.cleanup_done = false;
        sup.hello_request_id = None;
        sup.system_ready_seen = false;
        sup.pending_ready_event = None;
        sup.stdout_event_count.store(0, Ordering::Relaxed);
        sup.malformed_line_count.store(0, Ordering::Relaxed);
        // 旧会话 Router 残余（防御：正常路径 cleanup 已 Shutdown/take）
        if let Some(old) = sup.router.take() {
            old.shutdown();
        }
        sup.router_tx.take();
        sup.stderr_tail
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .clear();

        if crate::desktop_windows::attached() {
            crate::window::new_session();
            let input=crate::input::wiring::build_production_manager();
            input.lock().unwrap_or_else(|p|p.into_inner()).gate().enable_input(false);
            crate::input::install_manager(input);
        }
        let session_id = Supervisor::generate_session_id();
        sup.session_id = Some(session_id.clone());

        // §2.6：解析 sidecar 命令（存在性检查；失败 → Exited(spawn_failed)）
        let (program, args) = match resolve_sidecar_command(&session_id) {
            Ok(pair) => pair,
            Err(e) => {
                sup.state = EngineState::Exited;
                sup.last_error = Some(format!("spawn_failed: {e}"));
                sup.exited_at_wall = Some(rfc3339_now());
                return StartOutcome::Failed(e);
            }
        };

        // 状态迁移：Idle/Exited → Starting（转换表权威）
        sup.state = EngineState::transition(sup.state, TransitionEvent::StartRequested)
            .expect("Idle/Exited → Starting 必为合法迁移");
        sup.started_at_wall = Some(rfc3339_now());
        sup.started_instant = Some(Instant::now());
        sup.generation += 1;
        let generation = sup.generation;

        // Job Object（§2.2 步骤 4）
        let job = match job::JobObject::new() {
            Ok(j) => j,
            Err(e) => {
                sup.state = EngineState::Exited;
                sup.last_error = Some(format!("spawn_failed: {e}"));
                sup.exited_at_wall = Some(rfc3339_now());
                return StartOutcome::Failed(e);
            }
        };

        // stdio 传输层（设计修订-01 分支 A：命名管道；句柄继承需在 spawn 时快照，
        // 故必须先于 spawn 建立）。失败 → Exited(pipe_setup_failed)。
        let mut pipes = match transport::SessionPipes::build(&session_id) {
            Ok(p) => p,
            Err(e) => {
                sup.state = EngineState::Exited;
                sup.last_error = Some(format!("pipe_setup_failed: {e}"));
                sup.exited_at_wall = Some(rfc3339_now());
                return StartOutcome::Failed(e);
            }
        };
        let child_stdio = match pipes.take_child_stdio() {
            Ok(s) => s,
            Err(e) => {
                sup.state = EngineState::Exited;
                sup.last_error = Some(format!("pipe_setup_failed: {e}"));
                sup.exited_at_wall = Some(rfc3339_now());
                return StartOutcome::Failed(e);
            }
        };

        // 参数数组 spawn（§2.3 步骤 5：无 shell，03：非用户控制字符串拼 shell）。
        // cwd = 仓库根（B1 发现：backend.ipc 包导入依赖仓库根 sys.path；main.py
        // 已有引导逻辑，此处双保险——B3 接线约定）。
        #[cfg(all(windows, debug_assertions))]
        let mut cmd = development_venv_command(&program).unwrap_or_else(|| Command::new(&program));
        #[cfg(not(all(windows, debug_assertions)))]
        let mut cmd = Command::new(&program);
        cmd.args(&args)
            .current_dir(sidecar_working_dir())
            .stdin(child_stdio.stdin)
            .stdout(child_stdio.stdout)
            .stderr(child_stdio.stderr);
        // TASK-009：注入导入 token 密钥（仅当已安装；测试/未装配时不注入，行为不变）。
        if let Some(secret_hex) = crate::import::shared_secret_hex() {
            cmd.env("ST_IMPORT_TOKEN_SECRET", secret_hex);
        }
        // DEF-TASK008-01：注入 host-frame token 密钥（同上；未装配不注入）。
        if let Some(secret_hex) = crate::selection::hostframe::shared_secret_hex() {
            cmd.env("ST_HOSTFRAME_TOKEN_SECRET", secret_hex);
        }
        // TASK-017：输入能力置位信号——仅当已装配 InputManager 时注入
        // （Python 侧据此把 hello.capabilities 的 input 置 available，否则维持
        // unavailable + reason，诚实降级）。
        if crate::window::shared().is_some() { cmd.env("ST_WINDOW_AVAILABLE","1"); }
        if crate::input::shared_manager().is_some() {
            cmd.env("ST_INPUT_AVAILABLE", "1");
            if crate::desktop_windows::attached() {cmd.env("ST_INPUT_MANAGED","1");}
        }
        #[cfg(target_os = "windows")]
        {
            // CREATE_NO_WINDOW：不弹控制台窗口（Win32 进程创建标志）
            use std::os::windows::process::CommandExt;
            use windows::Win32::System::Threading::CREATE_NO_WINDOW;
            cmd.creation_flags(CREATE_NO_WINDOW.0);
        }
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                sup.state = EngineState::Exited;
                sup.last_error = Some(format!("spawn_failed: {e}"));
                sup.exited_at_wall = Some(rfc3339_now());
                return StartOutcome::Failed(format!("{e}"));
            }
        };

        // spawn 成功 → 立即落入 Job（§2.2 步骤 6；失败则杀掉后报 spawn_failed）
        if let Err(e) = job.assign(&child) {
            let _ = child.kill();
            let _ = child.wait();
            sup.state = EngineState::Exited;
            sup.last_error = Some(format!("spawn_failed: {e}"));
            sup.exited_at_wall = Some(rfc3339_now());
            return StartOutcome::Failed(e);
        }

        let pid = child.id();
        sup.pid = Some(pid);

        // 确认命名管道连接状态（client 在 CreateFileW 时已连接 → 535 正常路径；
        // 其他错误按 pipe_setup_failed 清理，修订-01）。失败路径先杀子进程再落状态。
        if let Err(e) = pipes.confirm_connections() {
            let _ = child.kill();
            let _ = child.wait();
            sup.state = EngineState::Exited;
            sup.last_error = Some(format!("pipe_setup_failed: {e}"));
            sup.exited_at_wall = Some(rfc3339_now());
            return StartOutcome::Failed(e);
        }

        sup.child = Some(child);
        sup.job = Some(job);

        // 分发父端句柄：stdin 写端归 Supervisor（单 writer），读端移交 drain 线程
        let stdin_client = pipes.take_stdin_client().expect("stdin client 未被消费");
        let stdout_server = pipes.take_stdout_server().expect("stdout server 未被消费");
        let stderr_server = pipes.take_stderr_server().expect("stderr server 未被消费");
        let (tx, rx) = mpsc::channel::<StdoutMessage>();
        let writer_error = tx.clone();
        sup.stdin_client = Some(writer::SessionWriter::new(stdin_client, move || {
            let _ = writer_error.send(StdoutMessage::Eof);
        }));
        sup.stdout_pipe = Some(stdout_server);
        sup.stderr_pipe = Some(stderr_server);
        drop(pipes); // 剩余未消费句柄关闭（应已全部取走）

        // stderr drain 线程：持续读至 EOF，每行入 500 行环形缓冲
        // （07 §4：stderr 必须持续 drain，避免管道满死锁；不转发 UI，S06）
        let stderr_source = sup.stderr_pipe.take().expect("stderr pipe 刚写入");
        let tail = sup.stderr_tail.clone();
        sup.stderr_join = Some(std::thread::spawn(move || {
            let mut reader = BufReader::new(stderr_source);
            // BUG-6：UTF-8 畸形行不视为 EOF——以 lossy 预览记日志后继续读；
            // 仅真实 IO 错误（进程死亡/清理）终止循环。
            let _ = for_each_line_lossy(&mut reader, |line| {
                match line {
                    Ok(l) => {
                        let mut q = tail.lock().unwrap_or_else(|p| p.into_inner());
                        q.push_back(l.to_string());
                        while q.len() > STDERR_TAIL_CAPACITY {
                            q.pop_front();
                        }
                    }
                    Err(n) => {
                        eprintln!("[supervisor] 畸形 stderr 字节流（非 UTF-8，{n} 字节），已跳过");
                    }
                }
                true
            });
        }));

        // stdout drain 线程：持续解析 NDJSON 投递 Router（TASK-005 接线）。
        // - UTF-8 畸形/JSON 畸形/超限帧：malformed_line_count++ + 连续畸形
        //   计数（合法帧清零）；连续达 CONSECUTIVE_MALFORMED_LIMIT → 停止
        //   读取并发 MalformedOverflow（§6.5：关闸门+关会话）；
        // - 行字节 >2MiB：不解析、计数、PROTOCOL_ERROR 日志（§6.5 接收侧）；
        // - 有效行经 unbounded channel 送握手/bridge 线程（永不阻塞发送）；
        //   发送端仅存活于 drain 线程内：接收端退出后 recv Disconnected 兜底。
        let tx_for_thread = tx.clone();
        let stdout_count = sup.stdout_event_count.clone();
        let malformed_count = sup.malformed_line_count.clone();
        let session_for_thread = sup.session_id.clone().unwrap_or_default();
        let stdout_source = sup.stdout_pipe.take().expect("stdout pipe 刚写入");
        sup.stdout_join = Some(std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout_source);
            let mut consecutive_malformed: usize = 0;
            let mut overflow = false;
            // BUG-6：for_each_line_lossy 区分 UTF-8 畸形（计数+日志，不断流）
            // 与真实 IO 错误（终止循环，EOF 判定语义不变）。
            let read_result = for_each_line_lossy(&mut reader, |line| {
                // 畸形判定（共用收尾）：计数 + 连续计数 + 达阈值提前终止
                macro_rules! malformed {
                    ($log:expr) => {{
                        malformed_count.fetch_add(1, Ordering::Relaxed);
                        consecutive_malformed += 1;
                        eprintln!("{}", $log);
                        if consecutive_malformed >= CONSECUTIVE_MALFORMED_LIMIT {
                            overflow = true;
                            return false; // 停止读取；MalformedOverflow 由收尾发送
                        }
                        true
                    }};
                }
                match line {
                    Ok(raw) => {
                        if raw.is_empty() {
                            return true; // 空行不计畸形也不计数
                        }
                        // §6.5 接收侧：行字节 >2MiB → 不解析、丢行、PROTOCOL_ERROR
                        if raw.len() > MAX_FRAME_BYTES {
                            return malformed!(format!(
                                "[supervisor] PROTOCOL_ERROR: stdout 行 {} 字节超 2MiB 上限，已丢弃",
                                raw.len()
                            ));
                        }
                        match classify_stdout_line(raw) {
                            StdoutLineVerdict::Frame(envelope) => {
                                consecutive_malformed = 0; // 合法帧清零（§6.5）
                                if envelope.session_id == session_for_thread {
                                    stdout_count.fetch_add(1, Ordering::Relaxed);
                                }
                                // 接收端已消失（清理完成）→ 提前终止读取（原语义）
                                tx_for_thread.send(StdoutMessage::Line(envelope)).is_ok()
                            }
                            StdoutLineVerdict::DropSessionLine => {
                                // 契约③：丢弃+日志、不计畸形；结构其余合法视同
                                // 合法帧清零连续计数（与 Python 对称）
                                consecutive_malformed = 0;
                                eprintln!(
                                    "[supervisor] 丢弃 session_id 缺失/非法的 stdout 行（不计畸形）: {raw:.120}"
                                );
                                true
                            }
                            StdoutLineVerdict::Malformed(log) => malformed!(log),
                        }
                    }
                    Err(n) if n > MAX_FRAME_BYTES => malformed!(format!(
                        "[supervisor] PROTOCOL_ERROR: stdout 行 {n} 字节超 2MiB 上限，已丢弃"
                    )),
                    Err(n) => malformed!(format!(
                        "[supervisor] 畸形 stdout 字节流（非 UTF-8，{n} 字节），已跳过"
                    )),
                }
            });
            let _ = read_result; // 真实 IO 错误与 EOF 同样以 Eof 收尾（原语义）
            let final_msg = if overflow {
                StdoutMessage::MalformedOverflow
            } else {
                StdoutMessage::Eof
            };
            let _ = tx_for_thread.send(final_msg);
        }));
        drop(tx);

        // 30s 启动看门狗线程（§2.3 步骤 7；代数守卫防止误杀新一轮启动）
        let watchdog = Arc::downgrade(&self.inner);
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(STARTUP_WATCHDOG_SECS));
            if let Some(arc) = watchdog.upgrade() {
                let mut sup = lock_sup(&arc);
                if sup.state == EngineState::Starting && sup.generation == generation && !sup.cleanup_done {
                    eprintln!("[supervisor] 看门狗触发：30s 内未完成启动握手");
                    sup.run_cleanup_locked(Some("startup_timeout".to_string()));
                }
            }
        });

        StartOutcome::Initiated { generation, rx }
    }

    /// `engine_stop`（§4.1 幂等：非运行态直接返回当前状态）。
    pub fn stop(&self, reason: &str) -> EngineStatusView {
        let mut sup = lock_sup(&self.inner);
        match sup.state {
            EngineState::Starting | EngineState::Ready => {
                sup.run_cleanup_locked(None);
            }
            _ => {}
        }
        let _ = reason; // 骨架仅 engine_stopping 一种 reason；TASK-005 扩展
        sup.status()
    }

    /// `engine_status`。
    pub fn update_input_capability(&self,session:&str,enabled:bool,reason:Option<String>){
        let mut sup=lock_sup(&self.inner);
        if sup.session_id.as_deref()!=Some(session){return;}
        if let Some(cap)=sup.capabilities.iter_mut().find(|c|c.id=="input") {
            cap.state=if enabled {"available"} else {"unavailable"}.into(); cap.reason=reason;
        }
    }

    pub fn status(&self) -> EngineStatusView {
        lock_sup(&self.inner).status()
    }

    /// `engine_diagnostics`。
    pub fn diagnostics(&self) -> EngineDiagnostics {
        lock_sup(&self.inner).diagnostics()
    }

    /// 应用退出同步清理（§2.5 最后一步：RunEvent::Exit 完成前执行，幂等）。
    pub fn shutdown_sync(&self, reason: &str) {
        let mut sup = lock_sup(&self.inner);
        match sup.state {
            EngineState::Starting | EngineState::Ready => {
                sup.run_cleanup_locked(None);
            }
            EngineState::Stopping => {
                // 上一次清理未完成（理论上不可能：run_cleanup 同步完成）——兜底
                sup.run_cleanup_locked(None);
            }
            EngineState::Idle | EngineState::Exited => {
                // 从未启动或已清理：no-op（幂等），仅记录原因供诊断
                let _ = reason;
            }
        }
    }
}

impl Default for SupervisorHandle {
    fn default() -> Self {
        Self::new()
    }
}

impl SupervisorHandle {
    /// `ipc_request`（TASK-005 §5.1）：经当前会话 Router 投递（阻塞至响应/
    /// 超时；在 UI 线程外调用）。引擎非 Ready（Router 未装配或已随
    /// 清理 Shutdown）→ ENGINE_UNAVAILABLE 快速失败（retryable=true）。
    pub fn ipc_request(
        &self,
        id: &str,
        command: &str,
        params: serde_json::Value,
    ) -> Result<serde_json::Value, crate::ipc::IpcError> {
        self.session_router(None)?.request(id, command, params)
    }

    /// `ipc_cancel`（TASK-005 §5.1）：幂等；data = {cancelled, reason}（03）。
    pub fn ipc_cancel(
        &self,
        request_id: &str,
    ) -> Result<serde_json::Value, crate::ipc::IpcError> {
        self.session_router(None)?.cancel(request_id)
    }

    /// Bind the request to the same router while checking its session under the lock.
    pub(crate) fn session_router(&self, expected: Option<&str>) -> Result<RouterHandle, crate::ipc::IpcError> {
        let supervisor = lock_sup(&self.inner);
        if expected.is_some() && expected != supervisor.session_id.as_deref() {
            return Err(crate::ipc::IpcError::engine_unavailable("请求所属的引擎会话已结束"));
        }
        supervisor.router.clone().ok_or_else(|| crate::ipc::IpcError::engine_unavailable("引擎未运行（Router 未装配）"))
    }

    /// 注入 UI 事件出口（桌面入口初始化时调用一次；事件名 = 协议 name，payload = 重分配 seq 后完整信封）。
    pub fn set_event_emitter(&self, emitter: crate::ipc::eventbus::EmitFn) {
        lock_sup(&self.inner).event_emitter = Some(emitter);
    }
}

/// sidecar 子进程工作目录（B3 接线约定：spawn 时显式 cwd=仓库根——B1 发现
/// `python backend/main.py` 直接执行时 `backend.ipc` 包导入依赖仓库根
/// sys.path；main.py 已有引导逻辑，此处双保险）。打包态用 exe 目录。
fn sidecar_working_dir() -> PathBuf {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    #[cfg(debug_assertions)]
    {
        manifest.join("../..")
    }
    #[cfg(not(debug_assertions))]
    {
        std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.to_path_buf()))
            .unwrap_or(manifest)
    }
}

/// frame bridge 线程（TASK-005 接线：替代 TASK-004 的 post-ready 泵线程）：
/// 持续消费 stdout 消息直到 EOF（R10：drain 不停歇），有效帧改投 Router
/// 命令通道（RouterInput::Frame）；EOF / 连续畸形溢出 → 既有清理序列
/// （§5.2：stdout 断流不自动重启；§6.5：连续畸形关会话）。
fn frame_bridge_loop(inner: Arc<Mutex<Supervisor>>, generation: u64, rx: Receiver<StdoutMessage>) {
    while let Ok(msg) = rx.recv() {
        let mut sup = lock_sup(&inner);
        if sup.generation != generation || sup.state != EngineState::Ready {
            break;
        }
        match msg {
            StdoutMessage::Line(envelope) => {
                // DEF-TASK008-01：同步内部调用通道优先消化其响应帧（不经 Router，
                // 从而避免「Router 线程在 handler 内阻塞等待响应」的自锁）。
                // 未命中（非本通道）→ 走常规 Router 转发。
                if crate::ipc::hostcall::deliver(&envelope) {
                    continue;
                }
                // 投递 Router；发送失败 = Router 已 Shutdown（清理完成）→
                // 迟到帧只丢弃（§5.3：停止期间迟到消息不复活状态）
                if let Some(tx) = &sup.router_tx {
                    let _ = tx.send(RouterInput::Frame(envelope));
                }
            }
            StdoutMessage::Eof => {
                let reason = sup.eof_failure_reason();
                eprintln!("[supervisor] stdout 断流（{reason}），执行清理序列");
                sup.run_cleanup_locked(Some(reason));
                break;
            }
            StdoutMessage::MalformedOverflow => {
                eprintln!("[supervisor] 连续畸形帧达 {} 阈值，关闸门并关闭会话（§6.5）", CONSECUTIVE_MALFORMED_LIMIT);
                sup.run_cleanup_locked(Some("protocol_error: 连续畸形帧达阈值".to_string()));
                break;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// 数据根解析与进程参数（TASK-006 设计 §2.5）
// ---------------------------------------------------------------------------

/// release 态应用数据目录注入点（桌面入口初始化时调用；dev 态不注入，
/// 走 LOCALAPPDATA 默认分支）。OnceLock 传递，避免 Supervisor/resolve 依赖窗口框架。
static APP_LOCAL_DATA_DIR: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();

/// 注入 release 数据根（幂等：仅首次生效；测试路径不调用）。
pub fn set_app_local_data_dir(dir: PathBuf) {
    let _ = APP_LOCAL_DATA_DIR.set(dir);
}

/// 数据根解析纯函数（可测替身：env 值/注入根/编译分支均参数化）。
///
/// 优先级：`SIDECAR_DATA_DIR` 环境变量覆盖（QA/测试隔离）> app_local_data 注入
/// （release 由桌面入口提供）> 默认：dev（debug_assertions）=
/// `%LOCALAPPDATA%/ScreenTrigger-dev/`（隔离，绝不写仓库当真实用户数据）；
/// release 兜底 = `%LOCALAPPDATA%/ScreenTrigger/`。
pub fn resolve_data_dir_from(
    env_override: Option<&str>,
    app_local_data: Option<&std::path::Path>,
    debug_build: bool,
) -> PathBuf {
    if let Some(dir) = env_override {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir);
        }
    }
    if let Some(base) = app_local_data {
        return base.to_path_buf();
    }
    localappdata_root().join(if debug_build {
        "ScreenTrigger-dev"
    } else {
        "ScreenTrigger"
    })
}

/// 运行时数据根解析（OnceLock/env/编译分支组装；`resolve_data_dir_from` 纯逻辑）。
pub fn resolve_data_dir() -> PathBuf {
    resolve_data_dir_from(
        std::env::var("SIDECAR_DATA_DIR").ok().as_deref(),
        APP_LOCAL_DATA_DIR.get().map(|p| p.as_path()),
        cfg!(debug_assertions),
    )
}

/// `%LOCALAPPDATA%` 根（缺失时 USERPROFILE 推导；再缺省退回临时目录兜底）。
fn localappdata_root() -> PathBuf {
    if let Ok(v) = std::env::var("LOCALAPPDATA") {
        if !v.trim().is_empty() {
            return PathBuf::from(v);
        }
    }
    if let Ok(v) = std::env::var("USERPROFILE") {
        return PathBuf::from(v).join("AppData").join("Local");
    }
    std::env::temp_dir()
}

// ---------------------------------------------------------------------------
// 资源路径策略（§2.6）：开发态 vs 打包态；单一函数 + 编译期分支
// ---------------------------------------------------------------------------

/// 解析 sidecar 启动命令（§2.6）。
///
/// - 开发态：`SIDECAR_PYTHON` 环境变量优先；未设置时默认
///   `<仓库根>/.venv/Scripts/python.exe`；脚本为 `<仓库根>/backend/main.py`。
/// - 打包态：`<资源目录>/binaries/screentrigger-engine-*.exe`（PyInstaller 产物，
///   TASK-014 接入）；未找到时返回明确错误，**不回退系统 python**（07 §4）。
///
/// 返回（程序路径，参数数组——`--session-id` 与 `--data-dir`（TASK-006 §2.5，
/// Python 侧必填）已注入）。
pub fn resolve_sidecar_command(session_id: &str) -> Result<(PathBuf, Vec<String>), String> {
    let data_dir = resolve_data_dir();
    #[cfg(debug_assertions)]
    {
        let repo_root = sidecar_working_dir();
        let python = dev_python_path(&repo_root, std::env::var_os("SIDECAR_PYTHON").map(PathBuf::from))?;
        let script = repo_root.join("backend").join("main.py");
        if !script.is_file() {
            return Err(format!(
                "sidecar 脚本不存在（探测路径：{}）",
                script.display()
            ));
        }
        // 故障注入（TEST-016 场景触发路径，§6.2/§6.4）：仅 debug 构建读取
        // SIDECAR_FAULT_SCENARIO；合法枚举追加 --fault-scenario 参数。
        let fault = read_fault_scenario_override();
        Ok((
            python,
            build_sidecar_args(&script, session_id, fault.as_deref(), &data_dir),
        ))
    }
    #[cfg(not(debug_assertions))]
    {
        let exe_dir = std::env::current_exe()
            .map_err(|e| format!("无法定位当前可执行目录: {e}"))?
            .parent()
            .ok_or("无法定位当前可执行目录")?
            .to_path_buf();
        let sidecar = exe_dir.join("resources").join("binaries").join(PACKAGED_SIDECAR_NAME);
        if !sidecar.is_file() {
            return Err(format!(
                "打包态 sidecar 尚未构建（期望路径：{}）——不回退系统 python（docs/07 §4）",
                sidecar.display()
            ));
        }
        // 打包态不读取 SIDECAR_FAULT_SCENARIO（§4.2：--fault-scenario 仅测试路径使用）；
        // PyInstaller exe 不带脚本路径参数，直接注入 --session-id + --data-dir
        let mut args = vec![
            "--session-id".to_string(),
            session_id.to_string(),
            "--data-dir".to_string(),
            data_dir.to_string_lossy().to_string(),
        ];
        args.shrink_to_fit();
        Ok((sidecar, args))
    }
}

// ---------------------------------------------------------------------------
// 故障注入参数构造（TEST-016 触发路径；§6.4 mock 边界：只诱导故障，不制造成功假象）
// ---------------------------------------------------------------------------

#[cfg(any(debug_assertions, test))]
fn dev_python_path(root: &std::path::Path, override_path: Option<PathBuf>) -> Result<PathBuf, String> {
    let python = override_path.unwrap_or_else(|| root.join(".venv/Scripts/python.exe"));
    if python.is_file() {
        Ok(python)
    } else {
        Err(format!("开发态 sidecar 解释器不存在（探测路径：{}；可设 SIDECAR_PYTHON 环境变量覆盖）", python.display()))
    }
}

/// The CPython Windows venv redirector keeps inherited pipe handles until its child exits.
/// Start the same interpreter with the same venv identity, so the host sees a real stdout EOF.
#[cfg(all(windows, debug_assertions))]
fn development_venv_command(program: &std::path::Path) -> Option<Command> {
    let launcher = std::path::absolute(program).ok()?;
    let scripts = launcher.parent()?;
    if !launcher.file_name()?.to_str()?.eq_ignore_ascii_case("python.exe")
        || !scripts.file_name()?.to_str()?.eq_ignore_ascii_case("Scripts") {
        return None;
    }
    let config = std::fs::read_to_string(scripts.parent()?.join("pyvenv.cfg")).ok()?;
    let home = config.lines().filter_map(|line| line.split_once('='))
        .find(|(key, _)| key.trim().eq_ignore_ascii_case("home"))?.1.trim();
    let home = std::path::Path::new(home);
    let base = home.join("python.exe");
    if !home.is_absolute() || !base.is_file() {
        return None;
    }
    // Preserve custom executables and explicit interpreter overrides. Only unwrap CPython's own launcher.
    let template = home.join("Lib/venv/scripts/nt/venvlauncher.exe");
    if std::fs::read(template).ok()? != std::fs::read(&launcher).ok()? {
        return None;
    }
    let mut command = Command::new(base);
    command.env("__PYVENV_LAUNCHER__", launcher);
    Some(command)
}

/// `--fault-scenario` 合法枚举（与 docs §4.2 / backend/main.py FAULT_SCENARIOS 逐字对齐）。
#[cfg_attr(not(debug_assertions), allow(dead_code))]
const FAULT_SCENARIOS: [&str; 6] = [
    "crash_on_start",
    "silent_no_ready",
    "stderr_flood",
    "stdout_close_after_ready",
    "exit_after_ready",
    "stdout_garbage",
];

/// 校验故障注入枚举值是否合法。
#[cfg_attr(not(debug_assertions), allow(dead_code))]
fn is_valid_fault_scenario(value: &str) -> bool {
    FAULT_SCENARIOS.contains(&value)
}

/// 读取故障注入环境变量 `SIDECAR_FAULT_SCENARIO`。
///
/// **仅 `#[cfg(debug_assertions)]`（debug 构建）生效**：release 构建直接返回
/// None，不读取该变量——对齐设计 §6.4 mock 边界（--fault-scenario 仅测试
/// 路径使用，不得用于绕过依赖探针或伪造正常路径成功）。非法值忽略并记
/// stderr 日志，不 crash 宿主。
#[cfg_attr(not(debug_assertions), allow(dead_code))]
fn read_fault_scenario_override() -> Option<String> {
    #[cfg(debug_assertions)]
    {
        match std::env::var("SIDECAR_FAULT_SCENARIO") {
            Ok(value) if !value.is_empty() => {
                if is_valid_fault_scenario(&value) {
                    Some(value)
                } else {
                    eprintln!(
                        "[supervisor] 忽略非法 SIDECAR_FAULT_SCENARIO: {value}（合法枚举：{}）",
                        FAULT_SCENARIOS.join("/")
                    );
                    None
                }
            }
            _ => None,
        }
    }
    #[cfg(not(debug_assertions))]
    {
        None
    }
}

/// 构造 sidecar 启动参数数组（纯函数，便于单测；§4.2 顺序：
/// `<脚本路径> --session-id <id> [--fault-scenario <n>]`）。
///
/// `fault` 传入前已经过 `is_valid_fault_scenario` 校验（由
/// `read_fault_scenario_override` 完成过滤），故此处只做追加。
#[cfg_attr(not(debug_assertions), allow(dead_code))]
/// sidecar 参数构造（开发态）：脚本路径 → --session-id → --data-dir（TASK-006
/// §2.5，Python 必填）→ --fault-scenario（可选）。`data_dir` 由调用方经
/// `resolve_data_dir()` 解析后注入（测试直接传替身路径）。
fn build_sidecar_args(
    script: &std::path::Path,
    session_id: &str,
    fault: Option<&str>,
    data_dir: &std::path::Path,
) -> Vec<String> {
    let mut args = vec![
        script.to_string_lossy().to_string(),
        "--session-id".to_string(),
        session_id.to_string(),
        "--data-dir".to_string(),
        data_dir.to_string_lossy().to_string(),
    ];
    if let Some(scenario) = fault {
        args.push("--fault-scenario".to_string());
        args.push(scenario.to_string());
    }
    args
}

// ---------------------------------------------------------------------------
// RFC3339 UTC 时间戳（仅 std：无 chrono/time 依赖，§7.2 依赖清单受限）
// ---------------------------------------------------------------------------

/// 当前 UTC 时间的 RFC3339 字符串（毫秒精度，如 `2026-10-09T12:00:00.123Z`）。
pub fn rfc3339_now() -> String {
    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    format_rfc3339(dur.as_secs(), dur.subsec_millis())
}

/// Unix 秒 + 毫秒 → RFC3339 UTC（毫秒精度）。
fn format_rfc3339(secs: u64, millis: u32) -> String {
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (h, m, s) = (rem / 3_600, (rem % 3_600) / 60, rem % 60);
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02}T{h:02}:{m:02}:{s:02}.{millis:03}Z")
}

/// 天数（自 1970-01-01）→ (年, 月, 日)。Howard Hinnant civil_from_days 算法。
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

// ---------------------------------------------------------------------------
// 单元测试（§6.3：mod.rs 状态机全路径转换表、session_id 格式；
// Job Object 真实 OS 行为以 §6.1/§6.2 隔离窗口集成验收为准，不以 mock 证明）
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;
    use crate::sidecar::protocol::ProtocolParseError;
    use std::io::Cursor;

    // ---- BUG-6：for_each_line_lossy 畸形数据语义（§5.2/§6.2）----

    #[test]
    fn for_each_line_lossy_counts_malformed_and_continues() {
        // 非 ASCII 畸形字节（\xff\xfe）夹在合法行之间：旧实现把 InvalidData
        // 当 EOF 丢失后续行；修复后计数+日志且后续行继续送达。
        let input: &[u8] = b"line1\n\xff\xfe\nline2\r\n\nlast";
        let mut valid: Vec<String> = Vec::new();
        let mut malformed: Vec<usize> = Vec::new();
        let result = for_each_line_lossy(&mut Cursor::new(input), |line| match line {
            Ok(l) => {
                valid.push(l.to_string());
                true
            }
            Err(n) => {
                malformed.push(n);
                true
            }
        });
        assert!(result.is_ok(), "EOF 应正常返回");
        assert_eq!(valid, vec!["line1", "line2", "", "last"], "后续合法行不得丢失");
        assert_eq!(malformed, vec![2], "畸形段按字节计数一次");
    }

    #[test]
    fn for_each_line_lossy_empty_input_is_eof() {
        let mut valid: Vec<String> = Vec::new();
        let result = for_each_line_lossy(&mut Cursor::new(b"" as &[u8]), |line| {
            if let Ok(l) = line {
                valid.push(l.to_string());
            }
            true
        });
        assert!(result.is_ok());
        assert!(valid.is_empty(), "空输入无行回调");
    }

    #[test]
    fn for_each_line_lossy_strips_crlf_and_handles_no_trailing_newline() {
        // 行尾 \r\n / 连续 \r\n 剥离；行中间 \r 保留（与旧 lines() 一致）；
        // 末行无换行符也正常送达
        let input: &[u8] = b"a\r\nb\r\r\nc\r-x\nd";
        let mut valid: Vec<String> = Vec::new();
        for_each_line_lossy(&mut Cursor::new(input), |line| {
            if let Ok(l) = line {
                valid.push(l.to_string());
            }
            true
        })
        .expect("读取应成功");
        assert_eq!(valid, vec!["a", "b", "c\r-x", "d"]);
    }

    // ---- TASK-005 B5：stdout 行分类（契约③ drop-missing-session-id 双端对称）----

    #[test]
    fn classify_missing_session_id_drops_without_malformed() {
        // 仅缺 session_id：丢弃不计畸形（schema 用例 drop-missing-session-id：
        // counted=false，与 Python parse_frame 不过 session_id 的观测行为对称）
        let line = r#"{"protocol_version":1,"type":"event","seq":1,"name":"system_ready","timestamp":"2026-10-09T00:00:00Z"}"#;
        assert!(matches!(
            classify_stdout_line(line),
            StdoutLineVerdict::DropSessionLine
        ));
    }

    #[test]
    fn classify_missing_session_plus_missing_id_counts_malformed() {
        // 缺 session_id 且缺类型必填 id：畸形判定与 Python 一致（missing_field:id，
        // Python 先查类型必填集才轮到 session verify 丢弃）
        let line = r#"{"protocol_version":1,"type":"request","command":"ping"}"#;
        match classify_stdout_line(line) {
            StdoutLineVerdict::Malformed(log) => {
                assert!(log.contains("id"), "错误码应对齐 Python missing_field:id：{log}");
            }
            other => panic!("应计为畸形，实际 {other:?}"),
        }
    }

    #[test]
    fn classify_invalid_or_non_string_session_id_drops() {
        // 非法格式/非字符串 session_id：Python verify 比对不中 → 丢弃不计数，
        // Rust 同口径（DropSessionLine）
        assert!(matches!(
            classify_stdout_line(
                r#"{"protocol_version":1,"session_id":"坏!id","type":"event","seq":1,"name":"e","timestamp":"t"}"#
            ),
            StdoutLineVerdict::DropSessionLine
        ));
        assert!(matches!(
            classify_stdout_line(
                r#"{"protocol_version":1,"session_id":42,"type":"event","seq":1,"name":"e","timestamp":"t"}"#
            ),
            StdoutLineVerdict::DropSessionLine
        ));
    }

    #[test]
    fn classify_valid_frame_and_generic_malformed_unchanged() {
        // 合法帧照常 Frame；其余畸形（缺通用必填）照常计数（回归保护）
        let ok = format!(
            r#"{{"protocol_version":1,"session_id":"{}","type":"event","seq":1,"name":"system_ready","timestamp":"2026-10-09T00:00:00Z"}}"#,
            Supervisor::generate_session_id()
        );
        assert!(matches!(
            classify_stdout_line(&ok),
            StdoutLineVerdict::Frame(_)
        ));
        assert!(matches!(
            classify_stdout_line(r#"{"type":"request"}"#),
            StdoutLineVerdict::Malformed(_)
        ));
    }

    // ---- TASK-005 B3 接线层：frame_bridge（drain→Router 投递、EOF→清理联动）----

    /// 构造 Ready 态 Supervisor（无真实子进程；EOF 分类走 stdout_closed 分支）。
    fn ready_supervisor() -> Arc<Mutex<Supervisor>> {
        let arc: Arc<Mutex<Supervisor>> = Arc::new(Mutex::new(Supervisor::new()));
        {
            let mut sup = lock_sup(&arc);
            sup.state = EngineState::Ready;
            sup.session_id = Some("session-br".to_string());
            sup.generation = 1;
            sup.started_at_wall = Some(rfc3339_now());
            sup.started_instant = Some(Instant::now());
        }
        arc
    }

    fn frame_envelope(name: &str) -> ProtocolEnvelope {
        ProtocolEnvelope {
            protocol_version: 1,
            session_id: "session-br".into(),
            msg_type: "event".into(),
            id: None,
            command: None,
            params: None,
            seq: Some(1),
            name: Some(name.to_string()),
            timestamp: Some("2026-10-09T12:00:00.000Z".into()),
            payload: None,
            success: None,
            data: None,
            error: None,
        }
    }

    #[test]
    fn frame_bridge_forwards_frames_and_eof_triggers_cleanup_with_router_shutdown() {
        let sup_arc = ready_supervisor();
        // Router 探针通道：Frame 投递观测 + RouterHandle Shutdown 联动观测
        let (frame_tx, frame_rx) = mpsc::channel::<RouterInput>();
        let (shutdown_tx, shutdown_rx) = mpsc::channel::<RouterInput>();
        {
            let mut sup = lock_sup(&sup_arc);
            sup.router_tx = Some(frame_tx);
            sup.router = Some(RouterHandle::new(shutdown_tx));
        }
        let (tx, rx) = mpsc::channel::<StdoutMessage>();
        let inner = Arc::clone(&sup_arc);
        let bridge = std::thread::spawn(move || frame_bridge_loop(inner, 1, rx));

        // 1) 有效帧 → RouterInput::Frame 投递（drain→Router 链路）
        tx.send(StdoutMessage::Line(frame_envelope("script_state_changed")))
            .expect("发送帧");
        match frame_rx.recv_timeout(Duration::from_secs(2)).expect("Frame 应到达") {
            RouterInput::Frame(envelope) => {
                assert_eq!(envelope.name.as_deref(), Some("script_state_changed"));
            }
            RouterInput::UiRequest { .. } | RouterInput::UiCancel { .. } | RouterInput::Shutdown => {
                panic!("应投递 Frame，实际为其他输入类型")
            }
        }

        // 2) EOF → 清理序列（Exited）+ Router Shutdown（在途 ipc 快速失败联动）
        tx.send(StdoutMessage::Eof).expect("发送 EOF");
        bridge.join().expect("bridge 线程应正常结束");
        {
            let sup = lock_sup(&sup_arc);
            assert_eq!(sup.state, EngineState::Exited, "EOF 应触发清理到终态");
            assert!(sup.router.is_none(), "清理应摘除 Router 句柄");
            assert!(sup.router_tx.is_none(), "清理应摘除 Router 通道");
        }
        match shutdown_rx.recv_timeout(Duration::from_secs(2)).expect("Shutdown 应到达") {
            RouterInput::Shutdown => {} // pending.fail_all(ENGINE_UNAVAILABLE) 由此触发
            RouterInput::Frame(_) | RouterInput::UiRequest { .. } | RouterInput::UiCancel { .. } => {
                panic!("EOF 应联动 Router Shutdown，实际为其他输入类型")
            }
        }
    }

    #[test]
    fn frame_bridge_malformed_overflow_closes_session_with_protocol_error() {
        let sup_arc = ready_supervisor();
        let (frame_tx, _frame_rx) = mpsc::channel::<RouterInput>();
        {
            let mut sup = lock_sup(&sup_arc);
            sup.router_tx = Some(frame_tx);
        }
        let (tx, rx) = mpsc::channel::<StdoutMessage>();
        let inner = Arc::clone(&sup_arc);
        let bridge = std::thread::spawn(move || frame_bridge_loop(inner, 1, rx));

        // §6.5：连续畸形溢出信号 → 关闸门 + 关会话（last_error=protocol_error）
        tx.send(StdoutMessage::MalformedOverflow).expect("发送溢出信号");
        bridge.join().expect("bridge 线程应正常结束");
        let sup = lock_sup(&sup_arc);
        assert_eq!(sup.state, EngineState::Exited);
        assert!(
            sup.last_error
                .as_deref()
                .is_some_and(|e| e.starts_with("protocol_error")),
            "溢出清理应以 protocol_error 归类，实际 {:?}",
            sup.last_error
        );
    }

    #[test]
    fn for_each_line_lossy_early_stop_on_receiver_gone() {
        // stdout drain 原语义：接收端消失（on_line 返回 false）→ 提前终止
        let input: &[u8] = b"a\nb\nc\n";
        let mut seen: Vec<String> = Vec::new();
        for_each_line_lossy(&mut Cursor::new(input), |line| {
            if let Ok(l) = line {
                seen.push(l.to_string());
            }
            seen.len() < 2 // 读到第 2 行后停止
        })
        .expect("读取应成功");
        assert_eq!(seen, vec!["a", "b"], "提前终止后不得继续回调");
    }

    // ---- §2.4 状态机转换表：全路径 ----

    #[test]
    fn transition_table_legal_paths() {
        use EngineState::*;
        use TransitionEvent::*;
        // 启动：Idle/Exited → Starting（重启总是产生新会话，语义由调用方保证）
        assert_eq!(EngineState::transition(Idle, StartRequested), Some(Starting));
        assert_eq!(EngineState::transition(Exited, StartRequested), Some(Starting));
        // 握手完成：Starting → Ready
        assert_eq!(EngineState::transition(Starting, HandshakeCompleted), Some(Ready));
        // 停止受理：Starting/Ready → Stopping（Stopping 只能进 Exited）
        assert_eq!(EngineState::transition(Starting, StopRequested), Some(Stopping));
        assert_eq!(EngineState::transition(Ready, StopRequested), Some(Stopping));
        // 清理完成：Starting/Ready/Stopping → Exited
        assert_eq!(EngineState::transition(Starting, CleanupCompleted), Some(Exited));
        assert_eq!(EngineState::transition(Ready, CleanupCompleted), Some(Exited));
        assert_eq!(EngineState::transition(Stopping, CleanupCompleted), Some(Exited));
    }

    #[test]
    fn transition_table_illegal_paths_rejected() {
        use EngineState::*;
        use TransitionEvent::*;
        // 终态不复活（06）：Exited 只允许 StartRequested
        assert_eq!(EngineState::transition(Exited, StopRequested), None);
        assert_eq!(EngineState::transition(Exited, CleanupCompleted), None);
        assert_eq!(EngineState::transition(Exited, HandshakeCompleted), None);
        // Idle 从未启动：不能停止/清理/握手
        assert_eq!(EngineState::transition(Idle, StopRequested), None);
        assert_eq!(EngineState::transition(Idle, CleanupCompleted), None);
        assert_eq!(EngineState::transition(Idle, HandshakeCompleted), None);
        // Ready 不重复握手；Starting 不二次启动
        assert_eq!(EngineState::transition(Ready, HandshakeCompleted), None);
        assert_eq!(EngineState::transition(Starting, StartRequested), None);
        assert_eq!(EngineState::transition(Ready, StartRequested), None);
        assert_eq!(EngineState::transition(Stopping, StartRequested), None);
        assert_eq!(EngineState::transition(Stopping, StopRequested), None);
        assert_eq!(EngineState::transition(Stopping, HandshakeCompleted), None);
    }

    // ---- session_id 格式（§2.3/§4.2：session-<uuid-v4>，仅 [A-Za-z0-9-]）----

    #[test]
    fn session_id_format_and_uniqueness() {
        let id1 = Supervisor::generate_session_id();
        let id2 = Supervisor::generate_session_id();
        assert!(id1.starts_with("session-"));
        // uuid-v4：36 字符（8-4-4-4-12），总长 44
        assert_eq!(id1.len(), 8 + 36);
        assert!(protocol::is_valid_session_id(&id1));
        assert_eq!(id1.matches('-').count(), 5); // "session-" 1 个 + uuid 4 个
        // 唯一性：两次生成不同（重启新 session_id 的前提）
        assert_ne!(id1, id2);
        // 全部字符合法
        assert!(id1.chars().all(|c| c.is_ascii_alphanumeric() || c == '-'));
    }

    // ---- Supervisor 清理序列与诊断（无真实进程的替身验证）----

    #[test]
    fn new_supervisor_is_idle_and_status_shape() {
        let sup = Supervisor::new();
        let status = sup.status();
        assert_eq!(status.state, EngineState::Idle);
        assert_eq!(status.session_id, None);
        assert_eq!(status.pid, None);
        assert_eq!(status.uptime_ms, None);
        assert_eq!(status.last_error, None);
        assert!(status.capabilities.is_empty());
        assert!(status.versions.is_none());
        let diag = sup.diagnostics();
        assert_eq!(diag.exit_code, None);
        assert!(!diag.job_object_active);
        assert!(diag.stderr_tail.is_empty());
        assert_eq!(diag.stdout_event_count, 0);
        assert_eq!(diag.malformed_line_count, 0);
    }

    #[test]
    fn cleanup_from_ready_reaches_exited_and_is_idempotent() {
        let mut sup = Supervisor::new();
        // 构造 Ready 态（无真实子进程的替身路径；真实 OS 行为按 §6.3 由集成验收覆盖）
        sup.session_id = Some(Supervisor::generate_session_id());
        sup.state = EngineState::Ready;
        sup.capabilities.push(CapabilityView {
            id: "ocr".to_string(),
            state: "unavailable".to_string(),
            reason: Some("骨架未接入识别，模型未由引擎加载".to_string()),
        });
        sup.run_cleanup_locked(Some("stdout_closed".to_string()));
        assert_eq!(sup.state, EngineState::Exited);
        assert!(sup.cleanup_done);
        assert_eq!(sup.last_error.as_deref(), Some("stdout_closed"));
        assert!(sup.exited_at_wall.is_some());
        // 幂等：二次清理不 panic、不覆盖既有 last_error
        sup.run_cleanup_locked(Some("another".to_string()));
        assert_eq!(sup.state, EngineState::Exited);
        assert_eq!(sup.last_error.as_deref(), Some("stdout_closed"));
        // 终态不复活：Exited 后握手迁移非法
        assert_eq!(
            EngineState::transition(sup.state, TransitionEvent::HandshakeCompleted),
            None
        );
    }

    #[test]
    fn null_gate_close_is_noop() {
        let gate = NullGate;
        gate.close_gate("engine_stopping"); // 不 panic 即为约定
    }

    // ---- resolve_sidecar_command（§2.6）----

    #[test]
    fn resolve_dev_sidecar_reports_missing_python_clearly() {
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        let err = dev_python_path(&root, None).unwrap_err();
        assert!(err.contains(&root.join(".venv/Scripts/python.exe").display().to_string()));
    }

    #[test]
    fn old_frame_bridge_cannot_stop_restarted_engine() {
        let sup = ready_supervisor();
        let (tx, rx) = mpsc::channel();
        tx.send(StdoutMessage::Eof).unwrap();
        frame_bridge_loop(Arc::clone(&sup), 0, rx);
        assert_eq!(lock_sup(&sup).state, EngineState::Ready);
    }

    #[test]
    fn completed_watchdog_reports_failure_to_start_caller() {
        let mut sup = Supervisor::new();
        sup.state = EngineState::Starting;
        sup.run_cleanup_locked(Some("startup_timeout".into()));
        assert_eq!(sup.start_result(sup.generation).err().as_deref(), Some("startup_timeout"));
    }

    // ---- 故障注入参数构造（§6.2/§6.4；env 以替身 Option 传入，
    //      避免真实进程级 env 设置造成的测试间串扰，测试天然串行安全）----

    #[test]
    fn fault_scenario_env_unset_yields_no_flag() {
        // env 未设 → read_fault_scenario_override 返回 None → 无该参数
        let script = std::path::Path::new("backend/main.py");
        let args = build_sidecar_args(script, "session-x", None, std::path::Path::new("D:/data"));
        assert_eq!(
            args,
            vec![
                "backend/main.py".to_string(),
                "--session-id".to_string(),
                "session-x".to_string(),
                "--data-dir".to_string(),
                "D:/data".to_string(),
            ]
        );
        assert!(!args.contains(&"--fault-scenario".to_string()));
    }

    #[test]
    fn fault_scenario_env_valid_appends_flag() {
        let script = std::path::Path::new("backend/main.py");
        // 六枚举逐一验证：合法 env → 追加 --fault-scenario <值>
        for scenario in FAULT_SCENARIOS {
            assert!(is_valid_fault_scenario(scenario));
            let args = build_sidecar_args(
                script,
                "session-x",
                Some(scenario),
                std::path::Path::new("D:/data"),
            );
            let idx = args
                .iter()
                .position(|a| a == "--fault-scenario")
                .unwrap_or_else(|| panic!("场景 {scenario} 应追加 --fault-scenario 参数"));
            assert_eq!(args[idx + 1], *scenario, "flag 后应紧跟枚举值");
            // §4.2 参数顺序：脚本路径 → --session-id → --data-dir → --fault-scenario
            assert_eq!(args[1], "--session-id");
            assert_eq!(args[3], "--data-dir");
            assert!(idx > 4);
        }
    }

    #[test]
    fn fault_scenario_env_invalid_is_filtered() {
        // env 非法值 → resolve 路径过滤逻辑（is_valid → None）→ 不追加参数，
        // 宿主不 crash（与 read_fault_scenario_override 的 stderr 日志分支等价）
        let bogus_values = ["not_a_scenario", "CRASH_ON_START", "crash_on_start ", "stderr flood"];
        for bogus in bogus_values {
            assert!(!is_valid_fault_scenario(bogus), "{bogus:?} 不应在枚举内");
            let filtered = if is_valid_fault_scenario(bogus) { Some(bogus) } else { None };
            let args = build_sidecar_args(
                std::path::Path::new("m.py"),
                "session-y",
                filtered,
                std::path::Path::new("D:/data"),
            );
            assert!(
                !args.contains(&"--fault-scenario".to_string()),
                "非法值 {bogus:?} 不应追加参数"
            );
        }
    }

    // ---- 数据根解析（TASK-006 §2.5；纯函数替身测：env 值/注入根/编译分支参数化）----

    #[test]
    fn data_dir_env_override_wins() {
        // SIDECAR_DATA_DIR 覆盖最高优先（QA/测试隔离），压过注入根与默认分支
        let dir = resolve_data_dir_from(Some("E:/tmp/qa-data"), Some(std::path::Path::new("C:/app")), true);
        assert_eq!(dir, PathBuf::from("E:/tmp/qa-data"));
        // 空白覆盖值视为未设置（回退注入根）
        let blank = resolve_data_dir_from(Some("   "), Some(std::path::Path::new("C:/app")), true);
        assert_eq!(blank, PathBuf::from("C:/app"));
    }

    #[test]
    fn data_dir_app_local_beats_default_and_release_has_no_dev_suffix() {
        // release：使用宿主注入的数据目录
        let injected = resolve_data_dir_from(None, Some(std::path::Path::new("C:/app/data")), false);
        assert_eq!(injected, PathBuf::from("C:/app/data"));
        // 无注入时按编译分支取默认：dev 隔离 ScreenTrigger-dev / release ScreenTrigger
        // （localappdata_root 真实 env；只断言末段目录名，不断言盘符）
        let dev = resolve_data_dir_from(None, None, true);
        assert!(
            dev.ends_with("ScreenTrigger-dev"),
            "dev 默认应隔离 ScreenTrigger-dev：{}",
            dev.display()
        );
        let rel = resolve_data_dir_from(None, None, false);
        assert!(rel.ends_with("ScreenTrigger"), "release 兜底应为 ScreenTrigger：{}", rel.display());
        // 同一 LOCALAPPDATA 根下 dev 与 release 不重叠
        assert_ne!(dev, rel);
    }

    // ---- RFC3339 时间戳（时钟替身：纯函数验证）----

    #[test]
    fn rfc3339_epoch_and_known_instant() {
        assert_eq!(format_rfc3339(0, 0), "1970-01-01T00:00:00.000Z");
        // 2026-10-09T12:00:00Z = 1791547200 秒（设计 §4.3 示例时刻）
        assert_eq!(format_rfc3339(1_791_547_200, 500), "2026-10-09T12:00:00.500Z");
        // 闰年边界：2024-02-29T00:00:00Z = 1709164800 秒
        assert_eq!(format_rfc3339(1_709_164_800, 0), "2024-02-29T00:00:00.000Z");
        assert_eq!(rfc3339_now().len(), 24);
    }

    // ---- 畸形行计数路径（协议解析层已测，此处验证计数通道形状）----

    #[test]
    fn malformed_error_display_is_stable() {
        let e = ProtocolParseError::MissingField("session_id");
        assert!(e.to_string().contains("session_id"));
    }
}
