/**
 * 识别能力特性 barrel（TASK-015）：OCR 离线能力展示。
 */

export { default as OcrCapabilityPanel } from "./OcrCapabilityPanel";
export { OCR_REASON_FIELDS, OCR_REASON_PREFIX, parseOcrReason } from "../../lib/recognition/ocrReason";
export type { OcrReason, OcrReasonField } from "../../lib/recognition/ocrReason";
