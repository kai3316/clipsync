use crate::error::BridgeError;
use serde::{
    de::{self, MapAccess, SeqAccess, Visitor},
    Deserialize, Deserializer,
};
use serde_json::{Map, Value};
use std::fmt;

// Deserialize recursively before building Value so duplicate keys cannot disappear.
struct Unique(Value);

impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Strict;
        impl<'de> Visitor<'de> for Strict {
            type Value = Unique;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("finite JSON with unique object keys")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Unique, A::Error> {
                let mut object = Map::new();
                while let Some((key, Unique(value))) = map.next_entry::<String, Unique>()? {
                    if object.insert(key, value).is_some() {
                        return Err(de::Error::custom("duplicate key"));
                    }
                }
                Ok(Unique(Value::Object(object)))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Unique, A::Error> {
                let mut values = Vec::new();
                while let Some(Unique(value)) = seq.next_element()? {
                    values.push(value);
                }
                Ok(Unique(Value::Array(values)))
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Unique, E> {
                serde_json::Number::from_f64(v)
                    .map(|n| Unique(Value::Number(n)))
                    .ok_or_else(|| de::Error::custom("non-finite number"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
        }
        deserializer.deserialize_any(Strict)
    }
}

pub fn invalid() -> BridgeError {
    BridgeError::new("PROTOCOL_ERROR", "Invalid sidecar frame")
}

pub fn decode(raw: &[u8]) -> Result<Value, BridgeError> {
    let Unique(value) = serde_json::from_slice(raw).map_err(|_| invalid())?;
    if !value.is_object() {
        return Err(invalid());
    }
    Ok(value)
}

fn fields(value: &Value, expected: &[&str]) -> bool {
    value
        .as_object()
        .is_some_and(|o| o.len() == expected.len() && expected.iter().all(|k| o.contains_key(*k)))
}

fn text(value: &Value) -> bool {
    value.as_str().is_some_and(|s| !s.is_empty())
}

fn error(value: &Value) -> bool {
    fields(value, &["code", "message", "retryable"])
        && text(&value["code"])
        && value["message"].is_string()
        && value["retryable"].is_boolean()
}

pub fn validate(value: &Value, session: Option<&str>) -> Result<(), BridgeError> {
    let valid = match value["type"].as_str() {
        Some("ready") => {
            session.is_none()
                && fields(value, &["type", "protocol", "session_id", "pid", "health"])
                && value["protocol"].as_u64() == Some(1)
                && value["session_id"]
                    .as_str()
                    .is_some_and(|s| uuid::Uuid::parse_str(s).is_ok())
                && value["pid"].as_u64().is_some_and(|p| p > 0)
                && text(&value["health"])
        }
        Some("fatal") => fields(value, &["type", "error"]) && error(&value["error"]),
        Some("recovered") => {
            // The one-shot recovery pass. It has no session by construction —
            // the running sidecar is exactly what could not start — so a frame
            // claiming to be one is rejected once a session exists.
            session.is_none() && fields(value, &["type", "items"]) && value["items"].is_array()
        }
        Some("response") => {
            session.is_some()
                && text(&value["id"])
                && match value["ok"].as_bool() {
                    Some(true) => {
                        fields(value, &["type", "id", "ok", "result"])
                            && value["result"].is_object()
                            && value["result"]
                                .get("session_id")
                                .is_none_or(|s| s.as_str() == session)
                            && value["result"]
                                .get("seq")
                                .is_none_or(|s| s.as_u64().is_some())
                    }
                    Some(false) => {
                        fields(value, &["type", "id", "ok", "error"]) && error(&value["error"])
                    }
                    None => false,
                }
        }
        Some("resync") => {
            session.is_some()
                && fields(value, &["type", "session_id"])
                && value["session_id"].as_str() == session
        }
        Some("event") => {
            session.is_some()
                && fields(
                    value,
                    &[
                        "type",
                        "name",
                        "session_id",
                        "seq",
                        "event_id",
                        "occurred_at",
                        "correlation_id",
                        "schema_version",
                        "data",
                    ],
                )
                && value["session_id"].as_str() == session
                && value["seq"].as_u64().is_some_and(|s| s > 0)
                && value["schema_version"].as_u64() == Some(1)
                && text(&value["name"])
                && text(&value["occurred_at"])
                && value["event_id"]
                    .as_str()
                    .is_some_and(|s| uuid::Uuid::parse_str(s).is_ok())
                && value["correlation_id"].is_string()
                && value["data"].is_object()
        }
        _ => false,
    };
    if valid {
        Ok(())
    } else {
        Err(invalid())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn rejects_ambiguous_json() {
        for raw in [
            r#"{"type":"ready","type":"fatal"}"#,
            r#"{"result":{"a":1,"a":2}}"#,
            r#"{"x":NaN}"#,
            r#"{"x":Infinity}"#,
            r#"{"x":1e999}"#,
            "[]",
            "{} {}",
            r#"{"x":[{"a":1,"a":2}]}"#,
        ] {
            assert!(decode(raw.as_bytes()).is_err(), "{raw}");
        }
        assert!(decode(&[0xff]).is_err());
        assert!(decode(br#"{"x":[1,true,null,"ok"]}"#).is_ok());
    }

    #[test]
    fn checks_envelope_types_and_sessions() {
        let session = "e1f13c26-1caf-4b03-876c-17d921bd7d49";
        let ready =
            json!({"type":"ready","protocol":1,"session_id":session,"pid":1,"health":"ready"});
        assert!(validate(&ready, None).is_ok());
        assert!(validate(&ready, Some(session)).is_err());
        let mut response = json!({"type":"response","id":"id","ok":true,"result":{}});
        assert!(validate(&response, None).is_err());
        assert!(validate(&response, Some(session)).is_ok());
        response["ok"] = json!(1);
        assert!(validate(&response, Some(session)).is_err());
        response["ok"] = json!(true);
        response["result"] = json!({"session_id":"other"});
        assert!(validate(&response, Some(session)).is_err());
        assert!(validate(
            &json!({"type":"resync","session_id":"other"}),
            Some(session)
        )
        .is_err());
        assert!(validate(
            &json!({"type":"fatal","error":{"code":"X","message":"x","retryable":"false"}}),
            None
        )
        .is_err());
    }

    #[test]
    fn accepts_a_recovery_report_only_without_a_session() {
        let session = "e1f13c26-1caf-4b03-876c-17d921bd7d49";
        let recovered = json!({"type":"recovered","items":[
            {"artifact":"config","reason":"unreadable","files":["config.json.corrupt-1"]}
        ]});
        assert!(validate(&recovered, None).is_ok());
        assert!(validate(&json!({"type":"recovered","items":[]}), None).is_ok());
        // A recovery report can only come from the one-shot pass, and never
        // with the extra field an error frame would need.
        assert!(validate(&recovered, Some(session)).is_err());
        assert!(validate(&json!({"type":"recovered"}), None).is_err());
        assert!(validate(&json!({"type":"recovered","items":{}}), None).is_err());
        assert!(validate(
            &json!({"type":"recovered","items":[],"error":{"code":"X","message":"x","retryable":false}}),
            None
        )
        .is_err());
    }
}
