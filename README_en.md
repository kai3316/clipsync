<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp;
  <a href="README_zh.md">中文</a>
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
2. Run ClipSync on another device on the **same LAN**
3. Confirm the 8-digit pairing code that appears on both screens
4. Copy on one device → paste on the other. Done.

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

Clips are deduplicated by content hash, not timestamp. Rapid alternating copies between devices won't cause echo loops.

### File Transfer

- **Peer-to-peer** — files go directly between devices, not through a relay
- **Chunked protocol** — large files split into 1 MB chunks with ACK-based retransmit
- **Folder support** — drag a folder to send it as a zip
- **Pause/Resume** — pause mid-transfer and resume from where you left off
- **Progress tracking** — per-file progress bars with speed readout (Mbps)
- **Speed test** — measure raw LAN throughput between paired devices

### Web Companion

- Built-in HTTP server accessible from any device on the LAN
- QR code to connect — scan with phone camera, no app install needed
- **PWA** — "Add to Home Screen" on iOS/Android for a native feel
- View clipboard history, push text to desktop clipboard
- Upload and download files between phone and desktop
- Token-based authentication (auto-generated or custom)

### Security

- **TLS 1.3** — all transport encrypted with per-device Ed25519 certificates
- **AES-256-GCM** — application-layer encryption per frame
- **TOFU pairing** — trust-on-first-use with 8-digit code verification, certificate pinned thereafter
- **At-rest encryption** — private keys and clipboard history encrypted on disk (AES-256-GCM + PBKDF2)
- **Optional pre-shared password** — extra key entropy via PBKDF2 with 600K iterations
- **Certificate change detection** — alerts if a paired device's identity changes (MITM protection)

### Content Filtering

Regex-based filters to warn or block before sending sensitive data:
- Credit card numbers
- SSN / social security numbers
- API keys and tokens
- Email addresses
- Phone numbers
- Custom patterns

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
    history.py                #   Encrypted clipboard history store
    filter.py                 #   Regex-based content filtering
  config/
    config.py                 #   JSON config + encryption + atomic save
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
  sync/
    manager.py                #   SyncManager: clipboard change → encode → broadcast
    file_transfer.py          #   Chunked file transfer with ACK retransmit
  transport/
    connection.py             #   TransportManager + PeerConnection (TLS 1.3 sockets)
    discovery.py              #   mDNS service advertisement + browsing
  ui/
    dashboard.py              #   Main window: Overview, Devices, History, Transfers
    settings_window.py        #   Settings: Network, Appearance, Web Companion, Filter, Security, Advanced, Logs, About
    dialogs.py                #   Reusable dialogs (ask_string, ask_yesno, show_info, show_error)
    systray.py                #   Cross-platform system tray icon + menu
  web/
    server.py                 #   HTTP server: QR endpoint, history API, file upload/download, PWA manifest
tests/                        #   218 tests covering clipboard, codec, config, pairing, sync, file transfer, cross-platform
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
