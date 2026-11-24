/**
 * 脚本列表状态（TASK-012）：列表 / 分页 + 编辑草稿（源码 + 参数）+ 乐观锁（revision）写入。
 *
 * - 纯 TS 可观察 store（不依赖 React 运行时），配套 `useScriptStore` 以
 *   `useSyncExternalStore` 接入组件；
 * - 写入命令（save/delete）统一携带 `expected_revision`（更新时）；`CONFLICT`
 *   时 **保留草稿**（`conflict` 标志供 UI 提示"重新加载后重试"），不静默覆盖；
 * - `validate` 仅编译语法诊断（不落盘、不改 generation）；`ENGINE_UNAVAILABLE`
 *   经 `error` 暴露（诚实降级，不伪报 valid）。
 */

import { useSyncExternalStore } from "react";

import type { IpcError } from "../../lib/ipc/types";
import type { Script, ScriptParameter } from "../../lib/types/conditions";
import {
  deleteScript,
  listScripts,
  loadScript,
  saveScript,
  validateScript,
  PAGE_DEFAULT_LIMIT,
  type ListScriptsOptions,
  type SaveScriptParams,
  type ValidationResult,
} from "./api";

/** 编辑草稿（`id`/`revision` 为空表示新建）。 */
export interface ScriptDraft {
  id: string | null;
  revision: number | null;
  name: string;
  source: string;
  parameters: ScriptParameter[];
}

/** 空草稿（新建）。 */
export function emptyDraft(): ScriptDraft {
  return { id: null, revision: null, name: "", source: "", parameters: [] };
}

/** 从已加载脚本构造草稿（编辑既有）。 */
export function draftFromLoaded(script: Script, source: string): ScriptDraft {
  return {
    id: script.id,
    revision: script.revision,
    name: script.name,
    source,
    parameters: script.parameters.map((item) => ({ ...item })),
  };
}

/** 列表状态快照（不可变对象，每次变更替换引用以供 useSyncExternalStore 比较）。 */
export interface ScriptStoreState {
  items: Script[];
  total: number;
  offset: number;
  limit: number;
  loading: boolean;
  busy: boolean;
  error: IpcError | null;
  conflict: boolean;
  /** 当前编辑草稿。 */
  draft: ScriptDraft;
  /** 最近一次校验结果（null=尚未校验）。 */
  validation: ValidationResult | null;
}

const INITIAL_STATE: ScriptStoreState = {
  items: [],
  total: 0,
  offset: 0,
  limit: PAGE_DEFAULT_LIMIT,
  loading: false,
  busy: false,
  error: null,
  conflict: false,
  draft: emptyDraft(),
  validation: null,
};

/** invoke reject 值 → 规范 IpcError（防御非规范抛值）。 */
function asIpcError(err: unknown): IpcError {
  if (err && typeof err === "object") {
    const candidate = err as Partial<IpcError>;
    if (typeof candidate.code === "string" && candidate.code.length > 0) {
      return {
        code: candidate.code,
        message:
          typeof candidate.message === "string" && candidate.message.length > 0
            ? candidate.message
            : candidate.code,
        details: candidate.details,
        retryable: candidate.retryable === true,
      };
    }
  }
  if (err instanceof Error && err.message.length > 0) {
    return { code: "INTERNAL_ERROR", message: err.message, retryable: false };
  }
  return { code: "INTERNAL_ERROR", message: "未知错误", retryable: false };
}

/** 脚本列表可观察 store（不依赖 React；组件经 `useScriptStore` 订阅）。 */
export class ScriptStore {
  private state: ScriptStoreState = { ...INITIAL_STATE };
  private readonly listeners = new Set<() => void>();

  /** 当前快照（稳定引用，仅变更时替换）。 */
  readonly getState = (): ScriptStoreState => this.state;

  /** 订阅变更（返回退订函数；useSyncExternalStore 约定）。 */
  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private setState(patch: Partial<ScriptStoreState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of [...this.listeners]) {
      listener();
    }
  }

  /** 按当前分页拉取列表。 */
  async load(): Promise<void> {
    this.setState({ loading: true, error: null, conflict: false });
    try {
      const options: ListScriptsOptions = {
        offset: this.state.offset,
        limit: this.state.limit,
      };
      const page = await listScripts(options);
      this.setState({ items: page.items, total: page.total, loading: false });
    } catch (err) {
      this.setState({ loading: false, error: asIpcError(err) });
    }
  }

  /** 设置分页偏移（>=0；越界由服务端返回空页）。 */
  setOffset(offset: number): void {
    this.setState({ offset: Math.max(0, offset) });
    void this.load();
  }

  /** 开始新建（重置草稿为空）。 */
  startCreate(): void {
    this.setState({ draft: emptyDraft(), validation: null, error: null, conflict: false });
  }

  /** 开始编辑（拉取元数据 + 源码填充草稿）。 */
  async startEdit(scriptId: string): Promise<boolean> {
    try {
      const loaded = await loadScript(scriptId);
      this.setState({
        draft: draftFromLoaded(loaded.script, loaded.source),
        validation: null,
        error: null,
        conflict: false,
      });
      return true;
    } catch (err) {
      this.setState({ error: asIpcError(err) });
      return false;
    }
  }

  /** 更新草稿字段（局部覆盖）。 */
  updateDraft(patch: Partial<ScriptDraft>): void {
    this.setState({ draft: { ...this.state.draft, ...patch } });
  }

  /** 对草稿源码执行仅编译语法校验（不落盘）。 */
  async validate(): Promise<ValidationResult | null> {
    this.setState({ busy: true, error: null, conflict: false });
    try {
      const result = await validateScript(this.state.draft.source);
      this.setState({ busy: false, validation: result });
      return result;
    } catch (err) {
      // ENGINE_UNAVAILABLE 等：诚实暴露错误，不置 valid
      this.setState({ busy: false, error: asIpcError(err), validation: null });
      return null;
    }
  }

  /** 保存草稿（新建/更新；更新携带 revision 乐观锁；成功后重载列表）。 */
  async save(): Promise<boolean> {
    const draft = this.state.draft;
    const params: SaveScriptParams = {
      name: draft.name,
      source: draft.source,
      parameters: draft.parameters,
    };
    if (draft.id !== null && draft.revision !== null) {
      params.scriptId = draft.id;
      params.expectedRevision = draft.revision;
    }
    return this.mutate(() => saveScript(params));
  }

  /** 删除脚本（乐观锁）。 */
  async remove(scriptId: string, expectedRevision: number): Promise<boolean> {
    return this.mutate(() => deleteScript(scriptId, expectedRevision));
  }

  /** 清除最近错误 / 冲突标志（重试入口调用）。 */
  clearError(): void {
    this.setState({ error: null, conflict: false });
  }

  private async mutate(operation: () => Promise<unknown>): Promise<boolean> {
    this.setState({ busy: true, error: null, conflict: false });
    try {
      await operation();
      await this.load();
      this.setState({ busy: false });
      return true;
    } catch (err) {
      const error = asIpcError(err);
      // 乐观锁冲突：保留草稿、置冲突标志（调用方刷新基线后重试），不静默覆盖。
      this.setState({ busy: false, error, conflict: error.code === "CONFLICT" });
      return false;
    }
  }
}

/** 新建 store 实例（组件或测试持有）。 */
export function createScriptStore(): ScriptStore {
  return new ScriptStore();
}

/** React 订阅钩子（useSyncExternalStore：快照稳定引用，避免多余渲染）。 */
export function useScriptStore(store: ScriptStore): ScriptStoreState {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}
