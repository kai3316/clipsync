use crate::error::BridgeError;
use crate::protocol;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager};
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncWrite, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, Command},
    sync::{oneshot, watch, Mutex as AsyncMutex},
};

const MAX_FRAME: usize = 1024 * 1024;
type Reply = oneshot::Sender<Result<Value, BridgeError>>;

fn fail_pending(
    pending: &Mutex<HashMap<String, Reply>>,
    ready: &watch::Sender<Option<Result<(), BridgeError>>>,
    error: BridgeError,
) {
    let mut pending = pending.lock().unwrap();
    if matches!(&*ready.borrow(), Some(Err(_))) {
        return;
    }
    ready.send_replace(Some(Err(error.clone())));
    for (_, reply) in pending.drain() {
        let _ = reply.send(Err(error.clone()));
    }
}

/// Await a bridge's terminal failure. Readiness is a `watch` cell that starts
/// empty, becomes `Ok` when the sidecar reports ready, and is replaced by the
/// first terminal `Err` — a launch failure, a startup timeout, or a sidecar
/// that died after becoming ready. Waiting here therefore distinguishes
/// "never came up" from "came up and then died", which is exactly the
/// distinction the relaunch policy is built on.
pub(crate) async fn wait_failure(
    mut ready: watch::Receiver<Option<Result<(), BridgeError>>>,
) -> BridgeError {
    loop {
        if let Some(Err(error)) = ready.borrow().clone() {
            return error;
        }
        if ready.changed().await.is_err() {
            return BridgeError::unavailable();
        }
    }
}

/// The reason carried by a sidecar `app.restart_requested` event, or None when
/// the frame is not one. The sidecar cannot relaunch itself (this host spawned
/// it and holds its stdio), so the web panel's restart and factory-reset
/// actions travel back to the host as this event.
fn restart_request_reason(value: &Value) -> Option<String> {
    if value["type"] != "event" || value["name"] != "app.restart_requested" {
        return None;
    }
    Some(value["data"]["reason"].as_str().unwrap_or("").to_owned())
}

/// Whether a sidecar frame says something the native tray is drawing has
/// changed. The tray reads its state from the sidecar rather than the renderer,
/// so this covers the web UI and the phone — which never reach a Tauri command —
/// as well as the tray's own controls.
///
/// Only the events that can change what the tray currently shows: the device
/// name and the web companion behind `settings.changed`, the sync switch and
/// the armed pause deadline behind `sync.state.changed` — which is what
/// `sync.pause`, `sync.resume` and the sidecar's own auto-resume timer all
/// publish — and the peers submenu behind `devices.changed`.
/// `app.status.changed` is `sync.set_enabled`'s own.
///
/// Discovery and history events are deliberately not here: the tray draws no
/// discovery progress and no history, so refreshing on them would be a round
/// trip each for no visible difference.
fn changes_the_tray(value: &Value) -> bool {
    value["type"] == "event"
        && matches!(
            value["name"].as_str(),
            Some(
                "settings.changed"
                    | "sync.state.changed"
                    | "app.status.changed"
                    | "devices.changed"
            )
        )
}

/// Whether a sidecar frame is the phone asking this host to close its window.
/// Legacy closed only the webview window and kept the process running, which is
/// what hiding the main window to the tray does here.
fn hides_main_window(value: &Value) -> bool {
    value["type"] == "event" && value["name"] == "app.window_close_requested"
}

pub struct Bridge {
    input: AsyncMutex<ChildStdin>,
    child: AsyncMutex<Child>,
    pending: Mutex<HashMap<String, Reply>>,
    ready: watch::Sender<Option<Result<(), BridgeError>>>,
    session: Mutex<Option<String>>,
    stopping: std::sync::atomic::AtomicBool,
    notifications: tokio::sync::mpsc::Sender<Value>,
}

struct PendingGuard<'a> {
    pending: &'a Mutex<HashMap<String, Reply>>,
    id: String,
}

impl Drop for PendingGuard<'_> {
    fn drop(&mut self) {
        self.pending.lock().unwrap().remove(&self.id);
    }
}

async fn write_frame<W: AsyncWrite + Unpin>(
    input: &mut W,
    frame: &[u8],
    deadline: Duration,
) -> Result<(), BridgeError> {
    tokio::time::timeout(deadline, input.write_all(frame))
        .await
        .map_err(|_| BridgeError::unavailable())?
        .map_err(|_| BridgeError::unavailable())
}

pub async fn read_frame<R: AsyncBufRead + Unpin>(
    reader: &mut R,
) -> Result<Option<Vec<u8>>, BridgeError> {
    let mut frame = Vec::new();
    loop {
        let buffer = reader
            .fill_buf()
            .await
            .map_err(|_| BridgeError::unavailable())?;
        if buffer.is_empty() {
            return if frame.is_empty() {
                Ok(None)
            } else {
                Err(BridgeError::new("PROTOCOL_ERROR", "Truncated IPC frame"))
            };
        }
        let end = buffer.iter().position(|byte| *byte == b'\n');
        let take = end.map_or(buffer.len(), |position| position + 1);
        if frame.len() + take > MAX_FRAME + 1 {
            return Err(BridgeError::new(
                "PROTOCOL_ERROR",
                "IPC frame exceeds limit",
            ));
        }
        frame.extend_from_slice(&buffer[..take]);
        reader.consume(take);
        if end.is_some() {
            frame.pop();
            return Ok(Some(frame));
        }
    }
}

impl Bridge {
    pub async fn start(app: AppHandle) -> Result<Arc<Self>, BridgeError> {
        let mut command = Self::command()?;
        command
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true);
        #[cfg(windows)]
        command.creation_flags(0x08000000);
        let mut child = command.spawn().map_err(|_| {
            BridgeError::new(
                "SIDECAR_START_FAILED",
                "Could not launch the Python sidecar",
            )
        })?;
        let input = child.stdin.take().ok_or_else(BridgeError::unavailable)?;
        let output = child.stdout.take().ok_or_else(BridgeError::unavailable)?;
        let stderr = child.stderr.take().ok_or_else(BridgeError::unavailable)?;
        let (ready, _) = watch::channel(None);
        let (notifications, mut notification_events) = tokio::sync::mpsc::channel(32);
        let bridge = Arc::new(Self {
            input: AsyncMutex::new(input),
            child: AsyncMutex::new(child),
            pending: Mutex::new(HashMap::new()),
            ready,
            session: Mutex::new(None),
            stopping: std::sync::atomic::AtomicBool::new(false),
            notifications,
        });
        let notification_bridge = Arc::downgrade(&bridge);
        let notification_app = app.clone();
        tauri::async_runtime::spawn(async move {
            let mut last_sequence = 0;
            while let Some(event) = notification_events.recv().await {
                let Some(bridge) = notification_bridge.upgrade() else { break };
                if bridge.is_stopping() { break; }
                let sequence = event["seq"].as_u64().unwrap_or(0);
                if sequence <= last_sequence { continue; }
                last_sequence = sequence;
                crate::notifications::deliver(&bridge, &notification_app, &event).await;
            }
        });
        let startup = bridge.clone();
        let startup_app = app.clone();
        tauri::async_runtime::spawn(async move {
            if tokio::time::timeout(Duration::from_secs(15), startup.wait_ready())
                .await
                .is_err()
            {
                startup
                    .terminate(
                        &startup_app,
                        BridgeError::new("STARTUP_TIMEOUT", "Sidecar did not become ready"),
                    )
                    .await;
            }
        });
        let active = bridge.clone();
        tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(output);
            let result = async {
                while let Some(frame) = read_frame(&mut reader).await? {
                    let value = protocol::decode(&frame)?;
                    active.receive(&app, value)?;
                }
                Err::<(), _>(BridgeError::unavailable())
            }
            .await;
            let error = result.unwrap_err();
            if !active.stopping.load(std::sync::atomic::Ordering::SeqCst) {
                active.terminate(&app, error).await;
            }
        });
        // Drain without forwarding raw sensitive diagnostics to the renderer.
        tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(stderr);
            let mut sink = tokio::io::sink();
            let _ = tokio::io::copy(&mut reader, &mut sink).await;
        });
        Ok(bridge)
    }

    fn command() -> Result<Command, BridgeError> {
        #[cfg(debug_assertions)]
        {
            let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../..")
                .canonicalize()
                .map_err(|_| BridgeError::unavailable())?;
            let mut command = Command::new(
                std::env::var_os("CLIPSYNC_PYTHON").unwrap_or_else(|| "python".into()),
            );
            command
                .current_dir(&root)
                .args(["-u", "-m", "src.sidecar_main"]);
            // Development never opens the user's real clipboard database by default.
            if std::env::var_os("CLIPSYNC_CONFIG_DIR").is_none() {
                command.env("CLIPSYNC_CONFIG_DIR", root.join(".tauri-dev-data"));
            }
            Ok(command)
        }
        #[cfg(not(debug_assertions))]
        {
            let exe = std::env::current_exe().map_err(|_| BridgeError::unavailable())?;
            let name = if cfg!(windows) {
                "clipsync-sidecar.exe"
            } else {
                "clipsync-sidecar"
            };
            Ok(Command::new(exe.with_file_name(name)))
        }
    }

    fn receive(self: &Arc<Self>, app: &AppHandle, value: Value) -> Result<(), BridgeError> {
        let mut session = self.session.lock().unwrap();
        if matches!(&*self.ready.borrow(), Some(Err(_))) {
            return Err(BridgeError::unavailable());
        }
        protocol::validate(&value, session.as_deref())?;
        match value["type"].as_str() {
            Some("ready") => {
                if value["protocol"] != 1 || !value["session_id"].is_string() {
                    return Err(BridgeError::new(
                        "PROTOCOL_MISMATCH",
                        "Unsupported sidecar protocol",
                    ));
                }
                if self.ready.borrow().is_some() {
                    return Err(BridgeError::new(
                        "PROTOCOL_ERROR",
                        "Duplicate ready message",
                    ));
                }
                // Serialize readiness with failure and pending insertion.
                let _pending = self.pending.lock().unwrap();
                if matches!(&*self.ready.borrow(), Some(Err(_))) {
                    return Err(BridgeError::unavailable());
                }
                *session = value["session_id"].as_str().map(str::to_owned);
                self.ready.send_replace(Some(Ok(())));
                let _ = app.emit_to("main", "sidecar:state", json!({"state": "ready"}));
            }
            Some("response") => {
                let id = value["id"]
                    .as_str()
                    .ok_or_else(|| BridgeError::new("PROTOCOL_ERROR", "Missing response ID"))?;
                // Late replies after timeout are harmless; mutations are never replayed.
                if let Some(reply) = self.pending.lock().unwrap().remove(id) {
                    let result = if value["ok"] == true {
                        Ok(value["result"].clone())
                    } else {
                        Err(Self::remote_error(&value["error"]))
                    };
                    let _ = reply.send(result);
                }
            }
            Some("event") | Some("resync") => {
                if !value["session_id"].is_string() {
                    return Err(BridgeError::new("PROTOCOL_ERROR", "Invalid event session"));
                }
                if value["type"] == "event" && crate::notifications::candidate(&value) {
                    // Never wait for notification RPC replies inside the stdout reader.
                    let _ = self.notifications.try_send(value.clone());
                }
                // The native tray follows the saved state; this fires for
                // changes made from the web UI or the phone too, which never
                // reach a Tauri command.
                if changes_the_tray(&value) {
                    let handle = app.clone();
                    tauri::async_runtime::spawn(async move { crate::tray::refresh(&handle).await });
                }
                if let Some(reason) = restart_request_reason(&value) {
                    // Spawn rather than await: stop() waits for a reply only
                    // this reader task can deliver, and restart() must not run
                    // inside it. A factory reset also drops the webview's own
                    // storage, exactly like the native command does.
                    let bridge = self.clone();
                    let handle = app.clone();
                    tauri::async_runtime::spawn(async move {
                        if reason == "factory_reset" {
                            if let Some(window) = handle.get_webview_window("main") {
                                let _ = window.eval(
                                    "try { localStorage.clear(); sessionStorage.clear(); } \
                                     catch (e) {}",
                                );
                            }
                        }
                        bridge.stop().await;
                        handle.restart();
                    });
                }
                if hides_main_window(&value) {
                    // Hide, never exit: the phone's close button only dismisses
                    // the desktop window, exactly like the legacy webview.
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.hide();
                    }
                }
                let _ = app.emit_to("main", "sidecar:event", value);
            }
            Some("fatal") => return Err(Self::remote_error(&value["error"])),
            _ => return Err(BridgeError::new("PROTOCOL_ERROR", "Unknown sidecar frame")),
        }
        Ok(())
    }

    pub(crate) fn is_stopping(&self) -> bool {
        self.stopping.load(std::sync::atomic::Ordering::SeqCst)
    }

    pub(crate) fn remote_error(value: &Value) -> BridgeError {
        BridgeError {
            code: value["code"].as_str().unwrap_or("INTERNAL_ERROR").into(),
            message: value["message"]
                .as_str()
                .unwrap_or("Operation failed")
                .into(),
            retryable: value["retryable"].as_bool().unwrap_or(false),
        }
    }

    fn fail(&self, error: BridgeError) {
        fail_pending(&self.pending, &self.ready, error);
    }

    async fn terminate(&self, app: &AppHandle, error: BridgeError) {
        self.fail(error.clone());
        let _ = app.emit_to(
            "main",
            "sidecar:state",
            json!({"state":"failed","error":error.code}),
        );
        let mut child = self.child.lock().await;
        let _ = child.kill().await;
        let _ = child.wait().await;
    }

    pub(crate) async fn wait_ready(&self) -> Result<(), BridgeError> {
        let mut ready = self.ready.subscribe();
        loop {
            if let Some(result) = ready.borrow().clone() {
                return result;
            }
            ready
                .changed()
                .await
                .map_err(|_| BridgeError::unavailable())?;
        }
    }

    /// Resolve once this bridge reaches a terminal failure. The first `Err` a
    /// bridge reports is final — `fail_pending` never overwrites one — so the
    /// host's supervisor awaits this to learn that a ready sidecar has died.
    pub(crate) async fn wait_failure(&self) -> BridgeError {
        wait_failure(self.ready.subscribe()).await
    }

    pub async fn call(self: &Arc<Self>, method: &str, params: Value) -> Result<Value, BridgeError> {
        self.request(method, params, false).await
    }

    async fn request(
        self: &Arc<Self>,
        method: &str,
        params: Value,
        shutdown: bool,
    ) -> Result<Value, BridgeError> {
        self.wait_ready().await?;
        let id = uuid::Uuid::new_v4().to_string();
        let mut frame = serde_json::to_vec(&json!({
            "type": "request", "id": id, "method": method, "params": params,
            "correlation_id": id,
        }))
        .map_err(|_| BridgeError::new("VALIDATION_ERROR", "Invalid request"))?;
        if frame.len() > MAX_FRAME {
            return Err(BridgeError::new(
                "VALIDATION_ERROR",
                "Request exceeds IPC limit",
            ));
        }
        frame.push(b'\n');
        let (send, receive) = oneshot::channel();
        {
            let mut pending = self.pending.lock().unwrap();
            if self.stopping.load(std::sync::atomic::Ordering::SeqCst) && !shutdown {
                return Err(BridgeError::unavailable());
            }
            if let Some(Err(error)) = &*self.ready.borrow() {
                return Err(error.clone());
            }
            if pending.len() >= 128 {
                return Err(BridgeError::new("BUSY", "Too many pending commands"));
            }
            pending.insert(id.clone(), send);
        }
        let _cleanup = PendingGuard {
            pending: &self.pending,
            id: id.clone(),
        };
        let writer = self.clone();
        // The write owns its lock independently of caller cancellation. A timed-out
        // partial write poisons the session before another writer can acquire it.
        tauri::async_runtime::spawn(async move {
            let mut input = writer.input.lock().await;
            if !writer.pending.lock().unwrap().contains_key(&id)
                || matches!(&*writer.ready.borrow(), Some(Err(_)))
            {
                return;
            }
            if write_frame(&mut *input, &frame, Duration::from_secs(5))
                .await
                .is_err()
            {
                writer.fail(BridgeError::unavailable());
                let mut child = writer.child.lock().await;
                let _ = child.kill().await;
                let _ = child.wait().await;
            }
        });
        let result = tokio::time::timeout(Duration::from_secs(30), async {
            receive.await.map_err(|_| BridgeError::unavailable())?
        })
        .await
        .unwrap_or_else(|_| {
            Err(BridgeError::new(
                "REQUEST_TIMEOUT",
                "Result unknown; refresh state before repeating the operation",
            ))
        });
        result
    }

    pub async fn stop(self: &Arc<Self>) {
        if self
            .stopping
            .swap(true, std::sync::atomic::Ordering::SeqCst)
        {
            return;
        }
        let _ = tokio::time::timeout(
            Duration::from_secs(2),
            self.request("app.shutdown", json!({}), true),
        )
        .await;
        self.fail(BridgeError::unavailable());
        let _ = tokio::time::timeout(Duration::from_secs(1), async {
            self.input.lock().await.shutdown().await
        })
        .await;
        let mut child = self.child.lock().await;
        // LAN cleanup may need two five-second stop attempts before process exit.
        if !matches!(
            tokio::time::timeout(Duration::from_secs(12), child.wait()).await,
            Ok(Ok(_))
        ) {
            let _ = child.kill().await;
            let _ = child.wait().await;
        }
        self.fail(BridgeError::unavailable());
    }
}

/// Run the sidecar once in `--recover` mode and return the items it moved aside.
///
/// Recovery cannot be an ordinary RPC call: the sidecar refuses to start on
/// exactly the damage this repairs, so there is no session to send a request to
/// and no bridge to send it through. The host instead runs the same executable
/// as a one-shot child, reads the single frame it writes, and lets it exit.
pub(crate) async fn recover() -> Result<Value, BridgeError> {
    let mut command = Bridge::command()?;
    command
        .arg("--recover")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    let mut child = command.spawn().map_err(|_| {
        BridgeError::new(
            "SIDECAR_START_FAILED",
            "Could not launch the Python sidecar",
        )
    })?;
    let output = child.stdout.take().ok_or_else(BridgeError::unavailable)?;
    // The frame arrives before the process does any other work, so this bound
    // only covers moving files aside and opening the history database.
    let frame = tokio::time::timeout(Duration::from_secs(30), async {
        read_frame(&mut BufReader::new(output)).await
    })
    .await
    .map_err(|_| BridgeError::new("RECOVERY_TIMEOUT", "Data recovery did not finish"))??;
    // Wait for the child, but not forever: a process that lingers after writing
    // its frame must not hold the click that asked for the repair.
    if !matches!(
        tokio::time::timeout(Duration::from_secs(5), child.wait()).await,
        Ok(Ok(_))
    ) {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
    let frame = frame.ok_or_else(|| {
        BridgeError::new("RECOVERY_FAILED", "The sidecar stopped without reporting")
    })?;
    let value = protocol::decode(&frame)?;
    protocol::validate(&value, None)?;
    match value["type"].as_str() {
        Some("recovered") => Ok(value["items"].clone()),
        Some("fatal") => Err(Bridge::remote_error(&value["error"])),
        _ => Err(protocol::invalid()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn terminal_failure_drains_pending_and_preserves_first_error() {
        let pending = Mutex::new(HashMap::new());
        let (ready, _) = watch::channel(None);
        let (send, receive) = oneshot::channel();
        pending.lock().unwrap().insert("id".into(), send);
        fail_pending(
            &pending,
            &ready,
            BridgeError::new("STARTUP_TIMEOUT", "timeout"),
        );
        fail_pending(&pending, &ready, BridgeError::unavailable());
        assert!(pending.lock().unwrap().is_empty());
        assert_eq!(receive.await.unwrap().unwrap_err().code, "STARTUP_TIMEOUT");
        assert_eq!(
            ready.borrow().as_ref().unwrap().as_ref().unwrap_err().code,
            "STARTUP_TIMEOUT"
        );
    }

    #[tokio::test]
    async fn a_running_bridge_reports_no_failure_until_it_dies() {
        // Ready-then-dead is the supervisor's relaunch trigger, so `wait_failure`
        // must stay pending while the sidecar is merely running.
        let (ready, receiver) = watch::channel(None);
        ready.send_replace(Some(Ok(())));
        let waiting = tokio::spawn(wait_failure(receiver));
        tokio::task::yield_now().await;
        assert!(!waiting.is_finished(), "ready is not a failure");
        fail_pending(
            &Mutex::new(HashMap::new()),
            &ready,
            BridgeError::new("SIDECAR_UNAVAILABLE", "died"),
        );
        assert_eq!(waiting.await.unwrap().code, "SIDECAR_UNAVAILABLE");
    }

    #[tokio::test]
    async fn a_failure_that_already_happened_resolves_immediately() {
        // The supervisor arms after `Bridge::start` returns, which is also how a
        // launch failure or a startup timeout is reported.
        let (ready, receiver) = watch::channel(None);
        fail_pending(
            &Mutex::new(HashMap::new()),
            &ready,
            BridgeError::new("STARTUP_TIMEOUT", "timeout"),
        );
        assert_eq!(wait_failure(receiver).await.code, "STARTUP_TIMEOUT");
    }

    #[tokio::test]
    async fn a_dropped_readiness_channel_is_a_terminal_failure() {
        // The sender lives inside the Bridge; if a supervisor ever outlives it,
        // it must give up rather than wait forever.
        let (ready, receiver) = watch::channel(None);
        drop(ready);
        assert_eq!(wait_failure(receiver).await.code, "SIDECAR_UNAVAILABLE");
    }

    #[test]
    fn only_a_restart_event_asks_the_host_to_relaunch() {
        let event = |name: &str, reason: Value| {
            json!({"type":"event","name":name,"data":{"reason":reason}})
        };
        assert_eq!(
            restart_request_reason(&event("app.restart_requested", json!("factory_reset"))),
            Some("factory_reset".into())
        );
        assert_eq!(
            restart_request_reason(&event("app.restart_requested", json!("restart"))),
            Some("restart".into())
        );
        // A missing reason still restarts; the reason only selects the
        // extra webview-storage clear.
        assert_eq!(
            restart_request_reason(&json!({"type":"event","name":"app.restart_requested"})),
            Some(String::new())
        );
        assert_eq!(restart_request_reason(&event("settings.changed", json!("x"))), None);
        assert_eq!(
            restart_request_reason(&json!({"type":"response","name":"app.restart_requested"})),
            None
        );
    }

    #[test]
    fn only_the_state_events_refresh_the_tray() {
        let event = |name: &str| json!({"type":"event","name":name});
        for name in [
            "settings.changed",
            "sync.state.changed",
            "app.status.changed",
            "devices.changed",
        ] {
            assert!(changes_the_tray(&event(name)), "{name} did not refresh");
        }
        // Discovery and history churn constantly and change nothing the tray
        // currently draws; a refresh each would be a round trip per event for
        // no visible difference.
        for name in [
            "discovery.changed",
            "history.changed",
            "settings.live_applied",
            "pairing.resolved",
            "app.window_close_requested",
        ] {
            assert!(!changes_the_tray(&event(name)), "{name} refreshed");
        }
        // A response can never be a change, even with a matching name.
        assert!(!changes_the_tray(
            &json!({"type":"response","name":"settings.changed"})
        ));
        assert!(!changes_the_tray(&json!({"type":"event"})));
    }

    #[test]
    fn only_the_close_request_event_hides_the_main_window() {
        assert!(hides_main_window(
            &json!({"type":"event","name":"app.window_close_requested"})
        ));
        assert!(!hides_main_window(
            &json!({"type":"event","name":"app.qr_requested"})
        ));
        assert!(!hides_main_window(
            &json!({"type":"event","name":"settings.changed"})
        ));
        // A response can never carry a request, even with a matching name.
        assert!(!hides_main_window(
            &json!({"type":"response","name":"app.window_close_requested"})
        ));
    }

    #[tokio::test]
    async fn frames_preserve_utf8_and_boundaries() {
        let mut input = BufReader::new(&b"{\"ok\":true}\n{}\n"[..]);
        assert_eq!(
            read_frame(&mut input).await.unwrap().unwrap(),
            b"{\"ok\":true}"
        );
        assert_eq!(read_frame(&mut input).await.unwrap().unwrap(), b"{}");
        assert!(read_frame(&mut input).await.unwrap().is_none());
    }

    #[tokio::test]
    async fn oversized_and_truncated_frames_fail() {
        let input = vec![b'a'; MAX_FRAME + 2];
        assert!(read_frame(&mut BufReader::new(input.as_slice()))
            .await
            .is_err());
        assert!(read_frame(&mut BufReader::new(&b"partial"[..]))
            .await
            .is_err());
    }

    #[tokio::test]
    async fn exact_frame_limit_is_accepted() {
        let mut bytes = vec![b' '; MAX_FRAME];
        bytes.push(b'\n');
        assert_eq!(
            read_frame(&mut BufReader::new(bytes.as_slice()))
                .await
                .unwrap()
                .unwrap()
                .len(),
            MAX_FRAME
        );
        bytes.insert(0, b' ');
        assert!(read_frame(&mut BufReader::new(bytes.as_slice()))
            .await
            .is_err());
    }

    #[tokio::test]
    async fn canceled_request_removes_pending_sender() {
        let pending = Mutex::new(HashMap::new());
        let (send, receive) = oneshot::channel();
        pending.lock().unwrap().insert("id".into(), send);
        let request = async {
            let _guard = PendingGuard {
                pending: &pending,
                id: "id".into(),
            };
            std::future::pending::<()>().await;
        };
        assert!(tokio::time::timeout(Duration::from_millis(10), request)
            .await
            .is_err());
        assert!(pending.lock().unwrap().is_empty());
        assert!(receive.await.is_err());
    }

    #[tokio::test]
    async fn blocked_partial_write_has_bounded_deadline() {
        let (mut input, _output) = tokio::io::duplex(4);
        assert!(
            write_frame(&mut input, b"long frame\n", Duration::from_millis(10))
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn serialized_writes_survive_caller_cancellation() {
        use tokio::io::AsyncReadExt;
        let (input, mut output) = tokio::io::duplex(1);
        let input = Arc::new(AsyncMutex::new(input));
        let writer = input.clone();
        let (started, start) = oneshot::channel();
        let first = tokio::spawn(async move {
            let mut pipe = writer.lock().await;
            let _ = started.send(());
            write_frame(&mut *pipe, b"first\n", Duration::from_secs(2))
                .await
                .unwrap();
        });
        start.await.unwrap();
        // Dropping the caller's handle must not cancel the independently owned write.
        drop(first);
        let second = tokio::spawn(async move {
            write_frame(
                &mut *input.lock().await,
                b"second\n",
                Duration::from_secs(2),
            )
            .await
            .unwrap();
        });
        let mut bytes = [0; 13];
        tokio::time::timeout(Duration::from_secs(2), output.read_exact(&mut bytes))
            .await
            .unwrap()
            .unwrap();
        second.await.unwrap();
        assert_eq!(&bytes, b"first\nsecond\n");
    }
}
