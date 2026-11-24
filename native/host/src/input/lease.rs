//! 输入租约表（TASK-017 / M05、06§并发）。
//!
//! **职责**：一根执行一个租约（幂等拒绝重复获取）；活动根租约并发上限 3
//! （06：3 活动根执行），第 4 个 → `BUSY`（retryable=true）。租约 id 为
//! `"lease-"+uuid4` 的不透明字符串。

use std::collections::HashMap;

use crate::ipc::IpcError;

use super::model::{InputContext, Lease, LeaseId, MAX_ROOT_LEASES};

/// 活动租约表（execution_id → Lease）。
#[derive(Default)]
pub struct LeaseTable {
    by_execution: HashMap<String, Lease>,
}

impl LeaseTable {
    /// 新建空表。
    pub fn new() -> Self {
        LeaseTable { by_execution: HashMap::new() }
    }

    /// 获取租约：一根一个；超出并发上限 → BUSY。返回 `lease_id`。
    pub fn acquire(&mut self, execution_id: &str, context: InputContext) -> Result<LeaseId, IpcError> {
        if self.by_execution.contains_key(execution_id) {
            return Err(IpcError::invalid_state("该执行已持有输入租约"));
        }
        if self.by_execution.len() >= MAX_ROOT_LEASES {
            return Err(IpcError::busy());
        }
        let lease_id = LeaseId::generate();
        self.by_execution.insert(
            execution_id.to_string(),
            Lease {
                lease_id: lease_id.clone(),
                execution_id: execution_id.to_string(),
                context,
            },
        );
        Ok(lease_id)
    }

    /// 释放租约（未持有 → None）。
    pub fn release(&mut self, execution_id: &str) -> Option<LeaseId> {
        self.by_execution.remove(execution_id).map(|l| l.lease_id)
    }

    /// 按 lease_id 查租约。
    pub fn get(&self, lease_id: &str) -> Option<&Lease> {
        self.by_execution.values().find(|l| l.lease_id.as_str() == lease_id)
    }

    /// 按 execution_id 查租约。
    pub fn get_by_execution(&self, execution_id: &str) -> Option<&Lease> {
        self.by_execution.get(execution_id)
    }

    /// 活动租约数。
    pub fn active_count(&self) -> usize {
        self.by_execution.len()
    }
}
