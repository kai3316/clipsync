# Desktop Feature Parity

This is a migration checklist, not proof of completion. Existing behavior remains
the baseline; related defects may be fixed with regression tests. The old Python
GUI and desktop Web entry are retired only after their replacements work.
Mobile Web Companion and shared HTTP/WebSocket resources remain in scope.

## User Scope Exception

On 2026-09-08 the user explicitly requested skipping global hotkeys.
Do not implement or count hotkeys as a remaining migration acceptance gate.
Preserve existing stored bindings for compatibility; this exception does not
remove any other desktop feature or acceptance requirement.

## Evidence Required

For each capability, verify the native UI, closed Rust command, Python use case,
persisted state where applicable, failure behavior, and relevant cross-device
flow. A mock UI test alone does not establish parity. Do not infer completion
from the presence of a page, method or permission.

## Initial Inventory (2026-09-07)

| Capability | Existing source | New desktop state |
| --- | --- | --- |
| History list/search/pagination/pin/delete | `internal/web/api/history.py`, `src/main.py` | Pin/delete/reload verified, and the panel's filter bar is now native too (2026-09-12, "History Filter Bar"): `history.list` takes `kind` and `sort` against closed sets and answers `counts` and `has_history`, so the window carries 全部/文本/图片/文件/链接 chips with live counts, a newest/oldest toggle, and an empty state that names the kind instead of claiming there is no history. The search runs in the use case rather than the repository — a deliberate widening, documented there; a row's own action strip now fades in with the row rather than standing on every row at once (2026-09-12, "History Row Actions and the Header Lines") |
| History copy, rich paste, paste count, batch actions, push text | `internal/web/api/history.py` | Copy implemented with injected-writer tests; native copy, per-row pin/delete, batch pin/favorite/delete, clear and reading a clip's own text back for translation (2026-09-12, `history.text`) are wired, and the row now shows what the legacy row showed — the paste count, the application a clip was copied in, that window's title and the device it synced from (2026-09-12, "A history row's provenance"). "Native copy of a non-text clip remains pending" was stale: `history.copy` writes the row's whole decoded format bag, so copying an image row restores the image. Cross-device paste remains a platform gate |
| Persistent favorites, edit/delete/copy | `internal/web/api/favorites.py` | Shared store and native CRUD/copy page integrated; 53 frontend tests, 14 RPC tests, and native WebView CRUD/persistence smoke pass. "Native export remains pending" was stale (2026-09-12): `FavoritesView.vue` exports every favourite to Markdown in one click and reports the count and the path inline, matching the panel's one-click `exportFavorites` — one format in the window on both sides, though both APIs also accept `text`. Every multi-item gesture -- a drag, a group rename, a group delete -- is one request carrying the whole batch, additive to the legacy body, which is untouched, and each publishes the change to the other surfaces once (2026-09-12, the favourites-the-phone-writes and one-batch-body checkpoints below) |
| Device discovery, pairing, trust revocation, notes, restore, connectivity | `internal/web/api/devices.py`, `src/main.py` | LAN integrated; native pairing/revocation, persistent notes, manual connect/disconnect, archive-on-remove and restore/purge of removed devices are wired through closed RPC and the devices page. Device connectivity probing (`/api/device/test`) and certificate display (`/api/devices/certs`) are wired natively as per-channel ping/pong latency and pinned fingerprints. A chat invite from an unpaired peer drops the pairing code its connection auto-generated (the legacy `discard_pending_pairing` behaviour), so chatting alone never shows as a pairing request; real mDNS/platform acceptance remains pending |
| Capture, filters, rich formats, deduplication, receive/write/ACK | `internal/sync/manager.py`, `src/main.py` | Existing engine connected; bidirectional RPC history-copy flow tested with fake OS clipboard; real cross-machine/platform acceptance pending |
| Pause/resume and timed pause restoration | `internal/web/api/sync_control.py` | Native toggle plus timed pause/resume RPC and UI; persisted deadline is restored after sidecar restart; the footer carries the panel's timed pause as it was (2026-09-12) — 15/30/60-minute presets, a live 已暂停 · 剩余 {minutes} 分钟 countdown with 立即恢复, seeded from `timed_pause_until` and re-read on the panel's 15 s tick; the native tray carries the same presets and countdown, rendered from `settings.get` on its own 15 s tick (2026-09-12) |
| File send/receive, progress, cancel/retry, history, speed test | `internal/web/api/transfer.py`, `src/main.py` | Sidecar LAN runtime owns the real `FileTransferManager`; native UI/RPC cover listing, send, pause/resume, cancel, accept/reject, retry, history deletion and normalized speed-test DTOs, and the panel's "cancel all" is `transfers.cancel_all` (2026-09-12), which reads the live list in the runtime and cancels each row through the same per-row path. A send names its target (2026-09-12): the view offers paired, online devices only and refuses to start without one, and the runtime sends to that peer alone or raises `NOT_CONNECTED` — never widening a named target to a broadcast. Each row reads as the panel's did (2026-09-12): the state and failure labels are the user's language rather than the manager's state words, and the size, live rate and estimate come with them. The panel's 清空历史 button is `transfers.clear_history` (2026-09-12), which reports the count the runtime actually deleted. Its second send button is back too (2026-09-12): a directory is archived by the sidecar and the archive travels the same `transfers.send`, unlinked from the transfer's completion callback, which is the legacy panel's own cleanup. Native cross-device acceptance remains pending |
| Chat invitations, accept/decline, sessions, text, resend, typing, read/mute | `internal/web/api/chat.py` | Native invitations, text, typing, read, resend and persisted mute controls are wired; cross-device acceptance remains pending |
| Chat attachments, accept/decline/cancel, saved-file opening | `internal/web/api/chat.py` | Native send/accept/decline/cancel controls are wired through real ChatManager RPC. "Saved-file opening remains pending" was stale (2026-09-12): the row's 打开 button calls `open_chat_file`, which asks the sidecar for the received file's path (`chat.open_file` -> `internal/system/file_manager.py`) and hands that path to the platform's opener, and `desktop/tests/chat-attachments.test.ts` covers the success, the refusal on an outgoing or unfinished attachment, the refusal on an error, and the failure notice. Cross-device acceptance remains pending |
| Internet pairing and aliases/secrets | `internal/web/api/internetpair.py` | Generate/enter/status/rename/unpair are wired through native RPC; TTL, self-pair rejection, persistence and relay hello publication are covered; two-runtime relay acceptance remains pending |
| Relay delivery status, queue, receipts and retries | `internal/web/api/internetdelivery.py` | Persistent queue, ACK handling, bounded retries and native delivery-status RPC/UI are wired; receipts are routed to the surface that shows them (2026-09-12) — a relayed message's result lands on its own chat bubble and each peer's queue plus newest result lands on its device line, both folded from `relay.delivery.changed` instead of repainting the history — so the routing clause is closed; two-device broker acceptance remains pending |
| Settings and applying changes to live services | `internal/web/api/settings.py` | Native settings page and controlled `settings.get/update` IPC now cover appearance, device, clipboard, notification, sound, animation, source-tracking, translation, internet/relay, the advanced network fields (port, mDNS service type, poll/debounce/timeouts, log level, dedup, data dir) and the 安全 section (encryption toggle, pre-shared password set/clear with the strength rules, factory reset); a settings change made from the phone's web panel now applies live through the companion's `on_settings_change` (including the token, translation-key and factory-reset actions, and a host-relaunch event for restart/reset); the rail that jumps between the cards is grouped into five sections rather than one flat run of fourteen, it now opens one card at a time instead of jumping down a page that shows all fourteen, and it marks each card whose edits are not saved yet; the rail carries eleven cards where it carried fourteen, the AI configuration having left this page for a page of its own and the phone Companion card for the devices page (2026-09-12) |
| Export/import, backup creation/listing/restoration | `internal/web/api/settings.py`, `internal/data/backup.py` | Native backup create/list/restore and JSON/CSV/Markdown history export/import controls are wired; a backup taken on a migrated 1.x install is proven to restore its history through the live repository (`work/migrate-e2e.py`), and the upgrade matrix is covered on this row's sibling; a backup **written by 1.x itself** and restored onto a fresh install is proven too (2026-09-12, "A 1.x Archive on a Fresh Install"): the release tree's own `create_backup` runs over a 1.x install, and the archive's clips, paired device and settings all land through `backups.restore` — `work/migrate-e2e.py` carries the scenario; the 数据备份 card's own list carries each backup's date and size and a per-row 恢复备份 button, the shape the legacy panel's list had, without the file dialog it used to require (2026-09-12) |
| Translation and provider errors | `internal/web/api/translate.py` | Native translate RPC and settings-page text translation are wired through the existing provider/fallback implementation; the provider endpoint (`translate_url`) and key set/clear are editable in the native settings 翻译 service section, and a failed translation surfaces the provider error in the window's error band with a stale-result guard on the edited input; a history row can be translated too (2026-09-12), by reading the clip's own text back (`history.text`) and showing it read-only rather than copying it, which would overwrite the user's clipboard. The `translate_key_set` flag is echoed instead of the key, matching the web panel |
| AI tool profiles, inventory, preview/pull, local edit/trash/open | `internal/web/api/aiconfig.py` | Local file operations and paired-device inventory/preview/pull are wired; copy/overwrite/append controls, confirmation, dirty-editor and stale-response guards have UI tests. Batch pull and the version diff are wired (2026-09-12) — per-row ticks, a page-level select-all that reports "some, not all", a one-click "select the missing ones", and one `pull` request carrying every ticked entry, with ticks pruned to entries the peer still offers; per-row 缺失/对方较新/本机较新 badges and a summary line computed by `desktop/src/lib/aiconfig-diff.ts`, ported from the 1.x helpers. Async inventory refresh was already covered by the `aiconfig_inventory` event watcher. Both lists filter by path (2026-09-12), so the search box is closed too. Folder rows carry a per-folder tick that stands for the files beneath it (2026-09-12), so folder-level selection is closed, and they now start folded (2026-09-12), so a config item is one row until the reader opens it and the count the card reports counts items rather than files.  The wizard counts and confirms in those same units (2026-09-12): a folder is one migration target and a folder's row in the confirmation says how many files are behind it, while what travels is still the files the chosen strategy picked.  The guided migration wizard is wired (2026-09-12): a source device, three strategies with 只补缺失 as the default, and one pull, with the remote rows ticked to show what will move. Real cross-device receipt remains pending |
| Window/tray/menu, notifications, autostart, theme | `src/main.py`, `internal/ui/`, `internal/system/` | Tray icon/show/quit/restore and official autostart/notification plugins are integrated; the tray menu itself now carries the legacy entries (2026-09-12) — device label, sync checkbox, timed pause, 显示 ClipSync, 发送网址, 显示网页二维码, 设置, 导出日志, 检查更新, 关于, 退出 — rendered from `settings.get` and updated in place. Every legacy per-type notification class — incoming chat and transfer, pairing requests, device connect/disconnect and phone uploads — has Rust policy tests and a sidecar event source, the received-file/phone-upload sound plays from the sidecar under `sound_enabled` + `notifications_enabled`, and the in-app notice list is rendered. OS delivery remains unverified. Installed login-start remains pending. Theme parity is split (2026-09-12): the platform font stack is done — see "Theme Parity: Fonts Checkpoint" below — and the one part left is the legacy aurora palette, which is a product decision rather than a gap, because the shell's own token palette is deliberate and the interface was reorganized to a mature-Tauri standard rather than to a CustomTkinter reproduction. The tray's 📶 已连接设备 (N) peers submenu was listed as pending and is wired (2026-09-12): `desktop/src-tauri/src/tray.rs` renders one row per known device from the same `devices.changed` payload the window uses, marks the removed ones out, and carries the `the_peer_rows_come_from_devices_list_and_leave_the_removed_out` case; global hotkeys are explicitly excluded by user request |
| Password unlock and existing identity/history | `internal/security/`, `internal/config/config.py` | Identity decrypt/validate/save, the locked → unlock flow, every config migration, the hashed-peer-row repair and the corrupt-config archive are implemented; a data directory the app refuses to start on is repaired from the window by `recover_data_dir` (2026-09-12), which moves the offending files aside and relaunches the sidecar; a real 1.x install (v1 config, encrypted history) is carried across, rolled back and restored at process level (`work/migrate-e2e.py`) | the repair has not been watched in a running window (native smoke), and no directory written by a real 1.x build has been upgraded |
| Diagnostics, update, installation and platform support | `src/main.py`, build/release workflows | Diagnostics report/actions now share `internal/diagnostics/` with the legacy route and are wired through native RPC and the settings 运行诊断 dialog; the updater's check/status/download/open-folder surface, its 软件更新 panel and the peer-to-peer update exchange are wired. Installation/platform release gates remain pending |
| Mobile browser Companion | `internal/web/server.py`, `routes.py`, `ws.py` | Sidecar owns authenticated legacy server lifecycle; real HTTP page/history/auth/restart tests pass, the settings panel applies live (settings, security, token, translation key, factory reset, remote-access toggle), and every phone panel is wired to the host (device, transfer, diagnostics, update, upload, show-QR, send-URL, close). Static resources are bundled and byte-checked, and the device page is pushed on any snapshot change (the legacy fingerprint poll) instead of waiting for a manual refresh. The history page was the one surface that poll still served and then did not: legacy's `sync_mgr.on_history_change` broadcast every add straight to the panel, and the sidecar's rewiring of that hook to the event journal left the panel hearing only its own routes -- so a phone left open showed a list frozen at load until the bridge was given `history.changed` back (2026-09-12, the checkpoint below). Its update card had the same shape of hole and is closed the same way: legacy broadcast the lifecycle flat, the sidecar published it to the window alone, and the page's progress bar had nothing to move it (2026-09-12, the update-card checkpoint below). Its favourites are live as well: the native window's `favorites.changed` had no panel half, so a favourite added or edited in the window stayed invisible on the phone, and it now arrives as the same snapshot `GET /api/favorites` answers with (2026-09-12, the shared-favourites checkpoint below). The direction back is closed as well: the panel's own favourite routes write that store directly and published nothing, so a favourite changed on a phone reached no other surface, and they now publish `favorites.changed` on the runtime's journal -- what the window invalidates its cached list on, and what the phone bridge turns back into a panel snapshot. Every multi-item gesture saves as one request carrying the whole batch rather than one request per item, so one gesture is one snapshot (2026-09-12, the favourites-the-phone-writes and one-batch-body checkpoints below). Packaged HTTP, phone chat/file acceptance and desktop-route retirement remain pending |

## Stale-row Audit (2026-09-12)

The inventory is a checklist written before most of the shell existed, and a
clause that reads "remains pending" is a claim about the code, not a fact about
it.  Two of them were re-read against the code and the tests today.

- Chat attachments, "saved-file opening remains pending": wired.  The row's 打开
  button is shown only for an incoming attachment whose status is done, and it
  calls `openChatFile`, which is `open_chat_file` in `desktop/src-tauri/src/main.rs`
  — the command asks the sidecar for the path (`chat.open_file`, answered in
  `internal/adapters/sidecar/rpc.py` from `internal/system/file_manager.py`) and
  hands that path to `explorer`/`open`/`xdg-open`.  Only the cross-device
  acceptance part of that row is still open.
- Window/tray, "the tray's 已连接设备 (N) peers submenu remains pending": wired.
  `desktop/src-tauri/src/tray.rs` builds the submenu from the same device list
  the window reads, keeps removed devices out of it, and the tray suite carries
  `the_peer_rows_come_from_devices_list_and_leave_the_removed_out`.

What this audit does *not* establish: no claim above was reached by running the
window.  A submenu that is built correctly and a file that opens through the
right command are code-level facts; the OS-level acceptance they feed is still
the gate the rows name.

## Integration Checkpoint (2026-09-08)

- History count and age settings are exposed through native UI and validated IPC.
  Temporary-database tests verify capture-time count/age pruning, startup age
  pruning, pinned-row preservation and memory/database agreement.
- Current Rust unit tests: 21 passing; Vue TypeScript check and production build
  pass. These are not native OS acceptance tests.
- Latest packaged sidecar smoke verifies 39 static resources and two
  history-only startup/status/history/shutdown/reopen cycles. Later source
  changes require rebuilding; this smoke does not start Companion HTTP.
- No legacy presentation entry has been retired by this checkpoint. Completion
  still requires the entire inventory, not only the capabilities tested above.

## Packaged Companion Checkpoint (2026-09-08)

- Rebuilt the Windows x86_64 sidecar with
  `scripts/build-sidecar.ps1 -SelfTest`. The bundled executable is refreshed.
- Packaged smoke verified all 39 static resources against source bytes, two
  IPC startup/history/shutdown/reopen cycles, and two real authenticated
  Companion HTTP/start/stop/exit/restart cycles in temporary data directories.
- Full `python -m pytest tests/sidecar -q`: 276 passed. This includes the
  source-process packaging smoke tests, not just mocked HTTP behavior.
- Native application filtering now exposes persisted enabled/mode/process
  patterns through validated RPC. Blacklist/whitelist matching and frontend
  round trips are tested.
- AI inventory arrival events now invalidate the selected peer's cached
  inventory view without sending another network refresh. Unrelated peers are
  ignored, and changed-peer/unmounted reads are invalidated.
- These results do not establish phone chat/file acceptance, real two-machine
  synchronization, installed OS integration, or retirement of desktop routes.
  The old presentation entries remain until the full inventory is accepted.

## Real TLS Business Checkpoint (2026-09-08)

`tests/sidecar/test_runtime_integration.py` passes all three configurations:
unencrypted storage, encrypted storage, and password-protected encrypted storage.
Each uses two independent sidecar processes, persisted identities and real TLS
over loopback. Discovery and OS clipboard access remain test substitutes.

The expanded workflow verifies pairing/SAS, bidirectional history copy, a 2 MiB
file offer/accept/byte-identical receive/sender completion, rejection history on
both peers without additional output files, chat invitation/acceptance,
bidirectional text, local unread clearing, remote typing indication, chat
attachment offer/accept/byte-identical receive/completion, and the received-file
lookup RPC used by the native opener. Restart/trust/revocation assertions remain.

The rejection test exposed missing receiver-side history; the shared transfer
engine now records rejection and only rejects pending incoming transfers.
Native transfer snapshots normalize progress to 0-100 and paused status.
The desktop offers accept/reject/resume and protects against stale poll results
and file-picker completion after leaving the page.

These tests do not open a file in an OS application or prove mDNS, real
cross-machine LAN, broker relay, mobile chat/files, installation or tray parity.

## Native Device Management Checkpoint (2026-09-09)

The devices page now covers the full legacy device action set
(`/api/device/connect|disconnect|forget|restore|purge`, `/api/device/note`,
`/api/device/test`, `/api/devices/certs`, `/api/device/pair|unpair|reject`)
through closed RPC:

- `devices.connect`, `devices.disconnect`, `devices.forget`,
  `devices.restore`, `devices.purge` in the sidecar dispatcher, each validating
  a bounded `device_id` and calling only the LAN runtime surface.
- `forget` archives the peer in `Config.removed_peers` (name/address/notes and
  `removed_at`) instead of deleting it, tears down pairing/transport/delivery
  state, and reports `SAVE_FAILED` with a retryable error if persistence fails.
  A repeated forget never overwrites an existing archive.
- `devices.list` appends archived peers with `archived: true` and `removed_at`
  so the page can offer restore/purge; restored peers return unpaired.
- Rust `connect_device`, `disconnect_device`, `forget_device`, `restore_device`
  and `purge_device` commands, matching `build.rs` app-manifest entries and
  `capabilities/main.json` permissions; Vue bridge/store/devices-page controls
  with confirmation dialogs for forget and purge.
- `devices.test` probes every reachable channel to a known peer (`device_ping`
  over LAN when the transport reports the peer connected, and over the relay when
  the peer has a shared secret and is marked paired) and returns per-channel
  `ok`/`latency_ms`/`error` rows bounded by a 4s timeout; a channel whose send
  fails is decided immediately instead of waiting out the timeout. It runs
  outside `_command` so the receive path that records the `device_pong` is not
  blocked by the pairing lock. `devices.certs` lists each known peer's pinned
  certificate fingerprint (`fingerprint`, derived short form, paired flag).
- Rust `test_device`/`device_certs` commands with matching `build.rs`
  app-manifest entries and `capabilities/main.json` permissions; the devices page
  gained a per-device test-connection control that renders each channel's latency
  or failure reason and a certificate dialog showing pinned fingerprints.
- Tests at this checkpoint: full Python suite 1872 passed / 4 skipped,
  `tests/sidecar` 305 passed (49 LAN runtime tests plus RPC/application-runtime
  device command coverage), 102 frontend tests, 21 Rust tests, `vue-tsc` and the
  production build passing. These are mock/in-process results; real mDNS,
  physical cross-machine and OS clipboard acceptance remain pending.

## Native Clipboard Actions Checkpoint (2026-09-09)

The history and favorites pages now cover the legacy clipboard actions
(`/api/history/clear`, `/api/paste` + `/api/paste-rich`, `/api/batch-favorite`,
`/api/favorites/export`) through closed RPC:

- `history.copy` already restored every stored format; it now also bumps the
  entry's paste count, mirroring `/api/paste-rich` (the web UI's per-item copy).
  The increment is best-effort and logged — a counter write must never fail a
  copy — so the response stays `{"copied": true}`.
- `history.clear` removes every history entry and returns `{"cleared": n}`; the
  dispatcher publishes `history.changed` with the count so every client resets
  its list, selection and pagination cursor.
- `favorites.batch_add` adds up to 100 selected history entries in selection
  order, keeps the FULL stored text (the history preview is truncated at ingest
  and would silently cut the clip short), and skips entries that no longer exist
  or cannot be decoded. It publishes exactly one `favorites.changed` -- which the
  phone's page now hears as well, see "The Shared Favourites Checkpoint" below.
- `favorites.export` writes every favourite (stored order) to
  `clipsync-favorites-<timestamp>.md|.txt` in the user's Downloads directory
  (falling back to the data directory), mode 0600, and returns
  `{filepath, filename, count, format}`. It writes a file but changes no stored
  favourite, so it publishes no event.
- The renderer moved to `internal/data/favorites_export.py`, shared by the
  legacy web API and the native use case so the two cannot drift.
- Rust `clear_history`, `batch_favorite_history` and `export_favorites` commands
  with matching `build.rs` app-manifest entries and `capabilities/main.json`
  permissions; the history page gained 加入收藏夹 (batch) and 清空历史 (a
  confirmation dialog reporting the cleared count), and the favorites page
  gained a one-click Markdown export reporting the written path.
- Tests at this checkpoint: full Python suite 1900 passed / 4 skipped,
  `tests/sidecar` 333 passed, 109 frontend tests, 21 Rust tests, `vue-tsc` and
  the production build passing. These are mock/in-process results; real OS
  clipboard acceptance remains pending.

## Native URL, Push and Discovery Checkpoint (2026-09-09)

The last LAN-facing legacy actions now have native replacements:

- `url.send` sends an http(s) URL to one paired peer as a `nav_url` frame; the
  receive path opens only http/https URLs from paired peers in the default
  browser and publishes `url.received`. Both halves refuse every other scheme
  (`file://`, `javascript:`, custom OS handlers) so a peer cannot launch a local
  handler. The send deliberately runs outside `_command`: it is fire-and-forget,
  and holding the pairing lock across it would only stall the receive path.
- `clipboard.push` is the native `/api/push`: it writes the text to the local
  clipboard through the same writer wrapper as sync, suppresses the monitor for
  2s so the capture path does not re-broadcast the same text, records it in
  history, and broadcasts exactly one frame while sync is running. A refused
  clipboard write raises a retryable `CLIPBOARD_WRITE_FAILED` and sends nothing,
  mirroring the legacy fix that stopped reporting success for text the host
  never received. `sent: false` reports the clipboard-only case (sync off)
  without failing the request.
- `discovery.status` / `discovery.set_enabled` / `discovery.set_visible` mirror
  `/api/discovery/toggle` (browse) and `/api/visibility/toggle` (advertise),
  return the resulting state, publish `discovery.changed`, and report a
  retryable `DISCOVERY_TOGGLE_FAILED` when the platform refused the change
  instead of claiming it applied.
- Rust `send_url`, `push_text`, `discovery_status`, `set_discovery_enabled` and
  `set_discovery_visible` commands with matching `build.rs` app-manifest entries
  and `capabilities/main.json` permissions; the devices page gained a per-device
  发送网址 action and a 推送文本 dialog, and the settings page gained the
  启用局域网发现 / 隐藏本机 switches.
- Tests at this checkpoint: full Python suite 1931 passed / 4 skipped (108.12s),
  `tests/sidecar` 364 passed (31.96s), 123 frontend tests, 21 Rust tests,
  `vue-tsc` and the production build passing. These are mock/in-process results;
  real mDNS, physical cross-machine delivery and OS clipboard acceptance remain
  pending.

## Native Logs and Restart Checkpoint (2026-09-09)

- `logs.tail` returns the redacted tail of the application log (default 200
  lines, 1–1000 accepted, at most the last 256 KB of the file) for the settings
  page's log viewer. The tail window and the redaction rules moved to
  `internal/data/logs.py`, shared with the legacy `GET /api/logs` route so the
  two clients cannot drift; the route keeps its `?lines=`/`?tail=` behaviour and
  `routes._redact_sensitive_line` remains as a thin alias for the shared helper.
- The settings page gained a 诊断与维护 section: 查看日志 opens a dialog over the
  same tail with a 200/500/1000 line selector, and 重启应用 asks for
  confirmation and then restarts the host.
- `restart_app` is a Rust-only command: it stops the sidecar (the same graceful
  stop the exit path uses, so the data lock is released) before calling
  `AppHandle::restart`, which replaces the process. It deliberately does not go
  through the sidecar: a process relaunch is host behaviour, and the legacy
  `/api/restart` callback did the same.
- Rust `read_logs` (bounded 1–1000) and `restart_app` commands with matching
  `build.rs` app-manifest entries and `capabilities/main.json` permissions.
- `restart_app` is *not* the recovery path for a dead sidecar: it replaces the
  whole process. That path is the sidecar-restart checkpoint below, and neither
  is it the answer to a data directory the sidecar refuses to start on — that is
  `recover_data_dir` in the data-directory repair checkpoint, because the repair
  has to run outside the sidecar that will not start.
- Tests at this checkpoint: full Python suite 1937 passed / 4 skipped (109.40s),
  `tests/sidecar` 370 passed (32.65s), 125 frontend tests, 22 Rust tests,
  `vue-tsc` and the production build passing. The log viewer was not verified
  against an installed OS package, and the restart path was not exercised on a
  packaged build.

## Native Diagnostics Checkpoint (2026-09-09)

- `diagnostics.report` returns the live health snapshot the legacy
  `/api/diagnostics` route serves: the flat 9-item `checks` list, the v2
  `groups` (system / network / internet / ai_config / chat / transfer /
  filesystem), the summary verdict (`fail` when a critical check is down, else
  `warn`, else `ok`) and the companion/relay/paired counters. The builder moved
  to `internal/diagnostics/report.py` and the legacy `_get_diagnostics` now
  delegates to it, so the web panel and the desktop page cannot disagree about
  the state of the machine; the 26 legacy diagnostics tests pass unchanged.
- `diagnostics.request` performs the two OS repairs the report's hints point at
  (`firewall`, `local_network`) through `internal/diagnostics/actions.py`, the
  same module the legacy `/api/diagnostics/request` route calls.
  `WebServer._open_firewall`/`_open_firewall_elevated` became static so the rule
  can be repaired even when the companion is off.
- The report's strings live in the web panel's catalog, so
  `internal/diagnostics/localize.py` resolves the report's `*_key`/`*_params`
  pairs (and the group/item labels) from the same
  `internal/web/static/locales/*.json` files the panel reads, for the saved
  `config.language`, attaching `*_text` fields. The raw fields stay untouched,
  so the panel keeps resolving for itself.
- The settings page's 诊断与维护 section gained 运行诊断, which opens a dialog
  with the summary verdict, the overview line, the seven grouped cards with
  per-item status, and a 修复防火墙 action on a failed firewall item (plus
  打开本地网络权限 when the flat `permissions` check fails).
- Rust `diagnostics_report` / `diagnostics_request` commands with matching
  `build.rs` app-manifest entries and `capabilities/main.json` permissions;
  `validate_diagnostic_action` refuses anything but the two known actions.
- Tests at this checkpoint: full Python suite 1958 passed / 4 skipped (109.21s),
  `tests/sidecar` 391 passed (33.21s), 130 frontend tests, 23 Rust tests,
  `vue-tsc` and the production build passing. The diagnostics dialog was not
  verified against an installed OS package, and the elevated firewall repair was
  not exercised on a packaged build.

Nothing from this checkpoint's list is still without a native replacement. The
browser/Companion presentation routes (`/api/files`, `/api/upload`,
`/api/download`) are web-UI specific and belong to the preserved Companion;
file open/reveal and the data folder, the QR image, `/api/nav`, `/api/window`
and `/api/dialog-response` all have native replacements — the per-route table in
the Presentation Audit below is the current source.

## Native Update Checkpoint (2026-09-09)

- `update.check` mirrors `/api/update/check`: `{available, latest, current, url}`.
  The release lookup is bounded to ~8s inside the sidecar, so an unreachable
  GitHub answers "no update" instead of pinning the 30s Rust bridge call.
- `update.status` returns `{"state": {...}}` exactly like `/api/update/status`,
  and the same payload is published as the `update.state` event, so the settings
  panel applies either source with one field assignment. (The phone's panel needs
  it unwrapped first -- see "The Phone's Update Card Checkpoint" below.) Phases are
  `idle`/`downloading`/`ready`/`failed` with `fraction`/`downloaded`/`total`/
  `error`/`version`/`path`.
- `update.download` is fire-and-forget (`{ok, started, error}`); progress arrives
  as `update.state` events. `update.open_folder` takes no path from the client —
  the archive stays under `~/Downloads/clipsync-update/` and the sidecar reveals
  it, ready phase only. Installation stays manual, as in the legacy panel.
- The store applies `update.state`/`update.available` in place and returns before
  the debounced refresh, so a live progress bar is not reset by a reload.
- The settings page's 软件更新 section gained 自动检查更新 (persisted through
  `settings.update`, gating the silent 6-hour check on `auto_update_check`),
  立即检查更新, a progress bar with `role="progressbar"`, and a ready card with
  打开所在文件夹. An empty check answer reads as "unknown", not "up to date".
- Four Rust commands with matching `build.rs` app-manifest entries and
  `capabilities/main.json` permissions. The
  `every_handler_command_is_registered_in_build_and_acl` Rust test fails if any of
  the three registration sites drifts, so the closed ACL cannot silently drop one
  (it now parses `generate_handler!` and checks all 95 commands at once).
- Tests at this checkpoint: full Python suite 2000 passed / 4 skipped (108.92s),
  `tests/sidecar` 426 passed (33.15s), 139 frontend tests, 24 Rust tests,
  `vue-tsc` and the production build passing. No packaged or installed build was
  exercised, and no real release download was performed.
- Peer-to-peer update exchange (M2) is now wired too: `update.download`
  broadcasts `update_request` to connected peers before starting the
  release-server download, a paired peer that holds the cached asset answers
  with it (`kind="update"`), and the receive path hands the saved blob to
  `UpdateService.finish_from_peer`. Nothing waits on a peer — the server
  download runs in parallel.
- A peer blob is never trusted: it is checked against the published release
  digest (`fetch_latest_asset_info` + `verify_update_blob(source="p2p")`) on a
  background thread, because that lookup can block for ~30s. No reference
  available → the blob stays in the receive folder and the server download takes
  over without re-asking peers, so an unverifiable blob cannot ping-pong between
  devices; digest mismatch → discarded. The shared transfer layer already
  auto-accepted `kind="update"` and recorded the received kind, so only the
  runtime's serve/receive wiring and the service's peer entry point were new.
- Two-machine acceptance over a real LAN was not performed, and no peer blob was
  checked against a real GitHub release digest.

## Native Advanced Settings Checkpoint (2026-09-09)

- `settings.update` now accepts the advanced and network fields the legacy web
  panel exposes: LAN TCP port, mDNS service type, web history limit, sync
  debounce, clipboard poll interval, receive directory, transfer timeout, max
  reconnect attempts, log level, low-memory mode, retry capture, dedup method
  and the data directory. Bounds mirror `config._FIELD_RANGES` and the HTTP
  API's `_RANGE_LIMITS`, so the closed RPC cannot persist a value that would
  make the LAN runtime unbindable or busy-spin; the shell clamps numbers and
  fills the legacy defaults before sending, and the server guards the same
  bounds.
- The settings page gained a 网络与高级 section with those controls, the legacy
  restart hints (port, service type, data directory, and "some changes take
  effect after a restart"), and the same option sets (four log levels, both
  dedup methods). The Rust `update_settings` payload cap was raised from 24 to
  64 keys: the form submits every control in one call, and the sidecar — not a
  key count — validates names and values.
- Tests at this checkpoint: 24 new `tests/sidecar/test_rpc.py` cases (a round
  trip plus one per rejected bound or type) and a desktop test covering load,
  clamp and save; full Python suite 2056 passed / 4 skipped (109.74s),
  `tests/sidecar` 459 passed (34.39s), desktop vitest 158 passed, `vue-tsc`
  clean, `cargo test --locked` 31 passed. The fields that apply on restart were
  not verified against an installed OS package.
- Still without a native equivalent in the settings surface: the legacy 安全
  section — the encryption toggle, the pre-shared password set/change/clear and
  the factory reset (danger zone). That is the next settings increment.

## Native Security Settings Checkpoint (2026-09-09)

- `settings.update` now accepts `encryption_enabled`, `password` and the
  `clear_password` action, and the sidecar applies them live
  (`SidecarApplication._apply_encryption_change` / `_rewire_encryption`):
  setting a password turns encryption on, mirrors the password into
  `netpair_password`, rewrites `encryption_password_hash` with the new value,
  rebuilds the `EncryptionManager`, re-saves the config (so the private key at
  rest follows the new key) and re-wires the live transport plus the relay's
  netpair channels — the legacy host's `password` / `clear_password` /
  `encryption_enabled` branch. Without the re-wire the UI showed encryption on
  while frames still went out under the startup key state.
- New RPC `app.factory_reset` + native `factory_reset` command: the sidecar
  stops its own services (which releases the history DB and log handles Windows
  needs released), deletes the same user-data set the legacy host deletes
  (`internal/data/reset.py`, kept honest by a drift test against
  `Application._do_factory_reset`) and writes the `factory_reset_pending` /
  `web_fresh_pending` markers; the host clears the webview's own localStorage
  and relaunches. The legacy method is Tk-bound and cannot be imported, so the
  list lives in two places with a tripwire on each side.
- The settings page gained a 安全 section: the encryption toggle (submitted
  only when it changed, so an ordinary save never re-wires live encryption), a
  password + confirmation pair with the legacy strength checklist
  (length ≥ 12, upper/lower/digit/special) that blocks the save until it
  passes, the set/clear state, a clear-password confirmation, and a danger
  zone with a factory-reset confirmation.
- Password strength is validated once, server-side, by the HTTP layer
  (`netpair_passphrase_error`) exactly as the web panel does; the RPC only
  bounds the payload. The history DB keeps the manager it was opened with, as
  in the legacy app — the shell says the history key follows on restart.
- Tests at this checkpoint: 10 `tests/sidecar/test_rpc.py` cases (password
  round trip with live re-wire + hash + at-rest check, clear, toggle, eight
  rejected values, factory reset), a `test_lan_runtime.py` case for
  `apply_encryption`, a `test_sync.py` drift tripwire, and three desktop tests
  (strength gate, toggle/clear, factory-reset confirmation). Full Python suite
  2068 passed / 4 skipped (110.12s), `tests/sidecar` 470 passed (34.25s),
  desktop vitest 161 passed, `vue-tsc` clean, `cargo test --locked` 31 passed.
- Not covered here: a factory reset driven from the phone's web panel still
  does nothing, because the sidecar's companion server is built without
  `on_settings_change` (so no web-driven setting applies live there — the value
  is persisted and takes effect on restart). That companion callback is its own
  increment.

## Companion Settings Checkpoint (2026-09-09)

- `MobileCompanion` now hands the web server the host callbacks the legacy
  entry passed (`on_settings_change`, `on_restart`), and
  `SidecarApplication._on_companion_settings_change` is the host half of
  `POST /api/settings`. A change made from the phone's panel applies live
  instead of only landing in `config.json`: the plain fields go through the same
  `_on_settings_change` the desktop path uses (locale, history limits,
  encryption re-wire, `LanRuntime.apply_settings`), and the action keys the
  settings API cannot perform itself are handled here — `password` /
  `clear_password` / `encryption_enabled` (live encryption re-wire, with the
  password mirrored into `netpair_password` and the stored hash rewritten),
  `set_translate_key` / `clear_translate_key`, `regenerate_web_token` /
  `clear_web_token` (the new token is echoed back so the panel rewrites its own
  URL), and `factory_reset`.
- The sidecar cannot relaunch itself — the Tauri host spawned it and holds its
  stdio — so a web-driven restart or factory reset travels back as the new
  `app.restart_requested` event (with its reason) and the host stops the bridge
  and calls `restart()`, clearing the webview's own storage first for a factory
  reset. The panel's Restart App action (`POST /api/restart`, previously a 503)
  uses the same hand-off. Both are deferred off the HTTP handler thread, the way
  the legacy handler used `root.after`, so the response leaves before the server
  that served it is stopped.
- The panel's remote-access section can now switch off the server answering the
  request: `web_enabled` / `web_port` re-run `configure_companion` on a deferred
  thread. An explicit sync toggle also clears a pending timed pause in memory
  and on disk (`LanRuntime.apply_settings`), mirroring the legacy
  `_clear_pause_state` — otherwise the persisted deadline re-armed the pause on
  the next launch after the user turned sync back on.
- `_on_settings_change` mirrors `updated` into the live config before applying
  it. The HTTP and RPC paths already mutated the config first, but the timed
  pause's auto-resume calls the callback directly with `{"sync_enabled": True}`;
  without the mirror the engines read the still-paused value and nothing
  resumed.
- Tests at this checkpoint: four `tests/sidecar/test_companion.py` cases — the
  adapter forwarding, a real-HTTP round trip (plain fields, password set/clear,
  toggle, translation key, token rotation/clear), the factory-reset and
  `/api/restart` host hand-off, and the remote-access toggle moving the listener
  — plus a `test_lan_runtime.py` case for the cleared pause and one Rust test
  for the restart event. Full Python suite 2073 passed / 4 skipped,
  `tests/sidecar` 475 passed, `cargo test --locked` 32 passed.
## Companion Host-Callback Checkpoint (2026-09-09)

- Every callback the legacy server can reach is now passed in, so no phone panel
  answers `503 not available`: live sightings, hash resolution, reconnect
  progress, pending pairings (the shared SAS code is appended to each row),
  certificates, relay state/broker, overview, diagnostics and the diagnostics
  request, update status/download/open-folder, transfer lists, speed-test state
  and transfer actions, device actions and connection tests, discovery and
  visibility toggles, file open/reveal, uploads, and the three UI requests.
  `LanRuntime` owns the accessors behind them and keeps the legacy shapes: the
  panel's action names map onto the runtime's (`history_delete` → `delete`), an
  unknown action or a failed one is reported rather than raised, an unpair of an
  unknown device is not an error, and a forward is refused unless the target is
  connected.
- A phone upload is recorded (or forwarded to the chosen peer) and now also
  publishes `transfer.web_upload`, which the host renders as the legacy "file
  received" notification under the same `notify_transfer` switch. The body is a
  fixed string in the saved language — the file name never leaves the sidecar,
  matching the host rule that notifications carry no paths or names.
- The phone's QR and send-URL buttons and its close button arrive as
  `app.qr_requested`, `app.send_url_requested` and `app.window_close_requested`.
  The renderer answers the first with the Companion QR dialog, the second with
  the send-URL dialog resolved the way `_pick_peer_then` did — no connected peer
  → the legacy "没有已连接的设备可以发送。" message, exactly one → sent to it
  without a picker, several → a device select inside the dialog — and the Rust
  bridge answers the third by hiding the main window, which is what the legacy
  handler did to the webview (the app kept running).
- Tests at this checkpoint: `tests/sidecar/test_companion.py` 64 cases
  (including a parametrized case asserting all 32 phone-panel routes no longer
  answer `not available`, and the upload/forward/file-open paths) and
  `tests/sidecar/test_lan_runtime.py` 93 cases; five new
  `desktop/tests/app.test.ts` cases plus one Rust test for each host rule
  (`only_the_close_request_event_hides_the_main_window`,
  `phone_uploads_notify_with_the_transfer_switch_and_never_the_name`). Full
  Python suite 2135 passed / 4 skipped, `tests/sidecar` 537 passed,
  `cargo test --locked` 34 passed, desktop vitest 166 passed, `vue-tsc` clean.
- Still open for the Companion: packaged HTTP plus real-phone acceptance
  cannot run in this environment. (The legacy upload sound now plays from the
  sidecar and the device page is pushed on change — see the transfer-sound and
  device-page checkpoints.)

## Native Notification Checkpoint (2026-09-09)

The legacy host notified for four more situations than the shell did; all of
them are now sourced by the sidecar and rendered by the host.

- `LanRuntime._refresh` diffs the connected set into `device.connected` /
  `device.disconnected` carrying the peer name — the same transitions the legacy
  3-second poll loop turned into `notify_device_connect` notices.
- A newly pending pairing publishes `pairing.request` `{device_id, name, code}`.
  The notice is held for `PAIRING_NOTICE_DELAY` (1.2s, the legacy
  `PAIRING_NOTIFY_DEBOUNCE`) so a chat invite arriving on the same connection —
  chatting with an unpaired device must not look like a pairing request —
  suppresses it. De-duplication is keyed on the shared code, which is derived
  from both fingerprints and therefore repeats on every reconnect; it is
  forgotten on the terminal states (paired, rejected, unpaired, forgotten) but
  never on plain expiry, mirroring the legacy `_clear_pairing_notice` call
  sites, so a resolved pairing can notify again while an ignored request cannot
  re-prompt every five minutes.
- The Rust host (`notifications.rs`) notifies for both classes under their
  legacy switches — `notify_pairing` and `notify_device_connect`, still under
  the master `notifications_enabled` — with `{name}` / `{code}` templates in
  `i18n.rs` (`设备 "{name}" 请求配对 — 代码：{code}`,
  `{name} is now connected`, `{name} has disconnected`). Peer names are
  inserted literally, never parsed.
- The shell store records both classes as notices and puts the pairing code in
  the message; `pairing.request` was already listed but had no publisher.
- Tests at this checkpoint: five new `tests/sidecar/test_lan_runtime.py` cases
  (transition, one-shot, debounce window, chat suppression, resolved-vs-expired)
  — `tests/sidecar` 542 passed; one new Rust case
  (`pairing_and_presence_notify_with_their_own_switches`) — `cargo test
  --locked` 35 passed; one new `desktop/tests/store.test.ts` case and one new
  `desktop/tests/app.test.ts` case for the notice stack — 168 passed; full
  Python suite 2140 passed / 4 skipped; `vue-tsc` clean and `npm run build`
  passed.
- The store's notice list is rendered by `desktop/src/components/NoticeStack.vue`
  as a bottom-right polite live region with a dismiss button: one row per
  notice, translated title (mapped from the stable event name, falling back to
  it) and the store's six-second expiry — the legacy webview's toast. This also
  covers the legacy case where notifications are switched off: a pairing code is
  now visible in-app.
- An unpaired peer that invites us to chat no longer also shows as "wants to
  pair" — see the chat-invite checkpoint below.
- Those two switches were honoured by the host but not settable from the window
  until 2026-09-12 — see "Notification Controls and the Redacted-clip Notice".

## Chat-invite Pairing Checkpoint (2026-09-11)

Every unpaired connection auto-generates a shared pairing code, so a peer that
only wanted to chat also surfaced as a pairing request. The legacy host dropped
that pending pairing before answering the invite
(`_chat_handle_incoming_invite` → `discard_pending_pairing`); the runtime now
does the same.

- `LanRuntime._discard_pairing_for_chat` runs on a `chat_invite` frame from an
  unpaired peer: it discards the pending pairing, forgets the pairing-notice
  de-duplication and refreshes. The refresh republishes `devices.changed`, so
  the desktop device card drops the row, and the companion's device-page push
  (below) takes it off the phone within one poll; the invite banner itself is
  untouched — chatting keeps its own consent flow, which is why the invite is
  not auto-accepted as it was in legacy.
- Forgetting the de-duplication matters because the code is derived from both
  fingerprints: a later genuine request carries the same one and would
  otherwise be swallowed as already announced.
- The notice suppression now covers only a *live* conversation (`inviting`,
  `invited`, `active`) rather than any session record, so a declined or closed
  chat no longer silences that device's pairing notices forever.
- Tests at this checkpoint: three new `tests/sidecar/test_lan_runtime.py`
  cases (the invite clears the pending card and code while the invite survives;
  a paired peer's invite leaves its pairing alone; a request after the chat is
  closed notifies again) — `tests/sidecar` 548 passed; full Python suite 2146
  passed / 4 skipped.

## Companion Device-page Push Checkpoint (2026-09-11)

The phone's device page only received a snapshot when a client attached, so a
pairing request that appeared, resolved or expired on the desktop stayed on the
phone until a manual refresh. Legacy instead compared the rendered device
snapshot every few seconds in its status loop and broadcast `devices_updated`
on any change — the helper was ported (`WebSocketManager.devices_fingerprint`)
but nothing called it.

- `WebServer._device_broadcast_loop` now runs for as long as the listener does
  (`DEVICE_BROADCAST_INTERVAL = 3.0`, the legacy cadence): it fingerprints the
  device snapshot and broadcasts only when an already non-empty fingerprint
  changes, so a steady page stays quiet and a client that attaches mid-poll is
  not re-sent its own snapshot.
- The loop is started with the listener and stopped with it: `stop()` signals
  the thread, joins it (bounded by one interval) and only then shuts the WS
  manager down, so a poll can never run against a torn-down socket layer.
- This is what makes the chat-invite pairing cleanup visible on the phone, and
  it covers every other rendered device field — connected/paired transitions,
  reconnect progress, names, addresses, notes and the removed archive.
- Tests at this checkpoint: two new `tests/sidecar/test_companion.py` cases
  (the fingerprint drives one push per change, and the loop ends with the
  listener instead of polling a dead one), assertions added to the real-server
  lifecycle case (thread alive after start, dead after stop, replaced on
  restart) and one new `tests/test_web_api.py` case proving the fingerprint
  follows a discarded pending pairing — `tests/sidecar` 550 passed; full Python
  suite 2149 passed / 4 skipped.

## Relay Delivery Checkpoint (2026-09-11)

Relayed clipboard frames keep the legacy Round-17 delivery promise: 已送达
confirmation and offline retransmission. The persisted queue had been ported,
but not the policy around it — the old runtime retried everything on every
maintenance tick with a budget of 8, re-reported "failed" on every tick for a
row past that budget without ever dropping it, listed only queued rows, and had
no ACK tracking, so an unacknowledged send stayed "sent" forever and a genuinely
failed one was never reported.

- `internal/infrastructure/runtime/relay_delivery.py` (`RelayDelivery`) owns the
  legacy ledger over the persisted queue: a published clipboard frame is `sent`
  with a 15s ACK window; the peer's `relay_ack` settles it `delivered`; an
  elapsed window settles it `failed` — unless the same content already reached
  that peer, which counts as delivered (content-level ack fallback). A publish
  the broker would not take is `queued` and persisted instead.
- Retransmission keeps the legacy triggers and cadence: relay-online, any frame
  from that peer (LAN or relay — the peer just proved reachable), and a 60s
  sweep, with a 2s timeout sweep alongside. Each failed republish costs one of
  the five attempts; the fifth marks the row `failed` and drops it, once.
- Nothing is dropped silently: a queue-cap eviction and an undecodable payload
  are reported as `failed`, a restart re-lists persisted rows as `queued`, a
  fresh install never writes an empty `relay_pending.json`, and unpairing (LAN
  or internet) drops the peer's ledger and payloads so a broken-off device stops
  burning retries.
- A peer that is both relay-enrolled and internet-paired is published to once
  (the netpair channel wins), matching legacy, instead of twice.
- `relay_delivery_status` now reports the merged ledger (`items`, newest first,
  each row carrying status/preview/content_hash/kind/session_id) and
  `delivery_counts` the queued totals, so the native send list and the device
  badge read one source.
- Still open at this checkpoint: chat and file frames did not cross the relay
  yet (LAN-first send with a relay fallback and a relay-safe chunk size) — that
  is closed by the chat-over-relay checkpoint below. The phone panel's live
  pushes (`netpair_peer`, `internet_delivery`) were missing at this checkpoint
  and are closed by the phone live-push checkpoint below.
- Tests at this checkpoint: new `tests/sidecar/test_relay_delivery.py` (20
  cases) — `tests/sidecar` 570 passed; full Python suite 2169 passed /
  4 skipped.

## Bounded Sidecar Restart Checkpoint (2026-09-11)

The host kept its sidecar in a `OnceCell<Result<Arc<Bridge>, BridgeError>>`, so
the first terminal error was cached for the life of the process. A sidecar that
died mid-session left every command failing `SIDECAR_UNAVAILABLE` while the
window said "后台进程不可用，请退出并重新打开应用" — the only recovery was to
quit the app, and the successful-history-initialization case was the only one
that ever cleared it.

- The slot is now tri-state behind one async mutex — `Idle` (never launched, or
  cleared by a manual retry), `Live(Arc<Bridge>)`, `Failed(BridgeError)`. The
  lock spans `Bridge::start`, so concurrent commands join one sidecar and a
  launch failure is recorded once; holding it costs nothing because
  `Bridge::start` only spawns — readiness is awaited per call.
- Every `Live` bridge is supervised. The supervisor awaits readiness and then
  the terminal failure that follows it, which is the whole policy in one line:
  a bridge that *became ready* and then died is a crash worth recovering from,
  while one that never reported ready failed to *launch* — a missing binary, a
  Python that is not installed — and is deliberately not retried, exactly as
  before.
- A crash is relaunched at most `SIDECAR_RESTART_ATTEMPTS` (3) times, waiting
  `attempt * 2s` before each try, so a sidecar that dies the instant it starts
  cannot be respawned in a tight loop. A bridge stopped on purpose is never
  relaunched: `Bridge::stop` sets `stopping` before it fails pending, which
  covers quit, `restart_app` and the web panel's factory reset (all of which
  call `stop` first). A manual retry that installs a different bridge makes a
  waiting supervisor stand down by `Arc::ptr_eq`, so two supervisors can never
  fight over the slot.
- Progress reaches the window as `sidecar:state`:
  `{"state":"restarting","attempt":n,"error":code}` before each try,
  `{"state":"ready","attempt":n}` when one succeeds, and `{"state":"failed"}`
  when the attempts run out. The store treats `restarting` like `failed` — the
  snapshot on screen came from the process that just died, so devices, history
  and the selection are dropped rather than left looking live — but with no
  retry button, because the host is already retrying. On `ready` it clears the
  error and refreshes.
- The exhausted case leaves the failure cached and offers the band's retry,
  which is the new `restart_sidecar` command: it clears the slot and starts a
  fresh sidecar without replacing the window, so a transient sidecar problem
  costs a click instead of an app relaunch. The band's button now calls
  `store.reconnect`, which relaunches only when the error code is one of the
  sidecar-down codes and otherwise just refreshes — a transport hiccup needs no
  new process. `setError` also upgrades a sidecar-down error to retryable: the
  host reports its own failures as non-retryable, which used to leave a launch
  failure with no button at all. The old advice to quit and reopen is gone.
- Registered in all three sites (`generate_handler!`, `build.rs`,
  `capabilities/main.json`), which the existing cross-check test enforces.
- Proof: three `bridge::wait_failure` cases (a running bridge reports nothing
  until it dies, an already-recorded failure resolves at once, a dropped
  readiness channel is terminal), the relaunch-schedule case (bounded and
  strictly increasing), and five `store.test.ts` cases (restarting announces the
  attempt with no retry and drops the stale snapshot, an exhausted relaunch
  becomes a retry that relaunches, a plain transport error refreshes without
  relaunching, a non-retryable sidecar failure still gets the button, `ready`
  recovers the snapshot). `cargo test --locked` 39 passed, `tests/store.test.ts`
  53 passed, `vue-tsc --noEmit` clean. Not proven here: the recovery has not been
  watched in a running window, which is native smoke work.

## Data-directory Repair Checkpoint (2026-09-12)

A damaged data directory used to brick the new app. `bootstrap._load` pre-checks
`config.json` and raises `DATA_INVALID` *before* `config.load()` runs, so
`_archive_corrupt_config` never fired — there was no quarantine, unlike legacy's
archive-and-continue — and the sidebar's only repair, 恢复出厂设置, is a sidecar
RPC call that needs the very process refusing to start. The way out is now a
mode of the sidecar binary rather than a method of the running one.

- `internal/data/recovery.py` re-runs the checks the application itself runs:
  `config_problem` (not an object, unknown/typed-wrong version, an identity field
  of the wrong type, a non-boolean `encryption_enabled`), `identity_problem`
  (`prepare_identity` in memory — never saved — so a repair cannot write a fresh
  identity over the damaged one), `history_problem` (`PRAGMA quick_check`) and
  `records_without_identity` (rows, or a legacy `clipboard_history.json`, that
  carry ciphertext with no identity to unlock them).
- The predicates are *shared*: `bootstrap._load`, `_check_database` and
  `_check_identityless_history` now call them, so "what the app refuses to start
  on" and "what a repair moves aside" cannot drift apart.
- Quarantine renames to `<name>.corrupt-<stamp>` — never deletes, so a mistaken
  repair is one rename away from being undone, and the SQLite `-wal`/`-shm`
  companions travel with the database (a stale log would otherwise replay into
  the fresh one). The connection is closed explicitly rather than by `with`:
  on Windows an open handle would block the rename.
- Blast radius: a quarantined identity takes the history with it when the old
  config said encryption was on, because the at-rest key is derived from the
  fingerprint of the identity being destroyed and those rows can never be read
  again. A config too broken to answer (not JSON at all) keeps the history —
  that says nothing about encryption, and a rename is available later if it
  turns out to be unreadable. Neither locked nor first-run installs are damage:
  a password prompt is not corruption, and an identity the app can create on
  first run is exactly what a repair has to leave room for.
- Running the repair end to end found a second dead end that is **not** closed,
  and deliberately so: encryption on, a password hash stored, no identity. The
  sidecar answers `ready` with `health: "locked"` and rejects every password,
  because the hash is salted with the certificate fingerprint and there is no
  certificate left to derive it from. It looks like damage, and the first cut of
  this work refused it as `DATA_INVALID` and moved it aside — which was wrong,
  and a test caught it. The stored hash is a bare PBKDF2 digest that records no
  marker of which fingerprint salted it, so "salted with the identity now
  destroyed" (a lock nothing opens) is on disk identical to "salted with an
  empty fingerprint" (a working lock the right password opens). No predicate can
  separate them, so refusing would throw away a working password and moving the
  history would discard records the next unlock proves were fine. `inspect()`
  therefore reports nothing for a locked config — the history's key is exactly
  what the password unlocks — and this state is inherited from legacy, where the
  prompt was rejected and the process exited. The lock itself is a
  data-protection property (`factory_reset` refuses while locked, so a stolen
  laptop cannot be wiped without the password) and is left intact.
- `src/sidecar_main.py --recover` writes exactly one frame and exits:
  `{"type":"recovered","items":[{"artifact","reason","files"}]}`, or a `fatal`
  frame (the shape `protocol.rs` already validates) when the pass itself fails.
  Reasons are stable codes, so the window renders zh/en text instead of a
  sidecar English string.
- The new `recover_data_dir` command stops the sidecar while holding the slot
  lock — nothing may reopen the files being moved — runs that child through
  `Bridge::command()` with one bounded frame read and a bounded wait, then
  relaunches the sidecar, so a failed repair still leaves a background process.
  `protocol.rs` accepts a `recovered` frame only when no session exists, which
  is what makes it unreachable mid-session.
- The window offers the repair only for `DATA_INVALID` (nothing else the sidecar
  reports is repaired the same way): a repair button in the error band opens a
  confirmation dialog, and the store's `recoverData` names the moved files in a
  long-lived notice — they are renamed, not deleted, and the user may want them
  back.
- Deliberate divergence from legacy: legacy archived an unreadable config and
  continued silently, inventing a new identity over whatever the old one had
  encrypted. The new app refuses to start and offers one click to move the
  damage aside, so a key is never replaced underneath existing data without the
  user knowing. Where legacy and the new app agree is the lock nobody can open:
  legacy prompted, rejected every password and exited; the new app presents the
  same lock, and the repair declines to guess at it.
- Proof: `tests/test_recovery.py` cases (every finding, the companions, the
  idempotent second run, the locked / first-run / inert-password non-cases, and
  the password whose salt is gone being left alone), three added to
  `test_entrypoint.py` and `tests/sidecar/test_identity.py` (recover mode never
  constructs `SidecarApplication`; a failed pass is a `fatal` frame that leaks no
  path and is pointed at a scratch directory so a patch that stopped applying
  could not touch real data; a password whose identity is gone locks and is not
  repaired), a `protocol.rs` case, three `store.test.ts` cases, and 202 targeted
  Python tests re-run over the refactored startup gates. `cargo test --locked` 40
  passed, `vue-tsc --noEmit` clean, production build clean.
- Process-level proof of the whole loop (`work/recover-e2e.py`, three real
  sidecar runs per scenario against scratch directories): the two repairable ones
  — an unreadable config and an encrypted history no identity can unlock — each
  start as exit 2 `fatal DATA_INVALID`, `--recover` exits 0 and names the files
  it moved, and the next start reports `ready`/`health: ready` with a fresh
  config beside the archived one. The third — a password whose identity is gone —
  starts `locked`, is moved by nothing, and is still `locked` after `--recover`.
- `work/migrate-e2e.py` carries a 1.x install across and back, seven scenarios on
  scratch directories, each a real sidecar process driven over stdio frames. The
  directory is built with the project's own writers — a v1 config (no
  `config_version`, dict-form `peers`, a plaintext `encryption_password`, the
  private key encrypted under it, `ai_config_paths`, `filter_sensitive`) and a
  history whose rows are encrypted with that same identity — so what opens them
  is the app's own code, not a fixture. Verified: the install opens `ready` with
  no password prompt, its three clips decrypt through `history.list`, and the
  saved config is v2 on every axis (version, `peers` as a list carrying the 1.x
  peer and its notes, `filter_enabled_categories` upgraded rather than read as
  "all off", `ai_config_tools` + custom paths replacing the watch list, the
  password replaced by its hash); a v1 key whose password does not open it is
  refused `DATA_INVALID` with the config left **byte-identical** — a failed
  upgrade writes nothing.
- The password survives the migration, checked against the *next* start rather
  than assumed: the migrated config carries no plaintext password, so the second
  start is `locked` and `app.unlock` with the 1.x password is what opens it.
- Rollback, and what it means here: the migration is one-way *in the file*, so a
  user's own copy of the pre-upgrade config is the rollback path — and so is a
  backup, which is the one users take: `backups.create`, `history.clear` (3 rows
  gone), then `backups.restore` puts all three clips back through the live
  repository. Putting the old config back is not a downgrade of the data either:
  the rows are keyed by (fingerprint, password), not by the schema that named
  them, and the same three clips decrypt and migrate again.
- Restoring a quarantined file: the archive is a rename, so putting it back
  returns the app to exactly the state it was repairing — same refusal, same
  bytes — and the next `--recover` moves it again. Proven both ways round: a
  plaintext history the app can read runs *through* a repair (fresh identity, the
  same clips) while the config that was moved stays one rename from being
  restored; and a history whose rows are keyed is never adopted by the fresh
  identity, waits for the second pass to set it aside, and comes back whole —
  same device, same three clips decrypted — once its config is restored.
- The migration matrix found and fixed one real defect on the repair path:
  `records_without_identity` asked whether the *configuration* said encryption
  was on, so a plaintext history (a 1.x install with encryption off) met a fresh
  config — whose default is on — and was refused, then discarded on the next
  repair pass. The predicate now asks the rows: an encrypted field is base64 of
  `PREFIX || nonce || ciphertext` and passes `is_encrypted` after decoding, while
  the user's own text does not, so readable clips are read through a fresh
  identity and keyed ones still wait. Conservative in the one direction that
  matters: a schema the SELECT cannot read, a mixed history, or a legacy JSON
  with ciphertext in it all count as records and are kept back for their key.
- Not proven here: the repair has not been watched in a running window (native
  smoke work), and no upgrade has been run against a data directory that a real
  1.x build wrote — the v1 files are faithful reconstructions, not captured ones.

## Event-flood Backpressure (2026-09-12)

The host hands every sidecar event to the webview's queue and cannot see what the
renderer has drained, so a slow renderer has to be absorbed on the renderer side.
Two `store.test.ts` cases fire 500 events each and pin that: 500
`history.changed` events schedule exactly one snapshot fetch (each event resets a
60 ms timer rather than fetching), and 500 notice-worthy events leave exactly 5
notices and at most 6 live timers (five dismissals plus the pending refresh) — so
neither the list nor the timer set grows without bound. What is still open on this
row is the native smoke that kills the sidecar mid-session and watches the window
recover.

## Legacy Route Inventory (2026-09-12)

The settings/AI/translate/internet-pairing row asked for the legacy route table to
be inventoried rather than assumed. `internal/web/routes.py` serves 82 paths; each
one is now classified, so "nothing is missing" is a checked claim and not a hope.
The five classes below sum to exactly 82 (75 + 2 + 3 + 1 + 1), which is what makes
the inventory checkable rather than approximate: `set(re.findall(r'path == "(/api/[^"]+)"',
routes.py))` is the list, and every path in it appears in a class.

- **Native equivalent (75)** — every route with a sidecar RPC and a shell control:
  history (list/clear/pin/batch-pin/delete/batch-delete/batch-favorite/push/copy/
  item),
  devices (list/certs/note/pair/reject/unpair/connect/disconnect/forget/restore/
  purge/test), discovery and visibility toggles, sync pause/resume, transfers
  (list/send/cancel/pause/resume/retry/accept/reject/history-delete/speed-test),
  favorites (list/add/update/delete/export), settings, backups
  (list/create/restore), export/import, logs, diagnostics
  (report/request), update (check/status/download/open-folder), data open-folder,
  translate, chat (devices/sessions/messages/download/invite/accept/decline/
  file/file-accept/file-decline/file-cancel/text/resend/typing/read/mute/close),
  restart, show-QR, send-url, `/api/status` and `/api/nav` (its local branch —
  see the correction below).
- **Shell-native (2)** — `/api/window` and `/api/dialog-response` are the web
  panel driving its own browser window and answering host dialogs. The native
  window and its own dialogs replace them; no sidecar capability is involved.
- **Corrected: `/api/nav` was misfiled here.** Only its *peer* branch belongs
  with the two above — a `device_id` naming another device calls `on_nav_url`.
  With no `device_id` (or this device's own) the route opens the URL locally
  (`routes.py:682-684`, `webbrowser.open`), which is what legacy's history menu
  used for 在浏览器打开 on a link clip. A WebView has no browser, so replacing the
  window did not replace that branch: it needed a route of its own, now
  `history.open_link` (see "Open a Link Clip in the Browser Checkpoint",
  2026-09-12).
- **Companion presentation (3)** — `/api/files`, `/api/download` and `/api/upload`
  serve the phone's file browser. They belong to the preserved Companion, which
  still serves them; the shell has no equivalent because it has no browser.
- **Companion panel data (1)** — `/api/overview` feeds the phone's overview panel
  through the companion host callback; the shell renders its counts from the lists
  it already holds.
- **Uncalled legacy code (1)** — `/api/paste` (the plain-text variant of
  `/api/paste-rich`) has no caller anywhere in the panel. `history.copy` already
  carries the `plain_text` flag the route would have used
  (`use_cases/history.py::copy`), so the capability is implemented and simply
  unreachable from either UI — recorded rather than wired up, because adding a
  control no legacy user ever had is not parity.
- **Proven since the inventory** — reading a clip's own text back is now pinned
  end to end: `tests/sidecar/test_history_text.py` (ten cases), one
  `test_rpc.py` case through the real dispatcher, the capability in
  `app.status()`, and three `app.test.ts` cases for the row control. The clip's
  own words cross; the stored base64 payloads do not.
- **Migrated by this pass (2)** — `/api/history/item` and `/api/transfer/cancel-all`.
  The first was the one real capability gap the inventory turned up and had been
  read as a mere affordance: the list ships a truncated preview, so a window that
  wants to show or translate a *whole* clip had no way to get the text without
  copying it — which overwrites whatever the user is holding, the one thing a
  translate action must not do. It is now `history.text` → `HistoryUseCase.text`,
  the native counterpart of the legacy route, and the history list has a per-row
  translate control that reads the clip back, shows it read-only in a dialog and
  translates *that* rather than the row's cut-down preview. Two details are
  deliberate and pinned: a clip with no text of its own (an image) reports an
  empty string rather than its preview — the preview of an image is a label like
  `[Image]`, and feeding that to a translator would show the user something that
  is not their clip — and a clip longer than the IPC frame allows is cut at
  100,000 characters *and says so*, because the frame is capped at 1 MiB and a
  silent cut would show a fragment as the whole. The second is the transfers
  panel's "cancel all". the transfers
  panel's "cancel all". The shell had per-row cancel but no sweep. It is now
  `transfers.cancel_all` → `LanRuntime.cancel_all_transfers`, which reads the live
  list inside the runtime (a transfer that arrived since the window's last poll
  would otherwise survive a sweep the user already confirmed) and cancels each row
  through the same per-row path, so peer notification, history and the
  once-guarded callbacks are identical to clicking ✕ on that row. The count comes
  from the sidecar, so "cancelled 2" never counts a row that had already finished.
  Proof: two runtime cases (every active row offered to the per-row cancel, an
  empty id skipped, a `False` from a row that finished mid-sweep not counted;
  nothing active cancels nothing), two RPC cases (no parameters, requires the LAN
  runtime; one `transfers.changed` for the whole sweep rather than one per row),
  the capability advertised in `app.status()` (pinned in
  `test_application_runtime.py`), three `transfers.test.ts` cases (the sweep and
  its reported count, the disabled state with nothing running, a failed sweep
  surfaced rather than swallowed) and `cargo test --locked` 40 passed with the
  handler/build.rs/ACL cross-check.

## Chat and Files Over the Relay Checkpoint (2026-09-11)

The relay previously carried clipboard frames and nothing else: a chat message or
a chat file to a peer with no LAN route was simply dropped, so the legacy
behaviour of falling back to the relay (and chunking to fit it) was missing.

- The chat send closure is LAN-first with a relay fallback —
  `transport.send_to_peer` first, and only when that reports no delivery does
  the frame go out over `_relay_publish_to_peer`. A successful LAN send does
  *not* mirror onto the relay: chat has no content-level dedup, so a mirror
  would deliver the message twice.
- An internet-only peer (not in `get_connected_peers()` and internet-paired)
  has `chunk_size = ChatManager.RELAY_CHUNK_SIZE` (224 KiB) and
  `internet_cap = ChatManager.RELAY_FILE_CAP` (5 MiB) set on that closure, so a
  chat file is chunked to what the relay will actually carry instead of to the
  LAN size.
- Relay frames now enter through the same router LAN frames use, with
  `via_relay=True` selecting the gate that fits an un-certificated transport:
  an unknown device id is refused, `trusted` means internet-paired rather than
  paired, pairing frames are dropped with a warning, and file/speed-test frames
  are LAN-only. Chat and clipboard frames are accepted, and each sends the peer
  a `relay_ack` so the sender's ledger row settles — the same row the chat path
  writes with `kind = msg_type`, its `session_id` and a 40-char preview, which
  is what lets a chat `relay_ack` match anything at all.
- Relayed `file_chunk` frames ride at QoS 1 (every other relay frame stays at
  QoS 0), matching legacy's choice for the one frame type where a lost chunk
  costs a whole retry round.
- `chat_invite` dials first: an idle peer with a known address gets
  `_connect_and_wait` (legacy's 15s window) before the invite, and a peer that
  never comes up is reported as `chat.connect_timeout` — which the native shell
  renders as a notice naming the device.
- Proof: 7 new cases in `tests/sidecar/test_relay_delivery.py` (LAN-first send,
  no LAN mirror, relay-safe chunk size for an internet-only peer, QoS 1 for
  chunks, a relayed invite reaching the chat layer and being acked, a pairing
  frame refused over the relay) and 3 in `tests/sidecar/test_lan_runtime.py`
  (dial-before-invite, internet-only invite, unreachable-peer timeout report);
  `tests/sidecar` 619 passed.

## Phone Internet Panel Checkpoint (2026-09-11)

`/api/internetpair` and `/api/internetdelivery` are *bind-style* branches:
`internal/web/routes.py` hands them the request, and the module delegates to a
host object bound once at startup — which only `src/main.py` ever did. Under the
sidecar both answered 503 ("internet pairing unavailable" / "internet delivery
unavailable"), so the phone's pairing panel and its 待补发 badges were dead
buttons.

- `internal/infrastructure/runtime/internet_panel.py` (`InternetPanelHost`) is
  that host object now, bound by `MobileCompanion` to the runtime. It wears the
  legacy `_netpair_*` / `_delivery_*` surface, returns `(data, status)`, and
  translates application failures into the statuses the panel branches on (400
  for anything the user can fix, 503 for a missing relay link). It must never
  raise: the routes' own error handling is a bare `except Exception` → 500.
- Pairing status parity: a provisional base32 tag key — an `enter` that has not
  been confirmed by the partner's hello — is skipped instead of listed as a
  phantom paired device, the peer name falls back to the paired device's own
  `device_name`, and `online`/`last_seen` follow the 90s window.
- `enter` refuses when the relay is not online (legacy answered 503 rather than
  persisting a secret whose confirmation hello would never leave).
- `rename` validates the alias (must be text, ≤120 chars after stripping) and
  clears on whitespace; unpair pushes `netpair_peer` {status:"unpaired"} so
  sibling tabs drop the row, and no longer leaves the peer's ledger burning
  retries.
- The relay test (`/api/internetpair/test`) probes the staged broker list (or
  the saved one) in parallel via `probe_relay_endpoints` in
  `internal/transport/relay.py`, ranking reachable brokers first, and answers
  the legacy `{"results", "summary"}` envelope.
- The delivery route re-keys the runtime ledger's `items` to the panel's
  `sends`, so `pending` and the send rows come from the same merged ledger the
  native UI reads.
- Still open (closed by the next checkpoint): nothing emitted the WS pushes
  these routes pair with — `netpair_peer` on a confirmed pair or an unpair,
  `internet_delivery` on a ledger transition. Legacy broadcast them from the
  Application; the sidecar's event journal had no subscriber forwarding to
  `ws_manager`, so those phone cards refreshed only on their REST pull.
- Tests at this checkpoint: 9 cases in `tests/sidecar/test_internet_panel.py`
  (adapter contract, status shape, delivery key mapping, probe ranking/crash
  containment) and 8 in `tests/sidecar/test_companion.py` (route behaviour plus
  an every-route wiring guard).

### Phone Live Push Checkpoint (2026-09-11)

The "still open" gap above is closed. `EventJournal.subscribe` gives the
sidecar a listener hook that runs *after* a publish and outside the journal
lock — a slow or raising listener can neither stall a publisher nor corrupt
the replay buffer (a raising one is logged and skipped) — and
`internal/infrastructure/runtime/phone_push.py` (`PhonePush`) is its one
subscriber: it maps the runtime's event names onto the WS contract
`static/js/ws.js` already handles, built through the shared `WebSocketManager`
helpers so the wire shapes stay owned in one place.

- Forwarded: `transfer.progress` / `transfer.complete`, `chat.message` /
  `chat.sessions.changed` / `chat.file.progress` / `chat.file.done`,
  `relay.state.changed` → `relay_state` (with the broker),
  `relay.delivery.changed` → `internet_delivery`, `netpair.peer.changed` →
  `netpair_peer`, `pairing.request` → `pairing_request` (re-keyed to
  `peer_id`/`peer_name`, carrying the SAS the card shows beside the code),
  `pairing.resolved` → `pairing_resolved`, `device.connection_rejected` →
  `connect_rejected`, `device.connection_unreachable` → `connect_unreachable`,
  `aiconfig.file` → `aiconfig_file`.
- Deliberately not forwarded: `devices.changed` and `history.changed` — the web
  server pushes those itself, so forwarding would render the same snapshot
  twice.
- New runtime emissions behind those mappings: a confirmed `netpair_hello`
  reports its peer paired and online; an unpair publishes the severed pair from
  the runtime (so a desktop-side unpair reaches sibling tabs, not only the REST
  caller); every pairing resolution (local confirm, peer confirm/reject, the
  device-action reject, and the chat-invite supply chain that suppresses a
  prompt) publishes `pairing.resolved` with its status; and a connect click
  with no address to dial publishes `device.connection_unreachable` named from
  the config, then the pairing repo, then empty — the push truncates to 12
  chars exactly as legacy did.
- The bridge is attached and detached with the companion's `start`/`stop`, so a
  stopped server never leaves the runtime publishing into a closed manager.
- Tests: 14 cases in `tests/sidecar/test_phone_push.py` (payload shapes,
  non-forwarded names, a raising socket never reaching the publisher, idempotent
  detach), runtime cases for the unreachable push, pairing resolution and the
  chat-file wiring, plus a cross-process assertion in
  `tests/sidecar/test_runtime_integration.py` that both peers' chat-file
  progress and outcome actually arrive at the IPC client.
- Still open: packaged HTTP and phone acceptance on a real handset.

## Native Transfer Sound Checkpoint (2026-09-09)

The legacy host played a short system sound when a file arrived
(`src/main.py::_play_transfer_sound`, called from `_on_file_received` and
`_on_web_upload`). That cue is now sourced by the sidecar, which owns the
legacy dependency-free `play_sound` implementation and the live config, while
the notification itself stays the host's job.

- `LanRuntime._play_transfer_sound` requires `sound_enabled` (the pure sound
  toggle since v1.0.84) **and** the master `notifications_enabled`; neither
  gates whether a notification appears. A missing or failing audio tool is
  logged and swallowed so a receive can never break on it.
- It fires at both legacy call sites: a received clipboard file (the
  non-`update` branch of `_on_file_received`, so a peer-sent update blob stays
  silent) and a phone upload (`record_web_upload`, before the recording step —
  legacy beeped even when recording the upload failed).
- Chat file transfers keep their own path and stay silent, as in legacy.
- Tests at this checkpoint: three new `tests/sidecar/test_lan_runtime.py`
  cases (end-to-end receive beeps once while an update blob does not; both
  switches silence it and a failing audio tool never raises; a phone upload
  beeps on success and on a recording failure) — `tests/sidecar` 545 passed;
  full Python suite 2143 passed / 4 skipped.

## Native i18n Checkpoint (2026-09-09)

- The shell renders through `desktop/src/i18n`: Chinese source strings are the
  keys (`t("设置")`), `zh-CN` renders them verbatim, `en` looks up
  `desktop/src/i18n/en.ts`, an unknown locale falls back to Chinese, and
  `{name}` placeholders are interpolated (unknown ones stay visible).
  `desktop/tests/i18n.test.ts` is the gate for the whole shell: every `t()` key
  must exist in the table, no Chinese may be left outside a `t()` call, and no
  table entry may be unused.
- The first-run picker and the settings 语言 select mirror the legacy onboarding.
  The picker shows once per session and only while the read-only
  `settings.language_chosen` is false; it is deliberately bilingual (it must be
  readable before a language exists) and persists the pick through
  `settings.update`. The sidecar flips `language_chosen` on a `language` update,
  so dismissing the picker leaves the flag false and it returns on the next
  launch — the legacy wizard behaves the same way.
- The native surfaces read the same saved language in Rust
  (`desktop/src-tauri/src/i18n.rs`): tray labels, notification bodies and the
  `rfd` file-dialog filters. The locale comes from the sidecar's `settings.get`
  rather than the renderer, so a change made from the web UI — which never
  reaches a Tauri command — is applied too, through the `settings.changed`
  event. No new Tauri command or ACL entry was needed (95 commands / 97
  permissions, unchanged).
- Tests at this checkpoint: full Python suite 2032 passed / 4 skipped (110.49s),
  `tests/sidecar` 435 passed (34.04s), desktop vitest 157 passed, `vue-tsc`
  clean, `cargo test --locked` 31 passed. The picker and the native relabelling
  were not exercised against an installed OS package.

## Presentation Audit (2026-09-09)

The API-module scan behind the Initial Inventory was not exhaustive. This audit
covers the rest of the legacy presentation surface — route registration, desktop
callbacks, tray/menu actions, onboarding, security dialogs, native shortcuts and
packaged static resources — and names, for each entry, its consumer and the test
that replaces it. It also separates a presentation entry that can be retired
from backend code the preserved Companion still needs.

### Route registration

`internal/web/routes.py::dispatch` is one if/elif chain holding 85 route
literals; `internal/web/api/*` supplies the payloads. Three consumers:

1. the desktop webview dashboard (`static/index.html`, 31 scripts: 22
   components, 8 `js/` modules and Vue),
2. the phone Companion (`static/mobile.html`, self-contained),
3. the desktop host itself — callbacks a browser page cannot perform.

The native adapter is `internal/adapters/sidecar/rpc.py` (plus
`internal/adapters/sidecar/favorites.py`): 80 RPC methods advertised through
`app.status.capabilities`, reached from 91 `#[tauri::command]`s whose closed ACL
is `capabilities/main.json` (83 / 95 / 97 after gaps 1-5 closed on 2026-09-09).
The capability-by-capability mapping is the Initial Inventory table above; the
presentation-only entries are below.

**Desktop-host callbacks** — the panel POSTs these so the *host process* acts:

| Legacy route | Consumer | Native replacement | Replacement test |
| --- | --- | --- | --- |
| `/api/window` | panel title-bar close | Tauri window API; the phone's close request arrives as `app.window_close_requested` and hides the main window | `bridge::tests::only_the_close_request_event_hides_the_main_window`, `desktop` vitest |
| `/api/nav` | panel link click (local browser or peer) | `url.send` for a peer; the local branch is `history.open_link`, which opens the text of the row the window names (`app.open_link` covers the app's *own* links only) | `tests/sidecar/test_lan_runtime.py`, `tests/sidecar/test_history_open_link.py` |
| `/api/show_qr` | panel 手机 Companion | `companion.status` access URL + `companion.qr` image; the phone's button arrives as `app.qr_requested` and opens the same dialog | `tests/sidecar/test_companion.py`, `test_application_runtime.py::test_companion_qr_encodes_the_token_url_and_reports_missing_states`, `desktop/tests/app.test.ts` |
| `/api/send_url` | panel 发送网址 | native 发送网址 dialog → `url.send`; the phone's button arrives as `app.send_url_requested` and opens the same dialog with the connected peers to pick from | `tests/sidecar/test_lan_runtime.py`, `desktop/tests/app.test.ts` |
| `/api/dialog-response` | panel answers a server-pushed dialog | in-process Vue dialogs; nothing is pushed | `desktop/src/App.vue` vitest |
| `/api/discovery/toggle`, `/api/visibility/toggle` | panel switches | `discovery.set_enabled`, `discovery.set_visible` | `test_application_runtime.py` |
| `/api/sync/pause`, `/api/sync/resume` | panel pause/resume | `sync.pause`, `sync.resume` | `tests/sidecar/test_rpc.py` |
| `/api/restart` | panel restart | `restart-app` | Rust `every_handler_command_is_registered_in_build_and_acl` |
| `/api/logs` | panel log viewer | `logs.tail` | `test_application_runtime.py::test_logs_tail_reads_the_shared_log_and_redacts_local_secrets` |
| `/api/overview` | panel overview cards | header status line + devices page (no single payload) | `desktop` vitest |
| `/api/update/check|status|download|open-folder` | panel 软件更新 | `update.check/status/download/open_folder` | `tests/sidecar/test_update_service.py` |
| `/api/export`, `/api/import` | panel history export/import | `export-history`, `import-history` | Rust command tests |
| `/api/backup`, `/api/backups`, `/api/restore` | panel backup section | `create-backup`, `list-backups`, `restore-backup` | Rust command tests |
| `/api/file/open`, `/api/file/reveal`, `/api/data/open-folder` | panel received-file open/reveal and data-folder button | `transfers.action` `open`/`reveal` (path resolved server-side from history) + `data.open_folder` (`data|backups` enum); `update.open_folder` still covers the update archive | `test_lan_runtime.py::test_transfer_open_launches_the_received_file`, `::test_transfer_reveal_opens_the_containing_folder`, `test_application_runtime.py::test_open_data_folder_reveals_the_app_folder` |
| `/api/upload`, `/api/download`, `/api/files` | browser file pickers | `choose-file` + `send-file` for one pick, `choose-files` + `send-files` for the legacy picker's multi-select (several files leave as one archive and one transfer, 2026-09-12); received files land in the sidecar receive dir | Rust command tests, `test_lan_runtime.py`, `tests/test_archive.py` |

**Companion presentation routes** — phone-only, no native counterpart required.
The whole `static/` tree is served to `mobile.html` and `index.html`; the phone
uses `/api/chat/*`, `/api/transfer*`, `/api/history*`, `/api/favorites`,
`/api/files`, `/api/upload`, `/api/download`, `/api/pin`, `/api/delete`,
`/api/push`, `/api/paste-rich`, `/api/settings`, `/api/status` and
`/api/diagnostics*`. These stay: they are the preserved Companion, not a desktop
entry to retire.

### Tray menu (`internal/ui/systray.py`)

14 entries; all have a native replacement, and since 2026-09-12 twelve of them
are *in* the native tray menu (`desktop/src-tauri/src/tray.rs`), which renders
them from `settings.get` so a change made anywhere — this window, the web UI,
the phone, or the tray itself — is picked up.

| Tray entry | Native replacement |
| --- | --- |
| device name label | the tray's own 设备：{name} line, and the devices page / header status line |
| 同步 checkbox | the tray's 同步 checkbox → `sync.set_enabled`, and the footer's toggle |
| ⏸ 暂停 15/30/60 分钟 | the tray's 定时暂停同步 submenu → `sync.pause`, and the footer's own presets |
| 立即恢复 | the tray's 立即恢复, and the footer's, → `sync.resume` |
| 🖥 显示面板 | the tray's 显示 ClipSync, and the native window itself |
| 📤 发送链接 | the tray's 发送链接 → the window's own send-URL dialog; `url.send` |
| 📱 显示网页二维码 | the tray's 显示网页二维码 (greyed while the companion is off — the menu API has no per-item visibility), or 显示二维码 on the settings page → native QR dialog (`companion.qr`) |
| 📶 已连接设备 (N) | `devices.list`; **no tray submenu yet** — the only entry still without a tray counterpart |
| ⚙ 设置 | the tray's 设置… → the native settings page |
| 📝 导出日志 | the tray's 导出日志… → the window's logs dialog: native save dialog + `logs.export` |
| 🔍 检查更新 | the tray's 检查更新 → the window's update check, `update.check` |
| ℹ️ 关于 | 关于 ClipSync in the tray menu, or 关于 on the settings page → native About dialog |
| ⏻ 退出 | the tray's 退出 ClipSync → `quit-app` |

### Onboarding (`internal/ui/onboarding.py`)

One step: a bilingual first-run language picker writing `config.language`.
`settings.get`/`settings.update` carry `language` plus the read-only
`language_chosen` the picker is gated on, and `SidecarApplication.__init__`/
`_on_settings_change` apply the language to the sidecar's i18n.

Native equivalent (gap 5, closed 2026-09-09): the shell has its own layer
(`desktop/src/i18n`, Chinese source strings with an `en` table and a catalog
coverage test), a settings-page 语言 select, and a first-run picker that shows
once per session while `language_chosen` is false and is bilingual by design.
The native surfaces — tray labels, notification bodies and file-dialog filters —
follow the same saved language through `desktop/src-tauri/src/i18n.rs`, refreshed
by `settings.get` at startup and by the `settings.changed` event, so a change
made from the web UI is picked up too. The tray re-renders on a real locale
change only: rewriting fourteen labels under a menu that is already in the right
language means touching a menu the user may have open, for nothing.

### Security dialogs

`internal/ui/dialogs.py` (info/warning/error/yes-no/ask-string) and
`internal/web/dialog.py` (server-pushed modal + toast for webview mode). Native
replacements: the unlock screen (`app.unlock`), the pairing code dialog, and the
in-process confirmations in `App.vue` (revoke / forget / purge / restore /
clear-history / rotate-token / delete). The `dialog-response` round trip has no
native counterpart because native dialogs are in-process.

### Native shortcuts

`internal/system/hotkey.py` registers the 12 `config.DEFAULT_HOTKEYS`
(`paste_1..9`, `paste_plain`, `toggle_monitor`, `show_window`). Global hotkeys
are excluded from migration acceptance by the user scope exception above; no
in-window shortcut map exists in either shell.

### Packaged static resources

`internal/web/static/` is 39 files: `index.html`, `mobile.html`, `sw.js`,
`vendor/vue.global.prod.js`, `css/` (3), `js/` (8), `components/` (22),
`locales/` (2). All of it is Companion/legacy-webview presentation; the Tauri
frontend (`desktop/src`, 4 Vue files and 2 TS stores) references none of it.
`clipsync-sidecar.spec` bundles the tree into the sidecar binary, and
`scripts/smoke_sidecar.py::verify_companion_resources` byte-checks every packaged
file against the source tree.

### Presentation gaps this audit found

Five legacy presentation entries had no native replacement when this audit was
written. None is an API capability; each is presentation-only. Items are struck
off here as their increment lands; all five closed on 2026-09-09.

1. ~~Open/reveal a **received file** (`/api/file/open`, `/api/file/reveal`) and
   open the **data folder** (`/api/data/open-folder`)~~ — **closed 2026-09-09**.
   `transfers.action` accepts `open`/`reveal`, with the path resolved server-side
   from transfer history and only received files eligible; `data.open_folder`
   takes a `data|backups` enum. The history row offers both, and the settings page
   has 打开数据文件夹 / 打开备份文件夹.
2. ~~Export the log file (**save-as**), the tray's 📝 导出日志~~ — **closed
   2026-09-09**. The logs dialog's 导出日志… button opens a native save dialog
   (`rfd`) and calls `logs.export`, which copies the raw log — not the redacted
   tail — to the chosen path and reports the byte count.
3. ~~An **About** dialog (version, links) — the tray's ℹ️ 关于~~ — **closed
   2026-09-09**. The tray gained 关于 ClipSync, which focuses the window and
   emits `ui:about`; the settings page has 关于. Both open a native dialog showing
   the sidecar's version plus 项目主页 / 最新版本, which call `app.open_link` with a
   closed target key (`homepage` / `releases`) — the WebView never supplies a URL.
4. ~~The Companion **QR image** (the tray's 📱 显示网页二维码 and `/api/show_qr`)~~
   — **closed 2026-09-09**. `companion.qr` encodes the same
   `mobile.html?token=…` address the legacy dialog did (shared
   `internal/system/qr.py`, the same qrcode + Pillow stack) as a 220 px PNG data
   URL, and reports `COMPANION_NOT_RUNNING` / `QR_UNAVAILABLE` instead of
   failing. The tray's 显示网页二维码 and the settings page's 显示二维码 open it.
5. ~~The first-run **language picker** and a language control in settings~~ —
   **closed 2026-09-09**. The shell renders through `desktop/src/i18n`
   (`t()`/`setLocale`, Chinese source strings, an `en` table and catalog
   coverage tests), the first-run picker is gated on the read-only
   `settings.language_chosen` and is bilingual by design, and the settings page
   has a 语言 select. The native surfaces read the same saved language in Rust
   (`desktop/src-tauri/src/i18n.rs`), so tray labels, notification bodies and
   file-dialog filters follow a change made anywhere, including the web UI.

## Native Integration Checkpoint

On 2026-09-07, `node desktop/scripts/smoke-native.mjs` exited successfully using
an isolated synthetic data directory and the current Windows debug host. Verified:
native IPC, history display, pin/reload, persistent deletion, shell denial, exit,
and opening the same data with a new sidecar process. The smoke also verifies
native favorites CRUD and persistence. The launcher variant passed with the
development port occupied and used an alternate port. This did not test real OS
clipboard copying, LAN pairing, sync, file transfer, or macOS/Linux.

## CI Gate Checkpoint (2026-09-12)

Both workflows now run green on this machine, and the `lint` job had not been.
`ruff check .` reported **46 findings across 10 files** — none of them on a line
this migration added, which is the point: the tree had drifted out of its own
configured contract (`line-length = 100`, `select = [E, F, W, I, N, UP, B, SIM]`)
and the gate had been red long enough that nobody was reading it. Nine were
mechanical and 34 were long lines; three needed a decision (`B023`, a predicate
closing over a loop variable, now bound as a default argument; `SIM102`, a nested
`if` merged into one short-circuiting condition; `N818`, `SmokeFailure` renamed
`SmokeError`). Four files were changed beyond the reflow the lint required —
`rpc.py` (two identical relay-broker predicates became one `_broker_list`, so a
limit written twice cannot drift), `lan.py` and `file_transfer.py` (short-circuit
order preserved rather than evaluated eagerly) and `test_runtime_integration.py`
— and no observable behaviour changed. Every module touched has its own suite and
all of them ran, including `test_runtime_integration.py`, which covers the two
rewrites with real semantics (the transfer-status map and the bound predicate).

`pip install ruff` was unpinned in `test.yml`, so it is now `ruff~=0.15.0`: an
unpinned install picks up each release, and a release that adds a rule reddens
this gate with nothing in the repository having changed.

- Every command CI runs, as CI runs it: `ruff check .` clean over the tree
  (ruff 0.15.12); `pytest tests/ -q` **2263 passed, 4 skipped**; `npm run test:web`
  35 passed; and for `desktop.yml`: `pytest tests/sidecar -q` 640 passed,
  `npm run build` clean (249.15 kB JS), `npm test` 184 passed,
  `cargo test --locked` 40 passed.
- Both workflow files parse: `test.yml` jobs `lint`/`web`/`test`, `desktop.yml`
  job `desktop` across `windows-latest` / `macos-latest` / `ubuntu-24.04`.
- Still unproven, and it needs a runner this machine is not: the actual remote
  results, the three-OS matrix, and the log-collection step that only runs on
  failure.

## Notification Controls and the Redacted-clip Notice (2026-09-12)

The route inventory could not have found either of these: it counted routes, and
every settings switch saves through the same `POST /api/settings`. So this pass
inventoried the **controls** instead — what the native settings page renders,
against what the sidecar accepts and what the native host reads.

- **Readable but not settable.** `notify_pairing` and `notify_device_connect`
  were already read by the Rust host on every notification
  (`notifications.rs:58`, `:62`, `:66`, each under the master at `:43`; its own
  tests pin the gating) and were already settable from the phone panel — but the
  sidecar's `settings.update` validates a *closed* key set and did not list
  either, so the window could load a value it had no way to change. Both keys
  are accepted now and the settings page renders a checkbox each, disabled with
  the master exactly as `notify_transfer`'s already was. Greying them does not
  erase them: the host keeps honouring the per-type flags, so a save that reset
  them would silently undo a choice the user never re-opened.
- **`notify_sync` is deliberately not ported.** Nothing under `desktop/` reads
  it, so a switch would be a control that changes nothing. The key stays in the
  config and the legacy route (the web panel keeps its own control); the sidecar
  test asserts `settings.update` still refuses it, so this is a pinned decision
  rather than an oversight.
- **The redaction message reached nobody.** `sync.redacted` has been published
  since the content filter was wired (`lan.py:2308`, right after
  `filter_content`) and was consumed by nothing in `desktop/src`: a clip whose
  sensitive values were replaced left the device silently, where legacy said
  剪贴板同步 / 敏感内容未同步. It now renders in the notice stack, with the title
  mapped in `NoticeStack.vue` beside the catalog.

Gating it on `notify_sync` was the one design question, and legacy settles it:
the redaction site calls `_web_toast` **ungated** whenever web clients exist and
only falls back to the gated `_notify("notify_sync", ...)` in CTK mode. In the
mode being replaced the message was not per-type gated — and the shell's notice
stack is its home anyway, since the store has no settings access and the Rust
switches gate only OS notifications.

Recorded and not built: legacy's pairing *terminal-state* notifications
(completed / peer-rejected / expired). The shell has `pairing.resolved` events
with no notice, and the common completion path is already covered by the
device-connected notice; a second message for the same moment would be noise.
If the release-gate audit wants the rejected and expired cases they belong with
the pairing UI acceptance work.  (Half of it was built the same day — the
*confirm click's* own outcome, which is a different thing from an event: see
"A Confirm That Did Not Pair" below.  The events are still unnotified.)

Evidence: `ruff check .` clean; `pytest tests/sidecar/test_rpc.py` **168 passed**
(one new case: the two switches round-trip and survive a later unrelated update,
`notify_sync` still refused); `vue-tsc --noEmit` clean; `vitest run
tests/store.test.ts tests/app.test.ts tests/i18n.test.ts` **132 passed** (one new
store case for the redacted-clip notice, one new app case for the two checkboxes
loading, saving and greying with the master).

## Certificate-change Recovery Checkpoint (2026-09-12)

The event sweep that turned up the redacted-clip notice also turned up
`device.security_alert`: published by the runtime, consumed by nothing in the
shell — and behind it a legacy flow with no replacement at all. Legacy prompted
(`_on_security_alert` → Trust again / Keep unpaired, throttled 30s per peer) and
its two answers either re-pinned the certificate or unpaired the device. The
native app did neither: it showed nothing, and because the transport parks a peer
whose pin it refused — "it sits 'paired but unreachable' with no user-visible
signal", and only a changed pin lifts the block — the device stayed dark until
the user re-paired it from both sides.

- The runtime now holds the certificate the peer presented and publishes
  `device.security_alert` with the device name and whether there is one to pin,
  throttled per peer as legacy was (a reconnect loop prompts once, with the
  certificate the user was shown).
- `devices.retrust` replaces the pin (`update_peer_certificate`, or `add_peer`
  when the pairing manager never knew the peer), persists, and dials — re-pinning
  is what lets the transport resume reconnecting, and the dial asks for it now
  rather than waiting for the health tick. "Keep unpaired" is the existing
  `pairing.unpair`: that is what the answer means, and the runtime voids a pending
  prompt with any unpair. Forget clears the prompt and its throttle entry.
- The window asks with a modal carrying those two answers, and deliberately does
  not let it be dismissed (no close button, Escape prevented): under a pin-block a
  dismissed prompt is a silent device. It is not a trap, though — it waits the
  same two minutes legacy's web confirm dialog waited (`_web_dialog_async(
  "confirm", …, timeout=120)`), then leaves an in-app notice carrying the prompt's
  own wording, which is legacy's ungated `_notify_info` toast in webview mode. The
  peer's next connection raises the prompt again.
- Named divergence: legacy's startup case — a changed certificate found while
  loading peers, with no certificate available yet — cannot arise here, because
  nothing raises `CertificateChangedError` at load. The "no certificate" alert
  therefore comes only from the dial side, which knows just the pin it refused;
  retrusting it would pin a certificate nobody has seen, so the runtime refuses
  with `VALIDATION_ERROR` and the dialog disables that button and says the device
  must connect again. The accept side alerts with the real certificate when it
  does, which re-enables the button in place.

Evidence: five new `tests/sidecar/test_lan_runtime.py` cases; `devices.retrust`
added to `test_rpc.py`'s argument/`LAN runtime` table and
`test_application_runtime.py`'s runtime-member table; three new `store.test.ts`
cases (the prompt survives an answer the runtime refused, it gives up after two
minutes and leaves a notice, and an answered prompt leaves none) and two new
`app.test.ts` cases (the dialog re-pins on request; the no-certificate alert
disables the button). `cargo test --locked` 40 passed, including the
handler↔`build.rs`↔ACL cross-check that covers the new command.

## Failed-connect Feedback Checkpoint (2026-09-12)

The last pair the event sweep found reaching only the phone:
`device.connection_rejected` (`lan.py:1378` — a peer answered our dial with its
rejection marker, i.e. it removed us) and `device.connection_unreachable`
(`lan.py:1577` — there was nowhere to dial: not advertising, no saved address).
The runtime publishes them *for a UI's benefit*, and says why in a comment: the
connect route "only answers `{accepted: false}`, which every UI renders as a bare
failure. The reason rides out as an event instead." That consumer was
`phone_push.py`; the native window had none.

What the window showed instead was a band reading 操作未完成，请刷新后重试 — advice
that cannot work, since refreshing does not make an absent device appear. Legacy
did not blur the two: `connect_rejected` / `connect_unreachable` toasts name the
cause and what to check, and `device-card.js` refuses to read success out of the
connect route at all ("`{ok:true}` only means the handshake was *initiated* …
claiming success here is the lie that left a rejected connect looking like a
silent no-op").

- Both events now reach the notice stack, with legacy's wording: "«name» refused
  the connection — that device may have removed you" and "Can't find «name» — make
  sure it is running ClipSync and on the same network". The name comes from the
  event when it carries one, then the snapshot, then the short id — the same
  fallback `phone_push.py` applies to the unnamed case.
- `action()` takes an optional list of result fields whose `false` is already
  reported by an event, and only the connect route passes it, so
  `accepted: false` no longer paints the misleading band. The tolerance is
  deliberately narrow: `start_pairing` returns that same shape for the same
  nowhere-to-dial condition and has no event to explain it, so it still reports as
  a failed action, and a test pins that.
- Not ported: legacy's optimistic "connecting…" toast. The shell renders the
  runtime's own `connecting` state on the card and converges from the snapshot, so
  a toast for that moment would be the claim legacy's own comment warns against.
- No runtime change was needed: the events, payloads and phone rendering were
  already correct. The gap was a missing reader on this side.

Evidence: three new `store.test.ts` cases (both reasons reach the notice stack
with the right name, a tolerated `accepted: false` leaves no error band, and
`start_pairing`'s false still reports) and one new `app.test.ts` case that clicks
Connect on an offline device and reads the notice; `vue-tsc --noEmit` clean;
`vitest run tests/store.test.ts tests/app.test.ts tests/i18n.test.ts` 142 passed.

## Open a Link Clip in the Browser Checkpoint (2026-09-12)

The route inventory's single wrong classification hid a capability. `/api/nav`
was filed as "shell-native — the web panel driving its own browser window" beside
`/api/window` and `/api/dialog-response`. That is true only of its *peer* branch:
`routes.py:678-681` takes a `device_id` and calls `on_nav_url` for another
device. With no `device_id`, or with this device's own, the route falls through
to `routes.py:682-684` — `import webbrowser; webbrowser.open(url)` — and that
local branch is what legacy's history context menu used for **在浏览器打开** on a
row whose `linkUrl` it had scraped out of the clip. Replacing the browser window
with a WebView therefore did not replace this: a WebView has no browser, so the
affordance simply disappeared with the panel.

- The opener lives in `internal.system.about` beside the app's own-link opener,
  as a pair: `is_openable_url` (the gate) and `open_web_url` (gate, then
  `webbrowser.open`, answering `(ok, url-or-code)`, never raising). It keeps the
  posture the panel's `is_safe_nav_url` established — the OS is the last gate, so
  a non-web scheme never reaches it — and is strictly narrower than legacy's
  check: `http`/`https` only, a host required, no whitespace or control character
  anywhere (a newline inside a clip would otherwise smuggle a second argument
  past whatever the platform opener hands the shell), and a 2048-character cap,
  because a URL someone copied is short and a paragraph that merely begins with
  `http://` is not something to launch a browser with.
- **The URL comes from the row, never from the caller.** The window sends an
  `entry_id`; the sidecar reads that row's own text and opens that. The rule that
  nothing a WebView supplies may reach the browser survives intact while the user
  gets the affordance back, and the RPC test asserts that an extra `url`
  parameter is rejected rather than ignored.
- The sidecar reads the *whole* clip, not the preview legacy scraped. `open_link`
  decodes the row, takes its visible text (`text_of`, so an HTML clip opens the
  link inside its markup rather than the markup), trims surrounding whitespace
  and hands that to the opener. A clip reading "see https://example.com for
  details" opens nothing (`INVALID_URL`) — that text is a sentence, and legacy's
  preview-scrape would have launched a browser from it. An undecodable row is
  `DATA_INVALID`; a link the OS refuses is `OPEN_FAILED`, kept distinct from "this
  row is not a link" so the user is told which of the two happened.
- Not routed through the store's `action()` helper: an open mutates nothing, so
  there is no snapshot to re-read and no refresh to gate. The action reports its
  own outcome and the window announces the URL it opened
  (已在浏览器打开：{url}, legacy's wording). The row control appears only for a row
  that *looks* like a link (`content_type === "URL"` or an `http(s)` preview) —
  and the sidecar decides again from the full text before anything opens.

Evidence: `history.open_link` advertised in the runtime's capability list, pinned
by `test_application_runtime.py`, which also proves the injected opener is the
app's own (`about.webbrowser.open`) rather than anything a caller supplied; 21
new cases in `tests/sidecar/test_history_open_link.py`, whose default recorder
applies the real gate so "nothing opened" — not "the use case said no" — is what
they assert; one `tests/sidecar/test_rpc.py` case covering the route's parameter
surface; `open_history_link` registered in `generate_handler!`, `build.rs` and
`capabilities/main.json` (the Rust cross-check test enumerates all three);
`cargo test --locked` 40 passed; `pytest tests/sidecar -q` 671 passed;
`ruff check .` clean; `vue-tsc --noEmit` clean; `npm test` 198 passed;
`npm run build` succeeded.

## History Row Provenance Checkpoint (2026-09-12)

Closing the route inventory closed the *routes*; reading the panel's own row
component found what a route list cannot. `history-item.js` renders four things
the native row did not, and `history.list` did not even ship them:

| Legacy row showed | Source | Native state before |
| --- | --- | --- |
| `source_name` — the device a clip synced from | route's `_source_label`: a peer's name, `📱 Web` for a phone push, this device's name for a clip captured here | absent from the DTO |
| `source_app` — the application it was copied in | source tracking | absent from the DTO |
| `source_title` — that window's title | source tracking | absent from the DTO |
| `paste_count`, as a badge above zero | `increment_paste` on every copy | absent from the DTO |
| a *named* kind | `typeLabel`: 文本/图片/文件/HTML/RTF | the wire name, so a Chinese user read `IMAGE_PNG` |

None of it is derivable on this side: the list is the only payload the window
sees, a row's text does not say which machine it came from, and the copy that
bumps the count is the very action the window is asking about.

- The DTO grew the four fields. `source_title` is capped at
  `SOURCE_TITLE_LIMIT` (200): the title is chosen by whichever application was in
  front, and every row of every page would otherwise carry it in full. Legacy
  shipped it whole and clamped it in CSS — the same display, with a bounded
  payload.
- The name is resolved through an **injected callable**, not a captured map, so a
  peer paired after startup is named without a restart; `source_name(...)` in the
  use-case module holds the rule (legacy's own, kept because the window shows the
  same rows): a known id resolves to its name, an unknown id to the id it holds,
  and an empty source — how a clip captured on this device is stamped — to this
  device's name rather than a blank or an "unknown".
- Presentation mirrors the panel and stops where it stopped: the count badge
  appears only above zero, and the kind is named for the kinds the panel named
  (TEXT / URL / LINK / FILE / IMAGE / IMAGE_PNG / IMAGE_EMF), with anything else —
  HTML, RTF — left as the format's own name in either language, which is what
  `typeLabel` did with them.
- Not ported: the row's leading emoji type icon. The native row's leading column
  is the selection checkbox and the kind is named in words beside it.

Evidence: 11 new `tests/sidecar/test_history_provenance.py` cases — the four
fields, the absent case, the label being asked about the row rather than the
caller, the title cap, the label rule parametrized over all five kinds of source,
and two that run the app itself (the three kinds of source, and a peer paired
after startup named without a restart); `test_rpc.py`'s row-key guard updated to
the new set with its intent intact (no stored payload, no filesystem path); the
`HistoryItem`/`HistoryPage` test fixtures folded into `historyRow()`/`historyPage()`
helpers in `desktop/tests/app.test.ts`, since the DTO's next field would
otherwise edit every literal there; two new `app.test.ts` cases (the badges and
the named kind; the singular "1 次粘贴" English needs spelled out). The
catalog-coverage tests caught two real mistakes before they shipped — a
`t("HTML")` whose key no catalog had, and a CJK string in a comment — both fixed
by mirroring the panel rather than inventing a label.
`pytest tests/ -q` 2305 passed / 4 skipped; `ruff check .` clean;
`vue-tsc --noEmit` clean; `npm test` 200 passed; `npm run build` succeeded.

## History Filter Bar Checkpoint (2026-09-12)

The row above was closed and the *toolbar* was still missing. The native history
page had a search box, a refresh button and a clear button; the panel above it
had five kind chips with live counts and a newest/oldest toggle, so the only way
to find an image among a thousand text clips in the window was to scroll.

| Legacy control | Semantics | Native state before |
| --- | --- | --- |
| 全部 / 文本 / 图片 / 文件 / 链接 chips, each with a count | `history-panel.js::filterCounts` + `store.js::filteredHistory()`: 文本 = TEXT/HTML/RTF, 图片 = IMAGE/IMAGE_EMF, 文件 = FILE, 链接 = preview matches `^https?://` | no chips at all |
| the counts follow the current search | counts computed over the search-filtered list | — |
| a chip with nothing behind it is disabled | unless it is the active one | — |
| newest/oldest toggle | `store.historySort`, newest first by default | no toggle: newest only |
| search | preview / `source_name` / `content_type` | same three, in `repository.search` |

- **Membership**, legacy's rules with two deliberate differences, each for
  something this build has that the panel did not. `IMAGE_PNG` — the name this
  build stores a bitmap under, where the panel knew only `IMAGE` and older rows
  still say so — belongs to 图片. And a row *typed* `URL` counts as a link
  whichever way its text reads, which is what keeps the 链接 chip and the row's
  own 在浏览器打开 control in agreement; legacy matched the text alone, so a link
  clip with an empty preview sat outside its own chip.
- **Pinned first in both orders**, as two stable passes rather than one compound
  key: a compound key reverses the pinned half along with the timestamps and
  sinks pinned rows to the bottom of one direction. A pinned row the user has to
  scroll for is not pinned.
- **Search widened on purpose**, and the one place this searches more than the
  panel did: `source_app` and `source_title` are searched here, because a badge
  the user can read and not search for is worse than no badge. That is also why
  the search left `repository.search` for the use case — the repository cannot
  see those two columns — at the cost of one full read per page, bounded by
  retention.
- **Empty states**, with the panel's precedence: a search that matches nothing
  is reported as a search, and only otherwise does an active chip name its kind
  (还没有图片) instead of claiming there is no history. The chip row itself stays
  on screen either way — `has_history`, computed before the search and the
  filter — because a bar that disappears on a typo takes the way back out with
  it.
- **Protocol**: `history.list` gained `kind` and `sort` (refused against the
  closed `KINDS`/`SORTS` sets rather than treated as "all", which would show a
  filter that is not on), and the response gained `kind`, `sort`, `counts` and
  `has_history`. The Tauri command validates the same two sets so a window bug is
  answered locally. No handler, ACL or capability entry changed: the command
  already existed and only its arguments grew.

Evidence: 13 new `tests/sidecar/test_history_filters.py` cases (the membership
rules parametrized over all five chips, a chip returning only its kind with a
filtered `total`, the counts as what each chip would show and as following the
search rather than the chip, the search reaching all five displayed fields, both
orders, pinned first in both, the empty answer, `has_history` true while a search
matches nothing, and the closed sets); the RPC parameter table extended with the
two closed sets; five new `app.test.ts` cases (the chips and their badges, the
disabled empty one, the click reaching the bridge with the filter, the toggle
reversing it, the chips surviving an empty search, and the kind-named empty
state). `pytest tests/ -q` 2332 passed / 4 skipped; `ruff check .` clean;
`vue-tsc --noEmit` clean; `npm test` 204 passed; `npm run build` succeeded;
`cargo check` clean.

## Row Preview Checkpoint (2026-09-12)

The panel's history row opened a floating card on hover
(`preview-popover.js`, driven by `history-item.js::onMouseEnter`). The native row
had nothing, and its preview is clamped to three CSS lines — so the rest of a
pasted log or a long article was reachable only by copying it somewhere or
handing it to the translator.

| Panel card | Native card |
| --- | --- |
| Card under the pointer, flipped above the cursor near the viewport edge | Card anchored below its row, capped at `42vh`, scrollable |
| `pointer-events: none` — never blocks the list, never scrollable | Inside the hovered row, so the pointer can move onto it and read to the end |
| Repeated the kind, the time, the source and the pin state | Text only — the row already shows all four |
| Hover only | Hover *and* focus, because a hover-only card is unreachable by keyboard |

- The card is `aria-hidden="true"` because the clamp is **purely visual**: the
  row's own paragraph carries the whole preview in the DOM, so assistive tech
  already reads all of it, and the card exists only for the eyes that could not.
- A short preview gets a card too, as in the panel. The alternative — measuring
  whether the row is really clipped — turns on `scrollHeight` exceeding
  `clientHeight` for a `-webkit-line-clamp` box, which a test can only stub, so
  it would have been a rule no test could tell the truth about.
- No DTO, RPC or Rust change: the list already shipped the full preview.

Also corrected here: the favorites row above claimed native export was pending.
`FavoritesView.vue` exports every favourite to Markdown in one click and reports
the count and the path inline — the same one-click export the panel offered.

Evidence: one new `app.test.ts` case (no card before hover; the full
900-character preview uncut for the hovered row and only that row; `aria-hidden`;
closing on leave; `focusin` opening it for a keyboard user). `vue-tsc --noEmit`
clean; `npm test` 205 passed; `npm run build` succeeded.

## File-transfer Target Checkpoint (2026-09-12)

The native transfers view sent a file to **every** connected peer. Legacy could
not: its upload dialog required a target (`transfer.select_target`), the multipart
field carried it as `target_device`, and the host refused a target that was not
connected before handing the upload to `send_to_peer` (`src/main.py::
_on_web_forward_file`, `transfer-panel.js::uploadFile`).

| | Legacy panel | Native view |
| --- | --- | --- |
| Target | Required; a toast when none was picked | Required; the button is disabled until one is chosen, and the picker says why when none is online |
| Candidates | Connected devices | Paired **and** `online` — the same rule the send-URL dialog uses |
| Refusal | Host returns "peer offline" for an unconnected `target_device` | Runtime raises `NOT_CONNECTED`; the transfer is never started |
| No target named | Not reachable from the page | Still a broadcast — kept for callers with no page to ask on |

- Nothing is preselected: a file is the one thing on this page the user cannot
  take back, so the machine it goes to is named rather than assumed.
- The refusal is the runtime's, not the button's. `LanRuntime.send_file(path,
  device_id="")` resolves the id like every other device argument and never falls
  back to a broadcast — sending to everyone when one machine was asked for is the
  one outcome the user could not have meant, and `send_to_peer` would have
  dropped the frames silently and left a transfer that looked alive.

Evidence: three new `test_lan_runtime.py` cases, one new `test_rpc.py` rejection
(`device_id` must be a string), four `transfers.test.ts` cases. `ruff check .`
clean; the three touched sidecar suites 297 passed; `vue-tsc --noEmit` clean;
`npm test` 208 passed; `cargo check` clean.

## Transfer Row Checkpoint (2026-09-12)

The panel's transfers page named each file, its size, its live rate, what was
left of it, and — for a finished row — *why* it failed. The native rows showed
the transfer manager's own state word (`finalizing`, `awaiting_ack`, a raw
`error_disk`), so the window read as a protocol log.

| | Legacy panel | Native view (before) | Native view (now) |
| --- | --- | --- | --- |
| Running row | 已暂停 / 正在写入对方设备… / 等待对方接受…, else the percentage | `paused`, `finalizing`, `awaiting_ack` | The panel's labels; a plain sending/receiving row says nothing extra |
| Size, rate, estimate | `size`, `speed`, `eta` | none (the DTO dropped them) | All three: the runtime publishes `speed`/`eta`, the shell formats them |
| Failed row | The specific cause — 接收设备磁盘空间不足, 对方已拒绝接收, 传输超时, … | The raw reason string, beside a bare 未完成 | The same cause list; the reason slot replaces 未完成 |
| Unknown reason | `error_internal` rendered as the raw key | — | 传输失败 |
| Unnamed row | The raw key `transfer.unknown_file` | `Unknown file` in an English UI | 未知文件 |

- The ETA stays server-side, as it was: it needs the time units of the user's
  language. Its formatter is now one function that the legacy web API imports
  under its old name, so the phone and the window cannot drift.
- `desktop/src/i18n/format.ts` is the shell's `ClipsyncFormat` — `size`, `speed`,
  `dateTime` in one place, for the reason the legacy module exists.
- Four legacy catalog keys the panel called do not exist (`status_completed`,
  `status_cancelled`, `status_failed`, `unknown_file`, plus `err_internal` in
  the reason map), so the panel showed those raw key strings. The native shell
  has real strings for all of them rather than reproducing the defect.

Evidence: two new `test_lan_runtime.py` cases, two new `transfers.test.ts` cases.
`ruff check .` clean; 547 passed across the touched sidecar and web-API files
plus `test_file_transfer.py`; `vue-tsc --noEmit` clean; `npm test` 210 passed;
`npm run build` succeeded.

## Timed Pause Checkpoint (2026-09-12)

The dashboard's quick controls offered a *timed* pause; the native footer offered
one fixed duration and no way to see or end a pause.

| | Legacy dashboard | Native footer (before) | Native footer (now) |
| --- | --- | --- | --- |
| Arm a pause | 定时暂停同步 + 15/30/60 分钟 | 暂停 30 分钟, only | 定时暂停同步 + 15 分钟 / 30 分钟 / 1 小时, while sync runs |
| While paused | ⏸ 已暂停 · 剩余 {minutes} 分钟 + 立即恢复 | 恢复同步 (indistinguishable from sync turned off) | The same countdown and 立即恢复 |
| Deadline source | `settingsCache.timed_pause_until`, mirrored from the pause response | — | `timed_pause_until` from the settings payload, mirrored from `sync.pause`'s `until` |
| Counting down | 15 s `_pauseTickTimer`, refetching the settings when it passes | — | The same 15 s tick over the same one field |
| Transition notice | `overview.paused_toast` / `overview.resumed` | — | The same two strings, in the footer's notice line |

- A pause armed anywhere still counts down here: the deadline is the runtime's
  (`timed_pause_until` is a read-only field of the settings DTO the shell already
  fetches), so one armed from the tray or restored from disk on a restart is
  shown with the minutes it actually has left.
- The 15 s tick re-reads that one field only, never the whole payload: the same
  `settings` object backs the settings form, and a wholesale merge would discard
  a half-edited one.
- The resume stays the host's: this window calls `resume_sync` only when the user
  asks, and the `sync.state.changed` event it publishes brings the status back.
- Two deliberate differences: the presets are offered only while sync is
  *running*, because in this footer they share a row with the plain 恢复同步 a
  user needs after toggling sync off beside them (the dashboard had no such
  button and relied on its toggle); and the notices render in the footer rather
  than as a floating toast — the footer is the surface that is always on screen,
  and the per-page notice lines it replaces meant a notice from the transfers,
  favorites or chat pages was not shown at all.

Evidence: five new `app.test.ts` cases (presets while running; `pauseSync(30)`
then ⏸ 已暂停 · 剩余 30 分钟; 立即恢复 clearing it; a sync-off footer keeping
恢复同步; and a fake-clock case showing exactly one extra settings read once the
deadline passes). `vue-tsc --noEmit` clean; `npm test` 215 passed; `npm run
build` succeeded. No Rust change — `pause_sync` / `resume_sync` were already
registered and allowed, and no command's arguments changed.

## Tray Menu Checkpoint (2026-09-12)

The tray had four entries (显示 / 二维码 / 关于 / 退出). The legacy tray had the
whole menu, and the entries it was missing are the ones a user whose window is
*closed* needs — which is when the tray is the only surface there is.

| | Legacy tray | Native tray (before) | Native tray (now) |
| --- | --- | --- | --- |
| Entries | the full menu: labels, the sync checkbox, the pause items, the peers submenu and seven actions | 显示 / 网页二维码 / 关于 / 退出 | 12 entries in four groups — 设备：{name} and a header label, the 同步 checkbox, the 定时暂停同步 submenu, and 显示 ClipSync / 发送链接 / 显示网页二维码 / 设置… / 导出日志… / 检查更新 / 关于 ClipSync / 退出 ClipSync |
| State shown | device name, sync checkbox, paused countdown | — | 设备：{name}, 同步 checkbox, and the pause countdown, all from `settings.get` |
| Built | whole menu rebuilt on every change (`WM_UPDATE_MENU` on Windows to reach the tray thread) | one menu, never rebuilt | one menu, updated **in place** — labels, checkbox and the pause submenu's children change; the shape does not |
| Pause | ⏸ 暂停 15/30/60 分钟 / 立即恢复 | — | 定时暂停同步 submenu → `sync.pause {15|30|60}` / `sync.resume` |
| Window actions | 🖥 显示面板, 📤 发送链接, ⚙ 设置, 📝 导出日志, 🔍 检查更新, ℹ️ 关于 | 显示, 二维码, 关于 | All six, plus 退出: the tray focuses the window and asks it to open **its own** surface (`ui:settings`, `ui:export-logs`, `ui:check-update`, `ui:send-url`) — the same handlers the buttons call, not a second implementation |
| Entries carry an emoji | every one (🖥 📤 📱 ⚙ 📝 🔍 ℹ️ ⏻ ⏸ 📶) | none | none — the four entries that shipped first set that, and prefixing only the new ones would be worse than prefixing none |
| English | — | — | The tray follows the same saved `language`, re-rendering from the stored state so the `{name}` and `{minutes}` lines are rebuilt in the new language too |

- The tray reads its state from the sidecar, never from the renderer: the window
  may be closed, and the phone or the web UI can be what changed something. Three
  events trigger a refresh — `settings.changed`, `sync.state.changed` (what
  `sync.pause`, `sync.resume` and the sidecar's own auto-resume publish) and
  `app.status.changed` (`sync.set_enabled`'s) — and a burst collapses into one
  refresh plus at most one trailing one. Discovery and history churn constantly
  and change nothing the tray currently draws, so they are not triggers.
- The 15 s tick re-renders the pause submenu alone, with no round trip, and only
  when the minute it shows actually changed.
- Tray actions call the bridge directly (there is no `invoke` to carry a
  rejection) and refresh the state afterwards either way: a rejected toggle
  leaves the checkbox where the sidecar says it is and a rejected pause leaves
  the submenu on its presets. The control snapping back is the report, which is
  also how the legacy tray showed it.
- **Six legacy catalog keys did not exist** (`tray.sync`, `tray.pause_for`,
  `tray.pause_15m`/`_30m`/`_1h`, `tray.paused_left`, `tray.resume_now`), so the
  legacy tray rendered those literal key strings. The native labels use the
  words this window and the dashboard already use for the same actions
  (`desktop/src-tauri/src/i18n.rs`, `Tray`), and a test asserts the `{name}` and
  `{minutes}` placeholders survive.
- Three deliberate differences: a deadline that has already passed is *not*
  paused (the legacy kept showing the paused line with `max(1, 0)` minutes until
  something else cleared it) — the footer treats it as over, and the two native
  surfaces now agree; 显示网页二维码 is greyed rather than hidden while the
  companion is off, because the menu API has no per-item visibility and an entry
  that cannot work should read as one rather than come and go under the user's
  cursor; and the tray has no menu rebuild path at all, so it cannot hit the
  legacy `DestroyMenu`/`TrackPopupMenuEx` race.
- Still pending *as of this entry*: 📶 已连接设备 (N). The legacy submenu listed
  every *discovered* device with a state suffix, not only connected ones, and
  its count came from that same wider list; it needs a device-state vocabulary
  before it is worth building, so it is the next tray increment rather than part
  of this one.  (Built since — `device_lines` in `desktop/src-tauri/src/tray.rs`
  feeds the submenu and the state vocabulary is `DeviceState`; see the Stale-row
  Audit above.  The sentence is left standing because this entry is a log of what
  was true when it was written, not a description of the tree now.)

Evidence: three new `tray.rs` unit tests (an unarmed or passed deadline is not a
pause; whole minutes rounded up with a floor of one; the fill leaves braces in a
device name alone), one new `bridge.rs` test pinning the three refresh events
against five that must not refresh, and one new `app.test.ts` case driving
设置 / 导出日志 / 检查更新 / 发送链接 through the tray. The shell's
`onShowAbout`/`onShowQr` collapsed into one `onMenuAction` over six `ui:` events
(one listener each, because Tauri matches event names exactly). `cargo test` 45
passed; `cargo build` warning-free; `vue-tsc --noEmit` clean; `npm test` 216
passed; `npm run build` succeeded.

## Discovered-device Pairing Status Checkpoint (2026-09-12)

The user reported, on the devices page: "设备页面显示的未连接的设备有问题，一开始
怎么就会有一个勾和叉？而且勾还不能点。" — a device that is merely *disconnected*
drew the confirm(勾)/reject(叉) pair from the moment it appeared, and the confirm
could not be clicked.  Both halves have one cause.

- `PairingManager.get_pairing_status` answered `PAIRING_STATUS_PENDING` for any
  peer it held no lifecycle record for.  `LanRuntime._refresh` copies that answer
  straight onto the `devices()` row (`status = "paired" if paired else
  self.pairing.get_pairing_status(pid)`), so *every* unpaired device on the LAN
  was described as mid-pairing — the shell's `pairingPending` and the tray's
  `is_pairing` accept that value, so both native surfaces drew a pairing in
  flight for a device nobody had asked to pair with.
- The confirm was dead because the row's `pairing_code` comes from a real
  `_pending_pairings` entry, and a discovered device has none: the button renders
  `:disabled="busy || !device.pairing_code || …"`.  A real `pending` always
  carries a code, so the gate itself was right; only the status feeding it was
  not.
- Fix: `PAIRING_STATUS_NONE = ""` is now the default, and the empty string is
  what the removed-device rows already used for the field.  The three readers
  that can only run after a real pairing recorded a status are unchanged in
  behaviour; the tray's `classify` needed no edit because the source is now
  honest, not because the tray was exempt.
- Verified before changing the default, not assumed: all four `get_pairing_status`
  call sites were read.  `internal/infrastructure/runtime/lan.py`'s DTO was the
  only one that could read a peer with no record — `_confirm_pairing`, the
  `pairing.resolved` publish in the frame router, and `src/main.py`'s `_on_pair`
  are each reached only after a real handshake wrote the status they read.  The
  router also gates `mark_peer_confirmed` on `pid in pending`, so a recorded
  status always implies a local request carrying a code.

Evidence: `tests/test_pairing.py` 47 passed; `tests/sidecar/test_lan_runtime.py`
124 passed, including the new `test_devices_report_a_pairing_only_while_one_is_in_flight`
(a discovered device, then a connected-but-unpaired one, then a real local
request that does put a pairing in flight) and
`test_unknown_confirm_cannot_create_trust`, whose expectation now reads
`PAIRING_STATUS_NONE` because a refused confirm leaves no pairing behind it.

What this does *not* establish: the shell half is a code-level fact.  The desktop
suite drives `pairingPending` with fixture rows, so no one has yet looked at the
devices page of a running window — the on-screen result is the gate, not this
entry.

## Known Test Flakes (2026-09-12)

- `tests/sidecar/test_runtime_integration.py::test_real_rpc_pairing_bidirectional_copy_and_restart_trust[True-shared-test-secret]`
  fails intermittently — roughly one run in three — at
  `right.wait("devices.list", lambda d: not d["items"][0]["paired"])`, after the
  left process unpairs.  It is **not** caused by the pairing-status fix above:
  the file's `get_pairing_status` default was temporarily put back and the test
  failed the same way, at the same line, on the second attempt.
- What the failure shows: the right process still reported the peer as
  `online` and `paired` when the 10 s deadline passed.  A right-hand
  `pairing_unpair` handler would have called `transport.forget_peer`, which drops
  the peer from the connected set — so the frame never reached it, rather than
  arriving and being ignored.
- Why it can be lost: an unpair notice is best-effort by design.  `_end_pairing`
  calls `_send_pairing(pid, "pairing_unpair")` and discards the result, and
  `_deferred` — the retry queue — is written only for `pairing_confirm`
  (`lan.py:2301`) and its retry gate is `pid in pending or is_peer_paired(pid)`,
  which an unpair has just made false.  A half-open socket or a link that is down
  at that instant therefore loses the notice, and the far side keeps showing the
  device as paired until something else forces a re-check.  The legacy
  `src/main.py` `_on_unpair` is the same shape ("best effort" send, then forget),
  so this is shared pre-existing semantics, not a migration gap.
- Left standing rather than papered over: the deadline was not raised and the
  wait was not loosened.  The local side's trust *is* revoked (it forgets the
  peer and stops relaying to it); it is the far side's label that lags.  Making
  it converge is a design change in trust propagation — the peer would have to
  learn from its next refused connection or from a reconciliation on reconnect —
  and is not part of a UI increment.

## A 1.x Archive on a Fresh Install Checkpoint (2026-09-12)

The inventory's export/import row read "export/import of a backup made by legacy
remains pending".  That is a claim about a file, so it was settled by producing
the file rather than by reading the writer.

- `work/migrate-e2e.py` gains `a_legacy_archive_opens_in_the_new_app`: it checks
  out the 1.x release commit (`7a68981`) with `git archive`, builds a 1.x install
  in a sandbox, runs **that tree's own** `create_backup` over it, and restores the
  archive it produced onto a fresh install through `backups.restore`.
- What the run establishes: the archive's members are `config.json` and
  `history.json`; the restore reports `config: True`, 3 clips and 0 favorites with
  no `errors` entry (a 1.x install with no favorites legitimately has no
  `favorites.json`, and that is not an incomplete restore); the paired device comes
  back as `Legacy Phone (paired)` through `devices.list`; and the settings come back
  through `settings.get` — device name `Legacy Laptop` and port 19990.
- The archive carries no `private_key_pem`, `certificate_pem` or
  `encryption_password`, and the scenario asserts that: settings and known peers
  travel, the identity does not.  A backup that carried the key would hand a fresh
  install a stranger's device id.
- Why the peers import at all, which is worth recording because it is not obvious:
  1.x's on-disk config keeps `peers` in the pre-list dict form, but its
  `create_backup` normalises that to a list before writing the archive, and the
  restore path's `_validate_peer_entries` accepts exactly that list shape.  Reading
  the writer is what established this; the dict-form tolerance the *config* reader
  has (`internal/config/config.py::_parse_peer_list`) is not what makes a 1.x
  archive work, and a hand-made archive carrying dict-form peers still restores its
  settings and silently drops its peers.  That last case is recorded, not fixed: no
  migration path depends on it, and the restore path merges peer trust, so widening
  what it accepts is a change to make deliberately rather than in passing.

What this does *not* establish: the archive was produced by the release **source
tree** run on this machine, not by a running 1.x GUI or by a 1.x binary.  A real
1.x install's archive could still differ in ways only its build would show, and the
same file restored on a second machine is still the cross-machine gate.

One hazard worth naming, because it bit this harness: the 1.x `_config_dir()` is
`%APPDATA%/ClipSync` and ignores `CLIPSYNC_CONFIG_DIR`, so a first attempt read the
real install's configuration (it wrote nothing — it faulted before reaching
`create_backup`).  The scenario now points `APPDATA` at the sandbox, which is what
keeps a 1.x writer from reading, or writing its archive into, the user's own data.

## Theme Parity: Fonts Checkpoint (2026-09-12)

Row 41's "complete theme parity" clause covered three things the 1.x tree did, and
they are not of one kind:

1. **`appearance_mode` of system/light/dark with OS detection.** Already carried:
   the shell reads `prefers-color-scheme` and stamps `data-theme`, and the two dark
   blocks in `styles.css` exist because CSS cannot merge a media query and an
   attribute selector into one rule set.  Nothing was pending here.
2. **A bundled CustomTkinter palette** at `assets/themes/clipsync.json` — light
   `#F3F5FC`, dark `#05060D`, frames `#FFFFFF`/`#0A0E1E`.  **Not adopted**, and
   deliberately not silently rejected either: the shell's token palette
   (`--page`, `--surface`, `--accent`, …) is a deliberate design, and the standing
   instruction was to reorganize the interface to the standard of a mature Tauri
   application, not to reproduce a CustomTkinter look.  Adopting the aurora ramp
   would repaint every surface to match a UI toolkit this app no longer uses.
   Recorded as a product decision to make, not a migration gap to close.
3. **`internal/ui/fonts.py`'s `_PLATFORM_FONTS`** — a per-platform family list
   resolved at runtime through Tk.  This was a real gap with a functional failure
   mode, in two directions: a stack naming only `"Segoe UI"` reaches a dated
   default on macOS, and on a Linux box whose fontconfig has no CJK family before
   `system-ui` the entire Chinese interface renders as tofu.  Fixed.

**What changed.**  `styles.css`'s `:root` no longer hard-codes
`font-family: "Segoe UI", "Microsoft YaHei UI", system-ui, sans-serif`.  It
declares two tokens — `--font-ui`, which reproduces the legacy intent in that
platform's own order (Latin faces first, then the CJK families, so a glyph missing
from Segoe UI or SF falls to YaHei or PingFang rather than to an unrelated
fallback), and `--font-mono` — and sets `font-family: var(--font-ui)`.

CSS cannot inspect what is installed the way Tk could, so the order carries the
intent instead of a runtime probe.  That is the honest difference between the two
implementations, and it is why the stack is longer than the legacy list.

**Where the monospace face applies.**  The rule applied was: a surface whose text
exists to be read or compared character by character is monospace; prose stays in
the UI face.  Hence `.log-view`, the fingerprint in `.cert-list .fingerprint`
(newly classed in `App.vue`, because the digest is there precisely to be compared
against the one on the other screen) and `.update-ready-path` (one long
machine-made installer path).  `.translate-source`, `.translation-result` and the
`qrUrl` line stay in the UI face — the first two are prose, and the third is copied
rather than compared.

**Evidence.**  `npx vue-tsc --noEmit` clean; `npx vitest run` 218 passed in 8 files
(including `i18n.test.ts`, which the `App.vue` class addition had to leave
untouched, and the certificate-dialog case in `app.test.ts`, which matches on the
fingerprint's text rather than its markup).

**What this does *not* establish.**  Nobody has looked at a running window on
macOS or Linux; the stack is a code-level claim about what those platforms will
resolve, and the Linux CJK-tofu case in particular can only be confirmed on a box
that has the problem.  The aurora palette question is open by decision, not by
omission.

## AI Config Batch Pull Checkpoint (2026-09-12)

Row 40 named three clauses.  Two of them were not what the row said:

* **Async inventory refresh** was already there and the row had simply not been
  updated — `?refresh=1` on the inventory route asks the peer to re-send, and
  `application.ts` turns the arriving `aiconfig_inventory` event into a store flag
  that `App.vue` watches, re-reads the peer's inventory and clears the "已请求更新"
  line.  Left as found; the row now says so.
* **Batch workflows** were genuinely absent.  The `pull` route has always taken
  `items: [...]`, but the only caller built a one-element array from one row's
  button, so pulling ten files meant ten requests and ten unrelated progress
  batches.

**What changed.**  `App.vue`'s remote-inventory list gained a tick per row, a
"选择本页" box over the page, "拉取选中项（N）" and "清除选择" — and one
`pullAiRemote(items, mode)` behind both the row button and the batch button, so
the confirmation cannot be true of one entry and false of ten.  The dialog now
lists what a batch will write instead of naming a single path.

Two decisions worth naming:

* **Ticks are keys, not row indexes.**  A selection held by position would move
  onto a different file the moment a refresh reordered the peer's entries —
  silently, and in the direction of writing the wrong file.  Keys are
  `tool:root:rel_path`, and a refresh prunes the ones the peer no longer offers,
  because a tick the reader can see but the batch cannot send is a lie about what
  the button will do.
* **The page box reports "some, not all"** via `indeterminate`.  Treating a
  partial page as fully ticked would make one click clear selections the reader
  never chose.

**Evidence.**  `it("pulls the ticked remote entries as one batch, and drops a tick
the peer no longer offers")` in `desktop/tests/app.test.ts` — one call carrying
all three ticked entries, the count on the button, the prune after a refresh that
drops `one.md`, and the `checked`/`indeterminate` states of the page box.
`npx vue-tsc --noEmit` clean; `npx vitest run` 219 passed in 8 files.

**What this does *not* establish.**  "Real cross-device receipt" is the clause
still open, and it is a platform gate: no two machines have exchanged these files,
so what is proven here is that the shell asks once for many files and tracks the
batch — not that a peer writes them.  **Full legacy parity** is likewise untouched:
the 1.x AI-config UI has not been diffed against this one entry by entry, and the
one comparison made so far (the fonts) belongs to row 41.

## AI Config Version Diff and Batch Pull Checkpoint (2026-09-12)

Row 40's remaining work was "async inventory refresh, batch workflows and full
legacy parity".  The first was already built and the row had not been updated.
The other two are the subject here.

**The gap, stated plainly.**  The 1.x AI-config panel's whole reason for existing
was the answer to "what does the other machine have that I do not", and it said so
per row.  The shell listed the peer's files and said nothing about how they
related to this machine's — the reader had to compare two lists by eye, which is
the one thing the two lists are worst at supporting.

**What changed.**

* `desktop/src/lib/aiconfig-diff.ts` — the comparison, as pure functions:
  `buildAiLocalIndex`, `aiCompareState`, `aiDiffCounts`, `aiEntryKey`, `aiMtimeMs`.
  Ported from `internal/web/static/js/aiconfig-helpers.js`, whose rules were each
  arrived at against a real misreport and are kept with their reasons in the file
  header: folders take no part (no hash to compare, so every folder would differ
  from every folder); a v3 row matches under *its own* root (a path-only match
  would compare `commands/x.md` against a local `rules/x.md`); a row with no mtime
  is the older side rather than a crash; and an unloaded local index yields
  nothing, not `missing`, so no row flashes a false badge while the local walk is
  still out.
* The remote list shows a badge per differing row — 缺失, 对方较新, 本机较新 — and a
  summary line above it.  A matching row carries **no** badge: badging every row
  would make the differing ones harder to find, not easier.
* The local walk is now read as part of reading the peer's inventory, once.  The
  badges compare against it, and a diff that silently reports nothing because half
  its input is missing is worse than no diff.
* Batch pull: per-row ticks, "选择本页" (which reports "some, not all" through
  `indeterminate`), "选择缺失项（N）" and one `pull` request carrying every ticked
  entry, with ticks pruned to entries the peer still offers.  Both the row button
  and the batch button call the same `pullAiRemote(items, mode)`, so the
  confirmation cannot be true of one entry and false of ten.

**Why "missing" and not "every difference" is the one-click.**  The legacy
migration wizard's default strategy was `skip` — pull only what this device does
not have — and the reason is the reason here: a file that exists on both sides but
differs may be *newer here*, so ticking it on the reader's behalf could overwrite
their own later edit.  The badge says so; the button does not decide it for them.

**Evidence.**  `desktop/tests/aiconfig-diff.test.ts` (6 cases: the states
themselves, the sibling-root case, the path-only fallback for v2 and legacy peers,
the no-local-index and folder cases, mtime units, and the count/key contract) and
one UI case in `desktop/tests/app.test.ts` covering all four badges plus the
summary line plus "选择缺失项" ticking exactly one row.  `npx vue-tsc --noEmit`
clean; `npx vitest run` 226 passed in 9 files.

**What this does *not* establish.**  No two machines have exchanged these files:
the badges are computed from a peer's *cached* inventory and this machine's local
walk, so what is proven is that the comparison and the one-shot request are right,
not that a peer writes anything.  That is "real cross-device receipt", still a
platform gate.  Three legacy surfaces remain unported and are named here rather
than claimed: the **search box** over both lists, **folder-level rows** (the shell
lists folders as entries but has no per-folder tick that expands to its files),
and the **guided migration wizard** (source device → diff → strategy → apply),
whose three strategies now have their pieces in place — the diff, the batch pull
and the copy/overwrite/append modes — but no single flow that arranges them.

## AI Config List Filter Checkpoint (2026-09-12)

The last checkpoint named three legacy surfaces still unported: the search box,
folder-level rows, and the migration wizard.  The search box is now closed.

**Why it mattered.**  A tool-profile inventory is not a short list: a Claude Code
install with several skills and commands runs to dozens of files, and the reader's
question is almost always about one of them.  Scanning two paginated lists for one
path is the slow way to answer a question the list already knows the answer to.

**What changed.**  `App.vue` gained a filter per list — `搜索本机配置` and
`搜索远程配置`, `type="search"` so the control is the platform's — matching
case-insensitively against the row as it reads on screen (`tool / rel_path`), so
typing a tool key narrows to that tool.  (That matching was superseded the same
day, when the lists became trees and the header began naming the tool: see *The
AI-Config Tree Checkpoint*.  The runs-before-pagination rule stands.)  The filter
runs **before** pagination, and
the footer's page count and both next/previous guards are computed from the
filtered length; narrowing the query resets to page one, because a page number past
the end of a narrowed list would render an empty list beside a live "next" button.

Three details that are decisions rather than mechanics:

* **Ticks live on entries, not rows.**  A tick made before filtering is still there
  after it, and a row that a filter hides keeps its tick — which is why the batch
  button's count can be larger than the number of visible rows.  Hiding a row is
  not the same as unchoosing it.
* **"选择本页" means the visible page.**  It reads the same filtered slice the list
  does, so it cannot mean one set of rows while the list shows another.
* **An empty result says so.**  "没有匹配的配置项" replaces a blank list, because a
  filter that matched nothing and an inventory that is empty look identical
  otherwise — and they are very different answers.

**Evidence.**  `it("narrows either list by path without disturbing the ticks or the
count")` in `desktop/tests/app.test.ts`: both rows, a tick held across a filter
narrow and widen, the no-match notice, the batch count still reading the tick, and
the local box narrowing the local list.  `npx vue-tsc --noEmit` clean;
`npx vitest run` 227 passed in 9 files.

**What this does *not* establish.**  Still no two machines: the remote list is a
peer's cached inventory.  **Folder-level rows** remain — the shell lists a folder
as an entry and can pull it, but has no per-folder tick that expands to the files
beneath it, so a skill folder is one row rather than a tree.  The **guided
migration wizard** also remains: its three strategies now have their pieces (the
diff states, the batch pull, the copy/overwrite/append modes) but no single flow
that arranges them, and its "skip" default is available only as the one-click
"选择缺失项" rather than as a strategy chosen for a whole migration.

## AI Config Card Density Checkpoint (2026-09-12)

The standing UI directive was that the code is built but the interface and its
usability lag, "especially the settings page, where everything is crammed
together".  The AI card was the worst case of it, and the work above made it
worse before it made it better: features were being added to one section that
already stacked two inventories, an editor, two paginations and a batch bar.

**What changed.**

* The card's two halves are now named: **本机配置** and **远程同步**, as `<h3>`
  headings in the same style as 危险区域 — blocks inside the card that are not
  settings of their own.  The `<h3>` rule gained a second selector for the remote
  half, because that half is wrapped in `.ai-remote` and so its heading is a
  grandchild of the section; naming it explicitly rather than loosening the rule
  to a descendant selector keeps the rule from catching headings inside nested
  blocks.
* Both lists are bounded and scroll themselves (`max-height: 340px;
  overflow: auto`).  Each half lists up to forty files, so without a ceiling the
  second half of the card starts below a screenful of rows the reader is not
  looking at — the card's own length was doing the cramming.

**Evidence.**  `npx vue-tsc --noEmit` clean; `npx vitest run` 227 passed in 9
files (the settings assertions go through `aria-label` and headings, so the
restructure is covered by the cases above rather than by new ones).

**What this does *not* establish.**  Nobody has looked at the page in a running
window: "less crammed" here means two headings and a scroll ceiling, which is a
claim about the rules, not about how it reads at 1120×760 or at the 380px minimum.
The rail still lists fourteen jumps, and below 700px it was hidden whole —
a clause closed later the same day: see *The Settings Rail on a Narrow Window
Checkpoint*.

## Folder-Level Row Ticks Checkpoint (2026-09-12)

The previous checkpoint named two legacy AI-config surfaces still unported.  The
folder rows are now closed; the migration wizard is not.

**The gap.**  A Claude Code skill is a folder, and the inventory lists the folder
*and* every file under it.  The shell could pull the folder (the server expands
`is_dir` requests) but had no way to tick its files as a group — so a reader
picking a whole skill ticked each file, one page at a time.

**What changed.**  A folder row's box stands for the files beneath it:

* `aiRowKeys(item)` — a file is itself; a folder is every **file** entry under the
  same root whose path starts with the folder's own path plus a separator.  The
  root is compared, not just the path prefix, because a folder belongs to exactly
  one root: this tool's `skills/foo/` and `commands/foo/` are two different
  folders.  The separator matters for the same reason in the other direction —
  `foobar/` is not a child of `foo/`.
* The box reads `checked` when every file under the folder is ticked and
  `indeterminate` when some are, and ticking it adds those file keys, never the
  folder's own key.  What gets pulled is therefore a list of files either way, so
  a folder that is also listed as a row cannot be pulled twice.
* A folder holding no files gets no box — there is nothing to shortcut to — but
  keeps a marker in the same column so the paths stay on one line down the list.
* "选择本页" and the page box read the same expanded keys, so a page whose folder
  is fully ticked reads as fully selected rather than as one unticked row.

**Evidence.**  `it("ticks a folder as the files under it, and leaves a sibling
root's folder alone")` in `desktop/tests/app.test.ts`: two folders of the same
name under two roots, a `foobar/` that must not be swept in, the empty folder with
no box, the partial state after unticking one file, and the batch request carrying
exactly the one remaining file rather than a folder.  `npx vue-tsc --noEmit`
clean; `npx vitest run` 228 passed in 9 files.

**What this does *not* establish.**  The legacy panel drew a **tree** — indented
rows with expand/collapse — and this is still a flat list whose folder rows happen
to precede their contents.  What is ported is the folder *selection* semantics,
not the tree rendering — which was closed the same day, in *The AI-Config Tree
Checkpoint* — and the ledger clause named only the former.  The
**guided migration wizard** also remains: its three strategies have their pieces
(the diff states, the folder and batch selection, the copy/overwrite/append modes,
and "选择缺失项" as its `skip` default) but no single flow that arranges them, and
still no two machines.

## Guided Migration Wizard Checkpoint (2026-09-12)

The previous checkpoint closed the folder rows and named the wizard as the one
AI-config surface still unported.  That clause is now closed.

**The gap.**  The legacy panel walked a reader through one flow: read the other
machine's inventory, compare it against this one, pick a conflict strategy, then
pull once.  The shell had every piece of that — the diff states, the folder and
page selection, the batch pull, and all three landing modes — but no single flow
that arranged them, so the reader had to build the wizard out of parts: read the
remote list, read the local list, read the badges, tick the rows by hand, and
choose the mode on a per-item dialog.

**What changed.**  A 迁移向导 button sits beside 读取远程库存 in the AI card, and
opens a dialog that is the whole decision:

* A source-device picker, defaulting to nothing.  With no device chosen the
  dialog says so and offers no strategy — a strategy chosen over an inventory
  it does not have would be a count of zero that means "nothing to do" and
  "nothing read" at once.
* Three strategies, in the order a cautious reader wants them: 只补缺失（默认）,
  全部拉取，已有的另存为副本, 全部拉取，覆盖本机文件.  The default is the one that
  cannot lose anything local, which is the same reason the legacy wizard
  defaulted to `skip`: a file present on both sides may be *newer here*, so
  ticking it on the reader's behalf could overwrite their own edit.
* One summary line in the dialog's own words for the current strategy — how many
  files it will bring, and for the overwrite case that the replaced files are
  kept as `.bak`.
* One button, labelled with the count it will act on, disabled at zero.
* The remote rows are ticked to mirror what the strategy chose, so what is about
  to move is visible on the list behind the dialog.  The tick is reactive rather
  than a snapshot, because the inventory arrives after the dialog opens.

**No new backend path.**  `mode: "copy"` already writes the real name when the
target does not exist — which *is* "only missing" — and `<stem>.from.<device>`
when it does, and `overwrite` already backs up to `.bak` before `os.replace`
(`internal/sync/ai_config.py`).  So `skip` and `copy` both send `copy`, and
`overwrite` is the only one that reaches the pull dialog's confirmation, exactly
as any other overwrite does.  The wizard arranges decisions the pull path could
already carry out.

**Evidence.**  `it("runs the migration wizard as one pull per strategy,
confirming only the one that overwrites")` in `desktop/tests/app.test.ts`: an
absent file, a locally-newer file and an identical one; the no-device state; the
`（1）` / `（2）` counts as the strategy changes; the batch payload for each
strategy; and that the overwrite case sends nothing until the confirmation is
clicked.  `npx vue-tsc --noEmit` clean; `npx vitest run` 229 passed in 9 files.

**What this does *not* establish.**  Nobody has run two machines against this.
The inventory the wizard decides from is a *cached* peer listing, so what the
wizard compares is whatever the last peer response said, and cross-device receipt
stays the platform gate it was.  The wizard keeps no record of a migration, so
re-running it re-decides from the current diff rather than resuming.  It is
per-device: a reader with three machines runs it three times.  (The panel's tree
rendering, the other clause this checkpoint named, was closed the same day —
*The AI-Config Tree Checkpoint*, below.)  Nothing here has been looked at in a
running window.

## The AI-Config Tree Checkpoint (2026-09-12)

Two checkpoints above ended by naming the same thing: the legacy panel drew a
**tree**, and the shell drew a flat list that happened to put folder rows before
their contents.  That clause is now closed.

**The gap.**  `local_listing()` sends one flat entry per watched file, each with
`tool`, `root`, `rel_path` and `is_dir`.  A folder is named only by being a prefix
of the paths under it, so "Claude Code, skills, writing, a.md" arrived as three
unrelated strings rather than as one row and an indent — and no folder could be
closed, so a skills directory of forty files put all forty on screen.

**What changed.**  `desktop/src/lib/aiconfig-tree.ts` (new) is the legacy
`aiconfig-panel.js` `groupedRows` / `rowsForGroup` pair ported as pure functions:
the inventory folds back into nodes keyed by (tool, root, path), each tool becomes
a group in the profile order the settings checkboxes above the list already use,
and the rows are walked depth-first, skipping the children of a folded folder.  A
node's key carries its root because a `rel_path` is relative to its own root:
`skills/foo/x.md` and `commands/foo/x.md` are two folders, not one.

Three rules came with it, and each is load-bearing:

* **A search flattens the tree.**  A match inside a folded folder would otherwise
  be invisible, which is why the panel dropped the tree entirely while a search
  was running; the result is depth-0 rows carrying their full paths, sorted by
  label.  The filter therefore matches the **path** rather than `tool / rel_path`
  as the flat list did — the tool is named by the header over its own rows, and
  the placeholder has always said 筛选路径…
* **A root hint appears only where one tool watches more than one root**, and only
  on a depth-0 row.  Two roots can hold the same relative path, so without it the
  row is ambiguous; a single-root group needs none, and a hint on every row would
  be noise.
* **A listed folder and a file under it are one row.**  The inventory sends the
  folder *and* what is inside it, so the two meet on one node — whichever arrives
  first — and the row is a folder either way, which is what keeps its tick box
  standing for its files (`Folder-Level Row Ticks Checkpoint`).

**Three desktop decisions, none of which the panel made.**  The shell pages its
list 40 rows at a time, and the pagination has always been over rows rather than
over the panel's per-tool sections: so a header is drawn wherever the tool changes
*within* a page, because a page that begins inside a group still has to say which
tool those rows belong to.  The second is the fold itself: the fold map ~ the folders the
reader has opened, so a name missing from it is folded ~ lives in the renderer,
keyed by node, rather than on the nodes, because the tree is rebuilt on every
inventory read and a flag on a node would be thrown away with it.  A folder
starts folded; that inversion is traced in the checkpoint below.
The third is that a folder the inventory never listed — one that exists only as a
prefix — is offered 打开 and nothing else: there is no file of its own to edit, and
the panel offered its trash button only for a row the inventory listed.

**Evidence.**  `desktop/tests/aiconfig-tree.test.ts` (new, 7 cases) covers the
builder directly: a flat listing folding into folders with a folder before its
files, the profile order with unnamed tools last, the root hint's two conditions,
a folder starting folded and keeping its own row, a search flattening
across folders and folding case, `itemCount` counting a group's config items,
and a listed folder plus a file under it meeting on one row in either arrival
order.  That count has since changed meaning ~ see the count checkpoint below ~ and
the case now reads as the count of top-level items: a folder is one, whatever is
inside it.  `it("draws each tool's config as a named tree, and folds a folder without
losing its row")` in `desktop/tests/app.test.ts` covers the wiring: the header
with its count, the fold by chevron and by name, and the folder row's one action.
The sibling-root case in `it("ticks a folder as the files under it…")` now also
asserts the two root hints.  `npx vue-tsc --noEmit` clean; `npx vitest run` 257
passed in 11 files.

**What this does *not* establish.**  Nobody has looked at it in a running window.
The indent of 18px per level, the chevron's column and the header's weight are the
panel's numbers carried across, not a measurement at 1120×760, and the list's 340px
scroll ceiling was chosen for a flat list.  This is still the shell's `<ul>` and not
the panel's `<table>`: there are no size or time columns, and the per-file 打开 /
移入回收区 buttons are the shell's, so a wide inventory reads differently here even
with the same rows in the same order.  The rows were drawn into two stacked
lists at the time; they are one list each now — *One AI List at a Time* below.

## Relay Delivery Receipts Checkpoint (2026-09-12)

The ledger, the ACK path, the bounded retries and the `relay.delivery.changed`
event were already in place — the window simply never listened for them.  Every
receipt fell through the store's event handler to a full `refresh()`, so one ack
repainted the clipboard history, and the only delivery state the interface showed
was the aggregate 待投递消息 count: the ledger's own rows, and the `msg_id` a
`ChatEntry` has carried since the nearby-chat work precisely so a receipt can be
matched to the bubble it belongs to, were read by nothing.

* `desktop/src/stores/delivery.ts` (new) is the mirror: a `msg_id` → status map
  for the bubbles, capped to the newest 250 ids, plus a per-peer summary
  (`pending`, `lastStatus`) for the send list.
* `relay.delivery.changed` is folded in place and the handler *returns*
  (`desktop/src/stores/application.ts`).  A receipt describes one send, not the
  clipboard, so it must not repaint the history list — that is the whole reason
  the legacy Round-17 rules were worth porting.
* The bubble pill is the legacy chat panel's rule set, not a new one: outgoing
  **text** only; nothing when the entry itself is `failed` (that message never
  left, so it keeps its failed label and its resend button); nothing without a
  `msg_id` (older hosts send none, and a receipt matched to no id would land on
  the wrong bubble); and nothing for an id the ledger never mentioned, which is
  every message sent over the LAN alone.
* The per-peer line is the legacy card's: 待补发 {count} while the peer is away,
  then the newest result as ✅/❌/⏳ over 已送达/未送达/待补发/发送中, drawn in the
  window's own icon set.  Both stay off until a ledger answers, so a backend that
  cannot report never reads as "nothing pending".
* A peer's ledger is seeded when the panel opens (`internet_pairing_status` →
  `relay_delivery_status` per peer), because the event stream only carries the
  transitions that happened while this window was open.  A fetch that started
  before a live receipt abandons its write-back rather than walking the count
  backwards.
* The mirror carries no store-wide error.  A host with no relay refuses
  `relay_delivery_status` for every peer it is asked about, so raising one would
  put an error band on the page for a capability the reader may never have
  paired for; legacy hid the line too.  The refusal is kept where it belongs —
  as that peer's `loadFailed`, which is what hides the row — and a later real
  transition still counts against it.
* Two departures from legacy, both deliberate: the fetched rows feed the bubble
  map too (legacy needed a live event, so a message sent before the window opened
  showed nothing), and the ledger's full row list gets no new view — the legacy
  panel had none, and the desktop already reads more of the ledger than legacy
  used.

**Evidence.**  `desktop/tests/delivery.test.ts` (12 cases): the four statuses and
nothing else; the bubble rules; a re-broadcast `queued` counted once; an unkeyable
row skipped whole; the 250-id cap; seeding newest-first; the host's own total
winning over the rows a window can count; a live receipt beating a snapshot that
started first; a refused fetch hiding the line while a later receipt still
counts; nothing read before the sidecar is ready; forget and reset.  Two
integration cases in `desktop/tests/app.test.ts` walk the same paths through
the window — a delivered chat receipt stamping its
bubble while the clipboard is *not* refetched, and a peer's 待补发 1 retiring when
its receipt arrives.  `npx vue-tsc --noEmit` clean; `npx vitest run` 244 passed in
10 files; `cargo check` clean.

**What this does *not* establish.**  Nothing has crossed a real broker: the two
devices that would produce these receipts are still the platform gate, so every
receipt exercised here was synthesized by a test.  The routing is per *surface*,
not per message kind — the ledger keys a clipboard send and a relayed chat message
in the same rows, and the fold branches on neither `kind` nor `session_id`; both
stay on the row for whatever ends up naming what was sent.  Nothing here has been
looked at in a running window.

## Window Footer and Settings Rail (2026-09-12)

The sidebar footer carried a 退出 button that ended the session.  A mature Tauri
window does not offer that there: closing the window already hides it to the tray,
and 退出 ClipSync is in the tray menu, so the footer now carries 最小化 instead —
`minimize_app`, authorized to the main window like every other command.  The
`quit_app` command and its `bridge.quit` wrapper were left in place even though
nothing in the window calls them now, because a quit that stops the sidecar before
exiting is still a legitimate thing for a caller to want; the tray does not use it
(it calls `app.exit(0)`, and `RunEvent::Exit` stops the bridge either way).

The same footer named the toolkit the window is built with — `Tauri Desktop ·
1.0.0` — which reads as a build stamp rather than a product.  It now reads 版本
{version}, through the key the diagnostics summary already uses, and hides itself
until the status lands instead of showing a bare framework name.

The settings rail was a flat run of fourteen jumps, which is the same complaint
the settings page carried before it was given a rail at all: a list to scan
rather than a list to aim at.  It is now five groups — 通用 / 连接 / 服务 / 数据
/ 系统 — which are the questions a reader arrives with, in the order the cards
appear on the page.  Grouping makes the rail taller than a short window, and a
sticky rail cannot be scrolled past its own bottom, so it scrolls itself inside
a ceiling that clears the header and the status bar; below the narrow breakpoint
it is collapsed rather than gone — its groups are hidden as before, but its
search box survives, which the checkpoint below closed on 2026-09-12.

The sidebar itself was a flat run of six rows, with 设置 third from the bottom
between two pages of content.  It is the window's own configuration rather than
another place to look, so it now sits below the five content pages behind a
rule.  The digit chords follow the rows the reader can see — they always did,
which is why `PAGES` is ordered as the template draws it — so the pages are now
Ctrl+1 to Ctrl+5 and settings is Ctrl+6.  Ctrl+, still opens preferences, which
is the chord a desktop user reaches for first.

**Evidence.**  `it("minimizes the window from its footer rather than ending the
session")` in `desktop/tests/app.test.ts`: the footer button calls `minimize` and
never `quit`.  The chord test asserts the row order that follows: 文件传输
advertises and answers to Ctrl+4, and Ctrl+6 opens the settings page.  The
version line's wording is copy and no test asserts it; the shell's
catalog-coverage test is what keeps it translated — it is also what keeps the
rail's five new group headings translated.  Neither the sidebar row's placement
nor the rail's grouping nor the save path has a test of its own: the jumps are
`scrollIntoView` calls that jsdom does not implement, and the ids they carry
are the anchors the cards already had.  `cargo check` clean and `npx vitest
run` 245 passed in 10 files after these changes.  `docs/tauri/rpc-v1.md` was kept in step with the window: it now carries a
row for `minimize_app`, says the window no longer calls `quit_app`, and names
`relay.delivery.changed` among the runtime events along with the one reason it is
folded rather than reloaded on.

**Corrected below.**  Two clauses of this section describe the rail as a list of
jumps on a scrolling page: that it is taller than a short window because it lists
fourteen places, and that below the narrow breakpoint its groups are hidden.  Both
were superseded on 2026-09-12 by *One Settings Card at a Time* — the rail opens a
card rather than jumping to one, which is why it is now the only way between them,
and why its groups survive at every width.

## Settings Card Pass (2026-09-12)

Three cards were carrying a defect of the same shape: a control or a remark that
described something other than what it sat next to.

* 同步 held four toggles and four relay fields as one run of eight rows.  The
  relay half is its own subject — the endpoints, the login, and whether internet
  sync is on at all — so it is now an 互联网同步 fieldset, the same device the
  剪贴板历史 card already used for its two filters.
* Both relay lists were single-line `<input>`s carrying the placeholder 每行一个地址.
  That is an instruction a single-line control cannot follow, and the model
  behind it splits on newlines (`desktop/src/App.vue`): a reader could only ever
  enter one address, or guess that commas also work.  They are `textarea rows=3`
  now — what the legacy panel used for the same two fields, and what the
  process-name list in the neighboring card already used for the same
  newline-list semantics.  Each one also carries the legacy panel's own hint,
  which says which list is the primary path and which is the fallback.
* 网络与高级 opened with a paragraph about the mDNS service type and then showed
  the TCP port first: the paragraph was the legacy `settings_window.service_type_hint`,
  hoisted out of its row, so it read as a description of the card and explained
  the wrong field.  It is back under the service-type control.  The rows
  themselves were not reordered.
* 数据备份's list of backups was a bare run of filenames: no name for the list,
  no way to ask for it again (the refresh button sat in the row of
  create/export actions, two blocks above the list it refreshes), and no way to
  restore what it showed — the only restore path was a file dialog, so a reader
  looking straight at the backup they wanted had to go and find the same file
  again.  The list now carries the legacy panel's own heading (可用备份) with
  the refresh button on that line, each row shows the `date` and `size` the
  host had been returning all along (`backups.list` → `list_backups`), and each
  row carries the legacy panel's per-row 恢复备份 button.  It fills in
  `restorePath` rather than restoring: the same confirmation dialog the picker
  feeds still stands between the click and the restore.  Sizes are folded into
  KB/MB/GB — the legacy panel printed the byte count raw — and each button names
  the backup it restores, since six rows of "恢复备份" are six identical buttons
  to a screen reader.

**Evidence.**  `it("edits the relay lists as the multi-line lists they are")` in
`desktop/tests/app.test.ts`: the control is a `TEXTAREA`, it opens holding both
saved addresses one per line, and two edited lines plus a trailing blank one
save as two addresses.  The rest of this pass is copy and placement, which no
test asserts: the shell's catalog-coverage test is what keeps the new strings
translated.  `it("restores a backup from the list without a file dialog")`
covers the backup row: age and size reach the list, the row's button names its
own backup, and clicking it opens the confirmation on the listed path with
`chooseFile` never called.  `it("marks the settings card being read in the
rail")` stands a fake `IntersectionObserver` in for the one jsdom does not
have: all fourteen cards are watched, nothing is marked until the observer
answers, the card whose head has gone by wins over the one below it, the
marker moves when that card leaves the top, and a jump marks its own card
outright.  `npx vue-tsc --noEmit` clean; `npx vitest run` 247 passed in 10
files.

**What this does *not* establish.**  Nothing here has been looked at in a
running window — the placement claims are reasoned from the stylesheet, not
seen.  The 网络与高级 card is still one run of twelve rows; the legacy settings
window grouped its own fields as 剪贴板与同步 / 文件传输 / 连接 / 日志与通知, but
this card also holds settings that postdate that window (去重方式, 低内存模式,
重试剪贴板捕获, 显示历史条数), so a grouping invented here would be a guess
rather than a port — and a wrong grouping is worse than a flat one.  (Closed
below: *The Advanced Card's Own Groups*, 2026-09-12 — the groups are the legacy
window's own, and the fields that postdate it were placed by subject.)  The legacy
panel's own save-path row has no separate button here: the data directory is a
field in the 网络与高级 card and rides the page's 保存设置 with every other
setting, which is the shell's own way of saving rather than the legacy panel's
per-section one.

The rail's marker for the card being read is the one part of this pass that
is not static, and its placement is the one thing left unverified here: the
root margin that decides which cards count as on screen is a reading of the
layout, not a measurement of it.  What the marker *does* with what it is told
— which card wins, and when — is not a judgment and is covered above.

**Corrected below.**  The rail's marker described here — an `IntersectionObserver`
deciding which of fourteen stacked cards the reader has reached — was replaced on
2026-09-12 by *One Settings Card at a Time*.  The page shows one card, so "which
card is the reader on" is the entry they clicked, and the observer, its root margin
and the test that stood one in for jsdom are all gone with it.

## One AI List at a Time Checkpoint (2026-09-12)

The AI card was the one card in the settings page that had grown a second card
inside it: 本机配置 and 远程同步, each with its own heading, its own filter and
its own paging.  Either list could be read only by scrolling past the other, and
the peer's list sat below the fold of a reader who had not chosen a device.

**What changed.**  The card now opens with a 查看设备 picker — 本机（这台设备）
and every paired device — and shows the one list it names.  The read button
follows the view (读取本机配置 / 读取远程库存, one or the other, never both),
while 迁移向导 stays on the shared row because it is opened with no device
chosen as often as with one and says so itself.  The peer's half became the
`v-else` of the local one, and the two `<h3>`s and the second peer `<select>`
went with it.

**A consequence worth naming.**  This is the first change in this stretch that
makes something *unreachable* rather than reachable: the local list is no longer
on screen while a device is chosen, so the local filter is reached by putting
the card back on this machine.  That matches the legacy panel, which showed one
list behind its device bar and said which — and the shell's own remote rows
already carry the comparison (缺失 / 对方较新 / 本机较新) that the stacked
layout was providing by proximity.

**One stale sentence went with it.**  The peer's half said 读取本机配置后可对比版本差异
whenever the peer's list was on screen without a local index to compare against —
naming a button that, under this layout, is not on screen while a device is
chosen.  It now says 尚未读取本机配置，暂时无法对比版本差异, which is the same
defect the settings card pass was about: a remark describing something other than
what sits next to it.

**An empty list is two states, not one.**  A peer nobody has asked and a peer
that answered with nothing both draw an empty region, and the card had no way to
tell them apart.  `aiRemoteRead` is set only where `peers[peerId]` is present —
a peer that never answered leaves the key absent — so the two lines are
尚未读取该设备的配置 and 该设备还没有可同步的配置项.  This is the same
null-is-not-zero rule the version badges already follow for a missing local
index, and it is why the flag is not simply "the read finished".

**Evidence.**  `it("shows one device's list at a time and says when a device has
not been read")` in `desktop/tests/app.test.ts`: this machine's list and no
pull-mode control while 本机 is chosen, the peer's half and no local row once a
device is picked, 尚未读取该设备的配置 before the read and
该设备还没有可同步的配置项 after one that returns no entries.
`it("narrows either list by path without disturbing the ticks or the count")`
now switches the card back to this machine for its local half, which is what
that case has to do under this layout.  The three catalog entries the removed
headings and select used (远程设备 / 本机配置 / 远程同步) had to be deleted as
well — the shell's stale-entry test is what proved no other view was rendering
them.  `npx vue-tsc --noEmit` clean; `npx vitest run` 258 passed in 11 files.

**What this does *not* establish.**  Nobody has looked at this in a running
window, and the picker's fit beside a 168px name column is reasoned from the
stylesheet, not seen.  It is still a `<select>`, not the panel's device pills —
though its options now carry each device's difference count, which is what the
panel's legs carried (*Per-Device Difference Counts* below).  The list's 340px
scroll ceiling was chosen when a list shared the card with another list and has
not been re-measured against the room it now has.

## Per-Device Difference Counts Checkpoint (2026-09-12)

The legacy panel's device bar told the reader, before anything was opened, which
of their devices differed from this machine and by how much.  The shell could not:
it kept only the selected peer's inventory, so the counts the runtime had already
computed for every peer were read and thrown away.

**What changed.**  `aiInventories` keeps every peer's last-known inventory by
device id, and `aiRemoteItems` / `aiRemoteLegacy` / `aiRemoteRead` are derived
from it for the selected device rather than stored beside the peer id — so
switching devices cannot leave the previous peer's rows on screen under the new
peer's name, and a device the reader returns to shows what it last said instead
of an empty panel.  The picker's options carry the count: `First（1 项不同）`.

**Where the count comes from, and where it does not.**  One read answers for
every peer, not just the one asked for — `ai_inventory` returns
`{"peers": <every peer the runtime has heard from>}` regardless of `peer_id` —
so the map fills from the read the card already makes.  A device nobody has read
and a device read before this machine's own listing landed both carry **no
count**, not a zero: the legacy bar said `diffTotal: 0` in exactly that case
(`aiconfig-panel.js`: `store.aiConfigLocal.loaded ? H.diffCounts(...) : {total:
0}`), and the reader cannot tell that zero apart from "identical".  This is the
same rule the version badges already followed for a missing local index.

**Where the map is filled from, before the reader chooses.**  A count is only
worth having if it is on screen while the reader is *choosing* a device, so
opening the settings page primes the map with one cached read — `aiInventory(false,
"")`, which asks no peer to re-send anything — the way the legacy panel primed
`/api/aiconfig/inventory` when its card mounted.  One such read answers for every
peer; nothing has to be opened first.

The counts themselves still wait on this machine's own listing: they compare two
sides, and the prime reads only the peers'.  So the card opens with devices
named but not counted, and the first listing that lands — the local read, or the
peer read, which reads the local listing too — lights up every device in the
picker at once, not only the one being looked at.

**A cached inventory is half of the diff, and cannot stand in for the read.**
The wizard chooses its rows from the diff, so it reads the inventory when it
opens — but only when the peer's list looked empty.  With a primed map the list
is never empty, so the read was skipped, the local listing was never taken, and
every row compared as new: 开始迁移（0） with the files right there on screen.
It reads whenever either half is missing now, not only the peer's.

**Two smaller honesty fixes came with it.**  A peer that never answered no longer
gets the message 已读取 0 个远程配置项 — the empty state says which of the two
empty states it is instead.  And the read result replaces the map wholesale on a
real answer while leaving it alone when the host answers with no `peers` at all,
which is the legacy store's rule exactly ("404 (older backend) / network error —
surface the empty state but never wipe data that was already on screen").

**Evidence.**  `it("marks each device with how many files differ, and keeps what
each device last said")` in `desktop/tests/app.test.ts`: no count on either
device before anything has been read, one read that names the count on *both*,
and a switch to the second device that makes no request at all — its rows,
and its 对方较新 badge against this machine's copy, were already known — which is
also why its mock has to answer the priming read and the reader's read alike.
The priming is covered by `it("shows one device's list at a time and says when a
device has not been read")` (a primed map that holds nothing for the device, so
it stays unread) and the wizard's read by `it("runs the migration wizard as one
pull per strategy, confirming only the one that overwrites")`, which reports
（0） under the bug above.
`npx vue-tsc --noEmit` clean; `npx vitest run` 259 passed in 11 files.

**What this does *not* establish.**  Nothing here has been seen in a running
window, and a count in a `<option>`'s text is the only place a native select can
put it — the panel's legs were buttons with a badge.  The map is also lost when
the window reloads, as the legacy store's was: nothing writes it down, so a fresh
window claims nothing until the settings page is next opened, which primes it
again from the runtime's cached answer.

## History Row Actions and the Header Lines (2026-09-12)

Two presentation gaps, both found by reading the panel's own stylesheet rather
than by guessing at a redesign.

| Legacy panel | Native before | Native now |
| --- | --- | --- |
| `history-item__actions` sat at `opacity: 0` and came back on `.history-item:hover` / `:focus-within` | every row carried all five of its buttons, always: on a full page that is a hundred buttons before a single clip is read | the same rule, on `.history-row`: the strip fades in when the row is pointed at or tabbed into |
| `@media (hover: none) { .history-item__actions { opacity: 1 } }` | — | the same guard: a pointer-less device has nothing to point with, so nothing may hide behind one |
| `opacity`, not `display` | — | the same: the grid column stays reserved, so revealing the strip shifts no layout, and a button is still reachable by keyboard — which is what brings the strip back |
| a device row's actions were the card's only controls | `.device-row .row-actions` keeps its buttons | unchanged — the reveal is scoped to `.history-row` |

A pinned history row was *not* an exception in the panel (only its left border
and background change), so it is not one here either.  The desktop row also
carries a per-row selection checkbox that the panel had no counterpart for —
the panel selected by click under a modifier — so the checkbox stays where it
is: hiding a control with no other way in would be a regression, not a port.

The page header above that list was one nested ternary per line, which had to
be read from the inside out to find a single page and which a seventh page
would only have made longer.  It is now `PAGE_HEADINGS` / `PAGE_SUMMARIES`,
keyed by the same `PAGES` tuple the sidebar and the chords are built from, so a
page that is drawn but not named there fails the type-check rather than
rendering blank.  Each entry is a function: the labels are translated and the
counts are live, and a table of strings read once would freeze both.

**Evidence.**  `it("names every page in the header, in both languages, with its
own count")` opens all six pages by chord and pins each heading and summary,
then switches to English and pins two of them again — the second half is what a
table built once would fail.

`npx vue-tsc --noEmit` clean; `npx vitest run` 248 passed in 10 files.

**What this does *not* establish.**  The fade itself is CSS, and jsdom applies
no stylesheet, so no test in this suite can see the strip appear or disappear:
the claim that it does is read from the stylesheet, in a running window, by
whoever next opens one.  The clipboard integration run and the packaged-window
smoke both remain outside this pass.

## A Confirm That Did Not Pair (2026-09-12)

`confirm_pairing` answers `{paired, status}`, not the `{accepted}` the other
device actions carry, and the store's `action` helper only reads `accepted` and
`copied`.  So a confirm that did **not** pair was reported as a plain success:
the card vanished on the refresh behind it and nothing said why the click had
come to nothing.  What that answer means is narrow and worth naming — the
runtime expires any request older than `PAIRING_TIMEOUT` *before* it looks at
the code, so `paired: false` is a request that was already over when it was
answered, which is exactly what the legacy dialog said.

| | Legacy | Native before | Native now |
| --- | --- | --- | --- |
| A confirm that paired | `pairing.accepted` — "已与 {name} 配对" — in an info dialog, plus "设备配对成功" in the footer | the row turns paired, and the runtime publishes `device.connected` | unchanged, and deliberately: the state change and that notice are the report, so a second message for the same moment is noise |
| A confirm that did not | `notify.pairing_failed` — "配对失败。验证码可能已过期。请重新连接。" — in an error dialog | nothing: the card disappeared on the next snapshot | the same sentence, as a notice in the notice stack, titled 配对请求 |

The notice carries no device name, because the legacy sentence did not: the
failure is about the *request*, which was already named on the card the reader
was looking at when they clicked.  The legacy string's line break is folded to
a space — the stack renders one paragraph, so a newline in the catalog would
have been collapsed by the browser anyway, and keeping it would have made the key
depend on an escape that the i18n sweep reads literally.

The peer-decided terminal states are still not notified: a peer rejecting or a
request expiring reaches the window as `pairing.resolved`, which the store
folds into an ordinary refresh.  That is a state change the row already shows
(the card is gone, the row says 已取消), so a notice for it is a separate
argument rather than a port.

**Evidence.**  `it("says so when a confirm click did not pair, instead of just
dropping the card")` mounts the devices page with a pending request, clicks
确认配对, and asserts both halves: the failed answer raises a notice carrying
the legacy sentence while `pairing.failed` is not an error band, and the answer
that *did* pair raises nothing.  `npx vue-tsc --noEmit` clean; `npx vitest run`
249 passed in 10 files.

**What this does *not* establish.**  Nothing was paired against a real second
machine: the `paired: false` behind this test was synthesized, and the real
expiry that produces it in the field is `PAIRING_TIMEOUT` wall-clock, which no
test here spends.  Nothing here has been looked at in a running window.

## The Advanced Card's Own Groups Checkpoint (2026-09-12)

The 网络与高级 card was thirteen rows in one unbroken run: a port, a sync
debounce, a receive directory, a log level and a data directory, with nothing
saying which of them belonged together.  The legacy Settings window had already
answered that question — it split these same fields into four cards of its own
(`internal/ui/settings_window.py`: `clipboard_sync_section`,
`file_transfer_section`, `connection_section`, `logging_section`) — and the
shell had flattened all four into one.

**What changed.**  The card is four `<fieldset>`s now, carrying the legacy
window's own names in its own order — 剪贴板与同步 / 文件传输 / 连接 / 日志与通知
— with every field the legacy window held sitting in the group that window put
it in: 同步去抖 and 剪贴板轮询间隔 with the clipboard rows, 接收目录 and 传输超时
together, TCP 端口 with mDNS 服务类型 and 最大重连次数 under 连接, and 日志级别
under 日志与通知.  The legends are the legacy catalog's own strings, so the
English is the legacy window's English ("Clipboard & Sync", "Logging &
Notifications") and not a new coinage.

**Two rows the legacy window places elsewhere, placed there here too.**  显示历史条数
is a web-page setting: the legacy Remote-access window kept the web port, this
limit and the access token on one card (`_web_history_limit_var` follows
`_web_port_var`), so the row moved to the 手机 Companion card beside the phone
port — outside that card's status block, because it is a stored setting and has
to be editable whether or not the service has been read.  The card's restart
remark (部分更改将在重启后生效) moved to the top of the card: the legacy window's
own `advanced_hint` sat under its title, and under the last row it read as a
footnote to whichever field happened to be above it.

**Where the post-legacy fields went.**  去重方式, 低内存模式 and 重试剪贴板捕获
join 剪贴板与同步 by subject — all three are things the clipboard itself does —
rather than under a group invented for them.  数据目录 stays outside every
group: the legacy window never offered an editable data directory (its 显示数据目录
button lived in the About window), so the row is the shell's, and a legend
invented for it here would be the guess the earlier pass refused to make.

**Evidence.**  Every one of these rows is reached by `aria-label`, so
`it("round-trips the advanced network settings and clamps out-of-range numbers")`
in `desktop/tests/app.test.ts` passes unchanged across the regrouping: it still
sets the debounce, the history limit, the log level, the dedup method and the
low-memory switch, and reads back what was saved.  The grouping itself is
asserted by nothing, because it is a reading of the legacy window's cards rather
than a behaviour: the shell's stylesheet already drew legends for the cards that
had them (`.settings-panel fieldset > legend` spans both columns), which is what
lets the nested rows keep the column grid.  `npx vue-tsc --noEmit` clean;
`npx vitest run` 259 passed in 11 files.

**What this does *not* establish.**  Nothing here has been looked at in a
running window: that a legend's 14px of padding reads as a group break rather
than as another row is the claim no test here can see.  连接 is also not the
legacy Network window — that window's relay fields live in this shell's 同步
card, where the internet-sync fieldset already holds them — so what is grouped
here is the three fields the shell kept in this card, which is a port, a
discovery service type and a reconnect count.

## The Settings Page's Own Search Checkpoint (2026-09-12)

The legacy Settings window opened with a search box over its own fields —
`settings_window.search_placeholder`, a per-panel match count, a dimmed sidebar
for the panels the query missed, and Return to open the first one that matched
(`internal/ui/settings_window.py`, *Settings search*).  The native settings page
has fourteen cards and no way to ask it anything, which is the longer page of
the two.

**What changed.**  The rail now opens with the same box.  Matching is a
case-insensitive substring over the text the reader can see, counted per card —
the legacy window's own rule, which collected the text of its widgets rather
than keeping a keyword table beside them; the shell walks the rendered cards for
the same reason, since a hand-written index over fourteen growing cards is one
more thing to keep in step.  A card that matches shows its count on the rail
(`网络与高级 · 1`), the cards that do not are dimmed, a status line says
`{count} 个分区匹配“{query}”` or 没有匹配“{query}”的设置项, Return opens the first
card that holds it in rail order, and Escape or the clear button puts the page
back.

**Two deliberate differences from the legacy search.**  It counts the *deepest*
element whose text matched, not every element on the way down: a card whose text
contains the query because a row inside it does is one match, and counting both
would report a number several times the size of what the reader sees.  And its
status line says 分区 where the legacy said 分组: in this shell a 分组 is a rail
heading and a 分区 is a card, and the count is of cards.

**What it does not do, and why.**  It does not filter.  The legacy search never
hid anything either — a card dropped from the page is a card the reader cannot
scroll back to once they have forgotten what it was called — so the query marks
and counts and leaves every card where it was.  The search string is the legacy
window's own (搜索设置…), and the English is the legacy catalog's
("Search settings…", "No settings match …"), with only the count line reworded.

**Evidence.**  `it("searches the settings page, counting what each card holds
without hiding any")` in `desktop/tests/app.test.ts`: nothing is claimed before
anything is typed; one card holds 低内存模式 in exactly one place, its rail entry
carries `· 1` and the other thirteen entries are dimmed while every card is still
rendered; Return marks that card as the one being read; a query no card holds
says so and claims no card; Escape empties the box and takes the marks, the
counts and the dimming with it.  `npx vue-tsc --noEmit` clean; `npx vitest run`
260 passed in 11 files.

**What this does *not* establish.**  Nothing here has been looked at in a
running window, and the marks are applied to the DOM rather than derived from the
template — so a card that renders *after* the query is typed (the Companion
status block, an AI inventory landing) is counted the next time the query
changes, not the moment it appears.  The legacy window had the same property and
re-applied its own state when a panel was rebuilt; this one re-applies on every
keystroke and when the page is opened, and that is what its counts describe.

**Amended below.**  "It does not filter" was true of a page that showed all
fourteen cards.  *One Settings Card at a Time* (2026-09-12) made the page show one,
so the same principle now reads the other way round: while a query is up the page
shows every card that holds it and nothing else, which never hides a match.  The
counts, the dimming, the marks and the Return key are unchanged.

## The Search Chords Checkpoint (2026-09-12)

The settings search gave the shell a second search box, and the shell's Ctrl+F
knew only about the first one: standing on the settings page it threw the reader
out to the history list and left the box in front of them alone.  The chord is
now page-relative — Ctrl+F focuses the search box belonging to the page being
read, and reaches the history page's from any page that has none of its own.

This is not a port.  The legacy Settings window bound no Ctrl+F at all (its
entry took Return and Escape only), and the dashboard bound Escape, Ctrl+W and
Ctrl+Q, none of which the shell carries: the window's close and quit are the
native ones, and the footer's button is now 最小化 rather than 退出.  The chord
map is the desktop convention, as its comment says, so the rule it follows here
is the convention's too — the platform's find key finds what is on screen.

**Evidence.**  `it("answers the window's own chords, and only when no dialog
owns the keyboard")` in `desktop/tests/app.test.ts` presses Ctrl+F twice: from
收藏库 (which has no box of its own) it lands on the history page with
`[aria-label="搜索历史记录"]` focused, and from 设置 it stays on the settings
page with `[aria-label="搜索设置…"]` focused.  `npx vue-tsc --noEmit` clean;
`npx vitest run` 260 passed in 11 files.

**What this does *not* establish.**  Nothing here has been looked at in a
running window, and only the two boxes that exist are covered: a third page
growing a search box of its own would have to be added to the same branch.

## The Settings Rail on a Narrow Window Checkpoint (2026-09-12)

Below 700px the settings rail was hidden whole, and that was written when the
rail was only a list of jumps.  The search box changed what hiding it costs: a
narrow window folds fourteen cards into one column and takes the rail's jumps
away with it, so the reader can no longer reach a card by name either.  The
search box is the one part of the rail that has no replacement by scrolling, and
the jump list is the part that does.

**What changed.**  `desktop/src/styles.css` keeps the rail above the cards at
the narrow breakpoint and hides only its groups: the box stays, the marks it
puts on the cards still land (the `.settings-hit` pass never consulted the
rail), the status line still counts the cards the query reaches, and Return
still jumps to the first of them.  What the rail alone carried is the per-card
counts and the dimming, so on a narrow window a query says *how many* cards
match rather than *which* — a difference in what the reader is told, not in what
the search does.  The box stops sticking: it had been sticky inside the rail's
own scroll box, and the rail it stuck inside is no longer one, so on narrow it
opens the page and scrolls away with it.  Ctrl+F brings it back, because the
browser scrolls a focused field into view — the chord checkpoint directly above
is what makes a non-sticky box acceptable here.

**Evidence.**  None by test: this is a media-query rule, and the suite runs in
jsdom, which applies no breakpoints.  `npx vitest run` 260 passed in 11 files
with no test edited — which shows only that nothing else moved, not that the
narrow window reads well.  The claim rests on the rules.

**What this does *not* establish.**  Nobody has looked at the page in a running
window at any width, let alone 700px or below.  Whether the box reads as part of
the page or as a stranded control, whether the hidden groups leave a gap above
the first card, and whether 16px is the right inset beside the panel's own 18px
margin are all things the rules state and only a window can answer.

**Superseded below.**  This section kept the rail's search box below 700px and
dropped its groups, which was right while the groups were only jumps.  Since
*One Settings Card at a Time* (2026-09-12) the groups are the page's only
navigation, so a narrow window keeps them as a wrapped strip above the card; the
box keeps its place at the top of that strip.

## The Transfer-History Clear Checkpoint (2026-09-12)

The legacy Transfers panel's history card carried a red **清除** button in its
header — `internal/ui/dashboard.py` builds it there, and `_on_clear_transfer_history`
asked through `ask_yesno` with the catalog's `transfers.clear_title` /
`transfers.clear_confirm` before calling `file_transfer_mgr.clear_history()`.  The
native page could delete one row and had no way to clear the list, so the one
action the panel offered over the whole history was the one action missing here.

**What changed.**  A new command, `transfers.clear_history` → `LanRuntime.clear_transfer_history`,
which counts the records under the runtime lock and then clears them — the same
rule "cancel all" follows, and for the same reason: a transfer that finished
between the page's last poll and the click is one the page's own list would not
have counted.  The sidecar publishes one `transfers.changed` and answers with
`{"cleared": n}`; the Tauri command is registered with its own generated
permission (`allow-clear-transfer-history`) and listed in the main capability.
The transfers page puts the button in the history card's own header, right-aligned
like the panel's, disabled while the list is empty, and it opens a confirm dialog
that names how many records are about to go.  Confirming reports the sidecar's
count; failing leaves the dialog open with the error inside it, which is the rule
the window's other dialogs follow.  **It clears records only**: a transfer in progress
is not a record, so the operation cannot disturb one — the reason the panel's
button sat on the history card rather than over both lists.

Two smaller things moved with it: the bulk-action report line is now `bulk-status`
rather than `cancel-status`, because "cancel all" and this share it and it is one
question — what did that button just do; and the three existing tests that reached
for a bare `button.danger` are scoped to `.toolbar button.danger`, since the page
now has two and the bare selector would mean whichever comes first in the
document.

**Evidence.**  `desktop/tests/transfers.test.ts`: the header button opens the
confirm without calling anything, the confirm names the list's own count, and
cancelling it neither clears nor reports; a second pass confirms and asserts the
command was called once with the sidecar's count on the report line.  A separate
case asserts the button is disabled with no records.  `tests/sidecar/test_rpc.py`
mirrors the `cancel_all` pair: parameters are refused (`{"transfer_id": …}`), the
runtime is required, and the method answers `{"cleared": 4}` with exactly one
`transfers.changed` event.  `npx vue-tsc --noEmit` clean; `npx vitest run` 262
passed in 11 files; `cargo check --manifest-path desktop/src-tauri/Cargo.toml`
clean; `python -m pytest tests/sidecar/test_rpc.py` 175 passed.

**What this does *not* establish.**  Nobody has pressed the button in a running
window, the dialog's modal behaviour in a real WebView is untested here, and no
transfer has actually been cleared by a real manager: the counts in every test
above come from a stub.  `docs/tauri/rpc-v1.md` gains the transfers rows it was
missing, and now says which other slices it still does not list.

## The Folder Send Checkpoint (2026-09-12)

The legacy Transfers panel had two send buttons.  **发送文件** opened
`askopenfilename`; **发送文件夹** opened `askdirectory`, zipped the folder into a
temp file (`zipfile` over `rglob`, members written as `p.relative_to(p.parent)` so
the picked folder stayed the archive's top level), sent *that*, and remembered the
path in `self._zip_cleanup` so the transfer's completion could unlink it.  It
refused a folder with no files rather than sending an archive that would arrive
empty.  The native page had only the file button: a folder could not leave the
machine at all.

**What changed.**  A new module, `internal/system/archive.py`, does the archiving
half and nothing else: `create_archive(folder)` writes a uniquely named
`<Folder>-*.zip` into the temp directory, returns `(path, file_count)`, raises
`ArchiveEmpty` for a folder holding no files (empty subfolders included, since an
archive of them transfers fine and arrives reading as a broken send), and unlinks its own partial archive if the zip fails partway.  `LanRuntime.send_files` calls it when more than one path
is picked or the one pick is a directory, and the archive — not the folder — is what the transfer
manager is given; the same `transfers.send` carries every shape, because on the wire
there is no difference.  The archive is remembered in `_outgoing_archives` under
`_archive_lock` and unlinked by `_on_transfer_complete` via `_reclaim_archive`,
which is the panel's `_zip_cleanup` under a different name: the receiver reads the
archive for as long as the transfer lasts, so the unlink waits for the terminal
event.  Three paths drop it earlier because no completion event will come — a
named target that is not connected, and a send the manager did not start.

The host gained `choose_folder` (a native folder dialog, registered with its own
generated permission and listed in the main capability), the page gained the
second button, and while a folder send is pending the report line says it is
archiving — the panel said the same thing in a progress dialog, and on a large
folder that wait is long enough to be worth naming.

**Evidence.**  `tests/test_archive.py` (6 tests at the time; 10 now): a real zip round-trip proving the
member layout is `Photos/a.txt` and `Photos/nested/b.txt` and that the bytes survive,
a name that starts with the folder and differs between two sends of it, `ArchiveEmpty`
for both empty shapes, and destination-directory creation.  `tests/sidecar/test_lan_runtime.py` (4 new tests): a directory send hands
the manager a `.zip` rather than the folder and registers exactly that path, the
archive is still on disk before the completion and gone with the map entry after it,
an empty folder is refused with `INVALID_ARGUMENT` and nothing started, an
unreachable target is refused with `NOT_CONNECTED` and leaves no archive in the temp
directory, and a plain file send registers no archive while its `transfer.complete`
event still goes out.  `desktop/tests/transfers.test.ts` adds a renderer case: the
folder button calls `chooseFolder` and not `chooseFile`, the archiving line is up
while the send is pending, and it clears with the folder's own path reaching
`sendFile`.  `npx vue-tsc --noEmit` clean; `npx vitest run` 263 passed in 11 files;
`python -m pytest tests/sidecar/test_lan_runtime.py tests/test_archive.py` 134 passed.

**What this does *not* establish.**  No folder has been sent between two machines:
every test above stops at a stubbed transfer manager or at the seam where the real
one would take over, so what a receiver sees on arrival, and what its
history row calls the archive, is untested here.  The legacy picker was also **multi-select** — its file button opened
`askopenfilenames` and zipped more than one file into a single archive
(`transfer.creating_archive` / `transfer.zipping` over the same temp-file path).
That is closed in the multi-select checkpoint at the end of this file.
## One Settings Card at a Time Checkpoint (2026-09-12)

The settings page was fourteen cards one after another ~ near a thousand lines of
markup in `App.vue` ~ with a rail beside it whose entries jumped down the page to
them.  That is the shape the reader complained about from the start: everything
piled together, "东西都排在一起".  The rail made the pile navigable without making
it shorter, and a card fourteen entries down was still fourteen cards of scrolling
away.

The page now shows one card.  `settingsSection` names which, `openSettingsCard` is
what the rail's buttons call, and every card is rendered with
`v-show="showSettingsCard(id)"`.  That ref is the whole of the page's state: what
the `IntersectionObserver` was for ~ deciding which of fourteen stacked cards the
reader had reached ~ is here exactly the entry that was clicked, so the observer,
its root margin, the helpers around it (`scrollToSection`, `observeSettingsSections`,
`unobserveSettingsSections`) and the test that stood a fake one in for the one jsdom
does not have are all gone with it.

`v-show` rather than `v-if` is not arbitrary.  The search pass walks all fourteen
cards to count what each holds and to mark the hits, and a card that was not
rendered could not be counted ~ so the page is one card *shown* at a time, not one
card *existing* at a time.

**The Save bar.**  It was already sticky at the bottom of the layout, which is what
kept a change made on the tenth card from being a scroll to the bottom of the page.
Now that the layout holds one card, the bar sits directly under the card the changed
field is on, and the stickiness is left to do only what it does for a card taller than
the window.

**The search, which a switcher could easily have broken.**  The rules, which are one
set rather than a special case per interaction:

* With no query up, the page is the card the rail points at, and the rail marks it
  (`.active` and `aria-current="true"`), the same way the sidebar marks the page
  being read.
* Typing a query counts every card and marks the hits across all fourteen ~ the
  rail entries carry `· N`, the matching ones stop being dimmed ~ while the
  reader stays on the card they were already on.
* Return opens the first card in the rail's own order that holds a hit, and ends the
  search.  That is the reason the counts are on the rail at
  all: a count that could not be walked to would only be decoration.
* A query nothing holds leaves the reader on the card they were on, rather than on
  an empty page under a rail that already says nothing matched.
* Clicking a rail entry during a search opens that card and clears the query.  The
  rail stays a list of every card while a query is up, so an entry that could not be
  opened would be a control that does nothing.
* Escape clears the marks, the counts and the dimming, as it did before.

**The narrow window.**  Below 700px `styles.css` used to drop the rail's groups
(`display: none`) and keep only its search box, which was right while the groups were
jumps to a page that showed everything anyway.  The groups are now the only way
between the cards, so the narrow rule keeps them: the rail goes static, each group
becomes a wrapped row of entries under its own heading, and the search box keeps its
place above them.  The alternative ~ hiding them and stranding the reader on one card
~ would have been a page with no way out of it.

**Evidence.**  `npx vue-tsc --noEmit` clean; `npx vitest run` 263 passed in 11 files.
The two rail cases in `desktop/tests/app.test.ts` were rewritten.  The first opens one
card from the rail and then a second, asserting after each that the card named is on
screen, that the one before it is not, and that `aria-current` moved with it.  The
second walks the whole search: nothing claimed before typing; `1 个分区匹配“低内存模式”`
and a `· 1` count on the rail entry that holds the hit; the page becoming that card
alone with the other thirteen dimmed; Return clearing the box, ending the search and
leaving the matching card open; a query nothing holds leaving the reader where they
were; a rail click during a search clearing the query and opening the card; and Escape
clearing the marks.  Visibility is asserted through `.style.display` rather than VTU's
`isVisible()`, because `v-show` hides with an inline style and VTU answers from
`getComputedStyle`, which in jsdom comes from the default stylesheet rather than the
inline style for a tree that was never attached to the document ~ the two disagree
there, and the inline style is the one `v-show` sets.

**What this does *not* establish.**  No CSS is loaded in the test environment, so the
suite proves which card the page *binds* to be shown, not that a real window hides the
others; nobody has looked at the settings page at any width, so "one card at a time"
is a claim about the rules rather than about how it reads at 1120×760 or at 380px.
The search pass reads every card's contents, and the only `required` field in the cards
is `device_name`'s, on the default card which is always mounted ~ a card whose required
field were hidden would still be validated, because `v-show` keeps every card in the
tree.  And the page's own affordances are unchanged from the checkpoints above: nothing
here makes a card shorter or a control clearer, only the way between them, and a
card left holding an edit the reader walks away from is the one question it could
not answer: see *Which Card Holds the Edit* below.
## Which Card Holds the Edit Checkpoint (2026-09-12)

The switcher above made the settings page one card long, and that left one thing
unanswered.  The Save bar is sticky and always on screen, so it went on saying there
were unsaved changes while the card holding them could be thirteen entries up the rail
and out of sight.  A reader who edits one card, walks to another and presses Save is
not misled ~ the save writes the whole form ~ but nothing on the page could tell them
which card was still pending, or that they should go back to it at all.

The rail now carries a dot on every card holding an edit the sidecar has not been told
about, beside the card's own name.

**How the card is named.**  The control that was typed in sits inside the card's own
`<section>`, and that section's id is already the card's name (`settings-<id>`, the
page's anchor), so `markSettingsEdited` reads the card off the event's own target.  The
alternative was a table of which setting belongs to which card: a second inventory of
the page to keep in step with the template, and one whose failure mode is silence ~ a
field added to a card and not to the table would simply never mark it.  Reading the
card off the control that changed cannot fall behind the template, because the control
is in the card.

The form reports the edit as it happens, on `input` and on `change`: `input` because a
keystroke is itself the edit, `change` because a select and a checkbox announce
themselves only once they are done.

**Not every control in a card is a form field.**  The internet card's pairing code, the
AI card's device picker and its two filter boxes are typed in or chosen inside a card and
change nothing the save writes.  Since a change is what the marks are about,
`markSettingsEdited` compares the form's own snapshot ~ the same one the Save bar
compares ~ either side of the event and marks nothing for an event that left the form
where it was.  That snapshot is watched rather than read at load and at save, because the
form also changes without an event: the pause countdown is re-read on a timer while the
page sits open.

**What is marked, and what that means.**  The marks are read through `settingsDirty`,
the same snapshot comparison the Save bar uses, so a value typed back to what was saved
marks nothing.  A card edited and then reverted keeps its dot for as long as some other
card is still unsaved ~ the marks are not re-derived per card, which is the table this
avoids ~ so the dot means "this card was typed in", not "this card differs".  It can
therefore say too much, and never too little: with nothing pending at all there is
nothing marked, and the dot and the bar are answering the same question.

The dot is a mark rather than a colour on the entry, because the entry's colour already
means which card is on screen, and it is pushed to the far end of the entry so the marks
line up in a column down the rail.  It carries `role="img"` and the sentence the Save
bar already says as its label, so a reader who cannot see it is told the same thing in
the same words, and it is not a second translation key for one state.

**Evidence.**  `it("marks the cards holding an edit the reader has walked away from")`
opens settings over a form with a saved name and port, and pins the whole shape: nothing
marked while the form is the saved state; the dot on the card that was typed in and not
on the other; its `role` and label; the mark staying put when the reader opens another
card, with the first card's `v-show` off screen; a second card's edit marking that one
as well and only that one; a control inside a third card that is not a form field ~ its
pairing code ~ marking nothing while two other cards are still marked, which is the case
the snapshot comparison exists for; and the save clearing both marks and putting the bar
back to the saved sentence.  `npx vue-tsc --noEmit` clean; `npx vitest run` 264 passed in 11
files.

**What this does *not* establish.**  The dot is CSS, and no stylesheet is loaded in the
test environment: that it is a dot at all ~ 7px, accent-coloured, at the far end of the
entry ~ is read from `styles.css` rather than seen, and it has not been looked at in a
running window, at 1120x760 or in the narrow strip the rail becomes below 700px.  The
dot is not a control: it does not open its card (the entry does) and it does not count
how many fields on that card changed.  Nothing about what the sidecar calls saved
changed here ~ the save is still one form, sent whole ~ and no test here types in a card
and reverts it to see what the mark does then.  The general card's language select is the
other side of the same coin and is left as it was: it persists itself through its own
`updateSettings` call and also lands in the form, so the Save bar reads dirty after a
language change that has already been saved.  That was true before this pass, and the dot
inherits it.## What the Config Count Counts Checkpoint (2026-09-12)

The AI card reports what it read, and both numbers it reported were wrong in the same
way: the sentence above the list read 已读取 24 个配置项 for a machine with three config
items on it, and the number beside a tool's name read 18 for a tool with two.

**What was being counted.**  `fileCount` was the count of the tool's *files* — the
inventory's own entries with `is_dir` false — and the card's own total was
`aiLocalItems.length`, the raw entry count.  Both are counts of what the walk found, and
a walk finds everything: on the machine this was noticed on, one Claude Code skill folder
held seventeen files across four nested folders, so the skill and the settings file beside
it were reported as eighteen config items and the card said twenty-four.  The reader never
asked how many files are on disk.  The question is how many config items there are, and a
skill is one of them: the files inside it are the skill's own internals, and nothing on
this page would be done differently because a skill has more files in it.

**What is counted now.**  `aiItemCount` counts distinct `(tool, root, first path
segment)` among the entries, which is exactly the set of top-level rows `rowsForGroup`
builds.  The group's number and the card's sentence are both that function — the group's
over its own bucket, the card's over every entry — so the two can no longer disagree, and
the sentence is the sum of the numbers beside the tool names rather than a second,
loosely-related tally.  That is the same three items the reader can see and open:
`settings.json`, the skill folder, and the other tool's `config.toml`.

**Two things the old count got right, kept.**  A tool that watches several roots can hold
two folders of the same name — `skills/foo/` and `commands/foo/` — and those are two rows
with their own root hint, so they are two items here as well; the root is part of the key
for the same reason it is part of `aiNodeKey`.  And an entry whose rel_path is empty names
the root itself, which the group already is: the tree skips it, and so does the count.

**The number is about the inventory, not about the view.**  Neither a search nor a folded
folder changes it — a filter is not what the card read — and a folder inside a folder is
not a second item either, since it is inside the skill like everything else under it.
Only the top level of a group is counted.

**Evidence.**  `it("counts one skill as one item, and sums the tools into the card's own
count")` in `desktop/tests/aiconfig-tree.test.ts` builds the shape that made this
necessary — twenty-four entries: a settings file, a skill folder listed with twenty-one
files under it across a nested folder, and another tool's one file — and pins the group
numbers (`2` and `1`), the card's total (`3`), a folder inside a folder counting once, the
two-roots case counting twice, an empty list counting nothing, and an entry naming its
root counting nothing.  The rewritten `it("counts a group's config items, where a folder
is one however full it is")` holds the same rule at the group level, and
`it("counts a skill folder as one config item, not as the files inside it")` in
`desktop/tests/app.test.ts` pins the two rendered numbers together — 已读取 3 个配置项
above the list and `2` / `1` beside the tool names — over a fixture of the same shape,
while asserting the rows themselves are untouched: the folder is still there to open and
its files are still listed under it.  `npx vue-tsc --noEmit` clean; `npx vitest run` 266
passed in 11 files.

**What this does *not* establish.**  Nobody has looked at it in a running window; the
numbers were read from the test fixtures and from a real inventory walk of this machine
(`~/.claude` holds `settings.json` and `skills/obe-softeng-report/` and nothing else the
profiles watch; `commands` and `agents` do not exist), not from the card itself.  The
count is a number of items, not a size: a folder of one file and a folder of two hundred
both read 1, which is the point, and nothing on the page says how much is inside one
before it is opened.  The peer's list is counted by the same function, but there is no
test here that reads a peer's inventory and checks the number in 已读取 N 个远程配置项.
The legacy web panel's own list (`aiconfig-panel.js`) is untouched and still counts files.

## What a Migration Target Is Checkpoint (2026-09-12)

The user's report was about the AI card's header line: `已读取 24 个配置项` on a
machine whose AI configuration is three things.  The count checkpoint above closed
the number.  Two consequences of the same rule were still open, because a count is
only one place a rule shows: the folders still drew *open*, so the list under the
number was a list of every file behind them, and the migration wizard still counted
and confirmed in files.  Both are closed here.

**A folder starts folded.**  `rowsForGroup` skipped the children of a *collapsed*
folder, but nothing was ever collapsed: the map came in empty and every folder drew
open, so a skill of twenty-one files put twenty-one rows on screen under a header
that said `1`.  The map has been inverted rather than pre-seeded: it now holds the
folders the reader has *opened*, and a name missing from it is folded, which is why
every folder starts that way without the renderer having to know a node key before
the tree is built.  Opening a folder shows the folders it is made of, not every file
in the tree under it, so the list stays a list of config items one level down as
well.  Fold state still lives in the renderer (`aiLocalExpanded` / `aiRemoteExpanded`
in `App.vue`) rather than on the nodes, for the reason it always did: the tree is
rebuilt on every inventory read, and a flag on a node would be thrown away with it —
and a refresh would spring every folder the reader had shut back open.

**A target is a config item.**  `desktop/src/lib/aiconfig-targets.ts` (new) folds an
inventory into *targets* by top-level name: `aiTargets(entries, qualifies)` returns
one target per distinct (tool, root, first path segment), each carrying the entries
that qualified under it.  It keys them with `aiNodeKey`, the same key the tree keys a
config item with, so the card's number and the wizard's list are one rule and cannot
disagree — which was the whole point of the complaint.  A name is a folder when the
inventory lists the folder itself or when anything is listed inside it, since a peer
may send either shape.  A name with nothing left after the filter is not a target at
all, so an empty folder and a folder the machine already matches never reach the
list.

**What travels is still the files.**  This is the part that looks like it should
change and must not.  The strategy's question is a fact about a file — "this machine
does not have it", "this machine's copy is newer" — and the diff that answers it
(`aiCompareState`) takes a file.  Handing the peer a folder instead would have the
peer expand it server-side (`_expand_items` in `internal/sync/ai_config.py`, which
already accepts a folder item) into *every* file under it, identical ones included,
so `只补缺失` would stop meaning what its radio button says and would leave
`<name>.from.<device>` copies of files that already matched.  So a target is the unit
of decision and of counting, and its `items` are the unit of transfer: `aiMigrateItems`
is the flat file list the peer is asked for, and it is still per file.

**Where the boundary is.**  The card's ticks stay per file, because a tick is a choice
about a file; a folder's box is still a shortcut for making all of them.  What counts
targets is everything the pull flow says out loud: the wizard's button and its summary
(`将补齐本机缺少的 {count} 个配置项` and the two `全部拉取` variants), the batch button
`拉取选中项（{count}）`, and the confirmation, which now lists targets and names how
many files sit behind a folder (`（{count} 个文件）`) instead of printing twenty-one
paths for one skill.

**Evidence.**  `desktop/tests/aiconfig-targets.test.ts` (new, 6 cases) pins the
targets directly: a folder and its files folding into one target, a name known as a
folder from either shape, the card's count and the wizard's list agreeing and keying
alike, a name with nothing to move dropping out, one tool's two roots keeping two
same-named folders apart with `foobar` not swept into `foo`, and entries that name no
config item being skipped in inventory order.  `it("counts a folder as one migration
target, whichever strategy is chosen")` in `desktop/tests/app.test.ts` drives the whole
flow through the wizard: a skill of three files with one missing plus one loose file
counts `（2）` under both strategies, `只补缺失` sends only the skill's missing file and
the loose one, and the overwrite confirmation reads `将写入以下 2 个配置项：` with
`claude / skill （2 个文件）` beside it.  The tree's fold is pinned by
`it("folds every folder until it is opened, and keeps the folder itself on screen")`
and, at the app level, by the folder-tick case opening each folder before asserting the
boxes inside it.  `npx vue-tsc --noEmit` clean; `npx vitest run` 273 passed in 12 files.

**What this does *not* establish.**  Nobody has looked at it in a running window, and
no real peer has been asked for a folder's worth of files: the wizard's click-through is
the DOM, and the peer's own expansion of a folder item (`_expand_items`) is exercised
only by the sidecar's Python tests, not by this flow — the shell deliberately never
sends one.  The count is a number of items and says nothing about size, and the legacy
web panel's list still counts and draws files.

## The Multi-Select Send Checkpoint (2026-09-12)

The folder-send increment left one clause open in its own closing paragraph: the
legacy file button opened `askopenfilenames` and zipped whatever it returned into a
single archive through the same temp-file path a folder took
(`transfer.creating_archive` / `transfer.zipping`), so picking three files was one
transfer to the receiver.  The native page picked exactly one file or one folder.

**What changed.**  `internal/system/archive.py::create_archive` now takes one path
or a sequence, which is the whole of the Python change: a picked file keeps its own
name as its member name, a picked folder keeps itself as the archive top level (the
panel's `relative_to(p.parent)`), and the archive's name is the single pick's own
name or `files-<N>` for several — the panel's two namings, unchanged.  A path that
is gone between the pick and the send raises `FileNotFoundError` out of the member
loop, before any archive exists; nothing picked at all raises `ValueError`.  The old
`NotADirectoryError` for a single non-folder path is gone, because a file is now a
legitimate thing to archive.

`LanRuntime.send_file` became `send_files(paths, device_id="")`.  It archives when
there is more than one pick **or** the single pick is a directory, so the common case
+— one file, one peer — is byte-for-byte the send it always was: same wire, same
receiving row, no archive to reclaim.  Everything past the archive is the folder
increment's code untouched: the same `_outgoing_archives` registration under
`_archive_lock`, the same `_reclaim_archive` on completion, the same unlink on a
`NOT_CONNECTED` refusal.

On the host, `send_file` became `send_files` (a `Vec<String>` and a device id, a
new generated permission and capability entry, a new line in `generate_handler!`),
and `choose_file` gained a sibling `choose_files` using `pick_files()`.  The page's
file button calls the multi-select picker; the folder button still calls
`chooseFolder`.  Several picks report `正在打包 {count} 个文件…` on the report line
while the send is pending, where one file says nothing — there is nothing to pack.

**Two decisions worth naming.**

* **A pick that vanished is an error, not a smaller send.**  The legacy panel passed
  its picked list through a per-file existence check and silently dropped what was
  missing.  Archiving a subset would deliver less than the user chose while their
  own screen said otherwise, so `create_archive` raises and the runtime answers
  `INVALID_ARGUMENT` with the path in the message.  This is a deliberate divergence
  from the panel, and it is the same rule the `只补缺失` work above applied to a
  folder: what the user picked is what goes, or nothing does.
* **One pick stays unarchived.**  Archiving a single file would change the wire and
  the receiving row for the most common send in the app, for no gain: the archive
  exists to make several files one transfer, and one file already is one.

**Evidence.**  `tests/test_archive.py` (10 collected) gains a single file keeping its
own name, three picks becoming one `files-3-*.zip` holding `a.txt`, `b.txt` and
`Documents/c.txt`, a one-item list still named after the file rather than `files-1`,
a vanished path raising `FileNotFoundError`, and nothing picked raising `ValueError`
+— its stale `NotADirectoryError` case is gone with the behaviour.  In
`tests/sidecar/test_lan_runtime.py`, two picks go out as one archive and one transfer
(asserted by opening the zip the manager was handed), a vanished pick is refused with
nothing started and no archive left behind, and an empty pick list is refused; the
eight `send_files` call sites of the older cases were rewritten to the list form.
`tests/sidecar/test_rpc.py` pins the new validation (`paths: []`, an empty path, a
non-string device id), and `tests/sidecar/test_runtime_integration.py` sends over a
real process boundary with `paths=[…]`.  In the renderer,
`desktop/tests/transfers.test.ts` gained "sends several picked files as one transfer,
and says it is packaging them" (three paths, one call, the packing line up while the
send is pending) and "sends nothing when the file picker is cancelled".  `npx vue-tsc
--noEmit` clean; `npx vitest run` 275 passed in 12 files; `cargo check` clean;
`python -m pytest tests/sidecar/test_lan_runtime.py tests/sidecar/test_rpc.py
tests/test_archive.py` 316 passed.

**What this does *not* establish.**  No file has been sent between two machines, and
none of these tests reach a real receiver: the zip is opened on this machine and the
transfer manager is a stub in every runtime case, so what arrives, and what the
receiver's row calls a `files-3-*.zip`, is untested here.  The desktop picker itself
is likewise never opened in a test — `choose_files` is mocked at the bridge, so the native
dialog's own multi-select is a code-level claim about `rfd`.  And the legacy picker's
**one-archive-per-pick** is reproduced; its progress dialog is not, since the report
line is one line rather than a modal.

## The Settings Layout Checkpoint (2026-09-12)

The settings page had been rebuilt twice — a card per group behind a rail with a
two-column row per setting, and then a search over it and a save bar under it — and
the note on it was still that the layouts are a mess.  Both passes were making claims
about the DOM, and **no stylesheet is loaded in the desktop tests**: jsdom assembles
these cards and never lays one out, so a page can satisfy every case in this
repository and still be a scrambled grid in the window.  This increment is that gap
read directly, the stylesheet against the markup it describes.

**Two defects, both structural.**

- **The fieldsets scattered their own rows.**  `.settings-panel fieldset` carried the
  card's two columns, `minmax(0, var(--setting-name)) minmax(0, 1fr)`, while the rows
  inside it were `.setting` labels that each carry those same two columns themselves
  and name no cell.  A grid places unplaced children into its columns in document
  order, so the first row landed in the 168px *name* column and was squeezed into the
  width of a label, the second landed in the control column, the third in the name
  column again — and where a row started depended on how many rows preceded it.
  All twenty rows inside the page's seven fieldsets — the groups of 同步,
  剪贴板历史 and 网络与高级 —were placed by that accident.  The fieldset is now a
  one-column grid, and each row places its own two columns.
- **Every checkbox sat against the right edge of its card.**  `.setting--check` folded
  the row to `grid-template-columns: minmax(0, 1fr)` while `.setting-control` kept
  `grid-column: 2`.  A grid item naming column 2 in a one-column grid summons that
  column back as an *implicit track*, so the row became an empty 168px column, the
  22px gap, then the box — the box at x=190 of a card whose own stylesheet comment
  said it "owns its whole row, at the same left edge as the controls above it".  All
  twenty checkbox rows in the page read that way.  The row now keeps both columns
  with an empty first, so the box lines up with the controls above it; the
  narrow-window rule names `grid-column: 1` for both children rather than folding the
  template alone, which would have left the same trap in place at 700px.

**Then the page's hierarchy**, which the two defects had been sitting under.

- **A rule under the title.**  A card's `h2` had no separator, so its title and its
  first row read as two lines of one list.  Sub-headings — an `h3`, a fieldset's
  `legend`, a `.setting-heading` — were indented into the control column, which made
  the security card's 危险区域 read as the label of the row beneath it.  They now span
  the card and carry the title's rule, and every fieldset is opened by a rule as well:
  no group in this page opens a card, all seven of them follow rows, and a card
  holding four groups otherwise showed fourteen unbroken rows.
- **The name lines up with the control's first line.**  `.setting` centred its two
  columns, so a row whose control carries a hint under it put the label halfway down
  the hint.  The row is now `align-items: start` with 9px of top padding on the name,
  which is where a 36px control's single line of text sits.
- **Controls stop at a width that suits what they hold.**  `width: 100%` on every field
  made a port number and a two-option select as wide as the 814px the card leaves.
  Numbers are 150px; selects and ordinary single-line fields 340px; and the six fields
  that hold a path, a URL or a key keep the full width by asking for it with
  `setting-wide`.  The rules are scoped to `.setting-control >` so a list's own filter
  box keeps the width its list gives it.
- **A block belongs to the card or to the row above it.**  The indented blocks and
  button rows are answers to a row; the ones that are a card's own work moved to the
  card's left edge with `setting-actions--card` (nine rows) and `setting-block--card`
  (eight lines), while the four inventories that are card content wherever they appear
  — the internet peer list, the backup list, the update progress bar and the ready
  path — need no class at all.

**Evidence.**  `npx vue-tsc --noEmit` clean; `npx vitest run` 275 passed in 12 files,
which is the same 275 as the increment before it — no case asserts on any class or
template this increment touched.  The fourteen `#settings-<id>` ids, the rail's
`.settings-nav` / `.settings-edited` / `.settings-match-count` marks, the search's
`.settings-search-status` and `.settings-hit`, and the AI card's
`.ai-local-list.setting-block li.ai-row` and `.ai-editor` selectors are all intact.
The stylesheet parses with balanced braces (332/332), and the class additions were
counted in the markup rather than observed in a window.

**What this does *not* establish.**  **Nothing here is a rendering.**  jsdom loads no
CSS, so no case in this repository can see a layout: the widths, the indent arithmetic
(168 + 22 = 190px) and the grid placement above are correct *by construction*, not
observed.  The page has not been opened in the desktop window.  The narrow-window
block is exercised by no case at all, and whether 700px is the right breakpoint for
these cards is a judgement that wants a window rather than a test.

## The Transfers Row Checkpoint (2026-09-12)

**What was checked.**  The same method the settings pass used — read the stylesheet
against the markup it describes — turned on the transfers page, which is the one place
in this window where a list row is not a fixed set of `span`s but a template with a
*variable* number of children.

**The defect.**  `.transfer-row` declared five columns —
`minmax(0, 1fr) minmax(120px, 240px) auto auto auto` — and both cards on the page used
it.  A *running* row fills those five: name, progress bar, percentage, two buttons.  A
*history* row does not: it carries no `progress` element at all, and every control it
does carry is optional.  In `TransfersView.vue` the tick is rendered only for
`completed`, the open/reveal pair only for a receive with a `path`, the retry button
only for a failed send with a `path`, and only the delete button is always there.  So
the grid placed whatever the row happened to have into the progress bar's track: a
completed row's tick, or a failed row's retry button, sat in a column sized for a bar
that was not present, and the two trailing `auto` tracks held the remaining buttons off
the card's right edge.  Every history row read as a name, a gap the width of a progress
bar, then a cluster of icons floating inside that gap.

**The fix.**  The history card is `transfer-list transfer-list--history`, and its rows
are flex rather than grid: `.transfer-main` takes the free space, and every other child
keeps its own width.  The row's *control count* is what varies here; how many columns a
row has is not a thing a terminal row should have to fill, and a template that names a
track per possible control is a template half of whose tracks are empty on every row.
Nothing was removed from a row: the tick, the two open buttons, the retry button and
the delete button are the same ones, in the same order, now sized by their own boxes.

**The narrow block.**  The `@media (max-width: 700px)` rule had the same defect in the
other direction.  It gave the running row three columns with the progress bar spanning
`1 / -1` on the second line, but a running row carries a percentage and two buttons
after the bar — four items for those three tracks once the bar had taken its line, so
the second button was auto-placed onto a third line at the far left.  The narrow
template now has four columns, `.transfer-main` takes the whole first line, and the bar
takes the first track of the second, which leaves the percentage and both buttons the
three tracks beside it.  Four is arithmetic rather than taste: it is what the widest
row needs.

**Evidence.**  `npx vue-tsc --noEmit` is clean and `npx vitest run` is 275 passed in 12
files (the transfers view's own 19 cases among them).  The stylesheet parses with
balanced braces.  The history card's class is on the one `div` whose header carries
the clear button, and the active card's `div` is unchanged.

**What this does *not* establish.**  **Nothing here is a rendering.**  jsdom loads no
CSS, so the twelve test files cannot see a layout, and no case in this repository
asserts a column count: the placement above is correct *by construction*, read off the
stylesheet and the template, not observed.  The transfers page has not been opened in
the desktop window.  The narrow block is exercised by no case at all — and the history
rows stay flex inside it, since `.transfer-list--history .transfer-row` is the more
specific selector, so the narrow template governs the running card only.

## The Page Gutter Checkpoint (2026-09-12)

**What was checked.**  After two defects in the settings page and one in the transfers
rows -- all found by reading the stylesheet against the markup it describes -- the same
reading was turned on the shell itself.  Not a row template this time: the *indent*
that every page-level strip in the window is placed by.

**The drift.**  The window indents its page content by 32px, and eleven rules in
`styles.css` wrote that number out for themselves -- `header`, `.toolbar`, the two
`.history-list` / `.device-list` lists, `.history-filters`, `.pagination`,
`.favorites-workspace`, `.settings-panel`, the two transfer cards' `.transfer-list` and
`.bulk-status`, `.speed-test`, `.error-band` -- while the narrow block restated the
narrow value, 18px, in eight of them.  A number eleven rules each carry is a number
they can disagree on, and three did:

- **`.error-band` was inset 24px**, in a window where every other page-level strip is
  inset 32px.  Its own narrow rule used 18px -- the narrow gutter -- so the intent was
  plainly to sit on the gutter, and the wide value is the one that drifted.  Its type
  therefore began 8px left of the page title above it and the rows below it, which is
  the one thing an alert band must not do.
- **`.history-filters` had no narrow rule at all**, so in a narrow window the chip row
  kept its 32px while `.history-list` directly beneath it dropped to 18px: the filter
  chips stood 14px to the right of the rows they filter, and the two never lined up.
- **`.pagination` was placed by two rules on the favorites page.**  There the footer is
  a child of `.favorites-list`, which sits inside `.favorites-workspace` -- a container
  that already carries the gutter -- so the strip's own 32px was a *second* indent and
  its count and its two chevrons stood 32px in from the rows above them.  The narrow
  block undid the padding at that one width, which is what a strip placed by two rules
  looks like: the workaround names the defect it works around.

**The fix.**  One `--page-gutter` on `:root`, used by every one of those eleven rules,
and moved once for a narrow window (`:root { --page-gutter: 18px }`) rather than
restated eight times.  Three narrow rules -- six selectors -- existed only to repeat the
indent and are gone: the two lists, the favorites workspace, and the transfer cards'
three strips.  Each of those selectors already takes the same value through its own
base rule, and each narrow rule kept only the block spacing that was actually different.
`.history-filters` now follows the gutter it was missing, and the favorites footer keeps
no side padding of its own: inside a list that carries the gutter, it takes that list's
edge.  `.chat-layout` -- the chat page's own card, in the component's scoped block
rather than in the stylesheet -- uses the token too, so the one page that styles itself
still lands on the same edge.

**Evidence.**  Every deletion is neutral by construction rather than by test: each
deleted narrow rule named a container whose base rule supplies the identical value
through the token, and each kept whatever else it said.  `npx vue-tsc --noEmit` is clean,
`npx vitest run` is 275 passed in 12 files, and the stylesheet's braces balance (336/336)
with `var(--page-gutter)` in exactly the sixteen places the eleven base rules and the
five narrow rules that change something else account for.

**What this does *not* establish.**  **Nothing here is a rendering.**  The 24px band, the
14px chip offset and the 32px footer are arithmetic read off two rules that disagree,
not measurements taken in a window: no case in this repository loads CSS, and the narrow
block is exercised by no case at all.  Whether 32px is the right gutter for this window
-- rather than 24px, which is what the error band chose -- is a judgement that wants a
window rather than a test.

## The Control Row Checkpoint (2026-09-12)

**What was checked.**  Two increments had found numbers that more than one rule had to
agree on: a card's two column widths, and the page gutter.  The next thing written twice
is not a number but a behaviour -- what a row of controls does when the controls on it
stop fitting.

**The defect.**  `.toolbar` declared `display: flex; gap: 12px` and said nothing about
wrapping.  The favorites page's toolbar carried a modifier of its own --
`.favorites-toolbar { flex-wrap: wrap; align-items: center }` -- so that row wrapped.  The
transfers page's toolbar, which carries more controls than any other row in this window,
did not: a `<select>`, then 发送文件, 发送文件夹, a refresh icon button, 速度测试 and
全部取消.

**A correction, because the first reading was wrong.**  The first account of this was
that the buttons past the edge are cut off or pushed behind a horizontal scrollbar.  That
is not what happens, and the stylesheet says so: nothing in it sets `white-space: nowrap`
on a button, and CJK text breaks between ideographs.  A button's automatic minimum size
is therefore about one character wide -- icon 17 + gap 8 + one glyph 14 + padding 28 +
border 2, roughly 69px -- so a flex row that may not wrap squeezes its buttons rather
than overflowing: at the widths where the transfers toolbar stops fitting, every
button's label breaks onto a second line and the row becomes four two-line buttons.  The
row genuinely runs out of width only at the bottom of the window's range, where the
controls cannot shrink far enough: four buttons, an icon and the select come to about
370px at their minimums, and a 380px window leaves the row 282px (380 - 62px rail - 36px
gutter).  There the last controls do sit past the right edge.

**Which widths are affected, and why it is not monotonic.**  At the base 14px the
controls want about 529px.  Above the 700px breakpoint the rail is 212px and the gutter
32px a side, so the row has `window - 276px`: broken from 805px down to the breakpoint.
Just below it the rail collapses 62px and the gutter 18px, so the row has
`window - 98px`: it fits again from 700px down to 627px -- the rail loses more than the
row needed -- and is squeezed again from there to about 470px, below which it overflows.
So the row is wrong in two bands and right in the one between them, which is the kind of
result that only falls out of writing the arithmetic down.

**The fix.**  `.toolbar` wraps and centres its items.  `.favorites-toolbar` is gone: the
rule it carried is the toolbar's.  The `<select>` rule that both pages wrote for
themselves -- identical declarations under two different selectors, `.favorites-toolbar
select` and `.transfers-view .toolbar select` -- is now one `.toolbar select`, and it
takes `min-height: var(--control)`, so the one control in those rows that did not take
the row's control height now does, which is the whole point of that token.  `.modal-actions`
wraps as well: a dialog's action row is the last place a control can be put out of reach,
and the about box's three buttons come to about 300px inside the 288px the minimum window
leaves in a 420px dialog -- a 12px overrun that is arithmetic rather than something seen.

**Evidence.**  `npx vue-tsc --noEmit` is clean; `npx vitest run` is 275 passed in 12 files
(the transfers view's 19 cases, which drive `.toolbar button.danger`, among them); the
stylesheet's braces balance (334/334); and `favorites-toolbar` appears nowhere in
`desktop/src` -- the class, the two rules that named it and the markup that carried it are
all gone together, so no rule is left describing a class nothing can have.

**What this does *not* establish.**  **Nothing here is a rendering.**  The 529px and the
370px are arithmetic on 14px full-width glyphs and a 2px border, so the three width
thresholds above (805, 627, 470) carry whatever error that has: they say which way the
row fails and roughly where, not exactly where.  jsdom loads no CSS, so no case here can
see any of it, and the narrow block is exercised by no case at all.  And whether a row of
two-line buttons is worse than a two-row toolbar is a judgement -- though the favorites
page has answered it in the markup since it was written, which is the evidence this
increment rests on.

## The Floating Notice Checkpoint (2026-09-12)

**What was checked.**  Every increment before this one was a number or a behaviour two rules
had to agree on.  This one is a placement: two things anchored to the same corner of the
window, and only one of them able to see the other.

**The defect.**  `.notice-stack` was `position: fixed; right: 22px; bottom: 22px` -- 22px
above the bottom of the window.  The window's last strip is `.bottom-status`, and its height
is not incidental: `button` takes `min-height: var(--control)`, so 36px, and the bar adds
11px of padding above and below and a 1px top border, about 59px.  Its controls therefore sit
11px to 47px above the window's bottom edge, and the stack's 22px laid the toast over their
top 25px of 36.  Those controls are the sync toggle, the pause label and the three presets --
which the bar's own comment says gather at its right edge, as does the toast.  A toast that
covers them covers the pause controls at the moment a user is most likely to want them.

**Why no constant fixes it.**  The obvious repair -- lift the stack by the bar's height --
needs a height the bar does not have.  At a narrow window `.bottom-status` wraps
(`flex-wrap: wrap`, with the runtime status line given a full line of its own), so the bar is
about 59px at a wide window and two or three lines at a narrow one.  A `calc()` of constants
would be right at one width and wrong at the other, and a fixed element is placed against the
window rather than against the box below it.  The bar's height is genuinely not a number this
stylesheet can restate.

**The fix.**  The stack is a child of the bar, taken out of its flow: `position: absolute;
bottom: calc(100% + 12px)`, which is 12px above the bar's top edge at every width, whether
the bar is one line or three.  `.bottom-status` takes `position: relative` -- it is now the
box floating notices are measured from, which is the only reason it is positioned at all.
The stack's right edge and the width it may take come from the page gutter:
`right: var(--page-gutter)` and `max-width: min(360px, calc(100% - var(--page-gutter) * 2))`.
That second one is a further defect the same move fixes: the width was capped by
`100vw - 44px`, measured against the window, so at a narrow window a 336px toast reached over
the 62px rail; `100%` here is the page column, so the toast stops at its own edge.  The
fade-in rules are untouched, and so is the `z-index: 20`: the bar is `position: relative` with
no `z-index`, so it creates no stacking context to trap a notice in, and the toast still
paints over the page and over the sticky toolbar.

**Also corrected here.**  One line of the page-gutter comment still ended in `--` where this
stylesheet writes `—`; it is the last of the four spots that increment left.

**Evidence.**  `npx vue-tsc --noEmit` is clean; `npx vitest run` is 275 passed in 12 files --
the notice cases among them, which find the stack by class wherever in the tree it sits, one
of them asserting the stack is absent when there are no notices; and the stylesheet's braces
balance 334/334, no rule having been added or removed.

**What this does *not* establish.**  **Nothing here is a rendering.**  The 59px and the 25px
are arithmetic on `--control`, the bar's padding and its one border, and the narrow bar's two
or three lines are rougher than that, since they depend on how the controls break.  No case in
this repository loads CSS, so no case can see the toast move; the narrow block is exercised by
no case at all.  And the fix moves a real question rather than answering it: the toast now
rides over the page's own bottom-right corner, where a pagination footer or a list row's last
buttons are.  That is where a floating notice normally goes, but it is a judgement -- the old
placement was not wrong about the toast, only about the bar.

## The Devices Page Checkpoint (2026-09-12)

**What was checked.**  Where the two controls that *add* a device live: pairing over the
internet (generate a code, enter the other machine's code, the peers, what the relay still
holds for each of them, how its last send ended) and the phone Companion service (read the
status, the port, start and stop, rotate the access token, the address a phone opens, the QR
code that carries it).  Both were cards in the settings page -- two of its fourteen, in the
连接 group behind the rail -- and the page about the other machines is the devices page, which
listed devices and offered no way to add one.

**The defect.**  Neither card is a preference of this machine.  A code the reader generates and
types *is* the step that pairs a peer; starting the phone service *is* how a phone joins.  The
settings page is where a reader goes to change how this machine behaves, so a reader who has
just connected a phone and wants a second one looks at the page that lists their devices -- and
found a list, a toolbar and an attic, with the two controls that matter on a page they had no
reason to open.  That is the standing complaint this stretch has been about, in its purest
form: not a hidden control but a control kept in the wrong room.

**A card is a card wherever it lives.**  Moving a `.settings-section` out of `.settings-panel`
took the field chrome with it.  The two column widths, the border, the radius, the padding and
`--setting-name` / `--setting-gap` were the card's all along -- a card's rows are laid out from
them -- but the chrome on the *fields* was the panel's: `width: 100%`, the border, the background
and the 8px/10px padding of an input, the 36px single-line height, and the three widths that
stop a control at the size of what goes in it (340px, 150px, `100%` for `.setting-wide`).  So
the moved cards kept their rows and lost their fields, which is a card of unstyled boxes: the
same defect class as the two this document has already recorded -- one fact about a control
decided in two places, and only one of them moved.  The selectors name the card beside the
panel now.  The panel's own name is kept rather than replaced, because the panel holds a
control that is in no card -- the rail's search box -- and that one's width comes from its own
rule.

**The status was behind a button.**  The peer list, the queued count, the latest send result and
the phone service's state are what a reader opens this page to see, and they arrived two ways,
neither of them right for the new page: the settings *load* read the relay (`refreshInternetPairing`
in `loadSettings`), and the phone status was read only when 读取手机服务状态 was pressed.  A card
that reads 已停止 until somebody presses a button is reporting on the page rather than on the
service, and the rows the service's own answer gates -- its address, its QR code -- are rows a
reader cannot find at all until an unrelated button is pressed for another reason.  Both are
read on the way in now, from the tab watcher that already followed the settings page in, and
the settings load reads the relay no longer: the card it read for has left that page.  The
companion read keeps its generation guard, which is what makes a read in flight unable to
overwrite a stop that landed while it was out.

**The order of the page.**  The two cards come after the devices this machine has and before the
archived ones, which is the order the page's sentences go in: what is here, how to add to it,
what has been taken away.  They are one grid, `.device-panels`, in two columns only when there
is room for two rows of a 168px name with its control beside them -- about 1,412px of window,
after the rail and two gutters -- and one column below that.  The track's minimum is written
`min(560px, 100%)` rather than `560px`, because a track whose minimum is 560px in a 524px column
pushes the card out of the page; `min(560px, 100%)` shrinks to the column instead.  The toolbar
gained `flex-wrap`, since a status line beside its buttons is a row that cannot always fit.

**The push button.**  It pushes text to this machine's clipboard and on to every paired device,
which is a sentence an icon cannot say, so it carries its label -- and it is disabled for a
reason the reader cannot see: the store's capabilities either include `clipboard.push` or they
do not.  A control that is grey with no explanation is a control nobody can act on, so the line
beside it says which capability is missing.  The dialog it opens is unchanged, explanation and
all.

**What stayed in settings.**  One field: 显示历史条数.  It is a setting like any other on that
page -- it rides the page's 保存设置 with every other field, so it has to be where the save bar
is -- and the card keeps a note saying where the service's own controls went.  The settings rail
carries thirteen cards now where this document's earlier count says fourteen.

**Evidence.**  `npx vue-tsc --noEmit` is clean; `npx vitest run` is 275 passed in 12 files --
the same 275, with five app cases now walking to the devices page for those cards (three
Companion cases, the QR case and the relay-ledger case), the marks case's typed-in non-form
control now the translation card's key, the rail's dimmed count one lower, and the Companion
cases' mocks made persistent values because the page's own read consumes the first one; and the
stylesheet's braces balance 336/336, no rule added or removed.

**What this does *not* establish.**  **Nothing here is a rendering.**  No case in this repository
loads CSS, so no case can see a card that moved, a field that regained its border or a grid that
chose one column: the 1,412px threshold is arithmetic on `--setting-name`, the control widths,
the rail's 212px and two 32px gutters, and it is rougher than that, since a hint under a control
makes a row taller without making it wider.  The three Companion cases assert through the DOM
that the card is on the devices page and reads on the way in; they cannot see that it looks
right there.  And the phone-side half of this remains what it was: the companion's own acceptance
on a real phone is still a release gate, as is the real mDNS/clipboard pair below it.

## The AI Page Checkpoint (2026-09-12)

**What was checked.**  Where the AI configuration lives.  The card read and wrote the
configuration files this machine's AI tools keep -- walking every configured directory on
disk, editing a file, moving one to the trash, and pulling a peer's over the relay -- and it
sat in the settings page, one of its cards, behind the rail's 同步与网络 group.

**The defect.**  Three things were wrong with it there, and only the first is the standing
complaint.  It is not a preference of this machine: the rest of that page decides how this
machine behaves, and this card goes and looks at a disk.  It was the one card that showed two
quite different lists depending on a `<select>` -- this machine's files, or a peer's -- so the
comparison the card exists for was between a list on screen and a list the reader could not
see at the same time; choosing the peer took the local list away.  And its two real form
fields rode the page's `保存设置`, so the reader who ticked a tool had to walk back to a page
they had left to write it.  A card that is the wrong kind of thing for its page, that hides
half of what it compares, and whose save is somewhere else, is three reasons to stop calling
it a card.

**The seventh page.**  AI 配置 is a page of its own, with its rail row among the content
pages -- a page that reads and writes files belongs beside history and transfers, not beside
settings -- and the settings row moved down one chord to Ctrl+7.  `PAGES` gained the entry and
`tab` is typed from `PAGES` rather than from a hand-written union, which is what makes a page
that exists in the rail and not in the type impossible rather than unlikely.  The page is laid
out as the two things it does: the tools and this machine's files in the left column, a paired
device in the right.  That is also the pair the difference line is about, so the line now sits
under two lists that are both on screen; the select names the peer and nothing else, since the
local inventory has a card of its own and no longer has to be selected into view.

**A save boundary that moved with the card.**  `aiEnabled` and `aiCustomPaths` were two of the
fields `settingsFormSnapshot()` watched and `saveSettings` wrote, so a checkbox drawn on one
page was saved by a button on another -- and the snapshot, which is what marks a card as
having unsaved edits, counted them against the settings page's cards.  This is the defect
class this document has recorded twice before, in its third form: one fact about a control
decided in two places, and only one of them moved when the control did.  Both fields left the
settings snapshot and the settings save, and the page has its own `aiProfilesFormSnapshot()`,
its own dirty comparison and its own 保存 AI 配置 button with the saved/unsaved line beside
it.  The read behind it sets its loaded flag only on success, so a read that failed is retried
on the next visit rather than leaving a page that shows an empty tool list as though the
machine had none; the button stays disabled until a read has succeeded.  `openSettings` primes
the peer inventories no longer and `loadSettings` reads the AI profiles no longer, because the
card those two reads existed for has left that page.  The rail carries twelve cards where the
increment before this one left it thirteen, and where this document's earlier count says
fourteen.

**What the page reads on the way in.**  A page's entry reads what its page shows, and this one
reads the profiles and the peer inventories -- the small call and the maps already cached in
the renderer.  It deliberately does *not* read this machine's own inventory: that call walks
every configured tool's directories on disk, which is a different order of thing from a status
the page is opened to see, and a reader passing through the page should not pay for it.  The
line under 读取本机配置 is what the last walk found, so the state is still reported -- it is
reported by the button that asked for it.

**One grid, two pages.**  `.device-panels` was the devices page's two-column grid and nothing
else's; the AI page needs the same one.  It is `.page-panels` now, with a `.page-col` wrapper
so a column can hold two cards: without it the grid would flow three cards into a two-column
track, and the AI page's third card would land under the first two instead of beside the card
it is compared with.  The narrow track is still written `min(560px, 100%)` for the reason the
devices checkpoint recorded -- a 560px minimum inside a narrower column pushes the card out of
the page -- and the page wrapper takes `--page-gutter` the way the panel does, since the grid
is the page's and not the panel's.

**The two keys that had to go.**  Eleven strings were added to `en.ts` for the new headings,
the empty states and the button.  Two were removed with the card's headings -- `AI 工具配置`
and `本机（这台设备）` -- and that removal is not cosmetic: the i18n test fails on a stale
entry in either direction, so a key whose Chinese no longer appears in any `t()` call has to
go rather than stay as a translation nobody can reach.

**Evidence.**  `npx vue-tsc --noEmit` is clean; `npx vitest run` is 277 passed in 12 files --
the same twelve files, two cases added: one for a profile read that fails and is retried on
the next visit without a third read after it succeeds, and one for a save from the page's own
button that asserts the argument list and then that the settings page's save leaves the call
count where it was.  Twenty AI cases now walk to the new page rather than the settings card,
the chords and header cases carry Ctrl+6 for the new rail row and Ctrl+7 for settings, the
rail's dimmed count is one lower, and the settings-retry case compares `mock.calls.length` for
the assertion that a re-visit reads nothing.  The stylesheet's braces balance 338/338, and
`device-panels`, `settings-ai` and `showSettingsCard("ai")` are all gone from `desktop/src`.

**What this does *not* establish.**  **Nothing here is a rendering.**  No case in this
repository loads CSS, so no case can see the two columns, the `.page-col` wrapper's effect on
where the third card lands, or the new rail row's place in the list: the two-column threshold
is the same ~1,412px arithmetic the devices checkpoint recorded, rougher than that because a
hint under a control grows a row in height without growing it in width.  The cases assert
through the DOM that the fields are on the new page, that the save is the page's own and that
the inventory is not read on entry; they cannot see that the page looks right, and they do not
touch the disk walk the inventory button performs.  What the page does with a real tool
directory on a real machine, and the peer half of it over a real relay, remain release gates --
the same ones the devices and companion checkpoints left open.

## The Orphaned Rule Checkpoint (2026-09-12)

**What was checked.**  Which classes the stylesheet carries that no markup draws.  One did:
`.type-icon`, declared twice -- a 34x38 box on the surface ground for a history row's type
glyph, and a narrow-window rule that hid it.  `desktop/src` contains the two declarations and
nothing else: no template writes the class, no test asserts on it, and no binding builds it
from a string.  So the base rule styled nothing at every width and the narrow rule hid a box
that was never there.

**How it got there.**  The rules around the two say it.  The history row is a grid, and its
narrow form is `grid-template-columns: minmax(0, 1fr) auto` -- a two-track row where the glyph's
own column used to be.  The element went with the redesign of that row and the two rules stayed,
which is the ordinary way dead CSS accumulates: a template edit removes the element, and a rule
two hundred lines away goes on describing it.

**What changed.**  Both declarations are deleted, and nothing else: no class was renamed, no
markup touched, and four braces left the stylesheet -- 338/338 at the end of the increment
before this one, 336/336 now.  This is housekeeping rather than a migration gap, and it is
recorded because it is the one class in `desktop/src/styles.css` that no page carried, which
is a fact the ledger can only hold by writing the removal down rather than by a test.

**Evidence.**  `npx vue-tsc --noEmit` is clean and `npx vitest run` is 277 passed in 12 files --
the same 277 in the same twelve files, no case added or changed, since no case could see the
rule: no test in this repository loads CSS.  The stylesheet's braces balance 336/336, and
`type-icon` appears nowhere in `desktop/src`.

**What this does *not* establish.**  **Nothing here is a rendering.**  No case in this
repository loads CSS, so the check that found this class dead is a check over the source -- the
two declarations in the stylesheet against every occurrence of the name in `desktop/src` -- and
not an observation of a page.  A class built at runtime from a name the type system cannot
follow would be invisible to it; there is no such construction in this shell today, which is
why the check holds here and would not hold in a tree that had one.

## The Settings Leftovers Checkpoint (2026-09-12)

**What was checked.**  The settings page's 手机 Companion card -- the card the devices checkpoint
recorded as what stayed behind.  It said, in its own note, that starting and stopping the
service, the port and the QR code were on the devices page, and under that note it held one
field.  A card whose heading names a phone, whose content is one number and whose last line is a
pointer at another page is a card the reader has to read twice: once to find out it is not what
they wanted, and once to find out where to go.

**Why the field could not follow the card.**  The devices checkpoint wrote the rule down when it
emptied this card: a field that rides the settings page's `保存设置` has to be on the settings
page, because that is where the button that writes it is.  Moving the last field would have meant
inventing a second save boundary on the devices page for one number -- the shape the AI page's
checkpoint settled, but there it was a card's own form with its own fields, not a stray bound
that belongs to a list the settings page already governs.

**Where it went instead.**  The field is a bound on the history: how many of the newest records
the phone's web page lists, where the two rows above it decide what this machine keeps.  It is in
the 剪贴板历史 card now, third in the run of bounds, and the hint under it is what says which
surface it bounds -- the card's heading is this machine's history and this one is the web page's,
so the row itself has to carry the difference.  The card is not a group of one: it already holds
the retention limit, the retention age and the two filters, so nothing has to justify a card of
its own for a single number.

**What the page is now.**  Eleven cards where the devices increment left thirteen and the AI
increment left twelve.  The 服务 group it sat in held the translation card and this one, and it
holds the translation card alone now: a group label over a single entry is thin, and it is what
is left of a group whose other member moved to the page its controls belong on.  The group is
still worth its label -- a reader aiming for the translation card finds it under 服务, which is
where a translation card is looked for -- but the arithmetic is recorded rather than smoothed
over, because the next card to move out of this page will leave a group with nothing in it.

**What did not change.**  The field's own plumbing: the same `settings.web_history_limit`, the
same `clampNumber(..., 30, 1, 500)` on the way out, the same row and hint.  It is not a new
control, it is the same control in the card whose subject it shares, and the save that writes it
is untouched -- which is the point, and is now asserted rather than assumed.

**Evidence.**  `npx vue-tsc --noEmit` is clean; `npx vitest run` is 278 passed in 12 files -- the
same twelve files, one case added.  The new case reads the DOM three ways: `#settings-companion`
is absent and no rail button is headed 手机 Companion; the field's closest
`section.settings-section` is `settings-history` and it is in no `fieldset`; and after typing 42
into it, the settings page's own 保存设置 is what carries `web_history_limit: 42` to the sidecar.
The rail's cases drop with the card: the "other eleven" in the one-card-at-a-time case is ten,
and the search case's dimmed count is 10 where it was 11.  The stylesheet is untouched -- braces
still 336/336, no rule added or removed -- and one `en.ts` key is gone with the note that used
it, which the i18n case enforces in both directions.

**What this does *not* establish.**  **Nothing here is a rendering.**  No case in this repository
loads CSS, so no case can see the eleven-card rail, the singleton group or the new row's place in
the history card: those are read off the source, and the claim that a bound on the history reads
better under the history heading than under a phone's is a judgement, not a measurement.  The
case asserts that the field rides the page's save; it cannot see the field's row, and it does not
touch the sidecar's own bound check.  Whether the phone's web page honours the number is the
companion acceptance that was already a release gate, and still is.

## The Rail Grouping Checkpoint (2026-09-12)

**What was checked.**  Every card the settings page has left, read for the defect the two
increments before this one were both instances of: a card whose heading names something whose
controls are on another page -- a shell with a note in it pointing elsewhere.  Eleven cards,
read one at a time, with their own controls counted rather than their headings trusted.

**The finding.**  There are no more of them.  The eleven cards hold between two and a dozen
controls each; the longest of them is the maintenance card, and its four buttons are all actions
rather than fields, which is what a maintenance card is for rather than a defect in it.  The
one loose end the audit did turn up is not a card at all but the rail above them.

**The group with one answer left.**  The rail gathers the cards into the questions a reader
arrives with: what this device is, what it talks to, what runs on it, what it keeps, and what it
is running.  The group asking what runs on this machine held two cards, and the phone service
was the one that answered the question -- it is a service this machine hosts.  It left for the
devices page, and the group was left holding the translation card, which is not the same kind
of thing: it configures a service this machine *calls*, with an address and an API key.  So a
label that had one entry under it was also a label whose question that entry did not answer.

**What changed.**  The translation card moved into the group about what this machine talks to,
which is where a reader looking for an outbound service looks, and the group it left is gone.
The rail is four groups over eleven cards, and no group holds a single entry.  The group's own
comment was restated from five questions to four rather than left describing a group that no
longer exists -- the same rule the card headings follow, applied to the rail.

**Evidence.**  `npx vue-tsc --noEmit` is clean; `npx vitest run` is 279 passed in 12 files -- the
same twelve files, one case added.  The new case reads the rendered rail rather than the source:
it collects `.settings-nav-group`, asserts the labels are 通用 / 连接 / 数据 / 系统 in order,
asserts every group's entry count is greater than one, and asserts 翻译 is among the entries of
the connectivity group.  One `en.ts` key went with the group -- `服务` -- which the i18n case
enforces in both directions, since a key whose Chinese no longer appears in any `t()` call has
to go rather than stay as a translation nothing can reach.  The stylesheet is untouched: braces
still 336/336.

**What this does *not* establish.**  **Nothing here is a rendering.**  No case in this repository
loads CSS, so no case can see the rail: the case asserts which entries sit under which label,
and cannot see how the groups read or whether four labels over eleven cards is better than five
over eleven.  That is a judgement, and the one behind it is written above rather than measured.
The reading of every card that produced the audit's negative result is likewise a reading of the
source -- a card whose controls are in a dialog, or in another component entirely, would not be
caught by it -- and the audit covers the settings page only -- the other six pages were read
this way, and came out the same, in the greyed-controls checkpoint after this one.

## The Greyed Controls Checkpoint (2026-09-12)

**The rule this page already had.**  A control the reader cannot use has to say why, in words, on
the surface where they are looking.  The shell learned it twice: the devices page's push-text
button carries a line beside it while the engine is stopped, and the settings page's discovery
card carries one under its toggles.  Both were added because a control the reader cannot explain
is a dead end, and a dead end is what the increment before them was about.

**The finding.**  The devices page has a second control that goes grey for the same reason as its
push button -- the send-URL button on every paired row -- and the sentence beside the push button
named only the push button.  A reader looking at a grey globe in a device row had one sentence
about pushing text several rows above it and nothing tying the two together.  Two controls, one
fact: the sidecar grants `clipboard.push` and `url.send` from a single condition
(`internal/application/bootstrap.py`, the runtime running or paused), so no state exists in which
one is grey and the other usable, and none in which the globe is grey with no sentence about the
engine on the page.

**What changed.**  The sentence is written once, in the devices toolbar, and names both controls:
同步引擎未运行，推送文本与发送网址不可用。  The condition it appears under is the union of the two
capability flags rather than the push flag alone, so a sidecar that ever granted them separately
would make the sentence appear rather than disappear.  Every paired row's send-URL button is
described by that sentence -- `aria-describedby`, pointing at its `id`, and pointing at nothing
once the capability is present -- because a `title` on a disabled control is not read everywhere,
and because the alternative, a reason inside the button's own label, would leave the reason in the
row after the engine came up.

**The audit's other results, which are negative.**  The same question -- is any control grey with
nothing said about it? -- was put to every capability-gated control in the shell, not only this
one, and the four that remain are answered.  The discovery card writes its own sentence.  The
footer's sync toggle has 同步引擎未启动 in the status span directly beside it, which is the same
fact the toggle reads.  `FavoritesView` replaces its whole page with 收藏库暂不可用 when its own
capability is missing, which is the strongest form of the answer.  The last two flags,
`update.status` and `diagnostics.report`, are in the sidecar's unconditional set: they can be
missing only while the whole window is not ready, which the shell's header names in the same
breath (正在启动 / 等待解锁 / 连接异常), so a note in the update card or the diagnostics card would
be a second explanation of one window-wide fact.  The settings page's save button was read the
same way and answers the same: its three reasons are a save in flight, a settings read that has
not finished, and a password field that has not passed its own rules -- and the last of those is
already said by the rule list and the mismatch line in the security card.

**The caveat this closes.**  The rail checkpoint left one: its audit read the settings page's
cards and recorded that the other six pages had not been read that way.  They have been read now,
with the same ruler -- a section whose heading names something whose controls are elsewhere, and a
page whose fields are saved by a button on another page -- and they hold none of either.  Their
sections are their own cards, and the three pages that are components (`FavoritesView`,
`TransfersView`, `ChatView`) keep their own store and their own save where they have one
(`FavoritesView`'s editor).  The store's own capability reads are guards inside load and action
functions rather than bindings on a control, so they have nothing to explain.

**Evidence.**  `npx vue-tsc --noEmit` is clean; `npx vitest run` is 281 passed in 12 files -- the
same twelve files, two cases added.  One case mounts the devices page with a paired device and no
capabilities, and asserts the rendered sentence, that both controls are disabled, and that the
row's send-URL button carries the `aria-describedby`; the other mounts the same page with
`clipboard.push` and `url.send` granted and asserts the sentence is absent, the button enabled, and
the description gone.  One `en.ts` key moved with the sentence -- 同步引擎未运行，无法推送文本。
became 同步引擎未运行，推送文本与发送网址不可用。 -- which the i18n case enforces in both
directions, so the old key could not be left behind as a translation nothing can reach.  The
stylesheet is untouched: braces still 336/336, and no new class was introduced, since the line uses
the `muted small` the push-text line already used.

**What this does *not* establish.**  **Nothing here is a rendering.**  No case in this repository
loads CSS, so no case can see the toolbar row with the sentence in it: the case asserts the
sentence's text and the button's attributes, and cannot see whether the line reads well beside the
button or whether the row is now crowded.  The `aria-describedby` is asserted as an attribute and
not as a behaviour -- jsdom computes no accessible description, and nothing here reads the page with
a screen reader, so what is established is the reference, not what a reader hears.  The union
condition is written for a state that does not exist today: with the sidecar granting the two
capabilities together, the union and the push flag alone would render identically, and the case
that grants one without the other is what pins the difference.  And the audit is a reading of the
source, made by searching `capabilities?.includes` across `src/` -- seven flags in `App.vue`, one
predicate handed to the favorites store, and four guards inside the store's loads and actions.  A
control greyed by something that is not a capability flag at all would not be caught by that ruler;
what the ruler covers is the class the shell had already fixed twice.

## The Copies That Did Not Move Checkpoint (2026-09-12)

**A fact the repository writes in three places.**  A Tauri command is reachable only when three
sites agree, and this repository says so in its own test: "the handler list, the manifest's closed
command list, and the window ACL.  Checked against each other rather than from a hand-kept list,
so a new command that forgets either site fails here instead of at runtime."  Two commands the
renderer calls had missed the middle site.  `choose_folder` is the transfers page's folder picker
-- the way a folder is sent as one archive, with the packing left to the sidecar -- and
`clear_transfer_history` is the 清除 button in the transfers page's history header, which the
clear-history checkpoint added.  Both were defined in `main.rs`, listed in `generate_handler!`,
bound in `src/api/bridge.ts`, called from `TransfersView.vue`, and named in
`capabilities/main.json` as `allow-choose-folder` and `allow-clear-transfer-history`.  Both were
absent from `build.rs`'s `AppManifest::commands`, which is the list the application's own
permissions are generated from.

**How it was found.**  The suite was run for the first time since the transfers work, and it was
red: `cargo test` failed on `every_handler_command_is_registered_in_build_and_acl`, naming
`choose_folder`.  The loop stops at the first gap, so the second command was behind the first --
adding one entry and re-running named `clear_transfer_history` rather than passing.  That is the
shape of this defect class: the test and the manifest and the ACL are three copies of one fact,
and two of them moved while the third did not, so the only thing that could see the divergence was
the one check that reads all three.

**What changed, in commands.**  Two entries in `build.rs`, beside the other transfers commands.

**The same shape in words.**  The native tray's peer rows carry a state word, and the file that
draws them says since it was written that they are "the four words the devices page uses".  Three
were: `离线` / "Offline", `等待确认` / "Awaiting confirmation" and `已发现` / "Discovered" are
exactly what the window's `pairingLabel` and `connectionLabel` write.  The fourth was 已连接 /
"Connected" for a peer that is up, and that is the *window header's* word for this machine's own
link to its engine -- the window calls that peer 在线 / "Online".  So one word named two facts, one
of them about the other machine, and the peer that is merely online without being trusted (a
consented chat session, which `classify` also buckets as `Connected`) was described as connected
when it was not.  The tray now says 在线 / "Online", and the pinned row test says so: 手机（在线） /
手机 (Online).  Its send entry was the same divergence inside one table -- the tray's English said
"Send URL to Device" while its Chinese said 发送链接到设备, legacy's word, and the window's button
has said 发送网址 since it was rebuilt; the tray's Chinese now says 发送网址到设备, so the tray
agrees with the window and with itself.

**What deliberately did not change.**  The peers submenu's own label stays 已连接设备 / "Connected
Devices": that is the roster's name and it is legacy's word, and the comment on the function that
renders it records the one deliberate difference -- the count behind it, which legacy took from
every *known* device and this takes from the connected ones.  The notification and notice strings
for a device that connects keep legacy's 已连接 ("{name} 已连接" / "Device connected"): those are
sentences about an event on a surface whose wording is legacy parity, not state words in a list
beside device names, which is where the two-facts-one-word collision was.

**Evidence.**  `cargo test` is 50 passed, 0 failed; the same run before the two entries was 49
passed, 1 failed.  The i18n assertions in `desktop/src-tauri/src/i18n.rs` still hold: both locales
non-empty, the placeholders kept, and the string count unchanged at 33.  `npx vitest run` is 281
passed in 12 files -- the renderer was not touched, and this checkpoint's only JavaScript-visible
change is none at all.  The parity table's tray row now lists the entry as 发送网址, which is what
the tray draws.

**What this does *not* establish.**  **The application has not been built or run**, so neither
failure mode was observed: whether the missing manifest entry denies the call at runtime or makes
the capability's permission unresolvable at packaging time is unverified, and the fix is justified
by the repository's own stated contract rather than by a reproduction.  The check is a string
search over `build.rs`, so a command listed there and missing from `generate_handler!` would still
pass it -- the contract is checked in one direction only.  And the tray's words have not been seen
in a real tray on any platform: what is established is the catalog entry and the row test, not how
手机（在线）reads in a native menu.

## The Unanswered Name Checkpoint (2026-09-12)

**The contract this is about.**  `app.status()` answers with a `capabilities` list, and the window
reads it before it draws: seven of the shell's controls are `ready && capabilities.includes(...)`,
so a string in that list is a promise that the thing behind it can be done.  The list is a list of
RPC method names, and it is meant to be exactly the methods the dispatcher routes.

**The finding.**  One name in it answered to nothing: `chat.resend`.  Resending a failed chat
message had been built as a verb of `chat.action` -- `chat.action` carries the action `"resend"`,
`resend_chat_text` sends that verb, and the list already contained `chat.action` -- so the ability
was reachable the whole time under the other name.  What was wrong was the promise: a client that
gated a control on `chat.resend` would have got a permanently false flag and a button that never
came alive, which is the defect the two checkpoints before this one were about, one layer down
where no reader looks.  It also hid well: `chat.resend` is an i18n key in the phone panel's own
locales (`重新发送`), so a search for the name lands on two translation files before it lands on
the list.

**What changed.**  The name is gone from the list, and the list now says above itself what it is
-- every entry is a method the dispatcher routes and nothing else -- with the one that had drifted
named, since the entry that explains a rule is worth more than the rule alone.  The removal was
not replaced by a method: resending is a verb of an existing method, and a second method for one
verb would be the copy that drifts next.  This is the same call the shell's i18n case enforces in
the other direction, where a translation key whose Chinese no longer appears in any `t()` call has
to go rather than stay as a translation nothing can reach.

**The guard.**  `tests/sidecar/test_rpc.py` gains a case that reads the advertised capabilities
back against the dispatcher's own source, the way the desktop's `every_handler_command_is_registered_in_build_and_acl`
reads `main.rs`: a hand-kept list of methods would have to be updated with the dispatcher, and that
is the copy that drifts.  It knows the four shapes the dispatcher uses -- `method == "x"`,
`method in (...)`, a dispatch table's keys, and the families it routes by `startswith` -- plus the
favorites table, which lives in its own module.  Both readings are guarded against matching
nothing (`> 60` capabilities in this fixture, which has no runtime up, and `> 80` dispatch
positions).  It was verified by putting the name back: the case failed with
`advertised but not routed by the dispatcher: ['chat.resend']` and the name was removed again.

**The sweep's negative results, which close the last checkpoint's caveat.**  The three command
sites agree in both directions now: 106 commands in `generate_handler!`, 106 in the manifest,
106 `allow-*` permissions in the ACL, with no entry on any side that the others lack -- the
reverse direction the previous checkpoint recorded as unchecked.  Every RPC-shaped literal in
`main.rs` outside its tests -- 88 distinct method names -- has a dispatch position in the
sidecar; the only other strings of that shape in the file are three file names its own tests
use (a log file, `config.toml`, and `main.rs`, which the three-site test above reads back).
The two names on the dispatch side that the host never sends are `favorites.changed`, which the sidecar publishes rather than answers, and
`app.shutdown`, which is implemented and covered by `test_handshake_status_and_shutdown` and which
nothing currently calls -- the host stops the sidecar by closing its stdin, which the ledger
records as the shutdown mechanism.  A route nothing takes is not the same defect as a route
nothing can answer, so it stays.

**Evidence.**  `python -m pytest tests/sidecar -q` is 725 passed; `tests/sidecar/test_rpc.py`
alone is 176 passed, one case more than before.  `tests/test_recovery.py`, the one legacy module
that reads the sidecar's own contract, is 24 passed.  The desktop was not rebuilt for this: no
Rust and no renderer file changed, so `cargo test` 50 passed and `npx vitest run` 281 passed stand
from the increment before.

**What this does *not* establish.**  The guard reads dispatch *shapes*, so a method routed by a
shape it does not know -- a name computed at runtime, a table built elsewhere -- would be read as
unrouted; the failure direction is a false alarm rather than a missed defect, but the check is not
a proof that the dispatcher's surface is exactly this set.  Nothing here runs the two processes
together: the capability list is read from a sidecar started in a test, and no window ever
received it, so what is established is that the list and the dispatcher agree, not that a control
in the running application follows.  And the capability list is still only asserted a member of by
the runtime tests, not compared as a set, so an entry added for a method that exists but can never
succeed would pass every check here.


## The Field Chrome Checkpoint (2026-09-12)

**The complaint, and what it turned out to be.** "Every text box in the application has a layout problem." The
window's form controls are drawn by a handful of scoped rules, and the accounting is by *container*: the unlock
screen, the history search chip, the toolbars, the favourites editor, the settings panel and every
`.settings-section` card each carry a rule that gives the controls inside them a border, a fill, a radius and
padding. Two groups of containers had none, and both were audited by walking every `<input>`, `<textarea>` and
`<select>` in `App.vue` and the three views against the selectors that cover them.

**The dialogs.** Six dialog fields -- the send-URL address and its target picker, the pushed-text box, the
translator's two language pickers, the log line count -- were drawn with no border, no fill and no padding, so
each was a line of text on the dialog's own surface with nothing saying it could be typed into. The labels were
the second half of the same defect: they are `<label>网址<input/></label>`, which is correct markup and reads
correctly only if the label is a stacked block; with no rule of its own a label is inline, so the word ran
straight into the box beside it. `.modal label` is now `display: grid` with a gap, and `.modal
input:not([type="checkbox"]):not([type="radio"]), .modal select, .modal textarea` restates the card's own chrome
at the dialog's width. The migration wizard inside the AI dialog needed a third rule: its rows carry the
settings cards' own `.setting` markup, whose two tracks are sized by `--setting-name`/`--setting-gap` -- both
declared *on `.settings-section`*. Inside a dialog the variables are undefined, so `grid-template-columns`
computed to `none` and the row's `grid-column: 2` summoned an implicit second track, pushing the control off
the dialog's edge. A dialog is 420px where a card is over 700; the honest fix is to stack those rows rather
than to import a name column there is no room for, so `.modal .setting` sets one track and both parts are
placed in it. `.modal .setting-control { grid-column: 1 }` (0,2,0) outranks the card's `.setting-control
{ grid-column: 2 }` (0,1,0), and `.setting--check .setting-control { display: flex }` still applies because it
sets a different property.

**The composer.** `ChatView.vue`'s message box was the other one: its scoped rule set only `flex: 1;
min-height: 44px; resize: vertical`, so it was a bare area of the page's surface with a caret in it. It now
takes the same border, radius, surface fill and padding as every other field, a `max-height` so a long draft
scrolls instead of growing the page, and a placeholder colour.

**Evidence.** `npx vue-tsc --noEmit` clean; `npx vitest run` 291 passed across 13 files. No test covers the
chrome itself: jsdom assembles the DOM and never lays it out, so a rendered assertion would read `v-show`'s
inline `display` and say nothing about a border. What is pinned is the markup the rules select on, through the
tests that already reach these dialogs.

**What this does *not* establish.**  No CSS was executed. Every claim above is a reading of the rules and of
where the elements sit in the tree -- which container each field is a descendant of, and what the cascade
resolves to -- not an observation of a laid-out window. A rule that is overridden by something this pass did
not read, or a field added later inside a container that carries no chrome, would both survive it. The
`.modal` rules were checked against the selectors in `styles.css` only; `FavoritesView.vue` and
`TransfersView.vue` share the `.modal` class, and their dialogs hold no labels or fields, so nothing there
changed -- which is a reading of those two files, not a test.


## The Route on a Record Checkpoint (2026-09-12)

**What the row could not say.** A history row has named the *device* a clip synced from since the provenance
increment, and the devices page has shown, per device, whether its local link is up and whether its internet
pairing is up. Neither answers the question the list raises: this row arrived from "Next room" -- over the
cable, or over the internet? They are different facts, and the device name cannot imply either, because a peer
paired both ways sends over whichever path is up and the relay is the fallback for the other. The route is now
recorded with the clip and shown as a chip beside the name.

**Where it is recorded.** `ClipboardContent` gains `transport`, one of `"lan"`, `"relay"` or `""` -- and a
third value was added later, by "The Route on a Pushed Row Checkpoint" below, for a clip pushed from this
machine's own web panel.
`SyncManager.handle_remote_message` takes a `via_relay` flag and stamps it at the same point it stamps
`source_device` -- one line away, because it is the same kind of fact about the same arrival; the relay router
`_receive_relay` passes True and the LAN router leaves it False, so the router that called decides the route.
Empty means "nothing to say": the clip was captured here, or the row predates the column. There is deliberately
no `"local"` value. `source_device` is already empty for a clip captured here, so a second field carrying the
same distinction would be a copy that can disagree; and the merge rule needs the empty, because a re-copy made
locally folds into the newest entry and must keep the route the *content* arrived on, exactly as it keeps
`source_device`. `ClipboardContent` is rebuilt in two places -- the plain-text downgrade and the app filter --
and both now carry the field through, or a filtered clip would lose the route it arrived with.

**Where it is kept.** The `history` table gains a `transport` column with the same `ALTER TABLE` guard the two
columns before it use, and its default is the honest answer for every row already on disk: a row that says
nothing about how it arrived beats one guessing LAN. Every write path carries it -- the incremental
`_insert_row`, the full resync in `_save`, the legacy-JSON migration, and `_persist_entry_update`, the last
because a merge can be the moment a route is first known. The read path appends it to the SELECT with the same
`len(row) > n` defence the columns before it use, so a database read by an older build of this code degrades to
"no route" rather than raising out of the load.

**Every copy of the fact, moved.** `history.list`'s DTO carries it, which is the whole of the RPC surface: the
method returns the DTO as built, so nothing in the dispatcher changed. The desktop types it as *optional* --
a window running against a sidecar that predates the field gets no key at all, and a row with no route shows
no chip rather than a guess. The JSON export, which is the documented backup format and what `backup.py`
archives, and the CSV carry it in both directions; a file written before the column existed simply has no key
and imports back with no route. The Markdown report prints the route beside the source it already printed,
because a reader comparing that file against the window needs the same pair of facts.

**The chip.** It is the devices page's own `.channel` element and its own two words -- 本地 with a plug,
互联网 with a globe, the same glyphs and the same titles -- so one vocabulary covers both pages for the same
two links. (A third word was added later, by "The Route on a Pushed Row Checkpoint" below, for the one route
that is not a peer link; the two peer links are still these two.) Neither route is painted as the exception: a clip that arrived over the relay is not a fault, so the
two differ by their glyph and never by a warning colour. It appears only when `transport` says something, which
is why a locally captured row and a row from before the column look the same: both have nothing to report.

**Evidence.** `python -m pytest tests/sidecar -q` is 732 passed, two cases more than before; the two are a new
`tests/sidecar/test_lan_runtime.py` case pinning that a chat dial suppresses the pairing offer while an
ordinary dial does not, and the DTO key-set assertion in `test_rpc.py` extended with the new name. `npx
vitest run` is 291 passed, one more than before: a new `app.test.ts` case rendering four rows -- relay, LAN,
no route, and an empty route -- and asserting both the chips that appear and the two that do not.

**What this does *not* establish.**  No relay and no LAN connection was made. The route is asserted through a
flag the routers pass, so what is pinned is that the two routers pass different ones and that the flag reaches
the stored row -- not that a frame which really travelled over an MQTT broker is labelled `relay`. The legacy
`src/main.py` application is unchanged and writes rows with no route, so a user running it sees no chip; that is
the migration's direction rather than a defect, but it means the two applications write the same table with
different provenance until the legacy desktop is removed. The phone's push panel was also left with no route by
this checkpoint: it writes through `internal/web/api/history.py` rather than through the sync manager. "The
Route on a Pushed Row Checkpoint" below gives that row the answer the path can honestly give -- that the clip
came in over this machine's own web server, a third route rather than a peer link -- and records the part that
stays unknowable: whether the browser was on this network or reached the machine through a proxy is not in the
request, so no later checkpoint can recover it from this path.


## The Route on a Pushed Row Checkpoint (2026-09-12)

**The last row with no route.** The checkpoint above recorded the gap in its own words: the phone's push panel
writes through `internal/web/api/history.py` rather than through the sync manager, so the row it records has a
source and no route -- and its source name says only "Web". A clip pushed from the panel is the one row whose
provenance the reader cannot reconstruct from anything else on it.

**The answer that path can give.** The push handler stamps `transport = "web"` where it builds the content.
That is a third route, and it is deliberately not either of the two peer links: a plain HTTP request does not
carry the path it took, so a browser on this network and one reaching the machine through a tunnel or a proxy
are indistinguishable here. Labelling it `lan` would be a guess presented as a fact, which is why the value
names the one thing the path does know -- that the clip arrived through this machine's own web server.

**Why stamping it is safe.** The content object is both recorded and broadcast, so the value rides the outgoing
`SyncMessage` -- and the receiving `SyncManager.handle_remote_message` overwrites `transport` unconditionally,
one line from where it overwrites `source_device`. A sender's own value for either field is exactly what the
receiver replaces, so no peer's row can inherit "web", and the wire value is inert by the same rule the existing
provenance fields already rely on.

**The third value everywhere the other two went.** `internal/clipboard/format.py` names it where the field is
documented; `internal/data/export.py`'s route map gains `"web": "web push"` beside "local link" and "internet
relay", so the Markdown report a user compares against the window uses the same vocabulary; `history_db`'s
column comment, `history.py`'s web API and `use_cases/history.py`'s DTO comment each enumerate three routes
instead of two.

**The window's chip, which had two cases.** It read `relay` and everything else, so a pushed row would have
been drawn as 本地 -- telling the user the phone was on this network, when the panel exists precisely so that it
need not be. The chip's word, title and glyph are now a lookup over the three routes: 本地 with a plug, 互联网
with a globe (both unchanged, and the same ones the devices page uses), and 网页 with a phone for the panel push.
An empty route still draws no chip at all, which is what a clip captured here and a row written before the
column have.

**The panel's own rows.** The phone's list named the device and nothing else, and the phone is the surface where
the question is asked most: it is the device that can be on either side of the relay. `get_history` now ships
`transport`, `history-item.js` draws a chip when the row has one, `index.html` styles it muted rather than
accent-tinted so it does not read as a second device badge, and both panel locale files carry the three words.

**Evidence.** The main suite is 1640 passed / 4 skipped, the sidecar suite 749 passed, the desktop suite 300
passed in 13 files, `vue-tsc --noEmit` clean, and the panel's `node --check` clean. Fifteen temporary reverts,
each of which made one of the new cases fail: the stamp, the stored column, the field's default, the export
label, the DTO and the window's three chip cases on one side; the panel's field, chip, chip rule, two of its
three route words and its locale file on the other. One was not load-bearing in its first form -- the Markdown
case passed every route in through the `transport` argument, so flipping the field's default changed nothing it
looked at -- and the row standing for a clip captured here is now built the way the clipboard monitor builds
one, with no route passed at all.

**What this does *not* establish.** No real browser and no phone were involved: the push was driven through the
handler with a stubbed writer, so what is pinned is the value the row is given and the words the three surfaces
draw from it -- not that a phone on mobile data produces it. Whether a push came from a browser on this network
or from one reaching the machine through a proxy is still unknowable from that path, and is now recorded as
unknowable rather than as either route. A push also still did not broadcast `history_updated`, so the panel
that pushed saw its own row on the next load: pre-existing at that point, unaffected there, and closed later the
same day as the smallest case of a larger one -- see "The Panel's Own Live History Checkpoint" below. The legacy
`src/main.py` application still writes rows with no route, so a user running it sees no chip on any row -- the
migration's direction rather than a defect, and unchanged by this checkpoint.


## The Chat Invite That Offered to Pair Checkpoint (2026-09-12)

**What the click did.** Clicking a device in the 附近聊天 list dialed it through the runtime's generic
`_connect`, which is the dial that sync and explicit pairing use -- and that dial offers the shared pairing
code to a peer that is not paired yet, because offering it is how two machines on this network come to trust
each other. So a code appeared on both screens for a consent neither side had given. The list is every device
the network advertises, including the ones nobody has paired with, so the first click on a freshly discovered
machine was the one that did it.

**What legacy did.** Both of the legacy chat-start paths -- the window's and the web panel's -- called
`connect_to_peer(..., no_auto_pairing=True)`, and `TransportManager.connect_to_peer` documents that parameter
as "set by the nearby-chat flow", with the reasoning spelled out: "an unpaired peer connected purely for a
consent-gated chat must NOT be auto-offered a shared pairing code (chat has its own invite/accept +
fingerprint consent)". The parameter survived the migration; the call site that used it did not.

**The fix, and its shape.** `LanRuntime._connect` takes `no_auto_pairing=False` and passes it through, and
`_connect_and_wait` forwards it; `_chat_invite` -- the only chat dial -- passes True. The default is the part
that matters and is why the flag is a parameter rather than the new behaviour everywhere: the other six
`_connect` call sites are the discovery, reconnect and device-list dials, and *those* must keep offering the
code, or a first pairing could never start from the one place a user goes looking for it.

**Evidence.** `tests/sidecar/test_lan_runtime.py` gains `test_a_chat_dial_does_not_offer_to_pair_but_an_ordinary
_one_does`, which drives a chat invite and then the device list's own connect through the suite's transport stub
and asserts the flag was `True` then `False`. The stub records it rather than swallowing it, so the pairing
offer cannot come back unnoticed, and `**kwargs` was added to the two per-case dial stubs that replace it.
`tests/sidecar/test_lan_runtime.py` is 132 passed; the suite is 732 passed.

**What this does *not* establish.**  No two machines were connected. The assertion is that the flag reaches
`connect_to_peer`, not that a real dial to an unpaired peer produces no code -- the code is generated inside
`connection.py`'s TLS handshake, which needs a peer on the other end of a socket and a certificate, so what is
pinned is the branch condition being met on the way in. And nothing here establishes that a chat with an
unpaired peer then works end to end: the invite rides the freshly dialed link, which the pre-existing
`test_chat_invite_dials_an_idle_peer_before_inviting` covers for a *paired* peer, and the unpaired case is
covered only to the extent that the dial is now made the way legacy made it.

## The Pairing That Never Answered Checkpoint (2026-09-12)

**What the user saw.** 在互联网配对页面提交配对码之后，界面只显示「已提交配对码」；没有任何成功或失败的
提示，其他已配对的互联网设备也不出现。

**Why it could not have worked.** Three independent defects sat on the one path the exchange exists to
walk, and any one of them alone was enough to stop it.

- *The pending code was not in the topic-to-secret lookup.* Generating a code puts its secret in
  `_pending` and the relay is subscribed to `netpair_topic(secret)` -- but the lookup that turned an
  incoming topic back into a secret read `config.netpair_secrets` alone. The hello answering a generated
  code therefore arrived on a channel this machine was listening to and was discarded for want of a secret,
  silently, on both machines. `channels()` and the new `secret_for_topic()` now both read `_all_secrets()`
  -- the pending codes merged with the persisted pairs -- so what we subscribe to and what we can decode
  are the same set by construction rather than by two lists agreeing.
- *The role decision had the generator's half only.* A 12-character code carries a 4-character tag derived
  from the generating device's id, so the receiver can tell which half it is: `incoming_tag == our tag`
  means we generated the code, `incoming_tag == our real device_id` means we entered one and are waiting.
  Only the first branch existed. The machine that *entered* a code never re-keyed its provisional entry to
  the sender's real device id, so it kept a 4-character tag that could never become a device; and the
  machine that *generated* the code never dropped the pending code it had just had answered.
- *The reply was addressed with the wrong name.* `_publish_hello` put our own 4-character tag in the frame's
  `peer_id`, which is the *recipient's* name on the wire. The one frame that completes the handshake
  therefore named, to the only machine waiting for it, a device that machine could not recognise as itself.

Taken together the exchange could not complete from either side, in either direction.

**And why even a working handshake would have shown nothing.** Three more defects sat in the surface. The
service's `status()` silently dropped provisional keys, so between the submit and the hello the card
displayed nothing at all -- and the line it did leave behind, 已提交配对码, described the click and then
never changed again, whether the pairing completed or nobody ever answered. The desktop never re-read the
status on `netpair.peer.changed`, so a pairing that completed while the reader was looking at the page
showed nothing until they left the page and came back. And the refusals the sidecar *does* return --
`INVALID_PAIRING_CODE`, `RELAY_OFFLINE`, `INTERNET_SYNC_OFF`, `SAVE_FAILED` -- fell through to the window's
top band, in English, at the opposite end of the window from the box holding the code.

**The fix.** `internal/infrastructure/runtime/internet_pairing.py` grows `_all_secrets()`,
`secret_for_topic()` and `handle_hello(source_device, incoming_tag, name, topic)`, which is the whole role
decision in one place: it resolves the secret from the topic, refuses a frame naming this machine or
carrying no source, decides generator-or-enterer, re-keys the entry to the sender's real device id, drops
the answered pending code when it generated one, notes the hello and saves. It returns the role and whether
a reply is owed, and `lan.py`'s `netpair_hello` branch is now a call to it plus the publish -- the branch
had been resolving the secret itself, against the wrong map. `_publish_hello` addresses the frame to the
recipient. `status()` reports the provisional keys under a new `waiting` list rather than dropping them,
with the time the code was submitted, and `unpair` clears that wait.

On the surface: `InternetPairingWait` and an optional `waiting` on `InternetPairingStatus` in
`desktop/src/api/types.ts`; `enterInternetPairingCode`'s answer is typed as the half-done thing it is
(`{ peer_id, waiting? }`); the store keeps the frame in `netpairEvent` so the card can re-read on it, and
names the moment in a notice (`已与 {name} 完成互联网配对`); the card shows the wait as a row of its own
-- a clock, 等待对方确认… or 等待 {name} 确认…, the submit time on hover, a 撤销 button -- beside the peer
list rather than among it, because a 4-character tag cannot be reached, sent to or renamed; and the four
refusals are mapped to Chinese beside the field, in the form's own line. The phone host
(`internet_panel.py`) passes `waiting` through its status payload for the same reason, though the phone's
panel does not render it yet.

**Evidence.** `tests/sidecar/test_lan_runtime.py` gains two cases, one per direction: a hello answering a
code this machine generated leaves `netpair_secrets == {"b1b2b3b4b5b6": secret}`, no pending code, no wait,
exactly one published hello named to the sender and one `netpair.peer.changed` `paired`; and entering a
code re-keys only when a reply naming this device comes back, publishing nothing of its own the second
time. Both were re-run against a temporary plugin that reverted all three defects and both failed for the
right reasons (`assert {} == {'b1b2b3b4b5b6': '22KNVPZ'}`; `assert 'BT9C' == '9EVR'`), so the cases pin the
bugs rather than the new code. `tests/sidecar/test_companion.py`'s confirmed-peers-only case now also
asserts the provisional tag arrives under `waiting`, which is what caught the phone host reducing the
payload to two keys. The desktop gains one `app.test.ts` case for the whole visible half: no wait row
before, a wait row and 提交时间 after submitting, a re-read and a notice when `netpair.peer.changed`
arrives while the page is open, and a refused code answered beside the field with no error band. Sidecar
suite 734 passed; desktop suite 292 passed in 13 files; `vue-tsc --noEmit` clean. A latent fixture gap came
out with it: the app suite's `companionStatus` was an unresolved `vi.fn()`, and opening the devices page
reads it whatever sub-tab is showing, so every devices-page case ran with a spurious band reading
"cannot read properties of undefined". It is resolved now, and the new case asserts the band's absence.

**What this does *not* establish.**  No two machines were paired. Both halves of the handshake are driven
in-process through the suite's transport stub with hand-built frames, so what is proven is the decision
each frame lands on and the payload that leaves -- not that a real broker delivers a hello between two
installations, which needs a real relay and two machines and stays a release gate. The `topic` argument is
resolved through `netpair_topic` over the stored secrets, so a frame arriving on a topic no secret of ours
derives is ignored; but `_receive_relay` still has no equivalent of legacy's `_relay_topic_identity`
verification, so a frame that reached the right topic is trusted on the strength of the secret alone, as
before. The new stack also never sweeps provisional keys at startup (legacy's `_netpair_drop_provisional`),
so a code entered and never answered stays visible and removable rather than being swept -- which is the
better of the two on a panel that now shows it; it is a deliberate divergence, and the kept-wait
checkpoint below pins it with tests. The phone's
web panel received `waiting` with no display for it at the time of that checkpoint; the waiting
checkpoint below adds the row. And the desktop-side file drop target
(`dragDropEnabled` plus `onDragDropEvent`) was unimplemented at the time of this checkpoint; the
file-drop checkpoint below is that target.
## The Channels We Listen On Checkpoint (2026-09-12)

The increment before this one recorded, in its own "what this does not establish", that `_receive_relay`
had no equivalent of legacy's `_relay_topic_identity` check -- a frame that reached the right topic was
trusted on the strength of the secret alone. This checkpoint is that check, plus the three neighbours it
turned out to be sharing a defect with, all on the surface that answers one question: which relay channels
does this machine listen on, and who may speak on them?

**What was wrong.**  Subscribing and publishing were not the same list. `channels()` built a netpair entry
per secret and nothing for the LAN-relay-enrolled family, so a device paired over the local network and
currently away from it was published *to* and never heard *from* -- its frames arrived on a topic this
machine had not subscribed to, which reads exactly like a peer that is simply not there. The netpair key
was derived from `netpair_password` alone, which is the same string as the user's passphrase only while
something has mirrored it: a config loaded from 1.x carries the old plaintext `encryption_password` and no
`netpair_password`, so the two ends of a pairing derived different keys from the same code and every frame
on the channel failed to decrypt -- the pairing reported success and nothing ever synced, with no error
anywhere. And a frame's self-declared `source_device` was believed on any topic whose secret we held, so
one paired peer could publish with a second paired device's id and have everything it sent -- clipboard
content, chat, receipts -- filed against a device that holds a pairing it never used. Around those, two
smaller ones: a receipt was routed before any binding, so an ack resolved against the source the frame
named rather than against the owner of the channel it arrived on; and only the handshake touched the
last-seen stamp, so a peer that synced all afternoon still read 离线 ninety seconds after the hello it
happened to have sent.

**What changed.**  A topic is derived from a shared secret, so the channel -- never a frame's
self-declared sender -- now names the peer that may speak on it. `topic_identity(topic)` answers the same
three cases legacy answered: a confirmed netpair pair or an enrolled pair names its owner's device id; a
netpair channel still keyed by a provisional 4-character tag names that tag, because that is all the code
ever carried; and a `pending:CODE` channel binds nobody, because the entering partner's identity is
established by its confirmation hello and by nothing else. `channels()` covers both families -- the
enrolled one derived from `derive_topic(our secret, theirs)`, one entry per enrolled pair, skipped when the
peer also holds a netpair pairing so nothing is subscribed to twice -- and `ensure_relay_secret()` is now
the one home for this machine's own relay secret, generated and persisted on first use, where it had been
generated inside the clipboard mirror's own loop. Every netpair key in the runtime derives through
`netpair_password()`, which is the one expression legacy used (`encryption_password or netpair_password`),
read fresh so a settings change reaches the channel keys without a restart. Finally, the same frame that
proves a peer alive is also the self-heal legacy called F04: the machine that entered a code stores a
provisional 4-character tag before a confirmation hello that is a single best-effort publish, and a lost
one used to leave that tag-keyed entry permanent -- a phantom "paired" device with no name and no real id,
which could be sent to and never answered. Any later frame re-keys it, on the same proof the hello would
have offered.

**Evidence.**  Seven new cases in `tests/sidecar/test_relay_delivery.py` (the suite 734 to 741), six
of them re-run against a temporary revert of the fix they cover: removing the bind attributes a frame
to the device it claimed rather than to the channel's owner; removing the self-heal leaves the phantom
tag in `netpair_secrets`; removing the enrolled family drops that channel from the map entirely;
deriving the key from `netpair_password` alone loses the user's password; and removing the last-seen
stamp leaves the peer 离线 under its own traffic. Sidecar suite 740 passed (88.17s); a re-run of the
same tree with one more case in it reported 740 of 741, the single failure being the pre-existing
load-sensitive cross-process wait in `test_runtime_integration.py` (`Timed out waiting for runtime
state`), which passes alone and on re-runs. Ruff clean on the changed modules and the test file, with
`lan.py` keeping only its pre-existing finding.

**What this does *not* establish.**  No broker and no second machine were involved. The channel map, the
identity a topic binds and the source a frame is attributed to are all proven in-process against a fake
relay that records what it was handed, so what is proven is the decision, not the delivery. Relay
enrollment was not ported at the time of that checkpoint; the section below ports it, with a terminating
answer in place of legacy's unbounded one. A held netpair
secret still entitles its holder to claim any device id in a `netpair_hello`, which is legacy's behaviour
as well and is recorded rather than repaired. The phone's web panel received `waiting` with no display
for it at the time of that checkpoint, which the waiting checkpoint below adds. And the desktop-side file drop target (`dragDropEnabled` plus `onDragDropEvent`) was
unimplemented at the time of this checkpoint; the file-drop checkpoint below is that target.

## The Relay Enrollment Checkpoint (2026-09-12)

This checkpoint closes the one item the previous section named as not ported. The relay-enrolled family has
been subscribed to and published on since "The Channels We Listen On"; what was missing was the exchange
that hands each machine's secret to the other, without which the family is a channel neither end can derive.

**What was wrong.**  Legacy's `_handle_relay_enroll` answers unconditionally, and its answer is another
`relay_enroll` over the same LAN link -- so two machines running it answer each other forever, each answer
provoking the next on a link that is already up, with nothing in the protocol to stop it. Its only send site
is `_start_internet_sync`, which runs once as the relay comes online: a peer that was not connected at that
instant was never enrolled, nothing retried, and the enrolled channel stayed reachable in one direction
only.

**What changed.**  The frame and its wire shape are unchanged; the answer now terminates.
`InternetPairingService.handle_enroll` stores the peer's secret -- 64 hex characters, checked on receipt,
because the value becomes the topic this machine subscribes to, and a short or non-hex one would be a channel
nobody can derive: a pairing that reads enrolled and syncs nothing, with no error anywhere -- and answers
when the secret is new to us, a rotation or the first we hear of it, or when we have not yet offered ours,
which `_enrolled` records and only a send that actually went out writes. A normal exchange is three frames
and stops: A offers, B stores and answers, A stores and answers, and B sees an unchanged secret from a peer
it has already offered to. The offer travels on the LAN link alone, because the frame carries the secret that
decides which public topic this machine listens on, so the one transport that may carry it is the one where
the peer is pinned by a certificate rather than by a key both ends already share; a `relay_enroll` arriving
over the relay is refused for the same reason, and legacy's send has no relay fallback either, so nothing a
real peer does is lost. Storing a secret refreshes the relay's channel map before the answer goes out, as
legacy did. The offer itself is sent when the relay comes online -- legacy's one-shot hook -- and again
whenever a peer's LAN link comes up, which closes the hole that hook left without adding a timer: a peer that
starts later is enrolled when it appears.

**Evidence.**  Five new cases in `tests/sidecar/test_relay_delivery.py` (the suite 741 to 746), each re-run
against a temporary revert of the line it covers: without the terminating guard a repeated frame draws a
second answer; without the validation six malformed secrets are stored; without the relay refusal a
relay-delivered enroll stores a secret and is answered; without the arrival hook nothing is offered when a
peer appears; and without the internet-sync or pairing guard a device is taught the secret with no relay to
answer on and no pairing to pin it. Sidecar suite 746 passed (93.64s). Ruff adds no finding.

**What this does *not* establish.**  No broker and no second machine were involved. Both ends of the
exchange are proven to reach the right decision in isolation, against a fake transport that records what it
was handed, not to travel. The "we have not offered ours yet" half assumes that a successful offer was
persisted by the peer that received it, which is what the frame is for and a property of the design rather
than a case these tests can reach. A held netpair secret still entitles its holder to claim any device id in
a `netpair_hello`, which is legacy's behaviour as well and is recorded rather than repaired. The phone's web
panel received `waiting` with no display for it at the time of this checkpoint; the waiting checkpoint
below adds the row. And the desktop-side file drop target stayed
unimplemented at the time of this checkpoint; the file-drop checkpoint below is that target.

## The Waiting Code Checkpoint (2026-09-12)

The backend has reported `waiting` -- the codes entered on this machine that nobody has answered yet -- since
the internet-pairing handshake fix, and the desktop window renders it. The web UI's Devices page did not: its
store normalized `peers` and `generated_code` and dropped the key, so the page showed nothing at all between
submitting a pairing code and the partner's reply. That is the window in which the user is looking for an
answer, and it is the one the original report described.

**What changed.**  `store.js` mirrors the list as `internetPairWaiting`, normalized to `peer_id`, `name` (the
partner's name once its hello lands, empty until then) and `since` (epoch seconds, or null when the entry
survived a restart -- the clock is lost, the wait is not). It is assigned rather than merged, so a fetch that
no longer reports a row is what ends the wait, and a backend that predates the key clears the list rather
than leaving it frozen; a `netpair_peer` confirmation refetches, because the backend is what re-keys the
provisional 4-char tag to the peer's real device id. The Devices page renders a row per wait above the paired
list: who it is waiting for (the tag until the partner's hello supplies a name, which is what the user has to
compare against what they typed), how long ago the code was submitted, and a Cancel that goes through the
same unpair the peer rows use, so a code typed for the wrong device has a way back out. A wait with no clock
shows no age, rather than `relTime`'s "never synced" -- a sync message about a pairing that has not happened.
The four new keys are web-only, like the rest of the netpair panel family, so the pinned web-only locale gap
in `tests/test_history.py` moves 706 to 710.

**Evidence.**  Five new cases in `tests/test_devices.py` -- the keys in both locales with the `{name}`
placeholder, the store's mirror and its end-on-confirmation, the panel row with its way out and its no-clock
fallback, the row's styles, and a `node --check` over the two files -- each re-run against a temporary
revert of the line it covers, eight reverts in all. The probe harness now asserts the tests pass unmodified
before it starts, because the first round-20 run passed inspection with a broken assertion of its own: a
reversion proves nothing against a test that was already failing. The legacy suite also caught the pinned
locale gap, which is what that count is for.

**What this does *not* establish.**  No phone and no partner were involved: the row is exercised against the
store and the payload shapes, not watched on a device, and nothing here proves the backend's `waiting` list
reaches a real browser. The standalone companion page (`mobile.html`) does not call the internet-pairing
routes at all, so it has no waiting state to show -- the phone-facing surface this covers is the web UI,
which is what a phone browser is served. A held netpair secret still entitles its holder to claim any device
id in a `netpair_hello`, which is legacy's behaviour as well and is recorded rather than repaired. And the
desktop-side file drop target stayed unimplemented at the time of this checkpoint; the
file-drop checkpoint below is that target.

## The Kept Wait Checkpoint (2026-09-12)

Legacy swept the provisional entry.  `_netpair_drop_provisional` ran when the config was loaded and
dropped every tag-keyed secret -- the entry written when a machine enters somebody else's pairing code
and their confirmation hello has not arrived -- on the two grounds its docstring gives: it is "a
few-seconds-long placeholder", and it is one "the UI cannot show or remove".  The port kept the shape
of that behaviour for the generated-code half (in memory, with a TTL) but not the sweep, and the
earlier checkpoint recorded that as a divergence that was not tested.

**What changed.**  The divergence is now deliberate and pinned.  Legacy's second reason stopped being
true when the panel grew its waiting row, and the first was never true for the user: a code entered
and never answered *is* the wait the row reports, and the row ends when the partner's hello arrives or
the user cancels it, not when the process restarts.  So the entry outlives the process: `status`
reports it under `waiting` with an empty name and a null `since` (both were the dead process's) and
never among the peers (a 4-char tag cannot be sent to), `channels` keeps listening on the topic that
code derives, and a hello arriving days later is accepted, re-keys the entry to the device that really
answered, and completes the pairing -- which legacy's sweep made impossible, leaving re-typing the code
on both machines as the only way back.  The re-key is guarded twice, by `note_relay_source` for any
frame from that device and by `handle_hello` for the hello itself.  The cost the sweep avoided is real
and is acknowledged: a code somebody read off a screen stays claimable until the user ends it, where
legacy's bound was the incidental one of the next restart.  Here the bound is the row and its Cancel,
which goes through `unpair` and drops the secret, the topic and the wait together.

**Evidence.**  Two cases in `tests/sidecar/test_relay_delivery.py` -- the entry a previous run left
behind is listed with no name and no clock, still on its channel, and gone from all three after a
cancel; and the late hello completes it -- each re-run against a temporary revert of the lines it
covers, five reverts in all.  The probe for the re-key reverts both of its guards, because reverting
one of two redundant guards proves nothing: the first version of that probe passed while the test it
was meant to break was still being rescued by the other path.  The probe harness asserts both cases
pass unmodified before it starts.

**What this does *not* establish.**  The restart is modelled by the state a restart leaves -- the
secret in the config, the clock and the name gone with the process -- rather than by an actual
process restart, so nothing here proves the secret survives a real config write and reload on disk.
No broker is involved: the late hello is a frame the test hands to `_receive_relay`, so what is
proven is the decision, not the delivery.  A held netpair secret still entitles its holder to claim
any device id in a `netpair_hello`, which is legacy's behaviour as well and is recorded rather than
repaired.  And the desktop-side file drop target (`dragDropEnabled` plus `onDragDropEvent`) was
unimplemented at the time of this checkpoint; the file-drop checkpoint below is that target.

## The File-Drop Checkpoint (2026-09-12)

Every checkpoint above names the same last implementation item, and this is it: the desktop window now
accepts a file dragged onto it.  It was never parity -- the legacy desktop has no drag-and-drop of its own
-- which is exactly why it lasted: nothing in the legacy surface said what a drop should do.  What the port
had instead was the transfers page's 发送文件 button, and a drop is that button with its picker already
answered.

- `tauri.conf.json`'s `dragDropEnabled` was the whole reason nothing could work: with it false Tauri hands
  the drop to the webview, whose own HTML5 drop event carries `File` objects whose real paths the browser
  does not expose.  It is true now, so the native side owns the drop and delivers `enter` / `over` / `drop`
  / `leave` with the OS's paths -- which is what a send needs, because the sidecar reads files by path.
- `bridge.onFileDrop` is one listener for the four phases, and a no-op outside the desktop like every other
  listener on that seam.
- The hint is the window's, not a card's: a file dragged in from the desktop can land anywhere on the window,
  so the whole window says it is a target while the files are over it.  It takes no pointer event, so the
  hint cannot be the thing that stops the drop landing.
- **The drop answers the picker, not the target.**  The paths are staged on the transfers page with the
  device question still open, and nothing leaves the machine until it is answered -- the same rule the button
  follows.  The window's copy is spent as soon as the page has it, so walking back to the page does not
  re-stage a drop that has already been sent or cleared.

**What this does *not* establish.**  No file was dragged from a real desktop onto a real window: the native
side is exercised as the payloads the host delivers, so what is proven is what this window does with them,
not that the OS hands them over on Windows, macOS and Linux -- that is a release gate, alongside the
cross-machine transfer acceptance the file-transfer row already names.  The staged send is not re-proven
here either: it is the same `send_files` call under the same target rule that the transfer checkpoint's own
cases cover.


## The Panel's Own Live History Checkpoint (2026-09-12)

**The route that ran out.**  The panel had four routes that broadcast: delete, pin, batch-delete and clear. Every
other way a row could appear or change was silent. Legacy had covered the rest with one line --
`sync_mgr.on_history_change` wired to `ws_manager.broadcast_history()`, commented in `src/main.py` as "what makes
newly copied items appear in the history panel without a manual refresh" -- and the sidecar rewired that hook to
the event journal, which is what the native window re-reads on. The panel half was dropped, on a belief written
into the bridge's own docstring: that the history API pushes its snapshots itself. It does, for the panel's own
actions, and for nothing else.

**What a user saw.**  A clip copied on the desktop, one arriving from a peer over the cable or the relay, a delete
or a clear made in the native window, a history import: none of them reached an open phone, which showed the list
as it stood when the page loaded. The panel is the one surface a user leaves open and walks away from — it is how
the phone is used — so the symptom is a list that quietly stops being true.

**Why the bridge and not a poll.**  The device page still keeps its fingerprint loop, so a second loop was the
available shape. It is the wrong one here: the events are already precise, and a loop would re-derive on a timer
what the journal says the moment it happens. `PhonePush` therefore maps `history.changed` onto the same two
manager helpers the routes use. Nothing ends up forwarded twice, because the web layer never publishes to the
journal: a panel-side pin or delete still announces once, from its route, and only what those routes do not cover
travels the new path.

**The one shape a snapshot cannot carry.**  The client's `history_updated` merge rebuilds the visible window but
keeps its pagination, so a wiped list would still offer "Load more" over nothing; clearing is therefore mapped to
the wipe message (and a falsy `cleared: 0` maps the same way — forgetting your offsets is the whole of what that
message is for). A batch delete stays a snapshot deliberately: its payload is the same `ids` a batch pin sends, so
the two cannot be told apart at that layer, and a snapshot is right either way — the client replaces the window in
place, and the snapshot's `total` drives the ghost calibration that catches a removal in a paged list.

**The push announces itself.**  `/api/push` was the case the previous checkpoint left open, and closing it needed
one line: after the row is recorded, `push_text` calls the sync manager's notification hook — the same call the
manager makes for a clip captured here or one that arrived from a peer, and the call whose docstring names this
use. It is not called when the history write raised: the text reached the clipboard, so the push still reports
success, but announcing a row that was never written would send every listener after something that is not there.

**What this does *not* establish.**  No real browser, phone or second machine was involved: the tests drive the
bridge and the manager, so what is pinned is that a change reaches the page, not that a phone holding mobile data
keeps its socket through a wifi/cellular transition. Nor is the payload re-proven — the `history_updated` shape is
the manager's existing one, pinned by the socketpair cases in `test_web_server.py`; what was checked here is that
the bridge's two new calls produce exactly that shape rather than something the page's handlers would drop.

## The Phone's Update Card Checkpoint (2026-09-12)

**What was missing.**  The phone's settings page carries an update card with a progress bar, a ready state and a
failure line, and its store documents itself as kept in sync by `update_state` WebSocket events and hydrated from
`GET /api/update/status` on load.  Only the hydration existed.  The runtime publishes the lifecycle as
`update.state` -- the same dict `update.status` answers with, wrapped as `{"state": snapshot}` so the native
window can apply a pushed event and a fetched status to one field -- and while the window consumes it, nothing
forwarded it to the page.  A download started from the phone therefore moved nothing.

**What the page needs, and what it now gets.**  `ws.js` folds the message payload onto its own copy and only when
that payload has a truthy `phase`: a flat state object, which is exactly what legacy's `_set_update_state`
broadcast.  `PhonePush` unwraps the event's `state` and pushes it under the message name the page already
handles, and drops an event carrying no state or no phase -- the client's own gate, applied before the message
exists rather than trusted to drop it after.

**The split is legacy's.**  A pushed event being flat while the fetched status is wrapped reads like an
inconsistency, and it is not one this migration introduced: legacy's `status()` answered `{"state": ...}` while
its broadcast carried the bare dict.  The two hydration paths still agree with each other; the live path is the
one that unwraps.

**What this does *not* establish.**  No download was run and no phone was involved.  What is pinned is the shape
the page merges and that it comes from the real `UpdateService` through the journal -- not that a progress bar on
a phone over mobile data moves smoothly, and not that the archive the ready state points at installs.

## The Shared Favourites Checkpoint (2026-09-12)

**What the event was for, and who was not listening.**  `favorites.changed` is published by the native request
adapter after every favourites write it handles, and the desktop window invalidates its cached list on it.
Nothing carried it to the phone, so a favourite added, edited, regrouped, reordered or deleted in the window
stayed invisible on a phone that had already loaded.  Not a legacy regression: legacy's desktop *was* this web
page, so one surface held one list and no push was needed.  The migration gave favourites two surfaces over one
`favorites.db`, and the event that exists for the window is the one the page needed.

**A snapshot, not a delta.**  `WebSocketManager.broadcast_favorites()` builds the payload from the same loader
behind `GET /api/favorites`, and `PhonePush` maps the runtime's event onto it -- so one function decides what a
favourite looks like on the wire, and the page merges exactly what its own route would have handed it.

**One apply rule.**  The page's new `favorites_updated` case and `app.js`'s page-1 load share
`store.replaceFavorites()`: in-place replacement, plus the ghost-group registry clear a reset needs.  The load
path's only change is that its four inline lines now call that helper.

**What this does *not* establish.**  No browser, phone or second machine was involved; the panel suite loads
the real store and client into jsdom and fires the socket callbacks by hand, so what is pinned is that a pushed
snapshot reaches the store.  And the direction is one-way: the panel's own favourite routes write the database
directly and publish nothing, so a favourite added from a phone still reaches no other client -- the same gap a
panel-initiated history delete has, and left open here because closing it means coalescing a drag's burst of
position updates rather than adding a line.

## The Favourites The Phone Writes (2026-09-12)

**The direction that had no event at all.**  The checkpoint above carried the native window's
`favorites.changed` to the phone.  Nothing carried a phone's change back: the panel's favourite routes
(`internal/web/api/favorites.py`, reached from `routes.py`) write `favorites.db` directly, while every
runtime-side write goes through the native request adapter that publishes.  A favourite added, edited,
regrouped or deleted on a phone reached no other client -- not another panel, and not the window, whose only
triggers are `favorites.changed` and the restore-time `data.changed`.  Legacy had one surface and needed no
event in either direction; the migration's two surfaces need both.

**One event, both surfaces.**  `MobileCompanion` hands `WebServer(on_favorites_change=...)` a callback that
publishes `favorites.changed` on the runtime's journal -- not the direct `ws_manager` broadcast the panel's
history routes use, which reaches panels only.  The journal is where the window already listens, and
`PhonePush` turns it back into the snapshot broadcast for the panels, so one publish covers both.

**A drag is one request.**  The panel reordered by patching every moved favourite, one request each: N round
trips on a phone, and with a publish per write, N snapshots behind one gesture, each partially applied.  The
same `PATCH /api/favorites` now also accepts `{"order": [{"id", "position"}, ...]}` -- additive, the
single-favourite body untouched -- and the panel sends the whole order at once.  A favourite deleted
mid-drag is skipped and the rest of the order lands; only an order naming nothing stored answers 404.  (The
key was `order` when this was written and is `updates` now -- see the next checkpoint.)

**What is still open.**  The panel's history routes broadcast to panels directly and so reach no native
window; left as they are, recorded as open.  The group burst this checkpoint called out -- renaming or
deleting a group patched every member, one published snapshot each -- is closed by the next checkpoint.  And no browser, phone or second machine was involved: the
end-to-end case speaks HTTP to a real server on a loopback port and reads the runtime's own journal back, so
what is pinned is that a phone's request reaches the journal -- not that a window in another process redraws.

## One Batch Body For Every Multi-Item Gesture (2026-09-12)

**The burst the checkpoint above left open.**  Its `{"order": [{"id", "position"}, ...]}` closed the burst for a
drag alone: renaming or deleting a group still patched every member, so one publish per write put a
half-renamed group on the wire per member.  Instead of a second shape for the group case, the body became a bag
of fields -- `{"updates": [{"id", ..., <fields>}, ...]}` -- and all three gestures send it: a drag sends the
positions that moved, a group rename sends `group`, a group delete sends an empty `group`.  Nothing had shipped,
so `order` was replaced rather than kept beside it; the route validates fields rather than positions, and
`position` is the one field it refuses to coerce, because a `true` or a `1.5` stored as a position is a client
bug.

**One gesture, one publish.**  The publish gate is still per request, so a gesture is one `favorites.changed`:
another reader of the window's cache sees the whole gesture or none of it, which is why the burst mattered.  A
failed group action now says so by staying silent -- the panel used to toast "renamed"/"deleted" once every
reply had settled, failures included.  Entries apply one by one, as they did when each was its own request.

**What this still is not.**  Not a transaction -- the route calls the repository once per entry, so a second
writer can interleave between them; it removes the publishes-per-gesture burst, not the shared store's own
non-atomicity, and no lock exists between the panel's routes and the runtime's adapter.  And no browser, phone
or second machine was involved.
