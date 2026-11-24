//! UI request 处理链（TASK-005 设计 §3.1 dispatcher.rs / §4.2 / §4.3 / §6）。
//!
//! 链路（§3.1）：去重前置判定（重放/CONFLICT/DUPLICATE_EXPIRED/在途挂接）→
//! 在途容量（普通 32 → BUSY；控制类豁免）→ 路由（本地 query_request /
//! 宿主类 CAPABILITY_DENIED / 转发 Python / 表外 UNKNOWN_COMMAND）→ 超时。
//!
//! `query_request` 本地拦截（§3.1：读取缓存/在途状态；03：只查询同会话）——
//! 响应历史唯一载体是 Rust 侧响应缓存，转发 Python 无法回答；此为设计 §5.2
//! 「本地/转发」二分中的本地分支（偏差说明见回报）。
//!
//! `stop_script` 转发前先 `gate.close_gate("stop_script")`（03/06：停止必须先由
//! Rust 关输入闸门；骨架 NullStopGate 调用点，M05 替换，单测断言顺序）。

use std::sync::mpsc::Sender as OneshotSender;

use serde_json::{Value, json};

use crate::sidecar::protocol::ProtocolEnvelope;

use super::Router;
use super::pending::{Dedup, RequestState, Waiter, NORMAL_INFLIGHT_LIMIT, fingerprint};
use super::types::{
    CommandClass, IpcError, Route, REQUEST_ID_MAX_LENGTH, MAX_FRAME_BYTES, command_class,
    envelope_response_result, request_envelope, route_command,
};

/// 转发入站帧（§4.2/§4.3）：response → pending 关联；event/host_request →
/// EventHandler（B3 eventbus/hostbridge）；跨 session 帧 → 丢弃+日志，不计畸形
/// （03：旧会话消息全部拒绝；用户契约③）。
impl Router {
    pub(crate) fn handle_frame(&mut self, envelope: ProtocolEnvelope) {
        if envelope.session_id != self.session_id {
            eprintln!(
                "[ipc] 丢弃跨 session 帧（got={}，want={}）",
                envelope.session_id, self.session_id
            );
            return;
        }
        match envelope.msg_type.as_str() {
            "response" => self.handle_response(envelope),
            "event" | "host_request" => self.events.on_frame(&envelope),
            other => eprintln!("[ipc] 忽略意外帧类型: {other}"),
        }
    }

    /// response 关联（§4.2）：按 id 命中 oneshot → 投递等待者 + 写响应缓存；
    /// 未命中（迟到响应）→ 只更新缓存历史，不投递、不复活执行（03）。
    fn handle_response(&mut self, envelope: ProtocolEnvelope) {
        let Some(id) = envelope.id.clone() else {
            return; // 解析层保证 response 必有 id（B3 扩展必填集），防御性丢弃
        };
        let result = envelope_response_result(&envelope);
        if !self.pending.complete(&id, &result) {
            self.pending.write_cache_only(&id, result);
        }
    }

    // -----------------------------------------------------------------------
    // ipc_request 处理链（§4.2 顺序图逐字）
    // -----------------------------------------------------------------------

    pub(crate) fn handle_ui_request(
        &mut self,
        id: String,
        command: String,
        params: Value,
        reply: Waiter,
    ) {
        // §5.1：id 非空 ≤128 → 否则 INVALID_PARAMS（不进入台账）
        if id.is_empty() || id.len() > REQUEST_ID_MAX_LENGTH {
            send_reply(
                &reply,
                Err(IpcError::invalid_params(
                    "请求 id 必须为非空且不超过 128 字符",
                )),
            );
            return;
        }
        let fp = fingerprint(&command, &params);
        match self.pending.classify(&id, &fp) {
            Dedup::Replay(cached) => send_reply(&reply, cached),
            Dedup::Conflict => send_reply(&reply, Err(IpcError::conflict())),
            Dedup::DuplicateExpired => send_reply(&reply, Err(IpcError::duplicate_expired())),
            Dedup::Join => {
                self.pending.attach(&id, reply);
            }
            Dedup::Admit => self.dispatch_admitted(id, command, params, fp, reply),
        }
    }

    /// 新请求：容量检查 → 路由 → 登记 → 转发/本地终结（§6.2/§5.2）。
    fn dispatch_admitted(
        &mut self,
        id: String,
        command: String,
        params: Value,
        fp: String,
        reply: Waiter,
    ) {
        let class = command_class(&command);
        // §6.2：普通类在途上限 32 → BUSY（不转发、不排队、不登记）；控制类豁免
        if class == CommandClass::Normal && self.pending.normal_inflight() >= NORMAL_INFLIGHT_LIMIT
        {
            send_reply(&reply, Err(IpcError::busy()));
            return;
        }

        // 本地命令：query_request 读取缓存/在途状态（§3.1；03 只查询同会话）。
        // 本地终结同样走台账（去重/缓存语义统一：同 ID 同内容重放查询结果）。
        if command == "query_request" {
            let answer = self.answer_query_local(&params);
            self.pending.admit(&id, &command, fp, class, reply);
            self.pending.complete(&id, &answer);
            return;
        }

        match route_command(&command) {
            Route::Unknown => {
                // 表外命令不出壳（M08）；登记后立即终结 → 最终响应入缓存，
                // 同 ID 同内容重复请求得到一致的重放
                self.pending.admit(&id, &command, fp, class, reply);
                self.pending
                    .complete(&id, &Err(IpcError::unknown_command(&command)));
            }
            Route::HostDenied => {
                // 宿主类骨架期（§5.2）：CAPABILITY_DENIED，不转发 Python
                self.pending.admit(&id, &command, fp, class, reply);
                self.pending
                    .complete(&id, &Err(IpcError::capability_denied(&command)));
            }
            Route::HostHandled => {
                // TASK-008：Rust 本地宿主能力（框选三命令）——同步执行 handler
                // 并回同样信封；不出壳、不转发 Python。
                self.pending.admit(&id, &command, fp, class, reply);
                let result = self.host_commands.dispatch(&command, &params);
                self.pending.complete(&id, &result);
            }
            Route::ForwardPython => {
                self.pending.admit(&id, &command, fp, class, reply);
                // 03/06：停止必须先由 Rust 关输入闸门（闸门先于转发，单测断言顺序）；
                // TASK-017：关闭**该执行**的闸门（从 params 取 execution_id）。
                if command == "stop_script" {
                    let execution_id = params
                        .get("execution_id")
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    self.gate.close_gate_for(execution_id, "stop_script");
                }
                if let Err(err) = self.forward_request(&id, &command, &params) {
                    // 管道断裂（ENGINE_UNAVAILABLE）/发送超限（PROTOCOL_ERROR）：
                    // 该请求立即终结（等待者得到错误；响应缓存照常记录终态）
                    self.pending.complete(&id, &Err(err));
                }
            }
        }
    }

    // -----------------------------------------------------------------------
    // ipc_cancel 处理链（§4.3 顺序图逐字）
    // -----------------------------------------------------------------------

    pub(crate) fn handle_ui_cancel(&mut self, request_id: String, reply: Waiter) {
        // pending 标记 cancelled（后续响应只写缓存不投递）
        let _was_pending = self.pending.mark_cancelled(&request_id);
        // 转发 cancel_request（控制类，不受 32 限制；03：幂等，not_found 亦有响应）
        let cancel_id = format!("cancel-{}", uuid::Uuid::new_v4());
        let params = json!({ "request_id": request_id });
        let fp = fingerprint("cancel_request", &params);
        self.pending
            .admit(&cancel_id, "cancel_request", fp, CommandClass::Control, reply);
        if let Err(err) = self.forward_request(&cancel_id, "cancel_request", &params) {
            self.pending.complete(&cancel_id, &Err(err));
        }
    }

    // -----------------------------------------------------------------------
    // 内部工具
    // -----------------------------------------------------------------------

    /// 构造 request 信封并经单 writer 转发（§6.5：序列化字节 >2MiB →
    /// PROTOCOL_ERROR 拒绝发送；写入失败 = 管道断裂 → ENGINE_UNAVAILABLE）。
    pub(crate) fn forward_request(
        &mut self,
        id: &str,
        command: &str,
        params: &Value,
    ) -> Result<(), IpcError> {
        let envelope = request_envelope(&self.session_id, id, command, params);
        let line_len = serde_json::to_string(&envelope)
            .map(|line| line.len())
            .unwrap_or(usize::MAX);
        if line_len > MAX_FRAME_BYTES {
            return Err(IpcError::protocol_error(&format!(
                "单帧 {line_len} 字节超过 2MiB 上限，拒绝发送"
            )));
        }
        self.sink
            .send_frame(&envelope)
            .map_err(|e| IpcError::engine_unavailable(&format!("stdin 写入失败：{e}")))
    }

    /// query_request 本地应答（03：{status:pending/completed/unknown,
    /// response:object或null}；params 缺 request_id → INVALID_PARAMS）。
    fn answer_query_local(&self, params: &Value) -> Result<Value, IpcError> {
        if let Some(key) = params.as_object().and_then(|object| object.keys().find(|key| *key != "request_id")) {
            return Err(IpcError::invalid_params("未声明的查询字段").with_details(json!({"field": key})));
        }
        let Some(request_id) = params.get("request_id").and_then(Value::as_str) else {
            return Err(IpcError::invalid_params("缺少 request_id（string）").with_details(json!({"field":"request_id"})));
        };
        if request_id.is_empty() {
            return Err(IpcError::invalid_params("request_id 不能为空"));
        }
        match self.pending.query(request_id) {
            RequestState::Pending { .. } => Ok(json!({ "status": "pending", "response": null })),
            RequestState::Completed(Ok(data)) => {
                Ok(json!({ "status": "completed", "response": data }))
            }
            RequestState::Completed(Err(err)) => {
                // 失败终态同样可查（response = 06 四字段错误对象）
                let err_value = serde_json::to_value(&err).unwrap_or(Value::Null);
                Ok(json!({ "status": "completed", "response": err_value }))
            }
            RequestState::Unknown => Ok(json!({ "status": "unknown", "response": null })),
        }
    }
}

/// 回复等待者（oneshot；接收端已放弃时发送失败被忽略）。
pub(crate) fn send_reply(
    reply: &OneshotSender<Result<Value, IpcError>>,
    result: Result<Value, IpcError>,
) {
    let _ = reply.send(result);
}
