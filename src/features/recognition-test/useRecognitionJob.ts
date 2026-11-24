/**
 * 识别 job 生命周期 hook（TASK-016）。
 *
 * 管理一次测试识别的异步 job：提交（`submit` 返回 `job_id`）→ 事件总线终态
 * （`recognition_completed`/`recognition_error`/`recognition_cancelled`）→ 兜底
 * `get_job` 对账（避免事件早于 job_id 落位时的竞态）。
 *
 * 关键口径（F03 / 02 / 03）：
 * - **未命中（`found=false`）不是错误**：`completed` 即终态，`result.found=false` 照常展示；
 * - **服务错误 / 超时 / 缺模型是错误**：`error` 终态呈现，**绝不**转成"未命中"；
 * - **取消后丢弃迟到结果**：取消即置 `cancelledRef`，此后到达的事件/对账一律忽略（F03）。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { onEvent } from "../../lib/ipc/protocolClient";
import {
  cancelRecognitionJob,
  getRecognitionJob,
  parseJobError,
  parseRecognitionResult,
  type RecognitionJobError,
  type RecognitionJobView,
  type RecognitionResult,
} from "./recognitionTestClient";

/** job 视图状态机（含 UI 侧 `submitting`）。 */
export type RecognitionJobState =
  | "idle"
  | "submitting"
  | "running"
  | "completed"
  | "error"
  | "cancelled";

/** 事件 payload 中识别 job 相关字段（防御式）。 */
interface JobEventPayload {
  job_id?: unknown;
  result?: unknown;
  error?: unknown;
}

/** hook 返回值。 */
export interface RecognitionJobController {
  state: RecognitionJobState;
  jobId: string | null;
  result: RecognitionResult | null;
  error: RecognitionJobError | null;
  /** 提交一次识别（`submit` 返回 `job_id`）；提交被拒（如 INVALID_PARAMS/BUSY）→ error。 */
  run: (submit: () => Promise<string>) => Promise<void>;
  /** 取消在途识别（幂等；终态后调用为 no-op）。 */
  cancel: () => Promise<void>;
  /** 复位（清空结果/错误，回到 idle）。 */
  reset: () => void;
}

/** 未知错误 → 06 四字段（兼容 IpcError / 字符串 / Error）。 */
function toJobError(err: unknown): RecognitionJobError {
  if (typeof err === "object" && err !== null) {
    const parsed = parseJobError(err);
    if (parsed !== null) {
      return parsed;
    }
  }
  if (typeof err === "string" && err.length > 0) {
    return { code: "INTERNAL_ERROR", message: err, retryable: false };
  }
  if (err instanceof Error && err.message.length > 0) {
    return { code: "INTERNAL_ERROR", message: err.message, retryable: false };
  }
  return { code: "INTERNAL_ERROR", message: "未知错误", retryable: false };
}

export function useRecognitionJob(): RecognitionJobController {
  const [state, setState] = useState<RecognitionJobState>("idle");
  const [jobId, setJobId] = useState<string | null>(null);
  const [result, setResult] = useState<RecognitionResult | null>(null);
  const [error, setError] = useState<RecognitionJobError | null>(null);

  const jobIdRef = useRef<string | null>(null);
  const cancelledRef = useRef(false);

  const reset = useCallback((): void => {
    jobIdRef.current = null;
    cancelledRef.current = false;
    setState("idle");
    setJobId(null);
    setResult(null);
    setError(null);
  }, []);

  /** 应用一次 job 视图（对账用）；取消后一律丢弃迟到结果。 */
  const applyView = useCallback((view: RecognitionJobView): void => {
    if (cancelledRef.current) {
      return; // 取消后丢弃迟到结果（F03）
    }
    switch (view.status) {
      case "completed":
        setResult(view.result);
        setError(null);
        setState("completed");
        break;
      case "error":
        setError(
          view.error ?? { code: "RECOGNITION_FAILED", message: "识别失败", retryable: false },
        );
        setResult(null);
        setState("error");
        break;
      case "cancelled":
        setState("cancelled");
        break;
      default:
        setState("running"); // pending/running
        break;
    }
  }, []);

  useEffect(() => {
    const matches = (payload: JobEventPayload): boolean =>
      typeof payload.job_id === "string" && payload.job_id === jobIdRef.current;

    const offCompleted = onEvent("recognition_completed", (env) => {
      const payload = (env.payload ?? {}) as JobEventPayload;
      if (!matches(payload) || cancelledRef.current) {
        return; // 非本次 job / 已取消 → 丢弃迟到结果
      }
      setResult(parseRecognitionResult(payload.result));
      setError(null);
      setState("completed");
    });
    const offError = onEvent("recognition_error", (env) => {
      const payload = (env.payload ?? {}) as JobEventPayload;
      if (!matches(payload) || cancelledRef.current) {
        return;
      }
      setError(
        parseJobError(payload.error) ?? {
          code: "RECOGNITION_FAILED",
          message: "识别失败",
          retryable: false,
        },
      );
      setResult(null);
      setState("error");
    });
    const offCancelled = onEvent("recognition_cancelled", (env) => {
      const payload = (env.payload ?? {}) as JobEventPayload;
      if (!matches(payload)) {
        return;
      }
      setState("cancelled");
    });
    return () => {
      offCompleted();
      offError();
      offCancelled();
    };
  }, []);

  const run = useCallback(
    async (submit: () => Promise<string>): Promise<void> => {
      setState("submitting");
      setResult(null);
      setError(null);
      cancelledRef.current = false;
      try {
        const id = await submit();
        jobIdRef.current = id;
        setJobId(id);
        setState("running");
        // 对账：终态可能已完成（事件早于本 await 落位），主动查一次。
        try {
          applyView(await getRecognitionJob(id));
        } catch {
          // 对账失败不致命：事件通道仍会送达终态。
        }
      } catch (err) {
        setError(toJobError(err));
        setState("error");
      }
    },
    [applyView],
  );

  const cancel = useCallback(async (): Promise<void> => {
    const id = jobIdRef.current;
    if (id === null) {
      return;
    }
    cancelledRef.current = true; // 先置位：此后迟到结果全部丢弃
    try {
      await cancelRecognitionJob(id);
    } catch {
      // 取消失败不改变终态呈现（03：终态取消为 false）；用户可重试。
    }
    setState("cancelled");
  }, []);

  return { state, jobId, result, error, run, cancel, reset };
}
