/**
 * 图片导入面板（TASK-009，最小 UI）：选择文件 → 导入 → 显示 ImageRef / 预览。
 *
 * 说明：
 * - 文件选择由 Rust 侧系统对话框完成，前端只拿到一次性 token（不接触路径）；
 * - 需引擎处于 ready（token 绑定当前会话）；
 * - 预览用 base64 → data URI 展示；缩略图仅用于显示，不用于识别。
 */

import { useCallback, useState } from "react";

import { toUserMessage } from "../../lib/ipc/engineClient";
import type { ImageRef } from "../../lib/types/region";
import { getImagePreview, importImage, pickImageFile, releaseImage } from "./importClient";

interface ImportPanelProps {
  /** 引擎状态：非 ready 时禁用导入（token 绑定当前会话）。 */
  engineState: string;
}

export default function ImportPanel({ engineState }: ImportPanelProps) {
  const [imageRef, setImageRef] = useState<ImageRef | null>(null);
  const [previewSrc, setPreviewSrc] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [released, setReleased] = useState(false);

  const ready = engineState === "ready";

  const handleImport = useCallback(async (): Promise<void> => {
    setBusy(true);
    setNotice(null);
    try {
      const token = await pickImageFile();
      if (token === null) {
        setNotice("已取消选择");
        return;
      }
      const ref = await importImage(token);
      setImageRef(ref);
      setReleased(false);
      const preview = await getImagePreview(ref.image_id, 400);
      setPreviewSrc(`data:${preview.mime};base64,${preview.base64}`);
      setNotice(`已导入：${ref.width}×${ref.height} ${ref.format}`);
    } catch (err) {
      setNotice(`导入失败：${toUserMessage(err)}`);
    } finally {
      setBusy(false);
    }
  }, []);

  const handleRelease = useCallback(async (): Promise<void> => {
    if (imageRef === null) {
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      await releaseImage(imageRef.image_id);
      setReleased(true);
      setNotice("已释放图像引用");
    } catch (err) {
      setNotice(`释放失败：${toUserMessage(err)}`);
    } finally {
      setBusy(false);
    }
  }, [imageRef]);

  return (
    <section className="import-panel" aria-label="图片导入">
      <h2>图片导入（TASK-009）</h2>
      <button type="button" onClick={() => void handleImport()} disabled={!ready || busy}>
        {busy ? "处理中…" : "选择文件并导入"}
      </button>
      {!ready && <p className="import-hint">引擎未就绪：请先启动引擎。</p>}
      {notice !== null && <p role="status">{notice}</p>}
      {imageRef !== null && (
        <div className="import-result">
          <dl>
            <dt>image_id</dt>
            <dd>{imageRef.image_id}</dd>
            <dt>尺寸</dt>
            <dd>
              {imageRef.width}×{imageRef.height}
            </dd>
            <dt>格式</dt>
            <dd>{imageRef.format}</dd>
            <dt>sha256</dt>
            <dd>{imageRef.sha256}</dd>
          </dl>
          {previewSrc !== null && (
            <img src={previewSrc} alt="导入预览" style={{ maxWidth: 320 }} />
          )}
          <button type="button" onClick={() => void handleRelease()} disabled={busy || released}>
            释放引用
          </button>
        </div>
      )}
    </section>
  );
}
