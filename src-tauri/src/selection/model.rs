//! 框选模型与端口（TASK-008）：会话、端口 trait、错误与超时常量。
//!
//! 端口设计目的：把 **真实 OS 副作用**（隐藏/恢复主窗口、覆盖层窗口生命周期、
//! 干净桌面捕获、脚本暂停、事件投递、时钟、睡眠）隔离到可注入 trait，使
//! `SelectionController` 的状态机/顺序/边界可**确定性单测**（含事件顺序断言），
//! 真机 GUI 交互（真实拖拽）单独标 NOT_RUN。

use serde::{Deserialize, Serialize};

use crate::types::region::{DisplayInfo, ImageRef, Rect};

/// 05/06：主窗口隐藏或捕获准备超时 5s（无专码，映射 INTERNAL_ERROR）。
pub const PREPARE_TIMEOUT_MS: u64 = 5_000;

/// 06/F01：瞬时捕获失败只自动重试一次（间隔 200ms）。
pub const CAPTURE_RETRY_DELAY_MS: u64 = 200;

/// 实时预览建议频率（F01：建议 10Hz）。事件按最新帧有界投递。
pub const PREVIEW_HZ: u64 = 10;

/// 隐藏完成等待轮询间隔（实现细节，不入契约）。
pub const HIDE_POLL_INTERVAL_MS: u64 = 25;

/// 框选会话（02 `SelectionSession` 逐字）：结束后不复用 ID。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub struct SelectionSession {
    pub selection_id: String,
    pub display_id: String,
    pub status: String,
    pub started_at: String,
}

impl SelectionSession {
    /// 新建（status 恒为 "selecting"；02 仅此一态）。
    pub fn new(selection_id: String, display_id: String, started_at: String) -> Self {
        SelectionSession {
            selection_id,
            display_id,
            status: "selecting".to_string(),
            started_at,
        }
    }
}

/// 端口错误：`transient=true` 表示可自动重试一次（06：仅瞬时捕获失败重试）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PortError {
    pub transient: bool,
    pub message: String,
}

impl PortError {
    pub fn permanent(message: impl Into<String>) -> Self {
        PortError { transient: false, message: message.into() }
    }

    pub fn transient(message: impl Into<String>) -> Self {
        PortError { transient: true, message: message.into() }
    }
}

impl From<String> for PortError {
    fn from(message: String) -> Self {
        PortError::permanent(message)
    }
}

pub type PortResult<T> = Result<T, PortError>;

/// 覆盖层窗口句柄（不透明，由端口授予与回收）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OverlayHandle(pub String);

/// 脚本暂停请求结果（F01：运行期间开始框选先请求暂停并等待输入释放；无活动脚本继续）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PauseOutcome {
    /// 有活动脚本，已请求暂停。
    Paused,
    /// 无活动脚本 → 直接继续（不阻塞框选）。
    NoActiveScript,
}

/// 宿主环境端口：显示器枚举、主窗口可见性/几何、覆盖层窗口生命周期。
pub trait HostEnvironmentPort: Send {
    /// 枚举显示器（02 `DisplayInfo`；物理像素）。
    fn list_displays(&self) -> PortResult<Vec<DisplayInfo>>;
    /// 主窗口当前物理矩形（用于选默认显示器）。
    fn main_window_rect(&self) -> PortResult<Rect>;
    /// 主窗口当前是否可见。
    fn is_main_window_visible(&self) -> bool;
    /// 设置主窗口可见性（瞬间隐藏/恢复）。
    fn set_main_window_visible(&self, visible: bool) -> PortResult<()>;
    /// 在指定显示器打开覆盖层（WS_EX_LAYERED + WDA_EXCLUDEFROMCAPTURE，见 wiring）。
    fn open_overlay(&self, display: &DisplayInfo, selection_id: &str) -> PortResult<OverlayHandle>;
    /// 关闭覆盖层。
    fn close_overlay(&self, handle: &OverlayHandle) -> PortResult<()>;
}

/// 桌面捕获端口：干净桌面物理矩形 → ImageRef（不改变既有捕获语义）。
pub trait DesktopCapturePort: Send {
    /// 抓取某显示器上物理矩形 `region` 的干净桌面帧（不含覆盖层/主窗口）。
    fn capture(&self, display_id: &str, region: &Rect) -> PortResult<ImageRef>;
    /// 释放图像引用（幂等）。
    fn release(&self, image_id: &str) -> PortResult<()>;
}

/// 脚本控制端口（F01：开始框选先请求暂停；结束后不自动恢复）。
pub trait ScriptControlPort: Send {
    fn request_pause(&self) -> PortResult<PauseOutcome>;
}

/// 事件出口端口（selection_started/updated/completed/cancelled）。
/// 生产实现把事件注入 IPC 事件总线（UI seq 由总线统一重分配）。
pub trait EventSink: Send + Sync {
    fn emit(&self, name: &str, payload: serde_json::Value);
}

/// 时钟端口（墙钟，仅日志/`started_at`；RFC3339 UTC）。
pub trait ClockPort: Send {
    fn now_rfc3339(&self) -> String;
}

/// 睡眠端口（超时/重试等待；测试用假实现避免真实等待）。
pub trait Sleeper: Send {
    fn sleep_ms(&self, ms: u64);
}
