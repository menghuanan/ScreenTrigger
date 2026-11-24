# schema/ — 三端协议一致性唯一来源

## 文件

| 文件 | 用途 |
|---|---|
| `envelope.schema.json` | NDJSON 信封 JSON Schema（draft-07）：五类型（request/response/event/host_request/host_response）必填集逐字对齐 TASK-005 设计 §5.4、错误四字段 `{code,message,details,retryable}`（docs/06）、hello data 六字段（docs/03）。 |
| `contract-cases.json` | 双端共享契约用例集：`thresholds`（三端共享阈值：2MiB/10000/100000/32/10/5s/30s、host 白名单、可合并事件、TASK-006 增 `configLimits`）+ `cases`（TASK-005 39 条协议用例 + TASK-006 新增 kind=`type_validation`/`config_patch` 用例）。 |
| `types.schema.json` | **TASK-006**：02 共享类型的机器可校验声明（Region/Rect/Point/LocalRect/DisplayInfo/ImageRef/ImageAsset/Frame/Target+条件树四类叶 params/Group/Script/RunConfig/MonitorConfig/ExecutionState/错误结构+06 全量错误码枚举/Capability/ProviderManifest/ModelManifest）。三端单一事实源，与 02 冲突以 02 为准；跨字段约束（canny low<high、mode 条件必填/必 null、条件树叶数 1–10/深度 ≤4/叶 ID 唯一/anchor 存在性、16KiB metadata、required=true→default null）由三端 validate() 纯函数实现。 |
| `config.schema.json` | **TASK-006**：config.json 文档完整 schema + 逐字段默认值（02 示例等价）+ `configLimits` 范围常量块。未知字段拒绝；model_directory 拒绝绝对路径/盘符/..（07 §6 组件判断）；schema_version>2 → SCHEMA_UNSUPPORTED、坏档 → DATA_CORRUPT（语义见 TASK-006 设计 §5.3）。 |

## 三端一致性验证方法

- **Python**：
  - TASK-005：`backend/tests/test_ipc_contract.py` 读 `contract-cases.json`，对 `applies_to` 含 `"python"` 的协议用例以内存 transport 替身 + 注入时钟执行；
  - TASK-006：`backend/tests/test_types_contract.py` 读同一份文件的 `type_validation` 用例驱动 `backend/types/` 的 validate() 纯函数（`config_patch` 用例的执行方为 `test_config_service.py` 与 Rust contract_tests，B2/B3 接入）；
  - 阈值/范围常量一律从用例集读取比对（`shared_*_match_contract_cases` 先例），禁止实现侧硬编码偏离。
- **Rust**：已有 `src-tauri/src/ipc/contract_tests.rs`（TASK-005）与 `src-tauri/src/types/contract_tests.rs`（TASK-006 B3）读同一份文件，提取到 `native/host/` 后继续运行相同用例；Rust 侧只按 `include_str!` 读 thresholds 固定字段与显式挑选的用例 id，**新增 kind/用例/thresholds 键不影响既有断言**（向后兼容约定：只增不改）。
- **原生前端**：TASK-006 将 `desktop/ScreenTrigger/Contracts/` 中的 C# DTO、字段校验和错误定位与同一套 schema/cases 对齐。原 TS 类型和 JSON 注释中的旧实现路径保留作迁移对照。
- 任何一端契约测试失败时，以本目录文件为准仲裁：要么实现错，要么 schema/cases 需按"变更规则"修订。

## 变更规则

1. **协议语义/类型变更**（必填集、错误码、阈值、类型字段、configLimits）必须先改本目录 schema/cases 并同步 docs/02、03、06 与三端实现，禁止单端先行。
2. `contract-cases.json` 的 `thresholds`（含 `configLimits`）是常量事实源：测试代码**不得**各自硬编码数值断言（应从用例集读取比对）。
3. 新增用例须带 `applies_to` 与 `expect` 字段；`expect.reason` 取值必须与两端解析器抛出的畸形原因串逐字一致（如 `missing_field:id`、`data_error_exclusive`）；`type_validation` 的 `expect.code/field` 与三端 validate() 错误定位逐字一致（如 `INVALID_REGION`+`region.width`）。过滤口径：`type_validation` 用例默认全端通用（目标执行端为 Rust、Python、C#；旧 TS 客户端用例由 C# 等价复验），`applies_to` 仅标注平台特定用例，缺省=全端适用。
4. `session_id` 在 envelope schema 层必填；运行时缺失/不匹配帧的"丢弃不计数"行为（用户冻结契约③）属于各端运行时语义，不体现在 schema 的 required 中。
5. 向后兼容：`cases`/`thresholds` 只增不改；删除或修改既有条目须三端同步并递增 `version`。

## 版本

- `contract-cases.json` version 2（TASK-006 扩展 type_validation/config_patch/configLimits）；envelope/types/config schema 随 TASK-005/006 初版交付。后续语义变更递增版本，旧证据按仓库约定登记 STALE。
