//! 输入闸门（TASK-017 / M05、06§时间取消与重试）。
//!
//! **职责**：per-execution 闸门 + 全局闸门。停止/取消/故障/退出时**立即**关闸，
//! 此后任何新派发一律被拒（03/06：停止必须先由 Rust 关输入闸门，不等待 Python）。
//! 同时记录“关闸后被拒的派发尝试次数”，供 TEST-008 断言“关闸后的新输入计数恒 0”
//! （被拒尝试计入 rejected，而实际派发计数保持 0）。
//!
//! **线程安全**：内部以 `Mutex` 保护状态，全部方法取 `&self`——这样闸门可与
//! 派发互斥区**解耦**（`Arc<GateTable>` 独立共享），使关闭操作不被长动作阻塞。

use std::collections::HashMap;
use std::sync::Mutex;

/// 闸门内部状态。
struct GateInner {
    /// 全局关闭（应用退出/引擎清理）。
    closed_all: bool,
    /// 全局关闭原因。
    all_reason: Option<String>,
    /// 逐执行关闭：execution_id → reason。
    per_exec: HashMap<String, String>,
    /// 关闸后被拒的派发尝试计数（TEST-008 不变量观测点）。
    rejected: u64,
}

/// 输入闸门表（线程安全；`Arc` 共享）。
pub struct GateTable {
    inner: Mutex<GateInner>,
}

impl Default for GateTable {
    fn default() -> Self {
        Self::new()
    }
}

impl GateTable {
    /// 新建（全开）。
    pub fn new() -> Self {
        GateTable {
            inner: Mutex::new(GateInner {
                closed_all: false,
                all_reason: None,
                per_exec: HashMap::new(),
                rejected: 0,
            }),
        }
    }

    /// 关闭单个执行的闸门（幂等；保留首次原因）。
    pub fn close(&self, execution_id: &str, reason: &str) {
        let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        inner
            .per_exec
            .entry(execution_id.to_string())
            .or_insert_with(|| reason.to_string());
    }

    /// 关闭全局闸门（应用退出/引擎清理；幂等）。
    pub fn close_all(&self, reason: &str) {
        let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        if !inner.closed_all {
            inner.closed_all = true;
            inner.all_reason = Some(reason.to_string());
        }
    }

    /// 闸门是否对该执行开放（全局关或该执行关 → false）。
    pub fn is_open(&self, execution_id: &str) -> bool {
        let inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        !inner.closed_all && !inner.per_exec.contains_key(execution_id)
    }

    /// 全局闸门是否关闭。
    pub fn is_closed_all(&self) -> bool {
        self.inner.lock().unwrap_or_else(|p| p.into_inner()).closed_all
    }

    /// 该执行的关闭原因（未关闭 → None）。
    pub fn reason_for(&self, execution_id: &str) -> Option<String> {
        let inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        if inner.closed_all {
            return inner.all_reason.clone();
        }
        inner.per_exec.get(execution_id).cloned()
    }

    /// 记录一次“关闸后被拒的派发尝试”。
    pub fn note_rejected(&self) {
        let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        inner.rejected += 1;
    }

    /// 关闸后被拒的派发尝试计数。
    pub fn rejected_after_close(&self) -> u64 {
        self.inner.lock().unwrap_or_else(|p| p.into_inner()).rejected
    }
}
