/**
 * 目标列表面板（TASK-011）：分页 + 名称查询 + 分组过滤（Q5 三态）+ 行操作。
 *
 * - 数据/分页/乐观锁状态由 `TargetStore` 持有，组件经 `useTargetStore` 订阅；
 * - 分组过滤值：`undefined`=不过滤、`null`=仅未分组、string=精确该组（不递归）；
 * - 行操作（编辑/删除/移组）携带实体 `revision` 作乐观锁；冲突经 store.conflict 提示。
 */

import { useCallback, useEffect, useState } from "react";

import { toUserMessage } from "../../lib/ipc/engineClient";
import type { Target } from "../../lib/types/conditions";
import type { GroupWithCount } from "./api";
import { useTargetStore } from "./store";
import type { TargetStore } from "./store";

interface TargetListProps {
  store: TargetStore;
  /** 分组列表（供过滤下拉与移组选项）。 */
  groups: GroupWithCount[];
  /** 请求编辑某目标（交由 TargetEditor 打开）。 */
  onEdit?: (target: Target) => void;
}

/** 分组过滤三态 → 下拉 string 编码（""=不过滤、"__null__"=未分组、其余=组 id）。 */
const FILTER_ALL = "";
const FILTER_UNGROUPED = "__ungrouped__";

export default function TargetList({ store, groups, onEdit }: TargetListProps) {
  const state = useTargetStore(store);
  const [queryDraft, setQueryDraft] = useState(state.query);
  const [filterCode, setFilterCode] = useState<string>(
    state.groupFilter === null ? FILTER_UNGROUPED : (state.groupFilter ?? FILTER_ALL),
  );

  // 首次挂载拉取列表
  useEffect(() => {
    void store.load();
  }, [store]);

  const handleSearch = useCallback((): void => {
    store.setQuery(queryDraft);
  }, [store, queryDraft]);

  const handleFilterChange = useCallback(
    (code: string): void => {
      setFilterCode(code);
      if (code === FILTER_ALL) {
        store.setGroupFilter(undefined);
      } else if (code === FILTER_UNGROUPED) {
        store.setGroupFilter(null);
      } else {
        store.setGroupFilter(code);
      }
    },
    [store],
  );

  const handleDelete = useCallback(
    (target: Target): void => {
      void store.remove(target.id, target.revision);
    },
    [store],
  );

  const pageStart = state.offset + 1;
  const pageEnd = state.offset + state.items.length;
  const canPrev = state.offset > 0;
  const canNext = state.offset + state.limit < state.total;

  return (
    <section className="target-list" aria-label="目标列表">
      <h2>目标列表（TASK-011）</h2>
      <div className="target-list-toolbar">
        <input
          type="search"
          value={queryDraft}
          placeholder="按名称搜索"
          onChange={(event) => setQueryDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              handleSearch();
            }
          }}
        />
        <button type="button" onClick={handleSearch} disabled={state.loading}>
          搜索
        </button>
        <select
          value={filterCode}
          onChange={(event) => handleFilterChange(event.target.value)}
          aria-label="分组过滤"
        >
          <option value={FILTER_ALL}>全部分组</option>
          <option value={FILTER_UNGROUPED}>仅未分组</option>
          {groups.map((group) => (
            <option key={group.id} value={group.id}>
              {"　".repeat(group.depth)}
              {group.name}
            </option>
          ))}
        </select>
      </div>

      {state.error !== null && (
        <p role="alert">
          {state.conflict ? "版本冲突：请重新加载后再试。" : `加载失败：${toUserMessage(state.error)}`}
        </p>
      )}
      {state.loading && <p role="status">加载中…</p>}

      <table>
        <thead>
          <tr>
            <th scope="col">编号</th>
            <th scope="col">名称</th>
            <th scope="col">分组</th>
            <th scope="col">revision</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {state.items.map((target) => (
            <tr key={target.id}>
              <td>{target.sample_no}</td>
              <td>{target.name}</td>
              <td>{target.group_id ?? "未分组"}</td>
              <td>{target.revision}</td>
              <td>
                {onEdit !== undefined && (
                  <button type="button" onClick={() => onEdit(target)} disabled={state.busy}>
                    编辑
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => handleDelete(target)}
                  disabled={state.busy}
                >
                  删除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {state.items.length === 0 && !state.loading && <p>无目标。</p>}

      <div className="target-list-pager">
        <button
          type="button"
          onClick={() => store.setOffset(Math.max(0, state.offset - state.limit))}
          disabled={!canPrev || state.loading}
        >
          上一页
        </button>
        <span>
          {state.total === 0 ? "0 / 0" : `${pageStart}–${pageEnd} / ${state.total}`}
        </span>
        <button
          type="button"
          onClick={() => store.setOffset(state.offset + state.limit)}
          disabled={!canNext || state.loading}
        >
          下一页
        </button>
      </div>
    </section>
  );
}
