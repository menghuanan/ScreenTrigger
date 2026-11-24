/**
 * TS 侧契约用例执行器（TASK-006 设计 §2.2/§9.1；三端同源）。
 *
 * 执行 schema/contract-cases.json 的 `type_validation` 用例（纯数据校验语义，
 * 平台无关），断言 TS 校验函数的 valid/code/field 与用例 expect 逐字一致；
 * name-101 边界用例按 `nameOverrideLength` 覆写生效长度（不内嵌 101 字符样本）。
 * 同时执行 configLimits 防漂移比对：contract-cases.thresholds.configLimits ↔
 * schema/config.schema.json configLimits ↔ 本端 CONFIG_LIMITS 常量（§9.1）。
 *
 * 执行方式说明（设计 §9.1"TS 构建期类型/单测跑同一份"）：本模块被 tsc 构建
 * 期类型检查覆盖（JSON 单一事实源被引入类型图）；运行期执行经仓库现有工具链
 * 完成（esbuild 打包 + node 运行，见 reports/TASK-006/raw/b4-ts-contract.log），
 * 不引入任何新 npm 依赖。config_patch 用例（11 条）不在 TS 执行面（§9.1 明确
 * 其执行端为 Rust contract_tests + Python test_config_service；TS 无配置 IO 层）。
 *
 * TASK-011：新增 `type-group-*-valid` 两条 group 结构用例（`applies_to` 含 python/rust），
 * 经下方既有 `case "group"` 分派（validateGroupFields）自动消费——分派表不变；
 * group 命令语义用例（kind=dispatch）执行端为 Python test_targets_contract（TS 无命令层）。
 *
 * TASK-012：新增 3 条 script 结构用例（`type-script-name-101-invalid`、
 * `type-script-parameters-not-array-invalid`、`type-script-minimal-valid`，`applies_to`
 * 含 python/rust），经下方既有 `case "script"` 分派（validateScript）自动消费——
 * 分派表不变；脚本源码预算/UTF-8/参数唯一性等**领域**规则（非实体 schema）用例的
 * 执行端为 Python test_scripts_domain（TS 无引擎领域层）。
 *
 * TASK-013：新增 `recognition_job` 用例（6 条识别命令 job 契约；`applies_to` 仅含 python，
 * **不在** TS type_validation 执行面）——其执行端为 Python `test_vision_contract`（TS 无识别引擎层）；
 * 识别结果 `{matches,truncated}`（Q4/02）无命令集合变更，总数仍 62。本执行器只消费
 * `kind === "type_validation"`，故 recognition_job 用例被自然忽略（分派表不变）。
 */

import contractCasesJson from "../../../schema/contract-cases.json";
import configSchemaJson from "../../../schema/config.schema.json";

import { CONFIG_LIMITS } from "./config";
import { validateGroupFields, validateScript, validateTarget } from "./conditions";
import { validateLocalRect, validatePoint, validateRegion, type TypeError } from "./region";
import {
  validateExecutionState,
  validateMonitorConfig,
  validateRunConfig,
} from "./runtime";

/** 单条用例执行结果。 */
export interface ContractCaseResult {
  id: string;
  passed: boolean;
  /** 失败时的差异说明；通过时为 "ok"。 */
  detail: string;
}

interface ContractExpect {
  valid: boolean;
  code?: string;
  field?: string;
}

interface TypeValidationCase {
  id: string;
  kind: string;
  applies_to?: string[];
  description?: string;
  typeName: string;
  value: unknown;
  expect: ContractExpect;
  nameOverrideLength?: number;
}

interface ContractCasesFile {
  version: number;
  thresholds: { configLimits: unknown };
  cases: TypeValidationCase[];
}

const contractCases = contractCasesJson as unknown as ContractCasesFile;

/** typeName → TS 校验函数（与 B3 Rust contract_tests 的分派表同构）。 */
function validatorFor(typeName: string): ((v: unknown) => TypeError | null) | null {
  switch (typeName) {
    case "region":
      return validateRegion;
    case "point":
      return validatePoint;
    case "local_rect":
      return validateLocalRect;
    case "target":
      return validateTarget;
    case "group":
      return validateGroupFields;
    case "script":
      return validateScript;
    case "run_config":
      return validateRunConfig;
    case "monitor_config":
      return validateMonitorConfig;
    case "execution_state":
      return validateExecutionState;
    default:
      return null;
  }
}

/**
 * nameOverrideLength 覆写：以"名".repeat(n) 生成 n 个 Unicode 码点的 name
 * （码点计数口径，见 conditions.ts validateNameValue 注释）。
 */
function applyNameOverride(value: unknown, overrideLength: number): void {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    (value as Record<string, unknown>).name = "名".repeat(overrideLength);
  }
}

/** 执行单条 type_validation 用例。 */
function runTypeValidationCase(c: TypeValidationCase): ContractCaseResult {
  const validate = validatorFor(c.typeName);
  if (validate === null) {
    return { id: c.id, passed: false, detail: `未知 typeName: ${c.typeName}` };
  }
  // 深拷贝（用例值不得被校验器/覆写污染）
  const value = structuredClone(c.value) as unknown;
  if (typeof c.nameOverrideLength === "number") {
    applyNameOverride(value, c.nameOverrideLength);
  }
  const err = validate(value);
  if (c.expect.valid) {
    return err === null
      ? { id: c.id, passed: true, detail: "ok" }
      : { id: c.id, passed: false, detail: `期望合法，实际拒绝：${err.code} ${err.field}` };
  }
  if (err === null) {
    return { id: c.id, passed: false, detail: "期望拒绝，实际通过" };
  }
  if (err.code !== c.expect.code || err.field !== c.expect.field) {
    return {
      id: c.id,
      passed: false,
      detail: `错误定位不符：期望 ${c.expect.code}/${c.expect.field}，实际 ${err.code}/${err.field}`,
    };
  }
  return { id: c.id, passed: true, detail: "ok" };
}

/**
 * configLimits 三方防漂移比对（§9.1）：thresholds.configLimits ↔
 * schema/config.schema.json configLimits ↔ CONFIG_LIMITS。
 */
export function checkConfigLimitsDrift(): ContractCaseResult {
  const fromCases = contractCases.thresholds.configLimits;
  const fromSchema = (configSchemaJson as Record<string, unknown>).configLimits;
  const fromTs = CONFIG_LIMITS;
  const casesStr = JSON.stringify(fromCases);
  const schemaStr = JSON.stringify(fromSchema);
  // TS 常量为 readonly 数组/字面量——序列化后应与 JSON 逐字一致（键序一致）
  const tsNormalized = JSON.stringify(fromTs);
  if (casesStr !== schemaStr) {
    return { id: "configLimits-drift", passed: false, detail: "contract-cases 与 config.schema 的 configLimits 不一致" };
  }
  if (casesStr !== tsNormalized) {
    return {
      id: "configLimits-drift",
      passed: false,
      detail: `CONFIG_LIMITS 与 schema 不一致：\n  schema: ${casesStr}\n  ts:     ${tsNormalized}`,
    };
  }
  return { id: "configLimits-drift", passed: true, detail: "ok" };
}

/** 执行 TS 侧全部契约用例（type_validation 全集 + configLimits 比对）。 */
export function runTsContractCases(): ContractCaseResult[] {
  const results: ContractCaseResult[] = [checkConfigLimitsDrift()];
  for (const c of contractCases.cases) {
    if (c.kind === "type_validation") {
      results.push(runTypeValidationCase(c));
    }
  }
  return results;
}

/** 汇总断言：任一失败即抛错（供构建期/自验脚本直接调用）。 */
export function assertTsContractCases(): void {
  const results = runTsContractCases();
  const failed = results.filter((r) => !r.passed);
  for (const r of results) {
    console.log(`${r.passed ? "PASS" : "FAIL"} ${r.id}${r.passed ? "" : `: ${r.detail}`}`);
  }
  console.log(`total=${results.length} passed=${results.length - failed.length} failed=${failed.length}`);
  if (failed.length > 0) {
    throw new Error(`TS 契约用例失败 ${failed.length} 条`);
  }
}
