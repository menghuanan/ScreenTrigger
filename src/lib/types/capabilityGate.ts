/**
 * capability gate（TASK-006 设计 §7；功能 → 所需能力映射表 + 评估/hook）。
 *
 * 规则（§7.3）："缺能力不显示可执行按钮"——按钮不渲染或禁用 + unavailable
 * 徽标与 reason；能力恢复（未来任务把 state 改 available）无需 UI 改码——由
 * 本映射表驱动。运维按钮（如"启动引擎"）永不受 gate；配置查看/修改为框架
 * 命令，不受 gate（§7.3 表）。
 *
 * 注意：gate 只评估能力维度；"引擎须就绪"由调用方（DisabledBusinessPanel）
 * 以 engineState === "ready" 叠加（TASK-004 §4.5 公式的 per-capability 化）。
 */

import { useMemo } from "react";

import type { CapabilityView } from "../ipc/types";
import type { CapabilityId } from "./capabilities";

/**
 * 业务功能七类（§7.3 映射表逐字；不含"配置查看/修改"——无能力需求不受 gate）。
 */
export type BusinessFunction =
  | "recognition_test" // 识别测试/OCR 相关
  | "script_run_debug" // 脚本运行/调试
  | "template_match_test" // 模板匹配测试
  | "contour_match_test" // 轮廓匹配测试
  | "color_match_test" // 颜色匹配测试
  | "input_actions" // 键鼠执行类
  | "capture_selection"; // 框选/导入/捕获类

/** 功能 → 所需能力映射表（§7.3 逐字；骨架期全部 unavailable → 恒禁用为真实状态）。 */
const REQUIRED_CAPABILITIES: Record<BusinessFunction, readonly CapabilityId[]> = {
  recognition_test: ["ocr"],
  script_run_debug: ["lua_script"],
  template_match_test: ["template_match"],
  contour_match_test: ["contour_match"],
  color_match_test: ["color_match"],
  input_actions: ["input"],
  capture_selection: ["capture"],
};

/** 全部业务功能（供 UI 遍历渲染）。 */
export const BUSINESS_FUNCTIONS: readonly BusinessFunction[] = [
  "recognition_test",
  "script_run_debug",
  "template_match_test",
  "contour_match_test",
  "color_match_test",
  "input_actions",
  "capture_selection",
];

/** 功能的中文名（UI 呈现用）。 */
export const BUSINESS_FUNCTION_LABELS: Record<BusinessFunction, string> = {
  recognition_test: "识别测试/OCR",
  script_run_debug: "脚本运行/调试",
  template_match_test: "模板匹配测试",
  contour_match_test: "轮廓匹配测试",
  color_match_test: "颜色匹配测试",
  input_actions: "键鼠执行",
  capture_selection: "框选/导入/捕获",
};

/** 功能所需能力列表（只读）。 */
export function requiredCapabilitiesFor(fn: BusinessFunction): readonly CapabilityId[] {
  return REQUIRED_CAPABILITIES[fn];
}

/** 单个能力的 gate 评估结果。 */
export interface CapabilityBlocker {
  id: CapabilityId | string;
  state: string;
  /** 能力缺失原因（unavailable 时的 reason；可能为 null）。 */
  reason: string | null;
}

/** 功能级 gate 评估结果。 */
export interface CapabilityGateEvaluation {
  /** true = 能力齐备，按钮可启用（引擎就绪与否由调用方叠加）。 */
  allowed: boolean;
  /** 阻断按钮的能力列表（state !== "available"）。 */
  blocking: CapabilityBlocker[];
  /** 首个阻断原因的 UI 文案（tooltip 用；allowed 时为 "能力已就绪"）。 */
  reasonText: string;
}

/**
 * 评估业务功能的能力 gate：所需能力任一缺失（无上报/非 available）即阻断。
 * 引擎未上报某能力（列表缺失）视为阻断（不得虚假可用）。
 */
export function evaluateCapabilityGate(
  fn: BusinessFunction,
  capabilities: CapabilityView[],
): CapabilityGateEvaluation {
  const required = REQUIRED_CAPABILITIES[fn];
  const blocking: CapabilityBlocker[] = [];
  for (const id of required) {
    const cap = capabilities.find((c) => c.id === id);
    if (cap === undefined) {
      blocking.push({ id, state: "not_reported", reason: "引擎未上报该能力" });
    } else if (cap.state !== "available") {
      blocking.push({ id: cap.id, state: cap.state, reason: cap.reason });
    }
  }
  const allowed = blocking.length === 0;
  const first = blocking[0];
  const reasonText = allowed
    ? "能力已就绪"
    : first === undefined
      ? "能力未就绪"
      : `${first.id} 不可用（${first.state}${first.reason !== null ? `：${first.reason}` : ""}）`;
  return { allowed, blocking, reasonText };
}

/** React hook：memoized gate 评估（§7.2 useCapability(fn)）。 */
export function useCapability(
  fn: BusinessFunction,
  capabilities: CapabilityView[],
): CapabilityGateEvaluation {
  return useMemo(() => evaluateCapabilityGate(fn, capabilities), [fn, capabilities]);
}
