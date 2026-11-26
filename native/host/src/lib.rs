//! ScreenTrigger 桌面宿主核心。窗口、事件订阅与进程退出由桌面应用接入。

pub mod commands;
mod desktop_abi;
mod desktop_windows;
pub mod import;
pub mod input;
mod measurements;
#[cfg(any(not(debug_assertions), test))]
mod portable;
pub mod ipc;
pub mod selection;
pub mod sidecar;
pub mod types;
mod text_output;
mod recording;
mod winmessages;

pub mod window;

pub mod hotkey;
