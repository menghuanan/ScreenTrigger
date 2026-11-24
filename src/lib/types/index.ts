/** 共享类型与校验 barrel（TASK-006 设计 §4.4）。 */

export * from "./region";
export * from "./conditions";
export * from "./runtime";
export * from "./capabilities";
export * from "./config";
export { runTsContractCases, checkConfigLimitsDrift, assertTsContractCases } from "./contractCases";
export type { ContractCaseResult } from "./contractCases";
export {
  BUSINESS_FUNCTIONS,
  requiredCapabilitiesFor,
  evaluateCapabilityGate,
  useCapability,
} from "./capabilityGate";
export type { BusinessFunction, CapabilityGateEvaluation } from "./capabilityGate";
