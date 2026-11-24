/**
 * scripts 特性 barrel（TASK-012）。
 *
 * - `api`：5 条命令的类型化 IPC 封装与诊断/草稿/校验类型；
 * - `store`：列表/分页 + 编辑草稿（源码 + 参数）+ 乐观锁写入的状态容器（含 useScriptStore）；
 * - 组件：`ScriptList`（列表 + 分页）、`ScriptEditor`（源码/参数编辑 + 语法诊断）。
 */

export * from "./api";
export * from "./store";

export { default as ScriptList } from "./ScriptList";
export { default as ScriptEditor } from "./ScriptEditor";
