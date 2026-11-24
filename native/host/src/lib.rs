//! ScreenTrigger 桌面宿主核心。窗口、事件订阅与进程退出由桌面应用接入。

pub mod commands;
mod desktop_abi;
pub mod import;
pub mod input;
pub mod ipc;
pub mod selection;
pub mod sidecar;
pub mod types;
