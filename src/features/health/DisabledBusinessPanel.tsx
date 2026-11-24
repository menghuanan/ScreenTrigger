/**
 * 业务按钮面板（TASK-004 §3.2；TASK-006 §7.3 per-capability gate 扩展）：
 * 新建目标 / 运行脚本 / 识别测试。
 *
 * 禁用规则（TASK-004 §4.5 公式的 per-capability 化，语义不变）：
 *   buttonDisabled = engineState !== "ready" || !gate.allowed
 *   其中 gate.allowed 由 capabilityGate 映射表逐按钮评估所需能力——能力恢复
 *   （未来任务把 state 改 available）无需改 UI 代码；配置查看/修改为框架命令
 *   不受 gate（§7.3 表）。
 * 骨架阶段 capabilities 全为 unavailable → 业务按钮恒禁用为真实状态。
 */

import type { CapabilityView, EngineState } from "../../lib/ipc/types";
import {
  BUSINESS_FUNCTION_LABELS,
  evaluateCapabilityGate,
  type BusinessFunction,
} from "../../lib/types/capabilityGate";

/** 按钮 → 业务功能映射（§7.3 表：新建目标=框选/导入/捕获类）。 */
const BUTTONS: ReadonlyArray<{ label: string; fn: BusinessFunction }> = [
  { label: "新建目标", fn: "capture_selection" },
  { label: "运行脚本", fn: "script_run_debug" },
  { label: "识别测试", fn: "recognition_test" },
];

interface DisabledBusinessPanelProps {
  engineState: EngineState;
  capabilities: CapabilityView[];
}

export default function DisabledBusinessPanel({
  engineState,
  capabilities,
}: DisabledBusinessPanelProps) {
  const engineReady = engineState === "ready";

  return (
    <section className="business-panel">
      <h2>业务操作（骨架占位）</h2>
      {!engineReady && (
        <p className="muted">
          能力未就绪：引擎未就绪（当前状态 {engineState}），业务按钮暂不可用
        </p>
      )}
      <div className="btn-row">
        {BUTTONS.map(({ label, fn }) => {
          const gate = evaluateCapabilityGate(fn, capabilities);
          const disabled = !engineReady || !gate.allowed;
          const tooltip = !engineReady
            ? "引擎未就绪"
            : gate.allowed
              ? `${label}（${BUSINESS_FUNCTION_LABELS[fn]}）`
              : gate.reasonText;
          return (
            <button
              key={fn}
              type="button"
              className="btn"
              disabled={disabled}
              title={tooltip}
            >
              {label}
            </button>
          );
        })}
      </div>
      <ul className="gate-list">
        {BUTTONS.map(({ label, fn }) => {
          const gate = evaluateCapabilityGate(fn, capabilities);
          const stateText = !engineReady
            ? "引擎未就绪"
            : gate.allowed
              ? "可启用"
              : `阻断：${gate.blocking.map((b) => `${b.id}=${b.state}`).join("、")}`;
          return (
            <li key={fn} className={gate.allowed && engineReady ? "gate-ok" : "gate-blocked"}>
              {label}（{BUSINESS_FUNCTION_LABELS[fn]}）—— {stateText}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
