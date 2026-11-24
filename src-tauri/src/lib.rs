//! ScreenTrigger 桌面壳（Tauri 2 Rust Host）。
//!
//! 职责（设计说明 §2.1/§2.2）：壳与生命周期唯一所有权——
//! - `sidecar::Supervisor`：Python sidecar 进程树唯一所有者（spawn/清理/状态机）；
//! - `ipc`：协议路由层（Router 单线程 + 事件总线 + hostbridge，TASK-005）；
//! - `commands`：Tauri invoke 面（4 条运维命令 + ipc_request/ipc_cancel，§4.1/§5.1）；
//! - 应用退出前经 `RunEvent` 钩子执行同步清理序列（§2.5），保证无孤儿进程。

mod commands;
mod sidecar;

// TASK-005（ipc 协议层）：模块注册 + Router 接线（per-session Router 由
// Supervisor 在 engine_start 阶段三装配，见 sidecar::wire_router）。
pub mod ipc;

// TASK-006（共享类型）：三端同源类型 + 纯校验函数（契约测试读
// schema/contract-cases.json type_validation/config_patch 用例）。
pub mod types;

// TASK-009（图片导入）：文件选择器 token 的一次性签发与绑定（M01 import_image）。
pub mod import;

// TASK-008（框选覆盖层与主窗口恢复）：Rust 本地宿主能力（M01/F01）。
pub mod selection;

// TASK-017（Rust 输入闸门、租约与动作）：唯一物理输入所有者（M05）。
pub mod input;

use sidecar::SupervisorHandle;
use tauri::{Manager, RunEvent};

/// 组装并运行 Tauri 应用。
///
/// - `manage(SupervisorHandle)`：全局唯一 Supervisor（单一进程所有权）；
/// - `setup`：注入 UI 事件出口（Tauri emit 适配 → EventBus；契约④：事件名 =
///   协议 name，payload = 重分配 UI seq 后的完整信封）；
/// - `invoke_handler`：注册 §4.1 四条运维命令 + §5.1 ipc_request/ipc_cancel；
/// - `RunEvent::ExitRequested` / `RunEvent::Exit`：窗口关闭/应用退出时
///   执行 `shutdown_sync()`（幂等）——退出序列见设计 §2.5，确保 Job Object
///   兜底清理整树、不遗留 Python/worker 残留进程。
pub fn run() {
    tauri::Builder::default()
        .manage(SupervisorHandle::new())
        .manage(import::ImportTokenIssuer::ephemeral())
        .setup(|app| {
            // TASK-009：把导入 token 密钥存入进程全局，sidecar spawn 时以
            // ST_IMPORT_TOKEN_SECRET 注入，Python 侧据此校验 token 签名。
            if let Some(issuer) = app.try_state::<import::ImportTokenIssuer>() {
                import::install_shared_secret_hex(issuer.secret_hex());
            }
            // DEF-TASK008-01：host-frame token 密钥（每次运行随机；sidecar spawn 时以
            // ST_HOSTFRAME_TOKEN_SECRET 注入，Python 侧据此校验宿主帧 token 签名）。
            let hf_issuer = selection::hostframe::HostFrameIssuer::ephemeral();
            selection::hostframe::install_shared_secret(
                hf_issuer.secret().to_vec(),
                hf_issuer.ttl_seconds(),
            );
            // TASK-006 §2.5：release 数据根注入（Tauri app_local_data_dir →
            // sidecar --data-dir）；dev 隔离目录 / SIDECAR_DATA_DIR 覆盖分支
            // 见 sidecar::resolve_data_dir。解析失败时留空走 LOCALAPPDATA 兜底。
            if let Ok(dir) = app.path().app_local_data_dir() {
                sidecar::set_app_local_data_dir(dir);
            }
            // 事件出口注入（契约④：TASK-004 的 system_ready 同名直转已移除；
            // 事件经 ipc/eventbus.rs 统一重分配 UI seq 后在此 emit）。
            let handle = app.handle().clone();
            app.state::<SupervisorHandle>().set_event_emitter(
                std::sync::Arc::new(move |name: &str, value: &serde_json::Value| {
                    use tauri::Emitter;
                    let _ = handle.emit(name, value.clone());
                }),
            );
            // TASK-008：装配框选管理器（生产端口 = Tauri 窗口/显示器 + GDI 捕获）；
            // 事件经 RouterEventSink 注入当前会话 Router 通道（UI seq 由事件总线重分配）。
            let sink = std::sync::Arc::new(selection::RouterEventSink::new());
            selection::install_event_sink(std::sync::Arc::clone(&sink));
            let (manager, image_store) = selection::wiring::build_production_manager(
                app.handle().clone(),
                Box::new(selection::SharedEventSink(std::sync::Arc::clone(&sink))),
            );
            selection::install_manager(manager);
            app.manage(image_store);
            // TASK-017：装配输入管理器（唯一物理输入所有者，M05）；装配后
            // sidecar spawn 时注入 ST_INPUT_AVAILABLE=1 → hello.capabilities 的
            // input 据实置 available；wire_router 取共享实例关闸门/注册宿主真 handler。
            let input_manager = input::wiring::build_production_manager();
            input::install_manager(input_manager);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::engine_start,
            commands::engine_stop,
            commands::engine_status,
            commands::engine_diagnostics,
            commands::ipc_request,
            commands::ipc_cancel,
            import::pick_image_file,
            selection::wiring::selection_report_region,
            selection::wiring::selection_preview
        ])
        .build(tauri::generate_context!())
        .expect("Tauri 应用构建失败（检查 tauri.conf.json / capabilities / 图标）")
        .run(|app_handle, event| match event {
            RunEvent::ExitRequested { .. } => {
                // 窗口全部关闭触发的退出请求：先同步清理 sidecar，再放行退出。
                app_handle
                    .state::<SupervisorHandle>()
                    .shutdown_sync("app_exit");
            }
            RunEvent::Exit => {
                // 兜底：ExitRequested 中已清理则此处为 no-op（幂等）。
                app_handle
                    .state::<SupervisorHandle>()
                    .shutdown_sync("app_exit");
            }
            _ => {}
        });
}
