# ScreenTrigger 全前端 WinUI 3 迁移设计

**可以整体迁移。** ScreenTrigger 首发平台为 Windows 10/11 x64，现有识别、脚本和存储通过宿主命令提供服务。迁移的主要成本是替换桌面宿主与界面的连接、重做实时框选，以及验证已有数据和离线安装。正式前端采用 WinUI 3，包含现有功能和首版待开发界面。[技术决策 D01、D02、D09、D10](decisions/技术决策.md)

## 现状与迁移范围

当前可追溯的实现入口是 `src/App.tsx` 和 `src-tauri/src/lib.rs`。前者按窗口标签加载主页面或覆盖层，后者注册命令、发送事件、装配输入闸门并管理 Python 子进程。框选窗口由 `src-tauri/src/selection/wiring.rs` 创建并加载 `index.html`。TASK-024 至 TASK-026 的运行、调试和状态交互，以及 TASK-027 的正式打包仍登记为未开始。此时转向可以避免先在网页端实现这些页面再重写。下表的 WinUI 3 界面均为待实施设计。

| 界面 | 仓库现状 | WinUI 3 交付与验证 |
|---|---|---|
| 主窗口、引擎健康与 OCR 能力 | 主入口已挂载健康、能力及启停面板 | XAML 主窗口包含导航、能力说明、诊断、暂停和停止入口。缺模型时仍能进入纯脚本流程。TASK-004、TASK-015、TASK-026 |
| 屏幕框选、实时预览及图片导入 | 独立 WebView 覆盖层、文件选择令牌和图像引用已存在 | 独立 WinUI 3 顶层窗口绘制选框、物理尺寸和动态 ROI。原生主窗发起系统文件选择，宿主签发一次性导入令牌。TASK-002、TASK-008、TASK-009 |
| 目标库、分组、条件与识别测试 | 已有目标组件，主入口尚未接入目标库。识别测试面板已挂载 | 从主导航进入目标库，编辑分组和目标，显示条件树、候选框、样本编号、逐条件证据及取消结果。TASK-011、TASK-016 |
| Lua 编辑器与调试器 | 已有脚本列表和编辑组件，主入口尚未接入。完整调试属后续任务 | XAML 实现列表、源码编辑、语法定位、断点、单步、只读变量和栈。保存冲突保留草稿。TASK-012、TASK-025 |
| 窗口选择、运行与区域监控 | 产品规格已定义，完整运行设置页待实施 | 原生窗口选择、区域预览、运行和监控表单呈现当前绑定、频率、命中和终止原因。TASK-018、TASK-019、TASK-023、TASK-024 |
| 设置、历史、恢复及所有应用弹窗 | 设置、日志和恢复的完整交互待实施 | XAML 页面及 ContentDialog 承载配置、热键冲突、错误、未保存确认和备份恢复。普通运行只显示状态，调试暂停时展示实时日志。TASK-019、TASK-026、TASK-028 |

所有应用自绘界面使用 `Microsoft.UI.Xaml`。操作系统提供的文件选择器、快捷键和窗口句柄通过原生接口使用。主窗口、额外窗口、编辑器、预览、错误页和对话框均纳入同一发布验收。网页版编辑器、嵌入式 WebView、浏览器后备入口以及另一套桌面 UI 均不作为交付方案。

## 宿主与数据边界

1. **C# / XAML 桌面应用**拥有应用窗口、焦点和控件状态。主窗口和覆盖层共用一个宿主会话和事件订阅，视图在 `DispatcherQueue` 上更新。关闭覆盖层只结束框选，退出应用才关闭引擎。
2. **Rust 原生宿主 DLL**与 WinUI 同进程。迁移既有 `sidecar`、`ipc`、`input`、`selection` 的可复用部分，将 Tauri 的窗口、`invoke`、事件和退出钩子替换为版本化 C ABI 与窗口适配。Rust 仍负责引擎进程树、输入租约、窗口身份、全局热键、停止和释放按键。停止热键由 Rust 独立消息线程处理，界面忙时也能关闸。
3. **Python 3.13.x 子进程**继续处理识别、Lua 5.4、配置和 JSON Repository。Rust 与 Python 沿用 UTF-8 NDJSON v1、请求 ID、会话和事件序号。C# 数据契约使用相同的 snake_case 字段、错误码和共享测试向量，UI 通过命令读取或修改用户数据。[协议与宿主适配](03-IPC协议.md) · [数据模型与目录所有权](02-数据结构.md)

建议工程位置为 `desktop/ScreenTrigger/`（C#、XAML、宿主客户端）和 `native/host/`（Rust DLL），引擎继续位于 `backend/`。旧树可作为行为对照。迁移验收以新应用的正常启动入口为准。移除旧工程在功能对齐且回归证据留存后执行。

## 第一项可执行工作

从无硬前置的 **TASK-002** 开始，在真实 Windows 上做一个独立 WinUI 3 双窗口探针。主窗口隐藏后，在选定显示器上用透明、置顶的原生窗口拖选正在变化的计时器。验证背景可见、选框可交互、捕获画面无黑块，在所选显示器内完成一次框选。保存预览、最终帧、物理坐标和前后焦点。分别验证正反向拖动、9×10 与 10×10 边界、Esc、关闭、捕获失败、负坐标及不同缩放。合格条件包括坐标误差 ≤1 物理像素、预览 p95 ≤200 ms、预览和成图均无界面像素、所有终止路径均恢复主窗。真机矩阵仍有缺口，见账本 ISSUE-TASK002-001。[框选规格](product/功能规格.md) · [验收矩阵](agent/验收与测试.md)

WinUI 3 支持 Windows 10 1809（build 17763）及以后版本。产品规格目前写到 Windows 10/11 x64，未细分 build。如果必须兼容早于 1809 的系统，就与全量 WinUI 3 目标存在平台冲突，需单独裁定最低支持版本。系统的覆盖层排除标记 `WDA_EXCLUDEFROMCAPTURE` 从 Windows 10 2004 起有完整语义，且须由拥有该顶层窗口的进程设置。TASK-002 需记录具体 OS build 和捕获后端；对缺少该能力的目标版本验证另一条干净实时捕获路径。无法满足时保留阻塞并提交兼容性决定。静态桌面截图不能满足实时预览验收。[Microsoft WinUI 3](https://learn.microsoft.com/windows/apps/winui/winui3/) · [窗口捕获排除的版本与所有权要求](https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity)

通过 WinUI 的窗口互操作可取得 HWND，并使用 AppWindow 管理本应用窗口。透明合成、命中测试、显示器切换和失焦后的表现仍需用实际捕获后端逐项测量。[Microsoft 窗口管理](https://learn.microsoft.com/windows/apps/develop/ui/manage-app-windows) · [获取窗口 HWND](https://learn.microsoft.com/windows/apps/develop/ui/retrieve-hwnd)

## 任务顺序与切换条件

| 阶段 | 交付 | 何时可以继续 |
|---|---|---|
| P0 | TASK-001 和 TASK-002 各自取证。TASK-003 冻结实测工具链，TASK-004 建立原生入口和 Rust/Python 启停，TASK-005、TASK-006 接通异步命令、事件与共享类型 | 硬前置达到 VERIFIED。真实入口能报告能力、取消、处理引擎断流并退出，无残留子进程 |
| P1 | 按任务卡接入框选、导入、目标、脚本、识别、窗口、热键与受控输入。旧页面作为行为对照 | 从原生主窗口走通每条已实现路径，确认输入边界、取消和焦点。组件单测之外保留入口操作记录 |
| P2 | TASK-024 至 TASK-026 完成运行、监控、调试、设置、历史和恢复交互 | 从同一应用用键盘遍历功能，验证普通/调试日志、异常提示、数据冲突和停止入口 |
| P3 | TASK-027 交付 WinUI、.NET、Windows App SDK、Rust、Python 与离线模型的 x64 安装链。TASK-028 验证原数据恢复，TASK-029 核对发布包 | 干净 Win10/11 上断网安装、启停、升级、卸载。核对应用构建图、包清单和进程树，默认构建与运行均不需要 npm/Vite、Tauri、业务 HTML/JS 或 WebView2 前端。证据绑定同一候选包 SHA-256 |

部署技术建议采用含 Windows App SDK 和 .NET 自包含输出的离线安装包，精确版本、许可证、依赖、体积及安装方式经 TASK-003、TASK-027 实测锁定。升级须识别旧宿主实际使用的数据根；走同一 Repository 校验已有目标、脚本和配置，遇到多份有效数据时提示选择并保留原件。[Microsoft 自包含部署文档](https://learn.microsoft.com/windows/apps/package-and-deploy/self-contained-deploy/deploy-self-contained-apps)
