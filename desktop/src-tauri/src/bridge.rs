use crate::error::BridgeError;
use crate::protocol;
use serde_json::{json, Value};
use std::{
    collections::{HashMap, VecDeque},
    path::PathBuf,
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
/// How much of the sidecar's stderr is kept to explain a failure. Bounded
/// because it is a live process's output and this must not grow with runtime.
const STDERR_LINES: usize = 40;
/// One stored line. The last line of a traceback is the exception, and a
/// `dyld` refusal is a sentence, so this is generous rather than tight.
const STDERR_LINE_CHARS: usize = 300;
type Reply = oneshot::Sender<Result<Value, BridgeError>>;

/// The tail of a sidecar's stderr.
///
/// Kept because a sidecar that dies before it reports ready otherwise leaves
/// no account of why anywhere: the app's own log view is an RPC to that same
/// dead process, so the failure reached the user as "background process is
/// unavailable" and nothing else.
#[derive(Default)]
struct StderrTail(Mutex<VecDeque<String>>);

impl StderrTail {
    fn note(&self, line: &str) {
        let line = line.trim();
        // Blank lines are dropped rather than stored: the buffer is a fixed
        // number of lines, and a traceback's empty separators would spend
        // them on nothing.
        if line.is_empty() {
            return;
        }
        let mut tail = self.0.lock().unwrap();
        if tail.len() == STDERR_LINES {
            tail.pop_front();
        }
        tail.push_back(line.chars().take(STDERR_LINE_CHARS).collect());
    }

    fn last(&self) -> Option<String> {
        self.0.lock().unwrap().back().cloned()
    }
}

/// The program a command will run. `as_std_mut` because tokio's `Command`
/// exposes no accessor of its own.
fn program_of(command: &mut tokio::process::Command) -> PathBuf {
    PathBuf::from(command.as_std_mut().get_program())
}

/// The failure to report when the sidecar could not be launched at all.
///
/// Names the file it tried.  In a packaged build the sidecar is one specific
/// path next to the main executable, so the bare sentence leaves a user -- and
/// a bug report -- unable to tell a file missing from the bundle from one that
/// is there and will not run; and the OS error that follows says which.  The
/// sentence this detail lands in is `i18n`'s, which is why none is built here.
fn launch_failed(program: &std::path::Path, error: &std::io::Error) -> BridgeError {
    BridgeError::new(
        "SIDECAR_START_FAILED",
        &format!("{}: {error}", program.display()),
    )
}

/// Attach the sidecar's own last words to a failure that has no reason of its
/// own.
///
/// A sidecar that dies before reporting ready produced "ClipSync background
/// process is unavailable" and nothing more, from a build where the one
/// account of the cause -- its stderr -- was being copied into `io::sink()`.
/// This is what turns that into something a user can act on or report.  It
/// goes to the window the error is already drawn in, on the user's own
/// machine, which is why a diagnosis here is not a disclosure.
///
/// The line becomes the error's whole message: the sentence around it belongs
/// to `i18n`, which is where it can be written in the language on screen, and
/// appending to a message here meant composing the two in one language.
///
/// Only the two codes that mean "the process itself failed": the rest carry a
/// reason already, and a refused data directory or a rejected frame has no
/// business being annotated with unrelated stderr noise.
fn explain(error: BridgeError, line: Option<String>) -> BridgeError {
    if error.code != "SIDECAR_UNAVAILABLE" && error.code != "STARTUP_TIMEOUT" {
        return error;
    }
    let Some(line) = line else {
        return error;
    };
    // One line, bounded: this rides in a banner, and a whole traceback would
    // push the sentence it is explaining off the end of it.
    error.with_message(line)
}

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

/// Whether a sidecar frame is an update a peer sent, checked and staged here.
///
/// Two things the frame has to say, and both are load-bearing: `ready` is what
/// makes the archive installable at all, and `source` is what tells a peer's
/// blob from this machine's own download. Whether the file is still on disk is
/// the installer's own question, asked a moment later.
///
/// The `source` field is the whole of this test's reason to exist. A local
/// download reaches `ready` when the reader clicked 下载更新 and asked for a
/// file, not for a restart — the card is in front of them with a button. A peer
/// blob reaches it because somebody *else* clicked 发送更新, and the reader here
/// asked for nothing: the receiving side answers an offer with a request of its
/// own (see `_on_update_offer`), so the archive arriving is the exchange working
/// as designed, and an update left staged is one the machine that can least
/// reach the release endpoint has to finish by hand.
///
/// `source` is absent on a sidecar older than it, and absent reads as "not a
/// peer's" — the same silence this frame got before the field existed.
fn peer_sent_update(value: &Value) -> bool {
    if value["type"] != "event" || value["name"] != "update.state" {
        return false;
    }
    let state = &value["data"]["state"];
    state["phase"] == "ready" && state["source"] == "p2p"
}

pub struct Bridge {
    input: AsyncMutex<ChildStdin>,
    child: AsyncMutex<Child>,
    pending: Mutex<HashMap<String, Reply>>,
    ready: watch::Sender<Option<Result<(), BridgeError>>>,
    session: Mutex<Option<String>>,
    stopping: std::sync::atomic::AtomicBool,
    /// The tail of what the sidecar wrote to stderr, for explaining a failure
    /// that carries no reason of its own. See `explain`.
    stderr: StderrTail,
    /// The task draining stderr, awaited before that tail is read so the last
    /// thing the sidecar said is not still sitting unread in the pipe.
    stderr_drain: Mutex<Option<tauri::async_runtime::JoinHandle<()>>>,
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
        // Read the program back before spawning it.
        let program = program_of(&mut command);
        let mut child = command
            .spawn()
            .map_err(|error| launch_failed(&program, &error))?;
        let input = child.stdin.take().ok_or_else(BridgeError::unavailable)?;
        let output = child.stdout.take().ok_or_else(BridgeError::unavailable)?;
        let stderr = child.stderr.take().ok_or_else(BridgeError::unavailable)?;
        let (ready, _) = watch::channel(None);
        let bridge = Arc::new(Self {
            input: AsyncMutex::new(input),
            child: AsyncMutex::new(child),
            pending: Mutex::new(HashMap::new()),
            ready,
            session: Mutex::new(None),
            stopping: std::sync::atomic::AtomicBool::new(false),
            stderr: StderrTail::default(),
            stderr_drain: Mutex::new(None),
        });
        let startup = bridge.clone();
        let startup_app = app.clone();
        tauri::async_runtime::spawn(async move {
            // Generous on purpose.  The sidecar is a PyInstaller onefile build,
            // so the clock here covers unpacking its Python runtime to a temp
            // directory, starting the interpreter, and only then bringing the
            // LAN runtime up -- measured at ~15s warm and ~20s cold on an M-series
            // laptop, against a 15s bound that the sidecar therefore lost by a
            // fraction of a second.  Losing it is not a slow start: this branch
            // terminates the sidecar, so the window reported SIDECAR_UNAVAILABLE
            // for a process that was in fact still coming up.  A timeout only
            // bounds a failure, so the cost of the headroom is paid by a sidecar
            // that is genuinely wedged.
            if tokio::time::timeout(Duration::from_secs(60), startup.wait_ready())
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
        // Keep the tail of stderr rather than discarding it.  When a sidecar
        // dies before it reports ready this is the only account of why that
        // exists anywhere: the app's own log view is an RPC to that same dead
        // process, so a failure of this shape used to reach the user as
        // "background process is unavailable" and nothing else.
        //
        // Read as bytes and decoded lossily: `read_line` rejects a line that
        // is not valid UTF-8, which would end the drain at the first such line
        // and lose everything after it -- including, on macOS, the `dyld`
        // failure this exists to catch.
        let stderr_bridge = Arc::downgrade(&bridge);
        let drain = tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(stderr);
            let mut buffer = Vec::new();
            loop {
                buffer.clear();
                match reader.read_until(b'\n', &mut buffer).await {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {}
                }
                let Some(bridge) = stderr_bridge.upgrade() else {
                    break;
                };
                bridge.stderr.note(&String::from_utf8_lossy(&buffer));
            }
        });
        *bridge.stderr_drain.lock().unwrap() = Some(drain);
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
            Ok(Self::declare_shell(command))
        }
        #[cfg(all(not(debug_assertions), target_os = "macos"))]
        {
            // A directory build, shipped as a bundle resource rather than as an
            // `externalBin`: that field takes a single executable and embeds it
            // beside this one, and what this sidecar needs is room for the
            // `_internal` tree it unpacked into at build time.  Shipping the
            // tree and starting it in place is what keeps macOS from having to
            // re-validate a fresh extraction on every launch, which is the
            // whole reason it is built this way on this platform.
            let exe = std::env::current_exe().map_err(|_| BridgeError::unavailable())?;
            let bundle = exe
                .parent()
                .and_then(|macos| macos.parent())
                .ok_or_else(BridgeError::unavailable)?;
            Ok(Self::declare_shell(Command::new(
                bundle
                    .join("Resources")
                    .join("sidecar")
                    .join("clipsync-sidecar"),
            )))
        }
        #[cfg(all(not(debug_assertions), not(target_os = "macos")))]
        {
            let exe = std::env::current_exe().map_err(|_| BridgeError::unavailable())?;
            let name = if cfg!(windows) {
                "clipsync-sidecar.exe"
            } else {
                "clipsync-sidecar"
            };
            Ok(Self::declare_shell(Command::new(exe.with_file_name(name))))
        }
    }

    /// Tell the sidecar which desktop shell is starting it.
    ///
    /// Both applications share one release and therefore publish two assets per
    /// platform, and the sidecar picks between them for its own download
    /// fallback with no other way to know which one it belongs to.  Left
    /// unsaid it assumes the legacy shell, whose asset is a different program
    /// entirely: the Tauri window would offer to update itself and hand the
    /// user the old application's installer.
    ///
    /// Every branch above goes through here rather than setting the variable
    /// where the child is built, because the one branch that forgot would be
    /// the one nobody runs until release.
    fn declare_shell(mut command: Command) -> Command {
        command.env("CLIPSYNC_SHELL", "tauri");
        command
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
                // No native notification is raised for any event: the window
                // says everything the event means, on its own status strip, and
                // this host is a background process whose reader opens it when
                // they go to pair or to look.  Ringing the desktop as well said
                // the same fact twice, the second time over whatever the reader
                // was actually doing.
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
                if value["name"] == "update.available" {
                    // Held back until this side has answered the one question
                    // the sidecar cannot: whether this build can replace itself.
                    //
                    // The window decides between "下载并安装" and a manual
                    // download from that answer, and the silent check originates
                    // in the sidecar, which knows nothing of the plugin.  Only a
                    // definite "no" rules the in-place install out over there, so
                    // a notice forwarded with no answer attached would offer an
                    // install this build cannot perform — which is why the answer
                    // is fetched first and travels with the frame.  The three
                    // answers are told apart in `main.rs::update_check`; a lookup
                    // that failed leaves the field off rather than claiming "no".
                    //
                    // Spawned rather than awaited: this runs on the stdout
                    // reader, and the manifest fetch must not stall the pipe.
                    let handle = app.clone();
                    let frame = value.clone();
                    tauri::async_runtime::spawn(async move {
                        let installable = match crate::updater(&handle) {
                            Ok(updater) => match updater.check().await {
                                Ok(Some(_)) => Some(true),
                                Ok(None) => Some(false),
                                Err(_) => None,
                            },
                            Err(_) => None,
                        };
                        let mut frame = frame;
                        if let (Some(data), Some(installable)) = (
                            frame.get_mut("data").and_then(|data| data.as_object_mut()),
                            installable,
                        ) {
                            data.insert("installable".into(), json!(installable));
                        }
                        let _ = handle.emit_to("main", "sidecar:event", frame);
                    });
                    return Ok(());
                }
                if peer_sent_update(&value) {
                    // The exchange's last step, and the one that was missing: a
                    // peer's archive is checked and staged by the sidecar, and
                    // then installed without a click.  Nobody here asked for
                    // anything, which is the point — the receiving side already
                    // answered the offer with a request of its own, and the
                    // bytes were held to the published release digest before
                    // they were staged, so there is nothing left to ask a
                    // reader that the file itself has not already answered.
                    //
                    // Spawned rather than awaited, for the reason the restart
                    // above gives: this runs on the stdout reader, and an
                    // install that stops the sidecar must not be started from
                    // inside the task that reads it.  A refusal is not fatal —
                    // the card still draws the ready archive with its own
                    // button, which is how the reader finishes by hand.
                    let bridge = self.clone();
                    let handle = app.clone();
                    tauri::async_runtime::spawn(async move {
                        let _ = crate::install_staged_update(&handle, &bridge).await;
                    });
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
        // Built through the error's own constructor so it is marked as the
        // sidecar's: that side worded the sentence in the user's language, and
        // several of the codes mean something else when this side raises them.
        BridgeError::from_sidecar(value)
    }

    fn fail(&self, error: BridgeError) {
        fail_pending(&self.pending, &self.ready, error);
    }

    async fn terminate(&self, app: &AppHandle, error: BridgeError) {
        // Let the stderr drain catch up before reading it.  The child is gone
        // by the time this runs, but what it wrote is still in the pipe and
        // the task copying it may not have been scheduled yet, so reading
        // without waiting is a race that loses exactly the line worth having.
        // Bounded, because a sidecar that left a grandchild holding the pipe
        // must not stall the failure the user is waiting on.
        let drain = self.stderr_drain.lock().unwrap().take();
        if let Some(drain) = drain {
            let _ = tokio::time::timeout(Duration::from_secs(2), drain).await;
        }
        let error = explain(error, self.stderr.last());
        self.fail(error.clone());
        // The message and the retryable flag travel with the code. A refused
        // data directory is the case that needs them: the sidecar knows which
        // application holds the directory and says so, and asking the user to
        // retry is wrong when retrying is exactly what cannot work.
        //
        // The message is the localized one, through the same call `Serialize`
        // makes: this event is the other route to the failure band, and a
        // sentence worded on one route but not the other is how half of them
        // arrived in English.  Read before `code` moves into the frame below.
        let message = error.localized_message();
        let _ = app.emit_to(
            "main",
            "sidecar:state",
            json!({
                "state": "failed",
                "error": error.code,
                "message": message,
                "retryable": error.retryable,
            }),
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
    let program = program_of(&mut command);
    let mut child = command
        .spawn()
        .map_err(|error| launch_failed(&program, &error))?;
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

    #[test]
    fn a_dying_sidecar_explains_itself_with_its_last_line() {
        // The reported shape: a packaged macOS build spawned the sidecar, the
        // process died before its ready frame, and the user got a band saying
        // "background process is unavailable" with no way to find out why.
        //
        // What is kept is the line itself.  The sentence around it is `i18n`'s
        // now, so the two are no longer glued together here in one language.
        let _held = crate::i18n::tests::locale();
        let error = explain(
            BridgeError::unavailable(),
            Some("dyld: Library not loaded: @rpath/Python".into()),
        );
        assert_eq!(error.code, "SIDECAR_UNAVAILABLE");
        assert_eq!(error.message, "dyld: Library not loaded: @rpath/Python");
        assert_eq!(
            error.localized_message(),
            "ClipSync 后台进程不可用。原因：dyld: Library not loaded: @rpath/Python"
        );
    }

    #[test]
    fn a_failure_that_has_its_own_reason_is_left_alone() {
        // The sidecar naming the application holding its data directory is the
        // case: asking the user to retry is wrong there, and unrelated stderr
        // noise appended to the sentence would only muddy it.
        let error = explain(
            BridgeError::from_sidecar(&json!({
                "code": "DATA_IN_USE",
                "message": "Another ClipSync holds this directory",
                "retryable": true,
            })),
            Some("some unrelated warning".into()),
        );
        assert_eq!(error.message, "Another ClipSync holds this directory");
        // The flag travels with the message, so annotating must not reset it.
        assert!(error.retryable);
    }

    #[test]
    fn a_silent_sidecar_still_reports_its_own_message() {
        // Nothing to add is not an error, and must not leave a trailing colon
        // -- nor, now that the sentence is a template, a label over an empty
        // slot saying "Reason:" with nothing after it.
        let _held = crate::i18n::tests::locale();
        let error = explain(BridgeError::unavailable(), None);
        assert_eq!(error.message, "");
        assert_eq!(error.localized_message(), "ClipSync 后台进程不可用。");
    }

    #[test]
    fn the_kept_tail_is_bounded_and_ignores_blank_lines() {
        let tail = StderrTail::default();
        for index in 0..(STDERR_LINES + 10) {
            tail.note(&format!("line {index}"));
            tail.note("   ");
        }
        // Oldest lines fall off, so what survives is the *end* of the output --
        // which is where a crash says what happened.
        assert_eq!(tail.last().unwrap(), format!("line {}", STDERR_LINES + 9));
        assert_eq!(tail.0.lock().unwrap().len(), STDERR_LINES);
        assert!(tail.0.lock().unwrap().iter().all(|line| !line.trim().is_empty()));
    }

    #[test]
    fn one_enormous_stderr_line_cannot_fill_the_banner() {
        let tail = StderrTail::default();
        tail.note(&"x".repeat(STDERR_LINE_CHARS * 4));
        assert_eq!(tail.last().unwrap().chars().count(), STDERR_LINE_CHARS);
    }

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
