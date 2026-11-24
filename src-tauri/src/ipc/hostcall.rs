//! 同步宿主→引擎内部调用（DEF-TASK008-01，方向 A 支撑设施）。
//!
//! **问题**：`confirm_selection` / `selection_report_region` 在 **Router 单线程**内
//! 同步执行（[`crate::ipc::HostCommandHandler`]），若在 `dispatch` 内部同步等待一条
//! Python 往返响应，则该响应只能由同一 Router 线程经 `handle_frame` 处理 →
//! **自锁死**。这正是 TASK-008 当初在宿主进程内用 GDI + `HostImageStore` 直接造
//! `sel-img-` 引用、从而破坏 03 公开契约（`get_image_preview`/`release_image` 只认
//! 引擎 `img-`）的根因（DEF-TASK008-01）。
//!
//! **本模块**：提供一条**受控的同步内部调用**通道——宿主可向引擎发一条内部命令并
//! 阻塞等待其响应，而**不经过 Router 的 pending/关联**。关键：响应帧由 **stdout
//! drain 线程**（`frame_bridge_loop`）在**转发 Router 之前**先行投递到本通道的等待者
//! （[`deliver`]），因此即使 Router 线程正阻塞在命令 handler 内也能收到响应，
//! **不自锁**。
//!
//! 边界：
//! - 仅用于**引擎内部约定命令**（当前：`register_host_frame` / 内部 `release_image`
//!   收尾）；**不改 03 公开命令目录**（对外仍 62 条）；
//! - 单帧仍受 2MiB 护栏（[`crate::ipc::types::MAX_FRAME_BYTES`]）；
//! - 期限取普通命令 5s；超时/管道断裂 → 诚实失败（INTERNAL_ERROR/ENGINE_UNAVAILABLE），
//!   绝不伪造成功。
//!
//! 未装配（引擎未启动）时 [`shared`] 返回 None，调用方据此诚实拒绝。

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::Value;

use crate::sidecar::protocol::ProtocolEnvelope;

use super::FrameSink;
use super::types::{
    IpcError, MAX_FRAME_BYTES, NORMAL_DEADLINE, envelope_response_result, request_envelope,
};

/// 等待者：per-call oneshot（承载关联后的结果）。
type Waiter = mpsc::Sender<Result<Value, IpcError>>;

/// 内部调用 id 前缀（与 Router/UI 请求 id 空间隔离，避免误关联）。
const HOSTCALL_ID_PREFIX: &str = "hostcall-";

/// 同步宿主→引擎调用客户端（线程安全；进程内单例经 [`install`] 装配）。
pub struct HostCallClient {
    /// 在途调用关联表：id → 等待者（drain 线程按 id 投递）。
    registry: Mutex<HashMap<String, Waiter>>,
    /// 单 writer（与 Router 共用 Supervisor 单 writer；写入天然串行）。
    sink: Mutex<Box<dyn FrameSink>>,
    /// 当前会话 id（信封必填；引擎重启经 [`Self::set_session`] 更新）。
    session_id: Mutex<String>,
    /// 调用序号（生成唯一 id）。
    seq: AtomicU64,
    /// 单次调用等待期限（普通命令 5s）。
    deadline: Duration,
}

impl HostCallClient {
    /// 以单 writer + 当前会话构造。
    pub fn new(sink: Box<dyn FrameSink>, session_id: String) -> Self {
        HostCallClient {
            registry: Mutex::new(HashMap::new()),
            sink: Mutex::new(sink),
            session_id: Mutex::new(session_id),
            seq: AtomicU64::new(0),
            deadline: NORMAL_DEADLINE,
        }
    }

    /// 更新当前会话 id（引擎重启/会话重建）。
    pub fn set_session(&self, session_id: String) {
        *self.session_id.lock().unwrap_or_else(|p| p.into_inner()) = session_id;
    }

    /// 当前会话 id（供 token 绑定）。
    pub fn session_id(&self) -> String {
        self.session_id
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .clone()
    }

    /// 在途调用数（诊断/测试）。
    pub fn inflight(&self) -> usize {
        self.registry.lock().unwrap_or_else(|p| p.into_inner()).len()
    }

    fn next_id(&self) -> String {
        let n = self.seq.fetch_add(1, Ordering::Relaxed) + 1;
        format!("{HOSTCALL_ID_PREFIX}{n}-{}", uuid::Uuid::new_v4())
    }

    /// 同步调用一条引擎内部命令；返回响应 `data`（失败为 06 四字段错误）。
    ///
    /// **注意**：会阻塞调用线程至响应到达或超时；仅由**非 drain 线程**调用
    /// （Router 线程 / Tauri 命令线程均可——响应由 drain 线程并行接收）。
    pub fn call(&self, command: &str, params: Value) -> Result<Value, IpcError> {
        let id = self.next_id();
        let (tx, rx) = mpsc::channel::<Result<Value, IpcError>>();
        self.registry
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .insert(id.clone(), tx);

        let envelope = request_envelope(&self.session_id(), &id, command, &params);
        // 2MiB 护栏（send 前测序列化字节；超限拒绝发送，诚实报错）。
        let line_len = serde_json::to_string(&envelope)
            .map(|line| line.len())
            .unwrap_or(usize::MAX);
        if line_len > MAX_FRAME_BYTES {
            self.remove_waiter(&id);
            return Err(IpcError::protocol_error(&format!(
                "内部调用单帧 {line_len} 字节超过 2MiB 上限，拒绝发送"
            )));
        }

        // 写请求：先持 sink 锁写出并立即释放，再阻塞等待——避免 drain 线程
        // 因抢不到 Supervisor 锁而无法投递（写锁期间不阻塞等待）。
        let send_result = {
            let mut sink = self.sink.lock().unwrap_or_else(|p| p.into_inner());
            sink.send_frame(&envelope)
        };
        if let Err(e) = send_result {
            self.remove_waiter(&id);
            return Err(IpcError::engine_unavailable(&format!(
                "内部调用写入 stdin 失败：{e}"
            )));
        }

        match rx.recv_timeout(self.deadline) {
            Ok(result) => result,
            Err(_) => {
                self.remove_waiter(&id);
                Err(IpcError::timeout(self.deadline.as_millis() as u64, &id))
            }
        }
    }

    fn remove_waiter(&self, id: &str) -> Option<Waiter> {
        self.registry
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .remove(id)
    }

    /// drain 线程调用：若入站帧是本通道的响应且 id 在途，投递给等待者并返回 true
    /// （表示已消化，**不应**再转发 Router）。否则返回 false（交给 Router 常规处理）。
    pub fn try_deliver(&self, envelope: &ProtocolEnvelope) -> bool {
        if envelope.msg_type != "response" {
            return false;
        }
        let Some(id) = envelope.id.clone() else {
            return false;
        };
        if !id.starts_with(HOSTCALL_ID_PREFIX) {
            return false; // 快速路径：非本通道 id
        }
        let Some(waiter) = self.remove_waiter(&id) else {
            return false; // 非本通道（或已超时移除）→ 交 Router
        };
        let result = envelope_response_result(envelope);
        let _ = waiter.send(result);
        true
    }
}

// ---------------------------------------------------------------------------
// 进程级单例（wire_router 装配；未装配 → None）
// ---------------------------------------------------------------------------

static HOSTCALL: Mutex<Option<Arc<HostCallClient>>> = Mutex::new(None);

/// 安装/替换当前会话的同步调用客户端（引擎重启时替换）。
pub fn install(client: Arc<HostCallClient>) {
    *HOSTCALL.lock().unwrap_or_else(|p| p.into_inner()) = Some(client);
}

/// 卸载（会话清理；未装配为 no-op）。
pub fn uninstall() {
    *HOSTCALL.lock().unwrap_or_else(|p| p.into_inner()) = None;
}

/// 读取当前会话的同步调用客户端（未装配 → None）。
pub fn shared() -> Option<Arc<HostCallClient>> {
    HOSTCALL
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .clone()
}

/// drain 线程入口：把响应帧先行投递给同步调用等待者；命中返回 true。
pub fn deliver(envelope: &ProtocolEnvelope) -> bool {
    match shared() {
        Some(client) => client.try_deliver(envelope),
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::Receiver;

    /// 内存 sink：记录写出的帧，便于测试断言。
    struct MemSink {
        sent: mpsc::Sender<Value>,
    }

    impl FrameSink for MemSink {
        fn send_frame(&mut self, envelope: &Value) -> Result<(), String> {
            let _ = self.sent.send(envelope.clone());
            Ok(())
        }
    }

    fn response_envelope(id: &str, data: Value) -> ProtocolEnvelope {
        ProtocolEnvelope {
            protocol_version: 1,
            session_id: "s1".to_string(),
            msg_type: "response".to_string(),
            id: Some(id.to_string()),
            command: None,
            params: None,
            seq: None,
            name: None,
            timestamp: None,
            payload: None,
            success: Some(true),
            data: Some(data),
            error: None,
        }
    }

    #[test]
    fn call_blocks_until_response_delivered_by_drain_thread() {
        let (sent_tx, sent_rx): (mpsc::Sender<Value>, Receiver<Value>) = mpsc::channel();
        let client = Arc::new(HostCallClient::new(
            Box::new(MemSink { sent: sent_tx }),
            "s1".to_string(),
        ));

        // 后台线程扮演 drain：收到请求后回响应。
        let client_for_reply = Arc::clone(&client);
        let reply_thread = std::thread::spawn(move || {
            let request = sent_rx.recv().expect("应写出请求");
            assert_eq!(request["command"], "register_host_frame");
            let id = request["id"].as_str().unwrap().to_string();
            let env = response_envelope(&id, serde_json::json!({"image": {"image_id": "img-1"}}));
            assert!(client_for_reply.try_deliver(&env), "响应应被本通道消化");
        });

        let data = client
            .call("register_host_frame", serde_json::json!({"token": "t"}))
            .expect("同步调用应成功");
        reply_thread.join().unwrap();
        assert_eq!(data["image"]["image_id"], "img-1");
        assert_eq!(client.inflight(), 0, "调用完成后在途应清空");
    }

    #[test]
    fn non_hostcall_response_is_not_consumed() {
        let (sent_tx, _sent_rx) = mpsc::channel();
        let client = HostCallClient::new(Box::new(MemSink { sent: sent_tx }), "s1".to_string());
        // 非本通道 id → 不消化（交 Router）
        let env = response_envelope("ui-1", serde_json::json!({}));
        assert!(!client.try_deliver(&env));
        // event 帧 → 不消化
        let mut ev = response_envelope("hostcall-9-x", serde_json::json!({}));
        ev.msg_type = "event".to_string();
        assert!(!client.try_deliver(&ev));
    }

    #[test]
    fn install_and_shared_roundtrip() {
        let (sent_tx, _rx) = mpsc::channel();
        let client = Arc::new(HostCallClient::new(Box::new(MemSink { sent: sent_tx }), "s1".to_string()));
        install(Arc::clone(&client));
        assert!(shared().is_some());
        assert_eq!(shared().unwrap().session_id(), "s1");
        assert!(!deliver(&response_envelope("nope", serde_json::json!({}))));
        uninstall();
        assert!(shared().is_none());
    }
}
