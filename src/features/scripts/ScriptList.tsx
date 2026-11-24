/**
 * 脚本列表面板（TASK-012）：分页 + 行操作（编辑/删除）。
 *
 * - 数据/分页/乐观锁状态由 `ScriptStore` 持有，组件经 `useScriptStore` 订阅；
 * - 排序由服务端保证（created_at↑ + id↑，Q6）；行删除携带实体 `revision` 作乐观锁；
 * - 冲突经 `store.conflict` 提示"重新加载后重试"。
 */

import { useCallback, useEffect } from "react";

import { toUserMessage } from "../../lib/ipc/engineClient";
import type { Script } from "../../lib/types/conditions";
import { useScriptStore } from "./store";
import type { ScriptStore } from "./store";

interface ScriptListProps {
  store: ScriptStore;
  /** 请求编辑某脚本（交由 ScriptEditor 打开）。 */
  onEdit?: (script: Script) => void;
}

export default function ScriptList({ store, onEdit }: ScriptListProps) {
  const state = useScriptStore(store);

  // 首次挂载拉取列表
  useEffect(() => {
    void store.load();
  }, [store]);

  const handleDelete = useCallback(
    (script: Script): void => {
      void store.remove(script.id, script.revision);
    },
    [store],
  );

  const pageStart = state.offset + 1;
  const pageEnd = state.offset + state.items.length;
  const canPrev = state.offset > 0;
  const canNext = state.offset + state.limit < state.total;

  return (
    <section className="script-list" aria-label="脚本列表">
      <h2>脚本列表（TASK-012）</h2>

      {state.error !== null && (
        <p role="alert">
          {state.conflict ? "版本冲突：请重新加载后再试。" : `加载失败：${toUserMessage(state.error)}`}
        </p>
      )}
      {state.loading && <p role="status">加载中…</p>}

      <table>
        <thead>
          <tr>
            <th scope="col">名称</th>
            <th scope="col">revision</th>
            <th scope="col">参数数</th>
            <th scope="col">source_sha256</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {state.items.map((script) => (
            <tr key={script.id}>
              <td>{script.name}</td>
              <td>{script.revision}</td>
              <td>{script.parameters.length}</td>
              <td>{script.source_sha256.slice(0, 12)}…</td>
              <td>
                {onEdit !== undefined && (
                  <button type="button" onClick={() => onEdit(script)} disabled={state.busy}>
                    编辑
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => handleDelete(script)}
                  disabled={state.busy}
                >
                  删除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {state.items.length === 0 && !state.loading && <p>无脚本。</p>}

      <div className="script-list-pager">
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
