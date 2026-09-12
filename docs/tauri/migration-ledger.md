# ClipSync Tauri Migration Ledger

## 2026-09-07

- Scope: Tauri 2 + Vue desktop, Python sidecar, existing mobile Web Companion.
- Verified: sidecar tests `201 passed`; desktop tests `54 passed`; Web Companion tests `35 passed`.
- Verified: Rust `cargo check --locked`; packaged sidecar IPC startup/status/history/shutdown/reopen twice.
- Verified: native smoke including launcher, IPC, favorites CRUD, persistence, shutdown/reopen, and shell denial.
- Added: internet pairing handshake, relay ACK/retry queue, delivery status RPC/UI, launcher and sidecar packaging checks.
- Added: native tray show/quit menu, close-to-tray lifecycle, and AI-config inventory/preview/pull/local file management through the sidecar.
- Added: Tauri-hosted Windows per-user autostart registration, status query, and failure rollback; the sidecar no longer owns desktop autostart.
- Verified: sidecar `207 passed`, desktop `54 passed`, Rust `14 passed`, Web Companion `35 passed`, production build, and direct native smoke after tray/AI integration.
- Verified: LAN/runtime integration suite `36 passed` and full sidecar regression remains green after AI and autostart changes (`208 passed`).
- Open: native notifications, global shortcuts, signed installer/update/rollback, cross-platform and two-device tests; tray and autostart still need packaged/manual OS acceptance.
- Decision: keep legacy Python GUI and desktop Web entry points until all open release gates pass; keep mobile Web Companion.
