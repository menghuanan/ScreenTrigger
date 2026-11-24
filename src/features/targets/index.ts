/**
 * targets 特性 barrel（TASK-011）。
 *
 * - `api`：10 条命令的类型化 IPC 封装与草稿/补丁/分组类型；
 * - `store`：列表/分页/查询/分组过滤 + 乐观锁写入的状态容器（含 useTargetStore 钩子）；
 * - 组件：`TargetList`（列表）、`TargetEditor`（草稿编辑）、`GroupTree`（三层分组树）。
 */

export * from "./api";
export * from "./store";

export { default as TargetList } from "./TargetList";
export { default as TargetEditor } from "./TargetEditor";
export { default as GroupTree } from "./GroupTree";
