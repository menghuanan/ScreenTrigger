//! 按键/鼠标持有账本（TASK-017 / M05、06§不变量 6、07§5）。
//!
//! **职责**：记录本程序各执行持有的键与鼠标按钮；`key_down` 幂等；`release_owned`
//! 按**逆序** key-up/mouse-up 释放，且**只释放本程序持有的键/鼠标按钮**
//! （绝不触碰其它根或外部程序的按键）——账本是唯一依据，因此天然满足不变量 6。

use std::collections::HashMap;

use super::model::{InputPort, Key, MouseButton};

/// 持有账本（按 execution_id 分区）。
#[derive(Default)]
pub struct Ledger {
    keys: HashMap<String, Vec<Key>>,
    buttons: HashMap<String, Vec<MouseButton>>,
}

impl Ledger {
    /// 新建空账本。
    pub fn new() -> Self {
        Ledger { keys: HashMap::new(), buttons: HashMap::new() }
    }

    /// 记录一次按键按下（**幂等**：同名键重复 down 不重复登记）。
    pub fn record_key_down(&mut self, execution_id: &str, key: Key) {
        let list = self.keys.entry(execution_id.to_string()).or_default();
        if !list.iter().any(|k| k.name == key.name) {
            list.push(key);
        }
    }

    /// 记录一次按键抬起（移除登记；列表空则清理分区）。
    pub fn record_key_up(&mut self, execution_id: &str, key: &Key) {
        let empty = if let Some(list) = self.keys.get_mut(execution_id) {
            list.retain(|k| k.name != key.name);
            list.is_empty()
        } else {
            false
        };
        if empty {
            self.keys.remove(execution_id);
        }
    }

    /// 记录鼠标按钮按下（幂等）。
    pub fn record_button_down(&mut self, execution_id: &str, button: MouseButton) {
        let list = self.buttons.entry(execution_id.to_string()).or_default();
        if !list.contains(&button) {
            list.push(button);
        }
    }

    /// 记录鼠标按钮抬起。
    pub fn record_button_up(&mut self, execution_id: &str, button: MouseButton) {
        let empty = if let Some(list) = self.buttons.get_mut(execution_id) {
            list.retain(|b| *b != button);
            list.is_empty()
        } else {
            false
        };
        if empty {
            self.buttons.remove(execution_id);
        }
    }

    /// 该执行当前持有的键（按下顺序）。
    pub fn held_keys(&self, execution_id: &str) -> Vec<Key> {
        self.keys.get(execution_id).cloned().unwrap_or_default()
    }

    /// 该执行当前持有的鼠标按钮。
    pub fn held_buttons(&self, execution_id: &str) -> Vec<MouseButton> {
        self.buttons.get(execution_id).cloned().unwrap_or_default()
    }

    /// 释放该执行持有的全部键/鼠标按钮（**逆序**，best-effort），返回释放项数。
    ///
    /// 只把账本中登记（=本程序持有）的键/按钮交给端口；**绝不触碰**其它根或外部
    /// 程序的按键（06§不变量 6）。逆序释放由端口 [`InputPort::release_keys`] 保证。
    pub fn release_owned(&mut self, execution_id: &str, input: &dyn InputPort) -> usize {
        let buttons = self.buttons.remove(execution_id).unwrap_or_default();
        let keys = self.keys.remove(execution_id).unwrap_or_default();
        let count = keys.len() + buttons.len();
        if count > 0 {
            input.release_keys(execution_id, &keys, &buttons);
        }
        count
    }

    /// 释放所有执行持有的键/鼠标按钮（应用退出清理）。
    pub fn release_all(&mut self, input: &dyn InputPort) -> usize {
        let executions: Vec<String> = self
            .keys
            .keys()
            .chain(self.buttons.keys())
            .cloned()
            .collect();
        let mut count = 0usize;
        for execution_id in executions {
            count += self.release_owned(&execution_id, input);
        }
        count
    }
}
