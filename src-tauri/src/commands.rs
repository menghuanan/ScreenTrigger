//! Tauri invoke 命令面（TASK-004 §4.1 四条运维命令 + TASK-005 §5.1 两条 IPC 命令）。
//!
//! 命令为同步命令：Tauri 在专用线程池执行，`engine_stop` 的 ≤2s 优雅期等待
//! 与 `ipc_request` 的 ≤5s/30s 期限等待均不阻塞 UI 线程。
//! 返回结构 serde 序列化为 snake_case，字段与设计逐字一致。

use tauri::State;

use crate::ipc::IpcError;
use crate::sidecar::{EngineDiagnostics, EngineStatusView, SupervisorHandle};

/// 命令错误（06 错误结构四字段子集：code/message/retryable）。
/// 引擎启动失败统一映射 ENGINE_UNAVAILABLE（06：允许用户重启，retryable=true）。
#[derive(Debug, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub struct CommandError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

/// 启动引擎（幂等：Starting/Ready 直接返回当前状态；Idle/Exited 允许启动/重启，
/// 重启总是生成新 session_id）。启动失败时状态已落为 Exited(last_error=...)。
///
/// 契约④（TASK-005）：system_ready **不再由此直转 UI**——握手成功后经
/// Router 事件总线投递（UI seq 重分配，见 sidecar::wire_router）。
#[tauri::command]
pub fn engine_start(state: State<'_, SupervisorHandle>) -> Result<EngineStatusView, CommandError> {
    state.start().map_err(|reason| CommandError {
        code: "ENGINE_UNAVAILABLE".to_string(),
        message: format!("启动失败：{reason}"),
        retryable: true,
    })
}

/// 停止引擎（幂等：非运行态直接返回当前状态）。
/// 退出序列（§2.5）：关闸门 → Router Shutdown（在途 ipc 快速失败）→ 拒绝新
/// 请求 → stdin EOF → 2s 优雅期 → TerminateProcess → Job Object CloseHandle
/// 兜底 → join 线程。
#[tauri::command]
pub fn engine_stop(
    state: State<'_, SupervisorHandle>,
) -> Result<EngineStatusView, CommandError> {
    Ok(state.stop("engine_stopping"))
}

/// 查询引擎健康状态（只读，任何时刻可调用）。
#[tauri::command]
pub fn engine_status(state: State<'_, SupervisorHandle>) -> EngineStatusView {
    state.status()
}

/// 查询引擎诊断（stderr 环形缓冲尾部、计数、Job Object 活跃性等）。
#[tauri::command]
pub fn engine_diagnostics(state: State<'_, SupervisorHandle>) -> EngineDiagnostics {
    state.diagnostics()
}

/// UI 请求入口（TASK-005 §5.1，03 信封 request；id 由 UI 生成，会话内唯一由
/// Rust 去重表裁决）。成功 → Ok(data)；失败 → Err(IpcError 四字段，06)。
#[tauri::command]
pub fn ipc_request(
    state: State<'_, SupervisorHandle>,
    id: String,
    command: String,
    params: serde_json::Value,
) -> Result<serde_json::Value, IpcError> {
    state.ipc_request(&id, &command, params)
}

/// 取消入口（TASK-005 §5.1，对应协议 cancel_request；幂等）。
/// data = {cancelled, reason}（03）。
#[tauri::command]
pub fn ipc_cancel(
    state: State<'_, SupervisorHandle>,
    request_id: String,
) -> Result<serde_json::Value, IpcError> {
    state.ipc_cancel(&request_id)
}
