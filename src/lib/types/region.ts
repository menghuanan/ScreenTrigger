/**
 * 空间与通用类型（TASK-006 设计 §3.1/§6.1；02 权威逐字段，TS 侧落点）。
 *
 * 对齐基准：schema/types.schema.json（单一事实源）+ B3 Rust
 * src-tauri/src/types/region.rs（错误字段路径逐字一致）：
 * - space 枚举错 → `region.space`；负坐标（client/image）→ `region.x`/`region.y`
 *   （先 x 后 y）；宽高 <10 → INVALID_REGION `region.width`/`region.height`；
 * - 空间互斥：display_id 仅 screen、binding_id 仅 client、image_id 仅 image，
 *   先必填后错配；未知键 → `region.<key>`（additionalProperties:false 语义）；
 * - 与运行 binding 一致性/显示器归属属运行时校验（M01），本层不做。
 * snake_case 三端统一，禁止同义字段（Match 用 bbox/center、Region 用 space）。
 */

/** 类型层校验错误（镜像 Rust TypeError：06 错误码 + 字段路径定位）。 */
export interface TypeError {
  /** 06 错误码（类型层只产生 INVALID_PARAMS / INVALID_REGION / CONFLICT）。 */
  code: "INVALID_PARAMS" | "INVALID_REGION" | "CONFLICT";
  /** 完整点分字段路径（如 region.x、conditions.params.threshold）。 */
  field: string;
}

function invalidParams(field: string): TypeError {
  return { code: "INVALID_PARAMS", field };
}

function invalidRegion(field: string): TypeError {
  return { code: "INVALID_REGION", field };
}

/** 严格整数（schema "type":"integer" 语义：拒绝 bool/浮点/NaN）。 */
export function asInteger(v: unknown): number | null {
  return typeof v === "number" && Number.isInteger(v) ? v : null;
}

/** 有限数（拒绝 bool/NaN/Infinity）。 */
export function asFinite(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** 非空字符串提取。 */
export function nonEmptyString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

/** sha256 十六进制串校验（64 位 hex，schema pattern 语义）。 */
export function isSha256Hex(s: string): boolean {
  return s.length === 64 && /^[0-9a-fA-F]{64}$/.test(s);
}

/** 路径组件判断（02/07 §6：拒绝 `..`/`.` 组件——组件判断，非 startswith）。 */
export function pathComponentsEscaped(segments: string[]): boolean {
  return segments.some((s) => s === ".." || s === ".");
}

/** 数据根相对路径校验（model_directory / ImageAsset.path 共用口径）。 */
export function isRelativeDataRootPath(s: string): boolean {
  if (s.length === 0 || s.startsWith("/") || s.startsWith("\\")) {
    return false;
  }
  const normalized = s.replaceAll("\\", "/");
  const first = normalized.split("/")[0] ?? "";
  if (first.includes(":")) {
    return false; // 盘符（如 C:/models）
  }
  return !pathComponentsEscaped(normalized.split("/"));
}

/** RFC3339 UTC 轻校验（schema pattern：YYYY-MM-DDTHH:MM:SS[.mmm]Z）。 */
export function validateRfc3339(v: unknown, field: string): TypeError | null {
  const s = typeof v === "string" ? v : null;
  if (s === null) {
    return invalidParams(field);
  }
  const ok =
    s.length >= 20 &&
    s[4] === "-" &&
    s[7] === "-" &&
    (s[10] === "T" || s[10] === "t") &&
    s[13] === ":" &&
    s[16] === ":" &&
    s.endsWith("Z");
  return ok ? null : invalidParams(field);
}

// ---------------------------------------------------------------------------
// 类型（02/schema 逐字段；snake_case）
// ---------------------------------------------------------------------------

/** Rect（02）：左闭右开 [x,x+w)×[y,y+h)，宽高正整数；不携带空间。 */
export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Point（02）：单像素读取，不受 Region ≥10 限制，坐标可负。 */
export interface Point {
  x: number;
  y: number;
}

/** LocalRect（02）：条件局部 ROI，相对候选 bbox，宽高 ≥1（不受 10×10 约束）。 */
export interface LocalRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Region 空间三枚举（02）。 */
export type RegionSpace = "screen_physical" | "window_client_physical" | "image_pixel";
export const REGION_SPACES: readonly RegionSpace[] = [
  "screen_physical",
  "window_client_physical",
  "image_pixel",
];

/** Region（02）：空间互斥——三个 id 各随其空间出现（见各变体接口）。 */
export interface RegionBase {
  space: RegionSpace;
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface RegionScreen extends RegionBase {
  space: "screen_physical";
  display_id: string;
}
export interface RegionClient extends RegionBase {
  space: "window_client_physical";
  binding_id: string;
}
export interface RegionImage extends RegionBase {
  space: "image_pixel";
  image_id: string;
}
export type Region = RegionScreen | RegionClient | RegionImage;

/** DisplayInfo（02）：rect 为 screen 物理坐标；dpi_scale > 0。 */
export interface DisplayInfo {
  id: string;
  rect: Rect;
  dpi_scale: number;
  primary: boolean;
}

/** ImageRef（02）：引用只在当前引擎会话有效；持久化转 ImageAsset。 */
export interface ImageRef {
  image_id: string;
  width: number;
  height: number;
  format: "png" | "jpeg";
  sha256: string;
}

/** ImageAsset（02）：数据根相对 path、storage_mode 两枚举。 */
export interface ImageAsset {
  path: string;
  sha256: string;
  width: number;
  height: number;
  format: "png" | "jpeg";
  storage_mode: "original" | "png_lossless";
}

/** Frame（02）：未绑定 geometry_revision=0、binding_id=null；图像导入 origin=(0,0)。 */
export interface Frame {
  frame_id: string;
  image_ref: ImageRef;
  space: RegionSpace;
  origin: Point;
  captured_at: string;
  geometry_revision: number;
  binding_id: string | null;
}

// ---------------------------------------------------------------------------
// 校验纯函数（无 IO；输入为 JSON 反序列化值，与契约用例三端同源）
// ---------------------------------------------------------------------------

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Rect 值校验（带路径前缀；DisplayInfo.rect 共用；宽高 ≥1）。 */
function validateRectValue(v: unknown, prefix: string): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams(prefix);
  }
  for (const key of ["x", "y", "width", "height"] as const) {
    const n = asInteger(v[key]);
    if (n === null) {
      return invalidParams(`${prefix}.${key}`);
    }
    if ((key === "width" || key === "height") && n < 1) {
      return invalidParams(`${prefix}.${key}`);
    }
  }
  return null;
}

/** Region 纯校验（§6.1 全规则；错误路径与 contract-cases 用例逐字对齐）。 */
export function validateRegion(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("region");
  }
  // 0. 未知键（schema additionalProperties:false）
  const allowed = new Set(["space", "x", "y", "width", "height", "display_id", "binding_id", "image_id"]);
  for (const key of Object.keys(v)) {
    if (!allowed.has(key)) {
      return invalidParams(`region.${key}`);
    }
  }
  // 1. space 三枚举
  const spaceRaw = nonEmptyString(v.space);
  if (spaceRaw === null || !REGION_SPACES.includes(spaceRaw as RegionSpace)) {
    return invalidParams("region.space");
  }
  const space = spaceRaw as RegionSpace;
  // 2. x/y 整数；screen 可负，client/image 不可负（先 x 后 y）
  const x = asInteger(v.x);
  const y = asInteger(v.y);
  if (x === null) {
    return invalidParams("region.x");
  }
  if (y === null) {
    return invalidParams("region.y");
  }
  if (space !== "screen_physical" && (x < 0 || y < 0)) {
    return invalidParams(x < 0 ? "region.x" : "region.y");
  }
  // 3. 宽高整数 ≥10（10×10 下限；太小 → INVALID_REGION）
  const width = asInteger(v.width);
  if (width === null) {
    return invalidParams("region.width");
  }
  if (width < 10) {
    return invalidRegion("region.width");
  }
  const height = asInteger(v.height);
  if (height === null) {
    return invalidParams("region.height");
  }
  if (height < 10) {
    return invalidRegion("region.height");
  }
  // 4. 空间互斥：所属 id 必填 + 交叉字段禁止（先必填后错配）
  const present = (f: unknown): boolean => nonEmptyString(f) !== null;
  const exists = (f: unknown): boolean => f !== undefined;
  if (space === "screen_physical") {
    if (!present(v.display_id)) {
      return invalidParams("region.display_id");
    }
    if (exists(v.binding_id)) {
      return invalidParams("region.binding_id");
    }
    if (exists(v.image_id)) {
      return invalidParams("region.image_id");
    }
  } else if (space === "window_client_physical") {
    if (!present(v.binding_id)) {
      return invalidParams("region.binding_id");
    }
    if (exists(v.display_id)) {
      return invalidParams("region.display_id");
    }
    if (exists(v.image_id)) {
      return invalidParams("region.image_id");
    }
  } else {
    if (!present(v.image_id)) {
      return invalidParams("region.image_id");
    }
    if (exists(v.display_id)) {
      return invalidParams("region.display_id");
    }
    if (exists(v.binding_id)) {
      return invalidParams("region.binding_id");
    }
  }
  return null;
}

/** Point 纯校验（单像素，坐标可负，不受 ≥10 限制）。 */
export function validatePoint(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("point");
  }
  for (const key of Object.keys(v)) {
    if (key !== "x" && key !== "y") {
      return invalidParams(`point.${key}`);
    }
  }
  if (asInteger(v.x) === null) {
    return invalidParams("point.x");
  }
  if (asInteger(v.y) === null) {
    return invalidParams("point.y");
  }
  return null;
}

/** LocalRect 纯校验（宽高 ≥1；0 宽 → local_rect.width）。 */
export function validateLocalRect(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("local_rect");
  }
  for (const key of Object.keys(v)) {
    if (key !== "x" && key !== "y" && key !== "width" && key !== "height") {
      return invalidParams(`local_rect.${key}`);
    }
  }
  if (asInteger(v.x) === null) {
    return invalidParams("local_rect.x");
  }
  if (asInteger(v.y) === null) {
    return invalidParams("local_rect.y");
  }
  const width = asInteger(v.width);
  if (width === null) {
    return invalidParams("local_rect.width");
  }
  if (width < 1) {
    return invalidParams("local_rect.width");
  }
  const height = asInteger(v.height);
  if (height === null) {
    return invalidParams("local_rect.height");
  }
  if (height < 1) {
    return invalidParams("local_rect.height");
  }
  return null;
}

/** DisplayInfo 轻校验（id 非空、dpi_scale>0；rect 结构校验）。 */
export function validateDisplayInfo(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("display_info");
  }
  if (nonEmptyString(v.id) === null) {
    return invalidParams("display_info.id");
  }
  const dpi = asFinite(v.dpi_scale);
  if (dpi === null || dpi <= 0) {
    return invalidParams("display_info.dpi_scale");
  }
  if (typeof v.primary !== "boolean") {
    return invalidParams("display_info.primary");
  }
  return validateRectValue(v.rect, "display_info.rect");
}

/** ImageRef 轻校验（id 非空、format 两枚举、sha256 hex64）。 */
export function validateImageRef(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("image_ref");
  }
  if (nonEmptyString(v.image_id) === null) {
    return invalidParams("image_ref.image_id");
  }
  if (asInteger(v.width) === null) {
    return invalidParams("image_ref.width");
  }
  if (asInteger(v.height) === null) {
    return invalidParams("image_ref.height");
  }
  const format = nonEmptyString(v.format);
  if (format !== "png" && format !== "jpeg") {
    return invalidParams("image_ref.format");
  }
  const sha = nonEmptyString(v.sha256);
  if (sha === null || !isSha256Hex(sha)) {
    return invalidParams("image_ref.sha256");
  }
  return null;
}

/** ImageAsset 校验（path 数据根相对、format/storage_mode 枚举、sha256 hex64）。 */
export function validateImageAsset(v: unknown): TypeError | null {
  if (!isRecord(v)) {
    return invalidParams("image");
  }
  const path = nonEmptyString(v.path);
  if (path === null || !isRelativeDataRootPath(path)) {
    return invalidParams("image.path");
  }
  const sha = nonEmptyString(v.sha256);
  if (sha === null || !isSha256Hex(sha)) {
    return invalidParams("image.sha256");
  }
  if (asInteger(v.width) === null) {
    return invalidParams("image.width");
  }
  if (asInteger(v.height) === null) {
    return invalidParams("image.height");
  }
  const format = nonEmptyString(v.format);
  if (format !== "png" && format !== "jpeg") {
    return invalidParams("image.format");
  }
  const mode = nonEmptyString(v.storage_mode);
  if (mode !== "original" && mode !== "png_lossless") {
    return invalidParams("image.storage_mode");
  }
  return null;
}
