//! IPC 强类型、静态命令路由表与错误结构（TASK-005 设计 §3.1/§5.1/§5.2/§6）。
//!
//! 范围（批次 B2）：
//! - [`IpcError`]：docs/06 错误结构四字段（code/message/details/retryable）；
//! - [`CommandClass`]（Normal/Control）与静态命令路由表（03 命令目录硬编码；
//!   表外命令直接 UNKNOWN_COMMAND 不出壳——03/M08：只能路由已注册命令）；
//! - [`RouterInput`]：单 Router 线程消费的四类输入（设计 §4.1/§2.2）；
//! - 出站请求信封构造（send 侧 2MiB 护栏在 dispatcher 执行，§6.5）；
//! - [`StopGate`]：stop_script 转发前关输入闸门的 Hook（M05 前 NullStopGate）。
//!
//! 不实现：事件总线/hostbridge（批次 B3，经 [`super::EventHandler`] 注入）。
//! 协议解析权威仍在 sidecar/protocol.rs（设计决策④），本模块只消费
//! `ProtocolEnvelope`；错误码逐字对齐 docs/06 错误码注册表。

use std::sync::mpsc;
use std::time::Duration;

use serde::Serialize;
use serde_json::{Value, json};

use crate::sidecar::protocol::ProtocolEnvelope;

// ---------------------------------------------------------------------------
// 常量（全部照 docs/03、docs/06 与 TASK-005 设计 §6，不发明数值）
// ---------------------------------------------------------------------------

/// 单帧最大字节数（03：单帧最大 2MiB，双端校验；2×1024×1024 = 2 097 152）。
pub const MAX_FRAME_BYTES: usize = 2 * 1024 * 1024;

/// UI 请求 id 约束（§5.1：非空、≤128 字符）。
pub const REQUEST_ID_MAX_LENGTH: usize = 128;

/// 普通/业务命令响应期限（06：普通 IPC 5s）。
pub const NORMAL_DEADLINE: Duration = Duration::from_millis(5_000);

/// hello/引擎启动期限（06：启动 30s）。
pub const HELLO_DEADLINE: Duration = Duration::from_millis(30_000);

// ---------------------------------------------------------------------------
// 错误结构（docs/06：{code,message,details,retryable} 四字段）
// ---------------------------------------------------------------------------

/// IPC 错误（06 错误码注册表逐字；details 不含堆栈敏感对象）。
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct IpcError {
    pub code: String,
    pub message: String,
    pub details: Value,
    pub retryable: bool,
}

impl IpcError {
    /// 06 四字段错误。无字段详情时返回空对象。
    pub fn new(code: &str, message: &str, retryable: bool) -> Self {
        IpcError {
            code: code.to_string(),
            message: message.to_string(),
            details: json!({}),
            retryable,
        }
    }

    /// 附加 details（链式）。
    pub fn with_details(mut self, details: Value) -> Self {
        self.details = details;
        self
    }

    /// 普通（Normal 类）在途 ≥32（06：BUSY，retryable=true）。
    pub fn busy() -> Self {
        Self::new("BUSY", "普通请求在途已达上限（32），请稍后重试", true)
    }

    /// 同 ID 不同内容（03：CONFLICT）。
    pub fn conflict() -> Self {
        Self::new("CONFLICT", "同一请求 ID 携带了不同内容", false)
    }

    /// 被淘汰 ID 再达（03：DUPLICATE_EXPIRED，不重做副作用）。
    pub fn duplicate_expired() -> Self {
        Self::new(
            "DUPLICATE_EXPIRED",
            "请求 ID 的历史响应已淘汰，请查询实体状态",
            false,
        )
    }

    /// 表外命令（M08：只路由已注册命令，不出壳）。
    pub fn unknown_command(command: &str) -> Self {
        Self::new("UNKNOWN_COMMAND", "未注册命令", false)
            .with_details(json!({ "command": command }))
    }

    /// 宿主类命令骨架期（06：CAPABILITY_DENIED——使用未提供的宿主能力）。
    pub fn capability_denied(command: &str) -> Self {
        Self::new("CAPABILITY_DENIED", "宿主能力未提供（骨架期未接入）", false)
            .with_details(json!({ "command": command }))
    }

    /// 进程退出/不可用（06：ENGINE_UNAVAILABLE，允许用户重启，retryable=true）。
    pub fn engine_unavailable(message: &str) -> Self {
        Self::new("ENGINE_UNAVAILABLE", message, true)
    }

    /// 请求超时（设计 §6.3/§9-D5：03/06 未给超时专码，采用 INTERNAL_ERROR +
    /// message 含期限，details 含 request_id 与 deadline_ms）。
    pub fn timeout(deadline_ms: u64, request_id: &str) -> Self {
        Self::new("INTERNAL_ERROR", &format!("请求超时({deadline_ms}ms)"), false)
            .with_details(json!({ "request_id": request_id, "deadline_ms": deadline_ms }))
    }

    /// 参数校验失败（06：INVALID_PARAMS，定位字段）。
    pub fn invalid_params(message: &str) -> Self {
        Self::new("INVALID_PARAMS", message, false)
    }

    /// 畸形帧/超限（06：PROTOCOL_ERROR）。
    pub fn protocol_error(message: &str) -> Self {
        Self::new("PROTOCOL_ERROR", message, false)
    }

    /// 未分类内部故障（06：INTERNAL_ERROR）。
    pub fn internal(message: &str) -> Self {
        Self::new("INTERNAL_ERROR", message, false)
    }

    /// 输入派发失败（06：INPUT_FAILED——SendInput 返回失败/不完整；**不自动重试**）。
    pub fn input_failed(message: &str) -> Self {
        Self::new("INPUT_FAILED", message, false)
    }

    /// 输入点/路径越界（06：REGION_LIMIT_VIOLATION；改参数/范围，false）。
    pub fn region_limit_violation(message: &str) -> Self {
        Self::new("REGION_LIMIT_VIOLATION", message, false)
    }

    /// 几何版本变更（06：STALE_FRAME——旧帧用于输入；丢弃并重新识别，true）。
    pub fn stale_frame(message: &str) -> Self {
        Self::new("STALE_FRAME", message, true)
    }

    /// 窗口身份失效/已关闭（06：WINDOW_NOT_FOUND；当前根执行失败，false）。
    pub fn window_not_found(message: &str) -> Self {
        Self::new("WINDOW_NOT_FOUND", message, false)
    }

    /// 失焦/最小化/遮挡导致不可操作（06：WINDOW_UNAVAILABLE；暂停等待用户处理，true）。
    pub fn window_unavailable(message: &str) -> Self {
        Self::new("WINDOW_UNAVAILABLE", message, true)
    }

    /// 状态非法（闸门已关 / 租约不符 / 重复 release；06：INVALID_STATE，false）。
    pub fn invalid_state(message: &str) -> Self {
        Self::new("INVALID_STATE", message, false)
    }
}

/// 从入站 `response.error`（Value）还原 [`IpcError`]（06 四字段；
/// 字段缺失按 INTERNAL_ERROR/空文案兜底，retryable 默认 false）。
pub fn ipc_error_from_value(value: &Value) -> IpcError {
    IpcError {
        code: value
            .get("code")
            .and_then(Value::as_str)
            .unwrap_or("INTERNAL_ERROR")
            .to_string(),
        message: value
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        details: value.get("details").filter(|v| v.is_object()).cloned().unwrap_or_else(|| json!({})),
        retryable: value
            .get("retryable")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    }
}

/// 入站 response 信封 → 请求结果（03：成功含 data、失败含 error，互斥）。
/// success=true 且无 data → Ok(Null)；success 非 true 且无 error → 内部错误兜底。
pub fn envelope_response_result(envelope: &ProtocolEnvelope) -> Result<Value, IpcError> {
    if envelope.success == Some(true) {
        Ok(envelope.data.clone().unwrap_or(Value::Null))
    } else {
        match envelope.error.as_ref() {
            Some(err) => Err(ipc_error_from_value(err)),
            None => Err(IpcError::internal("响应缺少 error 字段")),
        }
    }
}

// ---------------------------------------------------------------------------
// 命令分类与静态路由表（来源=docs/03 命令目录，§5.2；表外 → Unknown 不出壳）
// ---------------------------------------------------------------------------

/// 命令类别（§5.2）：Control 不受普通 32 在途限制（03：停止/心跳通道保留容量）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CommandClass {
    Normal,
    Control,
}

/// Rust 侧路由判定（§5.2）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Route {
    /// 转发 Python（框架命令 + 引擎业务命令 + Python 实现的宿主资源命令；业务命令
    /// 骨架期由 Python dispatcher 回 UNKNOWN_COMMAND——框架路由位已注册，§1.2）。
    ForwardPython,
    /// 宿主类命令（窗口绑定族，TASK-018 前）：骨架期统一回 CAPABILITY_DENIED，
    /// 不转发 Python（§5.2）。
    HostDenied,
    /// **Rust 本地宿主能力**（TASK-008 框选）：由 `HostCommandHandler` 处理，
    /// 不出壳。（03：UI 发给宿主的 start_selection_mode 类命令由 Rust 处理。）
    HostHandled,
    /// 表外命令：Rust 直接 UNKNOWN_COMMAND，不出壳（M08）。
    Unknown,
}

/// 框架命令（§5.2 第一类；转发 Python，Python dispatcher/jobs 已实现）。
/// TASK-006：config 三命令（get_config/update_config/reset_config）自宿主类
/// 移入本表（设计 §2.3：数据权威在 Python backend/config/；03 全双工条款
/// "其余命令路由给 Python"现文一致，无需改 03）。
const FRAMEWORK_COMMANDS: [&str; 9] = [
    "hello",
    "ping",
    "query_request",
    "cancel_request",
    "cancel_job",
    "get_job",
    "get_config",
    "update_config",
    "reset_config",
];

/// 宿主类命令由 **Python 实现**（资源类：捕获/图像缓存/导入），TASK-007/009 已交付
/// `backend/capture/commands.py` + `backend/image/commands.py`。按 03「其余命令路由
/// 给 Python」转发 Python（此前误置 HostDenied，导致 import_image / capture_region /
/// get_image_preview 等经 UI 的端到端路径返回 CAPABILITY_DENIED——TASK-008 修正）。
const HOST_PYTHON_COMMANDS: [&str; 7] = [
    "import_image",
    "get_display_list",
    "capture_region",
    "capture_full_screen",
    "capture_window",
    "release_image",
    "get_image_preview",
];

/// **Rust 本地宿主能力**（TASK-008 框选，03：UI 发给宿主的 start_selection_mode 类
/// 命令由 Rust 处理）：经 [`super::HostCommandHandler`] 在 Rust 内处理并回同样信封。
const HOST_HANDLED_COMMANDS: [&str; 3] = [
    "start_selection_mode",
    "confirm_selection",
    "stop_selection_mode",
];

/// 宿主窗口族命令（03 归属 Rust/后续任务 TASK-018，骨架期 CAPABILITY_DENIED）。
/// TASK-008：18→15（config 三命令移出，TASK-006）→ 再移 7 条 Python 实现命令与
/// 3 条框选命令 → 余 5 条窗口命令。
const HOST_COMMANDS: [&str; 5] = [
    "list_windows",
    "get_window_info",
    "bind_window",
    "unbind_window",
    "get_bound_window",
];

/// 引擎业务命令（§5.2 第三类；转发 Python → 骨架期 UNKNOWN_COMMAND，
/// 业务 handler 属 M03–M07）。
const ENGINE_COMMANDS: [&str; 44] = [
    // 识别测试（异步 job）。TASK-013：6 条识别命令由 Python 侧 register_vision_commands
    // 注册（job result：match_*/detect_color={matches,truncated}（Q4/02）；recognize_text={blocks}；
    // 条件树两命令=RecognitionResult）。无命令集合变更——total==62 断言不变。
    "recognize_text",
    "match_template",
    "match_contour",
    "detect_color",
    "evaluate_target_conditions",
    "test_recognition",
    // 目标与分组（TASK-011：create_group/update_group 扩展 parent_group_id 参数，
    // 仅改参数不新增命令——命令集合与 total==62 断言不变）
    "create_target",
    "get_target",
    "list_targets",
    "update_target",
    "delete_target",
    "create_group",
    "list_groups",
    "update_group",
    "delete_group",
    "move_target_to_group",
    // 脚本文件（TASK-012：元数据/源码存储/仅编译语法校验；5 命令已在表内，
    // 无命令集合变更——total==62 断言不变）
    "list_scripts",
    "load_script",
    "save_script",
    "delete_script",
    "validate_script",
    // 运行与调试
    "execute_script",
    "start_monitor",
    "pause_script",
    "resume_script",
    "stop_script",
    "get_script_state",
    "query_execution",
    "get_execution_logs",
    "list_execution_history",
    "clear_execution_logs",
    "list_catalog_backups",
    "create_catalog_backup",
    "preview_catalog_restore",
    "restore_catalog_backup",
    "add_breakpoint",
    "remove_breakpoint",
    "list_breakpoints",
    "step_into",
    "step_over",
    "step_out",
    "continue_execution",
    "get_variables",
    "get_stack",
];

/// 控制类命令（§5.2：不受 32 在途限制；03 停止/心跳通道保留容量）。
const CONTROL_COMMANDS: [&str; 5] =
    ["stop_script", "cancel_request", "cancel_job", "ping", "query_request"];

/// 命令类别判定（§5.2：控制类清单内 → Control，其余全部 Normal）。
pub fn command_class(command: &str) -> CommandClass {
    if CONTROL_COMMANDS.contains(&command) {
        CommandClass::Control
    } else {
        CommandClass::Normal
    }
}

/// 静态路由表判定（§5.2；表外 → Route::Unknown 不出壳）。
pub fn route_command(command: &str) -> Route {
    if FRAMEWORK_COMMANDS.contains(&command)
        || ENGINE_COMMANDS.contains(&command)
        || HOST_PYTHON_COMMANDS.contains(&command)
    {
        Route::ForwardPython
    } else if HOST_HANDLED_COMMANDS.contains(&command) || HOST_COMMANDS.contains(&command) {
        Route::HostHandled
    } else {
        Route::Unknown
    }
}

/// 请求期限（§6.3：hello/引擎启动 30s；普通/业务 5s；控制类无独立期限取 5s）。
pub fn deadline_for(command: &str) -> Duration {
    if command == "hello" {
        HELLO_DEADLINE
    } else {
        NORMAL_DEADLINE
    }
}

// ---------------------------------------------------------------------------
// Router 输入（设计 §4.1：全部入站事件汇入一条命令通道，单 Router 线程串行消费）
// ---------------------------------------------------------------------------

/// Router 事件循环的输入消息（设计 §4.1 逐字）。
pub enum RouterInput {
    /// 来自 stdout drain 线程的已解析帧。
    Frame(ProtocolEnvelope),
    /// UI invoke `ipc_request`（reply 为 per-request oneshot 发送端）。
    UiRequest {
        id: String,
        command: String,
        params: Value,
        reply: mpsc::Sender<Result<Value, IpcError>>,
    },
    /// UI invoke `ipc_cancel`（幂等；§4.3）。
    UiCancel {
        request_id: String,
        reply: mpsc::Sender<Result<Value, IpcError>>,
    },
    /// 引擎清理/应用退出：清空 pending（ENGINE_UNAVAILABLE 快速失败）。
    Shutdown,
}

/// 出站请求信封构造（03 消息封装：request 必填 protocol_version/session_id/
/// type/id/command/params；字段名与 03 逐字对齐）。
pub fn request_envelope(session_id: &str, id: &str, command: &str, params: &Value) -> Value {
    json!({
        "protocol_version": crate::sidecar::protocol::SUPPORTED_PROTOCOL_VERSION,
        "session_id": session_id,
        "type": "request",
        "id": id,
        "command": command,
        "params": params,
    })
}

// ---------------------------------------------------------------------------
// 输入闸门 Hook（§5.2：stop_script 转发前先关输入闸门；M05 替换实现，不改结构）
// ---------------------------------------------------------------------------

/// 输入闸门 Hook trait（与 sidecar::InputGate 语义一致；ipc 层以 trait 依赖注入
/// 保持独立编译可测，B3 接线时由 Supervisor 适配，设计 §2.1 解耦原则）。
pub trait StopGate: Send {
    /// 关闭输入闸门（stop_script 转发前 / 会话清理第一步）。
    fn close_gate(&self, reason: &str);

    /// 关闭**指定执行**的输入闸门（M05：`close_gate(execution_id, reason)`）。
    ///
    /// 默认实现委托 [`StopGate::close_gate`]，因此既有实现（`NullStopGate`/
    /// 契约测试 `GateSpy`）零改动仍满足“先关闸门”语义；`stop_script` 转发前调用点
    /// 改调本方法（TASK-017）。
    fn close_gate_for(&self, execution_id: &str, reason: &str) {
        let _ = execution_id;
        self.close_gate(reason);
    }
}

/// 骨架空实现：无输入派发能力，close_gate 为 no-op（M05 替换）。
pub struct NullStopGate;

impl StopGate for NullStopGate {
    fn close_gate(&self, _reason: &str) {}
}

// ---------------------------------------------------------------------------
// 单元测试（路由表完备性：03 命令目录 64 条全部可路由、分类正确）
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn control_commands_are_exactly_the_frozen_list() {
        for cmd in CONTROL_COMMANDS {
            assert_eq!(command_class(cmd), CommandClass::Control, "{cmd} 应为控制类");
            assert_eq!(
                route_command(cmd),
                Route::ForwardPython,
                "{cmd} 应转发 Python"
            );
        }
        // 控制类清单外全部 Normal
        assert_eq!(command_class("hello"), CommandClass::Normal);
        assert_eq!(command_class("execute_script"), CommandClass::Normal);
        assert_eq!(command_class("capture_region"), CommandClass::Normal);
    }

    #[test]
    fn routing_table_covers_full_command_catalog() {
        // 03 命令目录 68 条 = 框架 9（含 config 三命令，TASK-006 §2.3）
        //   + 宿主-Python 7（TASK-007/009 已实现）+ 框选 3（TASK-008 Rust 处理）
        //   + 窗口 5（TASK-018 前 HostDenied）+ 引擎 44；无重叠、无遗漏
        let total = FRAMEWORK_COMMANDS.len()
            + HOST_PYTHON_COMMANDS.len()
            + HOST_HANDLED_COMMANDS.len()
            + HOST_COMMANDS.len()
            + ENGINE_COMMANDS.len();
        assert_eq!(total, 68);
        for cmd in ["list_execution_history", "clear_execution_logs", "list_catalog_backups",
                    "create_catalog_backup", "preview_catalog_restore", "restore_catalog_backup"] {
            assert!(ENGINE_COMMANDS.contains(&cmd));
        }
        for cmd in FRAMEWORK_COMMANDS
            .iter()
            .chain(HOST_PYTHON_COMMANDS.iter())
            .chain(HOST_HANDLED_COMMANDS.iter())
            .chain(HOST_COMMANDS.iter())
            .chain(ENGINE_COMMANDS.iter())
        {
            assert_ne!(route_command(cmd), Route::Unknown, "{cmd} 应在命令目录内");
        }
        // 宿主窗口族：骨架期 CAPABILITY_DENIED
        for cmd in HOST_COMMANDS {
            assert_eq!(route_command(cmd), Route::HostHandled, "{cmd} 应为宿主窗口族");
        }
        // Python 实现的宿主资源命令：转发 Python
        for cmd in HOST_PYTHON_COMMANDS {
            assert_eq!(route_command(cmd), Route::ForwardPython, "{cmd} 应转发 Python");
        }
        // TASK-008 框选：Rust 本地处理
        for cmd in HOST_HANDLED_COMMANDS {
            assert_eq!(route_command(cmd), Route::HostHandled, "{cmd} 应由 Rust 处理");
        }
        // 框架 + 引擎业务全部转发 Python
        for cmd in FRAMEWORK_COMMANDS.iter().chain(ENGINE_COMMANDS.iter()) {
            assert_eq!(route_command(cmd), Route::ForwardPython, "{cmd} 应转发 Python");
        }
        // 表外命令不出壳
        assert_eq!(route_command("make_coffee"), Route::Unknown);
        assert_eq!(route_command(""), Route::Unknown);
        assert_eq!(route_command("set_region_limit"), Route::Unknown); // 已移除的旧命令
    }

    #[test]
    fn selection_commands_route_to_host_handled() {
        // TASK-008：框选三命令由 Rust 处理（非 ForwardPython、非 HostDenied）
        for cmd in ["start_selection_mode", "confirm_selection", "stop_selection_mode"] {
            assert_eq!(route_command(cmd), Route::HostHandled, "{cmd}");
            assert!(!HOST_COMMANDS.contains(&cmd), "{cmd} 不应残留宿主窗口族");
            assert!(!HOST_PYTHON_COMMANDS.contains(&cmd), "{cmd} 不在 Python 表");
        }
    }

    #[test]
    fn python_hosted_resource_commands_forward_python() {
        // TASK-008 修正：TASK-007/009 已实现的资源命令必须转发 Python，
        // 否则经 UI 的端到端路径会错误返回 CAPABILITY_DENIED
        for cmd in [
            "import_image",
            "get_display_list",
            "capture_region",
            "capture_full_screen",
            "capture_window",
            "release_image",
            "get_image_preview",
        ] {
            assert_eq!(route_command(cmd), Route::ForwardPython, "{cmd}");
            assert!(!HOST_COMMANDS.contains(&cmd), "{cmd} 不应残留 HostDenied 表");
        }
    }

    #[test]
    fn config_commands_route_forward_python_after_task006() {
        // TASK-006 §2.3：config 三命令转 ForwardPython（原 HostDenied → 演进基线，
        // QA 轮按 invalidations 重验）；普通类（非控制）+ 普通期限 5s
        for cmd in ["get_config", "update_config", "reset_config"] {
            assert_eq!(route_command(cmd), Route::ForwardPython, "{cmd} 应转发 Python");
            assert_eq!(command_class(cmd), CommandClass::Normal, "{cmd} 应为普通类");
            assert!(
                !HOST_COMMANDS.contains(&cmd),
                "{cmd} 不应残留在宿主类清单"
            );
            assert_eq!(deadline_for(cmd), NORMAL_DEADLINE, "{cmd} 取普通 5s 期限");
        }
    }

    #[test]
    fn deadline_is_30s_only_for_hello() {
        assert_eq!(deadline_for("hello"), HELLO_DEADLINE);
        assert_eq!(deadline_for("ping"), NORMAL_DEADLINE);
        assert_eq!(deadline_for("stop_script"), NORMAL_DEADLINE);
    }

    #[test]
    fn ipc_error_constructors_carry_frozen_codes() {
        assert_eq!(IpcError::busy().code, "BUSY");
        assert!(IpcError::busy().retryable);
        assert_eq!(IpcError::conflict().code, "CONFLICT");
        assert_eq!(IpcError::duplicate_expired().code, "DUPLICATE_EXPIRED");
        assert_eq!(IpcError::unknown_command("x").details["command"], "x");
        assert_eq!(IpcError::capability_denied("y").code, "CAPABILITY_DENIED");
        assert!(IpcError::engine_unavailable("e").retryable);
        let t = IpcError::timeout(5000, "ui-1");
        assert_eq!(t.code, "INTERNAL_ERROR");
        assert_eq!(t.message, "请求超时(5000ms)");
        assert_eq!(t.details["request_id"], "ui-1");
        assert_eq!(t.details["deadline_ms"], 5000);
    }

    #[test]
    fn response_envelope_mapping_success_and_error() {
        let mut env = ProtocolEnvelope {
            protocol_version: 1,
            session_id: "session-a".into(),
            msg_type: "response".into(),
            id: Some("ui-1".into()),
            command: None,
            params: None,
            seq: None,
            name: None,
            timestamp: None,
            payload: None,
            success: Some(true),
            data: Some(json!({ "engine_time": "t" })),
            error: None,
        };
        assert_eq!(envelope_response_result(&env).unwrap(), json!({ "engine_time": "t" }));
        env.success = Some(false);
        env.data = None;
        env.error = Some(json!({ "code": "NOT_FOUND", "message": "m", "details": {}, "retryable": false }));
        let err = envelope_response_result(&env).unwrap_err();
        assert_eq!(err.code, "NOT_FOUND");
        assert!(!err.retryable);
        // success=false 且无 error → 内部错误兜底
        env.error = None;
        assert_eq!(envelope_response_result(&env).unwrap_err().code, "INTERNAL_ERROR");
    }

    #[test]
    fn request_envelope_shape_matches_03() {
        let env = request_envelope("session-a", "ui-1", "hello", &json!({}));
        assert_eq!(env["protocol_version"], 1);
        assert_eq!(env["session_id"], "session-a");
        assert_eq!(env["type"], "request");
        assert_eq!(env["id"], "ui-1");
        assert_eq!(env["command"], "hello");
        assert_eq!(env["params"], json!({}));
    }
}
