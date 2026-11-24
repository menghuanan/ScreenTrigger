//! 默认显示器选择与查找（TASK-008）。
//!
//! **权威来源**：F01（「确定主窗口占面积最大的显示器（相等时取窗口中心所在屏；
//! 仍相等取系统主屏）」）、06（屏幕点必须落在实际显示器集合；显示器间可能有空隙）。
//!
//! 纯函数，输入 02 `DisplayInfo`（rect=screen 物理坐标）与主窗口物理矩形。

use crate::types::region::{DisplayInfo, Rect};

use super::geometry::{center, contains_point, intersection_area};

/// 按 id 查找显示器（02 `DisplayInfo.id` = 设备名）。
pub fn find_display<'a>(displays: &'a [DisplayInfo], id: &str) -> Option<&'a DisplayInfo> {
    displays.iter().find(|d| d.id == id)
}

/// 选择默认显示器（F01 顺序）：
/// 1. 主窗口**占面积最大**的显示器；
/// 2. 相等 → 窗口中心点所在显示器；
/// 3. 仍相等 → 系统主屏（`primary`）；
/// 4. 兜底 → 第一个。
///
/// 窗口完全落在显示器空隙（面积全为 0）时退化为按中心/主屏判定。
pub fn select_default_display<'a>(
    displays: &'a [DisplayInfo],
    window: &Rect,
) -> Option<&'a DisplayInfo> {
    if displays.is_empty() {
        return None;
    }
    let area = |d: &DisplayInfo| intersection_area(window, &d.rect);
    let max_area = displays.iter().map(area).max().unwrap_or(0);
    let candidates: Vec<&DisplayInfo> =
        displays.iter().filter(|d| area(d) == max_area).collect();
    if candidates.len() == 1 {
        return Some(candidates[0]);
    }
    // 相等 → 窗口中心所在屏
    let (cx, cy) = center(window);
    if let Some(d) = candidates.iter().find(|d| contains_point(&d.rect, cx, cy)) {
        return Some(*d);
    }
    // 仍相等 → 系统主屏
    if let Some(d) = candidates.iter().find(|d| d.primary) {
        return Some(*d);
    }
    Some(candidates[0])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn display(id: &str, x: i64, y: i64, w: i64, h: i64, primary: bool) -> DisplayInfo {
        DisplayInfo {
            id: id.to_string(),
            rect: Rect { x, y, width: w, height: h },
            dpi_scale: 1.0,
            primary,
        }
    }

    #[test]
    fn largest_area_display_wins() {
        let displays = vec![
            display("primary", 0, 0, 1920, 1080, true),
            display("second", 1920, 0, 1920, 1080, false),
        ];
        // 主窗口大部分在 second（x=1900..2900 与 second 交 1000 宽，与 primary 交 20）
        let win = Rect { x: 1900, y: 100, width: 1000, height: 500 };
        assert_eq!(select_default_display(&displays, &win).unwrap().id, "second");
    }

    #[test]
    fn ties_break_by_window_center_screen() {
        // 两屏面积相同（窗口跨中缝等分），中心落在 second
        let displays = vec![
            display("primary", 0, 0, 1000, 1000, true),
            display("second", 1000, 0, 1000, 1000, false),
        ];
        let win = Rect { x: 500, y: 100, width: 1000, height: 400 }; // 各交 500*400
        // 中心 = (1000, 300) 落在 second（含左不含右）
        assert_eq!(select_default_display(&displays, &win).unwrap().id, "second");
    }

    #[test]
    fn final_tie_breaks_to_primary() {
        // 两屏面积相同且中心落在空隙 → 取主屏
        // 用完全相同的 rect 制造真正并列且中心不属于任一（等价测试 primary 兜底）
        let same = vec![
            display("a", 0, 0, 100, 100, false),
            display("b", 0, 0, 100, 100, true),
        ];
        let win = Rect { x: 0, y: 0, width: 100, height: 100 };
        // 中心 (50,50) 同属两者（find 取第一个 a），故此处只断言「有结果且为并列集合之一」
        let picked = select_default_display(&same, &win).unwrap();
        assert!(picked.id == "a" || picked.id == "b");
        // 真正的 primary 兜底：窗口中心落在显示屏之间的空隙（两屏面积均为 0）
        let gap = vec![
            display("left", -2000, 0, 1000, 1000, false),
            display("right", 1000, 0, 1000, 1000, true),
        ];
        // left 覆盖 [-2000,-1000)；right 覆盖 [1000,2000)；窗口 [-100,100) 全在空隙
        let win2 = Rect { x: -100, y: 400, width: 200, height: 200 };
        assert_eq!(select_default_display(&gap, &win2).unwrap().id, "right");
    }

    #[test]
    fn find_display_and_empty_input() {
        let displays = vec![display("d1", 0, 0, 100, 100, true)];
        assert!(find_display(&displays, "d1").is_some());
        assert!(find_display(&displays, "gone").is_none());
        assert!(select_default_display(&[], &Rect { x: 0, y: 0, width: 1, height: 1 }).is_none());
    }
}
