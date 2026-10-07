#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod confine;
mod error;
mod i18n;
mod protocol;
mod tray;

use base64::Engine as _;
use bridge::Bridge;
use error::BridgeError;
use minisign_verify::{PublicKey, Signature};
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;
use tauri::{Emitter, Manager, State, WebviewWindow, WindowEvent};
use tokio::sync::Mutex;
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_updater::UpdaterExt;

/// How many times the host brings a ready-then-dead sidecar back before it
/// stops trying and leaves the retry to the user. The wait before attempt `n`
/// is `n * SIDECAR_RESTART_BACKOFF`, so a sidecar that dies instantly cannot
/// be respawned in a tight loop.
const SIDECAR_RESTART_ATTEMPTS: u32 = 3;
const SIDECAR_RESTART_BACKOFF: Duration = Duration::from_secs(2);

/// How long the updater plugin may spend fetching the release manifest. The
/// plugin sets no timeout of its own, and this lookup sits behind a settings
/// card the user is watching with a spinner on it — the same ~8s the sidecar
/// puts on its own release lookup.
const UPDATE_LOOKUP_TIMEOUT: Duration = Duration::from_secs(8);

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
    ///
    /// A retry while a bridge is already live has to stop the old one first:
    /// clearing the slot alone loses the only handle to that process, and the
    /// replacement launch then shares the data directory with a sidecar nobody
    /// can reach to stop.
    async fn restart_bridge(&self) -> Result<(), BridgeError> {
        let previous = {
            let mut slot = self.bridge.lock().await;
            std::mem::replace(&mut *slot, Slot::Idle)
        };
        if let Slot::Live(bridge) = previous {
            bridge.stop().await;
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
async fn configure_companion(window: WebviewWindow, host: State<'_, Host>, enabled: bool, port: u16, rotate_token: bool, clear_token: bool) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if port == 0 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid companion port"));
    }
    host.bridge().await?.call("companion.configure", json!({
        "enabled": enabled, "port": port, "rotate_token": rotate_token,
        "clear_token": clear_token,
    })).await
}

#[tauri::command]
async fn list_devices(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("devices.list", json!({})).await
}

/// The refresh button: asks the LAN who is here before answering, so the list
/// the window reads next is one that was just asked for rather than whatever
/// the last background round left behind.
#[tauri::command]
async fn scan_devices(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("devices.scan", json!({})).await
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

// Only the Windows path above calls this, so the dead-code lint is correct
// about every other target -- but the mapping it holds is platform-independent,
// and the case worth guarding is a permission failure being read as "not
// registered", which would silently drop a user's auto-start setting.  That is
// worth testing wherever it compiles, so this is annotated rather than cfg'd
// out of the non-Windows builds, which would take the test with it.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
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

/// Ask a paired device for the files behind one of its history entries.
///
/// The 下载 button on a row whose file lives on another machine.  What travels
/// is the entry id: the peer resolves the paths against its own history, which
/// is what keeps a request to files that peer published rather than to whatever
/// path the caller could name.  The bounds mirror the sidecar's own so a frame
/// it would reject never leaves here.
#[tauri::command]
async fn request_entry_files(
    window: WebviewWindow,
    host: State<'_, Host>,
    entry_id: String,
    device_id: Option<String>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    if entry_id.is_empty() || entry_id.chars().count() > 128 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid entry id"));
    }
    let device_id = device_id.unwrap_or_default();
    if device_id.chars().count() > 128 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid identifier"));
    }
    host.bridge()
        .await?
        .call(
            "transfers.request_entry_files",
            json!({"entry_id": entry_id, "device_id": device_id}),
        )
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
    if !matches!(action.as_str(), "cancel") {
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
/// Whether this machine sends what it copies to that device.
///
/// Named `set_device_sync` rather than `set_sync_enabled`, which already exists
/// on the host and means the machine-wide switch: one device's share of the
/// clipboard is a different question from whether sync runs at all.
#[tauri::command]
async fn set_device_sync(window: WebviewWindow, host: State<'_, Host>, peer_id: String, enabled: bool) -> Result<Value, BridgeError> {
    validate_id(&peer_id)?;
    chat_call(window, host, "devices.set_sync", json!({"peer_id": peer_id, "enabled": enabled})).await
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
async fn reveal_chat_file(window: WebviewWindow, host: State<'_, Host>, session_id: String, transfer_id: String) -> Result<Value, BridgeError> {
    validate_id(&session_id)?;
    validate_id(&transfer_id)?;
    // Unlike `open_chat_file`, which resolves a path and launches it from here,
    // the sidecar reveals the folder itself: `chat.reveal_file` goes through the
    // same `reveal_folder` the transfer list's 打开所在文件夹 already uses, so
    // one implementation decides how each platform's file manager is asked and
    // this command only carries the answer — a failure included — back up.
    chat_call(window, host, "chat.reveal_file", json!({"session_id": session_id, "transfer_id": transfer_id})).await
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

/// Probe the relay brokers the settings card has staged, or the saved ones.
///
/// The list is a parameter rather than read from the settings because a reader
/// tests what they are about to save, and because a broker that will not answer
/// is worth knowing about before it is written into the config the relay runs
/// on.  Empty means the saved list, which is what the older panel's button did
/// with an empty body.
#[tauri::command]
async fn internet_pairing_test(window: WebviewWindow, host: State<'_, Host>, brokers: Vec<String>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // Bounded here as well as in the sidecar: the probe opens a socket per
    // entry, so a malformed frame must not become a thousand connections.
    if brokers.len() > 16 || brokers.iter().any(|b| b.chars().count() > 2048) {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid relay broker list"));
    }
    host.bridge().await?.call("internet_pairing.test", json!({ "brokers": brokers })).await
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
async fn offer_device_update(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("devices.offer_update", json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn fetch_device_update(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("devices.fetch_update", json!({"device_id": device_id}))
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

/// Ask one device for its log, for debugging.
///
/// The peer answers with its own log as an ordinary transfer; what comes back
/// is filed by the sidecar under the device's name, so no path crosses this
/// boundary in either direction.
#[tauri::command]
async fn collect_device_log(
    window: WebviewWindow,
    host: State<'_, Host>,
    device_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&device_id)?;
    host.bridge()
        .await?
        .call("logs.collect", json!({"device_id": device_id}))
        .await
}

/// Ask every device on the network for its log.
///
/// Returns once the requests are away: each log arrives afterwards and is
/// announced on its own, so the click reports how many devices were asked
/// rather than waiting on the slowest one.
#[tauri::command]
async fn collect_all_logs(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("logs.collect_all", json!({})).await
}

/// Reveal the folder collected logs are filed in.
#[tauri::command]
async fn open_logs_folder(window: WebviewWindow, host: State<'_, Host>) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // The folder is owned by the sidecar; no path crosses the boundary.
    host.bridge().await?.call("logs.open_folder", json!({})).await
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
async fn copy_text(
    window: WebviewWindow,
    host: State<'_, Host>,
    text: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // Bounded exactly like push_text above, and for the same reason: the frame
    // that carries this is read into memory whole.
    if text.trim().is_empty() || text.chars().count() > 100000 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid text"));
    }
    host.bridge()
        .await?
        .call("clipboard.copy", json!({"text": text}))
        .await
}

#[tauri::command]
async fn get_overview(
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    host.bridge().await?.call("overview.get", json!({})).await
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

/// The picture and file list behind a row's hover card.
///
/// A read like `read_history_text`, and the only one the window makes without
/// the user asking for it: the card follows the pointer, so this rides the same
/// `validate_id` guard and nothing more — the sidecar decides what a card may
/// show, and answers with an empty one rather than an error for a row there is
/// nothing to show about.
#[tauri::command]
async fn preview_history_entry(
    window: WebviewWindow,
    host: State<'_, Host>,
    entry_id: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_id(&entry_id)?;
    host.bridge()
        .await?
        .call("history.preview", json!({"entry_id": entry_id}))
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
async fn reorder_favorites(
    window: WebviewWindow,
    host: State<'_, Host>,
    favorite_ids: Vec<String>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_batch_ids(&favorite_ids)?;
    host.bridge()
        .await?
        .call("favorites.reorder", json!({"favorite_ids": favorite_ids}))
        .await
}

#[tauri::command]
async fn create_favorite_group(
    window: WebviewWindow,
    host: State<'_, Host>,
    name: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_group_name(&name)?;
    host.bridge()
        .await?
        .call("favorites.group_create", json!({"name": name}))
        .await
}

#[tauri::command]
async fn rename_favorite_group(
    window: WebviewWindow,
    host: State<'_, Host>,
    name: String,
    rename_to: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_group_name(&name)?;
    validate_group_name(&rename_to)?;
    host.bridge()
        .await?
        .call(
            "favorites.group_rename",
            json!({"name": name, "rename_to": rename_to}),
        )
        .await
}

#[tauri::command]
async fn delete_favorite_group(
    window: WebviewWindow,
    host: State<'_, Host>,
    name: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    validate_group_name(&name)?;
    host.bridge()
        .await?
        .call("favorites.group_delete", json!({"name": name}))
        .await
}

/// A group name the registry will accept: the same 128-character bound a
/// favourite's `group` field carries, and not blank — a blank name is the
/// absence of a group, not a group called nothing.
fn validate_group_name(name: &str) -> Result<(), BridgeError> {
    if name.chars().count() > 128 || name.trim().is_empty() {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid favorite group"));
    }
    Ok(())
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
    //
    // That order is why this is not `items?`: the early return skipped the
    // restart the comment above promises, leaving the window with no background
    // process for as long as the repair took to fail.
    let items = match items {
        Ok(items) => items,
        Err(error) => {
            if let Err(restart) = host.restart_bridge().await {
                log_restart_failure(&error, &restart);
            }
            return Err(error);
        }
    };
    host.restart_bridge().await?;
    Ok(json!({ "items": items }))
}

/// Record a failure to bring the sidecar back after a repair pass.
///
/// The repair's own error is what the caller reports — it is the actionable one
/// — so a second failure to restart must not replace it. It still has to be
/// visible somewhere: a window with no background process and no account of why
/// is the state this whole path exists to avoid.
fn log_restart_failure(repair: &BridgeError, restart: &BridgeError) {
    eprintln!(
        "clipsync: data repair failed ({}: {}) and the sidecar did not restart ({}: {})",
        repair.code, repair.message, restart.code, restart.message
    );
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

/// The updater plugin, with this app's timeout applied.
///
/// `pub(crate)` because the bridge asks it the same question when it forwards the
/// sidecar's silent "an update exists" notice — see `bridge.rs`.
pub(crate) fn updater(app: &tauri::AppHandle) -> Result<tauri_plugin_updater::Updater, BridgeError> {
    Ok(app
        .updater_builder()
        .timeout(UPDATE_LOOKUP_TIMEOUT)
        .build()?)
}

/// Push one update-state frame down the same pipe the sidecar uses. The store
/// matches on `name` and merges `data.state` in place, so progress that
/// originated in Rust renders without the front end knowing who sent it.
fn emit_update_state(app: &tauri::AppHandle, state: Value) {
    let _ = app.emit_to(
        "main",
        "sidecar:event",
        json!({"name": "update.state", "data": {"state": state}}),
    );
}

#[tauri::command]
async fn update_check(
    app: tauri::AppHandle,
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // The sidecar bounds the release lookup itself (~8s) so a blocked GitHub
    // cannot pin this call; it answers "no update" instead of failing.
    let mut result = host.bridge().await?.call("update.check", json!({})).await?;
    // Whether this build can install what it finds is a different question with
    // a different answer, and only the updater plugin knows it: `check` is what
    // matches this machine's bundle against the release manifest.  It has three
    // answers, and the field carries the plugin's own, never a failure dressed
    // as one:
    //
    // * `Ok(Some(_))` — there is a payload for this platform: true.
    // * `Ok(None)` — a real refusal, this build has nothing it could install:
    //   false, and the card keeps the manual path, which is then the truth.
    // * anything else — the manifest could not be read (GitHub unreachable, or
    //   the release not carrying one yet): no field at all, which the window
    //   reads as "could not ask" and answers by offering the install it would
    //   otherwise have performed.  Reported as `false`, this turned a transient
    //   failure into a sentence telling the reader to replace the application
    //   by hand, on a machine that would have installed it without help a
    //   minute later.
    let installable = match updater(&app) {
        Ok(updater) => match updater.check().await {
            Ok(Some(_)) => Some(true),
            Ok(None) => Some(false),
            Err(_) => None,
        },
        Err(_) => None,
    };
    if let (Some(object), Some(installable)) = (result.as_object_mut(), installable) {
        object.insert("installable".into(), json!(installable));
    }
    Ok(result)
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

/// Leave the installer this upgrade downloaded behind, for other devices.
///
/// A newer machine is the one that can bring an older one up to date, and until
/// now the only way it could send the file was to fetch a second copy of it —
/// the same bytes, from the same release, on a machine that had just downloaded
/// them.  The download here is already verified by the updater plugin's own
/// signature check, so what it leaves in the cache is a file a peer can be given
/// as it stands; the peer checks it against the published digest either way.
///
/// Best effort, and silent about failing: this is a courtesy to the *next*
/// exchange, while the install it runs beside is the thing the user asked for.
/// A name the sidecar will not recognise, a temp directory that cannot be
/// written, a sidecar that has already stopped answering — each costs one more
/// download later and nothing now.  The bytes cannot cross the pipe themselves
/// (the frame cap
/// is a megabyte), so they are written to a file of the asset's own name and the
/// *name* travels with the path.
async fn keep_downloaded_installer(
    bridge: &Arc<Bridge>,
    url: &tauri::Url,
    signature: &str,
    bytes: &[u8],
) {
    // The asset's filename, which is the last segment of its download URL.  It
    // is the only name this file has: the plugin streams into memory, so the
    // name is what lets the sidecar file the bytes under the release's own
    // name.  The sidecar keeps the macOS updater payload
    // (`ClipSync.app.tar.gz`) too since 1.0.55, so a refusal here is no longer
    // the expected macOS answer -- it means the name did not match this shell's
    // own asset patterns.  The manifest signature travels beside the bytes: the
    // sidecar caches it so a peer with no route to the manifest can be served
    // the pair and verify it offline.
    let Some(name) = url
        .path_segments()
        .and_then(|segments| segments.last())
        .filter(|name| !name.is_empty())
    else {
        return;
    };
    let dir = std::env::temp_dir().join("clipsync-update-cache");
    let _ = std::fs::remove_dir_all(&dir);
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    let path = dir.join(name);
    if tokio::fs::write(&path, bytes).await.is_ok() {
        let _ = bridge
            .call(
                "update.cache_asset",
                json!({
                    "path": path.to_string_lossy(),
                    "name": name,
                    "signature": signature,
                }),
            )
            .await;
    }
    // Removed whatever happened: the sidecar copies what it keeps, and a copy
    // that did not happen leaves an installer in a temp directory that nothing
    // will ever clean up.  Whether it was kept is the sidecar's to say, and it
    // does -- its own log carries the refusal, with the name it refused.
    let _ = std::fs::remove_dir_all(&dir);
}

/// Download the pending update and replace this installation with it.
///
/// The sidecar owns checking and fetching, but it cannot do this half: a
/// process cannot replace the bundle it is running from.  Two easier paths were
/// both worse.  Handing the WebView the updater plugin's own JavaScript API
/// would give this window the ability to start a download and end the process,
/// which is the one thing the capability file says it does not have.  Calling
/// `download_and_install` would leave no seam to stop the sidecar in — and on
/// Windows `install` launches the NSIS installer and then calls
/// `process::exit(0)`, so `RunEvent::Exit` never fires and the shutdown hook
/// that normally stops the sidecar never runs.  It would survive the update as
/// an orphan holding the very data directory the new version is about to open.
#[tauri::command]
async fn update_install(
    app: tauri::AppHandle,
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    let bridge = host.bridge().await?;
    let update = match updater(&app)?.check().await {
        Ok(Some(update)) => update,
        Ok(None) => return Ok(json!({"ok": true, "installed": false, "reason": "up_to_date"})),
        // A manifest that could not be read is not "nothing to install", and the
        // card offers this command for exactly that case (see `update_check`).
        // The failing phase is published before the error travels up, so the
        // card can say what went wrong instead of the click appearing to do
        // nothing at all.
        Err(err) => {
            emit_update_state(&app, json!({"phase": "failed", "error": err.to_string()}));
            return Err(err.into());
        }
    };
    fetch_and_install(&app, &bridge, update).await
}

/// Install the update the sidecar has verified and staged on this disk.
///
/// The second route to an install, and the one the peer exchange needs.  The
/// file is one this process neither fetched nor can: the sidecar staged it from
/// its own download or from a blob a peer sent.  What makes either one
/// installable here is a minisign signature over *these* bytes from the release
/// signing key -- the manifest's while that manifest is reachable, or the one
/// the peer transfer carried when it is not.  A peer's own sha256
/// (`verified == "peer_verified"`) settles integrity, not origin, so by itself
/// it never reaches `run_staged_installer`.
///
/// The online check is also what keeps the platform's own artifact shape: its
/// download URL and signature are for this target, so a blob for another system
/// cannot verify.  Its three answers:
///
/// * `Ok(Some(update))` -- the staged bytes are checked against this update's
///   signature; only a match is installed (the NSIS installer from a private
///   copy on Windows, the payload through the plugin elsewhere).
/// * `Ok(None)` -- this build is already at the released version.  A staged
///   archive that names the running version is `up_to_date`; anything else is
///   revealed for the reader rather than run.
/// * `Err(_)` -- the manifest could not be read (offline, rate-limited).  That
///   is exactly the condition a peer-sent blob exists for: a `peer_verified`
///   blob whose transferred signature verifies against the embedded key and
///   whose signed version is newer may install.  Anything else -- no signature,
///   a failing one, an older version -- is revealed, never run.
#[tauri::command]
async fn update_install_ready(
    app: tauri::AppHandle,
    window: WebviewWindow,
    host: State<'_, Host>,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    let bridge = host.bridge().await?;
    install_staged_update(&app, &bridge).await
}

/// Write verified installer bytes to a private per-process temp path.
///
/// The staged path itself is never executed: a second transfer could replace
/// it between the signature check and the spawn, and the copy holds exactly
/// the bytes that were checked.
#[cfg(target_os = "windows")]
fn private_installer_copy(bytes: &[u8]) -> Result<std::path::PathBuf, BridgeError> {
    use std::time::{SystemTime, UNIX_EPOCH};
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or(0);
    let dir = std::env::temp_dir().join(format!(
        "clipsync-verified-{}-{}",
        std::process::id(),
        stamp
    ));
    std::fs::create_dir_all(&dir)
        .map_err(|err| BridgeError::new("UPDATE_ERROR", &err.to_string()))?;
    let path = dir.join("ClipSync_verified_x64-setup.exe");
    std::fs::write(&path, bytes)
        .map_err(|err| BridgeError::new("UPDATE_ERROR", &err.to_string()))?;
    Ok(path)
}

/// The updater plugin's signing key, as configured in `tauri.conf.json`.
///
/// `tauri_plugin_updater::Config` is public, but the plugin keeps its parsed
/// copy in private state; the same JSON is still in the app config under
/// `plugins.updater`, so it is deserialized from there rather than duplicated
/// here. `None` means this build has no verifiable key, which is a refusal, not
/// a fallback to trusting the file.
fn updater_pubkey(app: &tauri::AppHandle) -> Option<String> {
    let value = app.config().plugins.0.get("updater")?.clone();
    let config: tauri_plugin_updater::Config = serde_json::from_value(value).ok()?;
    (!config.pubkey.is_empty()).then_some(config.pubkey)
}

/// Decode the two base64-wrapped minisign texts, check *bytes* against the
/// release key, and return the signed file name and the version it carries.
///
/// The trusted comment is covered by minisign's global signature, so the
/// `file:` name -- and the version read out of it -- is as protected as the
/// payload.  That is what lets an offline host say "this is a newer release"
/// without a manifest.
fn verify_signed_release(
    bytes: &[u8],
    release_signature: &str,
    pubkey_b64: &str,
) -> Option<(String, String)> {
    fn decoded(value: &str) -> Option<String> {
        base64::engine::general_purpose::STANDARD
            .decode(value)
            .ok()
            .and_then(|raw| String::from_utf8(raw).ok())
    }
    let public_key_text = decoded(pubkey_b64)?;
    let signature_text = decoded(release_signature)?;
    let public_key = PublicKey::decode(&public_key_text).ok()?;
    let signature = Signature::decode(&signature_text).ok()?;
    public_key.verify(bytes, &signature, true).ok()?;
    let file_name = signed_file_name(signature.trusted_comment())?;
    let version = version_in_name(&file_name)?;
    Some((file_name, version))
}

/// The basename minisign's signed trusted comment names (`...\tfile:<name>`).
fn signed_file_name(trusted_comment: &str) -> Option<String> {
    let after = trusted_comment.split("file:").nth(1)?;
    let raw = after.split_whitespace().next().unwrap_or(after).trim();
    let base = raw.rsplit(['/', '\\']).next().unwrap_or(raw).trim();
    (!base.is_empty()).then(|| base.to_string())
}

/// The first dotted version run in *name*, the way the sidecar reads its asset
/// names (`ClipSync_1.0.55_x64-setup.exe`).
fn version_in_name(name: &str) -> Option<String> {
    let bytes = name.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        if !bytes[index].is_ascii_digit() {
            index += 1;
            continue;
        }
        let start = index;
        let mut has_dot = false;
        while index < bytes.len() && (bytes[index].is_ascii_digit() || bytes[index] == b'.') {
            has_dot |= bytes[index] == b'.';
            index += 1;
        }
        let candidate = name[start..index].trim_end_matches('.');
        if has_dot && !candidate.is_empty() {
            return Some(candidate.to_string());
        }
    }
    None
}

/// The version an offline peer blob may install as, with its signed file name.
///
/// Some only when the transferred signature verifies against the embedded key
/// *and* the version inside the signed name is newer than what is running.  The
/// version comes from the signature's trusted comment, never from the staged
/// filename, which the sender chose.
fn offline_signed_update(
    bytes: &[u8],
    release_signature: &str,
    pubkey_b64: &str,
    current: &semver::Version,
) -> Option<(String, String)> {
    let (file_name, version) = verify_signed_release(bytes, release_signature, pubkey_b64)?;
    let claimed = semver::Version::parse(&version).ok()?;
    (&claimed > current).then_some((file_name, version))
}

/// The answer for a staged file this build will not install on its own: leave
/// it where the card can point at it and let the reader decide.
async fn reveal_staged_update(bridge: &Arc<Bridge>) -> Result<Value, BridgeError> {
    bridge.call("update.open_folder", json!({})).await?;
    Ok(json!({"ok": true, "installed": false, "reason": "manual"}))
}

/// Install bytes that have passed a release-signature check.
///
/// Windows runs a private copy instead of the staged path: the signature is
/// checked on the bytes in memory, and a peer that can stage another update
/// with the same name must not be able to swap the file between that check and
/// the spawn.  macOS and Linux use the plugin's own `install` while the online
/// manifest named the payload; offline (`online == None`) they install from
/// these bytes themselves, because the plugin exposes no install-from-bytes.
/// Only a signature-verified payload reaches that branch -- see
/// `offline_signed_update` -- and a payload this build cannot install is
/// revealed for the reader instead of being run.
async fn install_verified_update(
    app: &tauri::AppHandle,
    bridge: &Arc<Bridge>,
    bytes: &[u8],
    signed_name: &str,
    version: &str,
    online: Option<tauri_plugin_updater::Update>,
) -> Result<Value, BridgeError> {
    #[cfg(target_os = "windows")]
    {
        let _ = online;
        run_verified_installer(app, bridge, bytes, signed_name, version).await
    }
    #[cfg(not(target_os = "windows"))]
    {
        let Some(update) = online else {
            // Offline peer update: the caller has already checked the
            // transferred minisign signature and the version inside its trusted
            // comment, so these are the bytes to install.  There is no
            // unsigned/old-sender path -- the signature check is the only way
            // in -- and a payload this build cannot install is revealed, never
            // executed.
            return match offline_update::install_offline_from_bytes(bytes, signed_name) {
                Ok(()) => {
                    bridge.stop().await;
                    emit_update_state(app, json!({"phase": "installing", "version": version}));
                    app.restart();
                }
                Err(err) => {
                    emit_update_state(
                        app,
                        json!({"phase": "failed", "error": err.localized_message()}),
                    );
                    reveal_staged_update(bridge).await
                }
            };
        };
        bridge.stop().await;
        emit_update_state(app, json!({"phase": "installing", "version": version}));
        if let Err(err) = update.install(bytes) {
            emit_update_state(app, json!({"phase": "failed", "error": err.to_string()}));
            return Err(err.into());
        }
        // macOS/Linux only swap files here; bringing the new build up is ours.
        app.restart();
    }
}

/// Run the release installer from a private copy of the verified bytes.
#[cfg(target_os = "windows")]
async fn run_verified_installer(
    app: &tauri::AppHandle,
    bridge: &Arc<Bridge>,
    bytes: &[u8],
    signed_name: &str,
    version: &str,
) -> Result<Value, BridgeError> {
    if !is_runnable_installer(signed_name) {
        return reveal_staged_update(bridge).await;
    }
    // The private copy holds exactly the bytes that were checked (see
    // `private_installer_copy`), so a second staged transfer cannot swap the
    // file out from under the installer.
    let Ok(path) = private_installer_copy(bytes) else {
        return reveal_staged_update(bridge).await;
    };
    let result = run_staged_installer(app, bridge, &path.to_string_lossy(), version).await;
    if result.is_err() {
        if let Some(directory) = path.parent() {
            let _ = std::fs::remove_dir_all(directory);
        }
    }
    result
}

/// [`update_install_ready`]'s body, reachable from the event reader as well.
///
/// A staged archive is installed from the bytes already on this disk only after
/// it is tied to the release signing key.  With the manifest in reach that tie
/// is `update.signature`; with the manifest unreachable it is the signature the
/// peer transfer carried (`state.signature`), and only a `peer_verified` blob
/// whose signed version is newer than this build may use it.  Everything else
/// -- no signature, a failing one, an older version, an old sender -- is only
/// revealed, never run.
///
/// `pub(crate)` for the same reason [`updater`] is: the bridge runs this on its
/// own when a peer's update arrives, and that frame comes down the sidecar's
/// stdout rather than through a command.
pub(crate) async fn install_staged_update(
    app: &tauri::AppHandle,
    bridge: &Arc<Bridge>,
) -> Result<Value, BridgeError> {
    let status = bridge.call("update.status", json!({})).await?;
    let state = status.get("state").cloned().unwrap_or(Value::Null);
    let path = state
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    let version = state
        .get("version")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    let verified = state
        .get("verified")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    let peer_signature = state
        .get("signature")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    // The sidecar's own answer to "may this be installed", read rather than
    // inferred: that side is the one that checked the bytes, and a path that
    // has gone since it said so is the same refusal.
    if state.get("phase").and_then(Value::as_str) != Some("ready")
        || path.is_empty()
        || !std::path::Path::new(&path).is_file()
    {
        return Err(BridgeError::new(
            "NOT_FOUND",
            "No verified update is staged",
        ));
    }
    let Some(pubkey) = updater_pubkey(app) else {
        return reveal_staged_update(bridge).await;
    };
    match updater(app)?.check().await {
        Ok(Some(update)) => {
            let Ok(bytes) = tokio::fs::read(&path).await else {
                return reveal_staged_update(bridge).await;
            };
            let Some((signed_name, _signed_version)) =
                verify_signed_release(&bytes, &update.signature, &pubkey)
            else {
                return reveal_staged_update(bridge).await;
            };
            let display_version = update.version.clone();
            install_verified_update(
                app,
                bridge,
                &bytes,
                &signed_name,
                &display_version,
                Some(update),
            )
            .await
        }
        Ok(None) => {
            if version == app.package_info().version.to_string() {
                Ok(json!({"ok": true, "installed": false, "reason": "up_to_date"}))
            } else {
                reveal_staged_update(bridge).await
            }
        }
        // Offline: the manifest is unreachable, which is exactly the case a
        // peer-sent blob exists for.  Only a peer_verified blob that carries a
        // signature verifying against the embedded key, and whose signed
        // version is newer than this build, may install; all else is manual.
        Err(_) => {
            if verified != "peer_verified" || peer_signature.is_empty() {
                return reveal_staged_update(bridge).await;
            }
            let Ok(bytes) = tokio::fs::read(&path).await else {
                return reveal_staged_update(bridge).await;
            };
            let current = app.package_info().version.clone();
            let Some((signed_name, signed_version)) =
                offline_signed_update(&bytes, &peer_signature, &pubkey, &current)
            else {
                return reveal_staged_update(bridge).await;
            };
            install_verified_update(app, bridge, &bytes, &signed_name, &signed_version, None).await
        }
    }
}

/// Whether a staged archive is one this process may run for the reader.
///
/// The shape test `run_staged_installer` makes, hoisted so the choice between
/// it and the plugin can be made before either runs.  Only the release's own
/// NSIS installer is that kind of file — the sidecar's asset matcher picked it,
/// and this is what re-checks that it still is one.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
fn is_runnable_installer(path: &str) -> bool {
    cfg!(target_os = "windows") && path.to_ascii_lowercase().ends_with("-setup.exe")
}

#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
/// Run the installer the sidecar staged, on the one platform where that is a
/// thing this process may do on the reader's behalf.
///
/// A Tauri NSIS payload is an installer a person would otherwise double-click,
/// so the same file can be run for them and the update finishes where the click
/// that started it, on the *other* machine, said it would.  A macOS `.dmg`, a
/// Linux `.deb` and an AppImage are not that: they are files a person opens and
/// acts on, and replacing a running bundle with one of them is what the plugin's
/// `.app.tar.gz` route does — a file the sidecar deliberately does not keep,
/// because it is not a file a person installs.  So everywhere else the folder is
/// revealed and the answer says the rest of the install is the reader's, which
/// is the card's existing manual wording rather than a spinner over nothing.
async fn run_staged_installer(
    app: &tauri::AppHandle,
    bridge: &Arc<Bridge>,
    path: &str,
    version: &str,
) -> Result<Value, BridgeError> {
    #[cfg(target_os = "windows")]
    {
        // Only the release's own installer, by shape: the sidecar's asset
        // matcher is what picked this file, and the one thing worth re-checking
        // before running something is that it is still that kind of file.
        if !path.to_ascii_lowercase().ends_with("-setup.exe") {
            let _ = bridge.call("update.open_folder", json!({})).await;
            return Ok(json!({"ok": true, "installed": false, "reason": "manual"}));
        }
        // The arguments the plugin passes for the install mode this app sets
        // (`passive`, in tauri.conf.json): a progress bar rather than the
        // installer's own pages, an upgrade rather than a fresh install, and the
        // relaunch at the end — which is the restart the user is promised, and
        // it comes from the installer rather than from here.
        //
        // **The sidecar is stopped first, and that order is the fix for "error
        // opening file for writing".**  It runs from the install directory in the
        // directory shape, and Windows locks a running process's image and every
        // DLL it loaded — 191 files inside `sidecar/`.  NSIS starts writing that
        // directory the moment it is spawned, so spawning it first meant writing
        // over files a live process held open for as long as `stop` took: up to
        // two seconds asking, twelve waiting, then a kill.
        //
        // The reason the old order existed still holds — a file that cannot be run
        // at all should be an error the card can show rather than the last thing a
        // half-torn-down app did — and it is still satisfied, because the process
        // that draws the card is this one.  What has been stopped is the sidecar,
        // and the card's message is about the installer.
        bridge.stop().await;
        emit_update_state(app, json!({"phase": "installing", "version": version}));
        let child = std::process::Command::new(path)
            .args(["/P", "/UPDATE", "/R"])
            .spawn();
        let child = match child {
            Ok(child) => child,
            Err(err) => {
                emit_update_state(app, json!({"phase": "failed", "error": err.to_string()}));
                return Err(BridgeError::new("UPDATE_ERROR", &err.to_string()));
            }
        };
        drop(child);
        // Unreachable in practice, and for the reason the plugin exits here too:
        // the installer replaces the running executable and brings the new
        // version back up itself, and the file it is replacing is this one.
        std::process::exit(0);
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = (app, version);
        let _ = bridge.call("update.open_folder", json!({})).await;
        Ok(json!({"ok": true, "installed": false, "reason": "manual"}))
    }
}

/// Fetch *update*, keep the installer for the next device that needs it, and
/// replace this installation with it.
///
/// Split out of `update_install` because `update_install_ready` reaches an
/// install by a second route, and the order of the steps below is the whole of
/// the Windows caveat in that command's own note.
async fn fetch_and_install(
    app: &tauri::AppHandle,
    bridge: &Arc<Bridge>,
    update: tauri_plugin_updater::Update,
) -> Result<Value, BridgeError> {
    let version = update.version.clone();
    let asset_url = update.download_url.clone();

    // Fetch before anything is torn down, so a download that fails — the usual
    // cause being a signature that does not verify — leaves a running app.  The
    // callback reports each chunk's length and the response's Content-Length,
    // not progress and not a running total, so the total is ours to keep.
    let progress_app = app.clone();
    let progress_version = version.clone();
    let mut downloaded: u64 = 0;
    let bytes = match update
        .download(
            move |chunk, total| {
                downloaded += chunk as u64;
                let fraction = match total {
                    Some(total) if total > 0 => (downloaded as f64 / total as f64).min(1.0),
                    _ => 0.0,
                };
                emit_update_state(
                    &progress_app,
                    json!({
                        "phase": "downloading",
                        "fraction": fraction,
                        "downloaded": downloaded,
                        "total": total.unwrap_or(0),
                        "error": "",
                        "version": progress_version,
                        "path": "",
                    }),
                );
            },
            || {},
        )
        .await
    {
        Ok(bytes) => bytes,
        // Every exit path has to leave a terminal phase behind: the card draws
        // a progress bar for `downloading` and an error line for `failed`, and
        // nothing at all for a phase that never arrives.
        Err(err) => {
            emit_update_state(app, json!({"phase": "failed", "error": err.to_string()}));
            return Err(err.into());
        }
    };

    // Keep the installer and its manifest signature for the next device that
    // needs them, before the sidecar goes: this is the only moment the file
    // exists, and the only process that can read it is the one that is about to
    // be replaced.
    let asset_signature = update.signature.clone();
    keep_downloaded_installer(bridge, &asset_url, &asset_signature, &bytes).await;

    // The bytes are in hand and verified, so the sidecar's work is done and its
    // lock has to go before the installer takes over — the same order
    // `restart_app` uses, and on Windows the last moment it is possible.
    bridge.stop().await;
    emit_update_state(app, json!({"phase": "installing", "version": version}));

    if let Err(err) = update.install(&bytes) {
        emit_update_state(app, json!({"phase": "failed", "error": err.to_string()}));
        return Err(err.into());
    }
    // Unreachable on Windows: a successful install hands the process to the
    // installer, which brings the new version back up itself.  On macOS and
    // Linux the swap happened inside this process, so the restart is ours.
    app.restart();
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

#[tauri::command]
async fn share_file_to_phone(
    window: WebviewWindow,
    host: State<'_, Host>,
    path: String,
) -> Result<Value, BridgeError> {
    authorize(&window)?;
    // A path the user picked in the host's own file dialog, so unlike the two
    // commands above it does cross the boundary.  The bound mirrors the
    // sidecar's own; whether the path is a regular file rather than a directory
    // or a device is decided there, next to the copy that needs it.
    if path.is_empty() || path.chars().count() > 4096 {
        return Err(BridgeError::new("VALIDATION_ERROR", "Invalid file path"));
    }
    host.bridge()
        .await?
        .call("companion.share_file", json!({"path": path}))
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
        // Registered for the Rust side only: this window is given no updater
        // permission, so the plugin's own commands stay unreachable from the
        // WebView and every update goes through `update_install` below.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            show_main_window(app);
        }))
        .plugin(
            // The window opens where it was left, at the size it was left, the
            // way the legacy dashboard did — it wrote its own geometry to
            // `dashboard_geometry.json` and put it back when that geometry was
            // still on a screen.  Position and size only: visibility is
            // deliberately not remembered, because closing this window hides it
            // rather than destroying it, and a shell that restored "hidden"
            // would start with no way in but the tray.
            tauri_plugin_window_state::Builder::default()
                .with_state_flags(
                    tauri_plugin_window_state::StateFlags::POSITION
                        | tauri_plugin_window_state::StateFlags::SIZE,
                )
                .build(),
        )
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
                // The message travels with the code. Emitting the code alone
                // left the renderer to fall back to a generic "background
                // process is unavailable", throwing away the one part that
                // says *what* failed -- the file that could not be launched
                // being the case worth knowing.
                emit_sidecar_state(
                    app.handle(),
                    json!({
                        "state":"failed",
                        "error": error.code,
                        "message": error.message,
                        "retryable": error.retryable,
                    }),
                );
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
            scan_devices,
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
            request_entry_files,
            list_chat_devices,
            list_chat_sessions,
            set_chat_muted,
            set_device_sync,
            list_chat_messages,
            open_chat_file,
            reveal_chat_file,
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
            internet_pairing_test,
            internet_pairing_enter,
            internet_pairing_rename,
            internet_pairing_unpair,
            relay_delivery_status,
            set_device_note,
            connect_device,
            disconnect_device,
            offer_device_update,
            fetch_device_update,
            collect_device_log,
            collect_all_logs,
            open_logs_folder,
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
            copy_text,
            get_overview,
            read_history_text,
            preview_history_entry,
            open_history_link,
            list_favorites,
            get_favorite,
            add_favorite,
            update_favorite,
            reorder_favorites,
            delete_favorite,
            copy_favorite,
            create_favorite_group,
            rename_favorite_group,
            delete_favorite_group,
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
            update_install,
            update_install_ready,
            open_data_folder,
            share_file_to_phone,
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
            tauri::RunEvent::WindowEvent { label, event, .. } if label == "main" => match event {
                WindowEvent::CloseRequested { api, .. } => {
                    if let Some(window) = handle.get_webview_window("main") {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                }
                _ => {}
            },
            tauri::RunEvent::Exit => {
                tauri::async_runtime::block_on(handle.state::<Host>().stop_bridge());
            }
            _ => {}
        }
    });
}

/// Install a signature-verified update from bytes on the platforms whose
/// updater plugin has no public install-from-bytes.
///
/// The online path (`install_verified_update` with `Some(update)`) keeps using
/// the plugin's own `install`; this module is reached only with `None`, after
/// `offline_signed_update` has checked the transferred minisign signature and
/// the version inside its trusted comment.  There is deliberately no unsigned
/// fallback: an old sender's file is revealed, never run.
///
/// The archive handling itself is target-split on purpose.  `tar` is a
/// unix-only dependency (see `Cargo.toml`), because that is where the plugin
/// already needs it and Windows has no .app/AppImage payload to unpack; the
/// classification, bundle lookup and path-safety checks below are plain
/// functions so the Windows tests can still exercise them.
mod offline_update {
    #![allow(dead_code)]

    use std::path::{Component, Path, PathBuf};

    use super::BridgeError;

    const GZIP_MAGIC: [u8; 2] = [0x1f, 0x8b];
    const DEB_MAGIC: &[u8] = b"!<arch>\n";
    const RPM_MAGIC: [u8; 4] = [0xed, 0xab, 0xee, 0xdb];
    const ELF_MAGIC: [u8; 4] = [0x7f, b'E', b'L', b'F'];

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub(super) enum InstallerKind {
        MacAppTarGz,
        Deb,
        Rpm,
        AppImage,
        Unknown,
    }

    fn offline_error(error: impl std::fmt::Display) -> BridgeError {
        BridgeError::new("UPDATE_ERROR", &error.to_string())
    }

    /// What the signed payload is, from its name and its first bytes.
    pub(super) fn classify_installer(bytes: &[u8], signed_name: &str) -> InstallerKind {
        let name = signed_name.to_ascii_lowercase();
        if name.ends_with(".app.tar.gz") {
            return InstallerKind::MacAppTarGz;
        }
        if bytes.starts_with(DEB_MAGIC) {
            return InstallerKind::Deb;
        }
        if bytes.starts_with(&RPM_MAGIC) {
            return InstallerKind::Rpm;
        }
        if name.ends_with(".deb") {
            return InstallerKind::Deb;
        }
        if name.ends_with(".rpm") {
            return InstallerKind::Rpm;
        }
        if is_gzip(bytes) || looks_like_elf(bytes) {
            return InstallerKind::AppImage;
        }
        InstallerKind::Unknown
    }

    pub(super) fn is_gzip(bytes: &[u8]) -> bool {
        bytes.starts_with(&GZIP_MAGIC)
    }

    pub(super) fn looks_like_elf(bytes: &[u8]) -> bool {
        bytes.starts_with(&ELF_MAGIC)
    }

    /// A tar entry path that cannot write outside its extraction root.
    ///
    /// `tar::Entry::unpack_in` guards this as well; the check is here so the
    /// refusal is this side's decision and can be tested without an archive.
    pub(super) fn tar_path_is_safe(path: &Path) -> bool {
        path.components()
            .all(|part| matches!(part, Component::Normal(_) | Component::CurDir))
    }

    /// The running `.app` bundle, found by walking up from the executable.
    pub(super) fn app_bundle_for_exe(exe: &Path) -> Option<PathBuf> {
        exe.ancestors()
            .find(|path| path.extension().and_then(|ext| ext.to_str()) == Some("app"))
            .map(Path::to_path_buf)
    }

    /// The top-level `*.app` bundle under an extraction root.
    pub(super) fn find_app_bundle(root: &Path) -> Option<PathBuf> {
        for entry in std::fs::read_dir(root).ok()?.flatten() {
            let path = entry.path();
            let is_bundle = path.is_dir()
                && path.extension().and_then(|ext| ext.to_str()) == Some("app")
                && path.join("Contents").is_dir();
            if is_bundle {
                return Some(path);
            }
        }
        None
    }

    /// Unpack a gzip'd `.app.tar.gz` into a private directory and return that
    /// directory (kept alive) with the top-level `*.app` bundle inside it.
    #[cfg(unix)]
    pub(super) fn extract_app_bundle(
        bytes: &[u8],
    ) -> Result<(tempfile::TempDir, PathBuf), BridgeError> {
        let directory = tempfile::Builder::new()
            .prefix("clipsync-update-")
            .tempdir()
            .map_err(offline_error)?;
        let root = directory.path().to_path_buf();
        let decoder = flate2::read::GzDecoder::new(bytes);
        let mut archive = tar::Archive::new(decoder);
        for entry in archive.entries().map_err(offline_error)? {
            let mut entry = entry.map_err(offline_error)?;
            let path = entry.path().map_err(offline_error)?.into_owned();
            if !tar_path_is_safe(&path) || !entry.unpack_in(&root).map_err(offline_error)? {
                return Err(offline_error(format!(
                    "archive entry {path:?} would escape the extraction root"
                )));
            }
        }
        let bundle = find_app_bundle(&root)
            .ok_or_else(|| offline_error("the archive holds no .app bundle"))?;
        Ok((directory, bundle))
    }

    /// The bytes of an AppImage: the archive member of a gzip'd tar, or the
    /// bytes themselves when they are already an ELF.
    #[cfg(unix)]
    pub(super) fn appimage_bytes(bytes: &[u8]) -> Result<Vec<u8>, BridgeError> {
        use std::io::Read;

        if is_gzip(bytes) {
            let decoder = flate2::read::GzDecoder::new(bytes);
            let mut archive = tar::Archive::new(decoder);
            for entry in archive.entries().map_err(offline_error)? {
                let mut entry = entry.map_err(offline_error)?;
                let path = entry.path().map_err(offline_error)?.into_owned();
                if !tar_path_is_safe(&path) {
                    return Err(offline_error(format!(
                        "archive entry {path:?} would escape the extraction root"
                    )));
                }
                let is_image = path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.ends_with(".AppImage"));
                if !is_image {
                    continue;
                }
                let mut data = Vec::new();
                entry.read_to_end(&mut data).map_err(offline_error)?;
                if !looks_like_elf(&data) {
                    return Err(offline_error("the archive's .AppImage member is not an ELF"));
                }
                return Ok(data);
            }
            return Err(offline_error("the archive holds no .AppImage member"));
        }
        if looks_like_elf(bytes) {
            return Ok(bytes.to_vec());
        }
        Err(offline_error("the payload is not an AppImage"))
    }

    #[cfg(target_os = "macos")]
    pub(super) fn install_macos_from_bytes(
        bytes: &[u8],
        signed_name: &str,
    ) -> Result<(), BridgeError> {
        if !signed_name.to_ascii_lowercase().ends_with(".app.tar.gz") {
            return Err(offline_error("the signed update is not a macOS .app.tar.gz"));
        }
        let (directory, new_bundle) = extract_app_bundle(bytes)?;
        let current = current_app_bundle()?;
        let result = replace_app_bundle(&current, &new_bundle);
        drop(directory);
        result
    }

    #[cfg(target_os = "macos")]
    fn current_app_bundle() -> Result<PathBuf, BridgeError> {
        let exe = std::env::current_exe().map_err(offline_error)?;
        app_bundle_for_exe(&exe)
            .ok_or_else(|| offline_error("could not locate the running .app bundle"))
    }

    #[cfg(target_os = "macos")]
    fn replace_app_bundle(current: &Path, new_bundle: &Path) -> Result<(), BridgeError> {
        let parent = current
            .parent()
            .ok_or_else(|| offline_error("the .app bundle has no parent directory"))?;
        let backup = unique_sibling(parent, current, "backup");
        match std::fs::rename(current, &backup) {
            Ok(()) => match std::fs::rename(new_bundle, current) {
                Ok(()) => {
                    let _ = std::fs::remove_dir_all(&backup);
                    Ok(())
                }
                Err(err) => {
                    // Put the old bundle back, so a failed install is not a
                    // missing application.
                    let _ = std::fs::rename(&backup, current);
                    Err(offline_error(err))
                }
            },
            Err(err) if err.kind() == std::io::ErrorKind::PermissionDenied => {
                privileged_app_replace(current, &backup, new_bundle)
            }
            Err(err) => Err(offline_error(err)),
        }
    }

    /// The `/Applications` swap as one privileged command: `mv current backup
    /// && mv new current`.  A failure leaves the backup in place for the
    /// rollback below; success cleans it up best-effort.
    #[cfg(target_os = "macos")]
    fn privileged_app_replace(
        current: &Path,
        backup: &Path,
        new_bundle: &Path,
    ) -> Result<(), BridgeError> {
        let current = path_text(current)?;
        let backup = path_text(backup)?;
        let new_bundle = path_text(new_bundle)?;
        let swap = format!(
            "mv {} {} && mv {} {}",
            shell_quote(&current),
            shell_quote(&backup),
            shell_quote(&new_bundle),
            shell_quote(&current),
        );
        let status = std::process::Command::new("osascript")
            .arg("-e")
            .arg(applescript_script(&swap))
            .status()
            .map_err(offline_error)?;
        if status.success() {
            let cleanup = format!("rm -rf {}", shell_quote(&backup));
            let _ = std::process::Command::new("osascript")
                .arg("-e")
                .arg(applescript_script(&cleanup))
                .status();
            return Ok(());
        }
        // If the swap's first half had already moved the bundle, put it back
        // before reporting the failure.
        if Path::new(&backup).exists() {
            let rollback = format!("mv {} {}", shell_quote(&backup), shell_quote(&current));
            let _ = std::process::Command::new("osascript")
                .arg("-e")
                .arg(applescript_script(&rollback))
                .status();
        }
        Err(offline_error(format!("the privileged swap exited with {status}")))
    }

    #[cfg(target_os = "macos")]
    pub(super) fn applescript_script(shell_command: &str) -> String {
        let escaped = shell_command.replace('\\', "\\\\").replace('"', "\\\"");
        format!("do shell script \"{escaped}\" with administrator privileges")
    }

    #[cfg(target_os = "macos")]
    pub(super) fn shell_quote(text: &str) -> String {
        format!("'{}'", text.replace('\'', "'\\''"))
    }

    #[cfg(target_os = "macos")]
    fn path_text(path: &Path) -> Result<String, BridgeError> {
        let text = path
            .to_str()
            .ok_or_else(|| offline_error("the bundle path is not valid UTF-8"))?;
        if text.chars().any(|ch| ch.is_control()) {
            return Err(offline_error("the bundle path contains a control character"));
        }
        Ok(text.to_string())
    }

    /// A sibling path that does not exist yet: the same directory is the same
    /// filesystem, so the renames above are atomic.
    #[cfg(target_os = "macos")]
    fn unique_sibling(parent: &Path, current: &Path, tag: &str) -> PathBuf {
        let name = current
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("ClipSync");
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or(0);
        parent.join(format!(
            ".{name}.clipsync-{tag}-{}-{stamp}",
            std::process::id()
        ))
    }

    #[cfg(target_os = "linux")]
    pub(super) fn install_linux_from_bytes(
        bytes: &[u8],
        signed_name: &str,
    ) -> Result<(), BridgeError> {
        match classify_installer(bytes, signed_name) {
            InstallerKind::Deb => install_package(bytes, ".deb", "dpkg", &["-i"]),
            InstallerKind::Rpm => install_package(bytes, ".rpm", "rpm", &["-U"]),
            InstallerKind::AppImage => install_appimage(bytes),
            InstallerKind::MacAppTarGz | InstallerKind::Unknown => {
                Err(offline_error("the signed update is not a Linux package or AppImage"))
            }
        }
    }

    /// A deb/rpm install through pkexec's usual authentication prompt.  The
    /// package is written to a temporary file that is removed on every exit.
    #[cfg(target_os = "linux")]
    fn install_package(
        bytes: &[u8],
        suffix: &str,
        program: &str,
        arguments: &[&str],
    ) -> Result<(), BridgeError> {
        use std::io::Write;

        let mut file = tempfile::Builder::new()
            .prefix("clipsync-update-")
            .suffix(suffix)
            .tempfile()
            .map_err(offline_error)?;
        file.write_all(bytes).map_err(offline_error)?;
        file.flush().map_err(offline_error)?;
        match std::process::Command::new("pkexec")
            .arg(program)
            .args(arguments)
            .arg(file.path())
            .status()
        {
            Ok(status) if status.success() => Ok(()),
            Ok(status) => Err(offline_error(format!("{program} exited with {status}"))),
            Err(err) => Err(offline_error(format!("could not run pkexec: {err}"))),
        }
    }

    /// Replace the running AppImage with the verified bytes: a same-directory
    /// temporary file keeps the rename atomic, and the old file is renamed to a
    /// backup first so a failure can roll it back.
    #[cfg(target_os = "linux")]
    fn install_appimage(bytes: &[u8]) -> Result<(), BridgeError> {
        use std::io::Write;

        let image = appimage_bytes(bytes)?;
        let current = current_appimage()
            .ok_or_else(|| offline_error("could not locate the running AppImage"))?;
        let parent = current
            .parent()
            .ok_or_else(|| offline_error("the AppImage has no parent directory"))?;
        let name = current
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("ClipSync.AppImage");
        let temp = parent.join(format!(".{name}.clipsync-new-{}", std::process::id()));
        let backup = parent.join(format!(".{name}.clipsync-backup-{}", std::process::id()));

        let write_result = std::fs::File::create(&temp).and_then(|mut handle| {
            handle.write_all(&image)?;
            handle.sync_all()
        });
        if let Err(err) = write_result {
            let _ = std::fs::remove_file(&temp);
            return Err(offline_error(err));
        }
        if let Ok(metadata) = std::fs::metadata(&current) {
            let _ = std::fs::set_permissions(&temp, metadata.permissions());
        }
        if backup.exists() {
            let _ = std::fs::remove_file(&backup);
        }
        if let Err(err) = std::fs::rename(&current, &backup) {
            let _ = std::fs::remove_file(&temp);
            return Err(offline_error(err));
        }
        if let Err(err) = std::fs::rename(&temp, &current) {
            // Roll the old AppImage back before reporting the failure.
            let _ = std::fs::rename(&backup, &current);
            let _ = std::fs::remove_file(&temp);
            return Err(offline_error(err));
        }
        let _ = std::fs::remove_file(&backup);
        Ok(())
    }

    #[cfg(target_os = "linux")]
    fn current_appimage() -> Option<PathBuf> {
        if let Some(value) = std::env::var_os("APPIMAGE") {
            let path = PathBuf::from(value);
            if path.is_file() {
                return Some(path);
            }
        }
        let exe = std::env::current_exe().ok()?;
        let is_appimage = exe
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.ends_with(".AppImage"));
        (is_appimage && exe.is_file()).then_some(exe)
    }

    #[cfg(not(target_os = "windows"))]
    pub(super) fn install_offline_from_bytes(
        bytes: &[u8],
        signed_name: &str,
    ) -> Result<(), BridgeError> {
        #[cfg(target_os = "macos")]
        {
            install_macos_from_bytes(bytes, signed_name)
        }
        #[cfg(target_os = "linux")]
        {
            install_linux_from_bytes(bytes, signed_name)
        }
        #[cfg(not(any(target_os = "macos", target_os = "linux")))]
        {
            let _ = (bytes, signed_name);
            Err(offline_error("offline install is unsupported on this platform"))
        }
    }
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
    fn a_group_name_is_a_name_and_not_the_absence_of_one() {
        // The bound is the one a favourite's own `group` field carries, and a
        // blank name is refused here rather than stored as a group called
        // nothing — the empty string means "no group", so it cannot also mean
        // a group.
        assert!(validate_group_name("Work").is_ok());
        assert!(validate_group_name(&"\u{1f600}".repeat(128)).is_ok());
        for name in ["", " ", "\t\n", &"a".repeat(129)] {
            assert_eq!(
                validate_group_name(name).unwrap_err().code,
                "VALIDATION_ERROR",
                "{name:?} should be refused"
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

    /// A real minisign vector: the payload, and the release key that signs it.
    ///
    /// Generated once for this test (legacy `Ed` algorithm, the mode the Tauri
    /// signer uses) so the verification path is exercised with bytes that
    /// actually satisfy minisign, not with a mock.
    const TEST_PUBKEY: &str =
        "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXkKUldRQkFnTUVCUVlIQ1BxaDdPZ1lESGliVUNMMGVaMHJ2djVGWFpwMDkrYXFXbm54c1V6cnh1R1UK";
    const TEST_SIGNATURE: &str =
        "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUldRQkFnTUVCUVlIQ0VqSm9VTGMxK09FL2h3S2xFdk1rUWJ4ZzRXTzgwcDJ2QnhEWmx4SS9GdjlKT2JUVHRQWmFhc1B6d1AwOUs1RitVM3YwazBSNk9OZmR3OUVKaEd2eXc0PQp0cnVzdGVkIGNvbW1lbnQ6IHRpbWVzdGFtcDoxNzUwMDAwMDAwCWZpbGU6Q2xpcFN5bmNfOS45LjlfeDY0LXNldHVwLmV4ZQpCbkI4TE96d0RjUkVacWsyWWFXN1RDcTVkZDMrZWZOcXNsRSt3RHRySC9kY2xUOVc2K0l3V0JaQ2Y3R3JxVFVIZG1KMDIwYnNXVm1GMWpwMjZRS0xCUT09Cg==";
    const TEST_BYTES: &[u8] = b"OFFLINE-PEER-UPDATE-BYTES";

    #[test]
    fn a_signed_release_names_its_file_and_version() {
        assert_eq!(
            verify_signed_release(TEST_BYTES, TEST_SIGNATURE, TEST_PUBKEY),
            Some((
                "ClipSync_9.9.9_x64-setup.exe".to_string(),
                "9.9.9".to_string()
            ))
        );
    }

    #[test]
    fn a_modified_payload_or_a_bad_key_does_not_verify() {
        let mut changed = TEST_BYTES.to_vec();
        changed[0] ^= 0xff;
        assert_eq!(
            verify_signed_release(&changed, TEST_SIGNATURE, TEST_PUBKEY),
            None
        );
        assert_eq!(
            verify_signed_release(TEST_BYTES, "not base64 at all", TEST_PUBKEY),
            None
        );
        assert_eq!(verify_signed_release(TEST_BYTES, TEST_SIGNATURE, ""), None);
    }

    #[test]
    fn an_offline_peer_update_must_be_signed_and_newer() {
        let current = semver::Version::parse("1.0.55").unwrap();
        assert_eq!(
            offline_signed_update(TEST_BYTES, TEST_SIGNATURE, TEST_PUBKEY, &current),
            Some((
                "ClipSync_9.9.9_x64-setup.exe".to_string(),
                "9.9.9".to_string()
            ))
        );

        let ahead = semver::Version::parse("99.0.0").unwrap();
        assert_eq!(
            offline_signed_update(TEST_BYTES, TEST_SIGNATURE, TEST_PUBKEY, &ahead),
            None,
            "a signed release older than this build is not an upgrade"
        );

        let mut changed = TEST_BYTES.to_vec();
        changed[1] ^= 0x01;
        assert_eq!(
            offline_signed_update(&changed, TEST_SIGNATURE, TEST_PUBKEY, &current),
            None
        );
        assert_eq!(
            offline_signed_update(TEST_BYTES, "", TEST_PUBKEY, &current),
            None,
            "no transferred signature is a manual install"
        );
    }

    #[test]
    fn the_signed_name_is_a_basename_and_the_version_is_the_dotted_run() {
        assert_eq!(
            signed_file_name("timestamp:1\tfile:/tmp/dir/ClipSync_1.2.3_x64-setup.exe"),
            Some("ClipSync_1.2.3_x64-setup.exe".to_string())
        );
        assert_eq!(
            signed_file_name("timestamp:1\tfile:..\\..\\evil.exe"),
            Some("evil.exe".to_string())
        );
        assert_eq!(
            version_in_name("ClipSync_1.0.55_x64-setup.exe"),
            Some("1.0.55".to_string())
        );
        assert_eq!(version_in_name("evil-setup.exe"), None);
    }

    #[test]
    fn installer_kinds_come_from_the_signed_name_and_the_magic() {
        use offline_update::InstallerKind::*;
        assert_eq!(
            offline_update::classify_installer(b"junk", "ClipSync_1.0.0_aarch64.app.tar.gz"),
            MacAppTarGz
        );
        assert_eq!(
            offline_update::classify_installer(b"!<arch>\ndeb", "ClipSync_1.0.0_amd64.deb"),
            Deb
        );
        assert_eq!(
            offline_update::classify_installer(b"\xed\xab\xee\xdbrpm", "ClipSync_1.0.0_x86_64.rpm"),
            Rpm
        );
        assert_eq!(
            offline_update::classify_installer(
                &[0x1f, 0x8b, 0x08, 0x00],
                "ClipSync_1.0.0_amd64.AppImage"
            ),
            AppImage
        );
        assert_eq!(
            offline_update::classify_installer(
                b"\x7fELF\x02\x01\x01",
                "ClipSync_1.0.0_amd64.AppImage"
            ),
            AppImage
        );
        assert_eq!(
            offline_update::classify_installer(b"random", "ClipSync_1.0.0_x64-setup.exe"),
            Unknown
        );
        assert_eq!(
            offline_update::classify_installer(b"\x7fELF\x02", "clipboard-note.txt"),
            AppImage,
            "magic wins over a name that says nothing"
        );
    }

    #[test]
    fn gzip_magic_is_recognised() {
        let payload = b"some bytes";
        let mut encoder =
            flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        std::io::Write::write_all(&mut encoder, payload).unwrap();
        let zipped = encoder.finish().unwrap();

        assert!(offline_update::is_gzip(&zipped));
        assert!(!offline_update::is_gzip(payload));
        assert!(!offline_update::is_gzip(b""));
        assert!(!offline_update::is_gzip(b"\x1f"));
    }

    #[test]
    fn tar_paths_that_escape_the_extraction_root_are_unsafe() {
        use std::path::Path;
        assert!(offline_update::tar_path_is_safe(Path::new(
            "ClipSync.app/Contents/Info.plist"
        )));
        assert!(offline_update::tar_path_is_safe(Path::new(
            "./ClipSync.app/Contents/Info.plist"
        )));
        assert!(!offline_update::tar_path_is_safe(Path::new("../evil.txt")));
        assert!(!offline_update::tar_path_is_safe(Path::new(
            "a/../../evil.txt"
        )));
        assert!(!offline_update::tar_path_is_safe(Path::new("/tmp/evil.txt")));
    }

    #[test]
    fn the_app_bundle_is_found_by_walking_up_from_the_executable() {
        let exe = std::path::Path::new("/Applications/ClipSync.app/Contents/MacOS/ClipSync");
        assert_eq!(
            offline_update::app_bundle_for_exe(exe),
            Some(std::path::PathBuf::from("/Applications/ClipSync.app"))
        );
        assert_eq!(
            offline_update::app_bundle_for_exe(std::path::Path::new("/usr/bin/clipsync")),
            None
        );
    }

    #[test]
    fn the_top_level_app_bundle_is_found_under_the_extraction_root() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("ClipSync.app/Contents")).unwrap();
        std::fs::create_dir_all(root.path().join("other")).unwrap();
        assert_eq!(
            offline_update::find_app_bundle(root.path()),
            Some(root.path().join("ClipSync.app"))
        );
        std::fs::remove_dir_all(root.path().join("ClipSync.app")).unwrap();
        assert_eq!(offline_update::find_app_bundle(root.path()), None);
    }

    // ── archive-level tests: compiled and run on the unix CI legs ────────

    #[cfg(unix)]
    fn app_tar_gz() -> Vec<u8> {
        let source = tempfile::tempdir().unwrap();
        let app = source.path().join("ClipSync.app");
        std::fs::create_dir_all(app.join("Contents/MacOS")).unwrap();
        std::fs::create_dir_all(app.join("Contents/Resources")).unwrap();
        std::fs::write(app.join("Contents/Info.plist"), b"<plist/>").unwrap();
        std::fs::write(app.join("Contents/MacOS/ClipSync"), b"binary").unwrap();
        std::fs::write(
            app.join("Contents/Resources")
                .join(format!("{}.dat", "L".repeat(120))),
            b"long data",
        )
        .unwrap();

        let encoder =
            flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        let mut builder = tar::Builder::new(encoder);
        builder.append_dir_all("ClipSync.app", &app).unwrap();
        builder.into_inner().unwrap().finish().unwrap()
    }

    #[cfg(unix)]
    #[test]
    fn an_app_tar_gz_is_extracted_and_its_bundle_found() {
        let (directory, bundle) = offline_update::extract_app_bundle(&app_tar_gz()).unwrap();
        assert_eq!(bundle.file_name().unwrap(), "ClipSync.app");
        assert_eq!(
            std::fs::read(bundle.join("Contents/Info.plist")).unwrap(),
            b"<plist/>"
        );
        let long = bundle
            .join("Contents/Resources")
            .join(format!("{}.dat", "L".repeat(120)));
        assert_eq!(std::fs::read(long).unwrap(), b"long data");

        // The directory owns the extracted tree until it is dropped.
        drop(directory);
        assert!(!bundle.exists());
    }

    #[cfg(unix)]
    fn tar_gz_raw_name(name: &str, data: &[u8]) -> Vec<u8> {
        let encoder =
            flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        let mut builder = tar::Builder::new(encoder);
        let mut header = tar::Header::new_gnu();
        header.set_size(data.len() as u64);
        header.set_mode(0o644);
        header.set_entry_type(tar::EntryType::Regular);
        let raw = name.as_bytes();
        assert!(raw.len() < 100, "test entry name is too long");
        header.as_gnu_mut().unwrap().name[..raw.len()].copy_from_slice(raw);
        header.set_cksum();
        builder.append(&header, data).unwrap();
        builder.into_inner().unwrap().finish().unwrap()
    }

    #[cfg(unix)]
    #[test]
    fn a_tar_entry_that_escapes_the_root_is_refused() {
        assert!(offline_update::extract_app_bundle(&tar_gz_raw_name("../evil.txt", b"x")).is_err());
        assert!(
            offline_update::extract_app_bundle(&tar_gz_raw_name("/tmp/evil.txt", b"x")).is_err()
        );
        assert!(
            offline_update::appimage_bytes(&tar_gz_raw_name("../evil.AppImage", b"\x7fELF"))
                .is_err()
        );
    }

    #[cfg(unix)]
    #[test]
    fn an_appimage_is_taken_from_a_gzip_tar_or_from_the_raw_bytes() {
        let payload: &[u8] = b"\x7fELF-appimage-payload";
        let encoder =
            flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        let mut builder = tar::Builder::new(encoder);
        let mut header = tar::Header::new_gnu();
        header.set_size(payload.len() as u64);
        header.set_mode(0o755);
        header.set_entry_type(tar::EntryType::Regular);
        header.set_path("ClipSync_9.9.9_amd64.AppImage").unwrap();
        header.set_cksum();
        builder.append(&header, payload).unwrap();
        let archive = builder.into_inner().unwrap().finish().unwrap();

        assert_eq!(offline_update::appimage_bytes(&archive).unwrap(), payload);
        assert_eq!(offline_update::appimage_bytes(payload).unwrap(), payload);
        assert!(offline_update::appimage_bytes(b"not an image").is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn applescript_script_escapes_the_shell_command_it_embeds() {
        let script = offline_update::applescript_script(r#"rm -rf '/tmp/a"b\c'"#);
        assert!(script.starts_with("do shell script \""));
        assert!(script.ends_with("\" with administrator privileges"));
        assert!(script.contains("\\\\"), "backslash must be escaped");
        assert!(script.contains("\\\""), "double quote must be escaped");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn a_macos_offline_install_refuses_a_payload_that_is_not_an_app_tar_gz() {
        assert!(offline_update::install_macos_from_bytes(
            b"not a tar",
            "ClipSync_1.0.0_x64-setup.exe"
        )
        .is_err());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_linux_offline_install_refuses_a_payload_it_cannot_classify() {
        assert!(offline_update::install_linux_from_bytes(
            b"not an installer",
            "ClipSync_1.0.0_x64-setup.exe"
        )
        .is_err());
    }
}
