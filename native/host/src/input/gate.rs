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
    active: HashMap<String, Option<String>>,
    paused: std::collections::HashSet<String>,
    epoch: u64,
    freeze: bool,
    input_enabled: bool,
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
                active: HashMap::new(),
                paused: Default::default(),
                epoch: 0,
                freeze: false,
                input_enabled: true,
            }),
        }
    }

    pub fn register(&self, id: &str, binding: Option<String>) -> Result<(), crate::ipc::IpcError> {
        let mut state = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        if state.closed_all || state.freeze || state.per_exec.contains_key(id) {
            return Err(crate::ipc::IpcError::invalid_state("执行已停止"));
        }
        state.active.insert(id.into(), binding);
        Ok(())
    }
    pub fn unregister(&self, id: &str) {
        let mut state = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        state.active.remove(id);
        state.paused.remove(id);
        state.per_exec.remove(id);
    }
    pub fn active(&self, binding: Option<&str>) -> Vec<String> {
        let state = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        state
            .active
            .iter()
            .filter(|(_, b)| binding.is_none() || binding == b.as_deref())
            .map(|(id, _)| id.clone())
            .collect()
    }
    pub fn enable_input(&self, enabled: bool) {
        self.inner
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .input_enabled = enabled;
    }
    pub fn stop_active(&self) -> u64 {
        let mut st = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        st.epoch += 1;
        st.freeze = true;
        for id in st.active.keys().cloned().collect::<Vec<_>>() {
            st.per_exec.insert(id, "hotkey_stop".into());
        }
        st.epoch
    }
    pub fn finish_stop(&self, epoch: u64) {
        let mut st = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        if st.epoch == epoch {
            st.freeze = false;
        }
    }
    pub fn pause(&self, id: &str) {
        self.inner
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .paused
            .insert(id.into());
    }
    pub fn resume(&self, id: &str) {
        self.inner
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .paused
            .remove(id);
    }
    pub fn paused(&self, id: &str) -> bool {
        self.inner
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .paused
            .contains(id)
    }

    /// 关闭单个执行的闸门（幂等；保留首次原因）。
    pub fn close(&self, execution_id: &str, reason: &str) {
        let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        inner
            .per_exec
            .entry(execution_id.to_string())
            .or_insert_with(|| reason.to_string());
        if reason == "stop_script" {
            crate::measurements::mark("closed", execution_id);
        }
    }

    /// 关闭全局闸门（应用退出/引擎清理；幂等）。
    pub fn close_all(&self, reason: &str) {
        let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        if !inner.closed_all {
            inner.closed_all = true;
            inner.all_reason = Some(reason.to_string());
        }
    }

    /// Serialize one OS submission with close. Never sleep or wait for IPC in this section.
    pub fn submit<T>(
        &self,
        execution: &str,
        send: impl FnOnce() -> Result<T, crate::ipc::IpcError>,
    ) -> Result<T, crate::ipc::IpcError> {
        let mut state = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        if state.closed_all || state.freeze || state.per_exec.contains_key(execution) {
            state.rejected += 1;
            return Err(crate::ipc::IpcError::invalid_state("输入闸门已关闭"));
        }
        if !state.input_enabled {
            return Err(crate::ipc::IpcError::capability_denied("停止热键未注册"));
        }
        if state.paused.contains(execution) {
            return Err(crate::ipc::IpcError::window_unavailable("执行已暂停"));
        }
        let result = send();
        if result.is_ok() {
            crate::measurements::mark("submitted", execution);
        }
        result
    }

    /// 闸门是否对该执行开放（全局关或该执行关 → false）。
    pub fn is_open(&self, execution_id: &str) -> bool {
        let inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        !inner.closed_all && !inner.per_exec.contains_key(execution_id)
    }

    /// 全局闸门是否关闭。
    pub fn is_closed_all(&self) -> bool {
        self.inner
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .closed_all
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
        self.inner
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .rejected
    }
}
