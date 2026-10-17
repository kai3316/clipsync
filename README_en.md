<p align="center">
  <a href="README_en.md">English</a> &nbsp;|&nbsp;
  <a href="README.md">中文</a>
</p>

<p align="center">
  <img src="assets/icon.svg" alt="ClipSync" width="96" height="96">
</p>

<h1 align="center">ClipSync</h1>

<p align="center">
  Copy on one device. Paste on the other.<br>
  Direct over your local network · end-to-end encrypted · no account
</p>

<p align="center">
  <a href="https://github.com/kai3316/clipsync/releases"><img src="https://img.shields.io/github/v/release/kai3316/clipsync?color=087c6b" alt="Release"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platforms">
</p>

<p align="center">
  <img src="docs/assets/en-overview.png" alt="The ClipSync overview page" width="820">
</p>

---

## What it does

ClipSync keeps the clipboard in sync between your computers: copy on one, paste on the other. Text, images, links and files all work.

After installing it, computers on the same Wi-Fi find each other. Copy on one and paste on the other; there is no account to sign in to, and content is not uploaded anywhere first. If the two computers are on different networks, turn on internet sync and it keeps working. A phone can use it from the browser after scanning a QR code, with no app to install.

It is free and open source, with installers for Windows, macOS and Linux.

It fits a few everyday situations: keeping the clipboard in sync between your own computers, sending a file from one to another, continuing to sync between home and the office, and reaching the computer's content from a phone. A new device has to be paired once, but no account is needed.

Pairing is how two devices first agree to trust each other: both show the same 8-digit number, you check that they match, and after that they connect automatically.

## Install

Download the installer for your system from [Releases](https://github.com/kai3316/clipsync/releases/latest):

| System | Installer |
|---|---|
| Windows 10 / 11 (64-bit) | `ClipSync_<version>_x64-setup.exe` |
| macOS 12+ (Apple silicon) | `ClipSync_<version>_aarch64.dmg` |
| Linux (x86_64) | `ClipSync_<version>_amd64.deb` or `.AppImage` |

The installer includes everything it needs; Python is not required. There is no build for Intel Macs yet.

**On the first launch on macOS**, the system may say the app is damaged and cannot be opened. The app is not notarized by Apple. Run this once in Terminal:

```bash
xattr -dr com.apple.quarantine /Applications/ClipSync.app
```

If it instead says the developer cannot be verified, right-click the app and choose Open.

**On Linux**, the clipboard needs `xclip` (X11) or `wl-clipboard` (Wayland). The `.deb` pulls in `xclip` automatically; if you use the `.AppImage`, install one yourself, or clipboard sync will not work. The app will show a warning in that case.

## First run

1. Connect both devices to the same Wi-Fi.
2. Start ClipSync on each.
3. Start pairing on either device. Both screens show the same 8-digit code; check that they match, then confirm.
4. Copy on one device and paste on the other.

If the computers are on different networks (one at the office, one at home, for example), turn on Settings → Internet sync on both and pair as usual. After that, content is encrypted before it is handed to a public forwarding service, which cannot read it.

## What it can do

- Clipboard sync: text, images, links and files, ready to paste on the other computer.
- Files and folders: drop them on the window and pick a computer; an interrupted transfer continues where it stopped.
- Different networks: turn on internet sync and it keeps working.
- Nearby chat: computers on the same Wi-Fi can chat without pairing first.
- Favorites: keep useful clips on a separate shelf.
- AI config: compare and move AI tool configuration between computers.
- Phone companion: open it in the phone's browser by scanning a QR code, with no app to install.
- Diagnostics: when a computer cannot be found or connects badly, check the network, firewall and permissions.

## Privacy and security

- On the same network, content travels directly between the computers, with no server in between.
- On different networks, content is encrypted before it is handed to a forwarding service, which cannot read it.
- The first pairing shows the same 8-digit number on both computers; any later change to the other device's identity raises an alert.
- The private key and clipboard history stored on the computer are encrypted.
- Card numbers, national ID numbers, API keys, private keys and passwords are replaced with `[FILTERED]` before sending (this can be turned off).

## Updates

The desktop app checks for updates on its own. Keep "Check for updates automatically" on in Settings → System, and a new version will be downloaded, installed and restarted. On Linux a `.deb` install asks for your password once; an `.AppImage` does not.

If another computer on the same Wi-Fi has already updated, the app can update from it without reaching GitHub.

## Troubleshooting

| Symptom | What to try |
|---|---|
| The computers cannot find each other | Make sure they are on the same Wi-Fi; turn off "AP isolation" or "client isolation" on the router |
| They cannot find each other but the network is fine | Check that the firewall allows UDP 5353 and TCP 19990, or use the one-click repair on the diagnostics page |
| Paired but not syncing | The devices page should say "connected" for the other computer; "paired" alone means the link is not up |
| Paired but not syncing | Check that sync is not paused on the overview page or in the tray |
| A device identity change warning | The other computer was reinstalled or reset. If that was you, confirm it; otherwise remove the device and pair again |
| Port already in use | Settings → Network and advanced → TCP port, and choose another |
| macOS says the app is damaged | Run the `xattr` command above |
| The phone cannot open the page | Make sure the phone and computer are on the same network, and check the address in Devices → Phone companion |
| The phone says the access code is invalid | The code changed; scan the QR code on the computer again |

## Running from source

You need Python 3.12+, Node.js 22.12+, Rust stable and the C toolchain for your platform.

```bash
git clone https://github.com/kai3316/clipsync.git
cd clipsync
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e .
```

Start the desktop app:

```powershell
.\Start-ClipSync.bat            # Windows
```

```bash
cd desktop && npm ci && npm run tauri -- dev    # macOS / Linux
```

`Start-ClipSync.bat` is a launcher for development, not the packaged application. The first run compiles the whole Rust program, and its data stays in `.tauri-dev-data` inside the repository. To use ClipSync, download an installer instead.

Run the tests:

```bash
python -m pytest tests -q
cd desktop && npm test && npm run test:e2e
```

## Links

- [Releases](https://github.com/kai3316/clipsync/releases): changes and installers
- [Issues](https://github.com/kai3316/clipsync/issues): report a problem
- [Landing page](https://kai3316.github.io/clipsync/index_en.html): screenshots and downloads

## License

MIT.
