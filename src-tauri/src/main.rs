// ScreenTrigger 桌面壳入口（TASK-004 设计 §3.3）。
// 实际 Builder 逻辑在 screen_trigger_lib::run()（lib.rs），便于集成测试复用。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    screen_trigger_lib::run()
}
