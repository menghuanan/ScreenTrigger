//! PendingMap：请求关联、去重台账与背压计数（TASK-005 设计 §3.1/§6.1/§6.2/§6.3）。
//!
//! 并发模型（§2.2）：全部方法只在**单 Router 线程**内调用，pending map/去重表/
//! seq 计数器无锁；请求等待用 per-request `std::sync::mpsc` oneshot（Sender 可
//! Clone，同内容在途重复请求挂接共享响应）。
//!
//! 阈值（逐字照 03/06，不发明）：
//! - 响应缓存 LRU 10000（03：请求 ID 最多保留最近 10000 个最终响应）；
//! - 已见 ID 集合 100000（淘汰窗口内再达 → DUPLICATE_EXPIRED；达 100000 且在途
//!   为空 → 清空集合与缓存重建空闲会话，非空闲推迟到下一个空闲检查点）；
//! - 普通类在途上限 32（超限 BUSY）；控制类独立计数不受限；
//! - 超时 5s（普通/业务）/30s（hello）。
//!
//! 去重判定次序（§6.1）：响应缓存 → 在途 pending → 已见集合 → 新请求。
//! 注：设计 §6.1 原文把"已见集合"列在"在途 pending"之前，但 §6.1 第 4 步明确
//! "已见集合插入 → 在途登记"，即在途 id 必然已在已见集合中——若先查已见集合，
//! §4.2 的"在途挂接"路径将不可达。按语义正确次序实现（缓存→在途→已见），
//! 已列入回报偏差说明。

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::mpsc::Sender;
use std::time::Instant;

use serde_json::Value;

use super::types::{CommandClass, IpcError, deadline_for};

// ---------------------------------------------------------------------------
// 阈值常量（03/06 冻结值）
// ---------------------------------------------------------------------------

/// 响应缓存 LRU 容量（03：最近 10000 个最终响应）。
pub const RESPONSE_CACHE_CAPACITY: usize = 10_000;
/// 已见 ID 集合容量（03：会话总数限制 100000，之后重建空闲会话）。
pub const SEEN_SET_CAPACITY: usize = 100_000;
/// 普通类在途上限（03：普通请求最多 32 在途，满则 BUSY）。
pub const NORMAL_INFLIGHT_LIMIT: usize = 32;

// ---------------------------------------------------------------------------
// 时钟注入（设计 §10：时钟经 trait 注入，支持契约测试替换）
// ---------------------------------------------------------------------------

/// 时钟抽象（单调；`now()` 可替换以支持契约测试注入时钟）。
pub trait Clock: Send {
    fn now(&self) -> Instant;
}

/// 系统单调时钟（生产实现）。
pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> Instant {
        Instant::now()
    }
}

// ---------------------------------------------------------------------------
// 台账元素
// ---------------------------------------------------------------------------

/// 内容指纹（§6.1：command + 规范化 params）。serde_json::Value 默认 BTreeMap
/// 键序（未启用 preserve_order）→ `to_string` 确定性；分隔符用不可见控制字符
/// 防拼接歧义。
pub fn fingerprint(command: &str, params: &Value) -> String {
    let canonical = serde_json::to_string(params).unwrap_or_default();
    format!("{command}\u{1}{canonical}")
}

/// 请求等待者（per-request oneshot 发送端；同内容在途重复请求共享响应 =
/// 多个 Sender 各自带独立 Receiver）。
pub type Waiter = Sender<Result<Value, IpcError>>;

/// 在途条目。
#[derive(Debug)]
pub struct PendingEntry {
    pub command: String,
    pub fingerprint: String,
    pub class: CommandClass,
    /// 超时期限（单调时钟时刻，经注入时钟计算）。
    pub deadline: Instant,
    /// 期限毫秒数（超时错误 message/details 用，§6.3/§9-D5）。
    pub deadline_ms: u64,
    /// 已被 cancel_request 取消（后续响应只写缓存不投递，§4.3）。
    pub cancelled: bool,
    pub waiters: Vec<Waiter>,
}

/// 去重前置判定结果（§6.1）。
#[derive(Debug)]
pub enum Dedup {
    /// 缓存命中且指纹一致：重放缓存响应，不重新执行（03）。
    Replay(Result<Value, IpcError>),
    /// 同 ID 不同内容（缓存或在途）：CONFLICT（03）。
    Conflict,
    /// 被淘汰 ID 再达：DUPLICATE_EXPIRED（03，不重做副作用）。
    DuplicateExpired,
    /// 在途挂接：相同指纹共享同一响应（§4.2）。
    Join,
    /// 新请求：登记后按容量/路由处理。
    Admit,
}

/// query_request 的请求状态（03：pending/completed/unknown）。
#[derive(Debug)]
pub enum RequestState {
    Pending {
        cancelled: bool,
    },
    Completed(Result<Value, IpcError>),
    Unknown,
}

/// 响应缓存槽位（指纹用于同 ID 异内容 CONFLICT 判定）。
struct CacheSlot {
    result: Result<Value, IpcError>,
    fingerprint: String,
}

// ---------------------------------------------------------------------------
// PendingMap（单线程无锁）
// ---------------------------------------------------------------------------

/// 在途请求台账 + 响应缓存 + 已见 ID 集合（§6.1 全部状态的唯一载体）。
pub struct PendingMap {
    clock: Box<dyn Clock>,
    entries: HashMap<String, PendingEntry>,
    normal_inflight: usize,
    control_inflight: usize,
    cache: HashMap<String, CacheSlot>,
    cache_order: VecDeque<String>,
    seen: HashSet<String>,
    seen_order: VecDeque<String>,
}

impl PendingMap {
    /// 生产构造（系统时钟）。
    pub fn new() -> Self {
        Self::with_clock(Box::new(SystemClock))
    }

    /// 注入时钟构造（契约测试用）。
    pub fn with_clock(clock: Box<dyn Clock>) -> Self {
        PendingMap {
            clock,
            entries: HashMap::new(),
            normal_inflight: 0,
            control_inflight: 0,
            cache: HashMap::new(),
            cache_order: VecDeque::new(),
            seen: HashSet::new(),
            seen_order: VecDeque::new(),
        }
    }

    /// 当前注入时钟时刻（Router 事件循环计算 recv 超时用）。
    pub fn now(&self) -> Instant {
        self.clock.now()
    }

    // ---- §6.1 去重前置判定 ----

    /// 去重判定：响应缓存 → 在途 pending → 已见集合 → Admit（语义次序见模块注释）。
    pub fn classify(&mut self, id: &str, fp: &str) -> Dedup {
        if let Some(slot) = self.cache.get_mut(id) {
            if slot.fingerprint == fp {
                let result = slot.result.clone();
                self.touch_cache(id);
                return Dedup::Replay(result);
            }
            return Dedup::Conflict;
        }
        if let Some(entry) = self.entries.get(id) {
            return if entry.fingerprint == fp {
                Dedup::Join
            } else {
                Dedup::Conflict
            };
        }
        if self.seen.contains(id) {
            return Dedup::DuplicateExpired;
        }
        Dedup::Admit
    }

    /// 在途登记（§6.1 第 4 步：已见集合插入 → 在途登记）。容量检查（§6.2）由
    /// dispatcher 在调用前完成——BUSY 请求不得进入台账。
    pub fn admit(&mut self, id: &str, command: &str, fp: String, class: CommandClass, waiter: Waiter) {
        self.insert_seen(id);
        let deadline_ms = deadline_for(command).as_millis() as u64;
        let deadline = self.clock.now() + deadline_for(command);
        self.entries.insert(
            id.to_string(),
            PendingEntry {
                command: command.to_string(),
                fingerprint: fp,
                class,
                deadline,
                deadline_ms,
                cancelled: false,
                waiters: vec![waiter],
            },
        );
        match class {
            CommandClass::Normal => self.normal_inflight += 1,
            CommandClass::Control => self.control_inflight += 1,
        }
    }

    /// 在途挂接：同内容重复请求追加等待者（共享响应，§4.2）。
    pub fn attach(&mut self, id: &str, waiter: Waiter) -> bool {
        match self.entries.get_mut(id) {
            Some(entry) => {
                entry.waiters.push(waiter);
                true
            }
            None => false,
        }
    }

    // ---- 完成 / 超时 / 取消 ----

    /// 响应到达：投递等待者（cancelled 条目不投递，只写缓存——§4.3）、写响应
    /// 缓存（LRU 淘汰最旧）、从在途移除；已见集合不变（§6.1）。
    ///
    /// 返回是否命中在途条目；false = 迟到响应（调用方走 `write_cache_only`）。
    pub fn complete(&mut self, id: &str, result: &Result<Value, IpcError>) -> bool {
        let Some(entry) = self.entries.remove(id) else {
            return false;
        };
        match entry.class {
            CommandClass::Normal => self.normal_inflight -= 1,
            CommandClass::Control => self.control_inflight -= 1,
        }
        if !entry.cancelled {
            for waiter in &entry.waiters {
                let _ = waiter.send(result.clone());
            }
        }
        self.write_cache(id, result.clone(), entry.fingerprint);
        // 空闲检查点：已见集合达容量且在途为空 → 重建（§6.1 会话重建）
        self.maybe_rebuild();
        true
    }

    /// 迟到响应（请求已超时移除/取消终态后到达）：仅**更新**既有缓存历史，
    /// 不投递、不复活执行（03）。缓存中无该 id（已淘汰）则忽略——避免把已过
    /// 淘汰窗口的 id 重新写入缓存造成指纹失配误判 CONFLICT。
    pub fn write_cache_only(&mut self, id: &str, result: Result<Value, IpcError>) -> bool {
        if !self.cache.contains_key(id) {
            return false;
        }
        let fp = self.cache[id].fingerprint.clone();
        self.write_cache(id, result, fp);
        true
    }

    /// 标记取消（§4.3：pending 标记 cancelled，后续响应只写缓存不投递）。
    pub fn mark_cancelled(&mut self, id: &str) -> bool {
        match self.entries.get_mut(id) {
            Some(entry) => {
                entry.cancelled = true;
                true
            }
            None => false,
        }
    }

    /// 超时扫描（§6.3）：到期条目 → INTERNAL_ERROR 超时响应（message 含期限、
    /// details 含 request_id/deadline_ms）→ 经 `complete` 写缓存+投递等待者
    /// （cancelled 条目不投递）。返回本次到期条目数。
    pub fn expire_due(&mut self) -> usize {
        let now = self.clock.now();
        let expired: Vec<String> = self
            .entries
            .iter()
            .filter(|(_, e)| e.deadline <= now)
            .map(|(k, _)| k.clone())
            .collect();
        let mut count = 0;
        for id in expired {
            // 直接构造超时错误并复用 complete（写缓存 + 投递 + 计数回收）
            let (deadline_ms, cancelled) = {
                let entry = &self.entries[&id];
                (entry.deadline_ms, entry.cancelled)
            };
            let _ = cancelled; // complete 内部按 cancelled 决定是否投递
            let timeout_err = IpcError::timeout(deadline_ms, &id);
            self.complete(&id, &Err(timeout_err));
            count += 1;
        }
        count
    }

    /// query_request（§3.1：读取缓存/在途状态；03：{status,response}）。
    pub fn query(&self, request_id: &str) -> RequestState {
        if let Some(entry) = self.entries.get(request_id) {
            return RequestState::Pending {
                cancelled: entry.cancelled,
            };
        }
        if let Some(slot) = self.cache.get(request_id) {
            return RequestState::Completed(slot.result.clone());
        }
        RequestState::Unknown
    }

    /// 引擎清理/跨进程重启（§6.1：pending 全清、ENGINE_UNAVAILABLE 快速失败
    /// UI 等待者；03 明文不承诺跨进程去重）。返回清空的在途条目数。
    pub fn fail_all(&mut self, err: IpcError) -> usize {
        let count = self.entries.len();
        for (_, entry) in self.entries.drain() {
            if entry.cancelled {
                continue;
            }
            for waiter in &entry.waiters {
                let _ = waiter.send(Err(err.clone()));
            }
        }
        self.normal_inflight = 0;
        self.control_inflight = 0;
        count
    }

    /// 会话重建（B3 接线：引擎重启/新 session 时调用）：快速失败在途等待者 +
    /// 清空去重台账（新会话全新台账）。
    pub fn reset(&mut self) {
        self.fail_all(IpcError::engine_unavailable("引擎会话已重建"));
        self.cache.clear();
        self.cache_order.clear();
        self.seen.clear();
        self.seen_order.clear();
    }

    // ---- 观测与内部维护 ----

    /// 最近到期时刻（Router 事件循环 recv_timeout 用；无在途 → None）。
    pub fn next_deadline(&self) -> Option<Instant> {
        self.entries.values().map(|e| e.deadline).min()
    }

    pub fn normal_inflight(&self) -> usize {
        self.normal_inflight
    }

    pub fn control_inflight(&self) -> usize {
        self.control_inflight
    }

    pub fn pending_len(&self) -> usize {
        self.entries.len()
    }

    pub fn cache_len(&self) -> usize {
        self.cache.len()
    }

    pub fn seen_len(&self) -> usize {
        self.seen.len()
    }

    /// 已见集合插入（容量达 100000：空闲即重建，非空闲推迟到下一个空闲检查点）。
    fn insert_seen(&mut self, id: &str) {
        if self.seen.len() >= SEEN_SET_CAPACITY {
            self.maybe_rebuild();
        }
        if self.seen.insert(id.to_string()) {
            self.seen_order.push_back(id.to_string());
        }
    }

    /// 会话重建（§6.1：已见 ID 集合 ≥100000 且在途为空 → 清空集合与缓存重建；
    /// 非空闲则推迟到下一个空闲检查点——本方法在 admit/complete 检查点反复调用）。
    pub fn maybe_rebuild(&mut self) {
        if self.seen.len() >= SEEN_SET_CAPACITY && self.entries.is_empty() {
            self.seen.clear();
            self.seen_order.clear();
            self.cache.clear();
            self.cache_order.clear();
        }
    }

    /// 写响应缓存（LRU：容量超限淘汰最旧；更新既有条目时刷新 LRU 位次）。
    fn write_cache(&mut self, id: &str, result: Result<Value, IpcError>, fp: String) {
        if self.cache.contains_key(id) {
            // 更新历史（迟到响应/终态覆盖）：先移除旧位次再入队尾
            self.cache_order.retain(|x| x != id);
        }
        self.cache.insert(id.to_string(), CacheSlot { result, fingerprint: fp });
        self.cache_order.push_back(id.to_string());
        while self.cache.len() > RESPONSE_CACHE_CAPACITY {
            match self.cache_order.pop_front() {
                Some(oldest) => {
                    self.cache.remove(&oldest);
                }
                None => break,
            }
        }
    }

    /// 缓存命中触碰（LRU 移到队尾）。
    fn touch_cache(&mut self, id: &str) {
        self.cache_order.retain(|x| x != id);
        self.cache_order.push_back(id.to_string());
    }
}

impl Default for PendingMap {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// 单元测试（台账级：指纹规范化 / LRU 淘汰 / 会话重建 / 超时分类；Router 链路级
// 契约测试见 contract_tests.rs）
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    /// 偏移注入时钟（测试用：now() = Instant::now() + offset）。
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

    fn ok(data: Value) -> Result<Value, IpcError> {
        Ok(data)
    }

    #[test]
    fn fingerprint_is_canonical_over_param_key_order() {
        let a = fingerprint("execute_script", &serde_json::from_str::<Value>(r#"{"b":2,"a":1}"#).unwrap());
        let b = fingerprint("execute_script", &serde_json::from_str::<Value>(r#"{"a":1,"b":2}"#).unwrap());
        assert_eq!(a, b, "BTreeMap 键序规范化：插入次序不同不影响指纹");
        let c = fingerprint("execute_script", &serde_json::from_str::<Value>(r#"{"a":1,"b":3}"#).unwrap());
        assert_ne!(b, c, "内容不同指纹必不同");
        let d = fingerprint("get_job", &serde_json::from_str::<Value>(r#"{"a":1,"b":2}"#).unwrap());
        assert_ne!(b, d, "命令不同指纹必不同");
    }

    #[test]
    fn classify_order_cache_then_pending_then_seen() {
        let mut map = PendingMap::new();
        let (tx, _rx) = std::sync::mpsc::channel();
        let fp1 = fingerprint("get_job", &serde_json::json!({"job_id": "j1"}));
        // 新请求 → Admit（进入已见集合 + 在途）
        assert!(matches!(map.classify("ui-1", &fp1), Dedup::Admit));
        map.admit("ui-1", "get_job", fp1.clone(), CommandClass::Normal, tx.clone());
        // 在途同指纹 → Join；异指纹 → Conflict（在途优先于已见集合）
        assert!(matches!(map.classify("ui-1", &fp1), Dedup::Join));
        let fp2 = fingerprint("get_job", &serde_json::json!({"job_id": "j2"}));
        assert!(matches!(map.classify("ui-1", &fp2), Dedup::Conflict));
        // 完成 → 缓存命中同指纹 Replay、异指纹 Conflict
        map.complete("ui-1", &ok(serde_json::json!({"status": "completed"})));
        assert!(matches!(map.classify("ui-1", &fp1), Dedup::Replay(Ok(_))));
        assert!(matches!(map.classify("ui-1", &fp2), Dedup::Conflict));
        // 从缓存/台账彻底未知的新 id → Admit
        assert!(matches!(map.classify("ui-9", &fp1), Dedup::Admit));
    }

    #[test]
    fn inflight_counters_track_by_class() {
        let mut map = PendingMap::new();
        let (tx, _rx) = std::sync::mpsc::channel();
        let fp = fingerprint("ping", &serde_json::json!({}));
        map.admit("c-1", "ping", fp.clone(), CommandClass::Control, tx.clone());
        map.admit("c-2", "ping", fp.clone(), CommandClass::Control, tx.clone());
        let fpn = fingerprint("execute_script", &serde_json::json!({}));
        map.admit("n-1", "execute_script", fpn, CommandClass::Normal, tx);
        assert_eq!(map.control_inflight(), 2);
        assert_eq!(map.normal_inflight(), 1);
        assert_eq!(map.pending_len(), 3);
        map.complete("c-1", &ok(Value::Null));
        assert_eq!(map.control_inflight(), 1);
        map.fail_all(IpcError::engine_unavailable("x"));
        assert_eq!(map.normal_inflight(), 0);
        assert_eq!(map.control_inflight(), 0);
        assert_eq!(map.pending_len(), 0);
    }

    #[test]
    fn complete_after_cancel_writes_cache_without_delivery() {
        let mut map = PendingMap::new();
        let (tx, rx) = std::sync::mpsc::channel();
        let fp = fingerprint("execute_script", &serde_json::json!({}));
        map.admit("ui-1", "execute_script", fp, CommandClass::Normal, tx);
        assert!(map.mark_cancelled("ui-1"));
        // cancelled：真实响应只写缓存，不投递等待者（§4.3）
        map.complete("ui-1", &ok(serde_json::json!({"state": "stopped"})));
        assert!(rx.try_recv().is_err(), "取消后响应不得投递");
        match map.query("ui-1") {
            RequestState::Completed(Ok(v)) => assert_eq!(v["state"], "stopped"),
            other => panic!("应命中缓存，实际 {other:?}"),
        }
    }

    #[test]
    fn expire_uses_deadline_ms_in_error_message() {
        let clock = FakeClock::default();
        let mut map = PendingMap::with_clock(Box::new(clock.clone()));
        let (tx, rx) = std::sync::mpsc::channel();
        let fp = fingerprint("get_job", &serde_json::json!({}));
        map.admit("ui-1", "get_job", fp, CommandClass::Normal, tx);
        assert_eq!(map.expire_due(), 0, "未到期不得触发");
        clock.advance(Duration::from_millis(5_100));
        assert_eq!(map.expire_due(), 1);
        match rx.recv_timeout(Duration::from_secs(1)).unwrap() {
            Err(e) => {
                assert_eq!(e.code, "INTERNAL_ERROR");
                assert_eq!(e.message, "请求超时(5000ms)");
                assert_eq!(e.details["request_id"], "ui-1");
            }
            other => panic!("超时应回 INTERNAL_ERROR，实际 {other:?}"),
        }
        // 超时后写缓存；迟到成功响应更新历史
        match map.query("ui-1") {
            RequestState::Completed(Err(_)) => {}
            other => panic!("超时响应应入缓存，实际 {other:?}"),
        }
        assert!(map.write_cache_only("ui-1", ok(serde_json::json!({"late": true}))));
        match map.query("ui-1") {
            RequestState::Completed(Ok(v)) => assert_eq!(v["late"], true, "迟到响应更新历史"),
            other => panic!("迟到响应应更新缓存，实际 {other:?}"),
        }
        // 缓存中不存在的 id → write_cache_only 忽略（不复活）
        assert!(!map.write_cache_only("ghost", ok(Value::Null)));
    }

    #[test]
    fn cache_eviction_at_10000_then_duplicate_expired() {
        let mut map = PendingMap::new();
        let (tx, _rx) = std::sync::mpsc::channel();
        // 10001 个请求-响应：第 10001 个把第 1 个挤出缓存
        for i in 0..=RESPONSE_CACHE_CAPACITY {
            let id = format!("ui-{i}");
            let fp = fingerprint("get_job", &serde_json::json!({"job_id": i}));
            map.admit(&id, "get_job", fp.clone(), CommandClass::Normal, tx.clone());
            map.complete(&id, &ok(serde_json::json!({"job_id": i})));
        }
        assert_eq!(map.cache_len(), RESPONSE_CACHE_CAPACITY);
        // 首个 id 已淘汰 → 已见集合命中 → DUPLICATE_EXPIRED
        let fp0 = fingerprint("get_job", &serde_json::json!({"job_id": 0}));
        assert!(matches!(map.classify("ui-0", &fp0), Dedup::DuplicateExpired));
        // 未淘汰的 id 仍可重放
        let fp1 = fingerprint("get_job", &serde_json::json!({"job_id": 1}));
        assert!(matches!(map.classify("ui-1", &fp1), Dedup::Replay(Ok(_))));
    }

    #[test]
    fn seen_rebuild_at_100000_when_idle_deferred_when_busy() {
        let mut map = PendingMap::new();
        let (tx, _rx) = std::sync::mpsc::channel();
        let fp = fingerprint("get_job", &serde_json::json!({}));
        // 先挂一个在途条目（构造"非空闲"前提），再填 99999 个已完成请求
        // → 已见集合 = 100000 且存在在途
        map.admit("inflight-a", "get_job", fp.clone(), CommandClass::Normal, tx.clone());
        for i in 0..SEEN_SET_CAPACITY - 1 {
            let id = format!("ui-{i}");
            map.admit(&id, "get_job", fp.clone(), CommandClass::Normal, tx.clone());
            map.complete(&id, &ok(Value::Null));
        }
        assert_eq!(map.seen_len(), SEEN_SET_CAPACITY);
        assert_eq!(map.pending_len(), 1);
        // 第 100001 次插入时在途非空 → 重建推迟：集合瞬时超限继续增长
        map.admit("inflight-b", "get_job", fp.clone(), CommandClass::Normal, tx.clone());
        assert_eq!(map.seen_len(), SEEN_SET_CAPACITY + 1, "非空闲时推迟重建");
        assert_eq!(map.cache_len(), RESPONSE_CACHE_CAPACITY, "缓存受 LRU 容量约束");
        // complete 触发空闲检查点：仍有一个在途 → 推迟；全部完成后 → 清空重建
        map.complete("inflight-a", &ok(Value::Null));
        assert_eq!(map.seen_len(), SEEN_SET_CAPACITY + 1, "仍有在途，重建推迟");
        map.complete("inflight-b", &ok(Value::Null));
        assert_eq!(map.seen_len(), 0, "空闲检查点应清空已见集合");
        assert_eq!(map.cache_len(), 0);
        // 重建后旧 id 按 Admit 处理（03：限制会话总数 100000，之后重建空闲会话）
        assert!(matches!(map.classify("ui-0", &fp), Dedup::Admit));
    }

    #[test]
    fn fail_all_notifies_waiters_with_engine_unavailable() {
        let mut map = PendingMap::new();
        let (tx, rx) = std::sync::mpsc::channel();
        let fp = fingerprint("execute_script", &serde_json::json!({}));
        map.admit("ui-1", "execute_script", fp, CommandClass::Normal, tx);
        assert_eq!(map.fail_all(IpcError::engine_unavailable("进程退出")), 1);
        match rx.recv_timeout(Duration::from_secs(1)).unwrap() {
            Err(e) => {
                assert_eq!(e.code, "ENGINE_UNAVAILABLE");
                assert!(e.retryable);
            }
            other => panic!("应快速失败，实际 {other:?}"),
        }
        // 幂等：再次 fail_all 无条目
        assert_eq!(map.fail_all(IpcError::engine_unavailable("again")), 0);
    }
}
