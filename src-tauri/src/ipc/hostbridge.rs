//! host_request/host_response 双向桥（TASK-005 设计 §3.1 hostbridge.rs / §4.3）。
//!
//! 职责：
//! - 接收 Python 发起的 `host_request`（03 宿主命令白名单：input_action /
//!   window_snapshot / acquire_input_lease / release_input_lease）；
//! - 查宿主 handler 注册表 → **骨架期注册表为空，白名单内命令一律回
//!   `CAPABILITY_DENIED` 的 host_response**（06：使用未提供的宿主能力），
//!   白名单外回 `UNKNOWN_COMMAND`；M01/M05 落地后注册真 handler，不改结构；
//! - host_request **不进入 UI 命令路由**（03）。
//!
//! 写回路径：host_response 经与 Router 相同的单 writer 通道写 stdin——
//! [`HostBridge`] 持有与 Router 共享的底层 sink 句柄（生产 =
//! `Arc<Mutex<Supervisor>>` 的包装；单 writer 语义由 Supervisor 锁保证）。
//!
//! [`IpcOutlet`] 是 B3 注入 Router 的组合出口（实现 B2 的 `EventHandler`）：
//! event 帧 → [`EventBus`]；host_request 帧 → [`HostBridge`] 应答。

use serde_json::{Value, json};

use crate::sidecar::protocol::{
    self, ProtocolEnvelope, SUPPORTED_PROTOCOL_VERSION,
};

use super::eventbus::EventBus;
use super::HostHandler;
use super::FrameSink;

/// 宿主 handler 注册表：命令名 → 真 handler（M01/M05 接入点）。
///
/// TASK-017：由骨架期 `HashMap<String,bool>` 升级为真 handler 位；未注册命令 →
/// `CAPABILITY_DENIED`（诚实降级）。
#[derive(Default)]
pub struct HostRegistry {
    handlers: std::collections::HashMap<String, Box<dyn HostHandler>>,
}

impl HostRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// 注册宿主 handler（M01/M05 接入点；同命令重复注册覆盖）。
    pub fn register(&mut self, command: &str, handler: Box<dyn HostHandler>) {
        self.handlers.insert(command.to_string(), handler);
    }

    /// 已注册命令数（诊断/测试）。
    pub fn registered(&self, command: &str) -> bool {
        self.handlers.contains_key(command)
    }

    /// 取命令对应 handler（未注册 → None）。
    fn get_mut(&mut self, command: &str) -> Option<&mut Box<dyn HostHandler>> {
        self.handlers.get_mut(command)
    }
}

/// 宿主桥：host_request 应答器（经共享 sink 写回 host_response）。
pub struct HostBridge {
    /// 与 Router 共享的底层传输（生产：Supervisor 单 writer 包装；测试：内存 spy）。
    sink: ArcMutexSink,
    registry: HostRegistry,
}

/// 共享 sink 句柄（跨线程互斥；FrameSink::send_frame 为 &mut self）。
pub type ArcMutexSink = std::sync::Arc<std::sync::Mutex<Box<dyn FrameSink>>>;

impl HostBridge {
    pub fn new(sink: ArcMutexSink) -> Self {
        HostBridge { sink, registry: HostRegistry::new() }
    }

    /// 注册真宿主 handler（M05 接入点；wire 装配时调用）。
    pub fn register_handler(&mut self, command: &str, handler: Box<dyn HostHandler>) {
        self.registry.register(command, handler);
    }

    /// 应答一条 host_request（§4.3 顺序图）：
    /// - 白名单内 + 已注册真 handler → 调用 handler，回 `data`/`error`（06 四字段）；
    /// - 白名单内 + 未注册 → `CAPABILITY_DENIED`（06：宿主能力未提供）；
    /// - 白名单外 → `UNKNOWN_COMMAND`（03：host_request 不进 UI 路由；
    ///   M08 只路由已注册命令）。
    /// 应答失败（stdin 断裂）只记日志——写失败即管道断裂，EOF 清理路径接管。
    pub fn respond(&mut self, request: &ProtocolEnvelope) {
        let Some(id) = request.id.as_deref() else {
            return; // 解析层保证 host_request 必有 id；防御性忽略
        };
        let Some(command) = request.command.as_deref() else {
            // 解析层保证 host_request 必有 command；防御性：按 UNKNOWN_COMMAND 应答
            self.send_reply(host_response_envelope(
                &request.session_id,
                id,
                false,
                None,
                Some(error_object("UNKNOWN_COMMAND", "host_request 缺少 command")),
            ));
            return;
        };

        if !protocol::is_host_request_command(command) {
            self.send_reply(host_response_envelope(
                &request.session_id,
                id,
                false,
                None,
                Some(error_object("UNKNOWN_COMMAND", "host 命令不在宿主白名单内")),
            ));
            return;
        }

        let params = request.params.clone().unwrap_or(Value::Null);
        let reply = match self.registry.get_mut(command) {
            Some(handler) => match handler.handle(command, &params) {
                Ok(data) => host_response_envelope(&request.session_id, id, true, Some(data), None),
                Err(err) => host_response_envelope(
                    &request.session_id,
                    id,
                    false,
                    None,
                    Some(serde_json::to_value(&err).unwrap_or_else(|_| {
                        error_object("INTERNAL_ERROR", "host handler 错误序列化失败")
                    })),
                ),
            },
            None => host_response_envelope(
                &request.session_id,
                id,
                false,
                None,
                Some(error_object("CAPABILITY_DENIED", "宿主能力未提供")),
            ),
        };
        self.send_reply(reply);
    }

    fn send_reply(&mut self, reply: Value) {
        if let Err(e) = self.sink.lock().unwrap_or_else(|p| p.into_inner()).send_frame(&reply) {
            eprintln!("[ipc] host_response 写入失败（管道断裂，EOF 清理路径接管）: {e}");
        }
    }
}

/// host_response 信封构造（03 消息封装逐字：success=false + error 四字段）。
fn host_response_envelope(
    session_id: &str,
    id: &str,
    success: bool,
    data: Option<Value>,
    error: Option<Value>,
) -> Value {
    let mut envelope = json!({
        "protocol_version": SUPPORTED_PROTOCOL_VERSION,
        "session_id": session_id,
        "type": "host_response",
        "id": id,
        "success": success,
    });
    let obj = envelope.as_object_mut().expect("host_response 信封为 object");
    if let Some(data) = data {
        obj.insert("data".to_string(), data);
    } else if let Some(error) = error {
        obj.insert("error".to_string(), error);
    }
    envelope
}

/// 06 错误四字段对象。
fn error_object(code: &str, message: &str) -> Value {
    json!({ "code": code, "message": message, "details": Value::Null, "retryable": false })
}

// ---------------------------------------------------------------------------
// 组合出口：注入 Router 的 EventHandler 实现（B2 预留投递位的具体化）
// ---------------------------------------------------------------------------

/// Router 的事件/host_request 出口：event → EventBus（契约④统一事件总线，
/// system_ready 不再直转）；host_request → HostBridge 应答。
pub struct IpcOutlet {
    bus: EventBus,
    bridge: HostBridge,
}

impl IpcOutlet {
    pub fn new(bus: EventBus, bridge: HostBridge) -> Self {
        IpcOutlet { bus, bridge }
    }

    /// 会话重建（引擎重启时由接线层调用）：seq 归零、清空合并槽。
    pub fn reset_session(&mut self, session_id: &str) {
        self.bus.reset_session(session_id);
    }
}

impl super::EventHandler for IpcOutlet {
    fn on_frame(&mut self, envelope: &ProtocolEnvelope) {
        match envelope.msg_type.as_str() {
            "event" => self.bus.on_event(envelope),
            "host_request" => self.bridge.respond(envelope),
            other => eprintln!("[ipc] 出口收到意外帧类型: {other}"),
        }
    }
}

// ---------------------------------------------------------------------------
// 单元测试（内存 spy sink 捕获 host_response；EventBus 侧见 eventbus.rs）
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    /// 内存 FrameSink spy：捕获全部写出帧。
    struct SinkSpy {
        frames: std::sync::Arc<std::sync::Mutex<Vec<Value>>>,
    }

    impl FrameSink for SinkSpy {
        fn send_frame(&mut self, envelope: &Value) -> Result<(), String> {
            self.frames.lock().unwrap().push(envelope.clone());
            Ok(())
        }
    }

    fn spy() -> (ArcMutexSink, std::sync::Arc<std::sync::Mutex<Vec<Value>>>) {
        let frames = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let sink: ArcMutexSink = std::sync::Arc::new(std::sync::Mutex::new(Box::new(SinkSpy {
            frames: Arc::clone(&frames),
        })));
        (sink, frames)
    }

    fn host_request(id: &str, command: &str) -> ProtocolEnvelope {
        ProtocolEnvelope {
            protocol_version: 1,
            session_id: "session-hb".into(),
            msg_type: "host_request".into(),
            id: Some(id.to_string()),
            command: Some(command.to_string()),
            params: Some(json!({})),
            seq: None,
            name: None,
            timestamp: None,
            payload: None,
            success: None,
            data: None,
            error: None,
        }
    }

    #[test]
    fn host_request_whitelisted_answers_capability_denied() {
        let (sink, frames) = spy();
        let mut bridge = HostBridge::new(sink);
        // 白名单内（input_action）+ 注册表空 → CAPABILITY_DENIED（骨架常态）
        bridge.respond(&host_request("py-1", "input_action"));
        let frames = frames.lock().unwrap();
        assert_eq!(frames.len(), 1);
        let reply = &frames[0];
        assert_eq!(reply["type"], "host_response");
        assert_eq!(reply["id"], "py-1");
        assert_eq!(reply["session_id"], "session-hb");
        assert_eq!(reply["success"], false);
        assert_eq!(reply["error"]["code"], "CAPABILITY_DENIED");
        assert_eq!(reply["error"]["retryable"], false);
    }

    #[test]
    fn host_request_outside_whitelist_answers_unknown_command() {
        let (sink, frames) = spy();
        let mut bridge = HostBridge::new(sink);
        bridge.respond(&host_request("py-2", "make_coffee"));
        let frames = frames.lock().unwrap();
        assert_eq!(frames[0]["error"]["code"], "UNKNOWN_COMMAND");
    }

    #[test]
    fn host_response_envelope_shape_matches_03() {
        let v = host_response_envelope(
            "session-hb",
            "py-1",
            true,
            Some(json!({ "ok": true })),
            None,
        );
        assert_eq!(v["protocol_version"], 1);
        assert_eq!(v["type"], "host_response");
        assert_eq!(v["id"], "py-1");
        assert_eq!(v["success"], true);
        assert_eq!(v["data"]["ok"], true);
        assert!(v.get("error").is_none(), "data/error 互斥必有一");
    }

    /// TASK-017：注册真 handler 后命中即回 data（不再 INTERNAL_ERROR）。
    #[test]
    fn registered_handler_returns_data() {
        struct EchoHandler;
        impl HostHandler for EchoHandler {
            fn handle(&mut self, command: &str, params: &Value) -> Result<Value, crate::ipc::IpcError> {
                Ok(json!({ "command": command, "echo": params }))
            }
        }
        let (sink, frames) = spy();
        let mut bridge = HostBridge::new(sink);
        bridge.register_handler("input_action", Box::new(EchoHandler));
        bridge.respond(&host_request("py-9", "input_action"));
        let frames = frames.lock().unwrap();
        assert_eq!(frames[0]["success"], true);
        assert_eq!(frames[0]["data"]["command"], "input_action");
    }

    /// TASK-017：真 handler 报错 → host_response 透出 06 四字段错误。
    #[test]
    fn registered_handler_error_is_propagated() {
        struct DenyHandler;
        impl HostHandler for DenyHandler {
            fn handle(&mut self, _command: &str, _params: &Value) -> Result<Value, crate::ipc::IpcError> {
                Err(crate::ipc::IpcError::invalid_state("闸门已关"))
            }
        }
        let (sink, frames) = spy();
        let mut bridge = HostBridge::new(sink);
        bridge.register_handler("acquire_input_lease", Box::new(DenyHandler));
        bridge.respond(&host_request("py-10", "acquire_input_lease"));
        let frames = frames.lock().unwrap();
        assert_eq!(frames[0]["success"], false);
        assert_eq!(frames[0]["error"]["code"], "INVALID_STATE");
    }
}
