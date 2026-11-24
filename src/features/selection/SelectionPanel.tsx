/**
 * 框选入口面板（TASK-008，主窗口内最小 UI）：开始框选 → 覆盖层 → 返回 Region/ImageRef。
 *
 * - 需引擎 ready（框选为引擎会话内能力）；
 * - 重复框选返回 BUSY（Rust 侧）；取消/Esc 丢弃临时图并恢复主窗口；
 * - 事件经 protocolClient 事件总线（selection_started/completed/cancelled）。
 */

import { useCallback, useEffect, useState } from "react";

import { toUserMessage } from "../../lib/ipc/engineClient";
import { onEvent } from "../../lib/ipc/protocolClient";
import type { ImageRef, Region } from "../../lib/types/region";
import {
  startSelection,
  stopSelection,
  type SelectionCancelledPayload,
  type SelectionCompletedPayload,
  type SelectionStartedPayload,
} from "./selectionClient";

interface SelectionPanelProps {
  /** 引擎状态：非 ready 时禁用框选。 */
  engineState: string;
}

interface Completed {
  region: Region;
  image: ImageRef;
}

export default function SelectionPanel({ engineState }: SelectionPanelProps) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const [displayId, setDisplayId] = useState<string | null>(null);
  const [completed, setCompleted] = useState<Completed | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const ready = engineState === "ready";

  useEffect(() => {
    const offs = [
      onEvent("selection_started", (env) => {
        const p = env.payload as SelectionStartedPayload | undefined;
        if (p) {
          setActiveId(p.selection_id);
          setDisplayId(p.display_id);
          setNotice("框选进行中：请拖动选择区域，Esc 取消");
        }
      }),
      onEvent("selection_completed", (env) => {
        const p = env.payload as SelectionCompletedPayload | undefined;
        if (p) {
          setCompleted({ region: p.region, image: p.image });
          setNotice(`已确认：${p.region.width}×${p.region.height}`);
        }
        setActiveId(null);
      }),
      onEvent("selection_cancelled", (env) => {
        const p = env.payload as SelectionCancelledPayload | undefined;
        if (p) {
          setNotice(`已取消（reason=${p.reason}）`);
        }
        setActiveId(null);
      }),
    ];
    return () => offs.forEach((off) => off());
  }, []);

  const handleStart = useCallback(async (): Promise<void> => {
    setBusy(true);
    setNotice(null);
    try {
      const id = await startSelection();
      setActiveId(id);
    } catch (err) {
      setNotice(`框选失败：${toUserMessage(err)}`);
    } finally {
      setBusy(false);
    }
  }, []);

  const handleStop = useCallback(async (): Promise<void> => {
    if (activeId === null) {
      return;
    }
    try {
      await stopSelection(activeId);
    } catch (err) {
      setNotice(`取消失败：${toUserMessage(err)}`);
    }
  }, [activeId]);

  return (
    <section className="selection-panel" aria-label="屏幕框选">
      <h2>屏幕框选（TASK-008）</h2>
      <button
        type="button"
        onClick={() => void handleStart()}
        disabled={!ready || busy || activeId !== null}
      >
        {busy ? "处理中…" : activeId !== null ? "框选中…" : "开始框选"}
      </button>
      {activeId !== null && (
        <button type="button" onClick={() => void handleStop()}>
          取消框选
        </button>
      )}
      {!ready && <p className="selection-hint">引擎未就绪：请先启动引擎。</p>}
      {displayId !== null && <p>显示器：{displayId}</p>}
      {notice !== null && <p role="status">{notice}</p>}
      {completed !== null && (
        <div className="selection-result">
          <dl>
            <dt>Region</dt>
            <dd>
              {completed.region.space} ({completed.region.x}, {completed.region.y}){" "}
              {completed.region.width}×{completed.region.height}
            </dd>
            <dt>image_id</dt>
            <dd>{completed.image.image_id}</dd>
            <dt>format</dt>
            <dd>{completed.image.format}</dd>
          </dl>
        </div>
      )}
    </section>
  );
}
