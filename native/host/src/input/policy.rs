//! Version-one script defaults are merged by the engine. The physical dispatch boundary
//! validates the effective fields again and samples each interval once per action ID.
use serde_json::{Map, Value};
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

use super::model::Point;

type Pair = [i64; 2];

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ActionPolicy {
    pub curve: Option<[Pair; 2]>,
    pub offset: Option<[Pair; 2]>,
    pub delay: Option<Pair>,
}

fn object<'a>(value: &'a Value, fields: &[&str], path: &str) -> Result<&'a Map<String, Value>, String> {
    let map = value.as_object().ok_or_else(|| path.to_owned())?;
    if let Some(name) = map.keys().find(|k| !fields.contains(&k.as_str())) {
        return Err(format!("{path}.{name}"));
    }
    Ok(map)
}

fn pair(value: Option<&Value>, low: i64, high: i64, ordered: bool, path: &str) -> Result<Pair, String> {
    let values = value.and_then(Value::as_array).filter(|v| v.len() == 2).ok_or_else(|| path.to_owned())?;
    let integer = |v: &Value| v.as_i64().filter(|n| (low..=high).contains(n)).ok_or_else(|| path.to_owned());
    let result = [integer(&values[0])?, integer(&values[1])?];
    if ordered && result[0] > result[1] { return Err(path.to_owned()); }
    Ok(result)
}

impl ActionPolicy {
    pub fn parse(value: &Value) -> Result<Self, String> {
        let map = object(value, &["version", "curve", "offset", "delay_ms"], "action_policy")?;
        if map.get("version").and_then(Value::as_u64) != Some(1) {
            return Err("action_policy.version".into());
        }
        let enabled = |name: &str| map.get(name).filter(|v| v.as_bool() != Some(false));
        let axes = |name: &str, fields: [&str; 2], ordered| -> Result<Option<[Pair; 2]>, String> {
            let Some(raw) = enabled(name) else { return Ok(None) };
            let path = format!("action_policy.{name}");
            let map = object(raw, &fields, &path)?;
            let axis = |key| pair(map.get(key), -32768, 32768, ordered, &format!("{path}.{key}"));
            Ok(Some([axis(fields[0])?, axis(fields[1])?]))
        };
        Ok(Self {
            curve: axes("curve", ["c1", "c2"], false)?,
            offset: axes("offset", ["x", "y"], true)?,
            delay: enabled("delay_ms").map(|v| pair(Some(v), 0, 5000, true, "action_policy.delay_ms")).transpose()?,
        })
    }

    pub fn validate_kind(&self, kind: &str) -> Result<(), String> {
        if self.curve.is_some() && !matches!(kind, "move" | "drag" | "click" | "click_anchor" | "drag_anchor") {
            return Err("action_policy.curve".into());
        }
        if self.offset.is_some() && !matches!(kind, "click" | "click_anchor") {
            return Err("action_policy.offset".into());
        }
        Ok(())
    }

    pub fn sample(&self, id: &str) -> SampledPolicy {
        let sample = |range: Pair, axis: &str| {
            let mut hash = DefaultHasher::new();
            (id, axis).hash(&mut hash);
            range[0] + (hash.finish() % (range[1] - range[0] + 1) as u64) as i64
        };
        SampledPolicy {
            curve: self.curve,
            offset: self.offset.map(|[x, y]| [sample(x, "x"), sample(y, "y")]).unwrap_or_default(),
            delay: self.delay.map(|r| sample(r, "delay") as u32).unwrap_or_default(),
        }
    }
}

#[derive(Default)]
pub struct SampledPolicy {
    pub curve: Option<[Pair; 2]>,
    pub offset: Pair,
    pub delay: u32,
}

impl SampledPolicy {
    /// Wait in small, cancellable intervals. The caller supplies the lease gate and clock.
    pub fn wait<E>(&self, mut check: impl FnMut() -> Result<(), E>, mut sleep: impl FnMut(u32)) -> Result<(), E> {
        let mut remaining = self.delay;
        while remaining != 0 {
            check()?;
            let interval = remaining.min(10);
            sleep(interval);
            remaining -= interval;
        }
        check()
    }

    pub fn trajectory(&self, from: Point, to: Point, slices: u32) -> Vec<Point> {
        let count = if self.curve.is_some() { slices.max(16) } else { slices };
        let axis = |a: i64, b: i64, i: usize, t: f64| {
            let a = a as f64;
            let b = b as f64;
            let result = match self.curve {
                None => a + (b - a) * t,
                Some([c1, c2]) => {
                    let u = 1.0 - t;
                    u*u*u*a + 3.0*u*u*t*(a + c1[i] as f64) + 3.0*u*t*t*(b + c2[i] as f64) + t*t*t*b
                }
            };
            result.round() as i64
        };
        (1..=count).map(|step| {
            if step == count { return to; }
            let t = step as f64 / count as f64;
            Point { x: axis(from.x, to.x, 0, t), y: axis(from.y, to.y, 1, t) }
        }).collect()
    }
}
