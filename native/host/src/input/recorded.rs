use serde_json::Value;
use crate::ipc::IpcError;
use super::action::parse_key;
use super::anchor::AnchoredPoint;
use super::model::{InputAction, RecordedEdge, RecordedKey};

pub const MAX_MS: u32 = 30 * 60 * 1000;
const MAX_EVENTS: usize = 8192;

fn invalid(field: &str) -> IpcError {
    IpcError::invalid_params(&format!("录制动作参数无效：{field}"))
}

pub fn duration(value: &Value) -> Result<u32, IpcError> {
    value.as_u64().filter(|ms| *ms <= u64::from(MAX_MS))
        .map(|ms| ms as u32).ok_or_else(|| invalid("duration_ms"))
}

pub fn parse(kind: &str, value: &Value) -> Result<InputAction, IpcError> {
    let fields = value.as_object().ok_or_else(|| invalid("kind"))?;
    if fields.len() != 3 { return Err(invalid("fields")); }
    match kind {
        "scroll_anchor" => {
            let delta_raw = value["delta_raw"].as_i64().and_then(|i| i16::try_from(i).ok())
                .ok_or_else(|| invalid("delta_raw"))?;
            Ok(InputAction::ScrollAnchor { anchor: AnchoredPoint::parse(&value["anchor"])?, delta_raw })
        }
        "drag_anchor" => {
            let data = value["points"].as_array().filter(|p| (2..=MAX_EVENTS+2).contains(&p.len()))
                .ok_or_else(|| invalid("points"))?;
            let points = data.iter().map(AnchoredPoint::parse).collect::<Result<Vec<_>,_>>()?;
            if points.iter().any(|p| p.scope != points[0].scope) { return Err(invalid("points.scope")); }
            Ok(InputAction::DragAnchor { points, duration_ms: duration(&value["duration_ms"])? })
        }
        "recorded_keys" => {
            let scope = serde_json::from_value(value["scope"].clone()).map_err(|_| invalid("scope"))?;
            let data = value["events"].as_array().filter(|v| (2..=MAX_EVENTS).contains(&v.len()))
                .ok_or_else(|| invalid("events"))?;
            let mut held = Vec::new();
            let mut last = 0;
            let mut events = Vec::with_capacity(data.len());
            for (i, v) in data.iter().enumerate() {
                let obj = v.as_object().ok_or_else(|| invalid("events"))?;
                let offset_ms = duration(&v["offset_ms"])?;
                if offset_ms < last || (i == 0 && offset_ms != 0) { return Err(invalid("offset_ms")); }
                last = offset_ms;
                let down = v["down"].as_bool().ok_or_else(|| invalid("down"))?;
                let key = match (obj.len(), v["key"].as_str()) {
                    (3, Some(name)) => RecordedKey::Named(parse_key(name)?),
                    (4, None) => {
                        let code = v["scan_code"].as_u64().filter(|n| (1..=127).contains(n))
                            .ok_or_else(|| invalid("scan_code"))?;
                        let extended = v["extended"].as_bool().ok_or_else(|| invalid("extended"))?;
                        RecordedKey::Physical { scan_code: code as u16, extended }
                    }
                    _ => return Err(invalid("events.key")),
                };
                if down {
                    if !held.contains(&key) { held.push(key.clone()); }
                } else {
                    let index = held.iter().position(|k| k == &key).ok_or_else(|| invalid("events.pair"))?;
                    held.remove(index);
                    if held.is_empty() && i + 1 != data.len() { return Err(invalid("events.pair")); }
                }
                events.push(RecordedEdge { key, offset_ms, down });
            }
            if !held.is_empty() { return Err(invalid("events.pair")); }
            Ok(InputAction::RecordedKeys { scope, events })
        }
        _ => Err(invalid("kind")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn key_groups_validate_pairing_and_time_before_dispatch() {
        let down = json!({"offset_ms":0,"down":true,"scan_code":28,"extended":true});
        let up = json!({"offset_ms":12,"down":false,"scan_code":28,"extended":true});
        let group = |edges: Vec<Value>| json!({"kind":"recorded_keys","scope":"window","events":edges});
        assert!(parse("recorded_keys", &group(vec![down.clone(), down.clone(), up.clone()])).is_ok());
        let mut wrong_key = up.clone();
        wrong_key["extended"] = json!(false);
        let mut invalid_scan = down.clone();
        invalid_scan["scan_code"] = json!(128);
        let mut later = down.clone();
        later["offset_ms"] = json!(13);
        for edges in [vec![down.clone(), wrong_key], vec![invalid_scan, up.clone()],
                      vec![down.clone(), later, up.clone()], vec![down.clone(), down],
                      vec![up.clone(), up]] {
            assert_eq!(parse("recorded_keys", &group(edges)).unwrap_err().code, "INVALID_PARAMS");
        }
    }

    #[test]
    fn relative_drag_cannot_cross_scopes_and_wheel_preserves_partial_detents() {
        let anchor = json!({"version":1,"scope":"region","strategy":"relative",
                           "reference_width":20,"reference_height":30,"x":6,"y":8});
        let mut window = anchor.clone();
        window["scope"] = json!("window");
        let drag = json!({"kind":"drag_anchor","duration_ms":150,"points":[anchor,window]});
        assert!(parse("drag_anchor", &drag).is_err());
        let wheel = json!({"kind":"scroll_anchor","anchor":anchor,"delta_raw":-60});
        assert!(matches!(parse("scroll_anchor", &wheel), Ok(InputAction::ScrollAnchor { delta_raw:-60, .. })));
    }
}
