//! 进程内契约测试（TASK-005 设计 §8.1，批次 B2 子集：去重/在途/超时/路由）。
//!
//! 原则：不 spawn 真进程——内存 transport 替身（[`SinkSpy`]）+ 注入时钟
//! （[`FakeClock`]+`wake_event` 唤醒 Router 事件循环）+ 闸门 spy（[`GateSpy`]，
//! 与 sink 共享同一时序日志断言 gate-first 顺序）。协议语义（关联/去重/背压/
//! seq）由本文件证明；真实 OS 行为（EOF/编码/管道缓冲/死锁）归集成测试。
//!
//! 阈值与用例定义与 `schema/contract-cases.json` 共享（B1 产出）：
//! `shared_thresholds_match_contract_cases` 直接读取该文件断言常量一致。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use super::pending::{Clock, NORMAL_INFLIGHT_LIMIT, RESPONSE_CACHE_CAPACITY, SEEN_SET_CAPACITY};
use super::types::{CommandClass, IpcError, RouterInput, MAX_FRAME_BYTES, NORMAL_DEADLINE, HELLO_DEADLINE, command_class};
use super::{FrameSink, NullEventHandler, Router, RouterHandle, StopGate};
use crate::sidecar::protocol::ProtocolEnvelope;

/// 契约用例固定会话 id（与 schema/contract-cases.json 一致）。
const SESSION: &str = "session-contract-1";

// ---------------------------------------------------------------------------
// 测试替身
// ---------------------------------------------------------------------------

/// 偏移注入时钟（now() = Instant::now() + offset；advance 推进 fake 时间）。
#[derive(Clone, Default)]
struct FakeClock {
    offset: Arc<Mutex<Duration>>,
}

impl FakeClock {
    fn advance(&self, d: Duration) {
        *self.offset.lock().unwrap() += d;
    }
}

impl Clock for FakeClock {
    fn now(&self) -> Instant {
        Instant::now() + *self.offset.lock().unwrap()
    }
}

/// 时序日志（gate spy 与 sink spy 共享，断言 gate-first 顺序）。
#[derive(Clone, Default)]
struct SeqLog(Arc<Mutex<Vec<String>>>);

impl SeqLog {
    fn push(&self, entry: String) {
        self.0.lock().unwrap().push(entry);
    }

    fn snapshot(&self) -> Vec<String> {
        self.0.lock().unwrap().clone()
    }
}

/// 内存 FrameSink 替身：记录每次转发的请求 id；`fail` 置位模拟管道断裂。
#[derive(Clone, Default)]
struct SinkSpy {
    sent: Arc<Mutex<Vec<String>>>,
    fail: Arc<AtomicBool>,
    log: SeqLog,
}

impl SinkSpy {
    fn sent_len(&self) -> usize {
        self.sent.lock().unwrap().len()
    }

    fn sent_ids(&self) -> Vec<String> {
        self.sent.lock().unwrap().clone()
    }
}

impl FrameSink for SinkSpy {
    fn send_frame(&mut self, envelope: &serde_json::Value) -> Result<(), String> {
        if self.fail.load(Ordering::SeqCst) {
            return Err("管道断裂（测试注入）".to_string());
        }
        let id = envelope
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("?")
            .to_string();
        self.sent.lock().unwrap().push(id.clone());
        self.log.push(format!("send:{id}"));
        Ok(())
    }
}

/// 输入闸门 spy：close_gate 调用记入共享时序日志。
#[derive(Clone, Default)]
struct GateSpy {
    log: SeqLog,
}

impl StopGate for GateSpy {
    fn close_gate(&self, reason: &str) {
        self.log.push(format!("gate:{reason}"));
    }
}

// ---------------------------------------------------------------------------
// 测试装配
// ---------------------------------------------------------------------------

struct Harness {
    /// 直接投递 RouterInput 的通道（喂帧/裸请求用）。
    tx: mpsc::Sender<RouterInput>,
    /// RouterHandle（与宿主命令共用请求入口）。
    handle: RouterHandle,
    sink: SinkSpy,
    log: SeqLog,
    clock: FakeClock,
    _join: JoinHandle<()>,
}

fn setup() -> Harness {
    let (tx, rx) = mpsc::channel::<RouterInput>();
    let clock = FakeClock::default();
    let log = SeqLog::default();
    let sink = SinkSpy {
        sent: Arc::new(Mutex::new(Vec::new())),
        fail: Arc::new(AtomicBool::new(false)),
        log: log.clone(),
    };
    let mut router = Router::new(
        SESSION,
        Box::new(sink.clone()),
        Box::new(GateSpy { log: log.clone() }),
        Box::new(clock.clone()),
        Box::new(NullEventHandler),
    );
    let join = std::thread::spawn(move || router.run(rx));
    Harness {
        handle: RouterHandle::new(tx.clone()),
        tx,
        sink,
        log,
        clock,
        _join: join,
    }
}

/// 发送裸 UI 请求（不等待），返回该请求的 oneshot 接收端。
fn send_raw(h: &Harness, id: &str, command: &str, params: Value) -> mpsc::Receiver<Result<Value, IpcError>> {
    let (reply, rx) = mpsc::channel();
    h.tx
        .send(RouterInput::UiRequest {
            id: id.to_string(),
            command: command.to_string(),
            params,
            reply,
        })
        .expect("router 通道存活");
    rx
}

/// 发送裸 UiCancel（不等待），返回 oneshot 接收端。
fn send_cancel_raw(h: &Harness, request_id: &str) -> mpsc::Receiver<Result<Value, IpcError>> {
    let (reply, rx) = mpsc::channel();
    h.tx
        .send(RouterInput::UiCancel {
            request_id: request_id.to_string(),
            reply,
        })
        .expect("router 通道存活");
    rx
}

/// 喂一条入站帧（经 RouterInput::Frame，drain 线程替身路径）。
fn feed_frame(h: &Harness, envelope: ProtocolEnvelope) {
    h.tx.send(RouterInput::Frame(envelope)).expect("router 通道存活");
}

/// 轮询等待条件成立（Router 线程异步处理的同步栅栏）。
fn wait_until(mut cond: impl FnMut() -> bool, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if cond() {
            return;
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    assert!(cond(), "等待条件超时（{timeout:?}）");
}

/// 等待指定 id 已被转发（保证 Router 已完成该请求的 admit+forward 处理）。
fn wait_forwarded(h: &Harness, id: &str) {
    wait_until(
        || h.sink.sent_ids().iter().any(|x| x == id),
        Duration::from_secs(2),
    );
}

fn recv_ok(rx: &mpsc::Receiver<Result<Value, IpcError>>) -> Value {
    match rx.recv_timeout(Duration::from_secs(2)).expect("等待回复超时") {
        Ok(v) => v,
        Err(e) => panic!("期望成功回复，得到错误 {e:?}"),
    }
}

fn recv_err(rx: &mpsc::Receiver<Result<Value, IpcError>>) -> IpcError {
    match rx.recv_timeout(Duration::from_secs(2)).expect("等待回复超时") {
        Ok(v) => panic!("期望错误回复，得到成功 {v:?}"),
        Err(e) => e,
    }
}

// ---- 信封构造（response 必填集：id/success/data|error）----

fn ok_response(id: &str, data: Value) -> ProtocolEnvelope {
    ProtocolEnvelope {
        protocol_version: 1,
        session_id: SESSION.to_string(),
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

fn err_response(id: &str, code: &str) -> ProtocolEnvelope {
    ProtocolEnvelope {
        protocol_version: 1,
        session_id: SESSION.to_string(),
        msg_type: "response".to_string(),
        id: Some(id.to_string()),
        command: None,
        params: None,
        seq: None,
        name: None,
        timestamp: None,
        payload: None,
        success: Some(false),
        data: None,
        error: Some(json!({ "code": code, "message": "x", "details": null, "retryable": false })),
    }
}

/// 唤醒事件帧（Router 事件循环的 fake 时钟推进同步栅栏；经 EventHandler 丢弃）。
fn wake_event() -> ProtocolEnvelope {
    ProtocolEnvelope {
        protocol_version: 1,
        session_id: SESSION.to_string(),
        msg_type: "event".to_string(),
        id: None,
        command: None,
        params: None,
        seq: Some(1),
        name: Some("script_iteration".to_string()),
        timestamp: Some("2026-10-09T12:00:00.000Z".to_string()),
        payload: None,
        success: None,
        data: None,
        error: None,
    }
}

// ---------------------------------------------------------------------------
// 契约用例（contract-cases.json B2 applicable 子集 + 路由/本地查询补充）
// ---------------------------------------------------------------------------

/// thresholds 共享：本实现常量与 schema/contract-cases.json（B1 产出）逐项一致。
#[test]
fn shared_thresholds_match_contract_cases() {
    let raw = include_str!("../../../../schema/contract-cases.json");
    let doc: Value = serde_json::from_str(raw).expect("contract-cases.json 应为合法 JSON");
    let t = &doc["thresholds"];
    assert_eq!(t["protocolVersion"].as_u64(), Some(1));
    assert_eq!(t["maxFrameBytes"].as_u64(), Some(MAX_FRAME_BYTES as u64));
    assert_eq!(
        t["dedupResponseCache"].as_u64(),
        Some(RESPONSE_CACHE_CAPACITY as u64)
    );
    assert_eq!(
        t["seenIdSetCapacity"].as_u64(),
        Some(SEEN_SET_CAPACITY as u64)
    );
    assert_eq!(
        t["normalInflightLimit"].as_u64(),
        Some(NORMAL_INFLIGHT_LIMIT as u64)
    );
    assert_eq!(t["normalDeadlineMs"].as_u64(), Some(NORMAL_DEADLINE.as_millis() as u64));
    assert_eq!(t["startupDeadlineMs"].as_u64(), Some(HELLO_DEADLINE.as_millis() as u64));
    assert_eq!(t["consecutiveMalformedLimit"].as_u64(), Some(10));
    // 控制类清单（control-class-commands 用例）与本实现一致
    for cmd in ["stop_script", "cancel_request", "cancel_job", "ping", "query_request"] {
        assert_eq!(command_class(cmd), CommandClass::Control, "{cmd} 应为控制类");
    }
    // 可合并事件清单（B3 eventbus 输入参数，知悉断言）
    assert_eq!(t["mergeableEvents"][0].as_str(), Some("selection_updated"));
    assert_eq!(t["mergeableEvents"][1].as_str(), Some("recognition_stats"));
}

/// 乱序响应按 id 关联（03：乱序按 id 关联；不允许"发请求后直接 readline 等一条"）。
#[test]
fn out_of_order_responses_correlate_by_id() {
    let h = setup();
    let r1 = send_raw(&h, "ui-1", "get_job", json!({ "job_id": "a" }));
    let r2 = send_raw(&h, "ui-2", "get_job", json!({ "job_id": "b" }));
    // 故意后发 ui-1 的响应（ui-2 先回）
    feed_frame(&h, ok_response("ui-2", json!({ "job_id": "b", "status": "x" })));
    feed_frame(&h, ok_response("ui-1", json!({ "job_id": "a", "status": "y" })));
    assert_eq!(recv_ok(&r2)["status"], "x", "ui-2 先回先达");
    assert_eq!(recv_ok(&r1)["status"], "y", "ui-1 乱序后仍按 id 关联");
    assert_eq!(h.sink.sent_len(), 2);
}

/// dedup-same-content-replay：同 ID 同内容重放缓存响应，不重新执行（03）。
#[test]
fn dedup_same_content_replays_cached_response() {
    let h = setup();
    let r1 = send_raw(&h, "ui-1", "execute_script", json!({ "script_id": "s1" }));
    wait_forwarded(&h, "ui-1");
    feed_frame(&h, ok_response("ui-1", json!({ "execution_id": "e1", "status": "starting" })));
    let first = recv_ok(&r1);

    // 重放：不再转发（forwardCount=1），返回已有响应
    let r2 = send_raw(&h, "ui-1", "execute_script", json!({ "script_id": "s1" }));
    let replay = recv_ok(&r2);
    assert_eq!(first, replay);
    assert_eq!(h.sink.sent_len(), 1, "重复请求不得二次转发（不重做副作用）");
}

/// dedup-different-content-conflict：同 ID 不同内容 → CONFLICT（03）。
#[test]
fn dedup_different_content_conflict() {
    let h = setup();
    let r1 = send_raw(&h, "ui-1", "get_job", json!({ "job_id": "a" }));
    wait_forwarded(&h, "ui-1");
    feed_frame(&h, ok_response("ui-1", json!({ "job_id": "a" })));
    let _ = recv_ok(&r1);

    let r2 = send_raw(&h, "ui-1", "get_job", json!({ "job_id": "DIFFERENT" }));
    let err = recv_err(&r2);
    assert_eq!(err.code, "CONFLICT");
    assert!(!err.retryable);
    assert_eq!(h.sink.sent_len(), 1, "CONFLICT 不转发");
}

/// 在途挂接（§4.2）：同 ID 同内容在途重复请求共享同一响应，只转发一次。
#[test]
fn inflight_same_content_joins_shared_waiter() {
    let h = setup();
    let r1 = send_raw(&h, "ui-1", "get_job", json!({ "job_id": "a" }));
    let r2 = send_raw(&h, "ui-1", "get_job", json!({ "job_id": "a" }));
    wait_forwarded(&h, "ui-1");
    assert_eq!(h.sink.sent_len(), 1, "在途重复请求只转发一次");
    feed_frame(&h, ok_response("ui-1", json!({ "job_id": "a", "status": "running" })));
    assert_eq!(recv_ok(&r1), recv_ok(&r2), "两个等待者共享同一响应");
}

/// dedup-expired-id：响应缓存 10000 淘汰后再达 → DUPLICATE_EXPIRED（03/R08）。
#[test]
fn cache_eviction_yields_duplicate_expired() {
    let h = setup();
    // 10001 个顺序请求-响应：第 10001 个把 ui-0 挤出 LRU
    for i in 0..=RESPONSE_CACHE_CAPACITY {
        let id = format!("ui-{i}");
        let rx = send_raw(&h, &id, "get_job", json!({ "job_id": i }));
        feed_frame(&h, ok_response(&id, json!({ "job_id": i })));
        assert_eq!(recv_ok(&rx)["job_id"], i);
    }
    // 已淘汰 id 再达 → DUPLICATE_EXPIRED（无论内容是否相同）
    let rx = send_raw(&h, "ui-0", "get_job", json!({ "job_id": 0 }));
    assert_eq!(recv_err(&rx).code, "DUPLICATE_EXPIRED");
    // 仍缓存的 id 同内容 → 重放
    let rx = send_raw(&h, "ui-10000", "get_job", json!({ "job_id": 10000 }));
    assert_eq!(recv_ok(&rx)["job_id"], 10000);
}

/// backpressure-busy-32：普通类 ≥32 → BUSY{retryable:true} 不转发；控制类豁免
/// （03：停止/心跳通道保留容量，不能被预览淹没）。
#[test]
fn busy_at_32_normal_and_control_bypasses() {
    let h = setup();
    // 占满 32 个普通在途
    for i in 0..NORMAL_INFLIGHT_LIMIT {
        let id = format!("n-{i}");
        let _rx = send_raw(&h, &id, "execute_script", json!({ "script_id": i }));
    }
    wait_until(|| h.sink.sent_len() == NORMAL_INFLIGHT_LIMIT, Duration::from_secs(2));
    // 第 33 个普通请求 → BUSY，不转发
    let busy_rx = send_raw(&h, "n-busy", "execute_script", json!({}));
    let err = recv_err(&busy_rx);
    assert_eq!(err.code, "BUSY");
    assert!(err.retryable);
    assert_eq!(h.sink.sent_len(), NORMAL_INFLIGHT_LIMIT, "BUSY 请求不转发");
    // 控制类不受限：ping 照常转发
    let ping_rx = send_raw(&h, "ctl-ping", "ping", json!({}));
    wait_until(|| h.sink.sent_len() == NORMAL_INFLIGHT_LIMIT + 1, Duration::from_secs(2));
    feed_frame(&h, ok_response("ctl-ping", json!({ "engine_time": "t" })));
    assert_eq!(recv_ok(&ping_rx)["engine_time"], "t");
    // 控制类 stop_script 在 32 满时照常受理（TEST-008 联动子集）
    let stop_rx = send_raw(&h, "ctl-stop", "stop_script", json!({ "execution_id": "e1" }));
    wait_until(|| h.sink.sent_len() == NORMAL_INFLIGHT_LIMIT + 2, Duration::from_secs(2));
    feed_frame(&h, ok_response("ctl-stop", json!({ "state": "stopping" })));
    assert_eq!(recv_ok(&stop_rx)["state"], "stopping");
}

/// control-class-commands + TEST-008 停止优先级（本任务可测部分）：
/// stop_script 转发前先 gate.close_gate（闸门先于 send，spy 时序断言）。
#[test]
fn stop_script_closes_gate_before_forwarding() {
    let h = setup();
    let rx = send_raw(&h, "ctl-stop", "stop_script", json!({ "execution_id": "e1" }));
    wait_forwarded(&h, "ctl-stop");
    assert_eq!(
        h.log.snapshot(),
        vec!["gate:stop_script".to_string(), "send:ctl-stop".to_string()],
        "close_gate 必须先于转发（03/06：停止必须先由 Rust 关输入闸门）"
    );
    feed_frame(&h, ok_response("ctl-stop", json!({ "state": "stopping" })));
    let _ = recv_ok(&rx);
}

/// timeout-normal-5s：注入时钟推进 5s → INTERNAL_ERROR（message 含期限）；
/// 迟到成功响应仍写缓存（更新历史，不复活执行，03）；超时后重放超时响应。
#[test]
fn timeout_then_late_response_updates_history() {
    let h = setup();
    let rx = send_raw(&h, "ui-1", "get_job", json!({ "job_id": "a" }));
    wait_forwarded(&h, "ui-1"); // 栅栏：保证 admit 已按推进前时钟计算期限

    h.clock.advance(Duration::from_millis(5_100));
    feed_frame(&h, wake_event()); // 唤醒事件循环 → 期限已过 → expire_due
    let err = recv_err(&rx);
    assert_eq!(err.code, "INTERNAL_ERROR");
    assert_eq!(err.message, "请求超时(5000ms)");
    assert_eq!(err.details["request_id"], "ui-1");
    assert_eq!(err.details["deadline_ms"], 5000);

    // 迟到的成功响应：写缓存更新历史（不投递——等待者已终结）
    feed_frame(&h, ok_response("ui-1", json!({ "job_id": "a", "status": "done" })));
    // query_request 读缓存 → completed + 迟到响应
    let qx = send_raw(&h, "q-1", "query_request", json!({ "request_id": "ui-1" }));
    let answer = recv_ok(&qx);
    assert_eq!(answer["status"], "completed");
    assert_eq!(answer["response"]["status"], "done");
    // 同 ID 同内容重放 → 返回缓存中的迟到响应，不重新执行
    let rx2 = send_raw(&h, "ui-1", "get_job", json!({ "job_id": "a" }));
    assert_eq!(recv_ok(&rx2)["status"], "done");
    assert_eq!(h.sink.sent_len(), 1, "超时后重放不得二次转发");
}

/// timeout-startup-30s：hello 期限 30s（5s 未到不超时）。
#[test]
fn hello_deadline_is_30_seconds() {
    let h = setup();
    let rx = send_raw(&h, "ui-hello", "hello", json!({}));
    wait_forwarded(&h, "ui-hello");

    // 推进 6s（普通期限已过）→ hello 不得超时
    h.clock.advance(Duration::from_millis(6_000));
    feed_frame(&h, wake_event());
    // 期限尚未到期：等待者不得收到任何回复（无忙等，直接探测一次）
    assert!(
        rx.try_recv().is_err(),
        "hello 在 30s 期限内不得超时（06：启动 30s）"
    );

    // 推进至 30s 之后 → 超时
    h.clock.advance(Duration::from_millis(25_000));
    feed_frame(&h, wake_event());
    let err = recv_err(&rx);
    assert_eq!(err.code, "INTERNAL_ERROR");
    assert_eq!(err.message, "请求超时(30000ms)");
}

/// query_request 状态三态（03：pending/completed/unknown；只查询同会话）。
#[test]
fn query_request_reads_local_pending_and_cache() {
    let h = setup();
    // unknown
    let qx = send_raw(&h, "q-0", "query_request", json!({ "request_id": "ghost" }));
    let answer = recv_ok(&qx);
    assert_eq!(answer["status"], "unknown");
    assert!(answer["response"].is_null());
    // pending
    let rx = send_raw(&h, "ui-5", "get_job", json!({ "job_id": "j" }));
    wait_forwarded(&h, "ui-5");
    let qx = send_raw(&h, "q-1", "query_request", json!({ "request_id": "ui-5" }));
    assert_eq!(recv_ok(&qx)["status"], "pending");
    // completed
    feed_frame(&h, ok_response("ui-5", json!({ "job_id": "j", "status": "done" })));
    let _ = recv_ok(&rx);
    let qx = send_raw(&h, "q-2", "query_request", json!({ "request_id": "ui-5" }));
    let answer = recv_ok(&qx);
    assert_eq!(answer["status"], "completed");
    assert_eq!(answer["response"]["status"], "done");
    // 参数缺失 → INVALID_PARAMS
    let qx = send_raw(&h, "q-3", "query_request", json!({}));
    assert_eq!(recv_err(&qx).code, "INVALID_PARAMS");
}

/// unknown-command-business-placeholder + 表外命令：不出壳（M08）；
/// 宿主类 → CAPABILITY_DENIED（§5.2 骨架期）。
#[test]
fn unknown_and_host_denied_routing() {
    let h = setup();
    // 表外命令：Rust 直接 UNKNOWN_COMMAND，不转发
    let rx = send_raw(&h, "u-1", "make_coffee", json!({}));
    let err = recv_err(&rx);
    assert_eq!(err.code, "UNKNOWN_COMMAND");
    assert_eq!(err.details["command"], "make_coffee");
    assert_eq!(h.sink.sent_len(), 0);
    // 表外命令同样入缓存：同 ID 同内容重放
    let rx = send_raw(&h, "u-1", "make_coffee", json!({}));
    assert_eq!(recv_err(&rx).code, "UNKNOWN_COMMAND");
    // 引擎业务命令（已注册框架位）：转发 Python → 骨架期由 Python 回
    // UNKNOWN_COMMAND；这里直接喂 Python 的错误响应验证关联与错误透传
    let rx = send_raw(&h, "u-2", "execute_script", json!({ "script_id": "s1" }));
    wait_forwarded(&h, "u-2");
    feed_frame(&h, err_response("u-2", "UNKNOWN_COMMAND"));
    let err = recv_err(&rx);
    assert_eq!(err.code, "UNKNOWN_COMMAND");
    // 宿主窗口族（TASK-018 前）：CAPABILITY_DENIED，不转发 Python
    // （TASK-008：capture_region 等 TASK-007/009 已实现的资源命令改为转发 Python，
    //  此处改用仍属宿主窗口族的 list_windows 验证 HostDenied 路径）
    let rx = send_raw(&h, "u-3", "list_windows", json!({}));
    let err = recv_err(&rx);
    assert_eq!(err.code, "CAPABILITY_DENIED");
    assert_eq!(err.details["command"], "list_windows");
    assert_eq!(h.sink.sent_len(), 1, "宿主窗口族与表外命令均不得转发（仅 u-2 转发）");
}

/// oversize-frame-2mib（发送侧）：序列化 >2MiB → PROTOCOL_ERROR 拒绝发送（§6.5）。
#[test]
fn oversize_send_is_rejected_with_protocol_error() {
    let h = setup();
    let big = "x".repeat(3 * 1024 * 1024);
    let rx = send_raw(&h, "ui-big", "execute_script", json!({ "blob": big }));
    let err = recv_err(&rx);
    assert_eq!(err.code, "PROTOCOL_ERROR");
    assert!(err.message.contains("2MiB"));
    assert_eq!(h.sink.sent_len(), 0, "超限帧不得发出");
}

/// Shutdown / 通道断开：pending 全清、ENGINE_UNAVAILABLE 快速失败（§6.1/§7）。
#[test]
fn shutdown_fails_all_pending_with_engine_unavailable() {
    let h = setup();
    let rx = send_raw(&h, "ui-1", "execute_script", json!({ "script_id": "s1" }));
    wait_forwarded(&h, "ui-1");
    h.handle.shutdown();
    let err = recv_err(&rx);
    assert_eq!(err.code, "ENGINE_UNAVAILABLE");
    assert!(err.retryable, "06：允许用户重启");
}

/// 单 writer 写失败（管道断裂替身）：请求以 ENGINE_UNAVAILABLE 终结（§7）。
#[test]
fn send_failure_fails_request_with_engine_unavailable() {
    let h = setup();
    h.sink.fail.store(true, Ordering::SeqCst);
    let rx = send_raw(&h, "ui-1", "ping", json!({}));
    let err = recv_err(&rx);
    assert_eq!(err.code, "ENGINE_UNAVAILABLE");
    assert!(err.message.contains("stdin"));
}

/// 旧 session 帧：丢弃+日志，不计畸形、不投递（03；用户契约③）。
#[test]
fn stale_session_frames_are_dropped() {
    let h = setup();
    let rx = send_raw(&h, "ui-1", "get_job", json!({ "job_id": "a" }));
    wait_forwarded(&h, "ui-1");
    let mut stale = ok_response("ui-1", json!({ "job_id": "a" }));
    stale.session_id = "session-old".to_string();
    feed_frame(&h, stale);
    assert!(rx.try_recv().is_err(), "旧 session 响应不得投递");
    // 正确 session 的响应照常关联
    feed_frame(&h, ok_response("ui-1", json!({ "job_id": "a", "status": "ok" })));
    assert_eq!(recv_ok(&rx)["status"], "ok");
}

/// §5.1：请求 id 非空 ≤128 → 否则 INVALID_PARAMS（不转发、不入台账）。
#[test]
fn invalid_request_id_rejected() {
    let h = setup();
    let rx = send_raw(&h, "", "ping", json!({}));
    assert_eq!(recv_err(&rx).code, "INVALID_PARAMS");
    let long_id = "x".repeat(129);
    let rx = send_raw(&h, &long_id, "ping", json!({}));
    assert_eq!(recv_err(&rx).code, "INVALID_PARAMS");
    assert_eq!(h.sink.sent_len(), 0);
}

/// §4.3 取消链路：pending 标记 cancelled → 转发 cancel_request（控制类）→
/// 迟到响应只写缓存不投递；query_request 可查（03：Promise 超时 ≠ 后台取消成功）。
#[test]
fn cancel_flow_marks_pending_and_updates_history() {
    let h = setup();
    let rx = send_raw(&h, "ui-1", "execute_script", json!({ "script_id": "s1" }));
    wait_forwarded(&h, "ui-1");

    // ipc_cancel：转发 cancel_request（幂等），等待 Python 的 {cancelled, reason}
    let cancel_rx = send_cancel_raw(&h, "ui-1");
    wait_until(|| h.sink.sent_len() == 2, Duration::from_secs(2));
    let cancel_id = h.sink.sent_ids()[1].clone();
    assert!(cancel_id.starts_with("cancel-"), "Router 自发请求 id 应可辨识: {cancel_id}");
    feed_frame(&h, ok_response(&cancel_id, json!({ "cancelled": true, "reason": "cancelled" })));
    let ack = recv_ok(&cancel_rx);
    assert_eq!(ack["cancelled"], true);
    assert_eq!(ack["reason"], "cancelled");

    // 迟到的真实响应：只写缓存，不投递已取消请求的等待者（§4.3）
    feed_frame(&h, ok_response("ui-1", json!({ "state": "stopped" })));
    assert!(rx.try_recv().is_err(), "取消后响应不得投递原等待者");
    // 但可经 query_request 查询（03：需查询状态）
    let qx = send_raw(&h, "q-1", "query_request", json!({ "request_id": "ui-1" }));
    let answer = recv_ok(&qx);
    assert_eq!(answer["status"], "completed");
    assert_eq!(answer["response"]["state"], "stopped");
    // 同 ID 同内容重放 → 返回缓存历史（不重做副作用）
    let rx2 = send_raw(&h, "ui-1", "execute_script", json!({ "script_id": "s1" }));
    assert_eq!(recv_ok(&rx2)["state"], "stopped");
    assert_eq!(h.sink.sent_len(), 2, "取消确认后重放不得再转发");
}

/// RouterHandle::request 驱动宿主命令：成功路径。
#[test]
fn router_handle_request_roundtrip() {
    let h = setup();
    // hello 转发 + 响应 → handle.request 返回 data（阻塞等待路径）
    let handle2 = h.handle.clone();
    let worker = std::thread::spawn(move || handle2.request("ui-h", "hello", json!({})));
    wait_until(|| h.sink.sent_len() == 1, Duration::from_secs(2));
    feed_frame(&h, ok_response("ui-h", json!({ "engine_version": "0.1.0" })));
    let result = worker.join().expect("worker 不 panic").expect("hello 成功");
    assert_eq!(result["engine_version"], "0.1.0");
}
