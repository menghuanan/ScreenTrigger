/**
 * `capabilities[ocr].reason` 规范格式解析（TASK-015 Q8；约定见 docs/03）。
 *
 * 后端按**固定规范格式**给出 OCR 真实覆盖（已安装识别模型 + 实证语言列表）：
 *
 * ```
 * OCR|state=<..>|code=<..>|models=<csv|->|verified=<csv|->|unverified=<csv|->|
 * models_dir=<..|->|library=<..|->|detail=<text>
 * ```
 *
 * 字段顺序固定为 `OCR_REASON_FIELDS`；空值占位 `-`。本解析器是**纯函数**：
 * 非 `OCR|` 前缀或结构非法 → `null`（调用方按"无结构化信息"处理，不猜测）。
 * 类型为 string，**不改** 03 既有 `reason:string|null` 线格式（无命令集合变更）。
 */

/** 规范格式固定前缀。 */
export const OCR_REASON_PREFIX = "OCR";

/** 规范格式字段顺序（与后端 `ocr_models.REASON_FIELD_ORDER` 逐字一致）。 */
export const OCR_REASON_FIELDS = [
  "state",
  "code",
  "models",
  "verified",
  "unverified",
  "models_dir",
  "library",
  "detail",
] as const;

/** 规范格式字段名。 */
export type OcrReasonField = (typeof OCR_REASON_FIELDS)[number];

/** 解析后的 OCR 能力披露（结构化投影）。 */
export interface OcrReason {
  /** 能力态（与 `CapabilityView.state` 冗余，便于单独展示）。 */
  state: string;
  /** 06 错误码（`ENGINE_UNAVAILABLE`/`MODEL_UNAVAILABLE`/`DATA_CORRUPT`…）；无则 null。 */
  code: string | null;
  /** 已安装识别模型列表（可经 `recognize_text{model_id}` 请求）。 */
  models: string[];
  /** **实证**语言（由已安装模型推断，不编造）。 */
  verified: string[];
  /** 首批声明但**未实证**的语言（如实披露）。 */
  unverified: string[];
  /** 模型目录（数据根 models/ 或配置项）绝对路径；无则 null。 */
  modelsDir: string | null;
  /** 权重库根（模型二进制所在）绝对路径；无则 null。 */
  library: string | null;
  /** 人类可读说明（如"未安装 paddleocr"）。 */
  detail: string;
  /** 规范格式原文（供原样展示/诊断）。 */
  raw: string;
}

/** 空值占位符（与后端 `REASON_EMPTY` 一致）。 */
const EMPTY = "-";

/** CSV 字段 → 字符串数组（占位/空 → []）。 */
function splitCsv(value: string): string[] {
  if (value === EMPTY || value === "") {
    return [];
  }
  return value.split(",");
}

/**
 * 解析 `capabilities[ocr].reason`；非规范格式 → `null`。
 *
 * @param reason 原始 reason（可能为 null）。
 * @returns 结构化投影；无法解析时 null。
 */
export function parseOcrReason(reason: string | null): OcrReason | null {
  if (reason === null || !reason.startsWith(`${OCR_REASON_PREFIX}|`)) {
    return null;
  }
  const chunks = reason.slice(OCR_REASON_PREFIX.length + 1).split("|");
  const parsed: Partial<Record<OcrReasonField, string>> = {};
  for (const chunk of chunks) {
    const eq = chunk.indexOf("=");
    if (eq < 0) {
      return null;
    }
    const key = chunk.slice(0, eq);
    if (!(OCR_REASON_FIELDS as readonly string[]).includes(key)) {
      return null;
    }
    parsed[key as OcrReasonField] = chunk.slice(eq + 1);
  }
  for (const field of OCR_REASON_FIELDS) {
    if (parsed[field] === undefined) {
      return null;
    }
  }
  const code = parsed.code as string;
  const modelsDir = parsed.models_dir as string;
  const library = parsed.library as string;
  return {
    state: parsed.state as string,
    code: code === EMPTY ? null : code,
    models: splitCsv(parsed.models as string),
    verified: splitCsv(parsed.verified as string),
    unverified: splitCsv(parsed.unverified as string),
    modelsDir: modelsDir === EMPTY ? null : modelsDir,
    library: library === EMPTY ? null : library,
    detail: parsed.detail as string,
    raw: reason,
  };
}
