//! 输入点/路径/几何的**校验**纯函数（TASK-017 / 06§坐标区域与输入不变量）。
//!
//! **权威在 Rust**（03：校验统一在引擎/宿主执行；Python 窄代理不做范围判断、
//! 不 clamp、不自动重试）。本模块为纯函数，供 [`super::model::RegionResolutionPort`]
//! 的生产实现与测试替身共用。
//!
//! **规则**（06§不变量）：
//! - 不变量 3：点必须落在**实际显示器集合**（不能用虚拟屏幕总包围盒替代——显示器
//!   之间可能有空隙）；
//! - 不变量 5：对动作整体验证，非法端点/路径拒绝，**不做 clamp**；
//! - 不变量 4：`geometry_revision` 变更后旧帧不可用于输入（旧帧 → `STALE_FRAME`）。
//!
//! **来源章节**：docs/06 §坐标区域与输入不变量、docs/02「显示器、窗口与运行
//! 辅助类型」、docs/03 §全双工与宿主调用。

use crate::ipc::IpcError;
use crate::types::region::Region;

use super::model::{InputContext, MonitorRect};

/// 点是否落在实际显示器集合内（半开矩形）。
pub fn point_on_displays(x: i64, y: i64, monitors: &[MonitorRect]) -> bool {
    monitors
        .iter()
        .any(|m| x >= m.x && x < m.x + m.width && y >= m.y && y < m.y + m.height)
}

/// 点是否落在区域矩形内（半开矩形）。
pub fn point_in_region(x: i64, y: i64, region: &Region) -> bool {
    x >= region.x
        && x < region.x.saturating_add(region.width)
        && y >= region.y
        && y < region.y.saturating_add(region.height)
}

/// 单点判定（越界/离屏 → `REGION_LIMIT_VIOLATION`，**无 clamp**）。
///
/// `allow_exit=false`（严格模式）：点须同时落在实际显示器集合**与**区域矩形内；
/// `allow_exit=true`：点只需落在实际显示器集合（允许离开区域，但不扩大识别区）。
pub fn evaluate_point(
    x: i64,
    y: i64,
    ctx: &InputContext,
    monitors: &[MonitorRect],
) -> Result<(), IpcError> {
    if !point_on_displays(x, y, monitors) {
        return Err(IpcError::region_limit_violation(&format!(
            "输入点 ({x},{y}) 不在实际显示器集合内"
        )));
    }
    if let Some(region) = &ctx.region {
        if !ctx.allow_exit && !point_in_region(x, y, region) {
            return Err(IpcError::region_limit_violation(&format!(
                "输入点 ({x},{y}) 超出严格区域，拒绝整笔动作（不 clamp）"
            )));
        }
    }
    Ok(())
}

/// Check each traversed physical pixel, including one-pixel gaps between monitors.
pub fn walk_segment(
    from: super::model::Point, to: super::model::Point,
    mut check: impl FnMut(super::model::Point) -> Result<(), IpcError>,
) -> Result<(), IpcError> {
    let distance = from.x.abs_diff(to.x).max(from.y.abs_diff(to.y));
    for step in 1..distance {
        let t = step as f64 / distance as f64;
        check(super::model::Point {
            x: (from.x as f64 + (to.x as f64 - from.x as f64) * t).round() as i64,
            y: (from.y as f64 + (to.y as f64 - from.y as f64) * t).round() as i64,
        })?;
    }
    Ok(())
}

/// `geometry_revision` 一致性判定（旧帧用于输入 → `STALE_FRAME`，retryable=true）。
///
/// `known_revision`：该绑定的当前几何版本（由 TASK-018 WindowManager 维护，经端口
/// 提供）；TASK-017 无窗口枚举，无绑定时传 None → 通过（“无绑定会话”）。
pub fn evaluate_geometry(
    binding_id: &str,
    geometry_revision: i64,
    known_revision: Option<i64>,
) -> Result<(), IpcError> {
    match known_revision {
        Some(current) if current != geometry_revision => Err(IpcError::stale_frame(&format!(
            "绑定 {binding_id} 的几何版本不匹配（当前 {current}，帧 {geometry_revision}）"
        ))),
        _ => Ok(()),
    }
}
