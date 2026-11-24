/**
 * 测试识别客户端（TASK-016；03 命令目录 `evaluate_target_conditions` / `test_recognition`）。
 *
 * 只做**识别与展示**相关命令的类型化封装（**绝不**触发点击/输入——F03：测试按钮只识别
 * 和显示结果，不点击）：
 *
 * - `testRecognition(imageId, draft)`：未保存草稿识别（结果 `target_id`/`sample_no`=null）；
 * - `evaluateTargetConditions(imageId, targetId)`：按已存目标识别；
 * - `getRecognitionJob(jobId)`：查 job 终态（`{status,result,error}`）；
 * - `cancelRecognitionJob(jobId)`：取消（复用 `cancel_job`，控制类命令）。
 *
 * wire 字段逐字对齐 02 §8：`Candidate={bbox,center,confidence,space}`、
 * `EvidenceLeaf={condition_id,matched,confidence,text,skipped}`（短路未执行的叶
 * `skipped=true,matched=null`，**不伪造分数**）、`Match`、`RecognitionResult`
 * （`{target_id,sample_no,target_revision,frame_id,found,matches,elapsed_ms,truncated}`）。
 * 解析为**防御式**：缺字段/形态异常 → null（不抛），由 UI 呈现"结果形态异常"，绝不臆造。
 */

import type { JobStatus } from "../../lib/ipc/types";
import { cancelJob, request } from "../../lib/ipc/protocolClient";
import type { ConditionNode } from "../../lib/types/conditions";
import type { LocalRect, Point, Rect, RegionSpace } from "../../lib/types";

/** 未保存目标草稿（03：`{name,group_id?,image_id,anchor_condition_id,conditions,storage_mode?}`）。 */
export interface RecognitionDraft {
  name: string;
  group_id?: string | null;
  image_id: string;
  anchor_condition_id: string;
  conditions: ConditionNode;
  storage_mode?: "original" | "png_lossless";
}

/** 候选（02：`{bbox,center,confidence,space}`）。 */
export interface RecognitionCandidate {
  bbox: Rect;
  center: Point;
  confidence: number;
  space: RegionSpace;
}

/** 证据叶（02 §8.3）：短路未执行 → `skipped=true,matched=null`。 */
export interface EvidenceLeaf {
  condition_id: string;
  matched: boolean | null;
  confidence: number | null;
  text: string | null;
  skipped: boolean;
}

/** 匹配（02）：`Match` wire 形态。 */
export interface RecognitionMatch {
  target_id: string;
  sample_no: number;
  space: RegionSpace;
  bbox: Rect;
  center: Point;
  confidence: number;
  geometry_revision: number;
  evidence: EvidenceLeaf[];
}

/** 识别结果（02 §8.1；`found ≡ matches 非空`）。 */
export interface RecognitionResult {
  target_id: string | null;
  sample_no: number | null;
  target_revision: number | null;
  frame_id: string;
  found: boolean;
  matches: RecognitionMatch[];
  elapsed_ms: number;
  truncated: boolean;
}

/** 06 四字段错误（job `error` / ipc 错误同形）。 */
export interface RecognitionJobError {
  code: string;
  message: string;
  details?: unknown;
  retryable: boolean;
}

/** `get_job` 视图（03：`{status,result,error}`）。 */
export interface RecognitionJobView {
  status: JobStatus;
  result: RecognitionResult | null;
  error: RecognitionJobError | null;
}

/** 叶 ROI（02：LocalRect，相对候选 bbox；宽高 ≥1，**非** Region）。 */
export type LeafRoi = LocalRect;

// ---------------------------------------------------------------------------
// 命令封装
// ---------------------------------------------------------------------------

/** 未保存草稿测试识别（`test_recognition`）；返回 `{job_id}`。 */
export async function testRecognition(
  imageId: string,
  draft: RecognitionDraft,
): Promise<string> {
  const data = await request<{ job_id: string }>("test_recognition", {
    image_id: imageId,
    draft,
  });
  return data.job_id;
}

/** 按已存目标识别（`evaluate_target_conditions`）；返回 `{job_id}`。 */
export async function evaluateTargetConditions(
  imageId: string,
  targetId: string,
): Promise<string> {
  const data = await request<{ job_id: string }>("evaluate_target_conditions", {
    image_id: imageId,
    target_id: targetId,
  });
  return data.job_id;
}

/** 查询识别 job（`get_job`）；未知 job_id → INVALID_PARAMS/NOT_FOUND 由管道 reject。 */
export async function getRecognitionJob(jobId: string): Promise<RecognitionJobView> {
  const data = await request<{ status: JobStatus; result: unknown; error: unknown }>(
    "get_job",
    { job_id: jobId },
  );
  return {
    status: data.status,
    result: parseRecognitionResult(data.result),
    error: parseJobError(data.error),
  };
}

/** 取消识别 job（控制类命令；终态取消为 `{cancelled:false}`，03）。 */
export function cancelRecognitionJob(jobId: string): Promise<{ cancelled: boolean }> {
  return cancelJob(jobId);
}

// ---------------------------------------------------------------------------
// 防御式解析（形态异常 → null；绝不臆造字段）
// ---------------------------------------------------------------------------

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function asFiniteNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function asInt(v: unknown): number | null {
  return typeof v === "number" && Number.isInteger(v) ? v : null;
}

function isPoint(v: unknown): v is Point {
  return isRecord(v) && asInt(v.x) !== null && asInt(v.y) !== null;
}

function isRect(v: unknown): v is Rect {
  return (
    isRecord(v) &&
    asInt(v.x) !== null &&
    asInt(v.y) !== null &&
    asInt(v.width) !== null &&
    asInt(v.height) !== null
  );
}

function parseEvidence(v: unknown): EvidenceLeaf | null {
  if (!isRecord(v)) {
    return null;
  }
  const conditionId = typeof v.condition_id === "string" ? v.condition_id : null;
  if (conditionId === null) {
    return null;
  }
  const matched = v.matched === true ? true : v.matched === false ? false : null;
  const confidence = asFiniteNumber(v.confidence);
  const text = typeof v.text === "string" ? v.text : null;
  const skipped = v.skipped === true;
  return {
    condition_id: conditionId,
    matched: skipped ? null : matched,
    confidence,
    text,
    skipped,
  };
}

function parseMatch(v: unknown): RecognitionMatch | null {
  if (!isRecord(v) || !isRect(v.bbox) || !isPoint(v.center)) {
    return null;
  }
  const space = typeof v.space === "string" ? (v.space as RegionSpace) : "image_pixel";
  const confidence = asFiniteNumber(v.confidence);
  if (confidence === null) {
    return null;
  }
  const evidenceRaw = Array.isArray(v.evidence) ? v.evidence : [];
  const evidence: EvidenceLeaf[] = [];
  for (const item of evidenceRaw) {
    const parsed = parseEvidence(item);
    if (parsed !== null) {
      evidence.push(parsed);
    }
  }
  return {
    target_id: typeof v.target_id === "string" ? v.target_id : "",
    sample_no: asInt(v.sample_no) ?? 0,
    space,
    bbox: v.bbox,
    center: v.center,
    confidence,
    geometry_revision: asInt(v.geometry_revision) ?? 0,
    evidence,
  };
}

/** 防御式解析识别结果；形态异常 → null（UI 呈现异常，不臆造）。 */
export function parseRecognitionResult(value: unknown): RecognitionResult | null {
  if (!isRecord(value)) {
    return null;
  }
  const matchesRaw = Array.isArray(value.matches) ? value.matches : null;
  if (matchesRaw === null || typeof value.found !== "boolean") {
    return null;
  }
  const matches: RecognitionMatch[] = [];
  for (const item of matchesRaw) {
    const parsed = parseMatch(item);
    if (parsed !== null) {
      matches.push(parsed);
    }
  }
  return {
    target_id: typeof value.target_id === "string" ? value.target_id : null,
    sample_no: asInt(value.sample_no),
    target_revision: asInt(value.target_revision),
    frame_id: typeof value.frame_id === "string" ? value.frame_id : "",
    found: value.found,
    matches,
    elapsed_ms: asInt(value.elapsed_ms) ?? 0,
    truncated: value.truncated === true,
  };
}

/** 防御式解析 job 错误（06 四字段）；形态异常 → null。 */
export function parseJobError(value: unknown): RecognitionJobError | null {
  if (!isRecord(value)) {
    return null;
  }
  const code = typeof value.code === "string" ? value.code : null;
  if (code === null) {
    return null;
  }
  return {
    code,
    message: typeof value.message === "string" ? value.message : code,
    details: value.details,
    retryable: value.retryable === true,
  };
}
