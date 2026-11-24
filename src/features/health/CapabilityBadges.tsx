/**
 * 能力徽标（TASK-004 设计 §3.2 CapabilityBadges）：
 * available(绿) / pending(灰) / unavailable(红+reason)；
 * ocr=unavailable 必须显式可见（设计 §6.1 步骤 3 断言"capabilities 全 unavailable
 * 且有 reason"在 UI 层不因折叠而不可见）。
 */

import type { CapabilityView } from "../../lib/ipc/types";

/** state → 徽标 class（未知 state 按灰处理，不虚假可用）。 */
function badgeClass(state: string): string {
  switch (state) {
    case "available":
      return "badge badge-available";
    case "pending":
      return "badge badge-pending";
    case "unavailable":
      return "badge badge-unavailable";
    default:
      return "badge badge-pending";
  }
}

/** state → 中文标签。 */
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

/** ocr 不可用时的醒目提示（真实覆盖见 OcrCapabilityPanel；此处仅概要）。 */
function criticalHint(capabilities: CapabilityView[]): string | null {
  const ocr = capabilities.find((c) => c.id === "ocr");
  if (ocr !== undefined && ocr.state === "unavailable") {
    return "识别（OCR）能力不可用：未安装引擎或离线模型（真实原因见 OCR 离线能力面板）";
  }
  return null;
}

interface CapabilityBadgesProps {
  /** 未 ready 时为空数组（Rust 侧保证）。 */
  capabilities: CapabilityView[];
}

export default function CapabilityBadges({ capabilities }: CapabilityBadgesProps) {
  if (capabilities.length === 0) {
    return (
      <section className="capability-panel">
        <h2>能力徽标</h2>
        <p className="muted">引擎未就绪，能力状态未知</p>
      </section>
    );
  }

  const hint = criticalHint(capabilities);

  return (
    <section className="capability-panel">
      <h2>能力徽标</h2>
      {hint !== null && <p className="critical-hint">⚠ {hint}</p>}
      <p className="muted">能力据实际装配置位；OCR 真实覆盖（已安装模型/实证语言）见下方 OCR 离线能力面板</p>
      <ul className="badge-list">
        {capabilities.map((cap) => (
          <li key={cap.id} className="badge-item">
            <span className={badgeClass(cap.state)}>
              {cap.id} · {stateLabel(cap.state)}
            </span>
            {cap.reason !== null && <span className="badge-reason">{cap.reason}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}
