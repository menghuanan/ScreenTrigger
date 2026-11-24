/**
 * 运行/监控/执行状态类型与校验（TASK-006 设计 §3.2/§6.2；02 权威逐字段）。
 *
 * 对齐基准：schema/types.schema.json + B3 Rust runtime.rs（错误字段路径逐字
 * 一致）。交叉约束（§6.2/裁决 §15.2）：
 * - mode 四枚举与 iterations/stop_target_id 条件必填/必 null；
 * - display_id/binding_id 互斥（双设 → `binding_id`，用例口径；未绑定必填
 *   display_id → `display_id`；绑定时 display_id 必须 null → `display_id`）；
 * - region 空间与绑定一致性 → `region.space`；**image_pixel region 作为
 *   运行/监控范围 → 拒绝 INVALID_PARAMS{field:"region.space"}**（架构师裁决）；
 * - MonitorConfig：on_match=stop→repeat 必须 false；on_timeout=retry→max_retries
 *   ≥1（stop/run_script 分支必须 0）；run_script 分支 script_id 必填（required
 *   先于 max_retries 清零校验，02 allOf 分支内 required 优先——B3 冻结顺序）。
 */

import { nonEmptyString, validateRegion, type TypeError } from "./region";

function invalidParams(field: string): TypeError {
  return { code: "INVALID_PARAMS", field };
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function asInteger(v: unknown): number | null {
  return typeof v === "number" && Number.isInteger(v) ? v : null;
}

function asFinite(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// ---------------------------------------------------------------------------
// 类型（02/schema 逐字段）
// ---------------------------------------------------------------------------

/** ExecutionState.status 八枚举（02 权威，澄清 K2；终态不转回 running）。 */
export type ExecutionStatus =
  | "starting"
  | "running"
  | "paused"
  | "debug_paused"
  | "stopping"
  | "completed"
  | "cancelled"
  | "failed";

export const EXECUTION_STATUSES: readonly ExecutionStatus[] = [
  "starting", "running", "paused", "debug_paused", "stopping", "completed", "cancelled", "failed",
];

/** 错误结构（06 四字段；schema errorStructure 同源）。 */
export interface ErrorStructure {
  code: string;
  message: string;
  details: Record<string, unknown>;
  retryable: boolean;
}

/** ExecutionState（02）：纯 monitor 时 script_id=null；result 只保留最后一次。 */
export interface ExecutionState {
  execution_id: string;
  kind: "script" | "monitor";
  status: ExecutionStatus;
  reason: string | null;
  iteration: number;
  script_id: string | null;
  current_line: number | null;
  result: unknown;
  error: ErrorStructure | null;
  started_at: string;
  updated_at: string;
  effective_hz: number | null;
  throttled: boolean;
}

/** RunConfig（02）：mode 互斥字段条件必填/必 null；display_id/binding_id 互斥。 */
export interface RunConfig {
  script_id: string;
  args: Record<string, unknown>;
  mode: "once" | "count" | "forever" | "until_target";
  iterations: number | null;
  stop_target_id: string | null;
  debug: boolean;
  /** null 表示选定显示器全屏或绑定客户区；image_pixel 空间被运行范围拒绝（§15.2）。 */
  region: RegionRef | null;
  display_id: string | null;
  binding_id: string | null;
  allow_exit: boolean;
  frequency_hz: number;
  total_timeout_ms: number;
}

/** MonitorConfig（02）：复用 RunConfig 的 scope 字段；不含 script_id/args/mode 等。 */
export interface MonitorConfig {
  target_id: string;
  region: RegionRef | null;
  display_id: string | null;
  binding_id: string | null;
  allow_exit: boolean;
  frequency_hz: number;
  total_timeout_ms: number;
  debug: boolean;
  repeat: boolean;
  wait_timeout_ms: number;
  on_match: "run_script" | "stop";
  action_script_id: string | null;
  action_args: Record<string, unknown>;
  on_timeout: "stop" | "retry" | "run_script";
  timeout_script_id: string | null;
  timeout_args: Record<string, unknown>;
  max_retries: number;
  retry_delay_ms: number;
  trigger: "rising_edge" | "every_match";
  cooldown_ms: number;
}

/** region 字段引用（结构同 region.ts Region | null；避免重复联合展开）。 */
export type RegionRef = Record<string, unknown>;

// ---------------------------------------------------------------------------
// 校验纯函数（与 B3 Rust runtime.rs 逐路径一致）
// ---------------------------------------------------------------------------

const RUN_CONFIG_KEYS = [
  "script_id", "args", "mode", "iterations", "stop_target_id", "debug", "region",
  "display_id", "binding_id", "allow_exit", "frequency_hz", "total_timeout_ms",
] as const;

const MONITOR_CONFIG_KEYS = [
  "target_id", "region", "display_id", "binding_id", "allow_exit", "frequency_hz",
  "total_timeout_ms", "debug", "repeat", "wait_timeout_ms", "on_match", "action_script_id",
  "action_args", "on_timeout", "timeout_script_id", "timeout_args", "max_retries",
  "retry_delay_ms", "trigger", "cooldown_ms",
] as const;

const EXECUTION_STATE_KEYS = [
  "execution_id", "kind", "status", "reason", "iteration", "script_id", "current_line",
  "result", "error", "started_at", "updated_at", "effective_hz", "throttled",
] as const;

/** RunConfig / MonitorConfig 共用：display_id 与 binding_id 互斥（§3.2）。 */
function validateDisplayBinding(doc: Record<string, unknown>): TypeError | null {
  const display = doc.display_id;
  const binding = doc.binding_id;
  const displaySet = nonEmptyString(display) !== null;
  const bindingSet = nonEmptyString(binding) !== null;
  if (displaySet && bindingSet) {
    return invalidParams("binding_id");
  }
  if (!bindingSet && !displaySet) {
    return invalidParams("display_id");
  }
  if (bindingSet && display !== null && display !== undefined) {
    return invalidParams("display_id");
  }
  return null;
}

/** RunConfig / MonitorConfig 共用：frequency 0.1–30、total_timeout_ms ≥0。 */
function validateFrequencyAndTimeout(doc: Record<string, unknown>): TypeError | null {
  const freq = asFinite(doc.frequency_hz);
  if (freq === null) {
    return invalidParams("frequency_hz");
  }
  if (freq < 0.1 || freq > 30) {
    return invalidParams("frequency_hz");
  }
  const timeout = asInteger(doc.total_timeout_ms);
  if (timeout === null) {
    return invalidParams("total_timeout_ms");
  }
  if (timeout < 0) {
    return invalidParams("total_timeout_ms");
  }
  return null;
}

/** RunConfig / MonitorConfig 共用：region 校验 + 空间/绑定一致性 + image_pixel 拒绝。 */
function validateScopeRegion(doc: Record<string, unknown>): TypeError | null {
  const region = doc.region;
  if (region === null || region === undefined) {
    return null;
  }
  const regionErr = validateRegion(region);
  if (regionErr !== null) {
    return regionErr; // region.* 前缀路径原样透传（澄清 K1）
  }
  const space = nonEmptyString((region as Record<string, unknown>).space) ?? "";
  if (space === "image_pixel") {
    // 运行/监控范围不接受图像像素空间（识别测试 job 专用；架构师裁决 §15.2）
    return invalidParams("region.space");
  }
  const displaySet = nonEmptyString(doc.display_id) !== null;
  const bindingSet = nonEmptyString(doc.binding_id) !== null;
  const consistent =
    space === "screen_physical" ? displaySet && !bindingSet
    : space === "window_client_physical" ? bindingSet && !displaySet
    : false;
  if (!consistent) {
    return invalidParams("region.space");
  }
  return null;
}

/** RunConfig 校验（§3.2：mode 互斥字段条件必填/必 null + scope 交叉约束）。 */
export function validateRunConfig(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("run_config");
  }
  for (const key of RUN_CONFIG_KEYS) {
    if (!(key in v)) {
      return invalidParams(key);
    }
  }
  if (nonEmptyString(v.script_id) === null) {
    return invalidParams("script_id");
  }
  if (!isRecord(v.args)) {
    return invalidParams("args");
  }
  if (typeof v.debug !== "boolean") {
    return invalidParams("debug");
  }
  if (typeof v.allow_exit !== "boolean") {
    return invalidParams("allow_exit");
  }
  const mode = nonEmptyString(v.mode);
  if (mode !== "once" && mode !== "count" && mode !== "forever" && mode !== "until_target") {
    return invalidParams("mode");
  }
  // mode 互斥字段：count → iterations ≥1；其余 → 必须 null
  if (mode === "count") {
    const it = asInteger(v.iterations);
    if (it === null || it < 1) {
      return invalidParams("iterations");
    }
  } else if (v.iterations !== null && v.iterations !== undefined) {
    return invalidParams("iterations");
  }
  // stop_target_id：仅 until_target 非空
  if (mode === "until_target") {
    if (nonEmptyString(v.stop_target_id) === null) {
      return invalidParams("stop_target_id");
    }
  } else if (v.stop_target_id !== null && v.stop_target_id !== undefined) {
    return invalidParams("stop_target_id");
  }
  const bindErr = validateDisplayBinding(v);
  if (bindErr !== null) {
    return bindErr;
  }
  const freqErr = validateFrequencyAndTimeout(v);
  if (freqErr !== null) {
    return freqErr;
  }
  return validateScopeRegion(v);
}

/** MonitorConfig 校验（§3.2：on_match/on_timeout 交叉约束 + scope 交叉约束）。 */
export function validateMonitorConfig(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("monitor_config");
  }
  for (const key of MONITOR_CONFIG_KEYS) {
    if (!(key in v)) {
      return invalidParams(key);
    }
  }
  if (nonEmptyString(v.target_id) === null) {
    return invalidParams("target_id");
  }
  for (const key of ["debug", "allow_exit", "repeat"] as const) {
    if (typeof v[key] !== "boolean") {
      return invalidParams(key);
    }
  }
  if (!isRecord(v.action_args)) {
    return invalidParams("action_args");
  }
  if (!isRecord(v.timeout_args)) {
    return invalidParams("timeout_args");
  }
  const wait = asInteger(v.wait_timeout_ms);
  if (wait === null || wait < 0) {
    return invalidParams("wait_timeout_ms");
  }
  const retryDelay = asInteger(v.retry_delay_ms);
  if (retryDelay === null || retryDelay < 0) {
    return invalidParams("retry_delay_ms");
  }
  const cooldown = asInteger(v.cooldown_ms);
  if (cooldown === null || cooldown < 0) {
    return invalidParams("cooldown_ms");
  }
  const maxRetries = asInteger(v.max_retries);
  if (maxRetries === null) {
    return invalidParams("max_retries");
  }
  // 全局范围 0–100（02；on_timeout 分支的 ≥1 约束在其后叠加）
  if (maxRetries < 0 || maxRetries > 100) {
    return invalidParams("max_retries");
  }
  // on_match 交叉约束
  const onMatch = nonEmptyString(v.on_match);
  if (onMatch !== "run_script" && onMatch !== "stop") {
    return invalidParams("on_match");
  }
  const actionScriptId = v.action_script_id;
  if (onMatch === "stop") {
    // 02：on_match=stop → repeat 必须 false；action_script_id 必须 null
    if (v.repeat === true) {
      return invalidParams("repeat");
    }
    if (actionScriptId !== null && actionScriptId !== undefined) {
      return invalidParams("action_script_id");
    }
  } else {
    // run_script → action_script_id 必填非空
    if (nonEmptyString(actionScriptId) === null) {
      return invalidParams("action_script_id");
    }
  }
  // on_timeout 交叉约束
  const onTimeout = nonEmptyString(v.on_timeout);
  if (onTimeout !== "stop" && onTimeout !== "retry" && onTimeout !== "run_script") {
    return invalidParams("on_timeout");
  }
  const timeoutScriptId = v.timeout_script_id;
  if (onTimeout === "retry") {
    // 02：retry → max_retries ≥1（0 违例，用例口径）
    if (maxRetries < 1) {
      return invalidParams("max_retries");
    }
    if (timeoutScriptId !== null && timeoutScriptId !== undefined) {
      return invalidParams("timeout_script_id");
    }
  } else if (onTimeout === "stop") {
    if (maxRetries !== 0) {
      return invalidParams("max_retries");
    }
    if (timeoutScriptId !== null && timeoutScriptId !== undefined) {
      return invalidParams("timeout_script_id");
    }
  } else {
    // run_script → timeout_script_id 必填非空（先于 max_retries 清零校验）；max_retries 必须 0
    if (nonEmptyString(timeoutScriptId) === null) {
      return invalidParams("timeout_script_id");
    }
    if (maxRetries !== 0) {
      return invalidParams("max_retries");
    }
  }
  const trigger = nonEmptyString(v.trigger);
  if (trigger !== "rising_edge" && trigger !== "every_match") {
    return invalidParams("trigger");
  }
  const bindErr = validateDisplayBinding(v);
  if (bindErr !== null) {
    return bindErr;
  }
  const freqErr = validateFrequencyAndTimeout(v);
  if (freqErr !== null) {
    return freqErr;
  }
  return validateScopeRegion(v);
}

/** ExecutionState 校验（status 八枚举；kind 两枚举；终态字段可 null）。 */
export function validateExecutionState(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("execution_state");
  }
  for (const key of EXECUTION_STATE_KEYS) {
    if (!(key in v)) {
      return invalidParams(key);
    }
  }
  if (nonEmptyString(v.execution_id) === null) {
    return invalidParams("execution_id");
  }
  const kind = nonEmptyString(v.kind);
  if (kind !== "script" && kind !== "monitor") {
    return invalidParams("kind");
  }
  const status = nonEmptyString(v.status);
  if (status === null || !EXECUTION_STATUSES.includes(status as ExecutionStatus)) {
    return invalidParams("status");
  }
  const iteration = asInteger(v.iteration);
  if (iteration === null || iteration < 0) {
    return invalidParams("iteration");
  }
  if (typeof v.throttled !== "boolean") {
    return invalidParams("throttled");
  }
  return null;
}
