# Changelog

All notable changes to ClipSync are documented in this file.

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
