# ScreenTrigger

ScreenTrigger 是 Windows x64 上的本地视觉自动化工具。用户通过屏幕框选或导入图片建立目标，用 Lua 5.4 执行识别、监控和受控键鼠操作，也可以运行纯脚本。桌面应用使用 WinUI 3，Rust 宿主管理引擎、窗口、热键与输入，Python 3.13 负责识别、脚本编排和本地数据。

**当前交付是 RC11 候选。** 真实主窗口已接入截图与导入、目标库、脚本、测试识别、运行设置、区域监控、历史及设置。已有安装、备份恢复、停止和两小时运行记录。韩语识别质量、阻塞推理的回收、若干交互及正式验收证据仍需补齐。详见[实现审查与剩余工期](docs/maintenance/implementation-review.md)。

## 开发与使用

| 要做的事 | 从这里开始 |
|---|---|
| 构建并运行现有界面、开发功能适配 | [开发规范](docs/maintenance/development.md) |
| 找模块、命令、数据及接口所有者 | [技术架构](docs/maintenance/architecture.md) |
| 复现问题、恢复数据、核对候选包、执行验收 | [维护手册](docs/maintenance/operations.md) |
| 规划现有测试面板、新用户端及分包 | [后续开发顺序与待定输入](docs/maintenance/roadmap.md) |
| 安装、建立目标、编写和调试脚本 | [用户指南](docs/user/README.md) |

后续先在现有界面上适配功能，把它保留为开发维护的可视化测试面板。新的用户端按产品方提供的美术设计另行开发，两端分开封装。当前候选仍只有一个桌面应用入口。

按主题查找[完整文档目录](docs/README.md)。早期设计、迁移计划和原始稿集中在[历史归档](docs/archive/README.md)。
