use serde::Deserialize;
use serde_json::Value;

use super::model::{MonitorRect, Point};
use crate::ipc::IpcError;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    Window,
    Region,
    Display,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Strategy {
    Relative,
    Offset,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnchoredPoint {
    version: u32,
    pub scope: Scope,
    strategy: Strategy,
    reference_width: i32,
    reference_height: i32,
    x: i32,
    y: i32,
}

impl AnchoredPoint {
    pub fn parse(value: &Value) -> Result<Self, IpcError> {
        let a: Self = serde_json::from_value(value.clone())
            .map_err(|e| IpcError::invalid_params(&format!("action.anchor: {e}")))?;
        if a.version != 1 || a.reference_width < 1 || a.reference_height < 1 {
            return Err(IpcError::invalid_params("anchor 版本或参考尺寸无效"));
        }
        if !(0..a.reference_width).contains(&a.x) || !(0..a.reference_height).contains(&a.y) {
            return Err(IpcError::region_limit_violation("录制点超出参考范围"));
        }
        Ok(a)
    }

    pub fn resolve(&self, rect: MonitorRect) -> Result<Point, IpcError> {
        let axis = |p: i32, old: i32, new: i64, origin: i64| {
            if !(1..=i64::from(i32::MAX)).contains(&new) {
                return Err(IpcError::region_limit_violation("当前范围尺寸无效"));
            }
            let scaled = match self.strategy {
                Strategy::Offset => i64::from(p),
                Strategy::Relative if old == 1 => 0,
                Strategy::Relative => {
                    (i64::from(p) * (new - 1) + i64::from(old - 1) / 2) / i64::from(old - 1)
                }
            };
            if scaled >= new {
                return Err(IpcError::region_limit_violation("局部点超出当前范围"));
            }
            origin
                .checked_add(scaled)
                .filter(|v| i32::try_from(*v).is_ok())
                .ok_or_else(|| IpcError::region_limit_violation("物理坐标超出 Win32 范围"))
        };
        Ok(Point {
            x: axis(self.x, self.reference_width, rect.width, rect.x)?,
            y: axis(self.y, self.reference_height, rect.height, rect.y)?,
        })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ScopeSnapshot {
    pub rect: MonitorRect,
    pub displays: Vec<MonitorRect>,
}

impl ScopeSnapshot {
    pub fn new(rect: MonitorRect, mut displays: Vec<MonitorRect>) -> Result<Self, IpcError> {
        if !covered(rect, &displays) {
            return Err(IpcError::region_limit_violation(
                "当前范围跨出显示器或包含空隙",
            ));
        }
        displays.sort_by_key(|m| (m.x, m.y, m.width, m.height));
        Ok(Self { rect, displays })
    }
}

/// Verify the whole rectangle against the monitor union, including interior holes.
/// Between adjacent monitor x edges the covering set of vertical intervals is constant.
pub fn covered(rect: MonitorRect, displays: &[MonitorRect]) -> bool {
    let right = rect.x.saturating_add(rect.width);
    let bottom = rect.y.saturating_add(rect.height);
    if rect.x >= right || rect.y >= bottom {
        return false;
    }
    let mut edges = vec![rect.x];
    for d in displays {
        edges.extend(
            [d.x, d.x.saturating_add(d.width)]
                .into_iter()
                .filter(|x| rect.x < *x && *x < right),
        );
    }
    edges.into_iter().all(|x| {
        let mut spans: Vec<_> = displays
            .iter()
            .filter(|d| d.x <= x && x < d.x.saturating_add(d.width))
            .map(|d| (d.y, d.y.saturating_add(d.height)))
            .collect();
        spans.sort_unstable();
        let mut end = rect.y;
        for (top, next) in spans {
            if top > end {
                break;
            }
            end = end.max(next);
        }
        end >= bottom
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shared_anchor_vectors() {
        let cases: Vec<Value> =
            serde_json::from_str(include_str!("../../../../schema/anchor-cases.json")).unwrap();
        for case in cases {
            let r = &case["rect"];
            let rect = MonitorRect {
                x: r["x"].as_i64().unwrap(),
                y: r["y"].as_i64().unwrap(),
                width: r["width"].as_i64().unwrap(),
                height: r["height"].as_i64().unwrap(),
            };
            let actual = AnchoredPoint::parse(&case["anchor"]).and_then(|a| a.resolve(rect));
            let value = match actual {
                Ok(p) => serde_json::json!([p.x, p.y]),
                Err(e) => serde_json::json!(e.code),
            };
            assert_eq!(value, case["expected"], "{}", case["id"]);
        }
    }

    #[test]
    fn rejects_interior_hole_but_accepts_adjacent_displays() {
        let rect = MonitorRect {
            x: -100,
            y: 0,
            width: 200,
            height: 50,
        };
        let left = MonitorRect { width: 100, ..rect };
        let right = MonitorRect { x: 0, ..left };
        assert!(covered(rect, &[left, right]));
        assert!(!covered(rect, &[left, MonitorRect { x: 1, ..right }]));
    }

    #[test]
    fn overlay_dip_converts_once_before_relative_projection() {
        use crate::selection::geometry::local_dip_rect_to_physical;
        let a = AnchoredPoint::parse(&serde_json::json!({"version":1,"scope":"region",
            "strategy":"relative","reference_width":3,"reference_height":3,"x":1,"y":1}))
        .unwrap();
        for percent in [100, 125, 150, 200, 300] {
            let r = local_dip_rect_to_physical(
                (4., 8.),
                (84., 48.),
                (-1200, 60),
                percent as f64 / 100.,
            )
            .unwrap();
            let p = a
                .resolve(MonitorRect {
                    x: r.x,
                    y: r.y,
                    width: r.width,
                    height: r.height,
                })
                .unwrap();
            assert_eq!(
                (p.x, p.y),
                (-1200 + 44 * percent / 100, 60 + 28 * percent / 100)
            );
        }
    }
}
