/**
 * 框选覆盖层（TASK-008，F01）：铺满选定显示器的透明顶层窗口。
 *
 * 职责：捕获指针拖拽 → 实时上报选区（局部 DIP + 覆盖层物理原点/scale，Rust 侧做
 * DPI 换算与边界校验）→ 显示物理坐标/宽高 + ROI 预览 → 松开鼠标确认 / Esc 取消。
 *
 * 覆盖层窗口本身经 Rust `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` 排除捕获
 * （R04），故其绘制的选框/文字不会进入截图。真实多 DPI 拖拽为 NOT_RUN（见运行报告）。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";

import { onEvent } from "../../lib/ipc/protocolClient";
import type { Region } from "../../lib/types/region";
import {
  confirmSelection,
  getSelectionPreview,
  reportRegion,
  stopSelection,
  type SelectionUpdatedPayload,
} from "./selectionClient";

interface SelectionOverlayProps {
  /** 当前覆盖层对应的一次性 selection_id。 */
  selectionId: string;
}

/** 指针拖拽态（局部 DIP，两对角点）。 */
interface DragState {
  a: [number, number];
  b: [number, number];
}

export default function SelectionOverlay({ selectionId }: SelectionOverlayProps) {
  const [drag, setDrag] = useState<DragState | null>(null);
  const [region, setRegion] = useState<Region | null>(null);
  const [previewSrc, setPreviewSrc] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const originRef = useRef<[number, number]>([0, 0]);
  const scaleRef = useRef<number>(1);
  const draggingRef = useRef(false);
  const regionRef = useRef<Region | null>(null);

  // 覆盖层物理原点 + 当前缩放（07 §1：用覆盖层实际物理原点，不放缩虚拟屏绝对坐标）
  useEffect(() => {
    void (async () => {
      try {
        const win = getCurrentWindow();
        const [pos, scale] = await Promise.all([win.outerPosition(), win.scaleFactor()]);
        originRef.current = [pos.x, pos.y];
        scaleRef.current = scale;
      } catch {
        originRef.current = [0, 0];
        scaleRef.current = 1;
      }
    })();
  }, []);

  // 订阅 selection_updated：拿到物理 region + ROI 预览
  useEffect(() => {
    const off = onEvent("selection_updated", (env) => {
      const payload = env.payload as SelectionUpdatedPayload | undefined;
      if (!payload || payload.selection_id !== selectionId) {
        return;
      }
      regionRef.current = payload.region;
      setRegion(payload.region);
      void getSelectionPreview(payload.preview.image_id)
        .then((p) => setPreviewSrc(`data:${p.mime};base64,${p.base64}`))
        .catch(() => undefined);
    });
    return off;
  }, [selectionId]);

  const closeOverlay = useCallback(() => {
    void getCurrentWindow().close().catch(() => undefined);
  }, []);

  const handleCancel = useCallback(
    (reason: string) => {
      setNotice(reason);
      void stopSelection(selectionId)
        .catch(() => undefined)
        .finally(closeOverlay);
    },
    [selectionId, closeOverlay],
  );

  const handlePointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    draggingRef.current = true;
    const a: [number, number] = [e.clientX, e.clientY];
    setDrag({ a, b: a });
  }, []);

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!draggingRef.current) {
        return;
      }
      const b: [number, number] = [e.clientX, e.clientY];
      setDrag((prev) => {
        if (prev === null) {
          return prev;
        }
        const next: DragState = { a: prev.a, b };
        void reportRegion(selectionId, next.a, next.b, originRef.current, scaleRef.current).catch(
          () => undefined,
        );
        return next;
      });
    },
    [selectionId],
  );

  const handlePointerUp = useCallback(() => {
    if (!draggingRef.current) {
      return;
    }
    draggingRef.current = false;
    const current = regionRef.current;
    if (current === null) {
      setNotice("选区小于 10×10 物理像素，无法确认");
      return;
    }
    void confirmSelection(selectionId, current)
      .then(() => closeOverlay())
      .catch((err: unknown) => {
        const msg =
          typeof err === "object" && err !== null && "message" in err
            ? String((err as { message: unknown }).message)
            : "确认失败";
        setNotice(msg);
      });
  }, [selectionId, closeOverlay]);

  // Esc 取消
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        handleCancel("已取消（Esc）");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [handleCancel]);

  const rect = drag === null ? null : normalize(drag);
  const physicalLabel =
    region === null
      ? "拖动选择区域"
      : `${region.width}×${region.height} @ (${region.x}, ${region.y})`;

  return (
    <div
      className="selection-overlay"
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      style={{ position: "fixed", inset: 0, cursor: "crosshair", background: "rgba(0,0,0,0.15)" }}
    >
      {rect !== null && (
        <div
          className="selection-rect"
          style={{
            position: "absolute",
            left: rect.left,
            top: rect.top,
            width: rect.width,
            height: rect.height,
            border: "1px solid #4ea1ff",
            boxShadow: "0 0 0 9999px rgba(0,0,0,0.35)",
            pointerEvents: "none",
          }}
        />
      )}
      <div
        className="selection-hud"
        style={{
          position: "fixed",
          left: 12,
          top: 12,
          padding: "6px 10px",
          background: "rgba(0,0,0,0.6)",
          color: "#fff",
          fontFamily: "monospace",
          fontSize: 13,
          pointerEvents: "none",
        }}
      >
        <div>{physicalLabel}</div>
        <div style={{ opacity: 0.7 }}>Esc 取消 · 松开鼠标确认（最小 10×10）</div>
        {notice !== null && <div style={{ color: "#ffb4b4" }}>{notice}</div>}
      </div>
      {previewSrc !== null && (
        <img
          src={previewSrc}
          alt="选区预览"
          style={{ position: "fixed", right: 12, bottom: 12, maxWidth: 240, border: "1px solid #888" }}
        />
      )}
    </div>
  );
}

/** 归一化反向拖动（局部 DIP）：返回左上角 + 宽高。 */
function normalize(d: DragState): { left: number; top: number; width: number; height: number } {
  const left = Math.min(d.a[0], d.b[0]);
  const top = Math.min(d.a[1], d.b[1]);
  return { left, top, width: Math.abs(d.b[0] - d.a[0]), height: Math.abs(d.b[1] - d.a[1]) };
}
