# 技术架构与模块所有权

当前可执行入口是 `ScreenTrigger.exe`。主窗口在同一进程加载 Rust DLL，宿主启动一个受管 CPython 子进程。Lua VM 和视觉 Provider 运行在引擎侧。新增功能应先定位状态或资源由谁拥有，再决定改哪个入口。

| 责任 | 当前实现入口 | 修改时应保持的行为 |
|---|---|---|
| WinUI 窗口、导航、可访问性 | [App 启动](../../desktop/ScreenTrigger/App.xaml.cs)、[主窗导航](../../desktop/ScreenTrigger/MainWindow.xaml)、[业务页装配](../../desktop/ScreenTrigger/MainWindow.Workspace.cs) | 页面共用宿主会话。引擎换代时清除失效页面和图像引用。关闭覆盖层恢复主窗，退出主应用才释放宿主。 |
| 托管客户端、协议类型 | [HostClient](../../desktop/ScreenTrigger/Host/HostClient.cs)、[SessionEvents](../../desktop/ScreenTrigger/Host/SessionEvents.cs)、[契约向量](../../schema/contract-cases.json) | 验证 C ABI 版本，关联请求和会话，处理事件序号间隙、超时和退出。原生回调数据复制后送 UI 调度队列。 |
| 引擎生命周期、双向通信 | [C ABI](../../native/host/src/desktop_abi.rs)、[sidecar 管理](../../native/host/src/sidecar/mod.rs)、[Python 入口](../../backend/main.py) | Python 标准输出承载 NDJSON v1，日志走标准错误。请求结果未知时按请求 ID 查询；重试写操作须保持去重语义。 |
| 框选、捕获与导入 | [选择控制器](../../native/host/src/selection/controller.rs)、[WinUI 覆盖层](../../desktop/ScreenTrigger/Selection/OverlayWindow.xaml.cs)、[图像导入](../../backend/image/import_service.py)、[捕获服务](../../backend/capture/service.py) | 区分物理屏幕、窗口客户区和图内坐标。文件选择器生成受限导入令牌。临时图像和帧绑定所有者、会话及寿命。 |
| 目标、分组、脚本源码与备份 | [目标仓库](../../backend/targets/repository.py)、[脚本服务](../../backend/scripts/service.py)、[备份恢复](../../backend/storage/backup.py) | 维护稳定的目标 ID 与样本号，写入检查修订。先提交资产与 generation，再原子切换 current。SQLite 的实际切库属后续设计。 |
| 识别与能力声明 | [识别服务](../../backend/vision/service.py)、[Provider 注册](../../backend/vision/registry.py)、[模型清单](../../manifest/models.catalog.json) | 模板、轮廓、OCR、颜色共享帧坐标与结果契约。未命中、模型缺失、故障、取消分别返回。识别测试只展示结果。 |
| Lua、监控、调试 | [Lua VM](../../backend/script/lua_vm.py)、[Host API](../../backend/script/hostapi.py)、[调度器](../../backend/monitor/scheduler.py)、[调试服务](../../backend/debug/service.py) | 纯脚本可在 OCR 缺失时运行。监控区分命中、等待超时、故障及取消。恢复调试时重新核对输入条件。 |
| 输入、快捷键与窗口身份 | [输入闸门](../../native/host/src/input/gate.rs)、[输入派发](../../native/host/src/input/dispatch.rs)、[快捷键](../../native/host/src/hotkey/mod.rs)、[窗口绑定](../../native/host/src/window/mod.rs) | Rust 在派发前核验租约、区域、窗口和停止状态。停止后释放本执行持有的键。UI 和识别 Provider 都通过宿主走受控路径。 |

## 用两条操作链定位问题

**测试识别。** 在「截图与导入」取得 ImageRef，在「目标库」准备草稿或保存目标，再到「测试识别」。主窗把图像、原点及草稿交给识别页，页面调用 `test_recognition`，引擎返回匹配框、编号与条件证据。定位框偏移时分别记录输入帧原点、候选框和 UI 绘制坐标。定位误识别时同时保留原帧、阈值、模型及逐条件结果。[识别页](../../desktop/ScreenTrigger/Recognition/RecognitionPage.xaml.cs)

**脚本输入。** 运行页通过宿主提交执行；Lua 调用受限 API，Python 输入代理把与执行关联的动作交给 Rust。归因漏执行或重复执行时，先按会话、execution_id、action_id 对齐请求、闸门和实际窗口事件，再核对 UI 收到的终态。热键停止和引擎异常退出必须覆盖 UI 忙碌时的路径。[输入代理](../../backend/input_proxy/proxy.py) · [宿主派发边界](../../native/host/src/input/wiring.rs)

共享字段及序列化以[数据结构](../02-数据结构.md)为准，通信以[IPC](../03-IPC协议.md)为准，脚本接口以[Lua API](../04-Lua脚本API.md)为准，错误与限额以[边界约束](../06-边界条件与约束.md)为准。当前 UI 兼有调试入口和用户操作入口，拆成两个应用的顺序见[后续规划](roadmap.md)。
