# 文档目录

日常开发从下表进入。维护文档描述当前实现；产品规格、共享接口、验收矩阵分别保留各自的权威入口。需要追溯原始决定时，再进入历史归档。

| 当下的问题 | 入口 |
|---|---|
| 项目离交付还差什么，如何修、如何测、约需多久？ | [实现审查与剩余工期](maintenance/implementation-review.md) |
| 在哪里增加功能、怎样用当前面板验证？ | [开发规范与本机命令](maintenance/development.md) |
| 一个 UI 操作如何走到 Rust、Python、Lua 和磁盘？ | [技术架构与模块所有权](maintenance/architecture.md) |
| 怎样诊断现场问题、恢复备份、构造候选和判定发布？ | [维护手册](maintenance/operations.md) |
| 怎么安排功能适配、维护 UI、用户 UI 和独立打包？ | [后续开发规划与设计占位](maintenance/roadmap.md) |
| 如何安装、运行或写脚本？ | [用户指南](user/README.md) · [离线安装](user/离线安装.md) · [脚本入门](user/脚本入门.md) · [配置与故障排查](user/配置与故障排查.md) |

## 修改时才需展开的规格

| 修改对象 | 权威来源 |
|---|---|
| 产品范围、行为、成功标准 | [产品定义](product/产品定义.md) · [功能规格](product/功能规格.md) · [需求追踪](product/审查与需求追踪.md) |
| 字段、数据布局、版本与恢复 | [数据结构](02-数据结构.md) · [机器可读 schema](../schema/README.md) |
| 宿主与引擎通信、事件、取消 | [IPC 协议](03-IPC协议.md) |
| 脚本可见的函数与返回值 | [Lua 脚本 API](04-Lua脚本API.md) |
| 资源上限、错误、坐标、故障行为 | [边界条件与约束](06-边界条件与约束.md) |
| 平台、模型、依赖或默认值决策 | [技术决策](decisions/技术决策.md) · [依赖清单](../manifest/README.md) |
| 首版任务、前置和验收证据 | [任务卡索引](agent/任务拆解.md) · [验收矩阵与测量标准](agent/验收与测试.md) · [检查与闭环](agent/检查与闭环.md) · [执行约定](agent/执行约定.md) |

本地授权工作区另有 `docs/agent/check-state.json`、`reports/evidence/` 和 `reports/audits/`。账本登记实施、问题与授权，审计推导验收状态。其他人仅检出源码时仍可运行文档检查和工具测试，业务审计需取得对应的原始材料。

[历史归档](archive/README.md)统一存放早期开发设计、阶段排期与 v1 原稿。现行任务卡、测试编号及接口契约继续供工具和维护工作引用。
