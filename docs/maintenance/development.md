# 开发规范与本机命令

用户端和开发面板共用宿主客户端。用户端工程为 [ScreenTrigger.User](../../desktop/ScreenTrigger.User/ScreenTrigger.User.csproj)，本机可用 `dotnet run --project desktop/ScreenTrigger.User` 启动。开发面板保留原有九个入口与自动化标识。先从[实现审查](implementation-review.md)或已确认的适配需求选一项，按[任务索引](../agent/任务拆解.md)核对所属模块、前置和测试组。

## 启动开发面板

在仓库根使用 x64 工具链。现有锁定记录为 CPython 3.13.14、Rust/Cargo 1.100.0、MSVC 14.52.37025、.NET SDK 11.0.100、.NET 10.0.15 和 Windows SDK 10.0.26100.0。WinUI 工程在构建阶段运行 Cargo 并复制同配置的宿主 DLL。环境版本、目标框架、NuGet 与 Cargo 锁分别见[原生工具链](../../manifest/native-dev-toolchain.json)、[应用工程](../../desktop/ScreenTrigger/ScreenTrigger.csproj)及[宿主锁文件](../../native/host/Cargo.lock)。

已有工作区可直接用项目解释器。首次配置时，用 CPython 3.13 x64 创建 `.venv`，执行 `.\.venv\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r manifest/probes-cp313-win-x64.txt`。模型权重另从已取得的本地介质按[目录哈希](../../manifest/models.catalog.json)校验。开发依赖与发布介质使用各自的锁文件。

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py --version
& $py tools/run_acceptance.py audit --scope baseline
& .\tools\start-dev-panel.ps1
```

在 Windows x64 开发者终端运行，使 `dotnet`、`cargo`、MSVC 链接器及 Windows SDK 可用。开发态宿主默认查找项目的 `.venv\Scripts\python.exe`。需要替换解释器时用 `SIDECAR_PYTHON`，指定本地权重库用 `ST_OCR_MODELS_DIR`。开新终端即可恢复原来的会话环境。面板就绪后先查看引擎版本和能力，走与需求有关的页面，在隔离数据根模拟一次失败或取消并验证可恢复。

## 自动化扩展验收

在仓库根运行以下命令。省略版本或指定 `--version v1` 时使用首版目录、首版账本和首版报告。自动化扩展使用 `automation-v2`，卡号、硬依赖和各测试组的验收场景见[开发入口](../agent/自动化扩展-开发入口.md)。下列各条分别执行，退出码 0 表示选中门槛通过，1 表示尚未就绪，2 表示参数、文件或工具出错。

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py tools/run_acceptance.py catalog --version automation-v2
& $py tools/run_acceptance.py audit --version automation-v2 --scope baseline
& $py tools/run_acceptance.py audit --version automation-v2 --task TASK-030
& $py tools/run_acceptance.py audit --version automation-v2 --test TEST-024
& $py tools/run_acceptance.py audit --version automation-v2 --scope release
& $py tools/check_docs.py
& $py tools/check_docs.py --version automation-v2
& $py -X utf8 -m unittest discover -s tools/tests -v
```

首次创建空账本用 `& $py tools/run_acceptance.py init --version automation-v2`。已有授权工作区继续使用 `reports/automation-v2/check-state.json`。开卡登记负责人、硬依赖审计、修改范围、测试组与可观察预期。实现就绪后登记 `IMPLEMENTED`；只有单卡审计的 `tasks` 项可以给出 `VERIFIED`。测试组入口核对该组自己的回执，release 汇总全部卡、测试组与独立的发布签核。

每次尝试把原始输出写入 `reports/automation-v2/raw/` 的新文件。运行 `template --version automation-v2 --check TASK-030.CATALOG` 或 `capture --version automation-v2 --check TASK-030.CATALOG -- <命令及参数>`（入口均为 `tools/run_acceptance.py`），按[证据和问题闭环约定](../agent/检查与闭环.md)补齐预期、实际、环境、哈希与审查，再用 `submit --version automation-v2 <草稿路径>` 提交。回执进入同版本的 `evidence/`；每份审计在 `audits/<运行号>/` 保存完整目录、结果和摘要。源码变化后用当前基线重跑受影响项及失效的前置卡，保留历次尝试。

将新版候选归档为 `dist/automation-v2/ScreenTrigger-automation-v2-<候选号>.zip`。逐包计算 SHA-256，将根目录相对路径与哈希填入新版账本的 `release_artifact`，包级回执使用同一引用。变更候选后复验。用 RC16 原审计的源码清单、原回执及原包在隔离目录复演首版 release，保存此次复演的原始输出作为 TASK-030 的兼容性对照。

保有原签核材料的工作区可运行 `& $py tools/replay_rc16.py`，按源码哈希恢复 RC16，在副本内调用原审计器。终端打印本次复演目录，其中 `replay.json` 给出签核差异，`fixtures.json` 列出旧数据文件、Lua、配置及专窗探针的运行命令。

## 便携包启动性能复验

在参考机的普通用户桌面，从仓库根运行。以下命令选择新版账本绑定的 ZIP，每次建立独立的介质、快照和原卷目录。将显示器参数设为当前主显示器的设备名，示例采用参考机的 `\\.\DISPLAY1`。

```powershell
$py = '.\.venv\Scripts\python.exe'
$trial = [guid]::NewGuid().ToString('N')
$zip = (Get-Content -Raw -Encoding utf8 reports/automation-v2/check-state.json | ConvertFrom-Json).release_artifact.path
$app = "dist/automation-work/startup040-$trial"
$snap = "dist/automation-work/snapshots040-$trial"
$raw = "reports/automation-v2/raw/TASK-040/$trial"
& $py tests/acceptance/portable_media.py extract $zip $app
& "$app/resources/python/python.exe" -I -B -X utf8 tests/acceptance/startup_snapshots.py $snap '\\.\DISPLAY1'
& $py tests/acceptance/startup_measure.py $zip $app $snap "$raw/cold" --mode cold
& $py tests/acceptance/startup_measure.py $zip $app $snap "$raw/warm" --mode warm
& $py tools/run_acceptance.py audit --version automation-v2 --task TASK-040
& $py tools/run_acceptance.py audit --version automation-v2 --test TEST-022
```

桌面探针在资源管理器中双击实际 EXE，再在正常运行面板执行预置脚本。冷测轮转纯 Lua、通用 OCR 和韩文 OCR，每组启动 20 个新进程。热测在同一进程重复执行，OCR 使用不同像素的冻结输入。保留每次操作的原始计时、进程树、模型与夹具身份、文字和退出状态。按每组第 19 个样本取 p95。复核原卷并提交回执后运行单卡和测试组审计。[启动指标与采样规则](../agent/自动化扩展-模型启动与离线发行.md)

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
### 绑定窗口的后台动作

在运行设置中绑定目标，使用“查询后台动作支持”查看五个动作，再勾选“在后台读取和操作绑定窗口”。支持声明只限当前目标。切换绑定会清除后台选择。前台仍可接受键鼠，后台动作的虚拟落点从所选区域中心开始。

| 窗口与状态 | 点击、左拖、滚轮、普通键、组合键 | 实际接收与限制 |
|---|---|---|
| 自有普通 Win32 根窗口和同进程 EDIT，另一窗口在前台 | 五类均已实测 | 接收方的鼠标消息点为客户区坐标，滚轮点为屏幕坐标，按键投到焦点后代或虚拟落点控件。 |
| 自有根窗口和 EDIT，客户区完全被遮挡 | 五类均已实测 | 前台仍可独立输入，真实指针位置不变。 |
| 自有窗口最小化 | 暂停 | 释放本执行在原控件持有的键。还原后需显式恢复。 |
| 未声明能力、拒绝访问、无响应或已关闭的窗口 | 未验证或不可用，原因随查询返回 | 超时保存结果未知，同一动作不自动重发；同标题的新窗口须重新绑定。 |
