/**
 * 目标/条件树/分组/脚本类型与校验（TASK-006 设计 §3.2/§6.2；02 权威逐字段）。
 *
 * 对齐基准：schema/types.schema.json + B3 Rust conditions.rs（错误字段路径
 * 逐字一致）：name 去首尾空白后 1–100 **Unicode 字符** → `name`；条件树叶 1–10、
 * 分支嵌套 ≤4 层、叶 ID 目标内唯一 → 树级 `conditions`；anchor 存在性 →
 * `anchor_condition_id`；参数错误定位 `conditions.params.<field>`（hsc →
 * `conditions.params.color_space`、HSV h 越界 → `conditions.params.color.h`、
 * canny low≥high → `conditions.params.canny_high`）；metadata ≤16KiB；
 * 跨实体唯一性（同层同名）归 Repository，本层仅字段校验。
 *
 * name 长度口径（三端一致）：Unicode 码点计数（Rust `chars().count()`、Python
 * `len(str)`）——TS 侧使用 `Array.from(name).length`，**不用** String.length
 * （UTF-16 码元会把代理对算成 2）。
 */

import {
  isSha256Hex,
  nonEmptyString,
  validateImageAsset,
  validateLocalRect,
  validateRfc3339,
  type LocalRect,
  type TypeError,
} from "./region";

function invalidParams(field: string): TypeError {
  return { code: "INVALID_PARAMS", field };
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function asInteger(v: unknown): number | null {
  return typeof v === "number" && Number.isInteger(v) ? v : null;
}

function asFinite(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// ---------------------------------------------------------------------------
// 类型（02/schema 逐字段）
// ---------------------------------------------------------------------------

/** 条件分支节点（02）：op ∈ {all,any}，children 非空。 */
export interface ConditionBranch {
  op: "all" | "any";
  children: ConditionNode[];
}

/** 条件叶节点（02）：id 目标内唯一、type 四枚举、params 随类型、roi 可选。 */
export interface ConditionLeaf {
  id: string;
  type: "template" | "contour" | "ocr" | "color";
  params: TemplateParams | ContourParams | OcrParams | ColorParams;
  roi?: LocalRect;
}

export type ConditionNode = ConditionBranch | ConditionLeaf;

/** 模板匹配参数（02）：threshold 0..1 必填；allow_scale=false 默认。 */
export interface TemplateParams {
  threshold: number;
  allow_scale?: boolean;
  scale_range?: [number, number];
  step?: number;
}

/** 轮廓匹配参数（02）：canny 0–255 且 low<high；min_area ≥1。 */
export interface ContourParams {
  threshold: number;
  min_area?: number;
  canny_low?: number;
  canny_high?: number;
}

/** OCR 参数（02）：NFC 归一化后比较；fuzzy=1−Levenshtein/max(len)。 */
export interface OcrParams {
  text: string;
  model_id: string;
  match_mode?: "exact" | "contains" | "fuzzy";
  min_confidence?: number;
  case_sensitive?: boolean;
  fuzzy_threshold?: number;
}

/** 颜色匹配参数（02）：color_space 仅 rgb|hsv（hsc 生成即 INVALID_PARAMS）。 */
export interface ColorParams {
  color_space: "rgb" | "hsv";
  color: ColorRgb | ColorHsv;
  tolerance?: ToleranceRgb | ToleranceHsv;
  min_pixel_count?: number;
  min_percentage?: number;
}

export interface ColorRgb {
  r: number;
  g: number;
  b: number;
}
export interface ColorHsv {
  /** 02：h∈[0,360)（即 ≤359 合法）；OpenCV H=0–179 转换只在适配器内部。 */
  h: number;
  s: number;
  v: number;
}
export interface ToleranceRgb {
  r?: number;
  g?: number;
  b?: number;
}
export interface ToleranceHsv {
  /** 02：h 容差 0–180（圆周最短距离），s/v 0–100。 */
  h?: number;
  s?: number;
  v?: number;
}

/** Target（02）：整体语义=anchor 命中 AND 条件树为真；metadata 唯一允许未知字段。 */
export interface Target {
  id: string;
  sample_no: number;
  revision: number;
  name: string;
  group_id: string | null;
  image: ImageAssetLike;
  thumbnail_path: string | null;
  anchor_condition_id: string;
  conditions: ConditionNode;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

/** ImageAsset 形状（复用 region.ts 的结构校验）。 */
export interface ImageAssetLike {
  path: string;
  sha256: string;
  width: number;
  height: number;
  format: "png" | "jpeg";
  storage_mode: "original" | "png_lossless";
}

/** Group（02）：最多 3 层（depth 0..2）；同层同名唯一归 Repository。 */
export interface Group {
  id: string;
  revision: number;
  name: string;
  parent_group_id: string | null;
  depth: number;
  created_at: string;
  updated_at: string;
}

/** 脚本参数声明（02）：required=true → default 必须 null。 */
export interface ScriptParameter {
  name: string;
  type: "string" | "number" | "boolean" | "object" | "array";
  required: boolean;
  default: unknown;
  description: string;
}

/** Script（02）：source_path 形态 scripts/<id>/<hash>.lua；source UTF-8 ≤1MiB 属 load 时。 */
export interface Script {
  id: string;
  revision: number;
  name: string;
  source_path: string;
  source_sha256: string;
  parameters: ScriptParameter[];
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// 校验纯函数
// ---------------------------------------------------------------------------

/**
 * name 校验（Target/Group/Script 共用）：去首尾空白后 1–100 Unicode 字符。
 * 码点计数：`Array.from` 按 Unicode 码点切分（与 Rust chars()/Python len 一致）。
 */
export function validateNameValue(name: unknown, field: string): TypeError | null {
  if (typeof name !== "string") {
    return invalidParams(field);
  }
  const trimmed = name.trim();
  const len = Array.from(trimmed).length;
  if (len < 1 || len > 100) {
    return invalidParams(field);
  }
  return null;
}

function requireKeys(obj: Record<string, unknown>, keys: readonly string[], top: string): TypeError | null {
  for (const key of keys) {
    if (!(key in obj)) {
      return invalidParams(key.length === 0 ? top : key);
    }
  }
  return null;
}

const TARGET_KEYS = [
  "id", "sample_no", "revision", "name", "group_id", "image", "thumbnail_path",
  "anchor_condition_id", "conditions", "created_at", "updated_at", "metadata",
] as const;

const GROUP_KEYS = ["id", "revision", "name", "parent_group_id", "depth", "created_at", "updated_at"] as const;

const SCRIPT_KEYS = [
  "id", "revision", "name", "source_path", "source_sha256", "parameters", "created_at", "updated_at",
] as const;

/** Target 校验（§6.2 条件树全规则 + 12 必填字段 + metadata ≤16KiB）。 */
export function validateTarget(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("target");
  }
  const missing = requireKeys(v, TARGET_KEYS, "target");
  if (missing !== null) {
    return missing;
  }
  if (nonEmptyString(v.id) === null) {
    return invalidParams("id");
  }
  const sampleNo = asInteger(v.sample_no);
  if (sampleNo === null || sampleNo < 1) {
    return invalidParams("sample_no");
  }
  const revision = asInteger(v.revision);
  if (revision === null || revision < 1) {
    return invalidParams("revision");
  }
  const nameErr = validateNameValue(v.name, "name");
  if (nameErr !== null) {
    return nameErr;
  }
  if (v.group_id !== null && nonEmptyString(v.group_id) === null) {
    return invalidParams("group_id");
  }
  if (v.thumbnail_path !== null && typeof v.thumbnail_path !== "string") {
    return invalidParams("thumbnail_path");
  }
  const imageErr = validateImageAsset(v.image);
  if (imageErr !== null) {
    return imageErr;
  }
  // metadata：JSON 对象，序列化 ≤16KiB（UTF-8 字节；唯一允许未知字段的例外）
  if (!isRecord(v.metadata)) {
    return invalidParams("metadata");
  }
  const metaBytes = new TextEncoder().encode(JSON.stringify(v.metadata)).length;
  if (metaBytes > 16 * 1024) {
    return invalidParams("metadata");
  }
  const createdErr = validateRfc3339(v.created_at, "created_at");
  if (createdErr !== null) {
    return createdErr;
  }
  const updatedErr = validateRfc3339(v.updated_at, "updated_at");
  if (updatedErr !== null) {
    return updatedErr;
  }
  // 条件树：结构 + 参数 + 叶数/深度/唯一性
  const leafIds: string[] = [];
  const treeErr = validateConditionNode(v.conditions, 1, leafIds);
  if (treeErr !== null) {
    return treeErr;
  }
  if (leafIds.length < 1 || leafIds.length > 10) {
    return invalidParams("conditions");
  }
  // anchor 必须指向存在叶
  const anchor = nonEmptyString(v.anchor_condition_id);
  if (anchor === null) {
    return invalidParams("anchor_condition_id");
  }
  if (!leafIds.includes(anchor)) {
    return invalidParams("anchor_condition_id");
  }
  return null;
}

/** 条件节点递归校验（depth 从 1 起；分支嵌套 ≤4 层，02）。 */
function validateConditionNode(v: unknown, depth: number, leafIds: string[]): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("conditions");
  }
  if (v.op !== undefined) {
    // 分支节点：op ∈ {all,any}；children 非空数组；深度 ≤4；不得混入叶字段
    const op = nonEmptyString(v.op);
    if (op !== "all" && op !== "any") {
      return invalidParams("conditions");
    }
    if (depth > 4) {
      return invalidParams("conditions");
    }
    if (!Array.isArray(v.children) || v.children.length === 0) {
      return invalidParams("conditions");
    }
    for (const child of v.children) {
      const err = validateConditionNode(child, depth + 1, leafIds);
      if (err !== null) {
        return err;
      }
    }
    if (v.id !== undefined || v.type !== undefined || v.params !== undefined) {
      return invalidParams("conditions");
    }
    return null;
  }
  // 叶节点：id 非空且目标内唯一；type 四枚举；params 按类型校验
  const id = nonEmptyString(v.id);
  if (id === null) {
    return invalidParams("conditions");
  }
  if (leafIds.includes(id)) {
    return invalidParams("conditions");
  }
  leafIds.push(id);
  const ctype = nonEmptyString(v.type);
  if (ctype === null) {
    return invalidParams("conditions.type");
  }
  if (!("params" in v)) {
    return invalidParams("conditions.params");
  }
  const params = v.params;
  const paramErr =
    ctype === "template"
      ? validateTemplateParams(params)
      : ctype === "contour"
        ? validateContourParams(params)
        : ctype === "ocr"
          ? validateOcrParams(params)
          : ctype === "color"
            ? validateColorParams(params)
            : invalidParams("conditions.type");
  if (paramErr !== null) {
    return paramErr;
  }
  if (v.roi !== undefined) {
    const roiErr = validateLocalRect(v.roi);
    if (roiErr !== null) {
      return { code: roiErr.code, field: `conditions.${roiErr.field}` };
    }
  }
  return null;
}

/** template params：threshold 0..1 必填；allow_scale/scale_range/step 可选。 */
function validateTemplateParams(params: unknown): TypeError | null {
  if (!isRecord(params)) {
    return invalidParams("conditions.params");
  }
  const threshold = asFinite(params.threshold);
  if (threshold === null) {
    return invalidParams("conditions.params.threshold");
  }
  if (threshold < 0 || threshold > 1) {
    return invalidParams("conditions.params.threshold");
  }
  if (params.allow_scale !== undefined && typeof params.allow_scale !== "boolean") {
    return invalidParams("conditions.params.allow_scale");
  }
  if (params.scale_range !== undefined) {
    const arr = params.scale_range;
    if (!Array.isArray(arr) || arr.length !== 2) {
      return invalidParams("conditions.params.scale_range");
    }
    for (const item of arr) {
      const f = asFinite(item);
      if (f === null || f < 0 || f > 1) {
        return invalidParams("conditions.params.scale_range");
      }
    }
  }
  if (params.step !== undefined) {
    const s = asFinite(params.step);
    if (s === null || s <= 0) {
      return invalidParams("conditions.params.step");
    }
  }
  return null;
}

/** contour params：threshold 0..1；min_area ≥1；canny 0–255 且 low<high（相等即违例）。 */
function validateContourParams(params: unknown): TypeError | null {
  if (!isRecord(params)) {
    return invalidParams("conditions.params");
  }
  const threshold = asFinite(params.threshold);
  if (threshold === null || threshold < 0 || threshold > 1) {
    return invalidParams("conditions.params.threshold");
  }
  if (params.min_area !== undefined) {
    const a = asInteger(params.min_area);
    if (a === null || a < 1) {
      return invalidParams("conditions.params.min_area");
    }
  }
  let low: number | null = null;
  let high: number | null = null;
  if (params.canny_low !== undefined) {
    low = asInteger(params.canny_low);
    if (low === null || low < 0 || low > 255) {
      return invalidParams("conditions.params.canny_low");
    }
  }
  if (params.canny_high !== undefined) {
    high = asInteger(params.canny_high);
    if (high === null || high < 0 || high > 255) {
      return invalidParams("conditions.params.canny_high");
    }
  }
  // 跨字段：canny_low < canny_high（违例定位 high；02）
  if (low !== null && high !== null && low >= high) {
    return invalidParams("conditions.params.canny_high");
  }
  return null;
}

/** ocr params：text/model_id 必填非空；match_mode/min_confidence/case_sensitive/fuzzy_threshold。 */
function validateOcrParams(params: unknown): TypeError | null {
  if (!isRecord(params)) {
    return invalidParams("conditions.params");
  }
  if (nonEmptyString(params.text) === null) {
    return invalidParams("conditions.params.text");
  }
  if (nonEmptyString(params.model_id) === null) {
    return invalidParams("conditions.params.model_id");
  }
  if (params.match_mode !== undefined) {
    const m = nonEmptyString(params.match_mode);
    if (m !== "exact" && m !== "contains" && m !== "fuzzy") {
      return invalidParams("conditions.params.match_mode");
    }
  }
  for (const key of ["min_confidence", "fuzzy_threshold"] as const) {
    if (params[key] !== undefined) {
      const f = asFinite(params[key]);
      if (f === null || f < 0 || f > 1) {
        return invalidParams(`conditions.params.${key}`);
      }
    }
  }
  if (params.case_sensitive !== undefined && typeof params.case_sensitive !== "boolean") {
    return invalidParams("conditions.params.case_sensitive");
  }
  return null;
}

/**
 * color params：color_space 仅 rgb|hsv（hsc 生成即 INVALID_PARAMS）；
 * color/tolerance 随空间取色型；HSV color h∈[0,360)（≤359 合法，02/schema）、
 * 容差 h∈[0,180]（圆周最短距离）、s/v∈[0,100]。
 */
function validateColorParams(params: unknown): TypeError | null {
  if (!isRecord(params)) {
    return invalidParams("conditions.params");
  }
  const space = nonEmptyString(params.color_space);
  if (space !== "rgb" && space !== "hsv") {
    return invalidParams("conditions.params.color_space");
  }
  if (!isRecord(params.color)) {
    return invalidParams("conditions.params.color");
  }
  const color = params.color;
  const tolerance = params.tolerance;
  if (space === "rgb") {
    for (const key of ["r", "g", "b"] as const) {
      const ch = asInteger(color[key]);
      if (ch === null || ch < 0 || ch > 255) {
        return invalidParams(`conditions.params.color.${key}`);
      }
    }
    if (tolerance !== undefined) {
      if (!isRecord(tolerance)) {
        return invalidParams("conditions.params.tolerance");
      }
      for (const key of ["r", "g", "b"] as const) {
        const ch = asInteger(tolerance[key]);
        if (ch === null || ch < 0 || ch > 255) {
          return invalidParams(`conditions.params.tolerance.${key}`);
        }
      }
    }
  } else {
    // HSV：color.h ∈ [0,360)（≤359；schema maximum 359 同口径）
    const h = asInteger(color.h);
    if (h === null || h < 0 || h >= 360) {
      return invalidParams("conditions.params.color.h");
    }
    for (const key of ["s", "v"] as const) {
      const ch = asInteger(color[key]);
      if (ch === null || ch < 0 || ch > 100) {
        return invalidParams(`conditions.params.color.${key}`);
      }
    }
    if (tolerance !== undefined) {
      if (!isRecord(tolerance)) {
        return invalidParams("conditions.params.tolerance");
      }
      const th = asInteger(tolerance.h);
      if (th === null || th < 0 || th > 180) {
        return invalidParams("conditions.params.tolerance.h");
      }
      for (const key of ["s", "v"] as const) {
        const ch = asInteger(tolerance[key]);
        if (ch === null || ch < 0 || ch > 100) {
          return invalidParams(`conditions.params.tolerance.${key}`);
        }
      }
    }
  }
  if (params.min_pixel_count !== undefined) {
    const n = asInteger(params.min_pixel_count);
    if (n === null || n < 1) {
      return invalidParams("conditions.params.min_pixel_count");
    }
  }
  if (params.min_percentage !== undefined) {
    const f = asFinite(params.min_percentage);
    if (f === null || f < 0 || f > 100) {
      return invalidParams("conditions.params.min_percentage");
    }
  }
  return null;
}

/** Group 字段校验（§3.2：depth 0..2；同层同名唯一性归 Repository）。 */
export function validateGroupFields(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("group");
  }
  const missing = requireKeys(v, GROUP_KEYS, "group");
  if (missing !== null) {
    return missing;
  }
  if (nonEmptyString(v.id) === null) {
    return invalidParams("id");
  }
  const revision = asInteger(v.revision);
  if (revision === null || revision < 1) {
    return invalidParams("revision");
  }
  const nameErr = validateNameValue(v.name, "name");
  if (nameErr !== null) {
    return nameErr;
  }
  if (v.parent_group_id !== null && nonEmptyString(v.parent_group_id) === null) {
    return invalidParams("parent_group_id");
  }
  const depth = asInteger(v.depth);
  if (depth === null || depth < 0 || depth > 2) {
    return invalidParams("depth");
  }
  const createdErr = validateRfc3339(v.created_at, "created_at");
  if (createdErr !== null) {
    return createdErr;
  }
  const updatedErr = validateRfc3339(v.updated_at, "updated_at");
  if (updatedErr !== null) {
    return updatedErr;
  }
  return null;
}

/** Script 校验（source_path 形态 + 组件判断；parameters required/default 约束）。 */
export function validateScript(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("script");
  }
  const missing = requireKeys(v, SCRIPT_KEYS, "script");
  if (missing !== null) {
    return missing;
  }
  if (nonEmptyString(v.id) === null) {
    return invalidParams("id");
  }
  const revision = asInteger(v.revision);
  if (revision === null || revision < 1) {
    return invalidParams("revision");
  }
  const nameErr = validateNameValue(v.name, "name");
  if (nameErr !== null) {
    return nameErr;
  }
  const sourcePath = nonEmptyString(v.source_path);
  if (sourcePath === null) {
    return invalidParams("source_path");
  }
  const pathErr = validateScriptSourcePath(sourcePath);
  if (pathErr !== null) {
    return pathErr;
  }
  const sha = nonEmptyString(v.source_sha256);
  if (sha === null || !isSha256Hex(sha)) {
    return invalidParams("source_sha256");
  }
  if (!Array.isArray(v.parameters)) {
    return invalidParams("parameters");
  }
  for (const p of v.parameters) {
    if (!isRecord(p)) {
      return invalidParams("parameters");
    }
    if (nonEmptyString(p.name) === null) {
      return invalidParams("parameters");
    }
    const ptype = nonEmptyString(p.type);
    if (ptype !== "string" && ptype !== "number" && ptype !== "boolean" && ptype !== "object" && ptype !== "array") {
      return invalidParams("parameters");
    }
    if (typeof p.required !== "boolean") {
      return invalidParams("parameters");
    }
    // 02：required=true 时 default 必须 null（缺失或非 null 均违例）
    if (p.required && (!("default" in p) || p.default !== null)) {
      return invalidParams("parameters");
    }
    if (typeof p.description !== "string") {
      return invalidParams("parameters");
    }
  }
  const createdErr = validateRfc3339(v.created_at, "created_at");
  if (createdErr !== null) {
    return createdErr;
  }
  const updatedErr = validateRfc3339(v.updated_at, "updated_at");
  if (updatedErr !== null) {
    return updatedErr;
  }
  return null;
}

/** source_path 校验：`scripts/<seg>/<hash>.lua`；组件判断拒绝 `..`/`.`/空段/绝对形态。 */
function validateScriptSourcePath(path: string): TypeError | null {
  if (path.startsWith("/") || path.startsWith("\\")) {
    return invalidParams("source_path");
  }
  const normalized = path.replaceAll("\\", "/");
  const segments = normalized.split("/");
  // 形态：scripts / <id> / <hash>.lua（恰好 3 段）
  if (segments.length !== 3 || segments[0] !== "scripts") {
    return invalidParams("source_path");
  }
  for (const seg of segments.slice(1)) {
    if (seg.length === 0 || seg === ".." || seg === ".") {
      return invalidParams("source_path");
    }
  }
  const file = segments[2] ?? "";
  if (!file.endsWith(".lua") || file === ".lua") {
    return invalidParams("source_path");
  }
  return null;
}
