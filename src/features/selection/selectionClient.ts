/**
 * 框选客户端（TASK-008，F01 / 03 命令目录）。
 *
 * - `startSelection` / `confirmSelection` / `stopSelection`：经既有 `ipc_request`
 *   路由到 Rust 本地宿主能力（`start_selection_mode` / `confirm_selection` /
 *   `stop_selection_mode` → `Route::HostHandled`）；
 * - `reportRegion`：覆盖层报告拖选矩形（内部 Tauri 命令 `selection_report_region`，
 *   单一 JSON `payload` 入参，避免命令多词参数大小写歧义）；
 * - `getSelectionPreview`：取 ROI 预览（内部命令 `selection_preview`）。
 *
 * 事件（`selection_started`/`updated`/`completed`/`cancelled`）经 protocolClient
 * 事件总线下发（Tauri 全局 emit，覆盖层窗口亦接收）。
 */

import { invoke } from "@tauri-apps/api/core";

import { request } from "../../lib/ipc/protocolClient";
import type { ImageRef, Region } from "../../lib/types/region";

/** selection_started 事件 payload（03：主窗口已隐藏）。 */
export interface SelectionStartedPayload {
  selection_id: string;
  display_id: string;
}

/** selection_updated 事件 payload（03：可合并；region + ROI 预览）。 */
export interface SelectionUpdatedPayload {
  selection_id: string;
  region: Region;
  preview: ImageRef;
}

/** selection_completed 事件 payload（03：与确认响应同一结果）。 */
export interface SelectionCompletedPayload {
  selection_id: string;
  region: Region;
  image: ImageRef;
}

/** selection_cancelled 事件 payload（03：正常取消/错误，finally 已恢复）。 */
export interface SelectionCancelledPayload {
  selection_id: string;
  reason: string;
}

/** 开始框选（display_id 省略 → 默认显示器）。返回 selection_id。 */
export async function startSelection(displayId?: string): Promise<string> {
  const params: Record<string, unknown> = {};
  if (displayId !== undefined) {
    params.display_id = displayId;
  }
  const data = await request<{ selection_id: string }>("start_selection_mode", params);
  return data.selection_id;
}

/** 确认选区（主窗口恢复后返回 image+region）。 */
export function confirmSelection(
  selectionId: string,
  region: Region,
): Promise<{ image: ImageRef; region: Region }> {
  return request<{ image: ImageRef; region: Region }>("confirm_selection", {
    selection_id: selectionId,
    region,
  });
}

/** 停止/取消框选（等价取消，恢复窗口）。 */
export async function stopSelection(selectionId: string): Promise<void> {
  await request<{ ok: boolean }>("stop_selection_mode", { selection_id: selectionId });
}

/** 覆盖层报告拖选矩形（局部 DIP 两对角点 + 覆盖层物理原点/scale）。 */
export async function reportRegion(
  selectionId: string,
  a: [number, number],
  b: [number, number],
  origin: [number, number],
  scale: number,
): Promise<{ emitted: boolean }> {
  const payload = JSON.stringify({
    selection_id: selectionId,
    ax: a[0],
    ay: a[1],
    bx: b[0],
    by: b[1],
    origin_x: origin[0],
    origin_y: origin[1],
    scale,
  });
  return invoke<{ emitted: boolean }>("selection_report_region", { payload });
}

/** 取 ROI 预览（mime + base64）。 */
export function getSelectionPreview(imageId: string): Promise<{ mime: string; base64: string }> {
  const payload = JSON.stringify({ image_id: imageId });
  return invoke<{ mime: string; base64: string }>("selection_preview", { payload });
}
