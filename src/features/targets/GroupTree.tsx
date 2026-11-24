/**
 * 三层分组树（TASK-011）：新建 / 改名 / 搬移 / 删除。
 *
 * - 层级 0..2（最多 3 层）；新增子组仅允许父 depth ≤ 1（子 = 父+1 ≤ 2）；
 * - 搬移用"键是否出现"语义：`parentGroupId=null`（键出现）=移至顶层；`parentGroupId=id`
 *   =挂到该组；不改名时不带 `name` 键（键缺省=不搬移/不改名，Q3）；
 * - 删除组：目标移至未分组、子组提升（服务端单事务保证），此处仅触发与刷新；
 * - `name` 计数/校验按 Unicode 码点（`Array.from`），与 TargetEditor 一致。
 */

import { useCallback, useEffect, useState, type ReactElement } from "react";

import { toUserMessage } from "../../lib/ipc/engineClient";
import {
  createGroup,
  deleteGroup,
  isValidName,
  listGroups,
  updateGroup,
  type GroupWithCount,
} from "./api";

interface GroupTreeProps {
  /** 选中分组（null=未分组/全部）时通知上层做列表过滤。 */
  onSelect?: (groupId: string | null) => void;
}

/** 顶层伪父键（parent_group_id=null 的分组）。 */
const ROOT_KEY = "__root__";

function parentKey(group: GroupWithCount): string {
  return group.parent_group_id ?? ROOT_KEY;
}

/** 收集某组的全部后代 id（用于排除非法搬移目标，禁止成环）。 */
function collectDescendants(groups: GroupWithCount[], rootId: string): Set<string> {
  const result = new Set<string>();
  const stack = [rootId];
  while (stack.length > 0) {
    const current = stack.pop();
    if (current === undefined) {
      break;
    }
    for (const group of groups) {
      if (group.parent_group_id === current && !result.has(group.id)) {
        result.add(group.id);
        stack.push(group.id);
      }
    }
  }
  return result;
}

export default function GroupTree({ onSelect }: GroupTreeProps) {
  const [groups, setGroups] = useState<GroupWithCount[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [newRootName, setNewRootName] = useState("");
  const [newChildName, setNewChildName] = useState("");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      setGroups(await listGroups());
    } catch (err) {
      setError(`加载分组失败：${toUserMessage(err)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const run = useCallback(
    async (action: () => Promise<unknown>, okNotice: string): Promise<void> => {
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        await action();
        setNotice(okNotice);
        await reload();
      } catch (err) {
        setError(toUserMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [reload],
  );

  const handleAddRoot = useCallback((): void => {
    if (!isValidName(newRootName)) {
      setError("组名须为 1–100 个字符（Unicode 码点）。");
      return;
    }
    void run(() => createGroup(newRootName.trim()), "已新建顶层分组").then(() =>
      setNewRootName(""),
    );
  }, [newRootName, run]);

  const handleAddChild = useCallback(
    (parentId: string): void => {
      if (!isValidName(newChildName)) {
        setError("组名须为 1–100 个字符（Unicode 码点）。");
        return;
      }
      void run(() => createGroup(newChildName.trim(), parentId), "已新建子分组").then(() =>
        setNewChildName(""),
      );
    },
    [newChildName, run],
  );

  const handleRename = useCallback(
    (group: GroupWithCount): void => {
      if (!isValidName(renameDraft)) {
        setError("组名须为 1–100 个字符（Unicode 码点）。");
        return;
      }
      // 键存在 name（改名），不带 parentGroupId（键缺省=不搬移）
      void run(
        () =>
          updateGroup({
            groupId: group.id,
            expectedRevision: group.revision,
            name: renameDraft.trim(),
          }),
        "已改名",
      ).then(() => setRenaming(null));
    },
    [renameDraft, run],
  );

  const handleMove = useCallback(
    (group: GroupWithCount, parentGroupId: string | null): void => {
      // parentGroupId 键始终出现：null=移至顶层、id=挂到该组（键出现即搬移）
      void run(
        () =>
          updateGroup({
            groupId: group.id,
            expectedRevision: group.revision,
            parentGroupId,
          }),
        parentGroupId === null ? "已移至顶层" : "已搬移",
      );
    },
    [run],
  );

  const handleDelete = useCallback(
    (group: GroupWithCount): void => {
      void run(() => deleteGroup(group.id, group.revision), "已删除分组");
    },
    [run],
  );

  const childrenOf = useCallback(
    (key: string): GroupWithCount[] =>
      groups
        .filter((group) => parentKey(group) === key)
        .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)),
    [groups],
  );

  const renderMoveOptions = useCallback(
    (group: GroupWithCount) => {
      const excluded = collectDescendants(groups, group.id);
      excluded.add(group.id);
      const candidates = groups.filter((g) => g.depth <= 1 && !excluded.has(g.id));
      return (
        <select
          aria-label={`移动 ${group.name}`}
          value={group.parent_group_id ?? ROOT_KEY}
          disabled={busy}
          onChange={(event) => {
            const value = event.target.value;
            handleMove(group, value === ROOT_KEY ? null : value);
          }}
        >
          <option value={ROOT_KEY}>顶层</option>
          {candidates.map((candidate) => (
            <option key={candidate.id} value={candidate.id}>
              {"　".repeat(candidate.depth)}
              {candidate.name}
            </option>
          ))}
        </select>
      );
    },
    [busy, groups, handleMove],
  );

  const renderNode = (group: GroupWithCount): ReactElement => {
    const children = childrenOf(group.id);
    return (
      <li key={group.id}>
        <div className="group-row">
          <button type="button" onClick={() => onSelect?.(group.id)} disabled={busy}>
            {group.name}
          </button>
          <span className="group-meta">
            （depth {group.depth}，成员 {group.target_count}，rev {group.revision}）
          </span>
          {renaming === group.id ? (
            <>
              <input
                type="text"
                value={renameDraft}
                onChange={(event) => setRenameDraft(event.target.value)}
              />
              <button type="button" onClick={() => handleRename(group)} disabled={busy}>
                确认
              </button>
              <button type="button" onClick={() => setRenaming(null)} disabled={busy}>
                取消
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => {
                  setRenaming(group.id);
                  setRenameDraft(group.name);
                }}
                disabled={busy}
              >
                改名
              </button>
              {renderMoveOptions(group)}
              <button type="button" onClick={() => handleDelete(group)} disabled={busy}>
                删除
              </button>
            </>
          )}
        </div>
        {group.depth < 2 && (
          <div className="group-add-child">
            <input
              type="text"
              value={newChildName}
              placeholder="新子组名"
              onChange={(event) => setNewChildName(event.target.value)}
            />
            <button type="button" onClick={() => handleAddChild(group.id)} disabled={busy}>
              加子组
            </button>
          </div>
        )}
        {children.length > 0 && <ul>{children.map((child) => renderNode(child))}</ul>}
      </li>
    );
  };

  return (
    <section className="group-tree" aria-label="分组树">
      <h2>分组树（TASK-011，最多 3 层）</h2>

      <div className="group-add-root">
        <input
          type="text"
          value={newRootName}
          placeholder="新顶层组名"
          onChange={(event) => setNewRootName(event.target.value)}
        />
        <button type="button" onClick={handleAddRoot} disabled={busy}>
          新建顶层组
        </button>
      </div>

      {error !== null && <p role="alert">{error}</p>}
      {notice !== null && <p role="status">{notice}</p>}
      {loading && <p role="status">加载中…</p>}

      {childrenOf(ROOT_KEY).length === 0 && !loading ? (
        <p>暂无分组。</p>
      ) : (
        <ul>{childrenOf(ROOT_KEY).map((group) => renderNode(group))}</ul>
      )}
    </section>
  );
}
