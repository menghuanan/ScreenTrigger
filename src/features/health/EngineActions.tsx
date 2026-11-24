/**
 * 运维按钮（TASK-004 设计 §3.2 EngineActions）：启动/停止引擎。
 *
 * disabled 逻辑（§3.2：starting/ready 禁启动；非运行态禁停止）：
 * - 启动：starting/ready/stopping 禁用（stopping 期间全部按钮禁用，§2.4）；
 *   exited 允许重启（ENGINE_UNAVAILABLE retryable=true，06）；
 * - 停止：仅 starting/ready 可停（stopping 已在停，其余无进程可停）。
 */

import type { EngineState } from "../../lib/ipc/types";

interface EngineActionsProps {
  engineState: EngineState;
  /** 命令在途（防双击重复 invoke；Rust 侧本就幂等，UI 层再挡一层）。 */
  busy: boolean;
  onStart: () => void;
  onStop: () => void;
}

export default function EngineActions({ engineState, busy, onStart, onStop }: EngineActionsProps) {
  const startDisabled =
    busy || engineState === "starting" || engineState === "ready" || engineState === "stopping";
  const stopDisabled = busy || (engineState !== "starting" && engineState !== "ready");

  const startLabel =
    engineState === "exited" ? "启动引擎（重启，新会话）" : "启动引擎";

  return (
    <section className="engine-actions">
      <h2>引擎操作</h2>
      <div className="btn-row">
        <button
          type="button"
          className="btn btn-primary"
          disabled={startDisabled}
          onClick={onStart}
        >
          {startLabel}
        </button>
        <button
          type="button"
          className="btn btn-danger"
          disabled={stopDisabled}
          onClick={onStop}
        >
          停止引擎
        </button>
      </div>
      <p className="muted">
        {engineState === "exited"
          ? "引擎已退出，可重新启动（每次启动生成新会话）"
          : engineState === "stopping"
            ? "正在执行停止序列（关闸门 → 优雅退出 → 兜底终止）"
            : "运维操作仅管理引擎进程，不涉及业务能力"}
      </p>
    </section>
  );
}
