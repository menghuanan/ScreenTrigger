/**
 * OCR 离线能力面板（TASK-015 Q8）：展示 `capabilities[ocr]` 的**真实覆盖**。
 *
 * - `available` → 明确"仅加载本地已安装模型、全程离线"；
 * - `unavailable` → 解析 `reason`（规范格式，见 docs/03）展示已安装模型 / 实证语言 /
 *   未实证语言 / 模型目录 / 权重库 / 说明 / 错误码；解析失败则原样展示 reason 文本，
 *   **绝不**把未知态伪装为可用（03：unavailable 的功能不能显示可用按钮）。
 */

import type { CapabilityView } from "../../lib/ipc/types";
import { parseOcrReason } from "../../lib/recognition/ocrReason";

/** state → 中文标签（与 CapabilityBadges 一致）。 */
function stateLabel(state: string): string {
  switch (state) {
    case "available":
      return "可用";
    case "pending":
      return "待接入";
    case "unavailable":
      return "不可用";
    default:
      return state;
  }
}

/** null/空 → "—"。 */
function orDash(value: string | null): string {
  return value === null || value === "" ? "—" : value;
}

/** 列表 → "、" 连接（空 → "—"）。 */
function listOrDash(items: string[]): string {
  return items.length === 0 ? "—" : items.join("、");
}

interface OcrCoverageRowProps {
  label: string;
  value: string;
}

function OcrCoverageRow({ label, value }: OcrCoverageRowProps) {
  return (
    <li className="ocr-coverage-row">
      <span className="ocr-coverage-key">{label}</span>
      <span className="ocr-coverage-value">{value}</span>
    </li>
  );
}

interface OcrCapabilityPanelProps {
  /** 引擎状态视图中的能力清单（未 ready 时为空数组）。 */
  capabilities: CapabilityView[];
}

export default function OcrCapabilityPanel({ capabilities }: OcrCapabilityPanelProps) {
  const ocr = capabilities.find((c) => c.id === "ocr");

  if (ocr === undefined) {
    return (
      <section className="capability-panel">
        <h2>OCR 离线能力</h2>
        <p className="muted">引擎未就绪，OCR 能力状态未知</p>
      </section>
    );
  }

  const parsed = parseOcrReason(ocr.reason);

  return (
    <section className="capability-panel">
      <h2>OCR 离线能力</h2>
      <p className="muted">状态：{stateLabel(ocr.state)}（{ocr.state}）</p>
      {ocr.state === "available" && (
        <p className="gate-ok">已就绪：仅加载本地已安装模型，全程离线（不联网下载）。</p>
      )}
      {parsed === null ? (
        ocr.reason !== null ? (
          <p className="badge-reason">{ocr.reason}</p>
        ) : (
          <p className="muted">无补充说明</p>
        )
      ) : (
        <ul className="ocr-coverage">
          <OcrCoverageRow label="已安装识别模型" value={listOrDash(parsed.models)} />
          <OcrCoverageRow label="实证语言" value={listOrDash(parsed.verified)} />
          <OcrCoverageRow label="未实证语言" value={listOrDash(parsed.unverified)} />
          <OcrCoverageRow label="模型目录" value={orDash(parsed.modelsDir)} />
          <OcrCoverageRow label="权重库" value={orDash(parsed.library)} />
          <OcrCoverageRow label="错误码" value={orDash(parsed.code)} />
          <OcrCoverageRow label="说明" value={orDash(parsed.detail)} />
        </ul>
      )}
    </section>
  );
}
