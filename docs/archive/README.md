# 历史开发文档

维护与功能适配从[现行文档](../README.md)进入。需要回查当时的设计、取舍或测试排期时，按下表查阅。历史稿中的环境要求、进度、目录名和命令保留当时口径，执行验收时以[当前矩阵](../agent/验收与测试.md)为准。

| 时点 | 文档 | 回查内容 |
|---|---|---|
| 首轮重构以前 | [v1 原项目说明](v1/README.md)、[v1 文档索引](v1/docs/README.md)、[原始文件哈希](v1/manifest.json) | 11 份原稿的内容及校验值 |
| 首次文档重构评审 | [原稿审查](initial-development/原稿审查.md) | 当时的六个问题、发现的矛盾和取舍依据 |
| 首版设计 | [项目概览](initial-development/00-项目概览.md)、[模块拆分](initial-development/01-模块拆分.md)、[功能流程图](initial-development/05-功能流程图.md) | 最初的模块边界、候选接口、状态机与时序 |
| 探针与开发 | [实现细节清单](initial-development/07-实现细节清单.md)、[前期开发指南](initial-development/开发指南.md) | 候选方案、工具链探针与启动记录 |
| 原生前端迁移 | [WinUI 3 迁移设计](initial-development/winui3-migration.md) | 前端替换背景、页面迁移顺序与当时的切换条件 |
| P0–P3 排期 | [阶段任务摘要](initial-development/agent/阶段任务.md)、[暂缓测试清单](initial-development/agent/deferred-testing.md) | 历次排期授权与当时的测试环境缺口 |

现行任务卡、产品规格和技术决策仍在各自目录中供检查器读取。历次审计、原始文件和正式回执仍保存在工作区的 `reports/`，账本沿用原始引用。
