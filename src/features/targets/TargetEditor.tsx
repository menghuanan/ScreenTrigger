/**
 * 目标草稿编辑器（TASK-011）：新建 / 编辑目标。
 *
 * - 草稿模型（Q4）：**无** `id/sample_no/revision/created_at/updated_at`，且**不含**
 *   `metadata`（改名/编辑元数据走 update_target.patch，此处不暴露）；
 * - `name` 长度按 **Unicode 码点** 校验（`Array.from(name).length`，非 `String.length`）；
 * - 编辑走乐观锁：携带 `target.revision` 作 `expected_revision`；`CONFLICT` 时保留
 *   草稿并提示"重新加载后重试"（不静默覆盖）；
 * - 条件树以 JSON 文本编辑（解析为 ConditionNode；非法 JSON 就地报错，不提交）。
 */

import { useCallback, useMemo, useState } from "react";

import { toUserMessage } from "../../lib/ipc/engineClient";
import type { ConditionNode, Target } from "../../lib/types/conditions";
import {
  isValidName,
  nameCodepointLength,
  normalizeName,
  type GroupWithCount,
  type StorageMode,
  type TargetDraft,
  type UpdateTargetPatch,
} from "./api";
import type { TargetStore } from "./store";
import { useTargetStore } from "./store";

interface TargetEditorProps {
  store: TargetStore;
  groups: GroupWithCount[];
  /** 编辑对象；null/缺省=新建。 */
  target?: Target | null;
  /** 新建所需 image_id（来自导入/捕获的 ImageRef）；编辑时可留空表示不改图。 */
  imageId?: string;
  /** 保存成功回调。 */
  onSaved?: () => void;
  /** 取消回调。 */
  onCancel?: () => void;
}

const DEFAULT_CONDITIONS = JSON.stringify(
  { op: "all", children: [{ id: "c1", type: "template", params: { threshold: 0.85 } }] },
  null,
  2,
);

export default function TargetEditor({
  store,
  groups,
  target = null,
  imageId = "",
  onSaved,
  onCancel,
}: TargetEditorProps) {
  const editing = target !== null;
  const [name, setName] = useState(target?.name ?? "");
  const [groupId, setGroupId] = useState<string>(target?.group_id ?? "");
  const [anchorId, setAnchorId] = useState(target?.anchor_condition_id ?? "c1");
  const [conditionsText, setConditionsText] = useState(
    target !== null ? JSON.stringify(target.conditions, null, 2) : DEFAULT_CONDITIONS,
  );
  const [storageMode, setStorageMode] = useState<StorageMode>("original");
  const [imageIdDraft, setImageIdDraft] = useState(imageId);
  const [localError, setLocalError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const trimmedName = normalizeName(name);
  const nameLength = nameCodepointLength(trimmedName);
  const nameValid = isValidName(name);

  const parsedConditions = useMemo<ConditionNode | null>(() => {
    try {
      return JSON.parse(conditionsText) as ConditionNode;
    } catch {
      return null;
    }
  }, [conditionsText]);

  const handleSubmit = useCallback(async (): Promise<void> => {
    setLocalError(null);
    if (!nameValid) {
      setLocalError(`名称须为 1–100 个字符（当前 ${nameLength} 个 Unicode 码点）。`);
      return;
    }
    if (parsedConditions === null) {
      setLocalError("条件树不是合法 JSON。");
      return;
    }
    setSaving(true);
    try {
      if (editing && target !== null) {
        const patch: UpdateTargetPatch = {
          name: trimmedName,
          conditions: parsedConditions,
          anchor_condition_id: anchorId,
          group_id: groupId === "" ? null : groupId,
        };
        const ok = await store.update(target.id, patch, target.revision);
        if (!ok) {
          return; // store.error / store.conflict 呈现于下方
        }
      } else {
        if (imageIdDraft === "") {
          setLocalError("新建目标需要 image_id（先导入或捕获一张图）。");
          return;
        }
        const draft: TargetDraft = {
          name: trimmedName,
          image_id: imageIdDraft,
          anchor_condition_id: anchorId,
          conditions: parsedConditions,
          storage_mode: storageMode,
          ...(groupId === "" ? {} : { group_id: groupId }),
        };
        const ok = await store.create(draft);
        if (!ok) {
          return;
        }
      }
      onSaved?.();
    } catch (err) {
      setLocalError(`保存失败：${toUserMessage(err)}`);
    } finally {
      setSaving(false);
    }
  }, [
    anchorId,
    editing,
    groupId,
    imageIdDraft,
    nameLength,
    nameValid,
    onSaved,
    parsedConditions,
    storageMode,
    store,
    target,
    trimmedName,
  ]);

  const storeState = useTargetStore(store);

  return (
    <section className="target-editor" aria-label="目标编辑">
      <h2>{editing ? "编辑目标" : "新建目标"}（TASK-011）</h2>

      <label>
        名称
        <input
          type="text"
          value={name}
          onChange={(event) => setName(event.target.value)}
          aria-invalid={!nameValid}
        />
      </label>
      <small>
        {nameLength} / 100（Unicode 码点）
      </small>

      <label>
        分组
        <select value={groupId} onChange={(event) => setGroupId(event.target.value)}>
          <option value="">未分组</option>
          {groups.map((group) => (
            <option key={group.id} value={group.id}>
              {"　".repeat(group.depth)}
              {group.name}
            </option>
          ))}
        </select>
      </label>

      <label>
        锚条件叶 ID
        <input
          type="text"
          value={anchorId}
          onChange={(event) => setAnchorId(event.target.value)}
        />
      </label>

      {!editing && (
        <>
          <label>
            image_id
            <input
              type="text"
              value={imageIdDraft}
              placeholder="导入/捕获得到的会话内 image_id"
              onChange={(event) => setImageIdDraft(event.target.value)}
            />
          </label>
          <label>
            storage_mode
            <select
              value={storageMode}
              onChange={(event) => setStorageMode(event.target.value as StorageMode)}
            >
              <option value="original">original（保留原导入字节）</option>
              <option value="png_lossless">png_lossless</option>
            </select>
          </label>
        </>
      )}

      <label>
        条件树（JSON）
        <textarea
          rows={10}
          value={conditionsText}
          onChange={(event) => setConditionsText(event.target.value)}
          aria-invalid={parsedConditions === null}
        />
      </label>

      {localError !== null && <p role="alert">{localError}</p>}
      {storeState.conflict && (
        <p role="alert">
          <span>版本冲突（CONFLICT）：数据已被改动，请重新加载后再提交。</span>
          <button
            type="button"
            onClick={() => {
              store.clearError();
              void store.load();
            }}
          >
            重新加载
          </button>
        </p>
      )}
      {storeState.error !== null && !storeState.conflict && (
        <p role="alert">保存失败：{toUserMessage(storeState.error)}</p>
      )}

      <div className="target-editor-actions">
        <button type="button" onClick={() => void handleSubmit()} disabled={saving || storeState.busy}>
          {saving ? "保存中…" : "保存"}
        </button>
        {onCancel !== undefined && (
          <button type="button" onClick={onCancel} disabled={saving}>
            取消
          </button>
        )}
      </div>
    </section>
  );
}
