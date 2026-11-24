/**
 * 图片导入客户端（TASK-009，M01 import_image）。
 *
 * - `pickImageFile`：调用 Rust 命令 `pick_image_file` 打开系统文件对话框，返回
 *   一次性 import_token（取消返回 null）。**token 只能由文件选择器生成**——前端
 *   从不构造/传递文件路径；
 * - `importImage`：经 ipc_request 发 `import_image {import_token}`，返回 ImageRef；
 * - `getImagePreview` / `releaseImage`：复用既有 get_image_preview / release_image。
 */

import { invoke } from "@tauri-apps/api/core";

import { request } from "../../lib/ipc/protocolClient";
import type { ImageRef } from "../../lib/types/region";

/** 打开系统文件选择器，返回一次性导入 token；用户取消返回 null。 */
export async function pickImageFile(): Promise<string | null> {
  const token = await invoke<unknown>("pick_image_file");
  return typeof token === "string" && token.length > 0 ? token : null;
}

/** 用一次性 token 导入图片，返回 ImageRef（跨会话/过期 token 由后端拒绝）。 */
export async function importImage(importToken: string): Promise<ImageRef> {
  const data = await request<{ image: ImageRef }>("import_image", {
    import_token: importToken,
  });
  return data.image;
}

/** 预览载荷（03：mime + base64，编码 ≤512KiB）。 */
export interface ImagePreview {
  mime: string;
  base64: string;
}

/** 生成预览（max_edge 可省略，默认 400；范围 1–800）。 */
export function getImagePreview(imageId: string, maxEdge?: number): Promise<ImagePreview> {
  const params: Record<string, unknown> = { image_id: imageId };
  if (maxEdge !== undefined) {
    params.max_edge = maxEdge;
  }
  return request<ImagePreview>("get_image_preview", params);
}

/** 释放图像引用（幂等；所有权校验）。 */
export async function releaseImage(imageId: string): Promise<void> {
  await request<{ ok: boolean }>("release_image", { image_id: imageId });
}
