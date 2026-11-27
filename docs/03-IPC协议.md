# IPC 协议 v1

## 录制草稿交接（TASK-034）

引擎命令 `compile_recording {scope,actions,interrupted,suggest_name?:bool}` 生成 `{scope,source,action_count,omitted}`。`omitted` 每项给出 `after` 和 `reason`。当 `suggest_name=true` 时额外返回 `suggested_name`，以录制脚本01 至 99 编号，全满时返回 null。新建时可传 `number_recording=true`，保存时按当前目录重新分配空闲编号。时间或序号损坏时返回 INVALID_PARAMS，超过脚本字节限制返回 RESOURCE_LIMIT。界面在有中止或无法表达的动作时显示位置、原因并请求确认。宿主命令 `release_recording_draft {recording_id}` 释放同会话已结束的草稿，返回 `{released:true}`。释放后，结束响应及嵌套查询的缓存副本一并失效。

租约输入沿用 `input_action`。锚点点击支持可选 `hold_ms`，`drag_anchor` 携带 `points,duration_ms`，`scroll_anchor` 携带 `anchor,delta_raw`，`recorded_keys` 携带 `scope,events`。数组顺序与经过的路径在宿主派发前整体校验。原有滚轮动作仍以格为单位。

## 单次屏幕取点（TASK-032）

WinUI 草稿发起 `start_point_pick {display_id?,binding_id?}`，返回 `{pick_id}`。指定显示器可选，默认使用主窗口所在显示器。框选与取点互斥。覆盖层接收左键按下和抬起，再用按下时的单像素屏幕点调用 `confirm_point_pick {pick_id,point}`。取消使用 `cancel_point_pick {pick_id,reason?}`。

主窗口恢复后，`point_pick_completed` 事件和确认响应携带 `pick_id,space,point,display_id`。指定窗口绑定时还包含 `client_point,binding_id,geometry_revision`。取消事件为 `point_pick_cancelled {pick_id,reason}`。会话内一个选择只发布一个终态，重复或越类请求返回 INVALID_STATE。

## 租约内的锚点输入（TASK-031）

`input_action` 新增 `kind=click_anchor`，字段为 `anchor:AnchoredPoint` 及沿用 click 的可选 `button`、`clicks`。宿主先核对活动租约与动作去重，再按本次绑定及范围解析。派发前复查范围、显示器覆盖、窗口几何版本与前台身份。沿用既有响应和错误。

Rust 与 Python 子进程间采用自定义 UTF-8 NDJSON v1 和双向 stdio。WinUI 3 的 C# 客户端经进程内 C ABI 调用 Rust，两个边界分别处理传输和生命周期。UI 不直接启动 Python 或读写用户数据。所有字段类型引用 [02](02-数据结构.md)，错误码引用 [06](06-边界条件与约束.md)。

## WinUI 3 与 Rust 宿主

WinUI 客户端通过 P/Invoke 装入版本化的 x64 宿主 DLL。启动校验 ABI 版本，注册事件和窗口服务，再开放引擎启停、状态、诊断、已登记命令及取消。C# 异步等待带 ID 的响应；Rust 在独立线程处理协议和全局停止。Rust 把应用窗口的显示、隐藏、关闭和焦点请求投递至 UI 线程并异步接收结果。UI 线程不得同步等待需要它自己完成的回调。

所有跨 ABI 的字节均带长度和明确所有者。输入在调用期间借用，回调数据在返回前复制；初始化和关停配对，先停止新回调、等待在途回调结束，再释放句柄和委托。主窗口与原生覆盖层共享请求 ID 分配器和事件订阅。按 session_id/seq 分发事件，缺口时重新查询状态，取消与停止走保留容量，不重放输入。ABI 规范和双端探针归 TASK-004、TASK-005。

系统文件选择器归属当前 WinUI 主窗，只有受信任的选择器适配器可把用户选择交给 Rust 签发会话令牌。业务页面与脚本只获得令牌。宿主本地还提供选区拖动上报和按图像引用拉取预览。图像像素走受大小限制的本地缓冲区。窗口、文件选择器与预览是宿主内部适配，沿用下方公开业务命令目录及 Rust/Python 信封。UI 无直接签发令牌、启动 Python 或发送 OS 输入的入口。

已实现的 x64 C ABI v1 使用 cdecl。`st_host_abi_version()` 返回 1。`st_host_open(1, event_callback)` 返回进程内唯一宿主的句柄。版本不匹配、回调为空或已有宿主时返回空指针。`st_host_call(handle, utf8, length, response_callback)` 在调用线程上返回一次结果。两类回调均接收字节指针和字节数，C# 在回调内复制 JSON，事件随后进入 DispatcherQueue。合法响应包含 `success` 和 `data` 或 `error`。调用参数无效时函数返回负值。入参上限 2 MiB。

ABI 消息的 `op` 可为 `start`、`stop`、`status`、`diagnostics`、`request` 或 `cancel`。`request` 传入 `id/command/params`，`cancel` 传入 `request_id`。桌面版本另支持内部窗口服务 attach，以及 pick_image、selection_region 和 select_execution 操作，均校验当前会话。pick_image 仅向界面交付一次性令牌。select_execution 记录多根并行时暂停/恢复热键所作用的根，停止热键立即关闭全部活动根的输入。引擎握手仍通过下方 NDJSON 协议。关闭前停止分配请求，调用 `st_host_begin_close` 退订并排空在途事件回调、关闭引擎。已有同步调用全部返回后调用一次 `st_host_close` 释放句柄与托管委托。实现与调用方分别见 `native/host/src/desktop_abi.rs`、`desktop/ScreenTrigger/Host/HostClient.cs`。

## 消息封装

UTF-8 一行一个 JSON、LF 分帧，内容换行用 JSON 转义。stdout 只放协议，库日志及诊断重定向 stderr；不使用 `EVENT:` 文本前缀。单帧最大 2MiB，超限中止该消息并报告 PROTOCOL_ERROR；连续 10 帧畸形（中间出现任何合法帧即清零计数）则关闭会话，双端对称执行（Python 侧达阈值以退出码 3 结束）。大图使用 ImageRef，不把完整 base64 图塞进请求/事件。

```json
{"protocol_version":1,"session_id":"session-1","type":"request","id":"ui-1","command":"execute_script","params":{"script_id":"s1","args":{},"mode":"once","display_id":"display-1"}}
```

```json
{"protocol_version":1,"session_id":"session-1","type":"response","id":"ui-1","success":true,"data":{"execution_id":"e1","status":"starting"}}
```

```json
{"protocol_version":1,"session_id":"session-1","type":"response","id":"ui-2","success":false,"error":{"code":"INVALID_REGION","message":"选区不能小于10×10物理像素","details":{"field":"region.width"},"retryable":false}}
```

```json
{"protocol_version":1,"session_id":"session-1","type":"event","seq":1,"name":"script_state_changed","timestamp":"2026-09-21T09:00:00Z","payload":{"execution_id":"e1","status":"running","reason":null}}
```

request/response 的 id 必填且同会话唯一；成功含 data、失败含 error，两者互斥。畸形帧必填集按类型执行：request 另必填 command；response 与 host_response 另必填 success 且 data/error 必有一、互斥；event 必填 seq/name/timestamp；host_request 必填 id/command/params。session_id 缺失或不匹配的帧丢弃并记 stderr 日志，不计畸形；类型错向但 JSON 合法的帧（如入站收到 response/event/host_response）仅记日志忽略，同样不计畸形。sequence 只用于 event；事件不充当请求响应。错误码统一字符串，不保留 v1 原稿的数字/字符串混用。message 面向用户，details 不含堆栈敏感对象；调用栈用结构化脚本 ID/行号提供。

连接建立时 Rust 分配 session_id，通过引擎启动参数传入（非用户控制字符串拼 shell）；第一请求 hello 返回 protocol_version、engine_version、lua_version、schema_version、capabilities、model_manifests。capabilities 元素固定为 {id:string, state:"available"|"pending"|"unavailable", reason:string或null}；lua_version 探测失败返回 null，且 capabilities 中 lua_script 标 unavailable。协议 major 不一致拒绝业务请求。capabilities 明确标 unavailable 的功能不能显示可用按钮。

## 全双工与宿主调用

桌面 ABI `pick_text_output {session_id,mode}` 打开受信选择器，取消返回空令牌；选定后返回 `{output_token,name}`。启动命令可携带该令牌。新增内部 host_request `text_output` 的操作为 bind（execution_id、output_token）、commit（execution_id、action_id、text）、close（execution_id）。路径仅由系统选择器交给宿主，内部调用不接收路径。commit 返回写入字节数；同一逻辑动作重试保持同一 ID。成功提交后发送一次 `text_exported {execution_id,action_id,bytes_written,block_confidences}`。分数数组按本次 OCR 返回的 TextBlock 源序列出所有原文非空块的 confidence，包含低置信度和重复块的各自分数。空快照和写入失败均无成功事件。

Lua 请求输入时 Python 发 `type=host_request,id=py-<uuid>,command=input_action,params={execution_id,lease_id,action_id,geometry_revision,action}`，Rust 回同 ID 的 `host_response`（相同 success/data/error 规则）。其他允许的 host 命令：window_snapshot、acquire_input_lease、release_input_lease，以及只读 query_pointer。query_pointer 的 params 为 {}，返回物理屏幕整数 {x,y}，允许负数；未装配返回 CAPABILITY_DENIED，系统读取失败返回 WINDOW_UNAVAILABLE。它不属于对外命令目录。单独 reader 持续读两端流，不允许“发请求后直接 readline 等一条”抢走其他响应。

UI 发给宿主的 start_selection_mode/list_windows/bind_window/hotkey 类命令由 Rust 处理；Rust 用同样信封转入事件总线。其余命令路由给 Python。消息 seq 由 Rust 面向 UI 出口统一分配，Python 内部序号不能冒充 UI seq。需要等待 host_response 的 Python worker 不占 IPC reader。

**内部约定（不进公开命令目录，对外命令总数仍为 62）**：宿主在无法经 UI 往返的同步上下文（如 `confirm_selection` 的宿主本地处理）中，可经内部同步通道向引擎发送引擎命令 `register_host_frame {token:string} → {image:ImageRef}`。宿主把捕获的最终帧写入数据根受控临时区并签发**一次性 host-frame token**（HMAC-SHA256 签名，会话绑定 + TTL + 单次；与 `import_image` 的 import_token 同一安全模型——引擎**只认 token，不接受任意路径**）；引擎校验 token/路径包含/sha256/尺寸后读入、**由引擎 SessionImageStore 签发** `ImageRef` 并删除临时文件。该命令仅供宿主↔引擎**内部**使用，不出现在 §命令目录，UI 与脚本不得调用；其线格式与安全约束见实现出处 `backend/image/host_frame.py`（引擎侧）与 `native/host/src/selection/`。

InputAction 为带 kind 的对象：move `{x,y,duration_ms}`；click `{x,y,button,clicks}`；drag `{from:{x,y},to:{x,y},duration_ms}`；scroll `{delta}`；key `{key,state:tap|down|up}`；text `{text,interval_ms}`；hotkey `{keys:[string]}`。点均 screen_physical；若非坐标动作则使用当前指针/前台并同样做区域/身份检查。输入响应 `{action_id,dispatched:true}` 仅表示调用完成，不保证目标应用业务成功。

## 超时、取消、去重与背压

- 普通命令响应期限 5s；hello/引擎启动 30s；执行/监控命令 5s 内受理，结果通过状态事件/查询获取，不等待脚本跑完。识别测试是异步 job：5s 内返回 job_id，推理单次限 5s。
- `cancel_request{request_id}` 仅取消尚未完成的可取消请求/job；`cancel_job{job_id}` 取消识别测试；`stop_script{execution_id}` 停止执行树。UI Promise 超时不等于后台取消成功，需查询状态；停止必须先由 Rust 关闭输入闸门。

桌面客户端取消对在途请求的等待时，宿主以 `CANCELLED` 结束该请求当前的等待者。后台取消是否成功取决于独立的取消回执。若原操作已经提交，其真实响应写回请求历史，可用 `query_request` 查询。
- 请求 ID 在会话内最多保留最近 10000 个最终响应。相同 ID/相同内容重复返回已有响应，不重新执行；同 ID 不同内容返回 CONFLICT。被淘汰 ID 再次收到返回 DUPLICATE_EXPIRED，不重做副作用。Rust 管会话已见 ID 集合并限制会话总数（100000，之后重建空闲会话）。跨进程重启去重不承诺，禁止自动重发写入或动作。
- `execute_script` 附可选 client_run_id；相同会话相同 client_run_id 返回同 execution_id。创建实体响应丢失时可用 query_request 查询 id；未知状态不能假定未写入。
- 写入进入 current 指针切换后不可取消；取消响应说明 `cancelled=false,reason=already_committed`，用户可查询 revision。未提交的草稿 job 可以取消。
- reader/writer 各自独立，单 writer 队列保证行完整。普通请求最多 32 在途，满则 BUSY；停止/心跳通道保留容量，不能被预览淹没。selection_updated/统计允许合并到最新；状态变更、终态必须可靠发送；无法投递则断开会话并停止输入，不无限增长内存。
- UI 事件 seq 由 Rust 面向出口统一分配：从 1 起单调递增，system_ready 为会话首事件（seq=1）；selection_updated/recognition_stats 允许合并到最新，合并窗口内覆盖不消耗 seq，故 seq 可出现缺口，UI 不得假设连续、只可假设单调递增。UI 发现缺口主动 query_execution/get_config/list_targets 重建（恢复流程将本地锚一次推进到实际收到的 seq，不逐帧重复恢复），不重放输入。迟到响应可以更新历史但不能复活已终止执行。启动每次使用新 session_id，旧会话消息全部拒绝。

## 命令目录

表中 `?` 表示可省略；参数未写默认值的必填。对象省略项按 02 默认补齐；其他字段禁止——该规则适用于全部命令（含 get_config 等只读命令），携带未声明参数报 INVALID_PARAMS，details.field 定位该多余字段。每条响应包含在统一 data 中。无返回值的变更用 `{ok:true}`。校验统一在引擎/宿主执行，UI 校验仅改善体验。

### 系统、捕获与识别

| command | params | data | 特殊规则 |
|---|---|---|---|
| hello | {} | 版本/能力/模型列表 | 每会话先调用 |
| ping | {} | {engine_time:string} | 无副作用 |
| query_request | {request_id:string} | {status:pending/completed/unknown,response:object或null} | 只查询同会话 |
| cancel_request | {request_id:string} | {cancelled:boolean,reason:string} | 幂等 |
| cancel_job | {job_id:string} | {cancelled:boolean} | 终态取消为 false；未知 job_id 亦返回 cancelled=false（幂等） |
| get_job | {job_id:string} | {status,result,error} | 会话内可查询进行中的作业及最近 256 个终态作业。终态自结束起最多保留 300 秒，先到数量或时间上限即淘汰，淘汰后返回 NOT_FOUND。查询不延长留存。 |
| get_display_list | {} | {displays:DisplayInfo[]} | 物理像素 |
| start_selection_mode | {display_id?:string} | {selection_id:string} | 受理不代表确认 |
| confirm_selection | {selection_id:string,region:Region} | {image:ImageRef,region:Region} | 主窗口恢复后再结束 |
| stop_selection_mode | {selection_id:string} | {ok:true} | 等价取消，恢复窗口 |
| import_image | {import_token:string} | {image:ImageRef} | token 只由文件选择器生成 |
| crop_image | {image_id:string,x:integer,y:integer,width:integer,height:integer} | {image:ImageRef} | 在已导入图或其裁剪副本的原尺寸像素中取半开矩形，左上角从零计。尺寸须为正且矩形完全在图内；越界报 INVALID_PARAMS。产生独立的 image_pixel 引用，保留源图。 |
| capture_region | {region:Region} | {image:ImageRef,frame_id:string} | 当前 UI 会话；不改变运行区域 |
| capture_full_screen | {display_id:string} | {image:ImageRef,frame_id:string} | 单显示器 |
| capture_window | {binding_id:string} | {image:ImageRef,frame_id:string} | 可见前台客户区 |
| release_image | {image_id:string} | {ok:true} | 幂等；所有权验证 |
| get_image_preview | {image_id:string,max_edge?:integer=400} | {mime:string,base64:string} | max_edge 1–800；编码≤512KiB |
| recognize_text | {image_id:string,model_id:string} | {job_id:string} | 最终 result={blocks:TextBlock[]} |
| match_template | {image_id:string,template_image_id:string,threshold?:number=.85} | {job_id:string} | 最终 result={matches:Candidate[]} |
| match_contour | {image_id:string,template_image_id:string,threshold?:number=.85} | {job_id:string} | 同上 |
| detect_color | {image_id:string,params:ColorParams} | {job_id:string} | 同上 |
| evaluate_target_conditions | {image_id:string,target_id:string} | {job_id:string} | 最终 result=RecognitionResult |
| test_recognition | {image_id:string,draft:TargetDraft} | {job_id:string} | 允许未保存草稿，结果 sample_no/target_id=null；绝不输入 |

TargetDraft 为 `{name,group_id?,image_id,anchor_condition_id,conditions,storage_mode?}`，与持久 Target 差异只有尚无 ID/编号/revision/时间；确认保存分配这些值。ColorParams 与 02 的 color 叶 params 相同。Candidate=`{bbox,center,confidence,space}`。

### 目标检测扩展契约 v1

生产调用方通过 `hello.capabilities` 与 `hello.model_manifests` 查当前可运行能力。当前发行清单没有 YOLO 检测器，缺少 `object_detect` 即该能力不可用。内部请求从注册表解析提供者，缺提供者返回 `CAPABILITY_DENIED`。版本化模型、请求和结果用于受信 Provider 适配，合成向量运行入口独立于生产工厂。[检测器契约](../schema/object-detection-v1.schema.json)

### 目标、分组和脚本文件

| command | params | data |
|---|---|---|
| create_target | {draft:TargetDraft} | {target:Target} |
| get_target | {target_id:string,preview_max_edge?:int(1–800)} | {target:Target,preview?:{mime,base64}}。指定边长时，从同一目标修订的已校验资产生成等比 PNG 预览。 |
| list_targets | {group_id?:string\|null,query?:string,offset?:int=0,limit?:int=100} | {items:Target[],total:int} |
| update_target | {target_id:string,expected_revision:int,patch:object} | {target:Target} |
| delete_target | {target_id:string,expected_revision:int} | {ok:true} |
| create_group | {name:string,parent_group_id?:string\|null} | {group:Group} |
| list_groups | {} | {groups:Group[]} |
| update_group | {group_id:string,name?:string,parent_group_id?:string\|null,expected_revision:int} | {group:Group} |
| delete_group | {group_id:string,expected_revision:int} | {ok:true} |
| move_target_to_group | {target_id:string,group_id:string或null,expected_revision:int} | {target:Target} |
| list_scripts | {offset?:int=0,limit?:int=100} | {items:Script[],total:int} |
| load_script | {script_id:string} | {script:Script,source:string} |
| save_script | {script_id?:string,expected_revision?:int,name:string,source:string,parameters:array,number_recording?:bool} | {script:Script} |
| set_script_hotkey | {script_id:string,expected_revision:int,hotkey:string或null} | {script:Script}。null 清除绑定，重复绑定返回原修订，同键占用返回 HOTKEY_CONFLICT。 |
| delete_script | {script_id:string,expected_revision:int} | {ok:true} |
| validate_script | {source:string} | {valid:boolean,diagnostics:array} |

update_target 白名单：name/group_id/conditions/anchor_condition_id/metadata；替换图片使用 image_id+storage_mode（宿主转 ImageAsset），不得传任意路径。更新 script 必须同时提供 ID 和 expected_revision；新建两者均省略。修改与删除影响活动执行的规则见 F02。搜索合并到 list_targets.query，不另设 search_targets。

分组契约扩展（用户 2026-10-12 裁决：只改既有两条命令参数，**对外命令当时共 62 条**）：`create_group.parent_group_id` 键缺省或显式 `null` 均为顶层（depth 0），字符串时须指向存在分组且 `depth=父.depth+1`；`update_group` 的 `name`/`parent_group_id` **至少给一个**（两者都键缺省 → INVALID_PARAMS，field=name），`name` 键缺省=不改名，`parent_group_id` 键缺省=不搬移、显式 `null`=移至顶层。同层组名经 NFC+大小写折叠后唯一（同层重名 → INVALID_PARAMS，field=name）；层级上限 3 层（depth 0..2，超限 → RESOURCE_LIMIT，field=parent_group_id）；父组/目标组不存在 → GROUP_NOT_FOUND（field=parent_group_id）。`list_targets.group_id`：键缺省=不过滤、显式 `null`=仅未分组、字符串=精确该组（不递归）。

脚本契约语义（TASK-012；5 命令与「更新/新建」规则已在上表逐字存在，**无命令集合变更**，当时共 62 条）：`save_script` **保存不执行、不做语法校验**（语法检查单独由 `validate_script` 提供），源码须为合法 UTF-8 且编码后**字节** ≤ 1MiB（超限 → RESOURCE_LIMIT，field=source）；`script_id` 与 `expected_revision` **同时给=更新、同时省=新建、只给其一 → INVALID_PARAMS**（field=缺失的伙伴字段）；同 `(name,parameters,source_sha256)` 重复保存为 **no-op**（不提交、不发 `catalog_changed`）。`delete_script` 对活动执行引用的脚本 → IN_USE（field=script_id）；删除只删元数据，源码资产 `scripts/<id>/<hash>.lua` 只保留不回收。`list_scripts` `limit` 取值 1–500、排序 `created_at` 升序后 `id` 升序。`validate_script` 返回 `{valid,diagnostics}`（`valid == diagnostics==[]`）；`diagnostics[].line` 自 1 起，`column` 为 `int≥1` 或 `null`（引擎无法**无歧义**定位列时为 `null`，不编造）；引擎未安装 Lua 编译器（lupa）时诚实抛 `ENGINE_UNAVAILABLE`（retryable=false，无 details），绝不伪报 `valid:true`。

识别契约语义（TASK-013；6 条识别命令与 `{job_id}` 受理已在上表逐字存在，**无命令集合变更**，当时共 62 条）：识别为**异步 job**，命令同步（≤5s）返回 `{job_id}`，终态经 `get_job` 与事件双路可查。`match_template`/`match_contour`/`detect_color` 的 job `result` 为 `{matches:Candidate[], truncated:boolean}`（候选经 IoU>0.5 NMS 去重、最多 100 个，超出标 `truncated=true`）；`recognize_text` 为 `{blocks:TextBlock[]}`；`evaluate_target_conditions`/`test_recognition` 为 `RecognitionResult`（`found` 恒等于 `matches` 非空；未保存草稿结果 `target_id`/`sample_no=null`）。**未命中（`found=false`/空候选）是数据不是故障**：job 成功完成并发 `recognition_completed`，**不发** `recognition_error`；仅服务错误（模型缺失/能力未提供/超时/内部故障）发 `recognition_error`。单次推理超时（>5s）→ `RECOGNITION_TIMEOUT`（retryable=true）并丢弃结果；取消经 `cancel_job` 传导，终态发 `recognition_cancelled`；`recognition_stats` 由运行/监控层按采样窗口发射，不由识别基座发射。**缺模型分支按原因分类**：`recognize_text` 在 Provider 缺已安装模型时抛 `MODEL_UNAVAILABLE`（区别于缺引擎的 `ENGINE_UNAVAILABLE` 与未装配的 `CAPABILITY_DENIED`），三者均 `retryable=false`。

OCR 能力披露内容约定（TASK-015；**不改** `capabilities[].reason` 的 `string|null` 类型，仅固定 `id="ocr"` 项在 `unavailable` 态的**内容格式**，当时公开命令共 62 条）：`capabilities[ocr].reason` 为一行的**固定规范格式**，字段顺序固定，便于前端稳定解析（避免随版本漂移）与单测锁定：

```text
OCR|state=<..>|code=<..|->|models=<csv|->|verified=<csv|->|unverified=<csv|->|models_dir=<..|->|library=<..|->|detail=<text>
```

- `state`：能力态（`available` 时为 `available`；`unavailable` 时为 `unavailable`）；`code`：06 错误码（如 `ENGINE_UNAVAILABLE`/`MODEL_UNAVAILABLE`/`DATA_CORRUPT`）或占位 `-`；
- `models`：已安装**识别**模型 id（CSV，即 `recognize_text{model_id}` 可请求集合）；
- `verified` 与 `unverified` 分别列出有映射证据和待取证的语言。日语的版本化路由与固定样本依据见[补充取证裁决](decisions/技术决策.md#p3-ja-mapping)。逐语言质量由候选包的 TEST-004 单独测量。
- `models_dir`/`library`：数据根模型目录与权重库根**绝对路径**（可审计的装配来源）；空值占位 `-`；
- `detail`：人类可读说明（如"未安装 paddleocr（须在探针 venv 运行）"）；字段值内的 `|` 替换为 `/` 以保证可解析。

WinUI 能力面板按此展示真实覆盖；解析失败时必须**回退原样文本**，不得虚构可用（`available` 的功能才可显示可用）。该约定**不引入结构化字段**，仅约束 `reason` 字符串内容。

### 运行与调试

| command | params | data/约束 |
|---|---|---|
| execute_script | RunConfig + client_run_id?:string | {execution_id,status:starting} |
| start_monitor | MonitorConfig + client_run_id?:string | {execution_id,status:starting} |
| pause_script / resume_script / stop_script | {execution_id:string} | {state:ExecutionState}；非法转换 INVALID_STATE，停止幂等 |
| get_script_state | {execution_id:string} | {state:ExecutionState} |
| query_execution | {execution_id?:string,client_run_id?:string} | {state:ExecutionState}；恰填一项 |
| get_execution_logs | {execution_id:string,offset?:int=0,limit?:int=100} | {items:RunLog[],total:int}；limit≤500 |
| list_execution_history | {offset?:int=0,limit?:int=100} | {items:执行历史摘要[],total:int}；包括活动执行，脚本摘要可附启动时的 script_name，limit≤500 |
| clear_execution_logs | {execution_id:string} | {ok:true}；仅清除指定终态执行的日志，保留摘要 |
| add_breakpoint | {script_id:string,source_hash:string,line:int} | {breakpoint:Breakpoint} |
| remove_breakpoint | {breakpoint_id:string} | {ok:true} |
| list_breakpoints | {script_id:string} | {items:Breakpoint[]} |
| step_into / step_over / step_out / continue_execution | {execution_id:string} | {state:ExecutionState}；必须 debug_paused |
| get_variables | {execution_id:string,frame_id:string} | {variables:array,truncated:boolean}；只读 |
| get_stack | {execution_id:string} | {frames:array}；每帧 frame_id/script_id/line/source_hash |

Breakpoint=`{id,script_id,source_hash,line,enabled}`，line 为 ≥1 的可执行行；无条件表达式。RunLog=`{timestamp,execution_id,level,event,message,details}`。变量项 `{name,type,value,reference_id?,truncated}`，按 01 限额截断。

### 窗口、输入和配置

| command | params | data/规则 |
|---|---|---|
| list_windows | {} | {windows:WindowInfo[]} |
| get_window_info | {window_id:string} | {window:WindowInfo} |
| bind_window | {window_id:string} | {binding:WindowBinding}；仅空闲草稿，运行中拒绝 |
| unbind_window | {} | {ok:true}；运行中拒绝 |
| get_bound_window | {} | {binding:WindowBinding或null} |
| get_config | {} | {config:Config,data_root:string,recovery?:{revision:int}}；会话内发生自动备份恢复时附恢复修订 |
| update_config | {expected_revision:int,patch:object} | {config:Config} |
| reset_config | {expected_revision:int} | {config:Config}；不删除目标/脚本 |
| list_catalog_backups | {} | {backups:[备份编号]}，只列完整发布的备份目录 |
| create_catalog_backup | {} | {job_id}；完成后 result 含 backup_id、generation、counts |
| preview_catalog_restore | {backup_id} | {job_id}；result 含 backup_id、generation、counts、expected_current、expected_backup、current_state |
| restore_catalog_backup | {backup_id,expected_current,expected_backup} | {job_id}；result 含 generation、protection。成功后发送 catalog_changed |

P3 新增上述四条备份命令，后续增加本地图像裁剪，当前公开路由共 69 条。长操作用 get_job 查询。用户在确认恢复前可以取消对话框。存储写作业开始后 cancel_job 返回 cancelled=false，终态按实际指针提交结果记录。预览后当前指针或备份指纹改变返回 CONFLICT，客户端重新预览。CLI 复用相同的数据根锁、预览指纹和恢复实现。

输入通过 host_request/input_action 处理；不向 WinUI 客户端提供绕过 execution_id/lease 的裸 move_mouse/click_mouse/type_text 入口。手动输入测试需专用一次性 RunConfig，同样受停止与区域约束。旧 set_region_limit/clear_region_limit 已合并入启动配置，运行中不能扩权。热键注册归配置事务管理，不给 UI 任意 unregister 停止热键的命令。

## 事件目录

| name | payload 必填字段 | 语义 |
|---|---|---|
| system_ready | capabilities、versions | 引擎可服务；OCR 可仍 unavailable |
| system_error | error | 系统故障；不等于未命中 |
| selection_started | selection_id、display_id | 主窗口已隐藏 |
| selection_updated | selection_id、region、preview:ImageRef | 可合并；不在事件放大图 |
| selection_completed | selection_id、region、image:ImageRef | 与确认请求响应同一结果；按 ID 去重 |
| selection_cancelled | selection_id、reason | 正常取消/错误，finally 已恢复 |
| recognition_completed | job_id、result | 异步识别终态 |
| recognition_error | job_id、error | 只用于服务错误 |
| recognition_cancelled | job_id | 正常取消 |
| script_state_changed | execution_id、status、reason | 权威状态变更；终态可查询完整 result/error |
| script_iteration | execution_id、iteration | 完成一轮，非输入指令 |
| target_detected | execution_id、result:RecognitionResult | 仅触发判定成立时发，UI 不据此自动点击 |
| recognition_stats | execution_id、effective_hz、throttled、last_found | 可合并，普通未命中不刷错误日志 |
| breakpoint_hit / step_completed | execution_id、frame_id、script_id、line | debug_paused 快照可查 |
| log_message | log:RunLog | 仅调试暂停时订阅；普通模式不实时推送 |
| window_changed | binding_id、geometry_revision、state | 旧匹配作废 |
| catalog_changed / config_changed | revision或generation | UI 重新查询，不擅自改活动快照 |

## 已移除的原稿歧义

不保留无 ID 同步请求、EVENT 前缀、路径调用脚本、`normal` 与 `run_once` 双枚举、可写 evaluate_expression、裸 UI 输入命令或三份错误码表。前端迁移沿用现有协议、数据版本和 Lua 脚本契约。升级测试应从既有数据根加载目标、脚本、图片与配置。

## P0 原生客户端

每个 WinUI 应用共用一个 HostClient。BeginRequest 返回会话、请求 ID 和完成任务，查询及取消沿用原会话。原生 ABI 在同一把锁内检查会话并取得对应路由。普通请求最多 32 个在途，控制请求和停止命令各有独立容量。Rust 的单写线程可在命名管道上等待，停止输入与关闭进程树可同时执行。

两个窗口通过同一 DispatcherQueue 收取事件。关闭副窗会退订并丢弃仍在等待的界面更新。遇到序号缺口，客户端重读配置、分页目标列表及已知在运行的执行状态。结束的执行不会因迟来的状态通知重新出现。GetConfigAsync、UpdateConfigAsync、ResetConfigAsync 返回已解包的 ConfigDocument。更新和重置传原修订号，错误保留 code、message、details、retryable。

P2 扩展两个历史命令，公开目录共 64 条。get_execution_logs 可读取保留执行的日志分卷；当前会话内仍可查询内存摘要。日志页遇到消息字节上限时附 truncated=true 和 next_offset。清除活动执行的日志返回 INVALID_STATE。日志盘写入失败通过 system_error 披露，普通日志仍由用户主动查询。

## 后续自动化版本的模型管理命令

| 命令 | 参数 | 回执 |
|---|---|---|
| list_ocr_models | {} | revision、pipelines 及各管线的实际加载状态 |
| preview_ocr_model_change | {operation:"import" 或 "remove", model_id, directory?} | job_id，成功结果含 preview_id、revision、文件字节与静态引用。directory 为用户在本机选择的包含组件子目录的离线介质根，仅导入时提供 |
| apply_ocr_model_change | {preview_id, expected_revision} | job_id，提交成功后返回新模型库快照；复用现有 get_job、cancel_job。预览属于当前会话 |

首次实际请求按指定模型加载。hello 随时可重查当前能力和已装模型，system_ready 仍只发送一次。尚有健康管线时 OCR 可受理；具体是否已加载由 list_ocr_models 披露。缺失、损坏、取消按原识别 job 终态返回。
## ORB 能力查询

自动化扩展通过现有 hello 的能力表披露 `feature_match`。协议主版本沿用 1，能力和叶参数由调用者按标识选择。内置 `builtin.orb` 的 provider_version 为 1.0.0。通过 `test_recognition`、`evaluate_target_conditions` 及原有作业查询接口取得结果；匹配沿用源帧 ID、几何修订、半开 bbox、中心及分数。
## 脚本级和动作级配置

`save_script` 可提交版本化 `action_policy`。更新时省略则保留该脚本原设置，传仅含版本号的对象清空设置。单动作覆盖独立合并每个属性，再经既有 `input_action` 的 action.policy 送到 Rust。在该 action_id 的去重后采样一次并预检整条轨迹。异常、取消和部分派发沿用原动作契约。

## 绑定窗口后台取像

`get_window_capture_capabilities(binding_id)` 返回同一绑定的 `capture.non_foreground`、`capture.fully_occluded` 与 `reason`。两项独立取值 `unknown`、`available` 或 `unavailable`。切换前台后成功绘制才登记非前台能力；普通前台窗口的无异形客户区完全覆盖目标且后台绘制成功时登记全遮挡能力。最小化、隐藏和平台能力缺失直接报告原因。

`capture_window`、`capture_region`、`execute_script`、`start_monitor` 接受可选 `window_mode`。用 `list_windows` 和 `bind_window` 明确建立绑定，再将 `window_mode` 设为 `background`。单帧成功返回新的 `frame_id` 和 `image`，图像可按原协议预览和释放。捕获前后均校验宿主保存的窗口身份和几何。窗口最小化返回 `WINDOW_UNAVAILABLE`，关闭或绑定失效返回 `WINDOW_NOT_FOUND`，绘制超时返回 `CAPTURE_FAILED`。
## 前台录制会话

`start_recording {context}` 使用运行设置的 `display_id`、`binding_id` 和可选 `region`。同一桌面同时仅允许一个录制或输入执行；同一请求 ID 的重发沿用一般请求去重。`pause_recording`、`resume_recording`、`get_recording_state`、`finish_recording`、`cancel_recording` 均仅接受当前会话的 `{recording_id}`。状态含 `status`、`action_count`、`interrupted`、`keyboard_source`。`finish_recording` 返回冻结的内存 `actions`。取消清除草稿，结束后拒绝新事件。

调用方可在开始请求里传入新的 UUID 字段 `recording_id`。省略时由宿主生成。WinUI 在启动回执超时后按本次 ID 查询状态，避免再次启动应用。即使首次读到的状态已经是 `finished`，调用方仍通过 `finish_recording` 取得与该 ID 对应的动作。

Windows 鼠标来源是可见录制时段内的鼠标事件。键盘来源 `focused_window_messages` 需要目标窗口主动提供自身收到的键消息。当前普通第三方窗口的完整键序支持尚未验收。窗口原生接入在顶层 HWND 设置非零属性 `ScreenTrigger.RecordingInput.v1`。实际焦点及鼠标归属确认后，宿主向该 HWND 发送 `WM_COPYDATA`，`dwData=0x53545231`，16 字节为本次随机令牌，`wParam` 为可见停止窗口。目标只对自己的非密码焦点控件转发实际键按下和释放。回复使用 `WM_COPYDATA`，`wParam` 为实际焦点 HWND，`dwData=0x53545232`，数据为原令牌、两个小端 `u16`（VK、扫描码）及四个 `u8`（扩展位、按下位、受保护控件标志、版本 1）。受保护控件拒绝采集。协议不接受文本、剪贴板、候选词、其他窗口消息或磁盘时间线。

用户显式开始录制，并通过范围内点击建立归属后，标准 Edit、RichEdit、按钮和列表等具有已知输入语义的前台控件还可报告 `keyboard_source=foreground_command_keys`、`keyboard_available=true`。此来源只提供导航键、功能键、修饰键和已列明的输入法命令键。每次采样前后均复核实际焦点及范围，原生编辑框同时复核密码样式和运行时遮盖字符。快捷键多边沿次序无法确定或采样间隔过大时，中止本次组合。短于采样间隔的脉冲无法重建。普通窗口的字母数字完整键序仍须目标主动接入。未取得来源时客户端显示“当前焦点尚无键盘录制来源”。
使用普通应用时，显式传入 `start_recording {context, isolated_app:{executable,scope}}`。用户选择本地 GUI 可执行文件。宿主在新桌面启动该程序，`window` 取新进程的客户区，`region` 和 `display` 分别使用当前显示器上的屏幕选区和整台显示器。状态中的 `isolated_app.desktop_name` 和 `pid` 标识本次目标，`keyboard_source=isolated_control_keys` 表示隔离来源。宿主在该桌面成为输入桌面、目标前台标准控件取得焦点并通过密码检查后采样。两次采样之间完全结束的短按可能漏录，无法确定次序的组合会标记中止。结束或取消后 `keyboard_available` 为 false，并返回原桌面。

## 已绑定窗口的后台输入

`get_window_input_capabilities({binding_id})` 返回绑定、`input.{click,drag,scroll,key,hotkey}` 和 `reason`。各项为 `available`、`unknown` 或 `unavailable`。目标可用 Windows 注册消息 `ScreenTrigger.BackgroundInput.Capabilities.v1` 声明其已实现的消息处理能力。查询的两个参数为零，返回值为 0x53540000 加上五项低位掩码（依次为 1、2、4、8、16）。无声明的普通窗口展示待验证；目标最小化、隐藏、拒绝访问或无响应时展示原因。

执行的 `window_mode: "background"` 由受理时的窗口绑定和输入租约锁定。`input_action` 沿用已有动作。鼠标消息使用该目标或同进程子控件的客户区物理坐标，滚轮消息使用屏幕物理坐标。键盘消息只定位到该目标线程的焦点后代或虚拟落点所在控件。成功回执仍为 `dispatched:true`，程序内业务效果由接收方确认。超时报告 `INPUT_FAILED` 和 `details.delivery_unknown:true`，相同动作 ID 不重发。后台脚本查询位置时，宿主 `query_pointer` 另收该执行的 `execution_id` 和 `lease_id`，返回本租约的虚拟落点。
