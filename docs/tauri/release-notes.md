# ClipSync New Desktop Preview

## Current state

The Tauri desktop is the active desktop implementation and starts through
`Start-ClipSync.bat` or `scripts/start-desktop.ps1`. The Python sidecar uses the
existing configuration, history, favorites, pairing, transfer, chat, backup,
translation, AI profile, and Web Companion-compatible business services.

## Verification

- Python sidecar suite: 201 passed.
- Vue desktop suite: 54 passed.
- Web Companion suite: 35 passed.
- Native smoke: launcher, IPC, persistence, shutdown/reopen, and permission
  checks passed.
- Packaged sidecar smoke: two isolated restart cycles passed.

## Rollback

Do not remove or overwrite the existing data directory during rollback.
Stop the Tauri desktop and use the legacy desktop entry point with the same
configuration directory. Re-run the history and configuration backup checks
before switching back. The mobile Web Companion remains enabled independently.

## Release blockers

Signed installer/update verification, tray and notification behavior, global
shortcut/autostart support, cross-platform validation, and two-device
internet/LAN synchronization tests are still required before retiring legacy
desktop entry points.
