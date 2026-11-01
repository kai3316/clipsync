//! The native tray menu.
//!
//! The menu is built once at startup and then *updated in place*: labels, the
//! sync checkbox and the pause submenu's children change, the menu's shape does
//! not.  The legacy desktop rebuilt its whole menu on every state change — and
//! on Windows posted a window message to do it on the tray thread, because
//! destroying the open `HMENU` races `TrackPopupMenuEx`.  Mutating the items the
//! menu already holds avoids that class of problem, so this tray has no
//! rebuild path at all.
//!
//! State comes from the sidecar (`settings.get`, plus `devices.list` for the
//! peers submenu), never from the renderer: the window may be closed, and the
//! phone or the tray itself can be the one that changed something.  [`refresh`]
//! is what a relevant event triggers; [`tick`] re-renders only the pause
//! countdown, which is the one label that changes with no event behind it.

use crate::i18n;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::menu::{
    CheckMenuItem, CheckMenuItemBuilder, Menu, MenuBuilder, MenuItem, MenuItemBuilder, Submenu,
    SubmenuBuilder,
};
use tauri::{AppHandle, Manager, Wry};

/// Menu item ids, shared with the click handler in `main.rs`.
pub const ID_SHOW: &str = "show";
/// Opens the quick-paste overlay without bringing the main window back.
pub const ID_QUICK_PASTE: &str = "quick-paste";
pub const ID_QR: &str = "qr";
pub const ID_ABOUT: &str = "about";
pub const ID_QUIT: &str = "quit";
pub const ID_SEND_URL: &str = "send-url";
pub const ID_SETTINGS: &str = "settings";
pub const ID_EXPORT_LOGS: &str = "export-logs";
pub const ID_CHECK_UPDATE: &str = "check-update";
pub const ID_SYNC_TOGGLE: &str = "sync-toggle";
/// Every recent-clip row starts with this, followed by the entry id: one
/// submenu whose rows are made on demand, so the click handler recognises the
/// family rather than a fixed set of names.
pub const ID_RECENT_PREFIX: &str = "recent:";
pub const ID_PAUSE_15: &str = "pause-15";
pub const ID_PAUSE_30: &str = "pause-30";
pub const ID_PAUSE_60: &str = "pause-60";
pub const ID_RESUME: &str = "resume";
/// The disabled header, device and pause-countdown labels, and the presets
/// submenu.  They carry ids like every other item (a nameless item still gets
/// one) but no click can reach them, so only this module names them.
const ID_HEADER: &str = "header";
const ID_DEVICE: &str = "device";
const ID_PAUSE: &str = "pause";
const ID_PAUSE_STATUS: &str = "pause-status";
const ID_DEVICES: &str = "devices";
const ID_RECENT: &str = "recent";

/// How many recent clips the menu lists.  Five is what fits under one's
/// thumb in a menu that also has to hold the sync switch and the peers.
const RECENT_ROWS: usize = 5;

/// How much of a clip a row shows.  A menu row is one line; the clip is not
/// shortened anywhere else, and this one is a label, not a payload.
const RECENT_CHARS: usize = 36;

/// How often the pause countdown is re-rendered.  The dashboard ticked its own
/// every 15 s, and the label is whole minutes, so nothing staler than this can
/// be shown for longer than a minute.
const PAUSE_TICK: std::time::Duration = std::time::Duration::from_secs(15);

/// The app's own name: the same words in every language, as legacy's
/// `ui.app_name` was.
const APP_NAME: &str = "ClipSync";

/// Everything the tray menu says about the running app.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TrayState {
    /// This device's name, for the disabled header.
    pub device_name: String,
    pub syncing: bool,
    /// Whether the web companion is running; the QR entry is useless without it.
    pub web_enabled: bool,
    /// Wall-clock epoch of an armed timed pause, 0 when none is armed.
    pub pause_until: f64,
    /// Every peer worth listing, most alive first.
    pub devices: Vec<DeviceLine>,
    /// The newest clips, newest first: what the tray can put back on the
    /// clipboard without opening the window.
    pub recent: Vec<RecentLine>,
}

/// One row of the recent-clips submenu: the entry to copy, and the text the
/// row shows for it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecentLine {
    /// The history entry id, which is the whole of what a click needs: copy
    /// goes through `history.copy`, so the payload never travels to the menu.
    pub id: String,
    /// The clip's own preview, untruncated and unformatted.
    pub preview: String,
}

/// One row of the peers submenu: a name and what it is doing.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeviceLine {
    pub name: String,
    pub state: DeviceState,
}

/// What a peer is doing, in the four words the devices page uses.
///
/// The legacy tray said the same five things, but wrote them straight into the
/// label as hard-coded English (`connected` / `offline` / `pairing…` /
/// `found`) — so a Chinese tray showed "手机  (connected)".  These carry the
/// state instead, and the words come from the catalog.
///
/// The claim in the first line is what the row test below pins: `Connected`
/// draws 在线 / "Online", which is the page's `connectionLabel` word for a peer
/// that is up.  It used to draw 已连接 / "Connected", and that is not a peer's
/// word anywhere else in this application — 已连接 is what the window's header
/// says about *this* machine's link to its engine, so one word named two
/// facts, one of them about the other machine.  The words for the other three
/// states were already the page's (`离线` / `等待确认` / `已发现`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceState {
    Connected,
    Offline,
    Pairing,
    Found,
    /// A dial in flight, or a peer being redialled: the window's 连接中.  The
    /// tray had no word for it, so a device whose retry was on its third
    /// attempt read 离线/已发现 here while the devices page said 连接中 and
    /// counted the attempt in the chip's title — two surfaces disagreeing
    /// about whether the app was still trying.
    Connecting,
}

impl DeviceState {
    fn label(self, s: &i18n::Tray) -> &'static str {
        match self {
            Self::Connected => s.state_connected,
            Self::Offline => s.state_offline,
            Self::Pairing => s.state_pairing,
            Self::Found => s.state_found,
            Self::Connecting => s.state_connecting,
        }
    }

    /// Listing order: what is up now, then what is on its way, then the rest.
    /// The legacy tray grouped its sources the same way.
    ///
    /// A dial sits with the machines this one knows rather than with the ones
    /// merely seen: the page's own ordering (`deviceRank`) puts a dialling
    /// device in the group its pairing puts it in, and the tray's groups are
    /// the states, so the nearest is the second.
    fn rank(self) -> u8 {
        match self {
            Self::Connected => 0,
            Self::Pairing => 1,
            Self::Offline => 2,
            Self::Connecting => 2,
            Self::Found => 3,
        }
    }
}

/// Classify a `devices.list` row the way the devices page does.
///
/// The same predicate set as the window's `pairingPending` and `pairingLabel`,
/// so the two native surfaces cannot describe one device in two different ways.
/// A device can hold two pairings at once — a pinned local one and a code
/// pairing that crosses the internet — and one word has to answer for both: the
/// local link is what the row reports while it is up, and the relay's own view
/// of the peer answers when it is not.  A device paired only by code is paired,
/// which is what the page's `pairingLabel` says about the same row; calling it
/// 已发现 here was the tray disagreeing with the page.
fn classify(device: &Value) -> DeviceState {
    let relay_paired = device["relay_paired"] == true;
    let paired = device["paired"] == true || relay_paired;
    let online = device["connection_state"] == "online"
        || (relay_paired && device["relay_online"] == true);
    let status = device["pairing_status"].as_str().unwrap_or_default();
    // A dial in flight is the page's 连接中 (`device-row.ts::deviceStatus`),
    // asked about before the settled states for the same reason it is there:
    // a machine being called is not one nothing is happening to, and 离线 says
    // the opposite of what is going on.  A code waiting for an answer still
    // wins — that is the one state the user has to do something about.
    if !online && !is_pairing(status)
        && (device["reconnecting"] == true || device["connection_state"] == "connecting")
    {
        return DeviceState::Connecting;
    }
    if paired {
        return if online {
            DeviceState::Connected
        } else {
            DeviceState::Offline
        };
    }
    if is_pairing(status) {
        return DeviceState::Pairing;
    }
    // Talking to us without being trusted yet — a consented chat session —
    // reads the same as a paired one, as it did in the legacy tray.
    if online {
        return DeviceState::Connected;
    }
    DeviceState::Found
}

/// The pairing states still in flight: the three the window's `pairingPending`
/// accepts, and the ones the legacy tray's pending list held.
fn is_pairing(status: &str) -> bool {
    matches!(status, "pending" | "peer_confirmed" | "confirmed_waiting")
}

/// What this machine calls the device, in one line.
///
/// The rule the window states once, in
/// `desktop/src/lib/device-row.ts::deviceLabel` — the name the user gave it
/// (`note`) outranks the one the peer published about itself (`name`) — spelled
/// again here because the tray is a native menu and cannot import the TypeScript
/// module it lives in.  Two spellings of one answer is a real cost, and it is
/// paid because the alternative was worse: this menu read `name` directly, so a
/// device the user had renamed on its own row was called one thing there and
/// another here, and the peers sorted under a name the list no longer showed.
///
/// A note of nothing but spaces is not a name, so it falls through.
fn display_name(device: &Value) -> String {
    let note = device["note"].as_str().unwrap_or_default().trim();
    if !note.is_empty() {
        return note.to_owned();
    }
    device["name"].as_str().unwrap_or_default().to_owned()
}

/// How two peer rows are ordered inside one state group.
///
/// The page orders the same rows by the name it shows, with
/// `localeCompare(..., { numeric: true, sensitivity: "base" })`
/// (`App.vue::activeDevices`): case-insensitive, and `Device 2` before
/// `Device 10`.  This used to be `a.name.cmp(&b.name)`, which compares UTF-8
/// bytes, so an upper-case name sorted before a lower-case one and a numbered
/// one sorted lexically — the two lists put the same peers in a different order.
///
/// What this reproduces: case folding (each character's lowercase form) and
/// numeric runs compared by value.  What it cannot: the locale's own collation
/// for Han characters (pinyin order), which the page gets from the WebView and
/// the standard library does not provide.  A CJK name therefore still sorts by
/// code point here; that residual difference is recorded in
/// `audit/fix/desktop-DONE.md` rather than hidden.
fn compare_names(left: &str, right: &str) -> std::cmp::Ordering {
    use std::cmp::Ordering;

    let left: Vec<char> = left.to_lowercase().chars().collect();
    let right: Vec<char> = right.to_lowercase().chars().collect();
    let (mut i, mut j) = (0, 0);
    while i < left.len() && j < right.len() {
        let (a, b) = (left[i], right[j]);
        if a.is_ascii_digit() && b.is_ascii_digit() {
            // The whole run, as a number: leading zeros mean nothing, a longer
            // run is a larger number, and equal lengths compare digit by digit.
            let a_start = i;
            let b_start = j;
            while i < left.len() && left[i].is_ascii_digit() {
                i += 1;
            }
            while j < right.len() && right[j].is_ascii_digit() {
                j += 1;
            }
            let a_run: String = left[a_start..i].iter().collect();
            let b_run: String = right[b_start..j].iter().collect();
            let a_trim = a_run.trim_start_matches('0');
            let b_trim = b_run.trim_start_matches('0');
            let order = a_trim
                .len()
                .cmp(&b_trim.len())
                .then_with(|| a_trim.cmp(b_trim));
            if order != Ordering::Equal {
                return order;
            }
            continue;
        }
        let order = a.cmp(&b);
        if order != Ordering::Equal {
            return order;
        }
        i += 1;
        j += 1;
    }
    left.len().cmp(&right.len())
}

/// The peer rows in a `devices.list` response (`{"items": [...]}`).
///
/// Removed devices are left out: they carry no live state, the legacy tray had
/// no such rows at all, and the devices page is where they can be restored.
fn device_lines(response: &Value) -> Vec<DeviceLine> {
    let mut lines: Vec<DeviceLine> = response["items"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|device| device["archived"] != true)
        .map(|device| DeviceLine {
            name: display_name(device),
            state: classify(device),
        })
        .collect();
    lines.sort_by(|a, b| {
        a.state
            .rank()
            .cmp(&b.state.rank())
            .then_with(|| compare_names(&a.name, &b.name))
            // A stable, total last word for two names that compare equal
            // (`Alice` and `alice`): the order must not depend on the input
            // order the sidecar happened to hand over.
            .then_with(|| a.name.cmp(&b.name))
    });
    lines
}

/// The newest clips in a `history.list` response (`{"items": [...]}`).
///
/// Rows with no id are dropped: the id is the whole of what a click resolves to,
/// and a row that cannot be copied should not be offered.  The list arrives
/// newest-first (the sidecar's own default sort), so nothing is re-sorted here —
/// a second opinion about "recent" is a second way for the two surfaces to
/// disagree.
fn recent_lines(response: &Value) -> Vec<RecentLine> {
    response["items"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|row| {
            let id = row["id"].as_str().unwrap_or_default();
            if id.is_empty() {
                return None;
            }
            Some(RecentLine {
                id: id.to_owned(),
                preview: row["preview"].as_str().unwrap_or_default().to_owned(),
            })
        })
        .take(RECENT_ROWS)
        .collect()
}

/// One recent row: one line, at most [`RECENT_CHARS`] characters.
///
/// The clipboard's contents are being put into a system menu, which is why this
/// is a preview and never the payload.  Whitespace runs collapse to one space —
/// a copied paragraph would otherwise turn five rows into twenty and push the
/// sync switch off the screen.  A clip with no text at all (an image, a file)
/// says so rather than drawing an empty row that reads as a rendering fault.
fn recent_label(preview: &str, s: &i18n::Tray) -> String {
    let collapsed = preview.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.is_empty() {
        return s.recent_untitled.to_owned();
    }
    if collapsed.chars().count() <= RECENT_CHARS {
        return collapsed;
    }
    let cut: String = collapsed.chars().take(RECENT_CHARS).collect();
    format!("{cut}…")
}

/// One peers row: the name and its state, in this locale's own punctuation.
///
/// The state goes in first and the name last: [`fill`] replaces every
/// occurrence, so a device called `{state}` — or anything else with a brace in
/// it — would otherwise be substituted back into the string it was just read
/// out of.  Inserting the untrusted part last leaves it as text.
fn row_label(device: &DeviceLine, s: &i18n::Tray) -> String {
    fill(
        &fill(s.device_entry, "{state}", device.state.label(s)),
        "{name}",
        &device.name,
    )
}

/// Whole minutes left on an armed deadline, or `None` when none is armed.
///
/// Rounded up, so a pause with seconds left still reads as a minute rather than
/// as nothing — and a deadline that has already passed is *not* armed, which is
/// where this and the legacy tray part ways: it kept showing the paused line
/// (with `max(1, 0)` minutes) until something else cleared the deadline.  The
/// window's footer treats a passed deadline as over, and the two native
/// surfaces say the same thing.
fn pause_left_minutes(pause_until: f64, now: f64) -> Option<u32> {
    if pause_until <= now {
        return None;
    }
    Some(((pause_until - now) / 60.0).ceil() as u32)
}

/// Fill a one-placeholder template.  A device name is untrusted text: it is
/// inserted literally, never parsed or interpreted.
fn fill(template: &str, placeholder: &str, value: &str) -> String {
    template.replace(placeholder, value)
}

/// Where the pause section sits in the menu.  The section is one entry — either
/// the presets submenu or the countdown line plus 立即恢复 — so it keeps the
/// same slot either way.
const PAUSE_SLOT: usize = 4;

/// What the pause section currently holds.
///
/// The structure has to be tracked, not just the text: the two states are
/// different entries, and swapping them means inserting and removing top-level
/// items, which must happen exactly once per transition.
#[derive(Clone, PartialEq, Eq)]
struct PauseRendered {
    /// The countdown line's last text; empty while the presets are shown.
    title: String,
    /// Whether the section holds the presets submenu.
    presets: bool,
}

impl Default for PauseRendered {
    /// What [`TrayMenu::build`] leaves in the menu: the presets submenu.
    fn default() -> Self {
        Self {
            title: String::new(),
            presets: true,
        }
    }
}

/// What the peers submenu currently shows, and the rows it holds.
///
/// The rows are rebuilt rather than relabelled: the device list has no bound,
/// so unlike the pause presets there is no fixed pool of items to rewrite.  The
/// handles are kept because removing a child needs one — dropping a
/// [`MenuItem`] does not take it out of its submenu.
/// What the recent-clips submenu currently shows.
///
/// Same shape as [`DevicesRendered`] and for the same reason — the list is
/// unbounded, so the rows are rebuilt rather than relabelled — plus the ids,
/// because two clips can carry the same preview while being different rows.
#[derive(Default)]
struct RecentRendered {
    rows: Vec<String>,
    ids: Vec<String>,
    items: Vec<MenuItem<Wry>>,
}

#[derive(Default)]
struct DevicesRendered {
    /// The submenu's own title, which carries the connected count.
    title: String,
    /// The rows it holds, in order.
    rows: Vec<String>,
    items: Vec<MenuItem<Wry>>,
}

/// The tray menu's items, kept so a state change can relabel them.
pub struct TrayMenu {
    /// The app handle, for building the peers rows: the device list is
    /// unbounded, so its items are made on demand rather than up front.
    app: AppHandle<Wry>,
    /// The assembled menu.  Held rather than only handed to the tray icon: the
    /// pause section's two states are different entries, so a transition
    /// inserts and removes top-level items.
    menu: Menu<Wry>,
    header: MenuItem<Wry>,
    device: MenuItem<Wry>,
    sync: CheckMenuItem<Wry>,
    /// The presets submenu, its three children, and the two entries a paused
    /// state swaps it for.
    pause: Submenu<Wry>,
    pause_presets: [MenuItem<Wry>; 3],
    pause_status: MenuItem<Wry>,
    resume: MenuItem<Wry>,
    show: MenuItem<Wry>,
    send_url: MenuItem<Wry>,
    web_qr: MenuItem<Wry>,
    /// The recent-clips submenu.  Always in the menu, like the peers one;
    /// only its children change.
    recent: Submenu<Wry>,
    /// The peers submenu.  Always in the menu; only its children change.
    devices: Submenu<Wry>,
    settings: MenuItem<Wry>,
    export_logs: MenuItem<Wry>,
    check_update: MenuItem<Wry>,
    about: MenuItem<Wry>,
    quit: MenuItem<Wry>,
    /// The last state rendered, so a refresh that changes nothing visible
    /// touches no menu item. `None` until the first one arrives: the menu is
    /// built before the sidecar can answer, and the default state is a real
    /// state — comparing against it would let a genuine one be skipped.
    state: Mutex<Option<TrayState>>,
    /// The pause section last rendered.  The tick renders unconditionally; only
    /// a real change is written.
    pause_rendered: Mutex<PauseRendered>,
    devices_rendered: Mutex<DevicesRendered>,
    recent_rendered: Mutex<RecentRendered>,
}

impl TrayMenu {
    pub fn build(app: &AppHandle<Wry>, locale: &str) -> tauri::Result<Self> {
        let s = &i18n::strings(locale).tray;
        let header = MenuItemBuilder::with_id(ID_HEADER, APP_NAME)
            .enabled(false)
            .build(app)?;
        // The name arrives with the first refresh, a moment from now; until
        // then the line is an ellipsis rather than a raw `{name}` template.
        let device = MenuItemBuilder::with_id(ID_DEVICE, fill(s.device, "{name}", "…"))
            .enabled(false)
            .build(app)?;
        let sync = CheckMenuItemBuilder::with_id(ID_SYNC_TOGGLE, s.sync).build(app)?;
        let pause_presets = [
            MenuItemBuilder::with_id(ID_PAUSE_15, s.pause_15m).build(app)?,
            MenuItemBuilder::with_id(ID_PAUSE_30, s.pause_30m).build(app)?,
            MenuItemBuilder::with_id(ID_PAUSE_60, s.pause_1h).build(app)?,
        ];
        let resume = MenuItemBuilder::with_id(ID_RESUME, s.resume_now).build(app)?;
        // The countdown is a disabled line of its own, as the legacy tray had
        // it — not a submenu whose title you have to open to read.  It is built
        // here and inserted only while a pause is armed.
        let pause_status = MenuItemBuilder::with_id(ID_PAUSE_STATUS, s.pause_for)
            .enabled(false)
            .build(app)?;
        let pause = SubmenuBuilder::with_id(app, ID_PAUSE, s.pause_for)
            .items(&[
                &pause_presets[0],
                &pause_presets[1],
                &pause_presets[2],
            ])
            .build()?;
        let show = MenuItemBuilder::with_id(ID_SHOW, s.show).build(app)?;
        let quick_paste = MenuItemBuilder::with_id(ID_QUICK_PASTE, s.quick_paste).build(app)?;
        let send_url = MenuItemBuilder::with_id(ID_SEND_URL, s.send_url).build(app)?;
        let web_qr = MenuItemBuilder::with_id(ID_QR, s.show_web_qr).build(app)?;
        // Built empty and filled by the first refresh; an empty submenu draws
        // as a bare title, which is what it is for the moment before the
        // sidecar answers.
        let recent = SubmenuBuilder::with_id(app, ID_RECENT, s.recent).build()?;
        let devices = SubmenuBuilder::with_id(app, ID_DEVICES, s.devices).build()?;
        let settings = MenuItemBuilder::with_id(ID_SETTINGS, s.settings).build(app)?;
        let export_logs = MenuItemBuilder::with_id(ID_EXPORT_LOGS, s.export_logs).build(app)?;
        let check_update = MenuItemBuilder::with_id(ID_CHECK_UPDATE, s.check_update).build(app)?;
        let about = MenuItemBuilder::with_id(ID_ABOUT, s.about).build(app)?;
        let quit = MenuItemBuilder::with_id(ID_QUIT, s.quit).build(app)?;
        let menu = MenuBuilder::new(app)
            .item(&header)
            .item(&device)
            .separator()
            .item(&sync)
            .item(&pause)
            .separator()
            .item(&show)
            .item(&quick_paste)
            .item(&send_url)
            .item(&web_qr)
            .separator()
            .item(&recent)
            .item(&devices)
            .separator()
            .item(&settings)
            .item(&export_logs)
            .separator()
            .item(&check_update)
            .item(&about)
            .item(&quit)
            .build()?;
        Ok(Self {
            app: app.clone(),
            menu,
            header,
            device,
            sync,
            pause,
            pause_presets,
            pause_status,
            resume,
            show,
            send_url,
            web_qr,
            recent,
            devices,
            settings,
            export_logs,
            check_update,
            about,
            quit,
            state: Mutex::new(None),
            pause_rendered: Mutex::new(PauseRendered::default()),
            devices_rendered: Mutex::new(DevicesRendered::default()),
            recent_rendered: Mutex::new(RecentRendered::default()),
        })
    }

    /// The assembled menu.  Built once; nothing here ever rebuilds it.
    pub fn menu(&self) -> &Menu<Wry> {
        &self.menu
    }

    /// The checkbox's own state, for the click handler: the OS has already
    /// flipped it by the time the event arrives.
    pub fn sync_checked(&self) -> bool {
        self.sync.is_checked().unwrap_or(false)
    }

    /// Render a new state.  A state that renders the same as the last one costs
    /// nothing: the labels and children are left as they are.
    pub fn set_state(&self, state: TrayState, s: &i18n::Tray, now: f64) {
        let mut last = match self.state.lock() {
            Ok(last) => last,
            Err(_) => return,
        };
        if last.as_ref() == Some(&state) {
            return;
        }
        *last = Some(state.clone());
        drop(last);
        self.render(&state, s, now);
    }

    /// Re-render just the pause submenu.  The countdown is the only label that
    /// changes with no event behind it, so this is what the tick calls.
    pub fn tick(&self, s: &i18n::Tray, now: f64) {
        let state = match self.state.lock() {
            Ok(state) => state.clone(),
            Err(_) => return,
        };
        // Nothing has been rendered yet, so there is no countdown to age.
        let Some(state) = state else {
            return;
        };
        self.render_pause(&state, s, now);
    }

    /// Write every label from `state`.  Idempotent, and safe to call purely to
    /// change language — which is how a locale change relabels the dynamic
    /// entries too.
    pub fn render(&self, state: &TrayState, s: &i18n::Tray, now: f64) {
        let _ = self.header.set_text(APP_NAME);
        let _ = self
            .device
            .set_text(fill(s.device, "{name}", &state.device_name));
        let _ = self.sync.set_text(s.sync);
        let _ = self.sync.set_checked(state.syncing);
        // The pause section's own words.  Which of its two entries is in the
        // menu is [`render_pause`]'s, but the words inside both are these.
        let _ = self.pause.set_text(s.pause_for);
        for (preset, label) in self
            .pause_presets
            .iter()
            .zip([s.pause_15m, s.pause_30m, s.pause_1h])
        {
            let _ = preset.set_text(label);
        }
        let _ = self.resume.set_text(s.resume_now);
        let _ = self.show.set_text(s.show);
        let _ = self.send_url.set_text(s.send_url);
        let _ = self.web_qr.set_text(s.show_web_qr);
        // Greyed rather than hidden, because the menu API has no per-item
        // visibility and because an entry that cannot work should read as one
        // rather than come and go under the user's cursor.
        let _ = self.web_qr.set_enabled(state.web_enabled);
        let _ = self.settings.set_text(s.settings);
        let _ = self.export_logs.set_text(s.export_logs);
        let _ = self.check_update.set_text(s.check_update);
        let _ = self.about.set_text(s.about);
        let _ = self.quit.set_text(s.quit);
        self.render_pause(state, s, now);
        self.render_devices(state, s);
        self.render_recent(state, s);
    }

    /// The peers submenu: how many devices are connected, and one row per known
    /// device saying what it is doing.
    ///
    /// The count is what the label claims.  The legacy tray put the number of
    /// *known* devices behind the word 已连接设备, so a machine with three
    /// discovered-but-offline peers read as "已连接设备 (3)" — the one place
    /// this deliberately does not match it.  The rows still list every known
    /// device, because "why does it say none connected" is answered by the
    /// offline rows underneath it.
    fn render_devices(&self, state: &TrayState, s: &i18n::Tray) {
        let connected = state
            .devices
            .iter()
            .filter(|device| device.state == DeviceState::Connected)
            .count();
        let title = if connected == 0 {
            s.devices.to_owned()
        } else {
            fill(s.devices_counted, "{count}", &connected.to_string())
        };
        // An empty submenu draws as a title with nothing under it, so it always
        // holds at least the one line saying there is nothing to list.
        let rows: Vec<String> = if state.devices.is_empty() {
            vec![s.no_devices.to_owned()]
        } else {
            state.devices.iter().map(|device| row_label(device, s)).collect()
        };
        let mut rendered = match self.devices_rendered.lock() {
            Ok(rendered) => rendered,
            Err(_) => return,
        };
        if rendered.title == title && rendered.rows == rows {
            return;
        }
        rendered.title = title.clone();
        rendered.rows = rows.clone();
        // The old handles are taken out under the lock, but the items that
        // replace them are built and swapped in without it: building a menu
        // item is not free, and nothing else may be kept waiting on it.
        let old = std::mem::take(&mut rendered.items);
        drop(rendered);
        let items: Vec<MenuItem<Wry>> = rows
            .iter()
            .filter_map(|label| {
                // Informational, as the legacy tray had them: no click reaches
                // a peer row, so there is nothing to wire up.
                MenuItemBuilder::new(label)
                    .enabled(false)
                    .build(&self.app)
                    .ok()
            })
            .collect();
        let _ = self.devices.set_text(title);
        for item in &old {
            let _ = self.devices.remove(item);
        }
        for item in &items {
            let _ = self.devices.append(item);
        }
        if let Ok(mut rendered) = self.devices_rendered.lock() {
            rendered.items = items;
        }
    }

    /// The recent-clips submenu: the newest few rows, each one a click away
    /// from the clipboard.
    ///
    /// The rows are the only clickable items this module builds on demand, so
    /// their ids carry the entry (`recent:<id>`) and the handler in `main.rs`
    /// recognises the prefix rather than a fixed list.
    fn render_recent(&self, state: &TrayState, s: &i18n::Tray) {
        // An empty submenu draws as a bare title, which reads as a menu that
        // failed to load; the same reason the peers submenu always holds one
        // line.
        let rows: Vec<String> = if state.recent.is_empty() {
            vec![s.no_recent.to_owned()]
        } else {
            state.recent.iter().map(|line| recent_label(&line.preview, s)).collect()
        };
        let ids: Vec<String> = if state.recent.is_empty() {
            vec![String::new()]
        } else {
            state.recent.iter().map(|line| line.id.clone()).collect()
        };
        let mut rendered = match self.recent_rendered.lock() {
            Ok(rendered) => rendered,
            Err(_) => return,
        };
        if rendered.rows == rows && rendered.ids == ids {
            return;
        }
        rendered.rows = rows.clone();
        rendered.ids = ids.clone();
        let old = std::mem::take(&mut rendered.items);
        drop(rendered);
        let items: Vec<MenuItem<Wry>> = rows
            .iter()
            .zip(ids.iter())
            .filter_map(|(label, id)| {
                if id.is_empty() {
                    // The "nothing copied yet" line is a statement, not an
                    // action — a click on it can only do nothing.
                    MenuItemBuilder::with_id(ID_RECENT, label)
                        .enabled(false)
                        .build(&self.app)
                        .ok()
                } else {
                    MenuItemBuilder::with_id(format!("{ID_RECENT_PREFIX}{id}"), label)
                        .build(&self.app)
                        .ok()
                }
            })
            .collect();
        for item in &old {
            let _ = self.recent.remove(item);
        }
        for item in &items {
            let _ = self.recent.append(item);
        }
        if let Ok(mut rendered) = self.recent_rendered.lock() {
            rendered.items = items;
        }
    }

    /// The pause section: the presets submenu while nothing is armed, and a
    /// countdown line plus 立即恢复 while a deadline is — the same two states the
    /// legacy tray had, and the same reason: the minutes left are the whole
    /// point of the feature, so they are on the face of the menu rather than
    /// behind a submenu.
    fn render_pause(&self, state: &TrayState, s: &i18n::Tray, now: f64) {
        let left = pause_left_minutes(state.pause_until, now);
        let next = PauseRendered {
            title: match left {
                Some(minutes) => fill(s.paused_left, "{minutes}", &minutes.to_string()),
                // Nothing dynamic is on screen while the presets are.
                None => String::new(),
            },
            presets: left.is_none(),
        };
        let mut rendered = match self.pause_rendered.lock() {
            Ok(rendered) => rendered,
            Err(_) => return,
        };
        if *rendered == next {
            return;
        }
        let structural = rendered.presets != next.presets;
        *rendered = next.clone();
        drop(rendered);
        if structural {
            // Exactly one of the two shapes is in the menu, so removing the
            // absent one fails harmlessly — which is also what makes the first
            // render safe whatever state arrives.
            if next.presets {
                let _ = self.menu.remove(&self.pause_status);
                let _ = self.menu.remove(&self.resume);
                let _ = self.menu.insert(&self.pause, PAUSE_SLOT);
            } else {
                let _ = self.menu.remove(&self.pause);
                let _ = self.menu.insert(&self.pause_status, PAUSE_SLOT);
                let _ = self.menu.insert(&self.resume, PAUSE_SLOT + 1);
            }
        }
        if !next.presets {
            let _ = self.pause_status.set_text(next.title);
        }
    }
}

/// One refresh at a time.  The sidecar's events arrive in bursts (a pairing
/// exchange emits several), and each refresh is a round trip, so a burst
/// collapses into one refresh plus at most one trailing one.
#[derive(Default)]
pub struct RefreshGate {
    running: AtomicBool,
    again: AtomicBool,
}

/// Apply a locale to the native surfaces: the tray, plus the cache the native
/// file dialogs read (they cannot await a settings read).
pub fn apply(app: &AppHandle, locale: &str) {
    let changed = app
        .try_state::<i18n::CurrentLocale>()
        .map(|current| current.set(locale))
        .unwrap_or(false);
    if !changed {
        return;
    }
    // Re-render rather than relabel: the device line and the pause countdown
    // carry values, so they are rebuilt from the stored state in the new
    // language instead of being redrawn item by item here.  Before the first
    // state arrives there is nothing to rebuild — the refresh that is about to
    // deliver one renders in the language just set.
    if let (Some(menu), Some(current)) = (
        app.try_state::<TrayMenu>(),
        app.try_state::<i18n::CurrentLocale>(),
    ) {
        if let Some(state) = menu.state.lock().ok().and_then(|state| state.clone()) {
            // The countdown line is in the previous language, so forget its
            // text — keeping which shape of the section is in the menu, since
            // this is a relabel and not a transition.
            menu.pause_rendered
                .lock()
                .map(|mut rendered| rendered.title.clear())
                .ok();
            menu.render(&state, current.tray(), now_seconds());
        }
    }
}

fn now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0)
}

/// Read the saved settings and render them onto the menu.
///
/// The host owns the native surface's language and reads its state from the
/// sidecar rather than the renderer, so a change made from the web UI — or by
/// the phone — is picked up too.
pub async fn refresh(app: &AppHandle) {
    // `try_state`: a settings event can only arrive once the sidecar is up,
    // but the host is managed a moment after the bridge is spawned.
    let Some(gate) = app.try_state::<RefreshGate>() else {
        return;
    };
    loop {
        // Take the gate, or record that the holder owes us one more pass and
        // leave — the holder consumes that, so the last event of a burst is not
        // dropped.  The gate is released at the *end* of a pass and this is the
        // top, so a request that lands in the gap between the two either opens
        // its own pass or is picked up by the swap below.
        if gate.running.swap(true, Ordering::SeqCst) {
            gate.again.store(true, Ordering::SeqCst);
            return;
        }
        read_and_apply(app).await;
        gate.running.store(false, Ordering::SeqCst);
        if !gate.again.swap(false, Ordering::SeqCst) {
            return;
        }
    }
}

async fn read_and_apply(app: &AppHandle) {
    let Some(host) = app.try_state::<crate::Host>() else {
        return;
    };
    let Ok(bridge) = host.bridge().await else {
        return;
    };
    let Ok(settings) = bridge.call("settings.get", json!({})).await else {
        return;
    };
    // Language first: every label below is drawn from its table.
    apply(app, i18n::locale_from_settings(&settings));
    // The peers submenu needs the second call.  It is the round trip that buys
    // `devices.changed` its place in the refresh triggers; without it the menu
    // would list devices as they were when something *else* last changed.
    //
    // A failed read is not an empty device list.  `.unwrap_or(Value::Null)`
    // rendered as "no connected devices", which is the opposite of the truth on
    // a machine with paired peers and only lasts until the next successful
    // refresh — the same reason `settings.get` above returns early instead.
    let Ok(devices) = bridge.call("devices.list", json!({})).await else {
        return;
    };
    // The third call, and the one that makes the tray a way to *take* something
    // rather than only to switch sync on.  A failed read returns early like the
    // two above: an empty list would say "nothing has been copied", which is a
    // claim about the user's history that a failed request cannot make.
    let Ok(recent) = bridge
        .call("history.list", json!({"limit": RECENT_ROWS}))
        .await
    else {
        return;
    };
    let settings = &settings["settings"];
    let state = TrayState {
        device_name: settings["device_name"].as_str().unwrap_or_default().to_owned(),
        syncing: settings["sync_enabled"] == true,
        web_enabled: settings["web_enabled"] == true,
        pause_until: settings["timed_pause_until"].as_f64().unwrap_or(0.0),
        devices: device_lines(&devices),
        recent: recent_lines(&recent),
    };
    if let (Some(menu), Some(current)) = (
        app.try_state::<TrayMenu>(),
        app.try_state::<i18n::CurrentLocale>(),
    ) {
        menu.set_state(state, current.tray(), now_seconds());
    }
}

/// Re-render the pause countdown; nothing else in the menu ages on its own.
pub fn tick(app: &AppHandle) {
    if let (Some(menu), Some(current)) = (
        app.try_state::<TrayMenu>(),
        app.try_state::<i18n::CurrentLocale>(),
    ) {
        menu.tick(current.tray(), now_seconds());
    }
}

/// Re-render the countdown every [`PAUSE_TICK`] for the life of the process.
///
/// Cheap by construction: no round trip, and the render is skipped unless the
/// minute it shows actually changed.
pub fn spawn_ticker(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(PAUSE_TICK).await;
            tick(&app);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: f64 = 1_700_000_000.0;

    #[test]
    fn an_unarmed_or_passed_deadline_is_not_a_pause() {
        assert_eq!(pause_left_minutes(0.0, NOW), None);
        assert_eq!(pause_left_minutes(NOW - 60.0, NOW), None);
        // Exactly at the deadline the pause is over: the host's auto-resume
        // fires then, and the window's footer stops counting down there too.
        assert_eq!(pause_left_minutes(NOW, NOW), None);
    }

    #[test]
    fn whole_minutes_are_rounded_up_with_a_floor_of_one() {
        // A pause with a second left still reads as a minute rather than as
        // nothing, so the label never flickers through "0 minutes".
        assert_eq!(pause_left_minutes(NOW + 0.4, NOW), Some(1));
        assert_eq!(pause_left_minutes(NOW + 30.0, NOW), Some(1));
        assert_eq!(pause_left_minutes(NOW + 60.0, NOW), Some(1));
        assert_eq!(pause_left_minutes(NOW + 61.0, NOW), Some(2));
        assert_eq!(pause_left_minutes(NOW + 30.0 * 60.0, NOW), Some(30));
        assert_eq!(pause_left_minutes(NOW + 60.0 * 60.0, NOW), Some(60));
    }

    #[test]
    fn a_fill_replaces_its_placeholder_and_leaves_the_rest_alone() {
        assert_eq!(fill("{name} 已连接", "{name}", "Pixel"), "Pixel 已连接");
        assert_eq!(fill("已暂停 · 剩余 {minutes} 分钟", "{minutes}", "15"),
            "已暂停 · 剩余 15 分钟");
        // A device name is inserted as text: braces in it are not a template.
        assert_eq!(fill("{name}", "{name}", "{minutes}"), "{minutes}");
    }

    #[test]
    fn a_paired_device_reads_offline_the_moment_its_connection_drops() {
        // Paired is the trust, connection_state is the link: the row follows
        // the link, which is what the user is asking about.
        assert_eq!(
            classify(&json!({"paired": true, "connection_state": "online"})),
            DeviceState::Connected
        );
        for gone in ["offline", "discovered", ""] {
            assert_eq!(
                classify(&json!({"paired": true, "connection_state": gone})),
                DeviceState::Offline,
                "{gone}"
            );
        }
        // And a device whose only pairing is the code pairing is paired too:
        // its word is the relay's own view of it, which is the reading the
        // devices page carries on the same row's 互联网 chip.
        assert_eq!(
            classify(&json!({"relay_paired": true, "relay_online": true,
                             "connection_state": "offline"})),
            DeviceState::Connected
        );
        assert_eq!(
            classify(&json!({"relay_paired": true, "relay_online": false,
                             "connection_state": "discovered"})),
            DeviceState::Offline
        );
    }

    #[test]
    fn a_device_being_dialled_says_so_rather_than_reading_offline() {
        // The window says 连接中 and counts the attempt (`重连中 3/10`); this
        // menu used to say 离线 for the same row, which is the one word that
        // tells the reader the opposite of what the app is doing.
        assert_eq!(
            classify(&json!({"paired": true, "connection_state": "connecting"})),
            DeviceState::Connecting
        );
        // The retry the sidecar decorates a row with, whose link has not come
        // back yet: the attempt is what the page's chip counts, and a row with
        // no link at all is still the one being called.
        assert_eq!(
            classify(&json!({"paired": true, "connection_state": "offline",
                             "reconnecting": true, "reconnect_attempt": 3,
                             "reconnect_max": 10})),
            DeviceState::Connecting
        );
        // An unpaired device being called, with no code waiting on an answer.
        assert_eq!(
            classify(&json!({"connection_state": "connecting", "pairing_status": ""})),
            DeviceState::Connecting
        );
        // A code on screen outranks the dial: it is the state the user has to
        // answer, and the page says so on its own chip beside the state.
        assert_eq!(
            classify(&json!({"connection_state": "connecting", "pairing_status": "pending"})),
            DeviceState::Pairing
        );
        // Settled rows are untouched by it.
        assert_eq!(
            classify(&json!({"paired": true, "connection_state": "offline"})),
            DeviceState::Offline
        );
        assert_eq!(
            classify(&json!({"paired": true, "connection_state": "online", "reconnecting": true})),
            DeviceState::Connected
        );
    }

    #[test]
    fn an_unpaired_device_says_which_of_the_two_it_is() {
        // Mid-pairing is the one to wait for, so it gets its own word.
        for pending in ["pending", "peer_confirmed", "confirmed_waiting"] {
            assert_eq!(
                classify(&json!({"connection_state": "connecting", "pairing_status": pending})),
                DeviceState::Pairing
            );
            // Even while its link is up: pairing outranks connected here,
            // because it is the state that needs the user.
            assert_eq!(
                classify(&json!({"connection_state": "online", "pairing_status": pending})),
                DeviceState::Pairing
            );
        }
        // Talking to us without being trusted — a consented chat — reads the
        // same as a paired device, as the legacy tray had it.
        assert_eq!(
            classify(&json!({"connection_state": "online"})),
            DeviceState::Connected
        );
        assert_eq!(
            classify(&json!({"connection_state": "discovered"})),
            DeviceState::Found
        );
        assert_eq!(
            classify(&json!({"connection_state": "offline", "pairing_status": "rejected"})),
            DeviceState::Found
        );
    }

    #[test]
    fn a_peer_the_user_renamed_is_named_by_that_rename_here_too() {
        // The rule the window's `deviceLabel` states, and the defect it was
        // reported as: the devices page showed 试试 while this menu showed the
        // name the peer published about itself, for one and the same device.
        let renamed = json!({
            "id": "483fa196a05a",
            "name": "Kais-MacBook",
            "note": "试试",
            "paired": true,
            "connection_state": "online",
            "pairing_status": "paired",
        });
        assert_eq!(display_name(&renamed), "试试");
        // And the page's other half: a row the user has not renamed keeps the
        // peer's own name, and a note of only spaces is not a name.
        assert_eq!(display_name(&json!({"name": "Kais-MacBook", "note": ""})), "Kais-MacBook");
        assert_eq!(display_name(&json!({"name": "Kais-MacBook", "note": "   "})), "Kais-MacBook");
        assert_eq!(display_name(&json!({"name": "Kais-MacBook"})), "Kais-MacBook");
        // The row this menu draws, end to end.
        let lines = device_lines(&json!({"items": [renamed]}));
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0].name, "试试");
    }

    #[test]
    fn the_peer_rows_come_from_devices_list_and_leave_the_removed_out() {
        let lines = device_lines(&json!({"items": [
            {"id": "b", "name": "Beta", "paired": true, "connection_state": "offline",
             "pairing_status": "paired"},
            {"id": "a", "name": "Alpha", "paired": true, "connection_state": "online",
             "pairing_status": "paired"},
            {"id": "c", "name": "Gamma", "paired": false, "connection_state": "connecting",
             "pairing_status": "pending"},
            {"id": "d", "name": "Delta", "paired": false, "connection_state": "offline",
             "pairing_status": "", "archived": true},
            {"id": "e", "name": "Epsilon", "paired": false, "connection_state": "discovered",
             "pairing_status": ""},
        ]}));
        let shown: Vec<(&str, DeviceState)> = lines
            .iter()
            .map(|line| (line.name.as_str(), line.state))
            .collect();
        // Connected first, then the one being paired, then the rest by name —
        // and the removed device is not a peer any more.
        assert_eq!(
            shown,
            vec![
                ("Alpha", DeviceState::Connected),
                ("Gamma", DeviceState::Pairing),
                ("Beta", DeviceState::Offline),
                ("Epsilon", DeviceState::Found),
            ]
        );
    }

    #[test]
    fn rows_in_one_group_sort_the_way_the_page_sorts_them() {
        // Both lists sort inside a state group; the page's comparator is
        // `localeCompare(..., { numeric: true, sensitivity: "base" })`, and the
        // tray used to compare UTF-8 bytes.  Case and number runs are the two
        // differences that show up in ordinary device names.
        let lines = device_lines(&json!({"items": [
            {"id": "1", "name": "studio 10", "paired": true, "connection_state": "online",
             "pairing_status": "paired"},
            {"id": "2", "name": "Alpha", "paired": true, "connection_state": "online",
             "pairing_status": "paired"},
            {"id": "3", "name": "studio 2", "paired": true, "connection_state": "online",
             "pairing_status": "paired"},
            {"id": "4", "name": "beta", "paired": true, "connection_state": "online",
             "pairing_status": "paired"},
        ]}));
        let names: Vec<&str> = lines.iter().map(|line| line.name.as_str()).collect();
        assert_eq!(names, vec!["Alpha", "beta", "studio 2", "studio 10"]);
        // Two names that fold to the same string still get a total order: the
        // comparator says equal, and `device_lines`'s last word decides it, so
        // the menu does not depend on the order the sidecar listed them in.
        assert_eq!(compare_names("Alice", "alice"), std::cmp::Ordering::Equal);
        assert_eq!(compare_names("Alpha", "alpha"), std::cmp::Ordering::Equal);
        let folded = device_lines(&json!({"items": [
            {"id": "2", "name": "alice", "paired": true, "connection_state": "online",
             "pairing_status": "paired"},
            {"id": "1", "name": "Alice", "paired": true, "connection_state": "online",
             "pairing_status": "paired"},
        ]}));
        let folded_names: Vec<&str> = folded.iter().map(|line| line.name.as_str()).collect();
        assert_eq!(folded_names, vec!["Alice", "alice"]);
    }

    #[test]
    fn a_row_is_the_name_and_the_state_in_this_locale_s_own_punctuation() {
        let row = DeviceLine {
            name: "手机".to_owned(),
            state: DeviceState::Connected,
        };
        let zh = &i18n::strings("zh-CN").tray;
        let en = &i18n::strings("en").tray;
        // The state words are the devices page's own, which is what the enum's
        // comment claims: a peer that is up is 在线 / "Online" here, exactly as
        // the page's `connectionLabel` writes it, and 已连接 stays reserved for
        // the window header's own link to the engine.
        assert_eq!(row_label(&row, zh), "手机（在线）");
        assert_eq!(row_label(&row, en), "手机 (Online)");
        // A name is inserted literally, whatever it contains.
        let brace = DeviceLine {
            name: "{state}".to_owned(),
            state: DeviceState::Found,
        };
        assert_eq!(row_label(&brace, zh), "{state}（已发现）");
    }

    #[test]
    fn the_recent_rows_are_the_newest_five_and_carry_their_ids() {
        // `history.list` answers newest-first, and this menu has no second
        // opinion about that: a different order here would be a different
        // answer to the same question.
        let items: Vec<Value> = (0..8)
            .map(|i| json!({"id": format!("e{i}"), "preview": format!("clip {i}")}))
            .collect();
        let lines = recent_lines(&json!({"items": items}));
        assert_eq!(lines.len(), RECENT_ROWS);
        assert_eq!(lines[0].id, "e0");
        assert_eq!(lines[4].preview, "clip 4");
        // A row without an id cannot be copied, so it is not offered at all —
        // which is also what keeps a click from naming an empty entry.
        let mixed = recent_lines(&json!({"items": [
            {"id": "a", "preview": "one"},
            {"preview": "no id"},
            {"id": "", "preview": "empty id"},
            {"id": "b", "preview": "two"},
        ]}));
        let ids: Vec<&str> = mixed.iter().map(|line| line.id.as_str()).collect();
        assert_eq!(ids, vec!["a", "b"]);
        assert_eq!(recent_lines(&json!({})), Vec::<RecentLine>::new());
        assert_eq!(recent_lines(&json!({"items": null})), Vec::<RecentLine>::new());
    }

    #[test]
    fn a_recent_row_is_one_short_line() {
        let zh = &i18n::strings("zh-CN").tray;
        let en = &i18n::strings("en").tray;
        // A copied paragraph would otherwise turn five rows into twenty and push
        // the sync switch off the screen.
        assert_eq!(recent_label("line one\nline two\t\tthree", zh), "line one line two three");
        assert_eq!(recent_label("   padded   out   ", zh), "padded out");
        // Cut at a character boundary, not a byte one: a CJK clip has to lose
        // whole characters.
        let long = "剪".repeat(RECENT_CHARS + 10);
        let cut = recent_label(&long, zh);
        assert_eq!(cut.chars().count(), RECENT_CHARS + 1);
        assert!(cut.ends_with('…'));
        // Exactly at the limit nothing is cut, so no row ends in an ellipsis it
        // does not need.
        let exact = "字".repeat(RECENT_CHARS);
        assert_eq!(recent_label(&exact, zh), exact);
        // A clip with no text at all says what it is instead of drawing an empty
        // row that reads as a rendering fault.
        assert_eq!(recent_label("", zh), "（图片或文件）");
        assert_eq!(recent_label("   \n ", en), "(image or file)");
        // Redacted clips arrive as the filter left them, and are shown as such.
        assert_eq!(recent_label("[FILTERED]", en), "[FILTERED]");
    }

    #[test]
    fn a_state_that_arrives_garbled_still_ranks_rather_than_panicking() {
        // Both rank and label are total: an unknown device never drops a row.
        assert_eq!(device_lines(&json!({})), Vec::<DeviceLine>::new());
        assert_eq!(device_lines(&json!({"items": null})), Vec::<DeviceLine>::new());
        assert_eq!(
            device_lines(&json!({"items": [{"name": "X", "paired": true}]})),
            vec![DeviceLine {
                name: "X".to_owned(),
                state: DeviceState::Offline,
            }]
        );
    }
}
