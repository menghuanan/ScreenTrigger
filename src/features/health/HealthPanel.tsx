/**
 * 引擎状态面板（TASK-004 设计 §3.2 HealthPanel）：状态机呈现（§2.4 表）+
 * session/pid/时间信息 + versions 展示 + 诊断区（engine_diagnostics）。
 */

import { useState } from "react";

import { engineDiagnostics, toUserMessage } from "../../lib/ipc/engineClient";
import type { EngineDiagnostics, EngineStatusView } from "../../lib/ipc/types";

/** §2.4 状态机呈现表：状态 → （中文标签，横幅色调 class）。 */
const STATE_PRESENTATION: Record<EngineStatusView["state"], { label: string; tone: string }> = {
  idle: { label: "引擎未启动", tone: "banner-idle" },
  starting: { label: "引擎启动中…", tone: "banner-starting" },
  ready: { label: "就绪", tone: "banner-ready" },
  stopping: { label: "正在停止…", tone: "banner-stopping" },
  exited: { label: "引擎已退出", tone: "banner-exited" },
};

/** null 安全展示：null → "—"。 */
function orDash(value: string | number | null): string {
  return value === null ? "—" : String(value);
}

interface HealthPanelProps {
  status: EngineStatusView;
}

export default function HealthPanel({ status }: HealthPanelProps) {
  const [diagnostics, setDiagnostics] = useState<EngineDiagnostics | null>(null);
  const [diagError, setDiagError] = useState<string | null>(null);
  const [diagLoading, setDiagLoading] = useState(false);

  const presentation = STATE_PRESENTATION[status.state];

  async function refreshDiagnostics(): Promise<void> {
    setDiagLoading(true);
    setDiagError(null);
    try {
      setDiagnostics(await engineDiagnostics());
    } catch (err) {
      setDiagnostics(null);
      setDiagError(toUserMessage(err));
    } finally {
      setDiagLoading(false);
    }
  }

  return (
    <section className={`health-banner ${presentation.tone}`}>
      <div className="banner-head">
        <span className="banner-label">引擎健康</span>
        <strong className="banner-state">{presentation.label}</strong>
        <span className="banner-session">会话 {orDash(status.session_id)}</span>
      </div>

      {status.state === "exited" && status.last_error !== null && (
        <p className="banner-detail">
          退出原因：{status.last_error}（可在下方重新启动引擎）
        </p>
      )}

      <dl className="banner-facts">
        <div>
          <dt>PID</dt>
          <dd>{orDash(status.pid)}</dd>
        </div>
        <div>
          <dt>启动于</dt>
          <dd>{orDash(status.started_at)}</dd>
        </div>
        <div>
          <dt>就绪于</dt>
          <dd>{orDash(status.ready_at)}</dd>
        </div>
        <div>
          <dt>运行时长</dt>
          <dd>{status.uptime_ms === null ? "—" : `${status.uptime_ms} ms`}</dd>
        </div>
      </dl>

      <div className="versions">
        <h3>版本信息</h3>
        {status.versions === null ? (
          <p className="muted">引擎未就绪，暂无版本信息</p>
        ) : (
          <dl className="banner-facts">
            <div>
              <dt>引擎</dt>
              <dd>{status.versions.engine_version}</dd>
            </div>
            <div>
              <dt>协议版本</dt>
              <dd>{status.versions.protocol_version}</dd>
            </div>
            <div>
              <dt>Lua</dt>
              <dd>{status.versions.lua_version ?? "不可用（运行时未接入）"}</dd>
            </div>
            <div>
              <dt>Schema</dt>
              <dd>{status.versions.schema_version}</dd>
            </div>
          </dl>
        )}
      </div>

      <details className="diagnostics">
        <summary
          onClick={(e) => {
            // 首次展开时拉取一次诊断（后续展开不重复请求，可手动刷新）
            if (!diagnostics && !diagLoading) {
              void refreshDiagnostics();
            }
            e.preventDefault(); // 由 React 受控展开细节，避免闪烁
            e.currentTarget.parentElement?.toggleAttribute("open");
          }}
        >
          诊断信息（stderr 尾部 / 计数 / Job Object）
        </summary>
        {diagError !== null && <p className="diag-error">{diagError}</p>}
        {diagnostics === null ? (
          <p className="muted">{diagLoading ? "读取中…" : "暂无诊断数据"}</p>
        ) : (
          <>
            <dl className="banner-facts">
              <div>
                <dt>exit_code</dt>
                <dd>{orDash(diagnostics.exit_code)}</dd>
              </div>
              <div>
                <dt>stdout 事件数</dt>
                <dd>{diagnostics.stdout_event_count}</dd>
              </div>
              <div>
                <dt>畸形行数</dt>
                <dd>{diagnostics.malformed_line_count}</dd>
              </div>
              <div>
                <dt>Job Object</dt>
                <dd>{diagnostics.job_object_active ? "活跃" : "已关闭"}</dd>
              </div>
              <div>
                <dt>退出于</dt>
                <dd>{orDash(diagnostics.exited_at)}</dd>
              </div>
            </dl>
            <button type="button" className="btn btn-secondary" onClick={() => void refreshDiagnostics()}>
              刷新诊断
            </button>
            <pre className="stderr-tail">
              {diagnostics.stderr_tail.length > 0
                ? diagnostics.stderr_tail.join("\n")
                : "（stderr 无输出）"}
            </pre>
          </>
        )}
      </details>
    </section>
  );
}
