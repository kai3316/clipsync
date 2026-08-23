# Changelog

All notable changes to ClipSync are documented in this file.

## [1.0.40] — 2026-08-23

### History (reconcile — change detection)
- **The page-1 reload's change detection is now full-field** (mirroring the WebSocket merge path): a reconnect that changes paste_count, timestamp, or source metadata — not just pin/preview — bumps the reconcile guard, so an in-flight calibration can't write back stale data over fresher rows. A malformed (null) row in the response is treated as a change instead of throwing.
- The store module header's description of the calibration timeout now matches the code and tests (it unwedges the lock without advancing the generation).

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.39] — 2026-08-23

### History (pin helpers — polish)
- **The page-1 wholesale reload only bumps the reconcile guard when the snapshot actually changed** — a reconnect that applied an identical list no longer invalidates an in-flight calibration or burns its throttle budget (aligned with the WebSocket merge path).
- `setPinned` / `setPinnedBatch` get proper docs, and batch-pin now uses a hash-set membership test (O(n+m)) with a null/empty guard, matching `removeHistoryItems`.
- The stale test docstring describing the calibration timeout as advancing the generation now matches the corrected contract.

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.38] — 2026-08-23

### History (pin reconcile — consolidated)
- **Every pin path now goes through one place.** New `store.setPinned` / `setPinnedBatch` helpers re-find rows by id, apply the flag, and bump the reconcile guard **unconditionally** — the server committed a change, so an in-flight calibration's pre-change snapshot must never write back, even when the toggled row left the loaded window during the round-trip (the v1.0.37 gap).
- Single-pin (history item + context menu), batch-pin, and the page-1 wholesale reload on reconnect all route through the guard, so no pin path can silently re-open the calibration race.
- Stale calibration comments corrected (the timeout still does not advance the generation).

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.37] — 2026-08-23

### History (pin/delete race fixes)
- **Toggling pin no longer writes to the wrong row.** A pinned item jumps to the top when the server broadcast's wholesale replace lands before the HTTP response — the response now re-finds the row by `entry_id` instead of using a stale captured index, so an unrelated item can't silently get pinned.
- **The reconcile guard now covers every pin path**: single-pin (history item + context menu) and batch-pin all bump the mutation tick, so an in-flight calibration can't revert a just-applied pin.
- **Batch delete shrinks the pagination cursor by the rows actually removed**, not the pre-confirm selection size — a WS broadcast that already removed (and shrunk) the rows no longer over-shrinks the cursor.
- Dead index computations removed from the context-menu delete and pin paths; the calibration comment block now matches the timeout contract.

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.36] — 2026-08-23

### History (reconcile)
- **Pin toggles now bump the reconcile guard.** An in-flight calibration could write back a stale snapshot that reverted a pin the user had just toggled (same-id pin changes don't always reach the broadcast comparator). Toggling pin marks the list mutated, so the calibration abandons its write-back.
- **The calibration docblock now matches the code** (the timeout is the one terminal state that does not advance the generation; stale comments no longer claim it does).
- **The context-menu delete is fully consolidated**: it shrinks the load-more cursor by the number of rows actually removed (not a stale pre-confirm index) and relies on the shared helper to prune the selection.

### Quick Paste
- **A mid-removal `FileNotFoundError` is no longer mistaken for "already cleaned"** — the entry is kept for the sweep to finish, while a genuinely-absent profile is still treated as cleaned.

### Tests
- Full suite 433 passed / 3 skipped.

## [1.0.35] — 2026-08-23

### History (reconcile)
- **A slow-but-valid reconcile response is no longer discarded.** v1.0.34's 16s timeout also advanced the generation counter, so any fetch slower than the timeout was thrown away — on a slow link the ghosts could never heal. The timeout now only unwedges the lock and consumes the back-off budget; a response that settles later still passes the generation guard and writes back (staleness vs. newer data remains the mutation tick's job).
- **The last hand-written history delete now goes through the consolidated helper** (context-menu's splice), and it shrinks the "load more" cursor like every other delete path — no more skipping the item that shifts into a deleted slot.

### Quick Paste
- **A missing profile directory is treated as already-cleaned** (no spurious "incomplete cleanup" keep-entry when an external temp cleaner already removed it).
- **Shutdown no longer promises a retry it can't keep** — the instance dict dies with the process, so a profile that can't be removed at shutdown is dropped with a visible path warning (the OS temp cleaner will reclaim it), and the retry sleep is only between attempts.

### Tests
- Calibration-semantics regression updated to the corrected timeout contract. Full suite 433 passed / 3 skipped.

## [1.0.34] — 2026-08-23

### History (pagination reconcile — consolidated)
- **The mutation contract is now one place.** All history mutations flow through `store.removeHistoryItems` / `store.clearHistory` / `store.mergeHistoryFresh`, which bump the reconcile guard centrally — a future delete/clear path can no longer silently re-open the ghost-resurrection race.
- **The reconcile back-off is consistent**: every terminal state (success, failure, timeout, race-abandon) consumes the throttle budget and advances the generation counter, so a busy clip stream can't trigger an unlimited full-history download, and a timed-out calibration's late response can never write back.
- **In-place content updates (pin, edits) on the first page now count as mutations** too, so a reconcile can't revert a just-applied change.

### Quick Paste
- **The `--app` window paste path is guarded as well** — a successful paste sets the pasted flag before the auto-close block, so the retry-exhaustion banner never covers a confirmed paste.
- **The abandoned-profile sweep never evicts a live popup** (process check first), keeps a partially-removed profile for a bounded retry (3 attempts), and the done-path only forgets an instance after its profile is actually removed — no more permanent partial-profile leaks or unbounded zombie entries.
- App-shutdown profile reclaim retries once and hands the rest to the next startup.

### Tests
- 10 new/updated regressions. Full suite 433 passed / 3 skipped.

## [1.0.33] — 2026-08-23

### History (pagination reconcile)
- **The ghost-calibration throttle only counts successful reconciles** — a fetch aborted by a racing deletion no longer blocks the next attempt for 30s.
- **The mutation guard covers every delete path**, not just the WebSocket broadcasts: the dashboard's own delete/clear/batch-delete now bump the mutation tick, and a `history_updated` merge that actually adds data bumps it too. An in-flight reconcile can no longer resurrect a locally-deleted row or clobber a just-arrived clip.
- **The reconcile lock can't wedge forever** — a fetch that never settles (old WebView without AbortController) is unwedged by a 16s fallback timer, and a superseded fetch can neither clear a newer calibration's lock nor write back.
- **The phone's reconcile is throttled like the dashboard** (30s), so a failing calibration no longer re-downloads the entire history every 5-second poll.

### Quick Paste
- **A successful paste is never overwritten by the close-retry banner** — if the done POST keeps failing, an already-pasted popup gets a light toast, not a "could not auto-close" screen replacing the confirmation.
- **An empty instance id is treated as "missing"** (accepted, no-op) instead of a 400, matching the intended legacy-client behaviour.
- **App shutdown closes popups in parallel** (one shared wait round instead of serial per-popup timeouts), and the abandoned-profile sweep keeps an entry for a later retry when a lingering child still holds profile locks.

### Tests
- 9 new regressions. Full suite 428 passed / 3 skipped.

## [1.0.32] — 2026-08-23

### Quick Paste
- **The popup closes on every platform now.** v1.0.31 stored the instance id as an int but the page sent it as a JSON string, so the close signal never matched and no popup ever closed. The id is now normalised to an int on the way in, invalid ids are rejected with a 400 (never a 500), and the regression test posts the real string form.
- **Closing a popup on macOS/Linux no longer kills the whole app.** The Chromium child was launched in the app's own process group, so the group-signal teardown signalled ClipSync itself; it now starts in its own session.
- **Abandoned popups are cleaned up** — dead instances and their temp profiles are swept before each new open and on app shutdown.
- **A done signal with no/unknown instance is a no-op**, never "close the most recent popup" (a stray POST could previously kill a popup it didn't come from).
- **Pasted-state confirmation survives a failed done POST** — the safety net retries up to 3×, then shows a persistent "close this window manually" notice instead of silently leaking.

### History
- **Ghost calibration can't resurrect deleted rows.** A deletion/clear that lands while the reconcile fetch is in flight is now respected (a mutation tick guards the write-back), the reconcile is throttled to once per 30s, and the dashboard + phone share one implementation instead of three drifting copies.
- The phone's reconcile failure path now pins the cursor and re-renders like the dashboard.

### Tests
- 6 new regressions (string-id close, invalid-id 400, process-group isolation, instance sweep/cleanup, None no-op). Full suite 419 passed / 3 skipped.

## [1.0.31] — 2026-08-23

### Quick Paste
- **The popup close mechanism actually works now.** The v1.0.30 Chromium `--app` launch handed the URL off to an already-running browser instance (the spawned process exited, so closing it was a no-op) — and with no browser running, closing killed the whole browser. Each popup now launches its own private Chromium instance (`--user-data-dir=<temp>`), tracks it by an instance id, and closes only that instance's process tree (`taskkill /T /F` / `killpg`), cleaning up its profile. The done signal (paste, Esc, X, and a 60s safety net) carries the instance id, so an abandoned popup can never close a different one.
- **A pasted Quick Paste becomes a terminal state** — the confirmation can't be replaced by a re-pastable live list.
- The done callback is wired through the normal dispatch parameters instead of a module-level slot, so a stale registration can't survive an app restart.

### History
- **Ghost entries no longer evict live ones.** The previous `total`-trim assumed a deleted entry is always the oldest row, but history is pinned-first ordered — trimming the tail could drop a live clip while the ghost stayed. When the loaded list exceeds the server `total`, clients now fetch the authoritative list and replace wholesale, then recompute the cursor.
- **Phone deletions heal again.** With no WebSocket on the phone, a deletion beyond the first page is now caught by the 5-second poll (which triggers the same authoritative reconcile when `total < loaded`).

### Misc
- Chat file-offer expiry is surfaced on the desktop too (the accept path returns the `None` sentinel, and the dashboard shows "request expired" instead of silently doing nothing).
- `postDone` uses the page's own timeout helper instead of a bare fetch.

### Tests
- 10 new / 6 updated regressions. Full suite 413 passed / 3 skipped.

## [1.0.30] — 2026-08-23

### Quick Paste
- **The popup can actually close itself now.** Browser tabs opened with `webbrowser.open_new` can't be closed by script, so the v1.0.29 affordances were still dead on desktop. The host now launches Quick Paste in a Chromium `--app` window when one is available, and the page signals a paste via `POST /api/quickpaste/done` so the app closes the window for real (X / Esc also work there). Without Chromium it falls back to a plain tab, which degrades to a "✓ Pasted — close this tab" state instead of pretending.
- **Keyboard shortcuts are back on plain desktop tabs.** Listbox focus, 1-9 / Arrow / Enter, and the "Press 1-9 to paste" hint are gated on touch *hardware* (via `pointer: coarse`) rather than whether the page was script-opened — so a keyboard user who bookmarked the page keeps working keys, while auto-close behaviour stays tied to the app-opened window.

### History (pagination)
- **No more 5-second list collapse on the phone.** The background poll prunes only when you haven't scrolled past the first page; once you've loaded more, it merges without pruning, and the server's `total` is used to trim genuinely-deleted ghost rows at the tail.
- **Desktop cursor is calibrated against `total`.** If the WebSocket missed a deletion broadcast, the loaded list can hold ghost rows that inflated the pagination cursor — "load more" would skip live entries. The page-1 merge now trims past `total` before recomputing the offset.

### Misc
- Accepting a chat file offer that just expired now shows "request expired" instead of a generic failure (the offer state is popped under the lock, so a racing accept can't observe a half-dead offer).
- Removed dead `peer_completed` bookkeeping in the chat file sender.

### Tests
- 9 new regressions. Full suite 402 passed / 3 skipped.

## [1.0.29] — 2026-08-23

### Quick Paste
- **Popup closing is keyed to how the page was opened, not the hardware.** The v1.0.28 fix gated close behaviour on `IS_TOUCH`, which is true on any touch-capable laptop even when using a mouse — so the popup still refused to close there. The host now opens Quick Paste with `?auto_close=1`, and auto-close / Esc / the X button are enabled only for script-opened popups. A plain browser tab (bookmark, copied link) keeps the X hidden instead of showing a button that can't close.

### History (pagination consistency)
- **Live-inserted entries no longer skip history.** The merge path advanced the pagination cursor by the number of freshly-inserted rows, which overruns the real position when de-duplication drops duplicates — later "load more" pages then silently skipped entries. The cursor is now recomputed from the list length (dashboard and phone).
- **Phone background polling prunes deleted entries.** Once a phone had scrolled past the first page, the merge kept every row that wasn't in the fresh snapshot — deleted clips lingered as ghosts forever. The page-1 poll now reconciles (removes missing ids, empties on clear).

### Misc
- Reverted an incomplete "unconfirmed delivery" chat-file status that nothing rendered; file sends report plain success again.
- `DELETE /api/files` matches filenames exactly (no trimming), so a name with leading/trailing spaces can't delete a different file or become undeletable.
- `web_history_limit` help text now says 1–500 (matching the accepted range and the 30 default) instead of 1–20.
- The chat stale-receive sweeper fires its callbacks outside the lock, like its sibling sweeper, so a slow WebSocket can't freeze the chat state.
- Phone delete-file and send share one error-handling helper (consistent 403 → re-scan-QR messaging).

### Tests
- New regressions: auto-close URL, cursor recompute, mobile prune, exact-filename delete, chat-file success status, deferred lock-out callbacks. Full suite 392 passed / 3 skipped.

## [1.0.28] — 2026-08-23

### Quick Paste
- **The desktop popup actually closes again.** The inline close handler referenced an IIFE-local function (a `ReferenceError`), and every close path was gated on `window.opener` — which is `null` for pages opened via `webbrowser.open`. The button is now bound via `addEventListener`, and closing (auto-after-paste, Esc, and the X button, which is visible on desktop) is gated on touch input instead. Paste-then-walk-away popups work as intended.
- The paste push now uses the same timeout as the history fetch, so a hung server can't leave the "pasting…" state forever.

### Phone companion (mobile.html)
- **History has pagination** — scroll to the bottom loads more (offset-based, de-duplicated), and background refreshes merge into the already-loaded pages instead of collapsing them back to the first 30.
- **Files can be deleted from the phone** — new `DELETE /api/files` endpoint (path-confined to the receive dir) plus a delete button with confirmation.
- **Token expiry is never silent**: the send path and the background polling both surface "re-scan the QR code" when a 403 comes back (throttled so polling doesn't spam).
- Upload pre-check leaves room for multipart overhead, matching the server's `Content-Length` limit.

### Dashboard
- **Startup no longer loads everything twice** — the WS `connected` event is the single load entry point (with a fallback timer in case the socket never opens).
- **WS reconnects keep your pagination** — history that was "load more"-ed is merged, not replaced, so a network blip doesn't drop you back to page 1.
- Overview fetches are de-duplicated in flight (5s timer + WS-triggered refresh share one request).

### Tests
- New regressions for the file-delete endpoint (path traversal, absolute paths, missing/deleted, reject-directory) and the Quick Paste / pagination guards. Full suite 386 passed / 3 skipped.

## [1.0.27] — 2026-08-23

### Web companion (convergence round)
- **Concurrent dialogs no longer overwrite each other.** Two server-pushed dialogs arriving together (a file transfer request next to a pairing prompt) used to fight over one slot — the first silently timed out after 2 minutes. Dialogs now queue client-side and pop one at a time.
- **Delete / pin / clear now reach every web client.** A history deletion, pin, or clear performed on one client (dashboard, phone) previously left other connected clients showing stale rows until a manual refresh. New `history_item_deleted` / `history_clear` WebSocket events keep every client in sync.
- **WebSocket heartbeats.** The server now pings clients and drops any that have been silent for ~90s, so a phone that went to sleep or lost its network no longer leaves a zombie "connected" entry inflating the device list and eating a slot.

### Web & API polish
- History pagination offset tracks live-inserted entries (no more drifting "load more" cursor after real-time pushes).
- Pin toggles surface failures; details view shows `entry_id` 0 correctly (no more "N/A" on the first clip).
- `AbortController` feature-detected (no crash on ancient WebViews); `uploadFile` gets the same timeout as other API calls; an abort during response parsing is reported as a timeout, not a phantom "HTTP 200".
- Dialog delivery is judged by actual sends, and a dialog queued while no client was attached gets a full response window from the moment it appears.
- Overview refresh is debounced (500 ms) — rapid copying no longer fires a full HTTP overview fetch per keystroke.

### Desktop
- **Windows webview shutdown cleans up child processes** (`taskkill /T /F`) instead of leaving GPU/renderer stragglers behind.
- First-run onboarding Tab/Shift+Tab navigation works even when focus is inside a card's label.

### Tests
- 14 new web regressions (dialog queue, delete/pin/clear broadcast, WS heartbeat + stale-client drop, dialog delivery/queue timing). Full suite 374 passed / 3 skipped.

## [1.0.26] — 2026-08-23

### Security
- **Clipboard file-transfer chunks now verify their sender.** `file_chunk` frames pass the unpaired-peer gate (chat file bytes ride them), so a chunk that doesn't match the transfer's peer is dropped — an unpaired or newly-unpaired device that learned a transfer_id can no longer inject bytes into a clipboard download it doesn't own.

### Web chat (first audit round)
- **Failed text sends are no longer swallowed.** A send rejected by the backend (peer offline, flood control) now keeps the draft and shows "send failed" instead of silently clearing the composer while the peer never receives anything.
- **Changing the file-receive directory no longer breaks chat-file downloads** — the chat manager's receive dir is updated too, and downloads confine against the chat dir (with a fallback to the web upload roots for older files).
- **No more per-message full reload or unread-badge flash** — an incoming message no longer triggers a redundant REST refetch while you're looking at the conversation.
- **File cards show real terminal states** (declined / failed / cancelled) instead of "Preparing…" or a false "Sent" — the wire statuses map to labels, and a sender whose receiver rejects the file marks it declined, not done.
- **Fast session switching can't mix conversations** — a stale message-list response is ignored if the active session changed.
- **Inviting an unreachable / rate-limited device gives feedback** — the API distinguishes "connecting in background" from "can't connect", and the UI shows the timeout error instead of a 2-second "Connecting…" with nothing after.
- **Chat file sends no longer trigger a "received file" notification, sound, or a Files entry** — chat uploads go to a staging area via `purpose=chat` and are cleaned up if the send fails.
- **Accept / decline / close / file actions surface failures** instead of silently doing nothing.
- **Closed sessions read as "Closed"**, not "Offline"; **incoming invites raise the unread badge** so a chat request isn't missed until you open the panel.

### Phone pages & dashboard
- **Phone companion pages are installable PWAs** — `mobile.html` and `quickpaste.html` ship a manifest (`?page=` variant, distinct app identity) and register the service worker, so "Add to Home Screen" yields a standalone, offline-capable app like the dashboard.
- **Chat sidebar unread badge memoized** — no redundant Tk repaints every fast refresh tick.

### Tests
- Web chat regressions added (send-failure, receive-dir, invite feedback, chat-upload staging + cleanup). Full suite 360 passed / 3 skipped.

## [1.0.25] — 2026-08-23

### Security & transport (regression round)
- **The anonymous-connection gate actually fires now.** v1.0.24's check matched `device_id.startswith("__anon__")`, but anonymous connections carry `device_id = "unknown"` (the `__anon__{ip}:{port}` string was only the `_peers` dict key) — the gate was dead code and chat-invite floods from fresh TLS connections could still reach the UI. Anonymous connections now carry an explicit `is_anonymous` flag that the transport gate honours.
- **A torn rejection marker is treated as a rejection** instead of being discarded (which sent the client into an invalid-frame → reconnect loop on congested LANs).
- **A rejected peer is no longer added to the inbound-reject set** — a forgotten device can still reach you again after the user re-pairs.

### Nearby chat
- **Session-id adoption migrates the text-rate buckets** — adopting a peer's new session id no longer orphans the old buckets (memory) or resets the flood budget (abuse).
- **`_file_sender` can't raise `NameError`** when the stale-transfer sweeper pops the send state while a blocked `sendall` returns — sid/tid are bound up front.
- **Multi-line messages are preserved**: the incoming-text sanitizer strips control/bidi characters but keeps `\n`/`\t` (the sender's transcript and receiver's view no longer diverge).
- **Accepting a file rolls back its receive state if the accept frame can't be sent** — no more "Receiving…" stuck for minutes with an open temp-file handle after the peer vanished.
- **The per-peer send_fn cache evicts the least-recently-used entry**, not merely the oldest-inserted one.

### Desktop
- **macOS autostart toggle reads the key the app actually writes** (`ProgramArguments`, not `Program`) — the "enabled at login" switch no longer always shows Off. It still verifies the binary exists.
- **The chat unread badge is only cleared while the chat panel is on screen** — switching to another panel no longer silently zeroes incoming-message indicators (which the dashboard-visible notification suppression would otherwise swallow).

### Tests
- Updated to match intended semantics (stall-sweep test uses a working accept send_fn; accept-rollback is the new contract). Full suite 351 passed / 3 skipped.

## [1.0.24] — 2026-08-23

### Nearby chat (deep-audit round)
- **Chat sends actually work again.** The transport's `send_to_peer`/`broadcast` returned `None` (fire-and-forget), which the chat layer read as "nothing delivered" — every text message showed as failed and file transfers could not start at all. The transport now returns a real bool: delivered vs. peer-not-connected vs. send-failed, so nothing is falsely marked sent and real failures are surfaced.
- **Session ids stay in sync after a peer restarts its session** — a re-invite carrying a new session id is adopted instead of leaving both sides "active" with mismatched ids and silently dropping every later message.
- **Anonymous connections can no longer spam chat invites.** The transport gate drops all application frames from identity-less `__anon__` connections, closing the flood-by-new-TLS-connection dialog/notification DoS (each fresh connection used to get a fresh invite-rate budget).
- **Incoming chat text is sanitized** (control / bidi-override characters stripped) before it reaches OS notifications and session previews — the invite-string sanitization now covers the message body too.
- **Rate-limit bookkeeping no longer grows without bound**; text flood control is now per-direction (a peer flooding in no longer halves your send budget) and failed sends roll their quota back.
- **Stalled file transfers are swept after 10 minutes** — the state entry and the half-written `.part` temp file are cleaned up; offline detection no longer kills a session mid-transfer on a slow (TCP-retransmit) link.

### Platform & tooling
- **Updater retries transient network failures** (3 attempts with backoff) instead of failing on one blip.
- **`notify-send` failures are logged** (return code checked, stderr captured) — honouring the v1.0.21 "notification failures are logged" contract.
- **macOS autostart toggle verifies the plist still points at a real binary** — a relocated portable app no longer shows a dead "enabled" state.
- **PyInstaller spec's hidden-import fallback** now includes the chat, updater and history-db modules.
- **Docs**: README (zh + en) — new "Nearby Chat" section, corrected architecture tree and content-filter docs, test count updated; PLAN.md entry path (`src/main.py`) and Python 3.12 requirement corrected.

## [1.0.23] — 2026-08-23

### Core reliability
- **Re-copying recent content no longer gets silently swallowed.** The dedup ring is now time-bounded (90s TTL): past the window, a repeated hash is treated as a deliberate new copy and goes into history and broadcasts again.
- **Pause-sync race closed.** A capture already in flight when the user pauses no longer reaches history or the network — two post-capture re-checks honour the pause before any write or broadcast.
- **Bare email addresses are no longer redacted by default.** The sensitive-content filter treated any email as a credential and replaced it with `[FILTERED]`, corrupting ordinary clips (signatures, pasted correspondence) on the receiving side. Emails are now an opt-in "email" category; real credential patterns (tokens, passwords, keys) stay default-on.
- **Corrupt history database self-heals.** A broken `clipboard_history.db` is quarantined (`*.corrupt-<ts>`) and a fresh database started, instead of silently degrading to memory-only history that vanishes on restart.

### Transport & protocol
- **Rejection probe no longer breaks the frame stream or stalls 2s.** The post-handshake check now uses a brief timed probe and replays any bytes that weren't actually the reject marker — outbound connects no longer pay a flat 2s and no longer eat the first application frame ("connected then instantly dropped").
- **Anonymous TLS connections are reaped after 60s.** A LAN device that completes TLS but never sends an identity frame can no longer hold threads/fds forever and block legitimate pairing.
- **App-layer encryption fails closed.** When encryption is enabled, unencrypted frames are dropped instead of passed through, and repeated decrypt failures close the connection so reconnect rebuilds key state.
- **mDNS instance names are unique.** A short id-hash suffix prevents two devices sharing an 8-char prefix from colliding on one mDNS name; registration failure now retries under an altered name.
- **Peer-supplied names are sanitized** (control chars stripped, 64-char cap) before reaching logs, notifications or the UI.
- **Decoder no longer crashes the receive loop** on valid-but-non-object JSON payloads or deeply nested frames.
- **Device rename no longer reports a ghost offline;** `confirm_pairing` enforces pairing-code expiry; `forget_peer` stops every matching connection, not just the last; a `FILE_COMPLETE` wait timeout is reported as `error_timeout` instead of a misleading "peer offline".

### Web & mobile
- **Regenerating the web token no longer lands the dashboard on the "link expired" dead end** — the new token is echoed to the authenticated caller so the page can rewrite its URL before reloading.
- **Multi-select "Add to favorites" keeps the full clip** (was silently truncated to a 200-char preview) and inserts incrementally instead of delete-and-reinsert-whole-table (which could roll back favorites edited elsewhere mid-batch).
- **Settings API validates numeric ranges server-side** (port 1024–65535, history limits, debounce, …) so a bad LAN request can't leave the network layer unable to start.
- **WebSocket handshake sends the snapshot before subscribing** — a racing broadcast can no longer be clobbered by the stale snapshot's wholesale replace.
- **Transfer history shows specific failure reasons** (`error_timeout` / `error_internal`) and the completion toast no longer celebrates failed or cancelled transfers.
- **Mobile history renders BMP/TIFF image clips with the correct MIME** instead of breaking as `image/png`.
- **"Remote access" off now gates DELETE/PATCH too** (it previously only guarded GET/POST), so LAN clients really are cut off.
- **Dashboard inline language follows the app locale**, not the browser's.
- **Discovery no longer hides a genuinely new device** whose name merely extends a shorter known name.

### Desktop UX
- **Webview transfer/retrust/peer-pick dialogs no longer block the Tk main thread for up to 120s** — they run off the worker thread and marshal results back via `after(0, …)`; the send-URL flow had a fourth instance of the same bug.
- **A corrupt config file is archived** (`config.json.corrupt-<ts>`) and fields validated instead of silently resetting device identity and overwriting the file; hotkey parsing defends against non-string values.
- **macOS hotkeys require an exact modifier match** — no more accidental paste when an extra modifier is held.
- **Backups restore paired peers** (public keys re-exchanged on reconnect); **export and backup writes are atomic** (`.part` + replace); backups validate `history.json` before packaging.
- **Dashboard LAN-IP lookup moved off the UI thread** (30s cache) — the periodic 5s stutter is gone.
- **QR / phone-guide dialogs release their grab on close**, so other windows stay clickable.
- **"Port in use" shows the right command per platform** (`netstat -ano | findstr :{port}` on Windows, `lsof` elsewhere).
- **CSV import tolerates empty/non-numeric cells**; the language dropdown shows names (English / 简体中文); Linux sound fallback honours exit codes; the tray-startup-failure notice is localized.

### Nearby Chat（附近聊天）
- **全新「附近聊天」功能：与同一局域网内的设备直接通信，无需预先配对。** 在主页左侧边栏新增「💬 附近聊天」入口（沿用现有核心功能风格），可对已发现但未配对的设备发起聊天、收发文字与文件；已配对设备同样支持聊天，并断线自动重连。
- **严格的双向同意模型，未接受邀请不泄露任何内容。** 对方必须显式接受邀请后才能开始；未配对设备弹窗展示短证书指纹供线下核对，已配对设备自动接受；邀请按设备限速（5 次/5 分钟）且同时最多 3 个待处理邀请，文字按会话防洪（30 条/10 秒）。
- **文件传输复用现有分块通道**（256KB 分块、2GiB 上限），每个收件文件单独确认；文件名消毒、路径穿越拦截、重名自动改名、磁盘写入字节与预期大小双重校验。
- **会话全生命周期护栏：** 心跳保活 + 离线检测（45s ping / 150s 静默判离线）、互邀确定性收敛、并发上限、死会话自动清扫、取消配对即关闭会话；断线/失败状态在会话中可见，不再静默丢失。

## [1.0.22] — 2026-08-23

### Fixed
- **Copying no longer pops a "No devices connected" notification on every copy.** v1.0.21 added a transient warning when there were no connected peers, but with a 5-second throttle it was effectively an alert on every copy for frequent copiers. The copy now simply broadcasts to the (empty) peer set with no nag.

## [1.0.21] — 2026-08-23

### macOS & Linux platform (Round 3)
- **macOS tray menu is live again.** The tray runs in a subprocess whose menu used to be frozen — the peer list always showed "No devices" and the Web-QR item never appeared. The parent now pushes peers/web/sync state over the pipe and the child rebuilds its menu.
- **macOS can no longer go headless.** If the tray subprocess dies, it's now restarted automatically (with backoff) instead of leaving the app with no Dock icon, no tray, and no way to quit. A `_shutting_down` guard prevents a stray restart during quit.
- **`_hide_dock` uses the correct activation policy (Accessory, not Prohibited)** so CTk dialogs (settings, retrust, QR) can come to the front on macOS.
- **HiDPI / fractional scaling support** on Linux & Windows: the UI now detects the display scale and applies CTk widget/window scaling (clamped 1.0–2.5), so a 2x or GNOME-fractional display no longer renders the desktop at half size.
- **Dashboard & settings remember their window geometry** across hides and restarts (sidecar JSON per window), so Linux no longer loses position on every tray reopen.
- **Linux clipboard-tool check is backend-aware**: Wayland requires `wl-copy`/`wl-paste`, X11 requires `xclip` — the startup warning now names exactly which tool is missing instead of silently failing.
- **Linux `.desktop` autostart Exec is XDG-spec-quoted** (double quotes, not shlex single quotes) so a path with spaces or metacharacters actually launches.
- **Hotkey Accessibility detection is direct**: macOS `AXIsProcessTrusted` is probed so the "enable Accessibility" prompt fires even when the event tap is created but not trusted; the failure now also surfaces as a desktop notification.
- **Linux tray has a fallback**: if the AppIndicator backend can't start, the app logs and notifies instead of dying silently; notification failures (no daemon / no notify-send) are logged instead of swallowed.

### Transfer flow
- **"Finalizing on the receiving device…" state** replaces the confusing "Sending… 100%" while the receiver writes the file to disk; the web panel shows it with a spinner.
- **Sending to a peer that drops now fails fast with "peer went offline"** instead of hanging 120s in "awaiting-ack" — the disconnect path resolves the hashed discovery id to the real device id (the earlier wiring passed the hash and matched nothing) and fails matching transfers immediately.
- **`awaiting_ack` / `finalizing` transfers are cancellable in the classic dashboard** (previously no cancel button).
- **Copying with no peer connected gives a desktop notification in classic mode** instead of only a web toast that silently dropped when no web client was attached. *(Removed in v1.0.22 — see above — as it nagged on every copy.)*
- **Sensitive-content filtering is no longer silent**: the sender gets a throttled "Sensitive content was not synced" notice, and history/favorites items containing `[FILTERED]` show an explanatory note ("Some sensitive content was replaced with [FILTERED]").
- **Clipboard write failures surface** a "Clipboard write failed" notification instead of being swallowed.
- **Transfer sounds respect the notifications master switch** (no more dings with notifications off).
- **Web transfer history shows the specific failure reason** (disk full, size mismatch, timeout, peer offline, rejected, cancelled) instead of a generic "Failed".
- **Classic dashboard polls faster (800ms) during active transfers** so progress advances smoothly instead of lurching every 5s.

### Robustness (regressions found in self-review, fixed)
- macOS tray pipe sends are now serialized under a lock — the state-sync writer and the notification sender share one pipe and could otherwise interleave frames (desync, or a blocked send freezing the UI).
- The macOS notification pipe-sender thread is stopped before a tray restart instead of leaking and competing for the pipe.

## [1.0.20] — 2026-08-23

### Interaction & accessibility (Round 2)
- **Web dashboard is now keyboard-usable**: the right-click context menu is a real `role="menu"` with arrow-key navigation, Enter to activate, Escape to close with focus restored — and the advertised Ctrl+C / Del shortcuts now actually work (guarded so they never fire through a modal dialog). History and favorite cards activate with Enter/Space, the settings dialog is `role="dialog"` with a focus trap, peer-picker rows are proper radios with arrow-key selection, and emoji-only action buttons everywhere got accessible labels.
- **Async operations give feedback instead of silence**: overview toggles (sync / discovery / visibility / web companion) disable and show a busy state during the request and toast the real reason on failure; Show QR / Send URL no longer swallow errors; device-refresh failures show a distinct "failed to load — retry" state instead of a misleading "No devices found"; history "load more" and multi-select batch actions get busy states and failure toasts.
- **Settings dialog unsaved-changes awareness**: toggles that need a Save are now marked with an "unsaved" badge, closing with staged changes prompts for confirmation, and focus is trapped and restored.
- **Mobile-friendly polish**: toggle switches have 44px touch targets, the overview status bar wraps instead of clipping, cards get `:active` press feedback, and clip text is selectable (it's a clipboard manager — you should be able to drag-select a portion).

### Core flows — transfer failure taxonomy
- **The file-transfer failure reason is no longer discarded.** Every terminal state now carries a stable reason (`error_disk`, `error_size_mismatch`, `error_missing_chunks`, `error_security`, `rejected`, `peer_offline`, `timeout`, `cancelled`) from the transfer engine through history and into both UIs, so "Disk full" and "Peer went offline" no longer both read as a generic "File transfer failed".
- **A user-initiated cancel is reported as "Cancelled", not "Failed"** — in the notification and the history record.
- **Incoming transfer dialogs can no longer outlive the transfer**: if a request is cleaned up or the sender cancels while the prompt is open, answering shows a clear "transfer no longer available" instead of silently doing nothing.
- **Cancel can't double-notify**: the send loop and `cancel_transfer` share a once-guard so a cancel races the mid-send thread without firing two callbacks.

### Desktop (CTk) window behavior
- **⌘Q / Ctrl+Q now actually quits** the app instead of just closing the dashboard window and leaving a zombie in the tray.
- **"System" appearance mode is honored**: it resolves to the OS light/dark preference (Windows registry, macOS defaults, Linux gsettings) instead of silently rendering Light; re-opening a window re-reads the current system theme.
- **Escape no longer closes a window while you're typing** in the dashboard history search or any settings text field.
- **All themed dialogs accept Enter (default) and Escape (cancel)**, focus their default button, and give buttons a real hover state.
- Settings window closes with Escape/⌘W; the first-run language picker is now a proper modal with Tab/Enter/Escape support; the "About" tray item opens the Settings About panel instead of an ephemeral notification; hotkey-registration failures (macOS Accessibility) surface a one-time actionable dialog; toggling sync notifies "Sync active/paused"; pairing codes are grouped (1234 5678) and expired pairing requests are surfaced.

### Web companion & PWA
- **Regenerating the access token no longer bricks installed PWAs or open phone pages**: the manifest gets a stable `id`/`scope` and a "link expired — re-scan the QR" page instead of raw 403 JSON; a stale-token phone page shows guidance instead of a wrong "check your Wi-Fi" diagnosis.
- **Token is URL-encoded** when embedded in QR codes / copy-URLs, so a custom token with reserved characters can't break the flow.
- **iOS "Add to Home Screen" now yields a standalone app** (apple-mobile-web-app metas on all pages) and the quick-paste page hides its dead close button on normal browser tabs.
- **Phone pages follow the app's configured language** instead of the phone's browser language; quick-paste history fetches get an 8s timeout with a Retry state; mobile tap targets are ≥44px; the iOS auto-zoom-on-focus bug is fixed (16px textarea).
- **Static assets are always revalidated** (`no-cache` + ETag/304) so an app update never serves stale JS/CSS, while unchanged assets stay cheap over LAN.

## [1.0.19] — 2026-08-22

### Platform UX (macOS / Linux) — typography & theme
- **Cross-platform UI font resolution.** CustomTkinter defaults every font to "Roboto", which is missing on most macOS/Linux installs — so the whole desktop UI fell back to Tk's dated default. A new `internal/ui/fonts.py` resolves the best actually-installed UI font per platform (SF Pro / Helvetica on macOS, Segoe UI on Windows, Noto Sans / Ubuntu / Cantarell on Linux, with CJK fallbacks like PingFang SC / Microsoft YaHei / WenQuanYi) and applies it to every widget via a small `CTkFont` patch — no per-widget churn. Tk's default font is aligned too.
- **Web UI font stack is now system-native on every OS.** `--clipsync-font` lists each platform's UI font with interleaved CJK fallbacks (PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, WenQuanYi) so Chinese text — the app's default UI — renders crisply instead of falling back to an unrelated font, especially on Linux.
- **Custom aurora CTk theme.** The desktop windows now use a cyan→violet theme (`assets/themes/clipsync.json`) matching the web UI's brand palette instead of CustomTkinter's stock blue. Headers, sidebars, buttons and dialog accents were recolored to the same cyan/violet family.
- **Firefox scrollbar styling.** `::-webkit-scrollbar` is Chromium-only; Firefox (common on Linux) now gets matching thin styled scrollbars via `scrollbar-width`/`scrollbar-color`.
- **macOS keyboard shortcuts.** The dashboard now binds ⌘W (hide) and ⌘Q (close) on macOS instead of only Ctrl+W/Ctrl+Q.
- **macOS webview window size.** Chrome-based `--app` windows on macOS now pass `--window-size`, so the dashboard opens at the requested 960×720 instead of a browser-default size.

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
