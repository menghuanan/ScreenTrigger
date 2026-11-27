# Lua 脚本 API v1

## 从录制草稿生成 Lua（TASK-034）

运行页结束录制后可选择「编辑为 Lua 脚本」。脚本页沿用语法检查、修订保存和重新打开入口。`wait(seconds)` 表达动作间隔。`click_anchor(anchor,button?,clicks?,hold_ms?)` 保留点击的按住时间。`drag_anchor(points,duration_ms)` 依序经过比例点，`scroll_anchor(anchor,delta_raw)` 在指定位置发送有符号的原始滚轮增量。每个滚轮刻度为 120。`recorded_keys(scope,events)` 接受配对完整的按下和抬起，按各自的 `offset_ms` 执行。中文输入法保留实际键序。回放前在运行页重新选择窗口、选区或单屏。

## 从脚本草稿取点（TASK-032）

脚本页允许手填物理 X、Y，或从指定显示器取点后回填。点击“插入 click_at”将草稿点写入源码的当前选区。例如物理点 (300,400) 生成 `click_at(300, 400)`，仍使用已有运行租约和前台输入边界。

## 按本次运行范围定位（TASK-031）

`click_anchor(anchor,button?,clicks?)` 在本次运行的实际范围点击。例：`click_anchor({version=1,scope='window',strategy='relative',reference_width=101,reference_height=51,x=50,y=25})`。`offset` 保持局部物理偏移，`relative` 按各轴比例定位。窗口重新绑定或移动后使用当前客户区几何。旧 `click_at(x,y)` 始终使用绝对物理坐标。

运行时目标是 Lua 5.4。WinUI 3 编辑器和调试器经已注册命令保存源码、读取诊断及暂停快照，用户脚本通过本页的宿主 API 操作。脚本只引用已保存 script_id/target_id，不传任意本地路径。Lua 秒数在宿主边界转为整数毫秒（向上取整）；物理像素和数据类型见 [02](02-数据结构.md)。

## 环境、返回和错误

全局 `args` 为本次调用参数副本；`SCRIPT_ID` 当前脚本 ID；`EXECUTION_ID` 根执行 ID；`API_VERSION="1"`。`args.match` 只由监控成功动作注入，值为 RecognitionResult；`args.failure` 只由监控超时动作注入 `{reason="timeout",attempts=integer,target_id=string}`。参数名必须在脚本元数据声明。

可用基础库为宿主审核的 Lua base/string/table/math/utf8 子集；用户环境不提供 io、os、package、require、load、loadfile、dofile、debug、Python bridge。调试 hook 保留在宿主私有状态。宿主函数只收发 JsonValue/普通 Lua 数据，不返回 Python 对象。该能力边界不构成恶意脚本安全沙箱，首版运行用户自写、可信脚本。

API 普通错误抛出 table `{code,message,details}`，可用 `pcall` 捕获；引擎在顶层归一化 Lua 原生错误。未命中按函数定义返回 false/空表，不抛异常。取消/停止是控制信号：即使 pcall 截获内部异常，执行闸门仍关闭且 hook 会继续退出，不可借此继续输入。运行结束只返回一个 JSON 可序列化值，无 return 为 null。

运行上下文失效属于控制器处理：若动作尚未派发便发现 WINDOW_UNAVAILABLE，挂起当前API并进入paused；用户恢复后重新检查，目标类API重新识别，不把失焦直接变成普通Lua异常结束脚本。若动作已部分派发则释放持有输入、报告INPUT_FAILED并终止，禁止恢复时重播整段动作。STALE_FRAME且尚未派发时，click_target/move_to_target最多重新采样一次，仍过期才抛错；其他API不自动重试。上述重采样不属于重复已产生副作用的动作。
## 目标检测

表中 `?` 表示可选，签名是说明文本。

| 函数 | 参数及默认 | 返回 | 行为 |
|---|---|---|---|
| `find_target(target_id, region?)` | region=nil 使用当前有效识别区 | found:boolean, matches:Match[] | 单次当前帧，不等待；未命中 false,{} |
| `wait_for_target(target_id, timeout?, interval?)` | timeout=30s，0 无限；interval=nil 用运行频率 | found:boolean, match:Match或nil | 匹配取首个；等待到期 false,nil |
| `wait_for_any_target(target_ids, timeout?)` | 1–100 ID，无重复，timeout=30s | target_id或nil, Match或nil | 每轮同帧；按入参数组顺序取第一个命中目标 |
| `wait_for_all_targets(target_ids, timeout?)` | 同上 | found:boolean, map[target_id]=Match | 必须同一帧全部出现；超时 false,{}，不累计不同时间结果 |
| `wait_until_target_disappear(target_id, timeout?)` | timeout=30s | boolean | 当前已无目标立即 true；超时 false |

Match 含 target_id、sample_no、space、bbox、center、confidence、geometry_revision、evidence；**没有含糊的顶层 x/y**。`matches[1].center.x` 才是建议点击点。region 参数只能缩小当前识别区，不能越出或切换窗口；传 image_pixel 给屏幕检测拒绝。等待函数不吞掉引擎异常，timeout 只是“等待目标”的期限。

## 鼠标与键盘

输入 API 无返回值，异常时抛出错误；target 便利方法额外返回是否匹配。所有坐标为 screen_physical，允许负坐标但必须位于允许显示器/客户区。输入成功仅表示派发完成，不保证应用完成业务。

| 函数 | 参数/默认 | 语义 |
|---|---|---|
| `move_to(x,y,duration?)` | integer 坐标；duration=.2，0–5s | 受检查路径移动 |
| `move_to_target(id,offset_x?,offset_y?,duration?)` | 偏移=0；duration=.2 | 重新识别，移动至首个 Match.center+偏移；未命中 false，成功 true |
| `click(button?,clicks?)` | button=left，left/right/middle；clicks=1，1–2 | 当前指针位置点击，也做边界校验 |
| `click_at(x,y,button?,clicks?)` | 同上 | 先校验整个动作，移动再点击 |
| `click_target(id,offset_x?,offset_y?,button?)` | 偏移=0；button=left | 新帧重新识别；未命中 false，无输入；成功 true |
| `drag(from_x,from_y,to_x,to_y,duration?)` | duration=1s，0–5s | 左键拖拽；起终点及路径均合法才执行，终止时释放 |
| `scroll(delta)` | integer，-100..100，0 无动作 | 正数向上，单位滚轮格 |
| `press_key(key)` | 标准键名 | 按下并释放 |
| `key_down(key)` / `key_up(key)` | 标准键名 | 持有记账；重复 down 幂等；只释放本执行持有的键 |
| `hotkey(...keys)` | 1–5 个不同标准键名 | 依序按下、逆序释放，异常亦清理 |
| `type_text(text,interval?)` | ≤10000 Unicode 字符；interval=.05s，0–5s | Unicode 输入；不操作剪贴板 |

表中的移动时长和文字输入间隔为出厂默认值。省略这两个参数时，执行使用启动时设置中的 `input.move_duration_ms` 和 `input.type_interval_ms`。显式参数以秒为单位。

键名不区分大小写：a–z、0–9、F1–F12、up/down/left/right、enter/space/tab/backspace/delete/esc、ctrl/shift/alt/win、home/end/pageup/pagedown/insert/printscreen。不支持的键名先拒绝。系统保留快捷键不保证模拟成功。

`click_target` 不复用 wait_for_target 的旧坐标；中间窗口移动会作废旧帧。直接 `click_at` 使用调用者提供的数值，只做当前几何与边界校验，不负责证明内容仍是原目标；面向目标操作建议用 click_target。当 offset 后点不合法时拒绝，不裁剪。

当前脚本通过已有 `find_target` 使用已装配的目标识别能力。目标检测扩展 v1 在 Provider 边界验证类别、源帧和取消状态。本版 Lua 没有可调用的 YOLO 推理函数。[识别能力](03-IPC协议.md#目标检测扩展契约-v1)

## 图像与 OCR

用户在运行设置选定 TXT 后，`ocr` 对本次成功识别的 TextBlock 排版并写入；空数组保留文件。区域监控直接使用当次采样保留的同一帧识别文字。识别事件有相同内容时按各自的采样次数分别写入。路径、授权令牌和任意文件写入函数不暴露给 Lua。

| 函数 | 参数/默认 | 返回/语义 |
|---|---|---|
| `capture_screen(region?)` | nil 当前识别区 | ImageRef table；不可写文件 |
| `release_image(image)` | 当前执行拥有的 ImageRef | 无返回，幂等释放 |
| `ocr(image?,region?,model_id?,export?)` | image=nil 则捕获，model_id=nil 使用配置。export 缺省沿用本次运行的 TXT 授权，false 仅读取，true 要求已授权 TXT | TextBlock[]；无文字空数组；缺模型抛错 |
| `find_text(text,match_mode?,region?)` | 非空字符串，match_mode=exact，可 contains/fuzzy | found:boolean, TextBlock[]；使用配置模型及 02 默认匹配阈值 |
| `find_color(params,region?)` | params 为 02 ColorParams | found:boolean,Candidate[] |
| `get_pixel_color(x,y)` | 识别区内屏幕物理 Point | `{r,g,b}`；单像素读取不要求 10×10 |

ocr 的 image 非 nil 时 region 是该图的 image_pixel 子区；nil 时 region 是当前屏幕/窗口子区，不得同时跨空间。OCR 返回的是原始文本而不是用户输入的查询串；用户据 confidence 判断，不用错误的默认文字替代失败。模板/轮廓通过配置目标调用 find_target，首版不提供第二套 Lua 原始模板参数接口。

## 控制与工具

| 函数/值 | 参数/默认 | 返回/约束 |
|---|---|---|
| `wait(seconds)` | number≥0 | 可取消等待，不堵塞 IPC；wait(0) 让出执行 |
| `wait_random(min,max)` | 0≤min≤max | 同 wait |
| `log(message,level?)` | string≤4096字符；level=info，debug/info/warning/error | 写结构化日志；普通模式不弹窗 |
| `print(...)` | 标量/可 tostring 的值 | 重定向脚本日志，禁止直接写协议 stdout |
| `stop_script(reason?)` | reason="script_requested" | 请求终止整个执行树，不正常返回；终态 cancelled |
| `should_stop()` | 无 | boolean；只能用来友好结束，不是唯一停止机制 |
| `set_recognition_frequency(hz)` | 0.1–30 | 本根执行频率，下次 tick 生效；不改全局 config |
| `get_mouse_position()` | 无 | x,y 物理坐标，经宿主只读 query_pointer 获取 |
| `get_screen_size(display_id?)` | 默认运行显示器，绑定时客户区所在显示器 | width,height；左上原点需用区域配置，不能假设为 0 |
| `get_time()` | 无 | 单调秒数，用于相对计时，不是日历时间 |
| `random(min?,max?)` | 无参数→[0,1)，有参数需两整数 min≤max | 对应范围随机数；不用作安全 ID |
| `beep(frequency?,duration?)` | 800Hz/.2s；37–32767Hz，0–5s | 可取消提示音 |
| `json_array(table)` | 连续 1..n 的 table | JSON 数组标记，支持空数组 |
| `json_null` | 宿主唯一哨兵 | 仅用于 JSON null 序列化，不能作为 Python 对象反射 |

原稿中的 clipboard/paste_text、任意表达式执行属于扩展候选，首版不公开；原稿路径式 call_script 不延续。核心需求不依赖这些扩展。

## 脚本互调

`call_script(script_id:string,args?:table={}) -> JsonValue`

1. 使用运行启动时的脚本目录快照查 ID；不接受 `.lua` 路径，SCRIPT_NOT_FOUND 明确报错。
2. 按子脚本参数定义验证并进行 JSON 值复制；顶层 args 必须对象。
3. 创建子调用环境；继承区域、绑定、输入租约、根期限、取消和调试状态，不共享局部/global table。
4. 同步等待一个返回值；返回再复制到父 Lua 环境。错误附 child script_id/line/call_stack，默认传播。
5. 根深度为 1，最多 5；达到上限先报 CALL_DEPTH_EXCEEDED，不创建第 6 个 VM。父/子手动停止均停止根。

不可把两个返回值 `return true, data` 当协议；用 `{ok=true,data=...}`。JSON 空数组用 json_array，空对象 `{}`；含 null 用 json_null。结果转换失败是 SERIALIZATION_ERROR，不能静默扔掉字段。

## 示例

以下 ID 为占位，创建对应目标/脚本后替换；示例证明调用形状，尚未经过真实 Lua/桌面运行验证。更多操作教程见[脚本入门](user/脚本入门.md)。

```lua
-- 等待目标，输出样本编号；点击前重新定位
local found, match = wait_for_target("target-id", 10)
if not found then
  log("等待超时，未执行点击")
  return {ok = false, reason = "timeout"}
end
log("命中样本 " .. match.sample_no)
local clicked = click_target("target-id")
return {ok = clicked, sample_no = match.sample_no}
```

```lua
-- 子脚本：元数据声明 a、b 均为 required number
return {sum = args.a + args.b, values = json_array({args.a, args.b})}
```

```lua
-- 父脚本：业务错误可捕获，默认不重试含键鼠副作用的调用
local ok, result = pcall(function()
  return call_script("calculator-script-id", {a = 12, b = 30})
end)
if not ok then
  log("子脚本失败: " .. tostring(result.code or result), "error")
  return {ok = false}
end
return {ok = true, sum = result.sum}
```

```lua
-- 监控成功动作：脚本元数据声明 required match:object
local hit = args.match.matches[1]
log("监控命中 " .. hit.sample_no)
-- 使用稳定目标 ID 重新定位，不按已过期的帧坐标点击
return {clicked = click_target(hit.target_id)}
```

调试器在宿主中复制变量快照并按深度、总项数和字符串长度截断；相同 table 的后续引用返回 reference_id。调试操作和历史管理通过桌面宿主提供。

备份与恢复由设置页或离线管理命令调用。Lua 的能力表保持封闭，活动执行期间宿主拒绝切换目录快照。

后续自动化版本中，ocr 的 model_id 继续指识别端。相应管线首次使用时加载；已移除的模型引用保留原值并返回 MODEL_UNAVAILABLE。脚本未获得模型介质目录访问或安装权限。
## ORB 目标

将目标的锚点叶设为 `feature_match` 后使用既有 `find_target`。命中时返回当前帧的最佳单个纹理实例；无合格实例时沿用未命中语义。`click_target` 继续执行几何修订、区域、租约与停止检查。
## 单动作策略

在脚本编辑器保存动作策略作为该脚本默认。调用 `action_with_policy(options, api_name, ...)` 覆盖一次已注册的输入动作，包括 `click_anchor` 和 `click_target`。例如 `action_with_policy({version=1,curve=false,offset={x={0,0},y={0,1}}},"click_at",300,400)`。未提供的项继承脚本默认。曲线用于移动、点击、拖动，偏移仅用于点击，延迟作用于全部输入动作；脚本级选项只作用于适用的动作，显式的非法动作覆盖在派发前报错。

## 后台读取绑定客户区

在运行设置中绑定窗口并选择后台读取后，`capture_screen`、`get_pixel_color`、OCR 和目标监控从所选窗口的客户区取像。客户区 ROI 继续使用绑定坐标，`get_pixel_color(x,y)` 继续接收屏幕物理坐标。运行使用启动时选定的模式。最小化会暂停执行，还原后由用户恢复。
显式选择绑定窗口后台模式时，点击、左拖、滚轮、常规按键和组合键沿用现有 Lua 调用。鼠标位置及无参数位置查询来自该租约的虚拟指针。后台动作仅对目标声明并支持的消息类型派发，保留焦点和真实指针。
用户端从「开始工作」指定范围和脚本并启动执行。「编辑脚本」的装配台生成普通 Lua，手写代码和录制草稿均可在 Lua 模式维护。
