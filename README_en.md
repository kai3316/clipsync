<p align="center">
  <a href="README_en.md">English</a> &nbsp;|&nbsp;
  <a href="README.md">中文</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/kai3316/clipsync/master/assets/icon.svg" alt="ClipSync" width="96" height="96">
</p>

<h1 align="center">ClipSync</h1>

<p align="center">
  <strong>Copy on one device. Paste on another. Instantly.</strong>
  <br>
  Cross-platform &middot; LAN &middot; TLS 1.3 + AES-256-GCM &middot; Zero config
</p>

<p align="center">
  <a href="https://github.com/kai3316/clipsync/releases"><img src="https://img.shields.io/github/v/release/kai3316/clipsync?color=3498DB" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platforms">
</p>

---

## Why ClipSync?

You're working on your desktop and need to paste something on your laptop. Or you copied text on your phone and want it on your PC. Existing solutions either go through the cloud (privacy risk, requires internet) or only sync plain text (losing formatting and images).

ClipSync syncs your clipboard across devices **directly over your local network** — no account, no cloud, no internet required. It preserves all clipboard formats (text, HTML, RTF, images), transfers files peer-to-peer with encryption, and even serves a QR code so your phone can join via a web browser with no app install.

---

## Quick Start

1. [Download](https://github.com/kai3316/clipsync/releases/latest) and run — portable, no install needed
2. Pick your interface language on first launch — the picker shows both 中文 and English
3. Run ClipSync on another device on the **same LAN**
4. Confirm the 8-digit pairing code that appears on both screens
5. Copy on one device → paste on the other. Done.

> **macOS:** If Gatekeeper blocks the app, run `xattr -cr clipsync.app` then right-click → Open.

---

## How It Works

```
  +------------+                             +------------+
  |  Device A  |  --- mDNS discover -------> |  Device B  |
  |            |  <-- TLS 1.3 handshake ---  |            |
  |  clipboard |  --- clipboard content ---> |  clipboard |
  |            |  <-- AES-256-GCM frame ---  |            |
  +------------+                             +------------+
                                                   |
                                              QR code scan
                                                   |
                                             +------------+
                                             |   Phone    |
                                             |   (PWA)    |
                                             +------------+
```

1. **Discovery** — Devices find each other via mDNS/Zeroconf on the LAN. No IP configuration needed.
2. **Pairing** — Trust-on-first-use with 8-digit code verification. Ed25519 certificate pinning thereafter.
3. **Sync** — Clipboard changes are broadcast over TLS 1.3. Each frame is AES-256-GCM encrypted. A per-peer dedup ring prevents echo loops.
4. **Phone access** — Enable the Web Companion to get a QR code. Your phone scans it and gets a PWA for viewing history, pushing text, and transferring files.

---

## Features

### Clipboard Sync

| Format | Supported |
|--------|-----------|
| Plain text (UTF-8, CF_TEXT) | ✅ |
| Unicode text (CF_UNICODETEXT) | ✅ |
| HTML (CF_HTML / `text/html`) | ✅ |
| RTF (CF_RTF / `text/rtf`) | ✅ |
| Images (PNG, BMP, TIFF, DIB) | ✅ |
| EMF (Windows metafile) | ✅ |

Clips are deduplicated by content hash, not timestamp. Rapid alternating copies between devices won't cause echo loops. Dropped devices reconnect automatically in the background, with live "Reconnecting N/M" progress on the device panel.

### File Transfer

- **Peer-to-peer** — files go directly between devices, not through a relay
- **Chunked protocol** — large files split into 1 MB chunks with ACK-based retransmit
- **Folder support** — drag a folder to send it as a zip
- **Pause/Resume** — pause mid-transfer and resume from where you left off
- **Failures stay visible, one-click retry** — failed transfers remain in history tagged with the reason (disk full / peer offline / timeout, …); failed outbound files can be re-sent as-is
- **Progress tracking** — per-file progress bars with speed readout (Mbps)
- **Speed test** — measure raw LAN throughput between paired devices

### Internet Sync

- **Sync across networks — office ↔ home.** Enable "Internet sync" in settings and clipboard text / small payloads are mirrored over free public MQTT-over-WebSocket relays: zero cost, zero signup. The broker list defaults to three well-known free public services and is editable, with automatic failover.
- **End-to-end encrypted.** Relays only ever see ciphertext — the key is derived from both devices' secrets and the topic is unguessable. LAN delivery stays primary; the relay is a mirror path for paired peers outside the network, and duplicate-merge collapses double deliveries.
- **Compare the security code when pairing.** Both devices show the same short safety code (SAS) during pairing confirmation — verify they match before confirming, which closes the pairing-code man-in-the-middle window that matters on the public internet.
- **Internet pairing management on the Devices page.** Devices that have never met pair with a 12-character code; paired devices are managed on the Devices page — alias rename, online / last-synced status, and one-sided unpair.
- **Chat works across the internet.** Start a conversation with an internet-paired device even when it's off your network (text, typing indicator and session state ride the relay); delivery is LAN-first so a device reachable both ways gets each message exactly once.
- **Delivery confirmation + offline queue.** Relay messages carry an end-to-end ack (✓ delivered / ✗ not delivered in the bubble); if the peer or relay is unreachable, the clip lands in a persisted local queue and is retried automatically (up to 5 attempts).
- Note: free public relays are best-effort (no SLA); large file transfers and chat files stay on the LAN and don't go through the relay.

### AI Config Sync

- **Browse and migrate AI tool configs across your own devices.** A new "Config" tab lists what each paired device has on its watch list (CLAUDE.md, memory notes, skills, `.mcp.json`, ... — the monitored root folders are editable in settings), letting you preview any file and pull it over.
- **Never overwritten silently.** Choose per pull: overwrite / save a copy / append to Markdown (text files only). Inventories carry metadata only (path, hash, size, time) — file content moves only when you request it, over the same encrypted channel.
- **AI-tool config presets.** The default watch list covers the real paths used by Claude Code / Codex / Cursor / Gemini CLI (CLAUDE.md, settings.json, skills, rules, GEMINI.md, ...), added with one click; credential files (e.g. auth.json) stay excluded.
- **New-vs-old comparison badges.** The cross-device browser marks every file  missing / same / local newer / remote newer (hash then modified time), so you can pick the pull direction before anything is overwritten.
- **Local config manager.** Even with no paired devices you can manage the files on your watch list — browse, preview, edit & save (auto `.bak`), remove to a trash folder (never hard-deleted), and open the containing folder.
- Note: cross-device browsing requires paired devices; single files over 1 MB are truncated with a clear flag.

### Web Companion

- Built-in HTTP server accessible from any device on the LAN
- QR code to connect — scan with phone camera, no app install needed
- **PWA** — "Add to Home Screen" on iOS/Android for a native feel
- View clipboard history, push text to desktop clipboard
- Upload and download files between phone and desktop
- Stackable toast notifications; keyboard navigation in the history list (↑/↓ select, Enter copy, Del delete); newest/oldest sort toggle; one-click push a favorite to the desktop clipboard; right-click "open link in browser" for URL entries
- Token-based authentication (auto-generated or custom)
- **Comprehensive diagnostics** — the Diagnostics page groups every module (system / network / internet / AI config / chat / transfers / filesystem), each item with ok/warn/fail status, detail and a fix hint
- **Test relay connectivity** — a one-click button in Settings → Network probes every configured relay broker (TCP/TLS handshake, latency per broker) without disturbing the live session
- **Web-first direction** — new features land in the web dashboard only; the classic Tk desktop UI is being phased out (stability fixes only). The mobile page is kept in step with the desktop dashboard.

### Nearby Chat

- **No pairing required** — chat directly with discovered-but-unpaired devices on the LAN
- **Strict two-sided consent** — nothing flows until the other user explicitly accepts; unaccepted invites reveal no content
- Unpaired devices show a short certificate fingerprint for out-of-band verification; paired devices accept automatically
- Send text and files (chunked over the existing transfer channel) with automatic reconnect
- Works from both the desktop and the web dashboard (live session list, unread badges, file send/receive)
- Invites / text / files are rate- and concurrency-limited; file names sanitized + path-traversal blocked

### Security

- **TLS 1.3** — all transport encrypted with per-device Ed25519 certificates
- **AES-256-GCM** — application-layer encryption per frame
- **TOFU pairing** — trust-on-first-use with 8-digit code verification, certificate pinned thereafter
- **At-rest encryption** — private keys and clipboard history encrypted on disk (AES-256-GCM + PBKDF2)
- **Optional pre-shared password** — extra key entropy via PBKDF2 with 600K iterations
- **Certificate change detection** — alerts if a paired device's identity changes (MITM protection)

### Content Filtering

Regex-based filters that replace matches with `[FILTERED]` before syncing, and annotate them in history:
- Credit card numbers
- SSN / social security numbers
- API keys and tokens
- Private keys
- Passwords
- Email addresses (opt-in, OFF by default — keeps ordinary addresses in everyday text from being mangled)

### Data Management

- **Dual retention limits** — history trimmed by entry count and by age in days (configurable, 0 = keep forever); pinned items are never aged out
- **Multi-format export** — export the full clipboard history as JSON / CSV / Markdown
- Corrupted rows are isolated instead of breaking the whole history; clearing history reclaims disk space (VACUUM)

### System Tray

Runs quietly in the system tray with:
- Sync on/off toggle
- Connected device status (per device)
- Quick access to dashboard and settings
- Web Companion QR code popup
- Notifications for pairing requests and completed transfers

---

## Download

| Platform | File | Notes |
|----------|------|-------|
| Windows 10/11 | `clipsync.exe` | Portable, no admin needed |
| macOS 12+ | `clipsync.app` (zip) | Universal binary (Intel + Apple Silicon) |
| Linux (X11/Wayland) | `clipsync` (tar.gz x86_64) | Requires `xclip` or `wl-clipboard` |
| Linux (ARM64) | `clipsync` (tar.gz arm64) | Raspberry Pi 4/5, etc. |

[Latest release](https://github.com/kai3316/clipsync/releases/latest) &nbsp;|&nbsp; [Changelog](CHANGELOG.md)

---

## Install from Source

**Requirements:** Python 3.12+

```bash
git clone https://github.com/kai3316/clipsync.git
cd clipsync
python -m venv .venv
source .venv/bin/activate   # macOS / Linux
# .venv\Scripts\activate    # Windows
pip install -r requirements.txt
python src/main.py
```

**Linux** — install the clipboard backend for your display server:

```bash
sudo apt install xclip          # X11
sudo apt install wl-clipboard   # Wayland
```

**Windows** — clipboard I/O uses the native Win32 API. No extra dependencies.

---

## Build

```bash
pip install pyinstaller
pyinstaller clipsync.spec
```

Output in `dist/`: `clipsync.exe` (Windows), `clipsync.app` (macOS), or `clipsync` (Linux).

---

## Troubleshooting

| Problem | Likely Cause | Fix |
|---------|-------------|-----|
| Devices not discovering | Different subnet or AP client isolation | Ensure all devices are on the same LAN segment. Check router for "AP isolation" or "client isolation" settings. |
| Devices not discovering | Firewall blocking mDNS | Allow UDP port 5353 and TCP port 19990 (default) in firewall. |
| Sync not working | Peer not connected | Check Devices panel — peer should show "Connected". If "Paired" only, check firewall on both sides. |
| Sync not working | Sync toggle off | Click the sync icon in the system tray or toggle in dashboard. |
| Certificate change alert | Peer re-installed or identity reset | If you recently reset the peer, this is expected. Otherwise, Forget the peer and re-pair. |
| VPN causes wrong IP | VPN interface prioritized | Fixed in v1.0.1 — LAN IPs (192.168.x.x) now take priority over VPN interfaces. |
| Port conflict | Another app using port 19990 | Change the TCP port in Settings → Network. |

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| UI | CustomTkinter (cross-platform desktop) |
| Transport | Python `asyncio` + `ssl` (TLS 1.3) |
| Discovery | python-zeroconf (mDNS/DNS-SD) |
| Encryption | `cryptography` (Ed25519, AES-256-GCM, PBKDF2) |
| Clipboard | Win32 API / `pbpaste`+`pbcopy` / `xclip`+`wl-paste` |
| QR Code | `qrcode` + Pillow |
| Web Server | Python `http.server` (ThreadingHTTPServer) |
| Build | PyInstaller (single-file executable) |
| CI/CD | GitHub Actions (multi-platform build + release) |

---

## Architecture

```
src/main.py                   # Entry point: tray, lock file, lifecycle
internal/
  clipboard/                  # Native clipboard I/O per platform
    clipboard.py              #   Abstract base + factory
    clipboard_windows.py      #   Win32 clipboard API (CF_* formats)
    clipboard_darwin.py       #   macOS pbpaste/pbcopy + osascript
    clipboard_linux.py        #   Linux xclip / wl-clipboard
    format.py                 #   ClipboardContent dataclass + ContentType enum
    history_db.py             #   SQLite clipboard history store (encrypted) + corruption self-heal
    history.py                #   Legacy JSON history implementation (compat layer)
    dedup.py                  #   Dedup hashing
    retry.py                  #   Multi-round stabilization capture
    source_tracker.py         #   Source-app tracking + app filter
    filter.py                 #   Regex-based content filtering
  config/
    config.py                 #   JSON config + encryption + atomic save
  data/
    backup.py                 #   Backup / restore (incl. paired devices)
    export.py                 #   History export / import (JSON/CSV/Markdown)
  i18n/
    __init__.py               #   EN / ZH translation tables
  platform/
    autostart.py              #   OS-specific autostart registration
    notify.py                 #   Desktop notification (native or tkinter)
  protocol/
    codec.py                  #   Binary frame encoding (magic + version + JSON + zlib)
  security/
    encryption.py             #   AES-256-GCM at-rest encryption + PBKDF2
    pairing.py                #   Ed25519 identity, TOFU pairing, fingerprint verification
    fingerprint.py            #   Pairing SAS short security-code derivation
  sync/
    manager.py                #   SyncManager: clipboard change → encode → broadcast
    file_transfer.py          #   Chunked file transfer with ACK retransmit
    nearby_chat.py            #   Nearby chat: no pairing, two-sided consent, text/files
    ai_config.py              #   AI config sync: inventory collection + selective pull
  system/
    hotkey.py                 #   Global hotkeys
    updater.py                #   Update check + download
  transport/
    connection.py             #   TransportManager + PeerConnection (TLS 1.3 sockets)
    discovery.py              #   mDNS service advertisement + browsing
    relay.py                  #   Internet relay (MQTT/WebSocket, E2E encrypted)
  ui/
    dashboard.py              #   Main window: Overview, Devices, History, Transfers, Nearby Chat
    settings_window.py        #   Settings: Network, Appearance, Web Companion, Filter, Security, Advanced, Logs, About
    dialogs.py                #   Reusable dialogs (ask_string, ask_yesno, show_info, show_error)
    systray.py                #   Cross-platform system tray icon + menu
  web/
    server.py                 #   HTTP server + auth gating
    routes.py                 #   API route dispatch
    ws.py                     #   WebSocket live push
    api/                      #   devices/history/favorites/transfer/settings/translate/security/aiconfig
    static/                   #   Web dashboard, phone pages, PWA assets
tests/                        #   949 tests covering clipboard, codec, config, pairing, sync, file transfer, chat, web API, cross-platform
```

### Data Flow

```
Clipboard change (OS)
    → platform clipboard reader (native formats)
    → ClipboardContent (normalized data model)
    → SyncManager (dedup check, encode)
    → TransportManager (broadcast to all connected peers)
    → PeerConnection (TLS 1.3 socket write)
    → Network (LAN)
    → PeerConnection (TLS 1.3 socket read)
    → TransportManager (decode frame)
    → SyncManager (dedup check, write to local clipboard)
    → platform clipboard writer (native formats)
```

### Security Model

Each device generates an Ed25519 key pair at first launch. The public key becomes the device identity. On first contact with a new peer, both sides display an 8-digit pairing code (derived from the TLS 1.3 session). The user verifies and confirms the code on both sides. The peer's certificate fingerprint is then stored ("pinned"). Future connections verify the fingerprint — if it changes, the user is alerted (potential MITM).

All data on the wire is double-encrypted: TLS 1.3 provides transport security, and each frame body is independently AES-256-GCM encrypted. Data at rest (private keys, clipboard history) uses AES-256-GCM with a key derived from a device-specific seed via PBKDF2 (600K iterations).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, project structure, and guidelines.

PRs welcome. Please run `python -m pytest tests/ -v` before submitting.

---

## License

MIT — see [LICENSE](LICENSE)
