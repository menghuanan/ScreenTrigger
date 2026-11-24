/**
 * 脚本 IPC 封装（TASK-012 设计 §3：5 条命令，03 契约逐字）。
 *
 * - 复用统一客户端 `request<T>`（`ui-<n>` 请求 id、IpcError 四字段管道、超时兜底）；
 * - 返回值解包到裸实体（`{script}` → Script、`{script,source}` → LoadedScript）；
 * - `save_script` 新建/更新：更新**必须同时**给 `scriptId` 与 `expectedRevision`；
 *   新建两者均省略（仅当 `!== undefined` 时带上键，避免误触发更新）；
 * - `validate_script` 仅编译：`diagnostics[].column` 为 `int≥1` 或 `null`（引擎无法无歧义
 *   定位列时为 null，**不编造**）；lupa 缺失 → IpcError{ENGINE_UNAVAILABLE}；
 * - `name` 长度按 **Unicode 码点** 计数（`Array.from(name).length`）；源码预算按 **UTF-8
 *   字节**（`TextEncoder`）。
 */

import { request } from "../../lib/ipc/protocolClient";
import type { Script, ScriptParameter } from "../../lib/types/conditions";

/** 源码预算（设计 Q8：UTF-8 编码后字节 ≤ 1 MiB）。 */
export const SOURCE_MAX_BYTES = 1024 * 1024;
/** 分页 limit 范围（设计 Q6：1–500）。 */
export const PAGE_MIN_LIMIT = 1;
export const PAGE_MAX_LIMIT = 500;
/** 分页缺省（03：offset=0、limit=100）。 */
export const PAGE_DEFAULT_OFFSET = 0;
export const PAGE_DEFAULT_LIMIT = 100;
/** name 长度（Unicode 码点，去首尾空白后）。 */
export const NAME_MIN_CODEPOINTS = 1;
export const NAME_MAX_CODEPOINTS = 100;

/** 一条语法诊断（设计 Q4：line 自 1 起；column 为 int≥1 或 null）。 */
export interface Diagnostic {
  line: number;
  column: number | null;
  message: string;
}

/** `save_script` 参数（03：script_id?/expected_revision?/name/source/parameters）。 */
export interface SaveScriptParams {
  name: string;
  source: string;
  parameters: ScriptParameter[];
  /** 更新时必须与 `expectedRevision` 同时提供。 */
  scriptId?: string;
  expectedRevision?: number;
}

/** 列表分页参数（limit 1–500）。 */
export interface ListScriptsOptions {
  offset?: number;
  limit?: number;
}

/** 列表分页结果（03：{items,total}）。 */
export interface ScriptPage {
  items: Script[];
  total: number;
}

/** `load_script` 结果（03：{script,source}）。 */
export interface LoadedScript {
  script: Script;
  source: string;
}

/** `validate_script` 结果（03：{valid,diagnostics}；不变式 valid == (diagnostics.length===0)）。 */
export interface ValidationResult {
  valid: boolean;
  diagnostics: Diagnostic[];
}

/** 按 Unicode 码点计数（**不**用 `String.length`——那是 UTF-16 码元）。 */
export function nameCodepointLength(name: string): number {
  return Array.from(name).length;
}

/** 名称是否合法（去首尾空白后 1–100 码点）。 */
export function isValidName(name: string): boolean {
  const length = nameCodepointLength(name.trim());
  return length >= NAME_MIN_CODEPOINTS && length <= NAME_MAX_CODEPOINTS;
}

/** 源码 UTF-8 字节数（设计 Q8：预算按字节，非字符）。 */
export function sourceByteLength(source: string): number {
  return new TextEncoder().encode(source).length;
}

/** 源码是否在 1 MiB 预算内。 */
export function isSourceWithinBudget(source: string): boolean {
  return sourceByteLength(source) <= SOURCE_MAX_BYTES;
}

/** 分页 + 列表（list_scripts；排序由服务端保证 created_at↑ + id↑）。 */
export function listScripts(options: ListScriptsOptions = {}): Promise<ScriptPage> {
  const params: Record<string, unknown> = {};
  if (options.offset !== undefined) {
    params.offset = options.offset;
  }
  if (options.limit !== undefined) {
    params.limit = options.limit;
  }
  return request<ScriptPage>("list_scripts", params);
}

/** 取脚本元数据 + 源码（load_script；不存在 → IpcError{SCRIPT_NOT_FOUND}）。 */
export function loadScript(scriptId: string): Promise<LoadedScript> {
  return request<LoadedScript>("load_script", { script_id: scriptId });
}

/** 新建/更新脚本（save_script；更新须同时给 scriptId+expectedRevision）。 */
export async function saveScript(params: SaveScriptParams): Promise<Script> {
  const body: Record<string, unknown> = {
    name: params.name,
    source: params.source,
    parameters: params.parameters,
  };
  // 仅当明确给出时带上键（缺省=新建；只给其一由引擎判 INVALID_PARAMS）。
  if (params.scriptId !== undefined) {
    body.script_id = params.scriptId;
  }
  if (params.expectedRevision !== undefined) {
    body.expected_revision = params.expectedRevision;
  }
  const data = await request<{ script: Script }>("save_script", body);
  return data.script;
}

/** 删除脚本（delete_script；活动引用 → IpcError{IN_USE}；源码资产保留）。 */
export async function deleteScript(scriptId: string, expectedRevision: number): Promise<void> {
  await request<{ ok: boolean }>("delete_script", {
    script_id: scriptId,
    expected_revision: expectedRevision,
  });
}

/** 仅编译语法校验（validate_script；lupa 缺失 → IpcError{ENGINE_UNAVAILABLE}）。 */
export function validateScript(source: string): Promise<ValidationResult> {
  return request<ValidationResult>("validate_script", { source });
}
