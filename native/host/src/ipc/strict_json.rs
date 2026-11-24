//! Decode once, preserving protocol semantics for duplicate keys at every nesting level.
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value};
use std::fmt;

pub(crate) struct StrictValue(pub Value);

impl<'de> Deserialize<'de> for StrictValue {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(JsonVisitor).map(StrictValue)
    }
}

struct JsonVisitor;
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn duplicate_and_nonfinite_values_are_rejected_before_routing() {
        for raw in [r#"{"params":{"x":1,"x":2}}"#, r#"{"x":1e999}"#, r#"{"x":NaN}"#] {
            assert!(serde_json::from_str::<StrictValue>(raw).is_err(), "{raw}");
        }
        assert!(serde_json::from_str::<StrictValue>(r#"{"x":[1,2,null]}"#).is_ok());
    }
}
impl<'de> Visitor<'de> for JsonVisitor {
    type Value = Value;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str("a JSON value with unique keys") }
    fn visit_bool<E: de::Error>(self, v: bool) -> Result<Value, E> { Ok(v.into()) }
    fn visit_i64<E: de::Error>(self, v: i64) -> Result<Value, E> { Ok(v.into()) }
    fn visit_u64<E: de::Error>(self, v: u64) -> Result<Value, E> { Ok(v.into()) }
    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Value, E> {
        serde_json::Number::from_f64(v).map(Value::Number).ok_or_else(|| E::custom("non-finite number"))
    }
    fn visit_str<E: de::Error>(self, v: &str) -> Result<Value, E> { Ok(v.into()) }
    fn visit_string<E: de::Error>(self, v: String) -> Result<Value, E> { Ok(Value::String(v)) }
    fn visit_unit<E: de::Error>(self) -> Result<Value, E> { Ok(Value::Null) }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value,A::Error> {
        let mut array = Vec::new();
        while let Some(StrictValue(value)) = seq.next_element()? { array.push(value); }
        Ok(Value::Array(array))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value,A::Error> {
        let mut object = Map::new();
        while let Some(key) = map.next_key::<String>()? {
            if object.contains_key(&key) { return Err(de::Error::custom(format!("duplicate JSON key: {key}"))); }
            object.insert(key, map.next_value::<StrictValue>()?.0);
        }
        Ok(Value::Object(object))
    }
}
