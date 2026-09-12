# ClipSync IPC v1

Status: implemented initial slice, not the full migration contract.

Transport: stdin/stdout, UTF-8 NDJSON, maximum 1 MiB per frame excluding newline.
This is a custom protocol, not JSON-RPC 2.0. stderr is never forwarded to Vue.
Python rejects duplicate JSON keys, non-finite numbers, unknown envelope/parameter
fields, malformed UTF-8 and non-object parameters. A malformed envelope terminates
the session; a valid request with an application error gets an error response.

```json
{"type":"ready","protocol":1,"session_id":"uuid","pid":123,"health":"ready"}
{"type":"request","id":"uuid","method":"history.list","params":{"limit":30},"correlation_id":"uuid"}
{"type":"response","id":"uuid","ok":true,"result":{"session_id":"uuid","seq":0,"total":0,"offset":0,"items":[]}}
{"type":"response","id":"uuid","ok":false,"error":{"code":"APP_LOCKED","message":"Unlock ClipSync to access history","retryable":false}}
```

## Implemented Commands

| Tauri command | IPC method | Parameters | Result |
| --- | --- | --- | --- |
| get_app_status | app.status | none | version, health, device identity, capabilities, session/sequence |
| list_devices | devices.list | none | live LAN snapshot, connection state, two-sided pairing status/code/SAS |
| start_pairing | pairing.start | device_id | accepted; this is not a completed pairing |
| confirm_pairing | pairing.confirm | device_id, code (eight ASCII digits) | paired, status; both users must confirm |
| reject_pairing | pairing.reject | device_id | accepted |
| unpair_device | pairing.unpair | device_id | accepted |
| set_sync_enabled | sync.set_enabled | enabled (boolean) | enabled; saved to the existing config |
| list_history | history.list | query <=512 characters, offset 0..1000000, limit 1..100 | paged previews; no raw payloads or file paths |
| unlock_app | app.unlock | password 1..1024 characters | unlocked; password is not logged or persisted |
| delete_history | history.delete | entry_id | deleted |
| set_history_pinned | history.set_pinned | entry_id, pinned | final pinned value |
| copy_history | history.copy | entry_id | copied; fails explicitly on invalid payload or OS write failure |
| batch_pin_history | history.batch_set_pinned | entry_ids (1..100 unique nonempty strings, each <=64 characters), pinned | updated count |
| batch_delete_history | history.batch_delete | entry_ids (same constraints) | deleted count |
| list_favorites | favorites.list | query <=512, group <=128, offset 0..1000000, limit 1..100 | summary items, groups, total, offset, session/sequence |
| get_favorite | favorites.get | favorite_id (1..64 characters) | favorite with full content |
| add_favorite | favorites.add | title <=256, content <=65536, group <=128; all required | favorite |
| update_favorite | favorites.update | favorite_id, title, content, group, position (0..1000000); all required | favorite |
| delete_favorite | favorites.delete | favorite_id | deleted |
| copy_favorite | favorites.copy | favorite_id | copied; full text, never the list preview |
| quit_app | app.shutdown through host exit cleanup | none | accepted, then process exits; the window does not call it, quitting is the tray's |
| minimize_app | none (window control, no sidecar RPC) | none | accepted; the main window minimizes, the session keeps running |
| list_transfers | transfers.list | none | active rows with normalized progress/rate/ETA, finished history, live speed-test state |
| send_files | transfers.send | paths (1..64 nonempty strings, each <=4096 characters; required), device_id <=128 | transfer_id; a named target only, never widened to a broadcast.  More than one path, or one that is a directory, is archived by the sidecar first and the archive is what travels -- one transfer, unlinked when it finishes.  A path that is gone is refused rather than sent short |
| choose_files | none (host file dialog, no sidecar RPC) | none | the paths picked, or an empty list if the dialog was cancelled; the host owns the dialog, and several picks become one send |
| choose_file | none (host file dialog, no sidecar RPC) | kind ("history", "backup", "any") | chosen path or null; the host owns the dialog |
| choose_folder | none (host folder dialog, no sidecar RPC) | none | chosen path or null; the host owns the dialog, and the sidecar archives what it picks |
| transfer_action | transfers.action | action in cancel/pause/resume/accept/reject/delete/retry/open/reveal, transfer_id 1..128 | ok and the action's own result; open/reveal resolve the path from history, never from the caller |
| cancel_all_transfers | transfers.cancel_all | none | cancelled count; the sidecar reads the live list, so a row the window has not seen is included |
| clear_transfer_history | transfers.clear_history | none | cleared count; records only, so a transfer in progress is untouched |
| start_speed_test | transfers.speed_test | none | test_id; progress arrives as transfers.list state |

The rows above cover the slices as they landed, and this table is not yet complete:
the chat, AI-config, companion, diagnostics, logs, update, backup, translate,
internet-pairing, relay, settings, discovery and URL/clipboard-push commands are
implemented in the sidecar and still absent here.

Normal desktop startup instantiates the existing LAN engines through LanRuntime.
`sync_state` reflects its lifecycle and enabled state, not merely saved config.
`--history-only` explicitly disables network/clipboard monitoring for isolated IPC
tests and reports `not_started`. A healthy history service does not prove that
LAN started, that a peer is connected, or that internet/relay delivery is active.

Device `connection_state` is `discovered`, `connecting`, `online`, or `offline`;
history-only saved-peer summaries remain `unknown`. `pairing_code` and `sas` are
display strings; an empty code means no active local confirmation. A successful
socket connection or local confirmation alone never sets `paired=true`.

Runtime events include `devices.changed`, `sync.state.changed`, `history.changed`,
`aiconfig.file`, `relay.delivery.changed` and sanitized
`runtime.error`/device-security notifications. Clients invalidate and reload
authoritative snapshots rather than using events as their only state. A delivery
receipt is the one event folded in place rather than reloaded on: it describes a
single send, so the window keeps a mirror of it (`desktop/src/stores/delivery.ts`)
seeded from `relay_delivery_status` when the panel opens, and repainting the
history on every ack of a large transfer would cost the reader a scroll for
nothing.

Favorites retain the existing `favorites.db` fields and full content. List
previews are at most 256 characters; detail is fetched separately before editing.
Oversized legacy content is not overwritten with its preview. Writes publish
`favorites.changed`; copy writes the OS clipboard and normal capture may then
produce a history event. All favorites access is gated by password unlock.

Batch history operations address stable record IDs, not visible row positions.
IDs removed concurrently count as unmatched; the response reports the actual
repository match count. Each batch publishes one history invalidation event.
The commands do not promise durable operation idempotency or transactional
all-or-nothing storage across existing repository methods.

Rust creates request IDs. The Python session remembers its last 4096 IDs and
rejects duplicates, but this is not durable operation idempotency. Rust never
automatically replays mutations. A 30-second timeout means the result is unknown,
not canceled. Late responses are discarded.

History mutation events are emitted after their response. The Vue store subscribes
before querying and invalidates/reloads authoritative snapshots on an event.
It does not apply a possibly stale delta over a snapshot. Its request-generation
guard rejects old search responses. The bounded journal reports replay overflow.
The Python adapter now sends autonomous notifications while stdin is idle. A
dedicated input reader admits one bounded request at a time; the dispatcher thread
owns all stdout writes. Responses precede events caused by that command. Event
batches are capped at 16, with the bounded journal providing overflow/resync;
oversized notifications also request resync rather than wedging the stream.
EOF and app.shutdown terminate the input worker, including when the parent keeps
stdin open after the shutdown response. A blocked OS read after an output failure
still requires host pipe closure. Stable operation keys, platform main-loop
integration and full process-tree supervision remain required.

## Current Security Boundary

Only the local `main` window receives app command permissions. `build.rs` registers
every app command with `AppManifest::commands`; generated allow/deny permissions are
explicitly selected in the main capability. There is no generic RPC command, shell
plugin, filesystem plugin, remote capability, or HTTP fallback in Vue.

Development defaults to `.tauri-dev-data`, never the user's real config. An
explicit absolute `CLIPSYNC_CONFIG_DIR` selects a test data directory. The sidecar
and updated legacy entry share an OS file lock. The sidecar also creates a
compatible PID marker for earlier legacy executables. Unknown legacy PID locks
are conservatively refused, not removed; a recognized sidecar crash marker may
only be replaced after exclusive acquisition of the OS lock. Stale unknown legacy
lock recovery remains a separate migration task.

Existing config/history schemas are reused. Unsupported config versions and
corrupt SQLite files fail without clearing/quarantining user data. Password-locked
history is not opened before verification. Identity bootstrap reuses existing
PairingManager and EncryptionManager, validates certificate/key/device-ID agreement,
restores known peers, and persists a newly generated identity. Partial identities,
failed private-key authentication and encrypted history without an identity are
recovery errors, never reasons to silently regenerate keys. Legacy plaintext
passwords migrate to the existing verification-token format on successful startup.

Favorites JSON adoption now commits `PRAGMA user_version=1` in the same SQLite
transaction as imported rows (or adoption of an existing/empty store). The JSON
is preserved but no longer re-imported after the last favorite is deleted. Future
unknown versions are rejected. Older rollback binaries that ignore this marker
need separate migration/rollback verification; this change does not establish
release-level backward-compatibility certification.
