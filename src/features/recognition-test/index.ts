/**
 * recognition-test 特性 barrel（TASK-016）：复合条件与测试识别（只读）。
 *
 * - `RecognitionTestPanel`：主面板（条件树编辑 + 运行 + 结果/证据；
 *   **只识别与显示，不触发点击**）；
 * - `ConditionTreeEditor`：条件树编辑（结构/参数/ROI/anchor）；
 * - `EvidenceList`：识别结果与逐叶证据展示；
 * - `recognitionTestClient`：`test_recognition` / `evaluate_target_conditions` / `get_job`
 *   / `cancel_job` 的类型化封装与防御式解析；
 * - `useRecognitionJob`：识别 job 生命周期 hook（事件终态 + 对账 + 取消后丢弃迟到结果）。
 */

export { default as RecognitionTestPanel } from "./RecognitionTestPanel";
export { default as ConditionTreeEditor } from "./ConditionTreeEditor";
export { default as EvidenceList } from "./EvidenceList";

export {
  testRecognition,
  evaluateTargetConditions,
  getRecognitionJob,
  cancelRecognitionJob,
  parseRecognitionResult,
  parseJobError,
} from "./recognitionTestClient";
export type {
  RecognitionDraft,
  RecognitionResult,
  RecognitionMatch,
  RecognitionCandidate,
  EvidenceLeaf,
  RecognitionJobError,
  RecognitionJobView,
  LeafRoi,
} from "./recognitionTestClient";

export { useRecognitionJob } from "./useRecognitionJob";
export type { RecognitionJobController, RecognitionJobState } from "./useRecognitionJob";
