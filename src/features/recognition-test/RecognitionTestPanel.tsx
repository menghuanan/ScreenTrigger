/**
 * 复合条件与测试识别面板（TASK-016；F03 / 02 / 03）。
 *
 * **只读**测试：构造未保存草稿（`TargetDraft`）→ 发 `test_recognition {image_id,draft}` →
 * 展示识别结果与证据。**只识别与显示，绝不触发点击/输入**（F03）。
 *
 * 门禁（§7.3）：业务功能 `recognition_test` 需能力 `ocr` available，且引擎 ready 才可运行。
 * 能力缺失时不显示可执行按钮（本面板禁用"运行识别测试"并给出 reason）。
 *
 * 结果口径（02/F03）：
 * - 未命中 → `found=false,matches=[]`（非错误，正常数据）；
 * - 服务错误 / 推理超时 / 缺模型 → 错误呈现（**不**转成未命中）；
 * - 取消后丢弃迟到结果（见 `useRecognitionJob`）。
 */

import { useCallback, useMemo, useState } from "react";

import { useCapability } from "../../lib/types";
import type { CapabilityView } from "../../lib/ipc/types";
import type { ConditionNode } from "../../lib/types/conditions";
import ConditionTreeEditor, {
  collectLeafIds,
  collectLeaves,
  defaultConditionTree,
  isBranch,
} from "./ConditionTreeEditor";
import EvidenceList from "./EvidenceList";
import { testRecognition, type RecognitionDraft } from "./recognitionTestClient";
import { useRecognitionJob } from "./useRecognitionJob";

interface RecognitionTestPanelProps {
  /** 引擎状态（非 ready 时禁用）。 */
  engineState: string;
  /** 引擎上报名能力（能力门禁用）。 */
  capabilities: CapabilityView[];
}

/** 校验草稿输入；返回错误文案或 null。 */
function validateDraftInput(
  frameImageId: string,
  templateImageId: string,
  draftName: string,
  conditions: ConditionNode,
  anchorConditionId: string,
): string | null {
  if (frameImageId.trim().length === 0) {
    return "帧 image_id 必填";
  }
  if (templateImageId.trim().length === 0) {
    return "模板 image_id 必填（模板/轮廓叶的模板来源）";
  }
  const nameLength = Array.from(draftName.trim()).length;
  if (nameLength < 1 || nameLength > 100) {
    return "草稿名称去首尾空白后须为 1–100 字符";
  }
  const leafIds = collectLeafIds(conditions);
  if (leafIds.length < 1 || leafIds.length > 10) {
    return `条件叶数须为 1–10，当前 ${leafIds.length}`;
  }
  if (!leafIds.includes(anchorConditionId)) {
    return "锚点必须指向存在的条件叶";
  }
  if (isBranch(conditions) && conditions.op !== "all" && conditions.op !== "any") {
    return "分支仅支持 all / any（本版无 NOT）";
  }
  for (const leaf of collectLeaves(conditions)) {
    if (leaf.type === "ocr") {
      const params = leaf.params as { text?: unknown; model_id?: unknown };
      if (typeof params.text !== "string" || params.text.length === 0) {
        return `叶 #${leaf.id} 的 OCR 目标文本必填`;
      }
      if (typeof params.model_id !== "string" || params.model_id.length === 0) {
        return `叶 #${leaf.id} 的 model_id 必填`;
      }
    }
  }
  return null;
}

export default function RecognitionTestPanel({
  engineState,
  capabilities,
}: RecognitionTestPanelProps) {
  const gate = useCapability("recognition_test", capabilities);
  const ready = engineState === "ready";

  const initial = useMemo(() => defaultConditionTree(), []);
  const [conditions, setConditions] = useState<ConditionNode>(initial.conditions);
  const [anchorConditionId, setAnchorConditionId] = useState<string>(initial.anchorConditionId);
  const [frameImageId, setFrameImageId] = useState("");
  const [templateImageId, setTemplateImageId] = useState("");
  const [draftName, setDraftName] = useState("测试草稿");
  const [inputError, setInputError] = useState<string | null>(null);

  const job = useRecognitionJob();
  const running = job.state === "submitting" || job.state === "running";

  const handleRun = useCallback((): void => {
    const invalid = validateDraftInput(
      frameImageId,
      templateImageId,
      draftName,
      conditions,
      anchorConditionId,
    );
    if (invalid !== null) {
      setInputError(invalid);
      return;
    }
    setInputError(null);
    const draft: RecognitionDraft = {
      name: draftName.trim(),
      image_id: templateImageId.trim(),
      anchor_condition_id: anchorConditionId,
      conditions,
    };
    void job.run(() => testRecognition(frameImageId.trim(), draft));
  }, [frameImageId, templateImageId, draftName, conditions, anchorConditionId, job]);

  const disabled = !ready || !gate.allowed || running;

  return (
    <section className="recognition-test-panel" aria-label="复合条件与测试识别">
      <h2>复合条件与测试识别（TASK-016）</h2>
      <p className="muted">
        只读测试：识别并显示结果，**不触发任何点击/输入**。复合条件 = 锚点命中 AND 条件树为真。
      </p>

      {!ready && <p className="rt-hint">引擎未就绪：请先启动引擎。</p>}
      {ready && !gate.allowed && (
        <p className="rt-hint rt-blocked" role="status">
          能力未就绪，无法运行：{gate.reasonText}
        </p>
      )}

      <div className="rt-inputs">
        <label>
          帧 image_id（被测图像）：
          <input
            type="text"
            value={frameImageId}
            placeholder="如导入/框选得到的 image_id"
            onChange={(e) => setFrameImageId(e.target.value)}
          />
        </label>
        <label>
          模板 image_id（模板/轮廓叶的模板来源）：
          <input
            type="text"
            value={templateImageId}
            placeholder="模板像素 image_id"
            onChange={(e) => setTemplateImageId(e.target.value)}
          />
        </label>
        <label>
          草稿名称：
          <input type="text" value={draftName} onChange={(e) => setDraftName(e.target.value)} />
        </label>
      </div>

      <h3>条件树</h3>
      <ConditionTreeEditor
        value={conditions}
        onChange={setConditions}
        anchorConditionId={anchorConditionId}
        onAnchorChange={setAnchorConditionId}
      />

      {inputError !== null && (
        <p className="rt-input-error" role="alert">
          {inputError}
        </p>
      )}

      <div className="btn-row">
        <button
          type="button"
          className="btn btn-primary"
          onClick={handleRun}
          disabled={disabled}
          title={gate.allowed ? undefined : gate.reasonText}
        >
          {running ? "识别中…" : "运行识别测试"}
        </button>
        {running && (
          <button type="button" className="btn btn-danger" onClick={() => void job.cancel()}>
            取消
          </button>
        )}
        {!running && job.state !== "idle" && (
          <button type="button" className="btn" onClick={job.reset}>
            清空结果
          </button>
        )}
      </div>

      {job.state === "submitting" && <p className="muted">已提交，等待 job…</p>}
      {job.state === "cancelled" && (
        <p className="muted" role="status">
          已取消；迟到结果将被丢弃。
        </p>
      )}

      {job.state === "error" && job.error !== null && (
        <div className="rt-error" role="alert">
          <strong>识别错误（非未命中）</strong>
          <div>
            错误码：{job.error.code}
            {job.error.retryable && <span className="rt-retryable">（可重试）</span>}
          </div>
          <div>{job.error.message}</div>
        </div>
      )}

      {job.state === "completed" && job.result === null && (
        <p className="rt-input-error" role="alert">
          结果形态异常：无法解析 RecognitionResult。
        </p>
      )}
      {job.state === "completed" && job.result !== null && <EvidenceList result={job.result} />}
    </section>
  );
}
