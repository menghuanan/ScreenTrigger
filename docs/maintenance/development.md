# 开发规范与本机命令

现阶段用现有 WinUI 主窗开发和验证功能。先从[实现审查](implementation-review.md)或已确认的适配需求选一项，按[任务索引](../agent/任务拆解.md)核对所属模块、前置、关联 TEST 和已有问题。新功能的输入、依赖和设计留在[后续开发规划](roadmap.md)。

## 启动开发面板

在仓库根使用 x64 工具链。现有锁定记录为 CPython 3.13.14、Rust/Cargo 1.100.0、MSVC 14.52.37025、.NET SDK 11.0.100、.NET 10.0.15 和 Windows SDK 10.0.26100.0。WinUI 工程在构建阶段运行 Cargo 并复制同配置的宿主 DLL。环境版本、目标框架、NuGet 与 Cargo 锁分别见[原生工具链](../../manifest/native-dev-toolchain.json)、[应用工程](../../desktop/ScreenTrigger/ScreenTrigger.csproj)及[宿主锁文件](../../native/host/Cargo.lock)。

已有工作区可直接用项目解释器。首次配置时，用 CPython 3.13 x64 创建 `.venv`，执行 `.\.venv\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r manifest/probes-cp313-win-x64.txt`。模型权重另从已取得的本地介质按[目录哈希](../../manifest/models.catalog.json)校验。开发依赖与发布介质使用各自的锁文件。

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py --version
& $py tools/run_acceptance.py audit --scope baseline
dotnet build desktop/ScreenTrigger/ScreenTrigger.csproj -p:RestoreLockedMode=true
$env:SIDECAR_DATA_DIR = Join-Path $PWD ('reports\raw\dev-' + [guid]::NewGuid().ToString('N'))
& .\dist\desktop\bin\ScreenTrigger\debug_win-x64\ScreenTrigger.exe
```

在 Windows x64 开发者终端运行，使 `dotnet`、`cargo`、MSVC 链接器及 Windows SDK 可用。开发态宿主默认查找项目的 `.venv\Scripts\python.exe`。需要替换解释器时用 `SIDECAR_PYTHON`，指定本地权重库用 `ST_OCR_MODELS_DIR`。开新终端即可恢复原来的会话环境。面板就绪后先查看引擎版本和能力，走与需求有关的页面，在隔离数据根模拟一次失败或取消并验证可恢复。

## 一个改动的交付边界

1. 写明已有行为、期望行为、受影响的标准和可观察的失败。在已有首版卡内修复时沿用 TASK/TEST 编号。新增功能在确定范围后再分配独立任务、测试和前置，并同步索引、目录生成器和回归。
2. 选择资源所有者。WinUI 负责交互与焦点，Rust 负责输入授权、窗口身份和子进程，Python 服务负责业务与数据。面板经共享客户端调用命令。用请求 ID、会话、执行和修订关联观察结果。涉及跨端字段时同时更新 schema、共享测试向量、三端编解码及相应[契约](architecture.md)。
3. 保留旧数据和调用者的行为。仓库写入用修订和一致提交，重试先查询结果。停止或失焦时先验派发边界，再验证持有键释放。识别、取消、故障使用各自的结果通道。新增直接依赖登记精确版本、许可证、安装体积和包内来源。
4. 从正常应用入口复现。记录原失败、修复、原用例和受影响路径。自动采集命令及人工操作均应关联实际断言、原始文件哈希与审查人。共享接口、模型、数据集或候选包变化后重建基线；以新回执记录每次尝试。[执行约定](../agent/执行约定.md) · [检查与闭环](../agent/检查与闭环.md)

## 按影响选择回归

| 改动 | 最小回归范围 |
|---|---|
| Python 服务、脚本、识别与数据 | 后端套件、相关错误与取消用例。识别算法另跑冻结数据集并逐语言或逐方法判定，恢复逻辑另测跨进程中断。 |
| Rust 输入、绑定、热键、sidecar | Rust 单元和集成测试，随后在专用窗口记录实际事件、失焦、停止后的新增派发与抬键。 |
| 共享类型、IPC、WinUI | C# 类型/配置/协议/故障套件及三端共享向量，从主窗口走正常、错误、取消和关闭路径。 |
| 打包与维护脚本 | 使用新文件名构造候选，核对介质清单、入口、独立数据根、升级与恢复。详见[维护手册](operations.md)。 |

```powershell
& $py -m unittest discover -s backend/tests -t . -v
cargo test --locked --manifest-path native/host/Cargo.toml --target-dir dist/native-host
dotnet build desktop/ScreenTrigger.ContractTests/ScreenTrigger.ContractTests.csproj
$suite = '.\dist\desktop\bin\ScreenTrigger.ContractTests\debug\ScreenTrigger.ContractTests.dll'
$run = 'reports/raw/contracts-' + [guid]::NewGuid().ToString('N')
foreach ($mode in 'types','config','protocol','faults') {
    dotnet $suite $mode "$run/$mode"
    if ($LASTEXITCODE -ne 0) { throw "C# $mode 失败，请保留本次输出。" }
}
& $py tools/check_docs.py
& $py -m unittest discover -s tools/tests -v
```

业务任务还应提交对应检查项的证据，执行例如 `& $py tools/run_acceptance.py audit --task TASK-015`，再执行相应累计阶段审计。单测成功、静态文档检查通过以及账本的 IMPLEMENTED 各有独立含义。放行条件见[验收矩阵](../agent/验收与测试.md)。
