/**
 * 目标 / 分组 IPC 封装（TASK-011 设计 §4：10 条命令，03 契约逐字）。
 *
 * - 复用统一客户端 `request<T>`（`ui-<n>` 请求 id、IpcError 四字段管道、超时兜底）；
 * - 返回值解包到裸实体（`{target}`/`{group}` → 目标/分组对象），与既有
 *   `importClient` 风格一致；
 * - `TargetDraft` **不含** `id/sample_no/revision/created_at/updated_at`，且按 Q4
 *   裁决 **不含 `metadata`**（02 枚举无该字段；03 携带未声明字段即 INVALID_PARAMS）；
 * - `name` 长度一律按 **Unicode 码点** 计数（`Array.from(name).length`，非
 *   `String.length` 的 UTF-16 码元）——三端口径：Python `len` / Rust `chars().count`。
 */

import { request } from "../../lib/ipc/protocolClient";
import type { ConditionNode, Group, Target } from "../../lib/types/conditions";

/** 资产存储模式（02 ImageAsset；缺省 "original"=保留原导入字节）。 */
export type StorageMode = "original" | "png_lossless";

/** 新建目标草稿（03 + Q4：无 ID/编号/revision/时间、无 metadata）。 */
export interface TargetDraft {
  name: string;
  group_id?: string | null;
  image_id: string;
  anchor_condition_id: string;
  conditions: ConditionNode;
  storage_mode?: StorageMode;
}

/** 列表查询参数（Q5：`groupId` 键缺省=不过滤、显式 `null`=仅未分组、字符串=精确该组）。 */
export interface ListTargetsOptions {
  groupId?: string | null;
  query?: string;
  offset?: number;
  limit?: number;
}

/** 列表分页结果（03：{items,total}）。 */
export interface TargetPage {
  items: Target[];
  total: number;
}

/** 更新补丁（03 白名单：name/group_id/conditions/anchor_condition_id/metadata/image_id+storage_mode）。 */
export interface UpdateTargetPatch {
  name?: string;
  group_id?: string | null;
  conditions?: ConditionNode;
  anchor_condition_id?: string;
  metadata?: Record<string, unknown>;
  image_id?: string;
  storage_mode?: StorageMode;
}

/** 分组 + 派生成员数（03 list_groups：target_count 派生，不持久化）。 */
export interface GroupWithCount extends Group {
  target_count: number;
}

/** update_group 参数（Q3：`name` 与 `parentGroupId` 至少给一个；键缺省=不改/不搬移）。 */
export interface UpdateGroupParams {
  groupId: string;
  expectedRevision: number;
  name?: string;
  /** 键出现（含显式 `null`）即触发搬移；`null`=移至顶层。 */
  parentGroupId?: string | null;
}

/** name 下限（去首尾空白后，Unicode 码点）。 */
export const NAME_MIN_CODEPOINTS = 1;
/** name 上限（去首尾空白后，Unicode 码点）。 */
export const NAME_MAX_CODEPOINTS = 100;

/** 按 Unicode 码点计数（**不**用 `String.length`——那是 UTF-16 码元）。 */
export function nameCodepointLength(name: string): number {
  return Array.from(name).length;
}

/** 规范化名称（去首尾空白；服务端同样 strip）。 */
export function normalizeName(name: string): string {
  return name.trim();
}

/** 名称是否合法（去首尾空白后 1–100 码点）。 */
export function isValidName(name: string): boolean {
  const length = nameCodepointLength(normalizeName(name));
  return length >= NAME_MIN_CODEPOINTS && length <= NAME_MAX_CODEPOINTS;
}

/** 新建目标（create_target；返回持久 Target）。 */
export async function createTarget(draft: TargetDraft): Promise<Target> {
  const data = await request<{ target: Target }>("create_target", { draft });
  return data.target;
}

/** 按 ID 取目标（get_target；不存在 → IpcError{TARGET_NOT_FOUND}）。 */
export async function getTarget(targetId: string): Promise<Target> {
  const data = await request<{ target: Target }>("get_target", { target_id: targetId });
  return data.target;
}

/** 分页 + 过滤列表（list_targets）。 */
export function listTargets(options: ListTargetsOptions = {}): Promise<TargetPage> {
  const params: Record<string, unknown> = {};
  // 键缺省=不过滤；显式 null=仅未分组（Q5）——故仅当 !== undefined 时带上键。
  if (options.groupId !== undefined) {
    params.group_id = options.groupId;
  }
  if (options.query !== undefined) {
    params.query = options.query;
  }
  if (options.offset !== undefined) {
    params.offset = options.offset;
  }
  if (options.limit !== undefined) {
    params.limit = options.limit;
  }
  return request<TargetPage>("list_targets", params);
}

/** 更新目标（update_target；乐观锁 expected_revision；冲突 → IpcError{CONFLICT}）。 */
export async function updateTarget(
  targetId: string,
  patch: UpdateTargetPatch,
  expectedRevision: number,
): Promise<Target> {
  const data = await request<{ target: Target }>("update_target", {
    target_id: targetId,
    expected_revision: expectedRevision,
    patch,
  });
  return data.target;
}

/** 删除目标（delete_target；活动引用 → IN_USE）。 */
export async function deleteTarget(targetId: string, expectedRevision: number): Promise<void> {
  await request<{ ok: boolean }>("delete_target", {
    target_id: targetId,
    expected_revision: expectedRevision,
  });
}

/** 新建分组（create_group；`parentGroupId` 缺省/null=顶层；父不存在 → GROUP_NOT_FOUND）。 */
export async function createGroup(name: string, parentGroupId?: string | null): Promise<Group> {
  const params: Record<string, unknown> = { name };
  if (parentGroupId !== undefined) {
    params.parent_group_id = parentGroupId;
  }
  const data = await request<{ group: Group }>("create_group", params);
  return data.group;
}

/** 全量分组（list_groups；含派生 target_count）。 */
export async function listGroups(): Promise<GroupWithCount[]> {
  const data = await request<{ groups: GroupWithCount[] }>("list_groups", {});
  return data.groups;
}

/** 更新分组（update_group；改名 / 搬移；键缺省区分"不改/不搬移"与"搬顶层"）。 */
export async function updateGroup(params: UpdateGroupParams): Promise<Group> {
  const body: Record<string, unknown> = {
    group_id: params.groupId,
    expected_revision: params.expectedRevision,
  };
  if ("name" in params && params.name !== undefined) {
    body.name = params.name;
  }
  // 键是否出现：出现即搬移（显式 null=移至顶层）。
  if ("parentGroupId" in params) {
    body.parent_group_id = params.parentGroupId ?? null;
  }
  const data = await request<{ group: Group }>("update_group", body);
  return data.group;
}

/** 删除分组（delete_group；直系目标移至未分组、子组提升）。 */
export async function deleteGroup(groupId: string, expectedRevision: number): Promise<void> {
  await request<{ ok: boolean }>("delete_group", {
    group_id: groupId,
    expected_revision: expectedRevision,
  });
}

/** 移动目标到分组（move_target_to_group；`groupId=null` → 未分组）。 */
export async function moveTargetToGroup(
  targetId: string,
  groupId: string | null,
  expectedRevision: number,
): Promise<Target> {
  const data = await request<{ target: Target }>("move_target_to_group", {
    target_id: targetId,
    group_id: groupId,
    expected_revision: expectedRevision,
  });
  return data.target;
}
