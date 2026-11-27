# 维护手册

## 从问题现场开始

先从主窗查看引擎状态、诊断和 OCR 能力，在设置中记下实际数据目录。记录 Windows build、显示缩放、应用版本或候选 SHA-256、出现问题的步骤、预期和实际结果。用临时数据根复现，保留原始数据。涉及脚本或监控时，记下 execution_id、终态和历史。只在专用前台测试窗口验证实际输入。

| 现象 | 检查顺序与处理 |
|---|---|
| 应用可开，引擎启动或握手失败 | 核对应用旁的宿主 DLL、C ABI 和体系结构，再检查解释器路径、包内运行库、冷启动耗时和 stderr。分别记录首次启动及重启，退出后核对进程树。 |
| OCR 不可用或误识别 | 在引擎页检查模型 ID、原因、实际权重根及文件哈希，先用固定图对照原文和结果。区分缺权重、坏权重、推理故障和有效空结果。纯脚本另走一次。 |
| 点击被拒、停止或监控行为异常 | 对齐执行、租约、动作、绑定窗口、帧几何版本和真实事件。失焦、窗口关闭、选区外的拒绝均需保留记录。停止后核对新增派发数及持有键释放。 |
| 保存冲突或重启后缺数据 | 先记设置页数据根、revision、current 指纹和 generation。复制整个数据根，在副本上核对 manifest、资产与编号；写入超时时先查结果。 |
| 界面与历史不一致 | 对齐会话、事件 sequence、请求 ID 和引擎重启时刻。分别检查普通运行和调试暂停的日志展示。协议超时可查询既有请求。 |

### 备份与恢复

在设置页创建备份，预览要恢复的备份，再确认。预览与确认之间发生写入时重新预览。恢复会保护当前目录记录和资产，并维持样本号高水位。设置、执行历史和日志须另随完整数据根备份。主窗无法启动时先退出应用，在安装的版本目录内运行以下只读步骤，替换为设置页记录的数据根及实际备份 ID。

```powershell
$root = 'D:\ScreenTrigger-test-data'
$engine = '.\resources\python\python.exe'
& $engine -I -B -m backend.storage.backup --data-dir $root list
$backupId = Read-Host '输入列表中的备份 ID'
& $engine -I -B -m backend.storage.backup --data-dir $root preview $backupId
```

经核对后，用同一 CLI 的 `restore`，传入相同备份 ID。将预览结果中的 `expected_current` 和 `expected_backup` 分别传给 `--expected-current` 和 `--expected-backup`。恢复后重新打开目标、脚本和图像，检查编号以及一次新写入。[恢复实现](../../backend/storage/backup.py) · [安装与升级说明](../user/离线安装.md)

## 候选包与发布

1. 按[用户端 Offline 发布配置](../../desktop/ScreenTrigger.User/Properties/PublishProfiles/Offline.pubxml)发布 x64 应用，用[介质脚本](../../tools/packaging/build_offline.py)指定发布输出、精确版本 CRT 及本地依赖。每批使用新 ZIP 文件名。脚本逐项校验发布清单的哈希，再补齐引擎、模型和运行时。
2. 在本机用同一介质、隔离的安装根/数据根/快捷方式目录走安装、启动、升级和卸载。对已有数据逐项比较目标编号、脚本、资产和设置，用一次性副本验证明确删除路径。核对安装后的入口、模块与进程树、包内 Python、模型、许可、外连观察及缺模型降级。平台结论写明实际系统 build 和显示配置。[安装程序](../../tools/packaging/Install.ps1) · [正式测试矩阵](../agent/验收与测试.md)
3. 将确认过的真实产物登记在本地账本 `release_artifact`。用包内 Python 运行 [installed_regression](../../tests/acceptance/installed_regression.py) 等安装态用例，另收集真实 UI、安全输入、模型质量、备份恢复、性能与两小时长稳证据。记录机器、数据集、模型、源码与候选哈希、省略场景、复核人。相同样本换包后需要重新验证。
4. 依序检查任务硬前置和累计阶段，最终执行 release。发布负责人核对审计报告，产品负责人审查体验、范围及候选并签核。将候选哈希、技术验收报告、各阶段结果与签核结论保存在本地 `reports/TASK-029/`。接手时的对照数据见 [RC11 实现审查](implementation-review.md)。

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py tools/check_docs.py
& $py -m unittest discover -s tools/tests -v
& $py tools/run_acceptance.py catalog
& $py tools/run_acceptance.py audit --scope baseline
foreach ($phase in 'P0','P1','P2','P3','release') {
    & $py tools/run_acceptance.py audit --scope $phase
    Write-Output "$phase exit=$LASTEXITCODE"
}
```

审计每次产生独立目录 `reports/audits/<run_id>/`。退出码 0 表示指定门槛通过，1 表示未就绪或检查失败，2 表示工具或输入有误。baseline 只审计输入与证据有效性；P3 与 release 还校验全组证据及候选绑定。

## 处理旧回执和失败

以 `result.json` 的 errors、issues 和 retired_receipts 为起点，按 run_id 和 SHA-256 对照原路径、备份及历史运行目录。2026-11-26 用户授权将清单中缺少 19 个源码版本的 43 份回执登记作废。原件已追回的按哈希验证；作废回执仍保留原件、原用例、关联回归和各次尝试。审计检查原用例及关联回归在当前候选上的新回执。其他缺件仍需逐份追回或另行裁定。

问题修复须重跑原用例及受影响用例，提交当前基线的原始输出和审查意见。规则、账本字段、文档快照以及变更后的失效处理见[检查与闭环](../agent/检查与闭环.md)。
## 自动化扩展便携包

使用仓库 CPython 3.13.14 x64 和现有离线工具链运行以下命令。输出名须为新候选。构建器先按工作区基线逐文件复制并校验源码，再从该副本发布 WinUI、Rust 宿主和 Python 引擎。默认选取目录内的两套 OCR 管线，ORB 使用包内 OpenCV。需要检查某个出厂选择时可重复传入 `--pipeline` 和可信的识别模型 ID。

```powershell
$crt = 'C:\Program Files\Microsoft Visual Studio\18\Community\VC\Redist\MSVC\14.52.37025\x64\Microsoft.VC145.CRT'
$candidate = 'dist/automation-v2/ScreenTrigger-automation-v2-' + (Get-Date -Format yyyyMMdd-HHmmss) + '.zip'
& .\.venv\Scripts\python.exe tools/packaging/build_portable.py --crt $crt --out $candidate
```

完整解压后运行根目录的 `ScreenTrigger.exe`。首次启动检查核心清单，将数据保存在同目录的 `data`。退出后整体移动或复制。旧安装数据和便携版升级的选择、核验及恢复步骤见随包的[便携版使用说明](../../tools/packaging/便携版使用说明.txt)。

标准便携包顶层只交付启动程序、`resources` 和使用说明。入口内嵌 WinUI 激活清单，在同一进程加载 `resources/ui` 中的 .NET、WinUI 和 Rust 宿主。出厂模型放在 `resources/weights`，Python 引擎在 `resources/python`。便携标记与核心哈希清单位于 `resources/manifest`。检验旧包和迁移数据时仍支持原有的平铺布局。

便携端使用简体中文。打包器只取 `zh-CN` 的 MUI，用锁定的 Windows SDK 重建界面资源索引，逐项比对中性资源、中文字符串及嵌入的 XAML 页面。构建期剔除未接入的 UI 组件、调试工具、依赖包测试、开发头文件和备用 Lua 实现，保留 Lua 5.4、四种 OCR 识别语言和第三方许可。VC 运行库的完整许可位于 `resources/licenses/VC`。[微软 VC v14 许可来源](https://visualstudio.microsoft.com/license-terms/vs2026-ga-visualcpp-v14-redist-runtime/) · [Windows SDK 资源索引器](https://learn.microsoft.com/en-us/windows/uwp/app-resources/makepri-exe-format-specific-indexers)

普通 Win32 编辑框的录制从运行页的“选择应用并在隔离桌面录制”进入。停止窗显示目标线程的输入布局。验英文按键时应同时核对英文布局、IME 状态和控件收到的按下及抬起。普通控件的键状态来自新启动应用的获焦线程；一次按下及抬起若全部落在两次采样之间，草稿可能漏掉这一键。无法判断多个边沿先后时，草稿会显示该动作中止。接入录制协议的控件可以逐条报告物理键消息。

隔离录制启动前，宿主会与同目录的 `recording-desktop-return.exe` 完成握手。宿主异常退出时，这个进程只在本次录制桌面仍可见时恢复原桌面。复核此路径可使用 `tests/acceptance/recording_desktop_exit.py --fault host`，同时核对原桌面自动恢复、目标终止和观察器未实施救援。
