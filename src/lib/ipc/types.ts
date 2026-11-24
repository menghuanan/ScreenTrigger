/**
 * IPC 类型定义。
 *
 * TASK-004 基线（运维面，与 src-tauri/src/sidecar/mod.rs、protocol.rs、
 * commands.rs 的 serde 序列化输出逐字对齐，向后兼容保留）：
 * - mod.rs::EngineState      → serde(rename_all = "lowercase")
 * - mod.rs::CapabilityView   → { id, state, reason }
 * - mod.rs::VersionsView     → { protocol_version, engine_version, lua_version, schema_version }
 * - mod.rs::EngineStatusView → 9 字段 snake_case
 * - mod.rs::EngineDiagnostics → 10 字段 snake_case
 * - protocol.rs::ProtocolEnvelope → `type` 字段（Rust 侧 msg_type 序列化回 "type"），
 *   Option 字段 skip_serializing_if = "is_none"（JSON 中可能缺省，TS 侧为可选）
 * - commands.rs::CommandError → { code, message, retryable }（engine_* 4 命令仍用）
 *
 * TASK-005 扩展（设计 §3.3/§5，B3 Rust 侧 serde snake_case 为基线）：
 * - EnvelopeType 五类型联合（protocol.rs parse_line 扩展 host_request/host_response）
 * - IpcError{code,message,details,retryable}（06 四字段，ipc_request/ipc_cancel 错误形态）
 * - EventName 联合（docs/03 事件目录逐字，19 个事件名）
 * - JobStatus（03 get_job/cancel_job 状态机：pending/running/completed/cancelled/error）
 * - CancelResult / QueryRequestResult / JobView（§5.3 命令表 data 形状）
 *
 * Rust `Option<T>` 序列化为 `T | null`（view 结构体无 skip 属性）；
 * 信封 Option 字段 skip_serializing_if=is_none → TS 可选属性。
 */

// ---------------------------------------------------------------------------
// TASK-004 基线类型（运维面）
// ---------------------------------------------------------------------------

/** 引擎健康状态机（mod.rs::EngineState，serde lowercase）。 */
export type EngineState = "idle" | "starting" | "ready" | "stopping" | "exited";

/** 能力徽标条目（mod.rs::CapabilityView，最小子集 {id,state,reason}，用户契约②）。 */
export interface CapabilityView {
  /** 能力标识，如 "ocr" / "lua_script" / "template_match" 等。 */
  id: string;
  /** "available" | "pending" | "unavailable"（capability-manifest 模板措辞）。 */
  state: string;
  /** state 非 available 时的原因说明；Rust Option → null 必现。 */
  reason: string | null;
}

/** 版本信息（mod.rs::VersionsView，hello 缓存）。 */
export interface VersionsView {
  protocol_version: number;
  engine_version: string;
  /** Rust Option<String> → string | null（lupa 探测失败时为 null，用户契约①）。 */
  lua_version: string | null;
  schema_version: number;
}

/** 引擎健康状态视图（mod.rs::EngineStatusView；engine_start/engine_stop/engine_status 共用）。 */
export interface EngineStatusView {
  state: EngineState;
  /** "session-<uuid-v4>"；从未启动时为 null。 */
  session_id: string | null;
  pid: number | null;
  /** RFC3339 UTC；null 必现于未启动/未就绪阶段。 */
  started_at: string | null;
  ready_at: string | null;
  /** 非运行态为 null（mod.rs::status()）。 */
  uptime_ms: number | null;
  /** 如 "startup_timeout" | "spawn_failed: ..." | "process_exited: code 7"。 */
  last_error: string | null;
  /** 未 ready 时为空数组（mod.rs::status() 保证非 undefined）。 */
  capabilities: CapabilityView[];
  /** 未 ready 时为 null。 */
  versions: VersionsView | null;
}

/** 诊断视图（mod.rs::EngineDiagnostics；engine_diagnostics 返回）。 */
export interface EngineDiagnostics {
  state: EngineState;
  session_id: string | null;
  pid: number | null;
  /** 进程退出码；优雅退出为 0，TerminateProcess 兜底为非 0。 */
  exit_code: number | null;
  /** 最近 ≤50 行 stderr（环形缓冲 500 行的视图）。 */
  stderr_tail: string[];
  /** 已读有效事件行计数（同 session）。 */
  stdout_event_count: number;
  /** 畸形 stdout 行计数（仅计数，不关会话，TASK-005）。 */
  malformed_line_count: number;
  /** Job Object 句柄是否活跃（KILL_ON_JOB_CLOSE 兜底是否就位）。 */
  job_object_active: boolean;
  started_at: string | null;
  exited_at: string | null;
}

/** 命令错误（commands.rs::CommandError；engine_* 4 条运维命令专用）。 */
export interface CommandError {
  /** 如 "ENGINE_UNAVAILABLE"。 */
  code: string;
  /** 面向用户的中文消息（Rust 侧已组装）。 */
  message: string;
  /** true 时 UI 允许用户重试（ENGINE_UNAVAILABLE retryable=true）。 */
  retryable: boolean;
}

// ---------------------------------------------------------------------------
// TASK-005 扩展类型（ipc_request/ipc_cancel + 事件总线）
// ---------------------------------------------------------------------------

/** 统一信封五类型（03 消息封装；protocol.rs parse_line 校验集合）。 */
export type EnvelopeType = "request" | "response" | "event" | "host_request" | "host_response";

/** 已知 IPC 错误码（03/06 错误码表；错误码为统一字符串，此处仅文档化已知值）。 */
export type KnownIpcErrorCode =
  | "UNKNOWN_COMMAND"
  | "CAPABILITY_DENIED"
  | "BUSY"
  | "DUPLICATE_EXPIRED"
  | "CONFLICT"
  | "ENGINE_UNAVAILABLE"
  | "INTERNAL_ERROR"
  | "NOT_FOUND"
  | "INVALID_PARAMS"
  | "INVALID_STATE"
  | "PROTOCOL_ERROR"
  | "RECOGNITION_TIMEOUT"
  | "CAPTURE_FAILED";

/**
 * IPC 命令错误（06 四字段；ipc_request/ipc_cancel 的 Err 形态）。
 * details 为任意 JSON 值（不含堆栈敏感对象，03）；Rust 侧可能省略 → 可选。
 */
export interface IpcError {
  code: string;
  /** 面向用户的中文消息。 */
  message: string;
  /** 结构化补充（如 {command} / {request_id}）；缺省或 null 均容忍。 */
  details?: unknown;
  retryable: boolean;
}

/**
 * 事件名联合（docs/03 事件目录逐字，19 个事件名）。
 * Tauri 事件名 = 协议 name（§4.4：eventbus 以协议名 emit）。
 */
export type EventName =
  | "system_ready"
  | "system_error"
  | "selection_started"
  | "selection_updated"
  | "selection_completed"
  | "selection_cancelled"
  | "recognition_completed"
  | "recognition_error"
  | "recognition_cancelled"
  | "script_state_changed"
  | "script_iteration"
  | "target_detected"
  | "recognition_stats"
  | "breakpoint_hit"
  | "step_completed"
  | "log_message"
  | "window_changed"
  | "catalog_changed"
  | "config_changed";

/** 可合并事件（03 事件目录标注"可合并"；积压时 Rust 侧合并到最新）。 */
export type MergeableEventName = "selection_updated" | "recognition_stats";

/** 控制类命令（§5.2：不受 32 在途限制，停止/心跳通道保留容量）。 */
export type ControlCommand =
  | "stop_script"
  | "cancel_request"
  | "cancel_job"
  | "ping"
  | "query_request";

/** 异步 job 状态机（03 get_job；backend/ipc/jobs.py 同表）。 */
export type JobStatus = "pending" | "running" | "completed" | "cancelled" | "error";

/** cancel_request data（03：{cancelled,reason}，幂等）。 */
export interface CancelResult {
  cancelled: boolean;
  /** reason ∈ {completed, not_found, not_cancellable, cancelled, already_committed} 等（03）。 */
  reason: string;
}

/** query_request data（03：{status, response}，只查询同会话）。 */
export interface QueryRequestResult {
  status: "pending" | "completed" | "unknown";
  response: unknown;
}

/** get_job data（03：{status, result, error}；未知 job_id → NOT_FOUND 错误）。 */
export interface JobView {
  status: JobStatus;
  /** 对应识别结果或 null。 */
  result: unknown;
  error: unknown;
}

/** system_ready 事件 payload（03 事件目录：capabilities、versions 必填）。 */
export interface SystemReadyPayload {
  capabilities: CapabilityView[];
  versions: VersionsView;
}

/**
 * 统一信封（03 消息封装；protocol.rs::ProtocolEnvelope 序列化形状）。
 * `type` 在 TS 侧即为 "type"（Rust msg_type 经 serde rename），五类型联合；
 * Option 字段在 JSON 中可能缺省，故均为可选属性。
 * 事件必填集含 seq/name/timestamp（§5.4，用户契约③）；request/response 必填 id。
 */
export interface ProtocolEnvelope {
  protocol_version: number;
  session_id: string;
  type: EnvelopeType;
  id?: string;
  /** UI seq：Rust eventbus 每 session 从 1 单调重分配（Python seq 不冒充 UI seq）。 */
  seq?: number;
  /** event 必有（parse_line 强校验）；值域=EventName，wire 层保持 string。 */
  name?: string;
  /** UTC RFC3339（02）；event 必填（§5.4）。 */
  timestamp?: string;
  /** 任意 JSON 值（serde_json::Value）；system_ready 事件为 SystemReadyPayload。 */
  payload?: unknown;
  success?: boolean;
  data?: unknown;
  error?: unknown;
}
