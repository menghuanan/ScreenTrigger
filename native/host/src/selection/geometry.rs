//! 框选几何：DPI 换算、拖动归一化、矩形/包含判定（TASK-008）。
//!
//! **权威来源**：07 §1（`local_dip_rect_to_physical` floor/ceil 公式、反向拖动先
//! 归一化、覆盖层实际物理原点 + 当前 scale）、06 §「坐标、区域与输入不变量」
//! 第 1 条（不能对虚拟屏幕绝对 x/y 直接乘 DPI；先转边界再求差值）、F01（反向
//! 拖动归一化、一次框选限一块显示器、区域完全落在选定显示器）。
//!
//! 全部为纯函数，不触碰 Win32/UI；物理像素、半开区间 `[x,x+w)×[y,y+h)`。

use crate::types::region::Rect;

/// 框选最小尺寸（06 硬限：宽高各 ≥10 物理像素）。
pub const MIN_SELECTION_SIZE: i64 = 10;

/// 几何运算错误（映射到 06 错误码由调用方负责）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GeometryError {
    /// scale 非正或非有限（07：scale 必须为正）。
    InvalidScale,
    /// 坐标含 NaN/Inf（不能作为物理像素）。
    NonFiniteCoordinate,
}

/// 反向拖动归一化：返回 `(lo, hi)`，`lo` 为逐分量较小者。
///
/// F01/07：从左下到右上、从右下到左上等反向拖动必须先归一化，不能出现负宽高。
pub fn normalize_drag(a: (f64, f64), b: (f64, f64)) -> ((f64, f64), (f64, f64)) {
    (
        (a.0.min(b.0), a.1.min(b.1)),
        (a.0.max(b.0), a.1.max(b.1)),
    )
}

/// 局部 DIP 矩形 → 物理矩形（07 §1 逐字公式）。
///
/// - `a`/`b` 为覆盖层局部 DIP 坐标（两对角点，顺序无关）；
/// - `origin` 为该覆盖层实际物理原点（**不是**虚拟屏幕绝对原点，06 第 1 条）；
/// - `scale` 为当前显示器缩放（>0）。
///
/// `left = origin.x + floor(min(a.x,b.x)*scale)`、`right = origin.x +
/// ceil(max(a.x,b.x)*scale)`，上下同理；宽高取边界差值（边缘左右 floor/ceil 避免
/// 少一列像素）。
pub fn local_dip_rect_to_physical(
    a: (f64, f64),
    b: (f64, f64),
    origin: (i64, i64),
    scale: f64,
) -> Result<Rect, GeometryError> {
    if !scale.is_finite() || scale <= 0.0 {
        return Err(GeometryError::InvalidScale);
    }
    for v in [a.0, a.1, b.0, b.1] {
        if !v.is_finite() {
            return Err(GeometryError::NonFiniteCoordinate);
        }
    }
    let (lo, hi) = normalize_drag(a, b);
    let left = origin.0 + (lo.0 * scale).floor() as i64;
    let top = origin.1 + (lo.1 * scale).floor() as i64;
    let right = origin.0 + (hi.0 * scale).ceil() as i64;
    let bottom = origin.1 + (hi.1 * scale).ceil() as i64;
    Ok(Rect {
        x: left,
        y: top,
        width: right - left,
        height: bottom - top,
    })
}

/// 是否达到 10×10 物理像素下限（06：宽高各 ≥10 才可确认）。
pub fn meets_minimum(rect: &Rect) -> bool {
    rect.width >= MIN_SELECTION_SIZE && rect.height >= MIN_SELECTION_SIZE
}

/// 点是否落在半开矩形内（含左/上，不含右/下）。
pub fn contains_point(rect: &Rect, x: i64, y: i64) -> bool {
    x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height
}

/// `inner` 是否完全落在 `outer` 内（半开区间，F01/06：区域须完全落在选定显示器）。
pub fn contains_rect(outer: &Rect, inner: &Rect) -> bool {
    if inner.width <= 0 || inner.height <= 0 {
        return false;
    }
    inner.x >= outer.x
        && inner.y >= outer.y
        && inner.x + inner.width <= outer.x + outer.width
        && inner.y + inner.height <= outer.y + outer.height
}

/// 两矩形半开区间交叠面积（0 表示不相交）；用于「主窗口占面积最大的显示器」。
pub fn intersection_area(a: &Rect, b: &Rect) -> i64 {
    let left = a.x.max(b.x);
    let top = a.y.max(b.y);
    let right = (a.x + a.width).min(b.x + b.width);
    let bottom = (a.y + a.height).min(b.y + b.height);
    if right <= left || bottom <= top {
        0
    } else {
        (right - left) * (bottom - top)
    }
}

/// 矩形中心点（用于「相等时取窗口中心所在屏」）。
pub fn center(rect: &Rect) -> (i64, i64) {
    (rect.x + rect.width / 2, rect.y + rect.height / 2)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn forward_drag_scales_with_floor_ceil() {
        // origin=(0,0), scale=1.0：DIP==物理
        let r = local_dip_rect_to_physical((10.0, 20.0), (110.0, 70.0), (0, 0), 1.0).unwrap();
        assert_eq!((r.x, r.y, r.width, r.height), (10, 20, 100, 50));
    }

    #[test]
    fn reverse_drag_is_normalized_positive_size() {
        // 从右下到左上拖动：顺序无关，宽高仍为正
        let r = local_dip_rect_to_physical((110.0, 70.0), (10.0, 20.0), (0, 0), 1.0).unwrap();
        assert_eq!((r.x, r.y, r.width, r.height), (10, 20, 100, 50));
        assert!(r.width > 0 && r.height > 0);
    }

    #[test]
    fn origin_is_overlay_physical_origin_not_virtual_screen() {
        // 覆盖层物理原点 (1920, -200)（负坐标第二屏），scale=1.0
        let r = local_dip_rect_to_physical((0.0, 0.0), (100.0, 100.0), (1920, -200), 1.0).unwrap();
        assert_eq!((r.x, r.y, r.width, r.height), (1920, -200, 100, 100));
    }

    #[test]
    fn fractional_dip_uses_floor_ceil_to_avoid_missing_column() {
        // scale=1.5：A=(10.3,10.3) B=(20.7,20.7)
        //   left = floor(10.3*1.5=15.45)=15；right = ceil(20.7*1.5=31.05)=32 → w=17
        let a = (10.3, 10.3);
        let b = (20.7, 20.7);
        let r = local_dip_rect_to_physical(a, b, (0, 0), 1.5).unwrap();
        assert_eq!((r.x, r.y), (15, 15));
        assert_eq!((r.width, r.height), (17, 17));
    }

    #[test]
    fn fractional_dip_with_nonzero_origin_never_multiplies_origin() {
        // 06 第 1 条：不能把覆盖层物理原点乘 scale。origin=(1920,0), scale=2.0
        //   left = 1920 + floor(10.0*2.0)=1940；right=1920+ceil(30.0*2.0)=1980 → w=40
        let r = local_dip_rect_to_physical((10.0, 10.0), (30.0, 30.0), (1920, 0), 2.0).unwrap();
        assert_eq!((r.x, r.y, r.width, r.height), (1940, 20, 40, 40));
        // 若误将 origin 乘 scale 会得到 3840+... 的荒谬值
        assert!(r.x < 2000, "原点不得被缩放");
    }

    #[test]
    fn invalid_scale_and_non_finite_rejected() {
        assert_eq!(
            local_dip_rect_to_physical((0.0, 0.0), (1.0, 1.0), (0, 0), 0.0),
            Err(GeometryError::InvalidScale)
        );
        assert_eq!(
            local_dip_rect_to_physical((0.0, 0.0), (1.0, 1.0), (0, 0), -1.0),
            Err(GeometryError::InvalidScale)
        );
        assert_eq!(
            local_dip_rect_to_physical((f64::NAN, 0.0), (1.0, 1.0), (0, 0), 1.0),
            Err(GeometryError::NonFiniteCoordinate)
        );
    }

    #[test]
    fn min_size_gate_matches_9x10_reject_10x10_accept() {
        assert!(!meets_minimum(&Rect { x: 0, y: 0, width: 9, height: 10 }));
        assert!(!meets_minimum(&Rect { x: 0, y: 0, width: 10, height: 9 }));
        assert!(meets_minimum(&Rect { x: 0, y: 0, width: 10, height: 10 }));
        assert!(meets_minimum(&Rect { x: 0, y: 0, width: 200, height: 120 }));
    }

    #[test]
    fn containment_is_half_open() {
        let outer = Rect { x: 0, y: 0, width: 100, height: 100 };
        // 完全在内
        assert!(contains_rect(&outer, &Rect { x: 10, y: 10, width: 80, height: 80 }));
        // 右边界恰好贴合（x+w=100 合法，半开）
        assert!(contains_rect(&outer, &Rect { x: 0, y: 0, width: 100, height: 100 }));
        // 越界 1 像素
        assert!(!contains_rect(&outer, &Rect { x: 10, y: 10, width: 91, height: 80 }));
        // 负原点（多屏）
        let neg = Rect { x: -1920, y: -200, width: 1920, height: 1080 };
        assert!(contains_rect(&neg, &Rect { x: -1900, y: 0, width: 10, height: 10 }));
    }

    #[test]
    fn intersection_area_and_center() {
        let a = Rect { x: 0, y: 0, width: 100, height: 100 };
        let b = Rect { x: 50, y: 50, width: 100, height: 100 };
        assert_eq!(intersection_area(&a, &b), 50 * 50);
        assert_eq!(intersection_area(&a, &Rect { x: 200, y: 0, width: 10, height: 10 }), 0);
        assert_eq!(center(&a), (50, 50));
    }
}
