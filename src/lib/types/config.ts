/**
 * 配置文档类型与 configLimits 常量（TASK-006 设计 §4.4/§5.1/§6.3；02 权威）。
 *
 * 对齐基准：schema/config.schema.json（字段/枚举/范围/默认值单一事实源，冲突
 * 以 02 为准）。TS 侧职责（§4.4）：**Config 文档类型 + configLimits 常量**——
 * 无本地配置逻辑（配置权威在 Python backend/config/，Rust 仅路由透传，§2.3）；
 * 常量与 schema 的防漂移比对在 contractCases.ts 执行（§9.1 configLimits 运行时
 * 比对的三端同源先例）。
 *
 * 敏感字段结论（§5.4）：本 schema 无任何凭据/密钥/令牌字段，无需脱敏。
 */

// ---------------------------------------------------------------------------
// configLimits（与 schema/config.schema.json configLimits 块逐字一致）
// ---------------------------------------------------------------------------

/** 02 范围常量（三端共享事实源；contractCases.ts 运行时与 schema 比对）。 */
export const CONFIG_LIMITS = {
  defaultFrequencyHz: { min: 0.1, max: 30 },
  threshold: { min: 0, max: 1 },
  ocrMinConfidence: { min: 0, max: 1 },
  pngCompressionLevel: { min: 0, max: 9 },
  previewHz: { min: 1, max: 20 },
  moveDurationMs: { min: 0, max: 5000 },
  typeIntervalMs: { min: 0, max: 5000 },
  totalTimeoutMs: { min: 0 },
  rotateBytesMin: 1048576,
  rotateBytesMax: 104857600,
  fileCount: { min: 1, max: 20 },
  themes: ["system", "light", "dark"],
  loggingLevels: ["debug", "info", "warning", "error"],
  languages: ["zh-CN"],
} as const;

// ---------------------------------------------------------------------------
// Config 文档类型（02 config.json 七节；snake_case 逐字段）
// ---------------------------------------------------------------------------

export interface GeneralConfig {
  language: "zh-CN";
  theme: "system" | "light" | "dark";
}

export interface ToleranceRgbConfig {
  r: number;
  g: number;
  b: number;
}

export interface ToleranceHsvConfig {
  h: number;
  s: number;
  v: number;
}

export interface RecognitionConfig {
  default_frequency_hz: number;
  default_threshold: number;
  rgb_tolerance: ToleranceRgbConfig;
  hsv_tolerance: ToleranceHsvConfig;
  /** 可 null 字段（§5.4 深合并：允许显式置 null）。 */
  ocr_model_id: string | null;
  ocr_min_confidence: number;
  /** 数据根相对目录（禁绝对路径/盘符/..，02/07 §6 组件判断）。 */
  model_directory: string;
}

export interface CaptureConfig {
  preview_hz: number;
  png_compression_level: number;
}

export interface InputConfig {
  allow_exit: boolean;
  move_duration_ms: number;
  type_interval_ms: number;
}

export interface ExecutionConfig {
  total_timeout_ms: number;
}

/**
 * hotkeys（02）：本任务仅纯数据保存；注册生效与回滚属热键任务（01 完整语义
 * 边界，设计 §1.2/§13-3）。
 */
export interface HotkeysConfig {
  start_selection: string;
  stop_script: string;
  pause_script: string;
}

export interface LoggingConfig {
  level: "debug" | "info" | "warning" | "error";
  rotate_bytes: number;
  file_count: number;
}

/** 完整 Config 文档（get_config/update_config/reset_config 返回的 data）。 */
export interface ConfigDocument {
  /** schema_version=2；>2 → SCHEMA_UNSUPPORTED（拒绝写入并提示升级）。 */
  schema_version: number;
  /** 初始 1、每次 update/reset 递增（§5.4）。 */
  revision: number;
  general: GeneralConfig;
  recognition: RecognitionConfig;
  capture: CaptureConfig;
  input: InputConfig;
  execution: ExecutionConfig;
  hotkeys: HotkeysConfig;
  logging: LoggingConfig;
}

/** update_config 深合并语义（§5.4）：对象递归合并；标量替换；数组整体替换。 */
export type ConfigPatch = Record<string, unknown>;
