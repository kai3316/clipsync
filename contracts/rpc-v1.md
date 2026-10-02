# ClipSync IPC v1

Status: every method the sidecar dispatches is implemented and listed below, and
`tests/sidecar/test_rpc_contract.py` holds this table and the dispatcher equal in
both directions.  Both of the table's own columns are held: the second against
the methods `rpc.py` routes, the first against the commands `main.rs` registers
in `generate_handler!`.  The migration is wider than this contract — the window,
the packaging and the signing have their own gates, in `release-notes.md` — and
what is not yet promised here is stated under *Runtime notes*.

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

Every method the dispatcher routes is listed here, and every command
`generate_handler!` registers appears in the first column;
`tests/sidecar/test_rpc_contract.py` holds both to their source. A row whose
second column is `none` has no IPC method: the host does that work itself, and
the row says so rather than leaving the command unlisted. One row may carry
several commands, which is how a family of one-line aliases over a single method
is written down. A row whose first column is empty is the mirror of that: the
host calls the method over the bridge from inside another command's work, so
there is no command of its own to name.

| Tauri command | IPC method | Parameters | Result |
| --- | --- | --- | --- |
| get_app_status | app.status | none | version, health, device identity, capabilities, session/sequence |
| unlock_app | app.unlock | password 1..1024 characters | unlocked; password is not logged or persisted |
| factory_reset | app.factory_reset | none | ok, factory_reset, deleted (a file count); the sidecar stops its own services and clears the data directory, then the host stops the bridge and relaunches the app |
| open_about_link | app.open_link | target in homepage/releases (the sidecar's own table; required) | ok, url; the WebView names a target, never a URL, and only the app's own links can be opened |
| quit_app | app.shutdown through host exit cleanup | none | accepted, then the request loop returns and the process exits.  No Tauri command calls it: the host bridge sends it during exit cleanup (`bridge.rs`), reached from `RunEvent::Exit` — which is where the tray's 退出 item and `quit_app` both lead |
| minimize_app | none (window control, no sidecar RPC) | none | accepted; the main window minimizes, the session keeps running |
| autostart_status | none (host plugin, no sidecar RPC) | none | the autostart switch's real state, read from the OS through `tauri-plugin-autostart` rather than from the stored setting; a query that fails is `AUTOSTART_FAILED`, never `false`, so "off" and "cannot read" stay distinct |
| restart_sidecar | none (host process, no sidecar RPC) | none | the background process stops and a fresh one is launched; the window and its state survive.  No method answers it: the bridge that would carry one is the thing being replaced |
| restart_app | none (host process, no sidecar RPC) | none | the sidecar is stopped first — `restart` replaces the process, so an unreleased data lock or an orphaned child would block the new instance — and then `AppHandle::restart` relaunches the application |
| recover_data_dir | none (host repair, no sidecar RPC) | none | items: what was moved aside.  The bridge slot is held for the whole pass so no command can launch a sidecar onto the files being repaired, and a sidecar is brought back whatever happened — a failed repair must not leave the window with no background process, so the repair's own error is reported first and the restart's second |
| get_settings | settings.get | none | settings; safe fields only, secrets never returned — `password_set`, `translate_key_set`, `netpair_password_set`, `relay_password_set` as booleans; live `internet_sync_state` and `current_relay_broker` |
| update_settings | settings.update | values (object; <=64 keys at the host); every key is optional and is either a boolean that must be a real boolean or one of: device_name <=128 characters, appearance_mode in system/light/dark, language <=32, app_filter_mode in blacklist/whitelist, log_level in DEBUG/INFO/WARNING/ERROR, dedup_method in sha256/simple, app_filter_list (<=256 entries, each 1..260 characters, stripped, no NUL/CR/LF), filter_enabled_categories (<=6 of credit_card/ssn/api_key/email/private_key/password), history_max_entries 10..10000, history_max_age_days 0..36500, port 1024..65535, web_history_limit 1..500, sync_debounce 0.05..10.0, clipboard_poll_interval 0.1..60.0, transfer_timeout 5..3600, max_reconnect_attempts 0..100, service_type 1..128 characters (stripped, no NUL/CR/LF), translate_url <=2048 (empty or http(s)://), relay_brokers and relay_private_brokers (<=16 URLs, each 1..2048), relay_username <=256, relay_password <=1024, relay_max_message_bytes 32768..1048576, file_receive_dir <=4096, data_dir <=4096, set_translate_key <=4096, clear_translate_key (must be true), password <=1024, clear_password (must be true) | ok and updated, the fields actually applied; any other key is VALIDATION_ERROR.  Readable but deliberately not settable here: notify_sync (nothing native reads it), sync_enabled and timed_pause_until (they go through sync.set_enabled / sync.pause — a direct write of the deadline could never arm the auto-resume timer), web_enabled, web_port, ui_backend, hotkeys, netpair_password.  set_translate_key / clear_translate_key must be the only key in the call and answer translate_key_set instead of updated; password / clear_password add password_set |
| set_sync_enabled | sync.set_enabled | enabled (boolean) | enabled; saved to the existing config |
| pause_sync | sync.pause | minutes (1..1440) | enabled (always false) and until (an epoch-seconds deadline); persisted as timed_pause_until |
| resume_sync | sync.resume | none | enabled (always true); clears the deadline and persists |
| list_devices | devices.list | none | live LAN snapshot, connection state, two-sided pairing status/code/SAS |
| list_chat_devices | chat.devices | none | the same live LAN snapshot rows `devices.list` returns, connection state and two-sided pairing status/code/SAS — the chat page's picker reads it from here |
| connect_device | devices.connect | device_id 1..128 characters (required) | accepted; a dial that was started, not a connection.  A dial with nowhere to go answers accepted false and rides out as `device.connection_unreachable` |
| disconnect_device | devices.disconnect | device_id 1..128 characters (required) | disconnected; reconnection is refused until the user connects again |
| forget_device | devices.forget | device_id 1..128 characters (required) | forgotten; the peer is told it is untrusted, and what is known is archived so it can be restored |
| restore_device | devices.restore | device_id 1..128 characters (required) | restored; known but unpaired, because the forget told the peer to un-pair |
| purge_device | devices.purge | device_id 1..128 characters (required) | purged; the archived row is dropped for good |
| test_device | devices.test | device_id 1..128 characters (required) | ok and results, one row per reachable channel (channel, ok, latency_ms, error); an empty list with error `no_channel` when neither LAN nor relay is reachable |
| device_retrust | devices.retrust | device_id 1..128 characters (required) | trusted; the certificate the device presented replaces the pin and the pairing stands |
| offer_device_update | devices.offer_update | device_id 1..128 characters (required) | sent; a frame telling the peer a newer build is out, offering the installer this machine kept from its own upgrade.  Needs no pairing — the peer answers with `update_request`, and what it receives is checked against the published release digest on its side before it can be installed.  Nothing is downloaded to answer this: a machine with no installer kept yet refuses with `update.no_asset` before it dials anything, and the device that is behind fetches the release itself; a peer it cannot dial comes back as `update.peer_unreachable`, an offer that never left as `update.offer_failed` |
| fetch_device_update | devices.fetch_update | device_id 1..128 characters (required) | sent; the same exchange from the other end — this device is the older one, so it asks the peer for its installer.  A request the peer answers with a blob is licensed on this side by the question itself, and the blob is checked against the published release digest before it can be staged; a peer with nothing kept answers `update_unavailable`, and the release is then fetched here, which is where the bandwidth belongs.  A peer it cannot dial comes back as `update.peer_unreachable`, a request that never left as `update.fetch_failed` |
| set_device_note | devices.note | device_id 1..128 characters, note <=512 characters; both required | ok |
| device_certs | devices.certs | none | devices: per known peer device_id, device_name, fingerprint, fingerprint_short, paired |
| scan_devices | devices.scan | none | enabled and visible — the state the scan ran in, since it changes nothing about either.  Asks the LAN who is here (announcing this device and waiting for the answers, about a second) instead of reading the last background round's result; the refreshed rows arrive through `devices.changed` |
| discovery_status | discovery.status | none | enabled and visible: whether mDNS browsing and advertising are active, both false when there is no discovery |
| set_discovery_enabled | discovery.set_enabled | enabled (boolean, required) | enabled, visible — the state after the toggle |
| set_discovery_visible | discovery.set_visible | enabled (boolean, required) | enabled, visible — the state after the toggle |
| send_url | url.send | device_id 1..128 characters, url 1..2048 characters; both required | sent and device_id (the resolved id); http(s) only, and the peer must be paired |
| relay_delivery_status | relay.delivery_status | peer_id optional, <=128 characters | pending, and items (newest first; at most 20 rows when no peer is named) each with msg_id, ts, status, preview, content_hash, kind, session_id, retries |
| start_pairing | pairing.start | device_id | accepted; this is not a completed pairing |
| confirm_pairing | pairing.confirm | device_id, code (eight ASCII digits) | paired, status; both users must confirm |
| reject_pairing | pairing.reject | device_id | accepted |
| unpair_device | pairing.unpair | device_id | accepted |
| internet_pairing_status | internet_pairing.status | none | generated_code, relay, enabled, peers (peer_id, name, alias, online, last_seen, paired), waiting (peer_id, name, since) |
| internet_pairing_generate | internet_pairing.generate | none | code |
| internet_pairing_enter | internet_pairing.enter | code 1..64 characters (required) | peer_id, waiting; this is not a completed pairing, the partner's answer is what completes it |
| internet_pairing_rename | internet_pairing.rename | peer_id 1..128 characters, name <=120 characters; both required | ok; an empty name clears the alias |
| internet_pairing_unpair | internet_pairing.unpair | peer_id 1..128 characters (required) | ok; also calls off a wait that has not completed |
| internet_pairing_test | internet_pairing.test | brokers optional: at most 16, each 1..2048 characters | results, one row per broker (endpoint, ok, latency_ms, detail), plus reachable and total; an empty list tests the saved brokers.  The only member of the pairing family that works with the LAN runtime down — it is a socket to a broker, not a session in the network |
| get_overview | overview.get | none | the dashboard counters: connected_count, paired_count, discovered_count, connected_names, history_count, history_today, history_pinned, history_images, active_transfers, transfer_completed, discovering, visible, sync_enabled, web_enabled, uptime_seconds, local_ip, port, platform, version, network_type, network_detail, and recent_items (up to 6 rows of text, type, time, pinned) — **amended 2026-09-14:** the six rows are now *history rows*, the same DTO `history.list` answers with, so each carries `id`, `timestamp`, `content_type`, `pinned`, `source_name`, `source_app`, `source_title`, `transport` and `paste_count`.  Only the preview is cut, to 80 characters, because the feed shows it on one line and every action on a row goes through the id — so a feed row can be copied, pinned, favourited, translated and deleted exactly as a history row can, which the four-key projection it replaced (no id at all) could not be |
| copy_text | clipboard.copy | text 1..100000 characters (required) | copied, len; local clipboard only, never a history entry and never a broadcast |
| push_text | clipboard.push | text 1..100000 characters (required) | ok, len, sent; writes the local clipboard and one history entry, and broadcasts when sync is running |
| open_data_folder | data.open_folder | which in data/backups (required) | ok, folder; the sidecar chooses the path, so no client path crosses the boundary |
| companion_status | companion.status | none | enabled, port, running, state, actual_port, url, access_url, token.  The raw access token is in the result whenever the companion is running and the app is unlocked; `access_url` embeds it as `mobile.html?token=…` |
| configure_companion | companion.configure | enabled (boolean, required), port 1..65535, rotate_token (boolean), clear_token (boolean) | the same record as `companion.status` after the change; the listener is restarted when the port or token changes.  The broadcast event carries neither URL nor token.  `clear_token` serves the companion token-free and, unlike `rotate_token`, outlives a restart — it is remembered so the mint-on-start path cannot undo it; `rotate_token` or an off→on transition arms a token again, and a clear wins when both are sent |
| companion_qr | companion.qr | none | ok, url, qr (a PNG data URL).  The access token is inside url when the companion runs; on failure ok false with error `COMPANION_NOT_RUNNING` or `QR_UNAVAILABLE` and qr null.  See the token note below |
| share_file_to_phone | companion.share_file | path 1..4096 characters | ok, the stored name, its path and size; the file is copied into the phone's share directory so the Files tab lists it and serves it without pairing.  The source is never moved, the name goes through the same sanitiser an upload's does, a name already in use is suffixed rather than replaced, and a directory or a path that is gone is refused |
| list_history | history.list | query <=512 characters, offset 0..1000000, limit 1..100 | paged previews; no raw payloads or file paths |
| read_history_text | history.text | entry_id 1..64 characters | id, text, truncated; text capped at 100000 characters, and an empty string for an entry with no text format |
| preview_history_entry | history.preview | entry_id 1..64 characters | kind, image, width, height, files, total; the picture for an image row and the file list for a file row, both downscaled/stat'd rather than shipped whole, and never a path.  Answers with an empty card — not an error — for a row that has gone, needs recovery, or holds nothing more to show, because a hover is not a request the caller made |
| open_history_link | history.open_link | entry_id 1..64 characters | opened (true) and url; the caller sends no URL — the sidecar reads the row's own text and decides what is openable |
| delete_history | history.delete | entry_id | deleted |
| set_history_pinned | history.set_pinned | entry_id, pinned | final pinned value |
| copy_history | history.copy | entry_id | copied; fails explicitly on invalid payload or OS write failure.  Rich-text paste decodes every format here — the legacy `/api/paste` and `/api/paste-rich` routes are both this |
| clear_history | history.clear | none | cleared, the number of entries removed |
| batch_pin_history | history.batch_set_pinned | entry_ids (1..100 unique nonempty strings, each <=64 characters), pinned | updated count |
| batch_delete_history | history.batch_delete | entry_ids (same constraints) | deleted count |
| export_history | history.export | format in json/csv/markdown | ok, filepath, filename, count, format; the sidecar chooses the path (Downloads, else the app data dir) and writes `clipsync-history-<timestamp>.<ext>`, leaving the file in place |
| import_history | history.import | path 1..4096 characters | ok and imported count; the caller names the file and the sidecar confines it to the app data dir or Downloads, caps it at 10 MB and accepts only .json/.csv |
| list_favorites | favorites.list | query <=512, group <=128, offset 0..1000000, limit 1..100 | summary items, groups, total, offset, session/sequence |
| get_favorite | favorites.get | favorite_id (1..64 characters) | favorite with full content |
| add_favorite | favorites.add | title <=256, content <=65536, group <=128; all required | favorite |
| update_favorite | favorites.update | favorite_id, title, content, group, position (0..1000000); all required | favorite |
| delete_favorite | favorites.delete | favorite_id | deleted |
| copy_favorite | favorites.copy | favorite_id | copied; full text, never the list preview |
| batch_favorite_history | favorites.batch_add | entry_ids (required; 1..100 distinct history ids, each 1..64 characters), group (required; <=128 characters) | added and ids; the FULL stored text is kept, not the truncated preview |
| create_favorite_group | favorites.group_create | name 1..128 characters, not blank | groups — the whole list, including groups with nothing in them yet |
| rename_favorite_group | favorites.group_rename | name 1..128 characters, rename_to (same bound); both required | renamed and groups; a rename to the same name answers renamed 0 rather than erroring |
| delete_favorite_group | favorites.group_delete | name 1..128 characters, not blank | moved and groups; the group's favourites are kept and simply left unfiled |
| reorder_favorites | favorites.reorder | favorite_ids (1..100 distinct ids, each 1..64 characters) | moved; the count whose position actually changed |
| export_favorites | favorites.export | format in markdown/text | filepath, filename, count, format; the sidecar chooses the path (Downloads, else the app data dir), the file is 0600, and no `favorites.changed` is written |
| list_transfers | transfers.list | none | active rows with normalized progress/rate/ETA, finished history, live speed-test state |
| send_files | transfers.send | paths (1..64 nonempty strings, each <=4096 characters; required), device_id <=128 | transfer_id; a named target only, never widened to a broadcast.  More than one path, or one that is a directory, is archived by the sidecar first and the archive is what travels -- one transfer, unlinked when it finishes.  A path that is gone is refused rather than sent short |
| choose_files | none (host file dialog, no sidecar RPC) | none | the paths picked, or an empty list if the dialog was cancelled; the host owns the dialog, and several picks become one send |
| choose_file | none (host file dialog, no sidecar RPC) | kind ("history", "backup", "any") | chosen path or null; the host owns the dialog |
| choose_folder | none (host folder dialog, no sidecar RPC) | none | chosen path or null; the host owns the dialog, and the sidecar archives what it picks |
| transfer_action | transfers.action | action in cancel/pause/resume/accept/reject/delete/retry/open/reveal, transfer_id 1..128 | ok and the action's own result; open/reveal resolve the path from history, never from the caller |
| cancel_all_transfers | transfers.cancel_all | none | cancelled count; the sidecar reads the live list, so a row the window has not seen is included |
| clear_transfer_history | transfers.clear_history | none | cleared count; records only, so a transfer in progress is untouched |
| start_speed_test | transfers.speed_test | none | test_id; progress arrives as transfers.list state |
| request_entry_files | transfers.request_entry_files | entry_id 1..128 characters (required), device_id <=128 | requested (true); the 下载 button on a history row whose file lives on another device.  The request carries the entry id and no path — the peer resolves the paths against its own history, which is what limits a request to files that peer published.  The download arrives as a transfer (`kind=clip_file`, auto-accepted, since the user already asked for it) and a refusal arrives as a `clip.file.denied` event with a reason code; nothing is claimed here about bytes having moved, and a named target must be paired and currently connected, so an unanswerable request is refused rather than left pending |
| list_chat_sessions | chat.sessions | none | sessions newest-activity-first, each with session_id, peer_id, peer_name, fingerprint_short, status, unread, online, last_preview, peer_typing; and muted, the sorted muted peer ids.  The host's notification path calls it too, to resolve a chat notification's session |
| list_chat_messages | chat.messages | session_id 1..128 characters; required | messages for that conversation, each entry_id, kind, outgoing, ts, text, text_key, fmt, file_name, file_size, mime, status, fraction, saved_path, transfer_id, msg_id; an unknown session is an empty list, not an error |
| invite_chat | chat.invite | peer_id 1..128 characters, peer_name 1..256 characters; both required | chat_session_id and connecting; the id is the conversation's, never the transport session's, and it is null while the link is still being dialed.  A peer that never answers publishes `chat.connect_timeout` rather than refusing the invite |
| chat_action, accept_chat_invite, decline_chat_invite, send_chat_text, resend_chat_text, close_chat, mark_chat_read | chat.action | action in send/accept/decline/read/close/resend, session_id 1..128 characters (required), text <=16000 characters (optional; for resend it carries the entry id) | ok; the action's own result is folded into that boolean, and false covers an unknown session, a state the action does not allow, flood control, or a resend of an entry that is not this session's own failed outgoing text.  The four named commands beside `chat_action` are one-line prefixes of it (`chat_action_command!`), one per action a reader can take without supplying free text; the two that answer an invitation are only ever reached with 必须经我同意 on, where the invitation waits for this answer instead of opening the session; they carry the same method and the same single `session_id`, plus `text` for send and `entry_id` for resend |
| send_chat_file, chat_file_action | chat.file | action in send/accept/decline/cancel, session_id 1..128 characters (required), transfer_id <=128 characters and path <=4096 characters (optional — send uses path, the rest use transfer_id) | ok and transfer_id, empty unless the action produced one; send returns the offer's transfer id and the bytes move only after the peer accepts |
| set_chat_muted | chat.mute | peer_id 1..128 characters, muted (boolean); both required | ok and muted, the sorted muted peer ids; the set is persisted to config |
| open_chat_file | chat.open_file | session_id 1..128 characters, transfer_id 1..128 characters; both required | path, for an entry of that session whose transfer_id matches and whose saved file is still on disk; the host opens it with the OS handler and returns ok.  A missing entry or a vanished file is NOT_FOUND rather than a path |
| reveal_chat_file | chat.reveal_file | session_id 1..128 characters, transfer_id 1..128 characters; both required | ok and folder, resolved from the same entry `chat.open_file` resolves — a session and a transfer id, never a path. The sidecar reveals it itself, through the same `reveal_folder` the transfer list's reveal uses, so a file whose folder has gone is REVEAL_FAILED rather than a silent ok. The legacy chat panel's 打开所在文件夹, beside 打开 |
| chat_typing | chat.typing | session_id 1..128 characters, typing (boolean); both required | ok; false for an unknown session |
| ai_inventory | ai.inventory | refresh optional bool (default false); peer_id <=128 characters (default empty, and an empty one with refresh covers every cached peer) | peers keyed by peer_id (name, legacy, entries, fetched_at), refreshed (only the peers a request was handed to), local (collected_at, entry_count, tools, custom_paths); a landed inventory arrives later as an `aiconfig.file` event, never in the result |
| ai_preview | ai.preview | peer_id 1..128 characters (required), tool 1..128 characters (required), rel_path 1..4096 characters (required), root <=256 characters | ok, content (<=64 KB), truncated; display-only, nothing touches disk.  An unpaired, offline or legacy peer answers ok false with an error code instead |
| ai_pull | ai.pull | peer_id 1..128 characters (required), items 1..100 (required; each `{tool, rel_path}`, optional root, is_dir for a folder), mode in copy/overwrite/append (default copy), batch_id <=128 characters | requested, errors, expanded; the bytes arrive asynchronously as `aiconfig.file` events echoing batch_id, never in the result.  An unpaired, offline or legacy peer yields requested 0 with the reason in errors |
| ai_profiles | ai.profiles | none | ok, tools (built-in profiles: key, label, entries of id/path/kind), enabled (the configured tool keys), custom_paths (the user's own paths, verbatim) |
| update_ai_profiles | ai.profiles.update | tools <=32 entries, custom_paths <=32 entries; both optional, and an omitted list means an empty one | the saved profile, same fields as `ai.profiles`; unknown tool keys are dropped and both lists deduped before the write |
| ai_local | ai.local.listing, ai.local.read, ai.local.save, ai.local.trash, ai.local.open | action in listing/read/save/trash/open (required; the host rejects any other and builds the method as `ai.local.<action>`); for every action but listing: tool 1..128 characters and rel_path 1..4096 characters (both required), root <=256 characters (optional; the tool's own root when empty); content 1..262144 characters with no NUL, required for save and refused for every other action | the action's own result.  listing: collected_at, tools, custom_paths, roots (root_index, tool, root, kind, path, count) and entries (with is_dir, so folders are openable).  read: ok, content (truncated past 64 KiB), truncated.  save: ok, plus backup_overwrote when the pre-save copy replaced a stale one — text only, written atomically, the original kept beside it as `.bak`.  trash: ok, trashed_to, under `<data_dir>/aiconfig_trash/<subpath>/<timestamp>_<name>`.  open: ok, and the OS default app is asked to open it.  Failures are `{"ok": false, "error": <code>}` — invalid_item, no_local_root, not_found, unsafe_path, binary, invalid_encoding, too_large, content_required, backup_failed, io_error, trash_dir_failed, move_failed, open_failed.  `rel_path` is resolved under the root on the sidecar's side, so the window names a file rather than a location.  This family is routed by a method *prefix*, which is why it went unlisted: the contract's guard read comparisons, and this is a slice |
| translate_text | translate.text | text 1..5000 characters (required), target_lang <=16 characters (default en), source_lang <=16 characters (default auto) | ok, translated, source_lang, target_lang (plus truncated on the free fallback); the configured translation key is sent only as an Authorization header and is never returned or echoed |
| read_logs | logs.tail | lines 1..1000 (default 200) | logs: the last lines, newest last, each redacted before it leaves — user home, config directory and the web token become `[redacted]` |
| export_logs | logs.export | dest 1..4096 characters (required; supplied by the host's own save dialog, never by the WebView) | ok, path, bytes; the copy is the RAW log, not the redacted tail `logs.tail` serves — see the note below |
| collect_device_log | logs.collect | device_id 1..128 characters (required) | sent; asks one device for its own log and answers as soon as the request is away, because the log travels back as an ordinary `kind=log` transfer and is filed when it lands (~/Downloads/ClipSync-logs/<device>-<time>.log, announced as `log.collected` or `log.failed`).  Needs no pairing and is refused by the peer unless its own `log_sharing` setting is on, which it says through `log_denied` → `log.unavailable`.  A device this machine does not know comes back as NOT_FOUND, one it cannot dial as `log.peer_unreachable`.  The copy served is redacted the way `logs.tail` redacts — this is the one log path that leaves the machine on its own motion, so it is never the raw file `logs.export` copies |
| collect_all_logs | logs.collect_all | none | requested, and devices (device_id, name) — one entry per device the sidecar actually dialed.  Returns once every request is away rather than when the logs arrive: each one lands later and is announced on its own.  Every known device is asked except this machine and the archived rows, and no pairing is required |
| open_logs_folder | logs.open_folder | none | ok, folder; creates `~/Downloads/ClipSync-logs` if it is not there and asks the OS to reveal it.  The sidecar owns the path, so no client path crosses the boundary |
| diagnostics_report | diagnostics.report | none | v2, summary, checks, groups (system, network, internet, ai_config, chat, transfer, filesystem), connected_count, paired_count, web_companion_running, web_port, lan_ip, os, version; no secret values — the secrets map and the relay lists contribute counts only |
| diagnostics_request | diagnostics.request | action in firewall/local_network (required) | ok; a refusal is ok false with an error sentence, never a raised error |
| update_check | update.check | none | available, latest, current, url, installable; the lookup is bounded (~8s) and answers "no update" rather than failing when the release host is unreachable.  `installable` is added by the host, not the sidecar, and carries the updater plugin's own answer to whether this build can replace itself, which has three values and not two: true when the plugin matched this machine's bundle against the release manifest, false for a real refusal (no entry for this platform), and **absent when the manifest could not be read at all** — GitHub unreachable, or the minutes a release exists before its manifest is attached.  A lookup that failed says nothing about what this build can do, so it is not sent as false; the window offers the in-place install for anything but a false |
| update_status | update.status | none | state (phase in idle/downloading/ready/failed, fraction, downloaded, total, error, version, path, source); path is the staged archive's absolute path and is returned to the WebView verbatim.  `source` is `github` for this machine's own download and `p2p` for one a peer sent, and the host reads it because the two are finished differently (see `update_install_ready`) |
| update_download | update.download | none | ok, started, error; progress arrives as `update.state` events, and peers are asked in parallel for a copy they already hold |
| update_open_folder | update.open_folder | none | ok; refused with ok false and an error sentence unless the phase is ready, and no path crosses the boundary |
| update_install | none (host process, no sidecar RPC) | none | ok, installed, reason (`up_to_date`).  The other half of the updater, in the host because a process cannot replace the bundle it is running from.  Fetches and verifies through the updater plugin, then stops the sidecar before installing — on Windows `install` launches the NSIS installer and calls `process::exit(0)`, so no exit handler runs after it and an unstopped sidecar would outlive the update holding the data lock.  Progress reuses the `update.state` channel the sidecar writes, with an `installing` phase the sidecar never emits, and every failing path publishes `failed` with an error sentence before returning.  Windows does not return from a successful install: the installer brings the new version up.  macOS and Linux swap in-process and the host restarts.  The window is given no updater permission, so the plugin's own commands stay out of the WebView's reach |
| update_install_ready | none (host process, no sidecar RPC) | none | ok, installed, reason (`up_to_date`, `manual`).  The install half of the lifecycle for an archive already on this machine — the sidecar's own verified download, or the blob a peer sent, which was checked against the published release digest before it was staged — and so the one install that needs no network of its own.  `update.status` is read first: anything but `ready` with a file still at `path` is a NOT_FOUND refusal.  With that in hand the plugin is asked first, because it is the better answer whenever it has one: it fetches this platform's own updater payload and verifies the manifest's signature, and the staged archive is not always that payload (a `.dmg` is not the `.app.tar.gz` the plugin unpacks).  A manifest that answers with an update is fetched and installed exactly as `update_install` does; one that answers "already current" is `up_to_date` with nothing run, since a digest-pinned archive cannot be newer than the released version this build already has.  A manifest that **cannot be read** falls through to the staged file, which is the case the peer path exists for — on Windows the `-setup.exe` its asset matcher picked is run with `/P /UPDATE /R`, the install mode's own arguments, so the relaunch comes from the installer; on macOS and Linux the folder is revealed and the answer is `installed` false with reason `manual`, because a `.dmg`, a `.deb` or an AppImage is a file a person opens rather than one this process can replace itself with.  The bridge runs this on its own for the update a peer sent — `update.state` with phase `ready` and `source` `p2p` — which is the whole reason `source` exists |
| | update.cache_asset | path 1..4096 characters (required), name <=255 characters (the asset's own filename; what the copy is stored as) | ok and cached.  No Tauri command leads here: the host's own update install calls it over the bridge with the installer it has just verified, which is the only moment that file exists on this machine and the only process that can read it is the one about to be replaced.  The path is a temp file, so the name is what the copy is kept under, and it is checked against this build's own installer names first — a file that is not one this application could install, the macOS `.app.tar.gz` payload included, is refused rather than kept as something a peer might be sent.  Every other file already in the cache is pruned, so what this machine offers is always the newest installer it holds |
| list_backups | backups.list | none | backups newest first, each filename, absolute path, size and date; directories and non-zip files are skipped |
| create_backup | backups.create | none | backup_path; config.json, history.json and favorites.json in one timestamped zip, written as .part and renamed so an interrupted backup is never listed as restorable, and a history export that does not round-trip aborts the backup |
| restore_backup | backups.restore | path 1..4096 characters; required | config (a boolean plus the history and favorites counts); members are zip-slip checked before extraction, a partial restore raises RESTORE_PARTIAL_FAILED after publishing `data.changed`, and the response is the summary either way |

### Notes on the table

Three rows carry a difference between the two sides that is worth stating rather
than smoothing over, and one is a bound that differs in the other direction.

**The companion token is in three results, not one.** `companion.status`,
`companion.configure` and `companion.qr` all return the raw access token — the
last two because they return the same record or embed its URL. This is the token
the phone page authenticates with, so the window needs it to draw the QR code;
the boundary that matters is the one that already holds: the broadcast event
carries neither URL nor token, and nothing but the local `main` window can call
these three.

**`export_logs` is the one unredacted log path.** `logs.tail` scrubs the user
home, the config directory and the web token before its lines leave, and
`logs.collect` serves a peer the same redacted copy — it leaves on its own
motion, to a machine the user may never have paired with, so it is held to the
same rule. The export copies the raw log, because that is what a user asking for
a log file wants to send, and the destination is chosen by the host's own save
dialog rather than by the WebView.

**`update_status` returns an absolute path** (the staged archive under
`~/Downloads/clipsync-update/`) while `update_open_folder` refuses to let one
cross. Both are the app's own directory and the window is the only caller; the
asymmetry is noted so that a future command does not take the `update_open_folder`
comment as a rule the family follows.

**Ids are narrower at the host than at the sidecar.** `validate_id` in the Rust
host rejects an id over 64 characters, while every `session_id` / `peer_id` /
`device_id` bound above is 128. The IPC contract is the 128; a caller that could
not produce such an id anyway is the window, which is where the 64 lives.

## Runtime notes

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
`aiconfig.file`, `relay.delivery.changed`, `relay.state.changed` and sanitized
`runtime.error`/device-security notifications. Clients invalidate and reload
authoritative snapshots rather than using events as their only state. Two events
are folded in place rather than reloaded on. A delivery receipt describes a
single send, so the window keeps a mirror of it
(`desktop/src/stores/delivery.ts`) seeded from `relay_delivery_status` when the
panel opens, and repainting the history on every ack of a large transfer would
cost the reader a scroll for nothing. The relay's own state *is* the line the
pairing card draws (本机中继), and the card read it only when it was opened —
which left that line reporting whatever the link was doing at that moment until
刷新 was pressed, on a link that transitions without any user action. It is
published on every transition (`connecting`, `online`, `error`, `off`, from
`RelayTransport`'s state callback), the store keeps the newest one
(`state.relayState`), and it is dropped when the sidecar it describes goes away.

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
compatible PID marker for earlier legacy executables. An unknown legacy marker is
refused while either pid it names is still running and reclaimed when neither is:
the marker outlives a crash, a kill and a reboot, and refusing on one of those
orphans locked the application out of its own data until the file was deleted by
hand. A recognized sidecar crash marker may only be replaced after exclusive
acquisition of the OS lock. Both halves are in the working tree and pinned by
`tests/sidecar/test_rpc.py`; the shipped 1.0.2 predates them.

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
