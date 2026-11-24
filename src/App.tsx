/**
 * 单屏布局（TASK-004 §3.2 App；TASK-005 §3.3/契约④ 最小适配）：
 * 健康横幅 + 能力徽标 + 运维/业务按钮组装。
 *
 * TASK-005 变更（仅此一处结构性变更）：
 * - system_ready 直转监听（listenSystemReady）已随契约④移除；
 * - 切换为 protocolClient 事件总线订阅（onEvent/onGap）；
 * - 注册 seq 缺口恢复钩子（§6.4：engine_status 重查 + resync 通知，不重放输入）；
 * - 业务按钮禁用公式语义不变（§4.5/TASK-004）。
 *
 * 状态管理：单屏 useState，不引入状态管理库（§7.1）。
 */

import { useCallback, useEffect, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";

import {
  engineStart,
  engineStatus,
  engineStop,
  toUserMessage,
} from "./lib/ipc/engineClient";
import { onEvent, onGap } from "./lib/ipc/protocolClient";
import type { EngineStatusView } from "./lib/ipc/types";
import CapabilityBadges from "./features/health/CapabilityBadges";
import DisabledBusinessPanel from "./features/health/DisabledBusinessPanel";
import EngineActions from "./features/health/EngineActions";
import HealthPanel from "./features/health/HealthPanel";
import ImportPanel from "./features/import/ImportPanel";
import OcrCapabilityPanel from "./features/recognition/OcrCapabilityPanel";
import { RecognitionTestPanel } from "./features/recognition-test";
import SelectionPanel from "./features/selection/SelectionPanel";
import SelectionOverlay from "./features/selection/SelectionOverlay";

/** 覆盖层窗口标签（与 Rust selection::wiring::overlay_label 逐字一致）。 */
const OVERLAY_LABEL_PATTERN = /^selection-overlay-(.+)$/;

/**
 * 应用入口分流（TASK-008）：覆盖层窗口渲染 [`SelectionOverlay`]（同一前端资源，
 * 由 Rust 打开的独立窗口加载），主窗口渲染原单屏布局。分流不引入路由库。
 */
export default function App() {
  const match = OVERLAY_LABEL_PATTERN.exec(getCurrentWindow().label);
  if (match !== null) {
    return <SelectionOverlay selectionId={match[1] ?? ""} />;
  }
  return <MainApp />;
}

/** Idle 初始视图（与 Rust mod.rs::Supervisor::new() 的 status() 形状一致）。 */
const INITIAL_STATUS: EngineStatusView = {
  state: "idle",
  session_id: null,
  pid: null,
  started_at: null,
  ready_at: null,
  uptime_ms: null,
  last_error: null,
  capabilities: [],
  versions: null,
};

/** 用户消息条类型。 */
interface Notice {
  kind: "info" | "error";
  text: string;
}

function MainApp() {
  const [status, setStatus] = useState<EngineStatusView>(INITIAL_STATUS);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  /** 拉取一次状态并落盘（挂载初始化、system_ready 锚点与 gap 恢复共用）。 */
  const refreshStatus = useCallback(async (): Promise<void> => {
    try {
      setStatus(await engineStatus());
    } catch (err) {
      setNotice({ kind: "error", text: `状态查询失败：${toUserMessage(err)}` });
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    // 契约④：system_ready 经事件总线到达（会话重建锚；seq 重置在 protocolClient 内）
    const offReady = onEvent("system_ready", () => {
      setNotice({ kind: "info", text: "引擎握手完成，进入就绪" });
      void refreshStatus();
    });
    // §6.4 onGap：resync 通知 → 骨架期日志级提示 + 状态重查（不重放输入）
    const offGap = onGap(({ expected, actual }) => {
      setNotice({
        kind: "error",
        text: `事件流检测到缺口（期望 seq=${expected}，实际 seq=${actual}），已触发状态重同步`,
      });
      void refreshStatus();
    });
    return () => {
      offReady();
      offGap();
    };
  }, [refreshStatus]);

  const handleStart = useCallback((): void => {
    setBusy(true);
    setNotice(null);
    engineStart()
      .then((next) => {
        setStatus(next);
        setNotice({ kind: "info", text: "启动命令已受理" });
      })
      .catch((err: unknown) => {
        setNotice({ kind: "error", text: toUserMessage(err) });
      })
      .finally(() => {
        setBusy(false);
        void refreshStatus(); // 兜底对齐（启动失败时状态已落 Exited）
      });
  }, [refreshStatus]);

  const handleStop = useCallback((): void => {
    setBusy(true);
    setNotice(null);
    engineStop()
      .then((next) => {
        setStatus(next);
        setNotice({ kind: "info", text: "停止命令已受理" });
      })
      .catch((err: unknown) => {
        setNotice({ kind: "error", text: toUserMessage(err) });
      })
      .finally(() => {
        setBusy(false);
        void refreshStatus();
      });
  }, [refreshStatus]);

  return (
    <main className="app">
      <header className="app-header">
        <h1>ScreenTrigger</h1>
        <span className="app-subtitle">IPC 协议骨架（TASK-005）：全双工、可取消、可诊断</span>
      </header>

      {notice !== null && (
        <div className={`notice notice-${notice.kind}`} role="status">
          {notice.text}
        </div>
      )}

      <HealthPanel status={status} />
      <CapabilityBadges capabilities={status.capabilities} />
      <OcrCapabilityPanel capabilities={status.capabilities} />
      <EngineActions
        engineState={status.state}
        busy={busy}
        onStart={handleStart}
        onStop={handleStop}
      />
      <ImportPanel engineState={status.state} />
      <SelectionPanel engineState={status.state} />
      <RecognitionTestPanel engineState={status.state} capabilities={status.capabilities} />
      <DisabledBusinessPanel engineState={status.state} capabilities={status.capabilities} />

      <footer className="app-footer">
        骨架阶段：未接入脚本/输入业务；识别能力据**实际装配**置位——OCR 不可用为真实能力状态（缺引擎/缺模型），非故障。
      </footer>
    </main>
  );
}
