# Changelog

All notable changes to ClipSync are documented in this file.

## [1.0.18] — 2026-08-22

### Fixed (regressions found by the v1.0.17 self-review)
- **Rejected incoming transfers no longer leak a direction entry** in memory (the per-transfer direction map grew one entry per rejected request forever).
- **Changing the receive directory now moves web/phone uploads too** — the Files list, download and delete now follow the same directory, so an uploaded file no longer appears missing after a receive-dir change.
- **Web-cancelled transfers show "Cancelled" immediately** — the WebSocket completion broadcast now carries the `cancelled` flag (the frontend's handler previously checked a field the backend never sent, so cancels briefly flashed "Failed" before a refetch corrected them).
- The **transfer target selector no longer silently re-aims** at a different device the instant the chosen peer goes offline — the "peer offline" hint shows and the send buttons disable instead.
- The web **speed test shows "Connect a device first"** when no peer is connected (was a generic "failed to start").

## [1.0.17] — 2026-08-22

### File transfer & speed test
- **Web pause/resume/cancel now actually reach the peer** — they previously dropped the control frame, so a web-paused transfer made the sender keep sending and then fail after 60s.
- **Speed test refuses to run with no connected peer** (previously reported a bogus "fast" result), and the desktop panel's speed unit is corrected to **MB/s** (was labeled Mbps, an 8× underestimate).
- **Receiver-side failures** (disk full / unwritable receive dir) now surface a notification instead of silently vanishing.
- Transfer completion notifications are **direction-aware**: the receiver is no longer told "File sent successfully", and a failed incoming transfer reports "File receive failed".
- **Sending to a peer that just went offline now fails loudly** (was a silent "success" with the file left on the sender).
- **Cancelled transfers are distinguished from failures** in transfer history (web + desktop), not lumped into "Failed".
- The incoming-transfer dialog shows the **sender's device name** instead of no identity.
- Phone/web uploads **honor the configured receive directory** and return a clear "file too large (max 128 MB)" instead of the cryptic "no file field".

### Desktop chrome
- Settings → Danger Zone **"Factory Reset" now actually wipes the data** — the exiting process previously re-created the config it had just deleted.
- Web **"Restart App" no longer intermittently quits with "another instance is already running"** (the single-instance lock is released before the new instance spawns).
- The tray **"Show Dashboard" reopens immediately** after closing the window (previously dead for up to 8 seconds), so tray QR/Send-URL actions aren't dropped.
- The per-event **transfer notification toggle now gates send confirmations** (was bypassed by direct calls).
- Dismissing the **startup encryption-password prompt exits cleanly with a message** instead of continuing with broken encryption.
- Settings "Restart App"/factory reset no longer print a Tk traceback; **TCP sync-port saves now show a restart-required note**.
- Web transfer **pause/resume/cancel button state reflects reality** (optimistic update + server reconcile), upload errors surface their real reason, and a stale target device is cleared when the peer goes offline.

## [1.0.16] — 2026-08-22

### Fixed (regressions found by the v1.0.15 self-review)
- **Regenerating or clearing the web token no longer lands on a raw 403 page.** The backend now returns the fresh token; the page rewrites the token in its URL and reloads seamlessly. (v1.0.15's "reload after regenerate" carried the now-stale URL token and hit a 403 dead-end with the SPA gone.)
- **Import is confined again.** v1.0.15's path relaxation let a token holder read arbitrary files off the disk by importing them into history and reading them back; import is now limited to the Downloads folder and the ClipSync data directory, and JSON items must carry ClipSync export fields to be accepted.
- **mobile.html**: a text clip no longer leaves a stale image on screen from a previously opened image clip, and the 5-second poll skips a tick while a request is still in flight (no overlapping, out-of-order renders on slow networks).
- **First-run wizard** now also appears on touch-primary desktop convertibles (it's gated on the local webview host in addition to the pointer type), instead of being silently suppressed by the `(pointer: fine)` check.
- The **restore summary toast is no longer immediately overwritten** by the restart-required note — both are shown together.

## [1.0.15] — 2026-08-22

### First-run & onboarding
- The **language picker now reappears on every launch until a language is actually chosen** — previously, closing it (or the config file already existing from identity bootstrap) permanently stranded English-only users in the default Chinese UI. Option cards also respond to clicks anywhere on them now, not just the thin border.
- The **first-run wizard's step 3 ("Use it on your phone") is reachable and real**: it shows the phone connect URL and a "Show QR Code" button (previously the step was dead code and the third progress dot lied).
- **Empty device names are rejected inline** with an error instead of being saved silently; the wizard now only appears on desktop-like clients (a phone opening the dashboard gets the normal app, not a "rename your PC" prompt); pressing Enter in the name field advances.
- Pairing codes render **grouped (1234 5678)** with an "expires in 5 minutes — reconnect to retry" hint.

### Mobile & phone pages
- `mobile.html` now **polls the active tab every 5s** (paused when the page is hidden), so new history/file entries appear without manually switching tabs.
- **Phone file uploads get a live progress bar, a cancel button, multi-file support** (uploaded sequentially), and a clear pre-flight error when a selection exceeds the 128 MB server cap (previously a silent "no file field").
- **Image clipboard items render in the phone modal** with tap-to-zoom (previously "(empty)").
- Phone fetches get an **8-second timeout with "same Wi-Fi / ClipSync running" guidance** instead of an indefinite shimmering skeleton.
- **Rich history actions (favorite/translate/paste-to-device/view) are reachable on iPhone** via touch long-press (iOS never fired the right-click menu); favorites can be **reordered with up/down arrows on touch** (HTML5 drag-and-drop doesn't work on phones); the quick-paste confirmation says **"Sent to computer"** instead of the misleading "Pasted!"; the quick-paste header respects the safe-area inset and localizes its tooltip/aria labels.

### Settings & data
- **Export JSON/CSV writes a real file to Downloads** (path shown in the confirmation) instead of deleting the temp file before the user could use it.
- **Import accepts any real file path** with clear localized errors (10 MB cap + content validation) instead of rejecting everything outside the private data directory.
- **Restore now auto-creates a backup of the current state first**, its confirmation states the real semantics (settings overwritten, history *merged*, not undoable), and a "restart required" toast appears when restored settings need one.
- **"History items shown" now actually persists** (the raw `v-model` string was being rejected by the backend type guard); the **theme choice persists to the app config**; **regenerating/clearing the web token warns first** and reloads the page with the fresh token so the session recovers.
- **Update downloads use a 120s timeout** (no more false "failed" on slow downloads) and **surface the real reason on failure** (e.g. "no release asset for this platform") instead of a generic error.
- The classic UI's **factory reset now deletes the full data set** (history/favorites databases included), matching the web path; the web **"Restart App" no longer reports a false failure** (exit is scheduled after the response flushes).
- Web/Data settings that need a restart now say so in the save confirmation.

### Regressions fixed from v1.0.14 (self-review pass)
- **Image items copied from the context menu paste the actual image again** (v1.0.14 changed "Copy" to a local text copy, which degraded pure-image items to a truncated text preview).
- Server-pushed dialog **focus is restored from the very first dialog** (not just the second), and a dialog **can no longer be double-submitted** while its response is in flight.
- **Cancelling a password change no longer leaks the encryption toggle** into a later unrelated save.

## [1.0.14] — 2026-08-22

### Desktop UI (fixed)
- **Saving Security settings can no longer wipe the pre-shared encryption password.** The password field always started blank and a save unconditionally copied it over the stored password, clearing the password hash and leaving the device identity key undecryptable after a restart. The field is now only applied when a new password is typed (with a confirmation explaining the private key is re-encrypted); a blank field leaves the stored password untouched.
- The dashboard's 5-second refresh no longer accumulates: every `show()`/re-show cancels the previous timer chain and the poll only re-arms while the window is actually visible, so repeated tray "Show Dashboard" clicks can't stack N refresh loops (a source of hidden-window CPU/battery drain and stutter).
- Uptime now measures app lifetime, not time since the window was last built — it no longer resets on every window rebuild.
- Copy-URL, note-editing, and other timer/dialog callbacks that could fire against a destroyed widget are now guarded (`winfo_exists` / try-except), fixing latent `TclError` crashes when a window is closed mid-action.
- The **first-run language picker** is no longer "consumed" by dismissing it: closing it with × returns to the picker on the next launch; only an actual language choice marks onboarding complete.
- The **Enable desktop notifications** toggle now applies live (no restart), the header theme toggle and Appearance radios stay in sync, and the receive-directory field expands `~` before validation so the documented `~/Downloads/ClipSync` placeholder is accepted.
- Enabling **Remote access** from desktop Settings now clearly states it takes effect after a restart instead of silently leaving the server off.
- The tray's sync checkbox is reconciled with the real sync-manager state instead of flipping optimistically, and non-Windows tray menu rebuilds are marshaled onto the tray thread (avoiding a context-menu race).
- A failed "Launch at login" toggle now reverts the switch and explains itself instead of silently sticking.

### Web dashboard (fixed)
- **Settings → Remote access "Copy" now appends the auth token** (the copied URL previously opened "Invalid token" on a phone, while Overview's copy worked) and the shown URL is derived live from the port/LAN-IP fields, so editing the port is immediately reflected.
- **Multi-select "Push to Desktop" actually pushes to the desktop** (via the server clipboard) instead of silently copying the merged text to the phone's browser clipboard.
- **Regenerating the web token no longer leaves the session and Overview Copy-URL on the dead token**; the stale token is cleared from the running session.
- **History pagination survives background clipboard syncs**: a LAN broadcast used to replace the loaded list with page 1 and discard everything the user had loaded via "Load more"; it now merges the broadcast in place (upsert by id, newest-first), preserving loaded pages.
- A failing dialog response (pairing/transfer/url-input) is no longer silent — the dialog stays open and a failure toast is shown instead of vanishing while the server keeps it pending.
- Server-pushed dialogs are now dismissible with **Esc** (blocking progress/confirm excepted), and Esc while typing in a search box no longer wipes selection/preview.
- The speed-test spinner can no longer stay spinning forever; WebSocket listeners are removed on teardown (no duplicate-handler stacking); the `web_history_limit` setting now applies to the first history fetch; a server dialog arriving over a pending local confirm no longer leaves the confirm promise stuck.
- **No more silent failures**: reordering favorites, renaming the device, and pausing/resuming/cancelling transfers all report success or failure; **Translate** now uses the full clip text instead of only its first 200 characters.
- **Clear all history** resets the pagination cursor like the other delete paths (no skipped items on later "Load more").

### Accessibility & mobile
- Pinch-zoom restored on all three pages (removed `user-scalable=no` / `maximum-scale=1`); `prefers-reduced-motion` handling added to the two phone pages (they previously ran infinite aurora/pulse animations for vestibular-sensitive users).
- The `lang` attribute is set at runtime to the actual UI language, so screen readers pronounce the text correctly.
- Settings toggles now expose `role="switch"` + `aria-checked` + accessible names (~27 controls); the quick-paste listbox is keyboard-focusable with `aria-activedescendant`; focus is returned to the triggering element after closing dialogs/modals; keyboard users can no longer tab into invisible (hover-only) history actions.
- Touch targets on the dashboard grow to ≥44 px on coarse-pointer devices; the app shell uses `100dvh` so the status bar isn't hidden behind mobile browser chrome; the quick-paste toast no longer overflows narrow phones; the status bar wraps instead of clipping.
- Muted meta-text contrast raised to ~5:1 on both themes.

### Localization & consistency
- Newly localized: desktop uptime/ETA, "QR err", log-export notifications, and placeholder hints; quick-paste relative-time strings and "(empty)" previews now render in the page language; the missing transfer pause/resume/cancel tooltip keys were added to both locales.
- Clipboard-type icons (URL/file/RTF/image) now come from one shared helper instead of three drifted per-tab mappings.
- Removed conflicting duplicate CSS rules (filter-chip background was silently overridden; duplicated media-query blocks), dead code, and a "Ungrouped" context menu whose only actions did nothing.

## [1.0.13] — 2026-08-22

### Security
- **Fixed a cross-origin token leak in the web companion.** The auth token was embedded in every static asset and those assets were served with `Access-Control-Allow-Origin: *`, so any web page could `fetch('http://127.0.0.1:<port>//index.html')` and read the token. Path aliases (`//index.html`, `/./index.html`) are now normalized to the canonical route, token-bearing static files are no longer CORS-readable, and all responses send `Referrer-Policy: no-referrer` + `X-Content-Type-Options: nosniff`.
- **Fixed stored XSS via the device name.** `device_name` / `device_id` are interpolated into an inline `<script>` literal, and `json.dumps` does not escape `<`, so a name containing `</script>` could execute arbitrary JS on the companion origin (exfiltrating the live token). Inline-script values are now escaped (`<` / `>` / `&`).
- The diagnostics "request permission" route (`/api/diagnostics/request`) is wired to its callback again, so the Firewall / Local Network permission buttons work from the web companion.
- `/api/logs` now redacts locally-sensitive strings (user home, config dir, web token) before serving them to web clients.
- Web API routes reject non-object JSON bodies with a clean 400 instead of a 500 plus a full stack trace.

### Sync & Clipboard
- Fixed a race in the sync manager where two debounced reads could run in parallel (a rich-content capture takes ~1.4s), broadcasting stale clipboard content out of order or dropping the newest copy — reads are now serialized.
- Windows: the clipboard reader and writer no longer race on `OpenClipboard`, so an incoming sync is no longer silently dropped mid-read; writes retry briefly, and a non-UTF-8 peer text decodes with `errors="replace"` instead of wiping the clipboard.
- Content filtering preserves the image format hint, so a filtered BMP/TIFF copy is no longer corrupted on Linux/macOS receivers.
- Linux: clipboard writes now check the tool's exit code and fall through to the secondary tool instead of silently "succeeding" on failure.
- History: `find_by_id` is type-tolerant (int vs str ids), fixing `/api/history/item` always returning 404; dashboard Copy/Delete act on the entry id so a search filter can no longer target the wrong entry; paste-to-top ordering is consistent between memory and the database so a just-pasted entry is not trimmed away on restart.

### File Transfer
- A late `file_chunk_ack` (e.g. after pause/resume) is now honored while waiting for `FILE_COMPLETE`, instead of being dropped after the first 3 seconds.
- Cancelling an incoming transfer no longer leaks the open temp handle / `.part` file, and stale `.part` files from a crash are swept on startup.
- Received files are verified against the total bytes actually written (`received_bytes`), not just the final file size, so a hole left by a short middle chunk is now caught.
- Release downloads stream to a `.part` file and are verified against the release size before rename, so an interrupted download never leaves a truncated installer at the final path.

### UI
- Transfers panel progress/state no longer freezes — the change-detection key now uses the real transfer fields.
- Fixed a Windows tray crash risk: the sync toggle no longer calls `update_menu()` (DestroyMenu) while the context menu is open.
- Dashboard `after()` timers (chunked history renderer, search debounce, copy-URL reset) are cancelled on hide/close so they can't fire against destroyed widgets.
- Dashboard network detection (`netsh` / `powershell`, up to ~15s) now runs once in a background thread instead of freezing the UI on every window build.

### First-run onboarding
- New bilingual **Choose Language** step on the very first launch: every label and option is shown in both 简体中文 and English, so anyone can complete it regardless of which language they read. The choice is remembered and changeable anytime in Settings → Appearance.

### Pairing & device lifecycle (interaction layer)
- **Pairing is now a true two-sided handshake.** Confirmation no longer happens in a vacuum: after you confirm, your device shows "已确认 · 等待对方确认…" and tells the peer; when the peer confirms (or rejects / un-pairs), you get a notification. Every state has a clear bilingual prompt.
- **Asymmetric confirmation handled**: one side confirming first puts the other side's card into a "对方已确认配对,请在此设备确认" prompt; both sides confirming completes the pairing; a never-confirmed request **expires after 5 minutes** with a "请求已过期" notice.
- **Reject / unpair propagated**: rejecting a pending request tells the peer ("对方已拒绝配对"); unpairing a paired device tells it too ("对方已取消配对"), and a device that was unpaired on the far side is notified on reconnect.
- **Device certificate changes** (reinstall / reset) now raise a friendly **重新信任 / 保持不配对** dialog instead of silently dropping the connection — both at startup (one dialog listing affected devices) and at runtime.
- **Rich pairing card UX**: the pairing code is larger, a guidance line explains to compare the code on both devices, and a verify-the-code confirmation dialog appears before accepting. Device cards show a clear status chip (已连接 / 已配对 · 离线 / 未配对 · 已发现) on both desktop and web.
- Backup **restore now validates every field** (types/ranges/enums) and persists immediately, so a malformed backup can no longer crash the transport on next start or silently vanish.

### Privacy & LAN exposure
- **No data reaches an unpaired peer**: the transport drops inbound app frames from unpaired peers and `broadcast()` skips them, so nothing (clipboard, history, files) is obtainable before both devices are paired.
- **mDNS advertisement tightened**: only RFC 1918 private LAN addresses are advertised (no public / VPN / virtual-adapter IPs), capped at 10, so the device exposes minimal network topology.
- macOS: fixed an unbounded Objective-C memory leak in the clipboard monitor's 0.4s pasteboard poll (autorelease pools around every ctypes→ObjC bridge call).

### Housekeeping
- Removed duplicate i18n dictionary keys and unused variables; applied ruff import-sorting / unused-import cleanups.

## [1.0.12] — 2026-08-22

### Fixed
- macOS: crash on launch (`*** CFHash() called with NULL ***` / SIGTRAP) in the global hotkey manager — the run loop mode is now passed correctly instead of as NULL
- Paired devices now auto-reconnect after a reboot/restart instead of sitting at "waiting for pairing"
- Windows: firewall setup no longer errors with `'NoneType' object has no attribute 'strip'`
- Logs tab and Diagnostics page now work on macOS/Linux (logs were read from the wrong directory; the diagnostics summary crashed with `UnboundLocalError`)
- Global hotkeys are now **off by default**, with a new settings toggle to re-enable them

### Settings
- Moved **Launch at login** out of the Network section into Preferences (applies immediately on toggle)

## [1.0.9] — 2026-08-21

### Diagnostics
- New standalone **Diagnostics** page in the left sidebar (Overview / History / Devices & File Transfer / Favorites / Diagnostics): one-click scan that reveals each check one by one (✓/✗) with a translated detail + actionable guidance line, then a final summary. Moved out of Settings → Advanced; the overview network-health chip now jumps straight to the page
- Checks cover the TCP server port, mDNS discovery, network advertising, web companion, network classification, firewall and permissions (macOS Local Network), plus Linux-specific checks (ufw/firewalld, avahi-daemon, xclip/wl-paste)
- The firewall and permissions checks carry a **Request permission / open settings** button: macOS opens the relevant System Settings pane; Windows re-applies the firewall allow rule or opens the firewall settings page
- Fixed the button previously failing with "Failed to open permission settings" (the `/api/diagnostics/request` backend route was missing)
- Diagnostics detail and guidance are now fully localized in English and Chinese, falling back to the server text when no translation exists

### Web Companion & onboarding
- PWA support: the web companion is now installable (app manifest, apple-touch-icon, token-safe service worker with offline app-shell caching)
- First-run onboarding wizard: name this device → pair a device → open it on your phone (skippable, persisted)

### Settings
- New **Logs** tab: view the log tail, refresh and export
- **Security** tab now lists trusted devices / certificate fingerprints
- Per-event notification toggles (device connect, transfer, pairing, sync)
- **Update download** button that fetches the platform release artifact into ~/Downloads
- i18n pass: replaced the remaining hardcoded user-facing strings across the frontend and backend dialogs

### UI
- Tray menu follows the app language and was redesigned (emoji icons, cleaner grouping)
- Title bar gained a **Refresh** button that reloads all data in one click
- History panel: filter chips, item count, sort and clear-all merged into a single combined sticky bar
- Modern app icon; toast text wrapping fixed; high-count badge / history layouts hardened
- Fixed square-corner glass inconsistencies, stale-data refreshes after idle, and a Settings → Advanced crash; unified remaining icons

## [1.0.8] — 2026-08-21

### Settings
- Translation configuration moved out of the Web Companion section into its own **Translation** tab
- Settings panel is now complete: added **Launch at login**, **mDNS service type**, **App Filter** (enable / black/whitelist mode / app list), **Clipboard behavior** (paste-to-top, low-memory mode, retry capture, dedup method, source-app tracking), and **Data locations** (data dir, favorites path)
- Almost every setting now takes effect **immediately** when saved, instead of only after a restart: auto-start, web companion on/off, sync debounce, retry capture, poll interval, low-memory mode, dedup method, reconnect attempts, transfer timeout, receive directory (the few that genuinely need a restart — TCP/mDNS/web ports, UI mode, encryption password, data paths — are clearly labelled)
- Fixed `dedup_method` never being wired (and crashing on "simple"); it now maps to a real hash (sha256 / md5)
- `paste_to_top` is now functional: re-using a history item surfaces it at the top

### Overview page
- Richer data from the backend: today's copies, pinned items, image count, completed transfers, total bytes transferred, connected-device names, discovered count, a recent clipboard activity feed, and the app version
- Redesigned overview: animated stat counters, a live network-map ring (connected / paired / discovered), connected-device neon chips, a recent-activity feed with staggered entry animations, and the device hero now shows version + OS
- Overview now reports the real network type (Wi-Fi / Ethernet + interface name) instead of a hard-coded "LAN"

### More
- **Global hotkey editor** added to Advanced settings (all 13 shortcuts, restart required)
- Config schema version added: old configs with `filter_enabled_categories: []` are migrated to `None` so existing installs keep content redaction ON, while a fresh save of `[]` stays a deliberate "disable all"
- Panel design aligned with the new overview: history filter chips and history items are now frosted-glass (no more flat white band), the transfer panel's send-file / folder buttons and speed test got proper glass layouts, and the overview "Connected Devices" card gained a count badge, a nicer empty state and solid quick-action buttons

## [1.0.7] — 2026-08-21

### Fixed
- Dashboard window no longer multiplies: opening is now idempotent (a live WebSocket client means the window is already open, and a short grace period covers page load), so repeated tray clicks / settings actions can't spawn duplicate browser windows or accumulate processes
- Closing the dashboard window is detected reliably, and quitting the app tells open dashboard windows to close themselves (no orphaned browser processes)
- App startup no longer fails in webview mode (the web server referenced `threading` without importing it)

### Changed (visual)
- Quick Paste (mobile/QR page) unified with the dashboard: cyberpunk aurora background with a slow drift, frosted-glass header and toast, pulsing title glow, and a cycling neon border on the selected item
- Dashboard: frosted-glass title bar, status bar, and toasts (backdrop blur + saturation over the aurora background)

## [1.0.6] — 2026-08-21

### Security
- Clipboard is now only broadcast to / accepted from **paired** peers — an unpaired TLS peer can no longer read or inject the local clipboard (pastejacking), open arbitrary URLs, or spawn transfer dialogs
- `nav_url` from peers is restricted to `http`/`https` (no more `file://` / custom-scheme launch)
- zlib frame decompression is capped (zip-bomb / OOM fix); /api/download rejects Windows drive-relative escapes
- Web settings save re-encrypts the device private key at rest; backup/export files are chmod 0600 and export temp files cleaned up
- Web token no longer logged to the (previously world-readable) log file
- Factory reset is no longer defeated by shutdown re-saving the deleted config
- Single-instance lock works for PyInstaller-frozen builds

### Fixed
- macOS web UI opens reliably even when Chrome/Edge is already running (browser binary launched directly with `--app`)
- Pairing: pairing requests now survive dashboard-closed (polled via /api/devices), Connect no longer claims success before the handshake, hashed-id reconnects still enforce cert pinning, pairing-code rate limit can't be reset by reconnecting
- File transfer: retransmission actually completes (finalizing flag reset), missing middle-chunk gaps detected, chunks stream to disk instead of buffering the whole file in RAM, 2 GiB size cap, paused transfers no longer auto-cancelled, web pause/resume no longer crash
- WebSocket: slow/stalled clients can no longer freeze clipboard sync; shutdown doesn't deadlock
- Clipboard: first copy after empty-clipboard start is no longer dropped, HTML/RTF-only changes detected, FILE/URL content dedups, image re-encode no longer re-broadcasts duplicates, Linux idle polling spawns far fewer subprocesses
- Web UI: transfers panel populates on load, redundant double-fetches removed, settings (sound/animation/language) persist, "Open file" and "Restart App" actually work, full clipboard text loads on copy/favorite instead of truncated preview
- Sensitive-content redaction is ON by default with broader matchers (tokens, keys, emails, JWT, AWS/GitHub/Slack secrets)
- CTk dashboard breath animation no longer re-queries everything at 5 fps; QR + LAN IP are cached

### Build / CI
- Linux hotkeys restored (`pynput` added to requirements)
- Release tag is verified to match `internal/version.py`; artifact smoke tests catch missing web UI; releases now run the test suite
- macOS bundle is ad-hoc signed with version keys; dead `pyobjc_framework_Cocoa` hiddenimport removed
- `upx` disabled (risky on macOS/arm64); customtkinter data bundled explicitly

## [1.0.5] — 2026-08-21

### Added
- "Check for Updates" (GitHub releases) and a versioned About dialog in the tray
- App icon (.ico) for Windows builds
- Quick actions (Show QR / Send URL) in the Overview panel
- Richer telemetry across the UI: status bar, overview stats, device cards, history, transfers

### Changed
- "Aurora Cyber" theme: unified cyan/violet/pink palette across light and dark, glass-morphism surfaces, aurora background glow
- Static assets served with `no-cache` so UI updates appear immediately after restart

### Fixed
- Settings panel and translate modal never registered (bare `t()` calls)
- Device unpair/forget/connect not refreshing the device list
- Tray actions (QR / send-url / settings) dropped when the webview window was not yet open
- Status bar showing stale "sync paused" / wrong connected-device count
- History pagination cursor after batch delete; transfer history missing direction/timestamp
- i18n keys missing from JSON locale files (sort control, update checker)
- Cross-platform: macOS tray wiring for check-update/about, Linux pynput dependency, color-mix fallback, spec icon placement
- macOS: web UI now launches the Chrome/Edge/Brave/Chromium binary directly with `--app`, so the dashboard opens even when the browser is already running (previously `open -a … --args --app` just activated the existing window and showed the browser start page instead of the app)
- Packaging: the web UI static files (`internal/web/static`) are now bundled by explicit filesystem path; `collect_data_files("internal.web")` silently skipped them during the spec's isolated package check, so packaged builds only ever served the minimal fallback page instead of the full dashboard

## [1.0.4] — 2026-08-21

### Added
- Modern web-based UI (WebView) with an in-app UI mode switch (Modern / Classic)
- Favorites panel in the web companion
- Per-device connect/disconnect controls
- Clipboard de-duplication and source tracking
- Durable SQLite-backed clipboard history database
- Send retry for failed clipboard syncs
- Global hotkey support

### Changed
- Version now has a single source of truth (internal/version.py) — pyproject, native About dialog, and web About panel all derive from it

## [1.0.0] — 2026-05-03

### Added
- Cross-platform clipboard sync (Windows, macOS, Linux)
- mDNS/Zeroconf automatic device discovery on LAN
- TLS 1.3 encrypted transport with Ed25519 certificates
- AES-256-GCM app-layer encryption per peer-pair
- At-rest encryption for private keys and clipboard history
- Optional pre-shared password for additional key entropy
- Trust-on-first-use (TOFU) device pairing with 8-digit codes
- System tray application with sync toggle and device status
- Dashboard with Overview, Devices, History, and Transfers panels
- Settings window with Network, Content Filter, Security, Advanced, Logs, and About sections
- **Web Companion** — built-in HTTP server for mobile phone access on the same LAN
  - QR code scanning to connect (no app install needed)
  - View clipboard history, push text to desktop, transfer files
  - PWA support with app icon for "Add to Home Screen" on iOS/Android
  - Pin/unpin and delete history items from the web page
  - File upload/download between phone and desktop
  - iOS install banner with instructions
  - Animations (fade-in cards, refresh spin, push button pulse)
- File transfer between paired devices with progress tracking
- Speed test for measuring LAN throughput
- Content filtering for sensitive data (credit cards, SSNs, API keys, etc.)
- Clipboard history with search, copy, delete, pin/unpin, and pinned-first sorting
- Dark mode support (light/dark/system)
- Auto-start on system login
- Desktop notifications for connect/disconnect and sync events
- Log viewer and export within the app
- PyInstaller standalone builds for all platforms
- Factory reset and restart buttons in advanced settings

### Security
- PBKDF2 password verification (password never stored in plaintext)
- Certificate pinning with change detection (potential MITM alert)
- Rate-limited pairing code attempts (5 per 5-minute window)
- Path traversal prevention in file transfers
