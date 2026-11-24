/**
 * Tauri invoke 封装——引擎运维面（TASK-004 §4.5 四命令保留；TASK-005 §3.3 明确
 * 本文件运维 4 命令不变）。协议命令统一入口见 protocolClient.ts。
 *
 * TASK-005 契约④适配：system_ready 直转占位已移除——Rust 侧不再直转，
 * UI 订阅统一走 protocolClient 事件总线（会话锚/seq 恢复在客户端侧）。
 *
 * 职责：
 * - 4 条运维命令：engineStart / engineStop / engineStatus / engineDiagnostics；
 * - 集中错误 → 用户可读中文文案（CommandError / Rust reason 字符串 / 兜底）。
 *
 * 不做：重试（§5.3 默认不自动重试）、缓存。
 */

import { invoke } from "@tauri-apps/api/core";

import type {
  CommandError,
  EngineDiagnostics,
  EngineStatusView,
} from "./types";

/** `engine_start`（幂等：Starting/Ready 直接返回当前状态；失败 reject CommandError）。 */
export function engineStart(): Promise<EngineStatusView> {
  return invoke<EngineStatusView>("engine_start");
}

/** `engine_stop`（幂等：非运行态直接返回当前状态）。 */
export function engineStop(): Promise<EngineStatusView> {
  return invoke<EngineStatusView>("engine_stop");
}

/** `engine_status`（只读，任何时刻可调用）。 */
export function engineStatus(): Promise<EngineStatusView> {
  return invoke<EngineStatusView>("engine_status");
}

/** `engine_diagnostics`（stderr 尾部、计数、Job Object 活跃性）。 */
export function engineDiagnostics(): Promise<EngineDiagnostics> {
  return invoke<EngineDiagnostics>("engine_diagnostics");
}

// ---------------------------------------------------------------------------
// 错误 → 用户可读中文文案（集中映射，组件不各自拼装）
// ---------------------------------------------------------------------------

/** Rust Supervisor 的 last_error/reason 字符串 → 中文文案（TASK-004 §5.2 错误路径表）。 */
function mapReason(reason: string): string {
  if (reason === "startup_timeout") {
    return "启动超时：30 秒内未完成引擎握手";
  }
  if (reason === "stdout_closed") {
    return "协议流中断，引擎已停止";
  }
  if (reason === "invalid_hello_response") {
    return "引擎握手响应非法，已停止";
  }
  if (reason === "protocol_error") {
    return "连续协议帧异常，会话已关闭";
  }
  if (reason.startsWith("spawn_failed")) {
    return `引擎启动失败：${reason.slice("spawn_failed".length).replace(/^[:\s]+/, "")}`;
  }
  if (reason.startsWith("process_exited")) {
    return `引擎进程已退出（${reason.slice("process_exited".length).replace(/^[:\s]+/, "")}）`;
  }
  return reason;
}

/** CommandError.code → 中文文案（commands.rs 目前仅 ENGINE_UNAVAILABLE）。 */
function mapCommandCode(code: string): string {
  switch (code) {
    case "ENGINE_UNAVAILABLE":
      return "引擎不可用，可尝试重新启动";
    default:
      return `命令执行失败（${code}）`;
  }
}

/**
 * invoke reject 值 → 用户可读中文。
 * Tauri 2 对 `Result<T, E: Serialize>` 的 Err 序列化为 CommandError 对象；
 * 兼容字符串 reason 与原生 Error，兜底给出通用文案。
 */
export function toUserMessage(err: unknown): string {
  if (typeof err === "string") {
    return mapReason(err);
  }
  if (err && typeof err === "object") {
    const e = err as Partial<CommandError>;
    if (typeof e.message === "string" && e.message.length > 0) {
      return e.message;
    }
    if (typeof e.code === "string" && e.code.length > 0) {
      return mapCommandCode(e.code);
    }
  }
  if (err instanceof Error && err.message.length > 0) {
    return `操作失败：${err.message}`;
  }
  return "操作失败：未知错误";
}
