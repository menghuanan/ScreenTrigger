/**
 * 协议伴生类型（TASK-006 设计 §3.3；02/schema 逐字段，TS 侧落点）。
 *
 * 对齐基准：schema/types.schema.json（capability/providerManifest/modelManifest/
 * errorCode）+ B3 Rust capabilities.rs（常量列表同源）：
 * - CAPABILITY_IDS 七枚举（§7.1 冻结面）；
 * - CAPABILITY_STATES 三枚举（TASK-005 冻结 {id,state,reason}）；
 * - ERROR_CODES = 06 错误码注册表全量 40 码。
 */

import type { TypeError } from "./region";

/** 能力 id 七枚举（§7.1；schema capability.id 枚举同源）。 */
export type CapabilityId =
  | "ocr"
  | "lua_script"
  | "template_match"
  | "contour_match"
  | "color_match"
  | "input"
  | "capture";

export const CAPABILITY_IDS: readonly CapabilityId[] = [
  "ocr",
  "lua_script",
  "template_match",
  "contour_match",
  "color_match",
  "input",
  "capture",
];

/** 能力 state 三枚举（TASK-005 冻结）。 */
export type CapabilityState = "available" | "pending" | "unavailable";

export const CAPABILITY_STATES: readonly CapabilityState[] = [
  "available",
  "pending",
  "unavailable",
];

/** Capability（TASK-005 冻结 {id,state,reason}；schema required 仅 id/state）。 */
export interface Capability {
  id: CapabilityId;
  state: CapabilityState;
  reason: string | null;
}

/** 06 错误码注册表全量 40 码（schema errorCode 枚举同源）。 */
export const ERROR_CODES: readonly string[] = [
  "INVALID_PARAMS",
  "INVALID_STATE",
  "BUSY",
  "CONFLICT",
  "IN_USE",
  "NOT_FOUND",
  "PROTOCOL_ERROR",
  "UNKNOWN_COMMAND",
  "DUPLICATE_EXPIRED",
  "ENGINE_UNAVAILABLE",
  "INTERNAL_ERROR",
  "DISPLAY_NOT_FOUND",
  "INVALID_REGION",
  "INVALID_IMAGE",
  "IMAGE_EXPIRED",
  "CAPTURE_FAILED",
  "TARGET_NOT_FOUND",
  "GROUP_NOT_FOUND",
  "MODEL_UNAVAILABLE",
  "LANGUAGE_UNSUPPORTED",
  "RECOGNITION_TIMEOUT",
  "RECOGNITION_FAILED",
  "SCRIPT_NOT_FOUND",
  "SCRIPT_SYNTAX_ERROR",
  "SCRIPT_RUNTIME_ERROR",
  "SCRIPT_TIMEOUT",
  "WAIT_TIMEOUT",
  "CALL_DEPTH_EXCEEDED",
  "SERIALIZATION_ERROR",
  "CAPABILITY_DENIED",
  "INPUT_FAILED",
  "REGION_LIMIT_VIOLATION",
  "STALE_FRAME",
  "WINDOW_NOT_FOUND",
  "WINDOW_UNAVAILABLE",
  "HOTKEY_CONFLICT",
  "STORAGE_ERROR",
  "DATA_CORRUPT",
  "SCHEMA_UNSUPPORTED",
  "RESOURCE_LIMIT",
];

/** 合法能力 id。 */
export function isValidCapabilityId(id: string): boolean {
  return (CAPABILITY_IDS as readonly string[]).includes(id);
}

/** 合法 06 错误码。 */
export function isValidErrorCode(code: string): boolean {
  return ERROR_CODES.includes(code);
}

/** 条件类型四枚举（条件叶/manifest supported_condition_types 共用）。 */
export type ConditionType = "template" | "contour" | "ocr" | "color";

/** ProviderManifest（00 Provider 接口段；TASK-006 只定义类型）。 */
export interface ProviderManifest {
  provider_id: string;
  api_version: number;
  provider_version: string;
  supported_condition_types: ConditionType[];
  model_ids: string[];
  supported_languages: string[];
  thread_safe: boolean;
  offline: boolean;
}

/** ModelManifest（00/07；裁决 A：字符集覆盖字段名=supported_charsets，三端禁改）。 */
export interface ModelManifest extends ProviderManifest {
  license: string;
  source: string;
  sha256: string;
  supported_charsets: string[];
}

/**
 * Capability 纯校验（id 七枚举、state 三枚举；reason 可 null/缺省）。
 * 返回 TypeError | null（与 lib/types 其他校验同风格）。
 */
export function validateCapability(v: unknown): TypeError | null {
  if (typeof v !== "object" || v === null || Array.isArray(v)) {
    return { code: "INVALID_PARAMS", field: "capability" };
  }
  const obj = v as Record<string, unknown>;
  if (typeof obj.id !== "string" || !isValidCapabilityId(obj.id)) {
    return { code: "INVALID_PARAMS", field: "capability.id" };
  }
  if (
    typeof obj.state !== "string" ||
    !CAPABILITY_STATES.includes(obj.state as CapabilityState)
  ) {
    return { code: "INVALID_PARAMS", field: "capability.state" };
  }
  if (obj.reason !== undefined && obj.reason !== null && typeof obj.reason !== "string") {
    return { code: "INVALID_PARAMS", field: "capability.reason" };
  }
  return null;
}
