/**
 * 目标列表状态（TASK-011）：列表 / 分页 / 查询 / 分组过滤 + 乐观锁（revision）写入。
 *
 * - 纯 TS 可观察 store（不依赖 React 运行时），配套 `useTargetStore` 以
 *   `useSyncExternalStore` 接入组件；
 * - 写入命令（create/update/move/delete）统一携带 `expected_revision`；`CONFLICT`
 *   时 **保留草稿**（调用方据 `conflict` 标志展示"重新加载后重试"），不静默覆盖；
 * - 任何写入成功后重新 `load()` 以拿到服务端一致快照（响应表示已落盘，03）。
 */

import { useSyncExternalStore } from "react";

import type { IpcError } from "../../lib/ipc/types";
import type { Target } from "../../lib/types/conditions";
import {
  createTarget,
  deleteTarget,
  getTarget,
  listTargets,
  moveTargetToGroup,
  updateTarget,
  type ListTargetsOptions,
  type TargetDraft,
  type UpdateTargetPatch,
} from "./api";

/** 分页缺省（03：limit=100；命令层限制 1–500）。 */
export const DEFAULT_PAGE_LIMIT = 100;

/** 列表状态快照（不可变对象，每次变更替换引用以供 useSyncExternalStore 比较）。 */
export interface TargetStoreState {
  items: Target[];
  total: number;
  offset: number;
  limit: number;
  query: string;
  /** undefined=不过滤；null=仅未分组；string=精确该组（Q5）。 */
  groupFilter: string | null | undefined;
  /** 查询在途。 */
  loading: boolean;
  /** 最近一次写入在途（禁用交互）。 */
  busy: boolean;
  /** 最近一次操作错误（含乐观锁 CONFLICT）。 */
  error: IpcError | null;
  /** 乐观锁冲突标志（供 UI 提示"重新加载后重试"）。 */
  conflict: boolean;
}

const INITIAL_STATE: TargetStoreState = {
  items: [],
  total: 0,
  offset: 0,
  limit: DEFAULT_PAGE_LIMIT,
  query: "",
  groupFilter: undefined,
  loading: false,
  busy: false,
  error: null,
  conflict: false,
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

/** 目标列表可观察 store（不依赖 React；组件经 `useTargetStore` 订阅）。 */
export class TargetStore {
  private state: TargetStoreState = { ...INITIAL_STATE };
  private readonly listeners = new Set<() => void>();

  /** 当前快照（稳定引用，仅变更时替换）。 */
  readonly getState = (): TargetStoreState => this.state;

  /** 订阅变更（返回退订函数；useSyncExternalStore 约定）。 */
  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private setState(patch: Partial<TargetStoreState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of [...this.listeners]) {
      listener();
    }
  }

  /** 按当前过滤/分页拉取列表。 */
  async load(): Promise<void> {
    this.setState({ loading: true, error: null, conflict: false });
    try {
      const options: ListTargetsOptions = {
        offset: this.state.offset,
        limit: this.state.limit,
      };
      if (this.state.query !== "") {
        options.query = this.state.query;
      }
      if (this.state.groupFilter !== undefined) {
        options.groupId = this.state.groupFilter;
      }
      const page = await listTargets(options);
      this.setState({ items: page.items, total: page.total, loading: false });
    } catch (err) {
      this.setState({ loading: false, error: asIpcError(err) });
    }
  }

  /** 设置分组过滤（undefined/null/string；重置到首屏后重载）。 */
  setGroupFilter(groupFilter: string | null | undefined): void {
    this.setState({ groupFilter, offset: 0 });
    void this.load();
  }

  /** 设置搜索关键字（子串匹配名称；重置到首屏后重载）。 */
  setQuery(query: string): void {
    this.setState({ query, offset: 0 });
    void this.load();
  }

  /** 设置分页偏移（>=0；越界由服务端返回空页）。 */
  setOffset(offset: number): void {
    this.setState({ offset: Math.max(0, offset) });
    void this.load();
  }

  /** 新建目标（成功后重载）。 */
  async create(draft: TargetDraft): Promise<boolean> {
    return this.mutate(() => createTarget(draft));
  }

  /** 更新目标（乐观锁；成功后重载）。 */
  async update(
    targetId: string,
    patch: UpdateTargetPatch,
    expectedRevision: number,
  ): Promise<boolean> {
    return this.mutate(() => updateTarget(targetId, patch, expectedRevision));
  }

  /** 移动目标到分组（`groupId=null` → 未分组；乐观锁）。 */
  async move(targetId: string, groupId: string | null, expectedRevision: number): Promise<boolean> {
    return this.mutate(() => moveTargetToGroup(targetId, groupId, expectedRevision));
  }

  /** 删除目标（乐观锁）。 */
  async remove(targetId: string, expectedRevision: number): Promise<boolean> {
    return this.mutate(() => deleteTarget(targetId, expectedRevision));
  }

  /** 读取单个目标最新版本（供冲突后刷新草稿基线；失败返回 null）。 */
  async refresh(targetId: string): Promise<Target | null> {
    try {
      return await getTarget(targetId);
    } catch {
      return null;
    }
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
export function createTargetStore(): TargetStore {
  return new TargetStore();
}

/** React 订阅钩子（useSyncExternalStore：快照稳定引用，避免多余渲染）。 */
export function useTargetStore(store: TargetStore): TargetStoreState {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}
