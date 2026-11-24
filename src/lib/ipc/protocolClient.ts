/**
 * 统一 IPC 客户端（TASK-005 设计 §3.3 protocolClient）。
 *
 * 职责（§3.3/§4.4/§6.3/§6.4）：
 * - `request<T>(command, params, opts?)`：ipc_request invoke 封装；id 生成 `ui-<n>`
 *   （会话锚重置）；Promise 超时 = 服务端期限 + 500ms 余量，超时≠取消成功提示；
 * - `cancelRequest / cancelJob`：取消入口（ipc_cancel 转发 cancel_request；cancel_job
 *   走控制类命令路由）；
 * - 事件订阅 `onEvent(name, handler)`：Tauri 事件名 = 协议 name；集中做 session
 *   过滤（旧会话迟到帧丢弃）、seq 连续性检查与缺口恢复钩子（§6.4）；
 * - `onGap(cb)`：业务层 resync 通知（骨架期日志 + 状态刷新，绝不重放输入）；
 * - system_ready 收到 → 会话重建锚：lastSeq 归零、ui-id 计数器重置（§6.4）。
 *
 * 不做：响应缓存/去重/在途容量（Rust pending.rs 职责）；自动重试（06/R08）。
 */

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

import { engineStatus } from "./engineClient";
import type { CancelResult, EventName, IpcError, ProtocolEnvelope } from "./types";
import type { ConfigDocument, ConfigPatch } from "../types/config";

// ---------------------------------------------------------------------------
// 常量（03/06：普通命令期限 5s；hello/引擎启动 30s 不经 ipc_request——引擎启动
// 走 engine_start 运维命令，不在此重复计时，§6.3）
// ---------------------------------------------------------------------------

/** 服务端普通命令响应期限（03：普通命令 5s）。 */
export const NORMAL_DEADLINE_MS = 5000;
/** 客户端 Promise 兜底超时 = 服务端期限 + 500ms 余量（§3.3：确保服务端超时先到）。 */
export const CLIENT_TIMEOUT_MARGIN_MS = 500;

// ---------------------------------------------------------------------------
// 会话与请求计数状态（单窗口应用，模块级即可；system_ready 锚点重置）
// ---------------------------------------------------------------------------

let nextRequestSeq = 1;
let currentSessionId: string | null = null;
let lastSeq = 0;
/** 是否已锚定会话（system_ready 或首个事件采纳，见 dispatch 注释）。 */
let anchored = false;

// ---------------------------------------------------------------------------
// 请求
// ---------------------------------------------------------------------------

export interface RequestOptions {
  /** 覆盖默认服务端期限+余量的客户端超时（ms）。 */
  timeoutMs?: number;
}

/** invoke reject 值 → 规范 IpcError（兼容对象/字符串/原生 Error）。 */
function normalizeIpcError(err: unknown): IpcError {
  if (err && typeof err === "object") {
    const e = err as Partial<IpcError>;
    if (typeof e.code === "string" && e.code.length > 0) {
      return {
        code: e.code,
        message: typeof e.message === "string" && e.message.length > 0 ? e.message : e.code,
        details: e.details,
        retryable: e.retryable === true,
      };
    }
  }
  if (typeof err === "string" && err.length > 0) {
    return { code: "INTERNAL_ERROR", message: err, retryable: false };
  }
  if (err instanceof Error && err.message.length > 0) {
    return { code: "INTERNAL_ERROR", message: err.message, retryable: false };
  }
  return { code: "INTERNAL_ERROR", message: "未知错误", retryable: false };
}

/**
 * 泛化 IPC 请求（§5.1 ipc_request）。id 由本客户端生成 `ui-<n>`（会话内唯一由
 * Rust 去重表裁决）；服务端超时以 IpcError 拒绝，客户端兜底超时仅在 Rust 命令
 * 通道本身失联时触发（§4.2 单线程 Router 正常时不会发生）。
 */
export function request<T = unknown>(
  command: string,
  params: Record<string, unknown> = {},
  opts: RequestOptions = {},
): Promise<T> {
  const id = `ui-${nextRequestSeq}`;
  nextRequestSeq += 1;
  const timeoutMs = opts.timeoutMs ?? NORMAL_DEADLINE_MS + CLIENT_TIMEOUT_MARGIN_MS;
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject({
        code: "INTERNAL_ERROR",
        message:
          `请求超时（期限 ${timeoutMs}ms）：Promise 超时不等于后台取消成功，` +
          `可用 query_request 查询状态（request_id=${id}）`,
        details: { request_id: id, command },
        retryable: false,
      } satisfies IpcError);
    }, timeoutMs);
    invoke<unknown>("ipc_request", { id, command, params })
      .then((data) => resolve(data as T))
      .catch((err: unknown) => reject(normalizeIpcError(err)))
      .finally(() => clearTimeout(timer));
  });
}

/** ipc_cancel data 归一（§5.1：data = {cancelled, reason}，幂等）。 */
function normalizeCancelResult(data: unknown): CancelResult {
  const obj = (data ?? {}) as Partial<CancelResult>;
  return {
    cancelled: obj.cancelled === true,
    reason: typeof obj.reason === "string" ? obj.reason : "",
  };
}

/** 取消在途请求（对应协议 cancel_request；幂等；控制类通道不受 32 限制）。 */
export async function cancelRequest(requestId: string): Promise<CancelResult> {
  const data = await invoke<unknown>("ipc_cancel", { requestId });
  return normalizeCancelResult(data);
}

/** 取消识别 job（控制类命令路由；data={cancelled}，终态取消为 false，03）。 */
export function cancelJob(jobId: string): Promise<{ cancelled: boolean }> {
  return request<{ cancelled: boolean }>("cancel_job", { job_id: jobId });
}

// ---------------------------------------------------------------------------
// 事件订阅（Tauri 事件名 = 协议 name）与会话重建锚（§4.4/§6.4）
// ---------------------------------------------------------------------------

type EventHandler = (envelope: ProtocolEnvelope) => void;

/** seq 缺口信息（§6.4：expected=last_seq+1，actual=实际收到）。 */
export interface GapInfo {
  expected: number;
  actual: number;
}
type GapHandler = (info: GapInfo) => void;

const handlers = new Map<EventName, Set<EventHandler>>();
const nativeListeners = new Map<EventName, UnlistenFn>();
let gapHandler: GapHandler | null = null;

/**
 * 注册 seq 缺口恢复钩子（§6.4 onGap ②：业务层 resync 通知）。单钩子（后注册
 * 覆盖前者）；返回取消注册函数。骨架期 App 以"日志 + engine_status 刷新"响应，
 * 绝不重放输入（03）。
 */
export function onGap(handler: GapHandler): () => void {
  gapHandler = handler;
  return () => {
    if (gapHandler === handler) {
      gapHandler = null;
    }
  };
}

/**
 * 订阅协议事件（§4.4：payload = UI seq 重分配后的完整信封）。
 * 首个订阅者懒建 Tauri listen；全部退订后自动解除。返回同步取消订阅函数。
 */
export function onEvent(name: EventName, handler: EventHandler): () => void {
  let set = handlers.get(name);
  if (set === undefined) {
    set = new Set();
    handlers.set(name, set);
  }
  set.add(handler);
  if (!nativeListeners.has(name)) {
    void listen<ProtocolEnvelope>(name, (ev) => dispatch(ev.payload)).then((unlisten) => {
      const current = handlers.get(name);
      if (current === undefined || current.size === 0) {
        // 订阅者在 listen 就绪前已全部退订 → 立即解除，避免泄漏
        unlisten();
      } else {
        nativeListeners.set(name, unlisten);
      }
    });
  }
  return () => {
    const current = handlers.get(name);
    if (current !== undefined) {
      current.delete(handler);
      if (current.size === 0) {
        handlers.delete(name);
        const unlisten = nativeListeners.get(name);
        if (unlisten !== undefined) {
          unlisten();
          nativeListeners.delete(name);
        }
      }
    }
  };
}

/** seq 推进（防御：信封无 seq 时不推进，仅 system_ready 锚点允许归零）。 */
function advanceSeq(seq: number | undefined): void {
  if (typeof seq === "number") {
    lastSeq = seq;
  }
}

/** 缺口上报（§6.4 onGap）：①engine_status 重查引擎健康；②resync 通知业务层。 */
function reportGap(expected: number, actual: number): void {
  void engineStatus().catch(() => undefined); // ① 只读重查；失败由 UI 层 notice 呈现
  gapHandler?.({ expected, actual }); // ② 业务层 resync（骨架期：日志+状态刷新）
}

/** 分发给该事件名的全部 handler（拷贝数组防迭代中退订；handler 异常不外溢）。 */
function deliver(name: EventName, envelope: ProtocolEnvelope): void {
  const set = handlers.get(name);
  if (set === undefined) {
    return;
  }
  for (const handler of [...set]) {
    try {
      handler(envelope);
    } catch (err) {
      console.error(`[ipc] 事件 ${name} handler 异常`, err);
    }
  }
}

/** 事件分发主路径：类型防御 → 会话锚/过滤 → seq 连续性 → 投递。 */
function dispatch(envelope: ProtocolEnvelope): void {
  if (envelope.type !== "event" || typeof envelope.name !== "string") {
    return; // 事件总线只承载 event 信封（防御）
  }
  const name = envelope.name as EventName;

  if (name === "system_ready") {
    // 会话重建锚（§6.4）：lastSeq 归零、ui-id 计数器重置、按新 session 过滤后续帧
    currentSessionId = envelope.session_id;
    lastSeq = 0;
    nextRequestSeq = 1;
    anchored = true;
    advanceSeq(envelope.seq); // system_ready 即新会话首事件（seq=1）
    deliver(name, envelope);
    return;
  }

  if (!anchored || currentSessionId === null) {
    // 未锚定（UI 重载后错过 system_ready）：采纳当前唯一活跃会话；
    // 首帧无法做连续性检查，以其 seq 为基线（超出设计明文的务实处理，见回报）
    currentSessionId = envelope.session_id;
    anchored = true;
    advanceSeq(envelope.seq);
    deliver(name, envelope);
    return;
  }

  if (envelope.session_id !== currentSessionId) {
    return; // 旧会话迟到帧：丢弃（03"旧会话消息全部拒绝"），不计缺口
  }

  const seq = typeof envelope.seq === "number" ? envelope.seq : null;
  if (seq !== null && seq !== lastSeq + 1) {
    reportGap(lastSeq + 1, seq); // 缺口：不重放输入，重查+通知（§6.4）
  }
  advanceSeq(seq ?? undefined);
  deliver(name, envelope);
}

// ---------------------------------------------------------------------------
// 配置三命令（TASK-006 §2.3/§5.2：经既有 ipc_request 泛化面转发 Python，
// 不新增 tauri command；错误四字段 IpcError 管道复用）
// ---------------------------------------------------------------------------

/**
 * config 命令响应解包（BUG-1 修复，QA Round 1 发现）。
 *
 * 03 命令目录权威：`get_config → {config:Config}`、`update_config/reset_config
 * → {config:Config}`——ipc_request 成功解析值为 `data={config: ConfigDocument}`
 * 嵌套形态（Python dispatcher 返回 {"config": …}，Rust 探针按 doc["config"]
 * 取值），**不是**裸 ConfigDocument。此前三封装直接把 data 当 ConfigDocument
 * 返回，调用方读 doc.revision 得 undefined → updateConfig(patch, undefined)
 * 必报 INVALID_PARAMS。
 *
 * 本函数解包 data.config 返回真正文档（与设计 §5.2"返回完整 Config 文档"
 * 意图一致）；运行时防御：data 无 config 键或形态异常（非对象）按 IpcError
 * 管道抛 PROTOCOL_ERROR（不返回 undefined）。export 供契约自验入口做防回归
 * 断言（reports/TASK-006/raw/ts-contract-entry.ts）。
 */
export function unwrapConfigDocument(data: unknown): ConfigDocument {
  if (typeof data === "object" && data !== null && !Array.isArray(data)) {
    const doc = (data as Record<string, unknown>).config;
    if (typeof doc === "object" && doc !== null && !Array.isArray(doc)) {
      return doc as ConfigDocument;
    }
  }
  throw {
    code: "PROTOCOL_ERROR",
    message: "config 命令响应缺少 config 字段（协议形态异常）",
    details: { command_response_shape: typeof data },
    retryable: false,
  } satisfies IpcError;
}

/** 三命令共用：请求 + 解包（异常形态经 IpcError 管道 reject）。 */
async function requestConfig(
  command: string,
  params: Record<string, unknown>,
): Promise<ConfigDocument> {
  return unwrapConfigDocument(await request<unknown>(command, params));
}

/**
 * 获取完整配置文档（get_config）。
 * §15.5 裁决：params 必须为空对象——严格白名单，本端恒传 `{}`。
 */
export function getConfig(): Promise<ConfigDocument> {
  return requestConfig("get_config", {});
}

/**
 * 深合并更新配置（update_config；§5.4：对象递归合并、标量替换、数组整体替换、
 * 可 null 字段显式置 null）。revision 冲突 → CONFLICT（重新 get 后人工重提）；
 * 非法 patch → INVALID_PARAMS{details:{field}}（旧配置不变，§8）。
 */
export function updateConfig(patch: ConfigPatch, expectedRevision: number): Promise<ConfigDocument> {
  // params 严格白名单：{expected_revision, patch}
  return requestConfig("update_config", {
    expected_revision: expectedRevision,
    patch,
  });
}

/** 重置为默认配置（reset_config；内容=默认值、revision 递增；不删除目标/脚本，03）。 */
export function resetConfig(expectedRevision: number): Promise<ConfigDocument> {
  // params 严格白名单：{expected_revision}
  return requestConfig("reset_config", { expected_revision: expectedRevision });
}
