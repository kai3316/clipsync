#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod error;
mod i18n;
mod notifications;
mod protocol;
mod tray;

use bridge::Bridge;
use error::BridgeError;
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;
use tauri::{Manager, State, WebviewWindow, WindowEvent};
use tokio::sync::Mutex;
use tauri_plugin_autostart::ManagerExt;

/// How many times the host brings a ready-then-dead sidecar back before it
/// stops trying and leaves the retry to the user. The wait before attempt `n`
/// is `n * SIDECAR_RESTART_BACKOFF`, so a sidecar that dies instantly cannot
/// be respawned in a tight loop.
const SIDECAR_RESTART_ATTEMPTS: u32 = 3;
const SIDECAR_RESTART_BACKOFF: Duration = Duration::from_secs(2);

/// The single sidecar this process owns.
enum Slot {
    /// Never launched, or cleared by the user's manual retry.
    Idle,
    Live(Arc<Bridge>),
    /// The last launch or relaunch failed. Cached deliberately: a sidecar that
    /// cannot start must not be respawned by every command that arrives, so a
    /// fresh attempt takes either a manual retry or a new app run.
    Failed(BridgeError),
}

struct Host {
    bridge: Arc<Mutex<Slot>>,
    app: tauri::AppHandle,
    settings_update: Mutex<()>,
}

impl Host {
    async fn bridge(&self) -> Result<Arc<Bridge>, BridgeError> {
        let mut slot = self.bridge.lock().await;
        match &*slot {
            Slot::Live(bridge) => return Ok(bridge.clone()),
            Slot::Failed(error) => return Err(error.clone()),
            Slot::Idle => {}
        }
        // The lock spans the launch so concurrent commands join one sidecar and
        // a failure is recorded once. `Bridge::start` only spawns; readiness is
        // awaited per call, so this never blocks on the sidecar coming up.
        match Bridge::start(self.app.clone()).await {
            Ok(bridge) => {
                *slot = Slot::Live(bridge.clone());
                drop(slot);
                supervise(self.bridge.clone(), self.app.clone(), bridge.clone());
                Ok(bridge)
            }
            Err(error) => {
                *slot = Slot::Failed(error.clone());
                Err(error)
            }
        }
    }

    /// Drop the cached failure and start over, for the user's manual retry.
    async fn restart_bridge(&self) -> Result<(), BridgeError> {
        {
            let mut slot = self.bridge.lock().await;
            *slot = Slot::Idle;
        }
        self.bridge().await.map(|_| ())
    }

    /// Stop whatever bridge is live, ignoring a cached failure. Exit only.
    async fn stop_bridge(&self) {
        let bridge = {
            let slot = self.bridge.lock().await;
            match &*slot {
                Slot::Live(bridge) => Some(bridge.clone()),
                _ => None,
            }
        };
        if let Some(bridge) = bridge {
            bridge.stop().await;
        }
    }
}

fn emit_sidecar_state(app: &tauri::AppHandle, state: Value) {
    use tauri::Emitter;
    let _ = app.emit_to("main", "sidecar:state", state);
}

/// The wait before relaunch attempt `attempt` (1-based). The wait grows with
/// each attempt so a sidecar that dies the instant it starts cannot be
/// respawned in a tight loop.
fn restart_delay(attempt: u32) -> Duration {
    SIDECAR_RESTART_BACKOFF * attempt
}

/// Watch a live bridge and bring it back if it dies on its own.
///
/// Only a sidecar that *became ready* and then died is relaunched. A bridge
/// that never reports ready failed to launch — a missing binary, a Python that
/// is not installed — and relaunching that would fail the same way, so the
/// cached error stands, exactly as it did before supervision existed.
fn supervise(slot: Arc<Mutex<Slot>>, app: tauri::AppHandle, bridge: Arc<Bridge>) {
    tauri::async_runtime::spawn(async move {
        if bridge.wait_ready().await.is_err() {
            return;
        }
        let error = bridge.wait_failure().await;
        // Quit, restart and factory reset all stop the bridge on purpose.
        if bridge.is_stopping() {
            return;
        }
        relaunch(slot, app, bridge, error).await;
    });
}

async fn relaunch(
    slot: Arc<Mutex<Slot>>,
    app: tauri::AppHandle,
    dead: Arc<Bridge>,
    first: BridgeError,
) {
    let mut last = first;
    for attempt in 1..=SIDECAR_RESTART_ATTEMPTS {
        emit_sidecar_state(
            &app,
            json!({"state": "restarting", "attempt": attempt, "error": last.code}),
        );
        tokio::time::sleep(restart_delay(attempt)).await;
        // The lock spans the launch, so a command asking for the bridge during
        // an attempt either waits for this one or takes the next.
        let mut guard = slot.lock().await;
        // A manual retry may have installed a different bridge while this one
        // waited. Its own supervisor is already armed, so this one stands down.
        if let Slot::Live(bridge) = &*guard {
            if !Arc::ptr_eq(bridge, &dead) {
                return;
            }
        }
        match Bridge::start(app.clone()).await {
            Ok(bridge) => {
                *guard = Slot::Live(bridge.clone());
                drop(guard);
                emit_sidecar_state(&app, json!({"state": "ready", "attempt": attempt}));
                supervise(slot, app, bridge);
                return;
            }
            Err(error) => {
                last = error;
                *guard = Slot::Failed(last.clone());
            }
        }
    }
    // Out of attempts: leave the failure cached and let the window offer a
    // manual retry instead of hammering a sidecar that will not start.
    emit_sidecar_state(&app, json!({"state": "failed", "error": last.code}));
}

fn authorize(window: &WebviewWindow) -> Result<(), BridgeError> {
    if window.label() != "main" {
        return Err(BridgeError::new(
            "PERMISSION_DENIED",
            "Command is restricted to the main window",
        ));
    }
    Ok(())
}

#[tauri::command]
async fn get_app_status(
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("app.status", json!({})).await
}

#[tauri::command]
async fn companion_status(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("companion.status", json!({})).await
}

#[tauri::command]
async fn configure_companion(window: WebviewWindow, host: State<'_, Host>, enabled: bool, port: u16, rotate_token: bool) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if port == 0 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid companion port"));
    }
    host.bridge().await?.call("companion.configure", json!({
        "enabled": enabled, "port": port, "rotate_token": rotate_token,
    })).await
}

#[tauri::command]
async fn list_devices(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("devices.list", json!({})).await
}

#[tauri::command]
async fn get_settings(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("settings.get", json!({})).await
}

#[tauri::command]
async fn update_settings(
    window: WebviewWindow,
    host: State<'_, Host>,
    values: Value,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    let object = values.as_object().ok_or_else(|| {
        BridgeError::new("VALIDATION_ERROR", "Settings must be an object")
    })?;
    // The settings form submits every control it shows in one call; the cap is
    // a sanity bound on that payload, not a field list (the sidecar validates
    // names and values).
    if object.len() > 64 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Too many settings"));
    }
    // Keep the preference snapshot and any rollback in the same transaction.
    let _settings_guard = host.settings_update.lock().await;
    let previous_autostart = if values.get("auto_start").is_some() {
        Some(saved_autostart(&host.bridge().await?.call("settings.get", json!({})).await?)?)
    } else {
        None
    };
    let result = host.bridge().await?.call("settings.update", values.clone()).await?;
    if let Some(auto_start) = values.get("auto_start").and_then(Value::as_bool) {
        if let Err(error) = configure_autostart(&host.app, auto_start) {
            let rollback = json!({"auto_start": previous_autostart});
            if host.bridge().await?.call("settings.update", rollback).await.is_err() {
                return Err(BridgeError::new("AUTOSTART_ROLLBACK_FAILED",
                    "Auto-start failed and the previous preference could not be restored"));
            }
            return Err(error);
        }
    }
    Ok(result)
}

fn saved_autostart(settings: &Value) -> Result<bool, BridgeError> {
    settings.get("settings").and_then(|value| value.get("auto_start"))
        .and_then(Value::as_bool)
        .ok_or_else(|| BridgeError::new("AUTOSTART_FAILED", "Could not read previous auto-start preference"))
}

fn configure_autostart(app: &tauri::AppHandle, enabled: bool) -> Result<(), BridgeError> {
    if enabled && cfg!(debug_assertions) {
        return Err(BridgeError::new("AUTOSTART_UNAVAILABLE",
            "Auto-start requires an installed release build"));
    }
    #[cfg(target_os = "windows")]
    if !enabled && !windows_autostart_entry_exists()? {
        return Ok(());
    }
    let manager = app.autolaunch();
    let result = if enabled { manager.enable() } else { manager.disable() };
    result.map_err(|_| BridgeError::new("AUTOSTART_FAILED", "Could not update auto-start"))
}

#[cfg(target_os = "windows")]
fn windows_autostart_entry_exists() -> Result<bool, BridgeError> {
    use winreg::{enums::HKEY_CURRENT_USER, RegKey};
    let result = RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey(r"Software\Microsoft\Windows\CurrentVersion\Run")
        .and_then(|key| key.get_raw_value("ClipSync"));
    autostart_entry_exists(result)
}

fn autostart_entry_exists<T>(result: std::io::Result<T>) -> Result<bool, BridgeError> {
    match result {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(BridgeError::new("AUTOSTART_FAILED", "Could not inspect auto-start entry")),
    }
}

#[tauri::command]
async fn autostart_status(window: WebviewWindow, app: tauri::AppHandle) -> Result<bool, BridgeError> {
    authorize(&window)?;
    app.autolaunch().is_enabled()
        .map_err(|_| BridgeError::new("AUTOSTART_FAILED", "Could not query auto-start"))
}

#[tauri::command]
async fn translate_text(window: WebviewWindow, host: State<'_, Host>, text: String, target_lang: Option<String>, source_lang: Option<String>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if text.trim().is_empty() || text.chars().count() > 5000 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid text"));
    }
    host.bridge().await?.call("translate.text", json!({"text": text, "target_lang": target_lang.unwrap_or_else(|| "en".into()), "source_lang": source_lang.unwrap_or_else(|| "auto".into())})).await
}

#[tauri::command]
async fn ai_profiles(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("ai.profiles", json!({})).await
}

#[tauri::command]
async fn update_ai_profiles(window: WebviewWindow, host: State<'_, Host>, tools: Vec<String>, custom_paths: Vec<String>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("ai.profiles.update", json!({"tools": tools, "custom_paths": custom_paths})).await
}

#[tauri::command]
async fn ai_inventory(window: WebviewWindow, host: State<'_, Host>, refresh: bool, peer_id: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if peer_id.chars().count() > 128 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid peer id"));
    }
    host.bridge().await?.call("ai.inventory", json!({"refresh": refresh, "peer_id": peer_id})).await
}

#[tauri::command]
async fn ai_preview(window: WebviewWindow, host: State<'_, Host>, peer_id: String, tool: String, root: String, rel_path: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if peer_id.is_empty() || tool.is_empty() || rel_path.is_empty() || root.chars().count() > 256 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid AI config item"));
    }
    host.bridge().await?.call("ai.preview", json!({"peer_id": peer_id, "tool": tool, "root": root, "rel_path": rel_path})).await
}

#[tauri::command]
async fn ai_pull(window: WebviewWindow, host: State<'_, Host>, peer_id: String, items: Vec<Value>, mode: String, batch_id: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if peer_id.is_empty() || items.is_empty() || items.len() > 100
        || !matches!(mode.as_str(), "copy" | "overwrite" | "append")
        || batch_id.chars().count() > 128
    {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid AI pull request"));
    }
    host.bridge().await?.call("ai.pull", json!({"peer_id": peer_id, "items": items, "mode": mode, "batch_id": batch_id})).await
}

#[tauri::command]
async fn ai_local(window: WebviewWindow, host: State<'_, Host>, action: String, tool: String, root: String, rel_path: String, content: Option<String>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    let params = ai_local_params(&action, tool, root, rel_path, content)?;
    host.bridge().await?.call(&format!("ai.local.{}", action), params).await
}

fn ai_local_params(action: &str, tool: String, root: String, rel_path: String, content: Option<String>) -> Result<Value, BridgeError> {
    if !matches!(action, "listing" | "read" | "save" | "trash" | "open") {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid AI config action"));
    }
    if action == "listing" {
        return Ok(json!({}));
    }
    if tool.is_empty() || tool.chars().count() > 128
        || rel_path.is_empty() || rel_path.chars().count() > 4096
        || root.chars().count() > 256
        || (action == "save" && content.is_none())
        || (action != "save" && content.is_some())
        || content.as_ref().is_some_and(|text| text.len() > 262144 || text.contains('\0'))
    {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid AI config parameters"));
    }
    let mut params = json!({"tool": tool, "root": root, "rel_path": rel_path});
    if let Some(content) = content {
        params["content"] = Value::String(content);
    }
    Ok(params)
}

#[tauri::command]
async fn list_transfers(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("transfers.list", json!({})).await
}

#[tauri::command]
async fn send_files(
    window: WebviewWindow,
    host: State<'_, Host>,
    paths: Vec<String>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // One picked file is still a list of one: the picker is multi-select, and
    // the sidecar archives several picks into a single transfer.  The bounds
    // mirror the sidecar's own so a frame it would reject never leaves here.
    if paths.is_empty() || paths.len() > 64 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid file paths"));
    }
    if paths
        .iter()
        .any(|path| path.is_empty() || path.chars().count() > 4096)
    {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid file path"));
    }
    // An empty target is the all-peers broadcast; whether a named one is
    // actually connected is the runtime's call, not this layer's.
    if device_id.chars().count() > 128 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid identifier"));
    }
    host.bridge()
        .await?
        .call(
            "transfers.send",
            json!({"paths": paths, "device_id": device_id}),
        )
        .await
}

/// The strings for the locale the native surfaces are showing.
fn native_strings(window: &WebviewWindow) -> &'static i18n::Strings {
    window
        .app_handle()
        .state::<i18n::CurrentLocale>()
        .strings()
}

#[tauri::command]
async fn choose_file(window: WebviewWindow, kind: Option<String>) -> Result<Option<String>, BridgeError> {
    authorize(&window)?;
    let strings = native_strings(&window);
    let dialog = rfd::FileDialog::new();
    let dialog = match kind.as_deref() {
        Some("history") => dialog.add_filter(strings.filter_history, &["json", "csv"]),
        Some("backup") => dialog.add_filter(strings.filter_backup, &["zip"]),
        _ => dialog,
    };
    Ok(dialog.pick_file().map(|path| path.to_string_lossy().into_owned()))
}

/// Pick several files to send at once, the way the legacy picker did.
///
/// The legacy 发送文件 button opened a multi-select dialog and zipped whatever
/// came back into one archive; this answers the same question with the same
/// multiplicity, and cancelling is an empty list rather than an error.
#[tauri::command]
async fn choose_files(window: WebviewWindow) -> Result<Vec<String>, BridgeError> {
    authorize(&window)?;
    Ok(rfd::FileDialog::new()
        .pick_files()
        .map(|paths| {
            paths
                .into_iter()
                .map(|path| path.to_string_lossy().into_owned())
                .collect()
        })
        .unwrap_or_default())
}

/// Pick a folder to send, the way `choose_file` picks a file.
///
/// A folder is sent as one archive, so what the sidecar needs from here is the
/// path and nothing else — the archiving happens on the sidecar's side, where
/// the transfer machinery already lives.
#[tauri::command]
async fn choose_folder(window: WebviewWindow) -> Result<Option<String>, BridgeError> {
    authorize(&window)?;
    Ok(rfd::FileDialog::new()
        .pick_folder()
        .map(|path| path.to_string_lossy().into_owned()))
}

fn validate_transfer_action(action: &str, transfer_id: &str) -> Result<(), BridgeError> {
    if !matches!(
        action,
        "cancel" | "pause" | "resume" | "accept" | "reject" | "delete" | "retry"
            | "open" | "reveal"
    ) || transfer_id.is_empty()
        || transfer_id.chars().count() > 128
    {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid transfer action"));
    }
    Ok(())
}

#[tauri::command]
async fn transfer_action(
    window: WebviewWindow,
    host: State<'_, Host>,
    action: String,
    transfer_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_transfer_action(&action, &transfer_id)?;
    host.bridge()
        .await?
        .call("transfers.action", json!({"action": action, "transfer_id": transfer_id}))
        .await
}

/// Cancel every active transfer in one call.
///
/// The sidecar reads the live list itself, so a transfer that arrived since the
/// window last refreshed is cancelled too rather than left running behind a
/// "cancel all" the user has already confirmed. It takes no id, so there is
/// nothing here to validate.
#[tauri::command]
async fn cancel_all_transfers(
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("transfers.cancel_all", json!({})).await
}

/// Delete every finished transfer's record.
///
/// Records only: a running transfer is not a record, so nothing in flight is
/// disturbed. The sidecar counts what it deleted and this returns that count —
/// the page reports what happened, not the length of the list it last read,
/// which a transfer finishing since then would have made wrong.
#[tauri::command]
async fn clear_transfer_history(
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("transfers.clear_history", json!({})).await
}

#[tauri::command]
async fn start_speed_test(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("transfers.speed_test", json!({})).await
}

#[tauri::command]
async fn send_chat_file(window: WebviewWindow, host: State<'_, Host>, session_id: String, path: String) -> Result<Value, BridgeError> {
    validate_id(&session_id)?;
    chat_call(window, host, "chat.file", json!({"action":"send","session_id":session_id,"path":path,"transfer_id":""})).await
}

#[tauri::command]
async fn chat_file_action(window: WebviewWindow, host: State<'_, Host>, action: String, session_id: String, transfer_id: String) -> Result<Value, BridgeError> {
    validate_id(&session_id)?;
    if !matches!(action.as_str(), "accept" | "decline" | "cancel") {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid chat file action"));
    }
    chat_call(window, host, "chat.file", json!({"action":action,"session_id":session_id,"transfer_id":transfer_id,"path":""})).await
}

#[tauri::command]
async fn list_backups(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("backups.list", json!({})).await
}
#[tauri::command]
async fn create_backup(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("backups.create", json!({})).await
}
#[tauri::command]
async fn restore_backup(window: WebviewWindow, host: State<'_, Host>, path: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if path.is_empty() || path.chars().count() > 4096 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid backup path"));
    }
    host.bridge().await?.call("backups.restore", json!({"path": path})).await
}

#[tauri::command]
async fn export_history(window: WebviewWindow, host: State<'_, Host>, format: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if !matches!(format.as_str(), "json" | "csv" | "markdown") {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid export format"));
    }
    host.bridge().await?.call("history.export", json!({"format": format})).await
}
#[tauri::command]
async fn import_history(window: WebviewWindow, host: State<'_, Host>, path: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if path.is_empty() || path.chars().count() > 4096 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid import path"));
    }
    host.bridge().await?.call("history.import", json!({"path": path})).await
}

async fn chat_call(
    window: WebviewWindow,
    host: State<'_, Host>,
    method: &str,
    params: Value,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call(method, params).await
}

#[tauri::command]
async fn list_chat_devices(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    chat_call(window, host, "chat.devices", json!({})).await
}
#[tauri::command]
async fn list_chat_sessions(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    chat_call(window, host, "chat.sessions", json!({})).await
}

#[tauri::command]
async fn set_chat_muted(window: WebviewWindow, host: State<'_, Host>, peer_id: String, muted: bool) -> Result<Value, BridgeError> {
    validate_id(&peer_id)?;
    chat_call(window, host, "chat.mute", json!({"peer_id": peer_id, "muted": muted})).await
}
#[tauri::command]
async fn list_chat_messages(window: WebviewWindow, host: State<'_, Host>, session_id: String) -> Result<Value, BridgeError> {
    validate_id(&session_id)?;
    chat_call(window, host, "chat.messages", json!({"session_id": session_id})).await
}

#[tauri::command]
async fn open_chat_file(window: WebviewWindow, host: State<'_, Host>, session_id: String, transfer_id: String) -> Result<Value, BridgeError> {
    validate_id(&session_id)?;
    validate_id(&transfer_id)?;
    let value = chat_call(window, host, "chat.open_file", json!({"session_id": session_id, "transfer_id": transfer_id})).await?;
    let path = value.get("path").and_then(Value::as_str)
        .ok_or_else(|| BridgeError::new("NOT_FOUND", "Received file is not available"))?;
    #[cfg(target_os = "windows")]
    let mut command = std::process::Command::new("explorer");
    #[cfg(target_os = "macos")]
    let mut command = std::process::Command::new("open");
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = std::process::Command::new("xdg-open");
    command.arg(path).spawn().map_err(|_| BridgeError::new("OPEN_FAILED", "Could not open received file"))?;
    Ok(json!({"ok": true}))
}
#[tauri::command]
async fn invite_chat(window: WebviewWindow, host: State<'_, Host>, peer_id: String, peer_name: String) -> Result<Value, BridgeError> {
    validate_id(&peer_id)?;
    chat_call(window, host, "chat.invite", json!({"peer_id": peer_id, "peer_name": peer_name})).await
}
#[tauri::command]
async fn chat_action(window: WebviewWindow, host: State<'_, Host>, action: String, session_id: String, text: Option<String>) -> Result<Value, BridgeError> {
    validate_id(&session_id)?;
    if !matches!(action.as_str(), "send" | "accept" | "decline" | "read" | "close" | "resend") {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid chat action"));
    }
    chat_call(window, host, "chat.action", json!({"action": action, "session_id": session_id, "text": text.unwrap_or_default()})).await
}

macro_rules! chat_action_command {
    ($name:ident, $action:literal) => {
        #[tauri::command]
        async fn $name(window: WebviewWindow, host: State<'_, Host>, session_id: String) -> Result<Value, BridgeError> {
            chat_action(window, host, $action.into(), session_id, None).await
        }
    };
}
chat_action_command!(accept_chat_invite, "accept");
chat_action_command!(decline_chat_invite, "decline");
chat_action_command!(mark_chat_read, "read");
chat_action_command!(close_chat, "close");

#[tauri::command]
async fn send_chat_text(window: WebviewWindow, host: State<'_, Host>, session_id: String, text: String) -> Result<Value, BridgeError> {
    chat_action(window, host, "send".into(), session_id, Some(text)).await
}

#[tauri::command]
async fn resend_chat_text(window: WebviewWindow, host: State<'_, Host>, session_id: String, entry_id: String) -> Result<Value, BridgeError> {
    validate_id(&session_id)?;
    validate_id(&entry_id)?;
    chat_action(window, host, "resend".into(), session_id, Some(entry_id)).await
}

#[tauri::command]
async fn chat_typing(window: WebviewWindow, host: State<'_, Host>, session_id: String, typing: bool) -> Result<Value, BridgeError> {
    validate_id(&session_id)?;
    chat_call(window, host, "chat.typing", json!({"session_id": session_id, "typing": typing})).await
}

#[tauri::command]
async fn list_history(
    window: WebviewWindow,
    host: State<'_, Host>,
    query: String,
    offset: u32,
    limit: u32,
    kind: String,
    sort: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if query.chars().count() > 512
        || offset > 1_000_000
        || !(1..=100).contains(&limit)
        // The sidecar validates these against its own sets too; they are checked
        // here as well so a window bug is answered locally, and because the two
        // halves of a filter must agree — a chip and a sort the sidecar does not
        // know would otherwise come back as an unreadable protocol error.
        || !["all", "text", "image", "file", "link"].contains(&kind.as_str())
        || !["newest", "oldest"].contains(&sort.as_str())
    {
        return Err(BridgeError::new(
            "VALIDATION_ERROR",
            "Invalid history query",
        ));
    }
    host.bridge()
        .await?
        .call(
            "history.list",
            json!({"query": query, "offset": offset, "limit": limit, "kind": kind, "sort": sort}),
        )
        .await
}

#[tauri::command]
async fn unlock_app(
    window: WebviewWindow,
    host: State<'_, Host>,
    password: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if password.is_empty() || password.chars().count() > 1024 {
        return Err(BridgeError::new(
            "VALIDATION_ERROR",
            "Invalid password length",
        ));
    }
    host.bridge()
        .await?
        .call("app.unlock", json!({"password": password}))
        .await
}

#[tauri::command]
async fn delete_history(
    window: WebviewWindow,
    host: State<'_, Host>,
    entry_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&entry_id)?;
    host.bridge()
        .await?
        .call("history.delete", json!({"entry_id": entry_id}))
        .await
}

#[tauri::command]
async fn set_history_pinned(
    window: WebviewWindow,
    host: State<'_, Host>,
    entry_id: String,
    pinned: bool,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&entry_id)?;
    host.bridge()
        .await?
        .call(
            "history.set_pinned",
            json!({"entry_id": entry_id, "pinned": pinned}),
        )
        .await
}

fn validate_log_lines(lines: u32) -> Result<(), BridgeError> {
    if !(1..=1000).contains(&lines) {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid line count"));
    }
    Ok(())
}

/// The two OS repairs the diagnostics report's hints point at. Anything else is
/// refused here rather than forwarded to the sidecar.
fn validate_diagnostic_action(action: &str) -> Result<(), BridgeError> {
    if action != "firewall" && action != "local_network" {
        return Err(BridgeError::new(
            "VALIDATION_ERROR",
            "Invalid diagnostics action",
        ));
    }
    Ok(())
}

fn validate_id(id: &str) -> Result<(), BridgeError> {
    if id.is_empty() || id.chars().count() > 64 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid identifier"));
    }
    Ok(())
}

fn validate_batch_ids(entry_ids: &[String]) -> Result<(), BridgeError> {
    if !(1..=100).contains(&entry_ids.len()) {
        return Err(BridgeError::new(
            "VALIDATION_ERROR",
            "Expected between 1 and 100 identifiers",
        ));
    }
    let mut seen = std::collections::HashSet::with_capacity(entry_ids.len());
    for id in entry_ids {
        validate_id(id)?;
        if !seen.insert(id.as_str()) {
            return Err(BridgeError::new("VALIDATION_ERROR", "Duplicate identifier"));
        }
    }
    Ok(())
}

#[tauri::command]
async fn batch_pin_history(
    window: WebviewWindow,
    host: State<'_, Host>,
    entry_ids: Vec<String>,
    pinned: bool,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_batch_ids(&entry_ids)?;
    host.bridge()
        .await?
        .call(
            "history.batch_set_pinned",
            json!({"entry_ids": entry_ids, "pinned": pinned}),
        )
        .await
}

#[tauri::command]
async fn clear_history(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("history.clear", json!({})).await
}

#[tauri::command]
async fn batch_favorite_history(
    window: WebviewWindow,
    host: State<'_, Host>,
    entry_ids: Vec<String>,
    group: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_batch_ids(&entry_ids)?;
    if group.chars().count() > 128 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid favorite group"));
    }
    host.bridge()
        .await?
        .call(
            "favorites.batch_add",
            json!({"entry_ids": entry_ids, "group": group}),
        )
        .await
}

#[tauri::command]
async fn export_favorites(
    window: WebviewWindow,
    host: State<'_, Host>,
    format: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if !matches!(format.as_str(), "markdown" | "text") {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid export format"));
    }
    host.bridge()
        .await?
        .call("favorites.export", json!({"format": format}))
        .await
}

#[tauri::command]
async fn batch_delete_history(
    window: WebviewWindow,
    host: State<'_, Host>,
    entry_ids: Vec<String>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_batch_ids(&entry_ids)?;
    host.bridge()
        .await?
        .call("history.batch_delete", json!({"entry_ids": entry_ids}))
        .await
}

#[tauri::command]
async fn start_pairing(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("pairing.start", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn confirm_pairing(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
    code: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    if code.len() != 8 || !code.bytes().all(|b| b.is_ascii_digit()) {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid pairing code"));
    }
    host.bridge()
        .await?
        .call(
            "pairing.confirm",
            json!({"device_id": device_id, "code": code}),
        )
        .await
}

#[tauri::command]
async fn reject_pairing(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("pairing.reject", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn unpair_device(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("pairing.unpair", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn internet_pairing_status(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("internet_pairing.status", json!({})).await
}

#[tauri::command]
async fn internet_pairing_generate(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("internet_pairing.generate", json!({})).await
}

#[tauri::command]
async fn internet_pairing_enter(window: WebviewWindow, host: State<'_, Host>, code: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if code.trim().is_empty() || code.len() > 64 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid internet pairing code"));
    }
    host.bridge().await?.call("internet_pairing.enter", json!({"code": code})).await
}

#[tauri::command]
async fn internet_pairing_rename(window: WebviewWindow, host: State<'_, Host>, peer_id: String, name: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&peer_id)?;
    if name.chars().count() > 120 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid peer name"));
    }
    host.bridge().await?.call("internet_pairing.rename", json!({"peer_id": peer_id, "name": name})).await
}

#[tauri::command]
async fn internet_pairing_unpair(window: WebviewWindow, host: State<'_, Host>, peer_id: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&peer_id)?;
    host.bridge().await?.call("internet_pairing.unpair", json!({"peer_id": peer_id})).await
}

#[tauri::command]
async fn relay_delivery_status(window: WebviewWindow, host: State<'_, Host>, peer_id: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if !peer_id.is_empty() {
        validate_id(&peer_id)?;
    }
    host.bridge().await?.call("relay.delivery_status", json!({"peer_id": peer_id})).await
}

#[tauri::command]
async fn set_device_note(window: WebviewWindow, host: State<'_, Host>, device_id: String, note: String) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    if note.chars().count() > 512 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid device note"));
    }
    host.bridge().await?.call("devices.note", json!({"device_id": device_id, "note": note})).await
}

#[tauri::command]
async fn connect_device(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("devices.connect", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn disconnect_device(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("devices.disconnect", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn forget_device(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("devices.forget", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn restore_device(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("devices.restore", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn purge_device(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("devices.purge", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn test_device(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("devices.test", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn device_certs(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("devices.certs", json!({})).await
}

#[tauri::command]
async fn device_retrust(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("devices.retrust", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn send_url(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
    url: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    if url.is_empty() || url.chars().count() > 2048 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid URL"));
    }
    host.bridge()
        .await?
        .call("url.send", json!({"device_id": device_id, "url": url}))
        .await
}

#[tauri::command]
async fn push_text(
    window: WebviewWindow,
    host: State<'_, Host>,
    text: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if text.trim().is_empty() || text.chars().count() > 100000 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid text"));
    }
    host.bridge()
        .await?
        .call("clipboard.push", json!({"text": text}))
        .await
}

#[tauri::command]
async fn discovery_status(
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("discovery.status", json!({})).await
}

#[tauri::command]
async fn set_discovery_enabled(
    window: WebviewWindow,
    host: State<'_, Host>,
    enabled: bool,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge()
        .await?
        .call("discovery.set_enabled", json!({"enabled": enabled}))
        .await
}

#[tauri::command]
async fn set_discovery_visible(
    window: WebviewWindow,
    host: State<'_, Host>,
    enabled: bool,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge()
        .await?
        .call("discovery.set_visible", json!({"enabled": enabled}))
        .await
}

#[tauri::command]
async fn set_sync_enabled(
    window: WebviewWindow,
    host: State<'_, Host>,
    enabled: bool,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge()
        .await?
        .call("sync.set_enabled", json!({"enabled": enabled}))
        .await
}

#[tauri::command]
async fn pause_sync(window: WebviewWindow, host: State<'_, Host>, minutes: i64) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if !(1..=1440).contains(&minutes) {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid pause duration"));
    }
    host.bridge().await?.call("sync.pause", json!({"minutes": minutes})).await
}

#[tauri::command]
async fn resume_sync(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("sync.resume", json!({})).await
}

#[tauri::command]
async fn copy_history(
    window: WebviewWindow,
    host: State<'_, Host>,
    entry_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&entry_id)?;
    host.bridge()
        .await?
        .call("history.copy", json!({"entry_id": entry_id}))
        .await
}

#[tauri::command]
async fn read_history_text(
    window: WebviewWindow,
    host: State<'_, Host>,
    entry_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&entry_id)?;
    host.bridge()
        .await?
        .call("history.text", json!({"entry_id": entry_id}))
        .await
}

#[tauri::command]
async fn open_history_link(
    window: WebviewWindow,
    host: State<'_, Host>,
    entry_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&entry_id)?;
    host.bridge()
        .await?
        .call("history.open_link", json!({"entry_id": entry_id}))
        .await
}

#[tauri::command]
async fn list_favorites(
    window: WebviewWindow,
    host: State<'_, Host>,
    query: String,
    group: String,
    offset: u32,
    limit: u32,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_favorite_query(&query, &group, offset, limit)?;
    host.bridge()
        .await?
        .call(
            "favorites.list",
            json!({"query": query, "group": group, "offset": offset, "limit": limit}),
        )
        .await
}

fn validate_favorite_query(
    query: &str,
    group: &str,
    offset: u32,
    limit: u32,
) -> Result<(), BridgeError> {
    if query.chars().count() > 512
        || group.chars().count() > 128
        || offset > 1_000_000
        || !(1..=100).contains(&limit)
    {
        return Err(BridgeError::new(
            "VALIDATION_ERROR",
            "Invalid favorites query",
        ));
    }
    Ok(())
}

fn validate_favorite_fields(title: &str, content: &str, group: &str) -> Result<(), BridgeError> {
    if title.chars().count() > 256
        || content.chars().count() > 65_536
        || group.chars().count() > 128
    {
        return Err(BridgeError::new(
            "VALIDATION_ERROR",
            "Invalid favorite fields",
        ));
    }
    Ok(())
}

fn validate_favorite_position(position: i64) -> Result<(), BridgeError> {
    if !(0..=1_000_000).contains(&position) {
        return Err(BridgeError::new(
            "VALIDATION_ERROR",
            "Invalid favorite position",
        ));
    }
    Ok(())
}

#[tauri::command]
async fn get_favorite(
    window: WebviewWindow,
    host: State<'_, Host>,
    favorite_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&favorite_id)?;
    host.bridge()
        .await?
        .call("favorites.get", json!({"favorite_id": favorite_id}))
        .await
}

#[tauri::command]
async fn add_favorite(
    window: WebviewWindow,
    host: State<'_, Host>,
    title: String,
    content: String,
    group: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_favorite_fields(&title, &content, &group)?;
    host.bridge()
        .await?
        .call(
            "favorites.add",
            json!({"title": title, "content": content, "group": group}),
        )
        .await
}

#[tauri::command]
async fn update_favorite(
    window: WebviewWindow,
    host: State<'_, Host>,
    favorite_id: String,
    title: String,
    content: String,
    group: String,
    position: i64,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&favorite_id)?;
    validate_favorite_fields(&title, &content, &group)?;
    validate_favorite_position(position)?;
    host.bridge()
        .await?
        .call(
            "favorites.update",
            json!({"favorite_id": favorite_id, "title": title, "content": content, "group": group, "position": position}),
        )
        .await
}

#[tauri::command]
async fn delete_favorite(
    window: WebviewWindow,
    host: State<'_, Host>,
    favorite_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&favorite_id)?;
    host.bridge()
        .await?
        .call("favorites.delete", json!({"favorite_id": favorite_id}))
        .await
}

#[tauri::command]
async fn copy_favorite(
    window: WebviewWindow,
    host: State<'_, Host>,
    favorite_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&favorite_id)?;
    host.bridge()
        .await?
        .call("favorites.copy", json!({"favorite_id": favorite_id}))
        .await
}

#[tauri::command]
async fn read_logs(
    window: WebviewWindow,
    host: State<'_, Host>,
    lines: u32,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_log_lines(lines)?;
    host.bridge()
        .await?
        .call("logs.tail", json!({"lines": lines}))
        .await
}

#[tauri::command]
async fn restart_app(
    window: WebviewWindow,
    app: tauri::AppHandle,
    host: State<'_, Host>,
) -> Result<(), BridgeError> {
    authorize(&window)?;
    // Stop the sidecar before relaunching: `restart` replaces the process, so
    // an unreleased data lock or orphaned child would block the new instance.
    if let Ok(bridge) = host.bridge().await {
        bridge.stop().await;
    }
    app.restart();
}

/// Start the sidecar again after the automatic relaunches ran out, or after a
/// launch failure that was cached deliberately. Unlike `restart_app` this
/// replaces only the background process: the window and its state survive, so
/// a transient sidecar problem costs no more than a click.
#[tauri::command]
async fn restart_sidecar(window: WebviewWindow, host: State<'_, Host>) -> Result<(), BridgeError> {
    authorize(&window)?;
    host.restart_bridge().await
}

/// Move damaged configuration, identity or history aside so the app can start.
///
/// The sidecar cannot be asked to do this: it refuses to start on exactly the
/// damage being repaired, which is what left a damaged install with no way out
/// — `factory_reset` is an RPC call and needs the process that will not start.
/// The host therefore stops the sidecar, runs the repair pass as a one-shot
/// child, and brings a fresh sidecar up on the repaired directory.
#[tauri::command]
async fn recover_data_dir(
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // Hold the slot for the whole pass so no command can launch a sidecar that
    // reopens the very files the repair is about to move.
    let mut slot = host.bridge.lock().await;
    let live = match &*slot {
        Slot::Live(bridge) => Some(bridge.clone()),
        _ => None,
    };
    *slot = Slot::Idle;
    if let Some(bridge) = live {
        bridge.stop().await;
    }
    let items = bridge::recover().await;
    drop(slot);
    // The sidecar was stopped for the pass, so bring one back whatever happened:
    // a failed repair must not leave the window with no background process. The
    // repair's own failure is reported first — it is the actionable one, and a
    // sidecar that will not come back already has the window's retry.
    let items = items?;
    host.restart_bridge().await?;
    Ok(json!({ "items": items }))
}

#[tauri::command]
async fn factory_reset(
    window: WebviewWindow,
    app: tauri::AppHandle,
    host: State<'_, Host>,
) -> Result<(), BridgeError> {
    authorize(&window)?;
    // The sidecar stops its own services and clears the data directory; it
    // cannot restart itself, and the webview's own storage (theme, onboarding,
    // layout) lives outside the sidecar's reach — clear both here, then hand
    // the relaunch to the same path restart_app uses.
    host.bridge()
        .await?
        .call("app.factory_reset", json!({}))
        .await?;
    let _ = window.eval("try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}");
    if let Ok(bridge) = host.bridge().await {
        bridge.stop().await;
    }
    app.restart();
}

#[tauri::command]
async fn diagnostics_report(
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge()
        .await?
        .call("diagnostics.report", json!({}))
        .await
}

#[tauri::command]
async fn diagnostics_request(
    window: WebviewWindow,
    host: State<'_, Host>,
    action: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_diagnostic_action(&action)?;
    host.bridge()
        .await?
        .call("diagnostics.request", json!({"action": action}))
        .await
}

#[tauri::command]
async fn update_check(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // The sidecar bounds the release lookup itself (~8s) so a blocked GitHub
    // cannot pin this call; it answers "no update" instead of failing.
    host.bridge().await?.call("update.check", json!({})).await
}

#[tauri::command]
async fn update_status(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("update.status", json!({})).await
}

#[tauri::command]
async fn update_download(
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // Fire-and-forget: progress arrives as `update.state` events, the result
    // only says whether a run started.
    host.bridge().await?.call("update.download", json!({})).await
}

#[tauri::command]
async fn update_open_folder(
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // The archive path is owned by the sidecar; no path crosses the boundary.
    host.bridge().await?.call("update.open_folder", json!({})).await
}

#[tauri::command]
async fn open_data_folder(
    window: WebviewWindow,
    host: State<'_, Host>,
    which: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if !matches!(which.as_str(), "data" | "backups") {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid data folder"));
    }
    // The folder is chosen by the sidecar; no path crosses the boundary.
    host.bridge()
        .await?
        .call("data.open_folder", json!({"which": which}))
        .await
}

fn log_export_name(name: Option<&str>) -> Option<&str> {
    // Only a suggested file name for the save dialog — the destination itself
    // is whatever the user picks there. Separators and NUL are refused so a
    // crafted value cannot steer the dialog out of its directory.
    let name = name?;
    if name.is_empty()
        || name.chars().count() > 128
        || name.contains('/')
        || name.contains('\\')
        || name.contains('\0')
    {
        return None;
    }
    Some(name)
}

#[tauri::command]
async fn export_logs(
    window: WebviewWindow,
    host: State<'_, Host>,
    default_name: Option<String>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // The user picks the destination in a native save dialog; only then does
    // the sidecar copy its log there, so no WebView-supplied path is written to.
    let mut dialog = rfd::FileDialog::new()
        .add_filter(native_strings(&window).filter_log, &["log", "txt"]);
    if let Some(name) = log_export_name(default_name.as_deref()) {
        dialog = dialog.set_file_name(name);
    }
    let Some(path) = dialog.save_file() else {
        return Ok(json!({"cancelled": true}));
    };
    host.bridge()
        .await?
        .call("logs.export", json!({"dest": path.to_string_lossy()}))
        .await
}

#[tauri::command]
async fn companion_qr(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // The address (and its token) is built by the sidecar; the WebView only
    // renders what comes back.
    host.bridge().await?.call("companion.qr", json!({})).await
}

fn validate_about_target(target: &str) -> Result<(), BridgeError> {
    // Closed target list: the WebView names a target, never a URL.
    if !matches!(target, "homepage" | "releases") {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid link target"));
    }
    Ok(())
}

#[tauri::command]
async fn open_about_link(
    window: WebviewWindow,
    host: State<'_, Host>,
    target: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_about_target(&target)?;
    host.bridge()
        .await?
        .call("app.open_link", json!({"target": target}))
        .await
}

/// Put the window away, leaving the session running.
///
/// The window's own footer offers this rather than quitting: closing the window
/// already hides it to the tray and the tray carries 退出 ClipSync, so a
/// minimize is the one window action that was not reachable from inside the
/// window itself.
#[tauri::command]
async fn minimize_app(window: WebviewWindow) -> Result<(), BridgeError> {
    authorize(&window)?;
    window
        .minimize()
        .map_err(|_| BridgeError::new("WINDOW_ERROR", "Could not minimize the window"))
}

#[tauri::command]
async fn quit_app(
    window: WebviewWindow,
    app: tauri::AppHandle,
    host: State<'_, Host>,
) -> Result<(), BridgeError> {
    authorize(&window)?;
    if let Ok(bridge) = host.bridge().await {
        bridge.stop().await;
    }
    app.exit(0);
    Ok(())
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Show the window and ask it to open one of its own surfaces.
///
/// The tray's window actions are the window's actions — sending a URL, settings,
/// exporting logs, checking for updates are the same four buttons.  A second
/// implementation in a second toolkit would be a second thing to keep in step,
/// so the tray asks the window to do it.
fn show_main_window_and(app: &tauri::AppHandle, event: &str) {
    use tauri::Emitter;
    show_main_window(app);
    let _ = app.emit_to("main", event, json!({}));
}

/// Run one sidecar call from the tray.
///
/// The tray is not a window: there is no `invoke` to carry a rejection back to,
/// so a failure has no dialog of its own.  What it has instead is the state
/// refresh that follows every attempt — a rejected toggle leaves the checkbox
/// where the sidecar says it is, and a rejected pause leaves the submenu on its
/// presets.  The control snapping back is the report, which is also how the
/// legacy tray showed it.
fn tray_call(app: &tauri::AppHandle, method: &str, params: Value) {
    let app = app.clone();
    let method = method.to_owned();
    tauri::async_runtime::spawn(async move {
        let Some(host) = app.try_state::<Host>() else {
            return;
        };
        if let Ok(bridge) = host.bridge().await {
            let _ = bridge.call(&method, params).await;
        }
        // On success this races the event the sidecar already published, and
        // the gate collapses the two into one refresh; on failure it is the only
        // refresh there will be, which is the point.
        tray::refresh(&app).await;
    });
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_autostart::Builder::new().app_name("ClipSync").build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            show_main_window(app);
        }))
        .setup(|app| {
            let host = Host {
                bridge: Arc::new(Mutex::new(Slot::Idle)),
                app: app.handle().clone(),
                settings_update: Mutex::new(()),
            };
            // Spawn only, without waiting for readiness. Record the result before
            // exit handling can run, including a launch failure, which is cached
            // so no command respawns it until the window asks for a retry.
            if let Err(error) = tauri::async_runtime::block_on(host.bridge()) {
                emit_sidecar_state(app.handle(), json!({"state":"failed","error":error.code}));
            }
            app.manage(host);
            // Built in the source language; `tray::refresh` below relabels it
            // from the saved setting as soon as the sidecar can answer.
            let tray_menu = tray::TrayMenu::build(app.handle(), i18n::DEFAULT_LOCALE)?;
            // The tray icon owns a handle to the same menu this host keeps
            // mutating; nothing rebuilds or swaps it.
            let menu = tray_menu.menu().clone();
            app.manage(tray_menu);
            app.manage(tray::RefreshGate::default());
            app.manage(i18n::CurrentLocale::default());
            let icon = app.default_window_icon().cloned()
                .ok_or_else(|| std::io::Error::other("Missing ClipSync tray icon"))?;
            let _tray = tauri::tray::TrayIconBuilder::with_id("clipsync")
                .icon(icon)
                .tooltip("ClipSync")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_tray_icon_event(|tray, event| {
                    if matches!(event, tauri::tray::TrayIconEvent::Click {
                        button: tauri::tray::MouseButton::Left,
                        button_state: tauri::tray::MouseButtonState::Up,
                        ..
                    }) {
                        show_main_window(tray.app_handle());
                    }
                })
                .on_menu_event(|app, event| match event.id().as_ref() {
                    // The sync switch: the OS has already flipped the checkbox,
                    // so the sidecar is told what the user now sees rather than
                    // the other way round.
                    tray::ID_SYNC_TOGGLE => {
                        let enabled = app
                            .try_state::<tray::TrayMenu>()
                            .map(|menu| menu.sync_checked())
                            .unwrap_or(false);
                        tray_call(app, "sync.set_enabled", json!({"enabled": enabled}));
                    }
                    tray::ID_PAUSE_15 => tray_call(app, "sync.pause", json!({"minutes": 15})),
                    tray::ID_PAUSE_30 => tray_call(app, "sync.pause", json!({"minutes": 30})),
                    tray::ID_PAUSE_60 => tray_call(app, "sync.pause", json!({"minutes": 60})),
                    tray::ID_RESUME => tray_call(app, "sync.resume", json!({})),
                    tray::ID_SHOW => show_main_window(app),
                    tray::ID_SEND_URL => show_main_window_and(app, "ui:send-url"),
                    tray::ID_QR => show_main_window_and(app, "ui:qr"),
                    tray::ID_SETTINGS => show_main_window_and(app, "ui:settings"),
                    tray::ID_EXPORT_LOGS => show_main_window_and(app, "ui:export-logs"),
                    tray::ID_CHECK_UPDATE => show_main_window_and(app, "ui:check-update"),
                    tray::ID_ABOUT => show_main_window_and(app, "ui:about"),
                    tray::ID_QUIT => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            // The saved language lives in the sidecar, so the tray catches up
            // once the bridge is reachable instead of blocking startup.
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move { tray::refresh(&handle).await });
            // The pause countdown is the one label no event can announce.
            tray::spawn_ticker(app.handle().clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_app_status,
            list_devices,
            companion_status,
            configure_companion,
            get_settings,
            update_settings,
            translate_text,
            ai_profiles,
            update_ai_profiles,
            ai_inventory,
            ai_preview,
            ai_pull,
            ai_local,
            autostart_status,
            list_transfers,
            send_files,
            choose_file,
            choose_files,
            choose_folder,
            transfer_action,
            cancel_all_transfers,
            clear_transfer_history,
            start_speed_test,
            list_chat_devices,
            list_chat_sessions,
            set_chat_muted,
            list_chat_messages,
            open_chat_file,
            invite_chat,
            chat_action,
            accept_chat_invite,
            decline_chat_invite,
            mark_chat_read,
            close_chat,
            send_chat_text,
            resend_chat_text,
            chat_typing,
            send_chat_file,
            chat_file_action,
            list_backups,
            create_backup,
            restore_backup,
            export_history,
            import_history,
            list_history,
            unlock_app,
            delete_history,
            set_history_pinned,
            batch_pin_history,
            batch_delete_history,
            clear_history,
            batch_favorite_history,
            export_favorites,
            start_pairing,
            confirm_pairing,
            reject_pairing,
            unpair_device,
            internet_pairing_status,
            internet_pairing_generate,
            internet_pairing_enter,
            internet_pairing_rename,
            internet_pairing_unpair,
            relay_delivery_status,
            set_device_note,
            connect_device,
            disconnect_device,
            forget_device,
            restore_device,
            purge_device,
            test_device,
            device_certs,
            device_retrust,
            send_url,
            push_text,
            discovery_status,
            set_discovery_enabled,
            set_discovery_visible,
            set_sync_enabled,
            pause_sync,
            resume_sync,
            copy_history,
            read_history_text,
            open_history_link,
            list_favorites,
            get_favorite,
            add_favorite,
            update_favorite,
            delete_favorite,
            copy_favorite,
            read_logs,
            restart_app,
            restart_sidecar,
            recover_data_dir,
            factory_reset,
            diagnostics_report,
            diagnostics_request,
            update_check,
            update_status,
            update_download,
            update_open_folder,
            open_data_folder,
            export_logs,
            open_about_link,
            companion_qr,
            minimize_app,
            quit_app,
        ])
        .build(tauri::generate_context!())
        .expect("Could not initialize ClipSync desktop");
    app.run(|handle, event| {
        match event {
            tauri::RunEvent::WindowEvent { label, event, .. } if label == "main" => {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    if let Some(window) = handle.get_webview_window("main") {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                }
            }
            tauri::RunEvent::Exit => {
                tauri::async_runtime::block_on(handle.state::<Host>().stop_bridge());
            }
            _ => {}
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn autostart_entry_distinguishes_absence_from_access_failure() {
        assert!(autostart_entry_exists(Ok(())).unwrap());
        assert!(!autostart_entry_exists::<()>(Err(std::io::ErrorKind::NotFound.into())).unwrap());
        assert!(autostart_entry_exists::<()>(Err(std::io::ErrorKind::PermissionDenied.into())).is_err());
    }

    #[test]
    fn autostart_rollback_uses_saved_value_and_rejects_unknown_state() {
        for enabled in [false, true] {
            assert_eq!(saved_autostart(&json!({"settings":{"auto_start":enabled}})).unwrap(), enabled);
        }
        for value in [json!({}), json!({"settings":{}}), json!({"settings":{"auto_start":"true"}})] {
            assert!(saved_autostart(&value).is_err());
        }
    }

    #[test]
    fn ai_listing_sends_empty_params_and_save_preserves_empty_content() {
        assert_eq!(ai_local_params("listing", "".into(), "".into(), "".into(), None).unwrap(), json!({}));
        assert_eq!(ai_local_params("save", "codex".into(), "config".into(),
            "config.toml".into(), Some("".into())).unwrap(),
            json!({"tool":"codex", "root":"config", "rel_path":"config.toml", "content":""}));
    }

    #[test]
    fn ai_local_rejects_missing_content_invalid_paths_and_unexpected_content() {
        for (action, path, content) in [
            ("save", "config.toml", None),
            ("read", "", None),
            ("read", "config.toml", Some("unexpected".into())),
            ("save", "config.toml", Some("\0".into())),
            ("delete", "config.toml", None),
        ] {
            assert!(ai_local_params(action, "codex".into(), "".into(), path.into(), content).is_err());
        }
    }

    #[test]
    fn favorite_query_boundaries() {
        assert!(validate_favorite_query("", "", 0, 1).is_ok());
        assert!(validate_favorite_query(
            &"\u{1f600}".repeat(512),
            &"\u{1f600}".repeat(128),
            1_000_000,
            100
        )
        .is_ok());
        for (query, group, offset, limit) in [
            ("a".repeat(513), String::new(), 0, 1),
            (String::new(), "a".repeat(129), 0, 1),
            (String::new(), String::new(), 1_000_001, 1),
            (String::new(), String::new(), 0, 0),
            (String::new(), String::new(), 0, 101),
        ] {
            assert_eq!(
                validate_favorite_query(&query, &group, offset, limit)
                    .unwrap_err()
                    .code,
                "VALIDATION_ERROR"
            );
        }
    }

    #[test]
    fn favorite_field_character_boundaries() {
        assert!(validate_favorite_fields("", "", "").is_ok());
        assert!(validate_favorite_fields(
            &"\u{1f600}".repeat(256),
            &"\u{1f600}".repeat(65_536),
            &"\u{1f600}".repeat(128)
        )
        .is_ok());
        for (title, content, group) in [
            ("a".repeat(257), String::new(), String::new()),
            (String::new(), "a".repeat(65_537), String::new()),
            (String::new(), String::new(), "a".repeat(129)),
        ] {
            assert_eq!(
                validate_favorite_fields(&title, &content, &group)
                    .unwrap_err()
                    .code,
                "VALIDATION_ERROR"
            );
        }
    }

    #[test]
    fn favorite_id_and_position_boundaries() {
        assert!(validate_id("a").is_ok());
        assert!(validate_id(&"\u{1f600}".repeat(64)).is_ok());
        assert!(validate_id("").is_err());
        assert!(validate_id(&"\u{1f600}".repeat(65)).is_err());
        for position in [0, 1_000_000] {
            assert!(validate_favorite_position(position).is_ok());
        }
        for position in [i64::MIN, -1, 1_000_001, i64::MAX] {
            assert_eq!(
                validate_favorite_position(position).unwrap_err().code,
                "VALIDATION_ERROR"
            );
        }
    }

    #[test]
    fn batch_ids_accept_count_and_character_boundaries() {
        assert!(validate_batch_ids(&["a".repeat(64)]).is_ok());
        assert!(validate_batch_ids(&["\u{1f600}".repeat(64)]).is_ok());
        let ids: Vec<String> = (0..100).map(|i| i.to_string()).collect();
        assert!(validate_batch_ids(&ids).is_ok());
        // IDs are compared exactly; neither normalization nor trimming is applied.
        assert!(validate_batch_ids(&["A".into(), "a".into(), " ".into()]).is_ok());
    }

    #[test]
    fn log_line_counts_stay_within_the_backend_range() {
        assert!(validate_log_lines(1).is_ok());
        assert!(validate_log_lines(1000).is_ok());
        for lines in [0, 1001, u32::MAX] {
            assert_eq!(
                validate_log_lines(lines).unwrap_err().code,
                "VALIDATION_ERROR"
            );
        }
    }

    #[test]
    fn diagnostic_actions_are_limited_to_the_two_os_repairs() {
        for action in ["firewall", "local_network"] {
            assert!(validate_diagnostic_action(action).is_ok());
        }
        for action in ["", "Firewall", "permissions", "quit", "../firewall"] {
            assert_eq!(
                validate_diagnostic_action(action).unwrap_err().code,
                "VALIDATION_ERROR"
            );
        }
    }

    /// The handler block's command names, read out of this file's own source.
    fn handler_commands() -> Vec<&'static str> {
        let source = include_str!("main.rs");
        let start =
            source.find("generate_handler![").expect("handler list") + "generate_handler![".len();
        let end = source[start..].find("])").expect("handler list end") + start;
        source[start..end]
            .split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
            .filter(|token| !token.is_empty())
            .collect()
    }

    #[test]
    fn every_handler_command_is_registered_in_build_and_acl() {
        // A command is only reachable when all three sites agree: the handler
        // list, the manifest's closed command list, and the window ACL. Checked
        // against each other rather than from a hand-kept list, so a new command
        // that forgets either site fails here instead of at runtime.
        let commands = handler_commands();
        assert!(commands.len() > 80, "parsed only {} commands", commands.len());
        let manifest = include_str!("../build.rs");
        let acl = include_str!("../capabilities/main.json");
        for command in commands {
            assert!(
                manifest.contains(&format!("\"{command}\"")),
                "{command} is missing from build.rs"
            );
            let permission = format!("\"allow-{}\"", command.replace('_', "-"));
            assert!(
                acl.contains(&permission),
                "{permission} is missing from capabilities/main.json"
            );
        }
    }

    #[test]
    fn about_link_targets_are_the_two_closed_keys() {
        for target in ["homepage", "releases"] {
            assert!(validate_about_target(target).is_ok(), "{target} should be valid");
        }
        for target in ["", "HOME", "https://github.com/kai3316/clipsync", "../homepage"] {
            assert_eq!(
                validate_about_target(target).unwrap_err().code,
                "VALIDATION_ERROR",
                "{target} should be refused",
            );
        }
    }

    #[test]
    fn sidecar_relaunch_is_bounded_and_backs_off_further_each_time() {
        // Bounded: a sidecar that cannot start is retried a handful of times and
        // then left to the user, never indefinitely.
        assert!((1..=5).contains(&SIDECAR_RESTART_ATTEMPTS));
        let delays: Vec<Duration> = (1..=SIDECAR_RESTART_ATTEMPTS).map(restart_delay).collect();
        assert!(delays.windows(2).all(|pair| pair[0] < pair[1]), "{delays:?}");
        // The last attempt alone still fits inside a conversation-sized pause.
        assert!(*delays.last().unwrap() <= Duration::from_secs(10));
    }

    #[test]
    fn log_export_name_rejects_paths_and_oversized_names() {
        assert_eq!(log_export_name(Some("clipsync_20260909_101500.log")), Some("clipsync_20260909_101500.log"));
        assert_eq!(log_export_name(None), None);
        for name in ["", "../escape.log", "sub/dir.log", "sub\\dir.log", "a\0b"] {
            assert_eq!(log_export_name(Some(name)), None, "{name:?} should be refused");
        }
        assert_eq!(log_export_name(Some(&"a".repeat(129))), None);
    }

    #[test]
    fn transfer_action_validation_accepts_open_and_reveal_only() {
        for action in ["cancel", "pause", "resume", "accept", "reject", "delete", "retry", "open", "reveal"] {
            assert!(validate_transfer_action(action, "t1").is_ok(), "{action} should be valid");
        }
        for action in ["", "launch", "Open", "open_file", "../open"] {
            assert_eq!(
                validate_transfer_action(action, "t1").unwrap_err().code,
                "VALIDATION_ERROR"
            );
        }
        for transfer_id in ["", &"x".repeat(129)] {
            assert_eq!(
                validate_transfer_action("open", transfer_id).unwrap_err().code,
                "VALIDATION_ERROR"
            );
        }
    }

    #[test]
    fn batch_ids_reject_invalid_counts_ids_and_duplicates() {
        let cases = [
            Vec::new(),
            (0..101).map(|i| i.to_string()).collect(),
            vec![String::new()],
            vec!["valid".into(), String::new()],
            vec!["a".repeat(65)],
            vec!["\u{1f600}".repeat(65)],
            vec!["same".into(), "other".into(), "same".into()],
        ];
        for ids in cases {
            assert_eq!(
                validate_batch_ids(&ids).unwrap_err().code,
                "VALIDATION_ERROR"
            );
        }
    }
}
