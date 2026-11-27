# ScreenTrigger

ScreenTrigger 是 Windows x64 上的本地视觉自动化工具。用户通过屏幕框选或导入图片建立目标，用 Lua 5.4 执行识别、监控和受控键鼠操作，也可以运行纯脚本。桌面应用使用 WinUI 3，Rust 宿主管理引擎、窗口、热键与输入，Python 3.13 负责识别、脚本编排和本地数据。

完整解压便携包到可写目录，双击根目录的 `ScreenTrigger.exe`。初次解压时，旁边只有 `resources` 和使用说明。首次运行会新建 `data` 保存个人数据。左侧依次为编写脚本、目标分组、日志、关于。脚本行可绑定窗口内热键并开始或停止执行。关于页提供项目链接和手动检查更新。

## 开发与使用

| 要做的事 | 从这里开始 |
|---|---|
| 构建并运行现有界面、开发功能适配 | [开发规范](docs/maintenance/development.md) |
| 找模块、命令、数据及接口所有者 | [技术架构](docs/maintenance/architecture.md) |
| 复现问题、恢复数据、核对候选包、执行验收 | [维护手册](docs/maintenance/operations.md) |
| 规划现有测试面板、新用户端及分包 | [后续开发顺序与待定输入](docs/maintenance/roadmap.md) |
| 安装、建立目标、编写和调试脚本 | [用户指南](docs/user/README.md) |

开发者在源码工作区运行 `tools/start-dev-panel.ps1` 可使用原有的截图、诊断、调试和测试面板。便携构建从[用户端工程](desktop/ScreenTrigger.User/ScreenTrigger.User.csproj)的发布清单取文件。[打包命令](docs/maintenance/operations.md)

按主题查找[完整文档目录](docs/README.md)。早期设计、迁移计划和原始稿集中在[历史归档](docs/archive/README.md)。
