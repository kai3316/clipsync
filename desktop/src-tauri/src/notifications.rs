use crate::bridge::Bridge;
use serde_json::Value;
use std::sync::Arc;
use tauri::AppHandle;
use tauri_plugin_notification::NotificationExt;

// Deliberately exclude history.changed: it has no incoming/outgoing provenance.
pub fn candidate(event: &Value) -> bool {
    match event["name"].as_str() {
        Some("transfer.request") => event["data"]["transfer_id"].as_str().is_some(),
        // A phone upload never passes through the peer-to-peer manager, so this
        // is the only signal the host gets that a file arrived.
        Some("transfer.web_upload") => event["data"]["name"].as_str().is_some(),
        // The legacy host polled for these transitions and notified on each
        // one; the code is what the user has to compare on both devices.
        Some("pairing.request") => {
            event["data"]["name"].as_str().is_some()
                && event["data"]["code"].as_str().is_some_and(|code| !code.is_empty())
        }
        Some("device.connected") | Some("device.disconnected") => {
            event["data"]["name"].as_str().is_some()
        }
        Some("chat.message") => {
            let entry = &event["data"]["entry"];
            entry["outgoing"] == false
                && (entry["kind"] == "text"
                    || (entry["kind"] == "file" && entry["status"] == "await_accept"))
        }
        _ => false,
    }
}

/// Fill a `{name}`/`{code}` template. Peer names are untrusted text: they are
/// inserted literally, never parsed or interpreted.
fn fill(template: &str, name: &str, code: &str) -> String {
    template.replace("{name}", name).replace("{code}", code)
}

fn body(event: &Value, response: &Value, chats: &Value, own: &str) -> Option<String> {
    // The body follows the same saved language as the tray (see crate::i18n).
    let strings = crate::i18n::strings(crate::i18n::locale_from_settings(response));
    let settings = &response["settings"];
    if settings["notifications_enabled"] != true || !candidate(event) {
        return None;
    }
    if event["name"] == "transfer.request" {
        return (settings["notify_transfer"] == true).then(|| strings.notify_transfer.to_owned());
    }
    // Legacy used the same `notify_transfer` switch and the "File Received"
    // title for a phone upload, so one preference governs both.
    if event["name"] == "transfer.web_upload" {
        return (settings["notify_transfer"] == true)
            .then(|| strings.notify_file_received.to_owned());
    }
    let name = event["data"]["name"].as_str().unwrap_or_default();
    if event["name"] == "pairing.request" {
        let code = event["data"]["code"].as_str().unwrap_or_default();
        return (settings["notify_pairing"] == true)
            .then(|| fill(strings.notify_pairing_request, name, code));
    }
    if event["name"] == "device.connected" {
        return (settings["notify_device_connect"] == true)
            .then(|| fill(strings.notify_device_connected, name, ""));
    }
    if event["name"] == "device.disconnected" {
        return (settings["notify_device_connect"] == true)
            .then(|| fill(strings.notify_device_disconnected, name, ""));
    }
    let sid = event["data"]["session_id"].as_str()?;
    let session = chats["sessions"]
        .as_array()?
        .iter()
        .find(|session| session["session_id"].as_str() == Some(sid))?;
    let peer = session["peer_id"].as_str()?;
    if own.is_empty()
        || peer.is_empty()
        || peer == own
        || chats["muted"]
            .as_array()?
            .iter()
            .any(|id| id.as_str() == Some(peer))
    {
        return None;
    }
    Some(strings.notify_chat.to_owned())
}

pub async fn deliver(bridge: &Arc<Bridge>, app: &AppHandle, event: &Value) {
    let empty = serde_json::json!({});
    let (chats, own) = if event["name"] == "chat.message" {
        let Ok(chats) = bridge.call("chat.sessions", empty.clone()).await else {
            return;
        };
        let Ok(status) = bridge.call("app.status", empty.clone()).await else {
            return;
        };
        (chats, status["device_id"].as_str().unwrap_or("").to_owned())
    } else {
        (Value::Null, String::new())
    };
    // Fetch last, not from renderer state or a startup cache. Locked/error states fail closed.
    let Ok(settings) = bridge.call("settings.get", empty).await else {
        return;
    };
    if bridge.is_stopping() {
        return;
    }
    if let Some(body) = body(event, &settings, &chats, &own) {
        if app
            .notification()
            .builder()
            .title("ClipSync")
            .body(body)
            .show()
            .is_err()
        {
            // Never log event payloads, peer names, paths or message contents.
            eprintln!("Native notification delivery failed");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn settings_get_response_contract_uses_nested_settings() {
        let event = json!({"name":"transfer.request","data":{"transfer_id":"t"}});
        // internal/web/api/settings.py returns {"settings": result}, not result.
        let response = json!({"settings":{
            "notifications_enabled":true, "notify_transfer":true, "device_name":"desktop"
        }});
        assert_eq!(
            body(&event, &response, &Value::Null, ""),
            Some("收到文件传输请求".to_owned())
        );
        let mut english = response.clone();
        english["settings"]["language"] = json!("en");
        assert_eq!(
            body(&event, &english, &Value::Null, ""),
            Some("Incoming file transfer request".to_owned())
        );
        for invalid in [
            response["settings"].clone(),
            json!({"settings":null}),
            json!({"settings":{"notifications_enabled":"true","notify_transfer":true}}),
            json!({"settings":{"notifications_enabled":false,"notify_transfer":true}}),
        ] {
            assert!(body(&event, &invalid, &Value::Null, "").is_none());
        }
    }

    #[test]
    fn chat_policy_fails_closed_and_never_previews_content() {
        let mut event = json!({"name":"chat.message","data":{"session_id":"s",
            "entry":{"kind":"text","outgoing":false,"text":"SECRET"}}});
        let mut settings = json!({"settings":{"notifications_enabled":true}});
        let mut chats = json!({"sessions":[{"session_id":"s","peer_id":"peer"}],"muted":[]});
        assert_eq!(
            body(&event, &settings, &chats, "self"),
            Some("收到新的聊天消息".to_owned())
        );
        settings["settings"]["language"] = json!("en");
        assert_eq!(
            body(&event, &settings, &chats, "self"),
            Some("New incoming chat message".to_owned())
        );
        settings["settings"]["language"] = json!("zh-CN");
        assert!(body(&event, &settings, &chats, "peer").is_none());
        assert!(body(&event, &settings, &chats, "").is_none());
        chats["muted"] = json!(["peer"]);
        assert!(body(&event, &settings, &chats, "self").is_none());
        chats["muted"] = json!([]);
        settings["settings"]["notifications_enabled"] = json!(false);
        assert!(body(&event, &settings, &chats, "self").is_none());
        settings["settings"]["notifications_enabled"] = json!(true);
        event["data"]["entry"]["outgoing"] = json!(true);
        assert!(body(&event, &settings, &chats, "self").is_none());
        event["data"]["entry"]["outgoing"] = Value::Null;
        assert!(!candidate(&event));
        assert!(body(&event, &Value::Null, &Value::Null, "self").is_none());
    }

    #[test]
    fn transfer_toggle_and_ambiguous_events() {
        let event =
            json!({"name":"transfer.request","data":{"transfer_id":"t","filename":"SECRET"}});
        let mut settings =
            json!({"settings":{"notifications_enabled":true,"notify_transfer":true}});
        assert_eq!(
            body(&event, &settings, &Value::Null, ""),
            Some("收到文件传输请求".to_owned())
        );
        settings["settings"]["notify_transfer"] = json!(false);
        assert!(body(&event, &settings, &Value::Null, "").is_none());
        for name in [
            "history.changed",
            "transfer.progress",
            "transfer.complete",
            "chat.sessions.changed",
        ] {
            assert!(!candidate(&json!({"name":name,"data":{}})));
        }
    }

    #[test]
    fn phone_uploads_notify_with_the_transfer_switch_and_never_the_name() {
        let event = json!({"name":"transfer.web_upload",
            "data":{"transfer_id":"t","name":"SECRET.pdf","size":12}});
        let mut settings =
            json!({"settings":{"notifications_enabled":true,"notify_transfer":true}});
        assert_eq!(body(&event, &settings, &Value::Null, ""), Some("收到文件".to_owned()));
        settings["settings"]["language"] = json!("en");
        assert_eq!(body(&event, &settings, &Value::Null, ""), Some("File received".to_owned()));
        // The same preference the peer-to-peer notification uses.
        settings["settings"]["notify_transfer"] = json!(false);
        assert!(body(&event, &settings, &Value::Null, "").is_none());
        // A name is required: the event only exists once a file was recorded.
        assert!(!candidate(
            &json!({"name":"transfer.web_upload","data":{"transfer_id":"t"}})
        ));
    }

    #[test]
    fn pairing_and_presence_notify_with_their_own_switches() {
        let mut settings = json!({"settings":{
            "notifications_enabled":true, "notify_pairing":true, "notify_device_connect":true
        }});
        let request = json!({"name":"pairing.request",
            "data":{"device_id":"p","name":"Pixel","code":"12345678"}});
        assert_eq!(
            body(&request, &settings, &Value::Null, ""),
            Some("设备 \"Pixel\" 请求配对 — 代码：12345678".to_owned())
        );
        settings["settings"]["language"] = json!("en");
        assert_eq!(
            body(&request, &settings, &Value::Null, ""),
            Some("Device \"Pixel\" wants to pair — code: 12345678".to_owned())
        );
        settings["settings"]["language"] = json!("zh-CN");
        settings["settings"]["notify_pairing"] = json!(false);
        assert!(body(&request, &settings, &Value::Null, "").is_none());
        // Presence uses the legacy connect switch, not the pairing one.
        settings["settings"]["notify_pairing"] = json!(true);
        settings["settings"]["notify_device_connect"] = json!(false);
        for name in ["device.connected", "device.disconnected"] {
            assert!(body(
                &json!({"name":name,"data":{"device_id":"p","name":"Pixel"}}),
                &settings,
                &Value::Null,
                ""
            )
            .is_none());
        }
        settings["settings"]["notify_device_connect"] = json!(true);
        assert_eq!(
            body(
                &json!({"name":"device.connected","data":{"device_id":"p","name":"Pixel"}}),
                &settings,
                &Value::Null,
                ""
            ),
            Some("Pixel 已连接".to_owned())
        );
        assert_eq!(
            body(
                &json!({"name":"device.disconnected","data":{"device_id":"p","name":"Pixel"}}),
                &settings,
                &Value::Null,
                ""
            ),
            Some("Pixel 已断开".to_owned())
        );
        // A code-less, empty-code or nameless frame is not a pairing request.
        for data in [
            json!({"name":"Pixel"}),
            json!({"name":"Pixel","code":""}),
            json!({"code":"12345678"}),
        ] {
            assert!(!candidate(&json!({"name":"pairing.request","data":data})));
        }
        assert!(!candidate(&json!({"name":"device.connected","data":{"device_id":"p"}})));
        // The master switch still gates everything.
        settings["settings"]["notifications_enabled"] = json!(false);
        assert!(body(
            &json!({"name":"device.connected","data":{"device_id":"p","name":"Pixel"}}),
            &settings,
            &Value::Null,
            ""
        )
        .is_none());
    }
}
