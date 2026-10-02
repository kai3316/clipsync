# The Legacy Surface, Route by Route

The migration's obligation is that the new page can do everything the two legacy
desktop fronts could.  The enumeration behind that claim was done by hand (§十 of
`migration-parity-audit.md`) and had to be corrected by §十二, which is what a
sweep is: true of the tree it was done on, and drifting the moment the tree
moves.  This file is the same enumeration written where a test can hold it.

It comes to **101 routes**.  97 of them are reached by the desktop web panel — 58
by the panel alone and 39 by the panel and the phone together — and those 97 carry
the obligation.  3 belong to the phone alone, which is kept rather than replaced
and owes the new page nothing.  1 is served and reached by neither page.

## What is measured, and what is judged

The **route** column and the **referenced in** column are read from source by
`tests/sidecar/test_legacy_surface_map.py`, not written here: the routes come
from an AST walk of the paths the web server compares against, and the
referenced-in column from the `/api/...` strings in each front's own files.  A
route added, removed or renamed in `internal/web/` turns that test red until this
table is updated.

*Referenced in* means "this front's source names that route": the route is a
literal in it, or a literal there ends in `/` and the route sits under it.  The
second clause is needed because both fronts build the chat and transfer actions
by concatenation — `api.js:1158` writes `'/api/chat/file/' + action` and
`mobile.html:3188` does the same — so a literal-only scan would read those
sub-routes as used by neither page.  Exactly four literals trigger it, two per
front, and they can be checked in one grep:

    /api/chat/        /api/chat/file/     (internal/web/static/js, components)
    /api/chat/        /api/chat/file/     (mobile.html)
    /api/transfer/                        (mobile.html)

The column is still a scan of the *source*, not of the behaviour, so it can also
over-claim in the other direction: a client method nothing invokes counts.  The
notes use that gap rather than hiding it — `/api/files` is named by the panel and
unreachable from any of its controls (`js/api.js:284`), which is how the audit
recorded it.

The **in the new page** column is a judgement, and it is the one thing no test
here can check.  The test holds that each named target *resolves* — a Tauri
command that `generate_handler!` registers, or one of the explicit non-commands
below — so a renamed or deleted command is caught.  It cannot check that the
target really is the counterpart of that route.  That is a reading of two
implementations, which is what the audit sections are for.

`panel` — `internal/web/static/js/` and `components/`, the desktop web panel.
`phone` — `internal/web/static/mobile.html`, the companion, which is **kept**
rather than replaced: a route only the phone calls owes the new page nothing, and
is listed so the two kinds cannot be confused.

The explicit non-commands: **front-end** (the window does it in Vue or through a
Tauri window API, with nothing to call), **companion only** (the phone's route,
which the new page need not have), and **not carried** (a desktop-panel route the
new page does not have — none are left, and adding one means saying so here).

**That judgement column has since been read, and eight rows were wrong.**  The
first four rows re-read against both implementations turned up two errors, so the
rest were read too.  Six had the wrong *target* — `/api/diagnostics`,
`/api/history/item`, `/api/nav`, `/api/restart`, `/api/upload` and
`/api/visibility/toggle` — and two had a note that was wrong or missing
(`/api/dialog-response`, whose target was right but whose note hid the one
capability it alone carries, and `/api/discovery/toggle`, which needed saying
that it is the other half of a pair).  Each row below says what it was and what
it is now.  The target errors failed in three recognisable ways, and the ways are
worth more than the list:

- **A name that misleads.**  `/api/visibility/toggle` is not window visibility —
  it is whether this machine announces itself on the LAN, and its callback is
  `start_advertising`.  Reading the name instead of the handler produced a row
  claiming the window did it in Vue, when a command existed all along.
- **A name that matches and a behaviour that does not.**  `/api/restart` restarts
  the whole application (`_restart_app` spawns a fresh instance and exits), not
  the sidecar; naming both commands widened the claim past what the route did.
- **Naming a neighbour.**  `/api/diagnostics` had `read_logs` on it because the
  report carries a log-*path* check; reading logs is `/api/logs`.

The guarantee this column carries is therefore a read one, not a checked one, and
those eight are the measure of the difference.  It is now a reading that has been
done — not one a test keeps true.

**And it cannot be made a checked one, which was measured rather than assumed**
(§十四 of `migration-parity-audit.md`).  The obvious mechanical substitute — take
the closure of the calls the route's handler makes and the closure of the calls
the command's RPC method makes, and require them to meet — was built and run
against this very table.  It is silent on **72 of the 96 rows that have a command
target**, and where it does speak it describes the neighbourhood rather than the
pair: `/api/transfer/cancel`, `/api/transfer/pause`, `/api/transfer/resume` and
`/api/transfer/retry` are reported as sharing **one bit-identical set of 22
symbols** holding six different `FileTransferManager` operations at once.  On the
one row read by hand all the way down — `/api/visibility/toggle` and
`set_discovery_visible`, which both call `Discovery.start_advertising`
(`internal/transport/discovery.py:483`) — it reports nothing shared.  So the
reading above is not a task waiting to be automated; it is the only instrument
this pairing admits.

| Route | Referenced in | In the new page | Note |
| --- | --- | --- | --- |
| `/api/aiconfig/inventory` | panel | ai_inventory |  |
| `/api/aiconfig/local` | panel | ai_local | the listing action |
| `/api/aiconfig/local/item` | panel | ai_local | the read action |
| `/api/aiconfig/local/save` | panel | ai_local | the save action |
| `/api/aiconfig/local/trash` | panel | ai_local | the trash action |
| `/api/aiconfig/open` | panel | ai_local | the open action |
| `/api/aiconfig/preview` | panel | ai_preview |  |
| `/api/aiconfig/profiles` | panel | ai_profiles, update_ai_profiles |  |
| `/api/aiconfig/pull` | panel | ai_pull |  |
| `/api/backup` | panel | create_backup |  |
| `/api/backups` | panel | list_backups |  |
| `/api/batch-delete` | panel | batch_delete_history |  |
| `/api/batch-favorite` | panel | batch_favorite_history |  |
| `/api/batch-pin` | panel | batch_pin_history |  |
| `/api/chat/accept` | both | accept_chat_invite | only reached with 必须经我同意 on, where the invitation waits for this answer |
| `/api/chat/close` | both | close_chat |  |
| `/api/chat/decline` | both | decline_chat_invite | the refusal is told to the peer and the session is reported as declined |
| `/api/chat/devices` | both | list_chat_devices |  |
| `/api/chat/download` | both | open_chat_file | the phone downloads the attachment; the window opens the saved copy |
| `/api/chat/file` | both | send_chat_file |  |
| `/api/chat/file/accept` | both | chat_file_action | the offer is held until this arrives; 等待接受 is what the card shows meanwhile |
| `/api/chat/file/cancel` | both | chat_file_action |  |
| `/api/chat/file/decline` | both | chat_file_action |  |
| `/api/chat/invite` | both | invite_chat |  |
| `/api/chat/messages` | both | list_chat_messages |  |
| `/api/chat/mute` | both | set_chat_muted |  |
| `/api/chat/read` | both | mark_chat_read |  |
| `/api/chat/resend` | both | resend_chat_text |  |
| `/api/chat/sessions` | both | list_chat_sessions |  |
| `/api/chat/text` | both | send_chat_text |  |
| `/api/chat/typing` | both | chat_typing |  |
| `/api/data/open-folder` | panel | open_data_folder |  |
| `/api/delete` | both | delete_history |  |
| `/api/device/connect` | panel | connect_device |  |
| `/api/device/disconnect` | panel | disconnect_device |  |
| `/api/device/forget` | panel | forget_device |  |
| `/api/device/note` | panel | set_device_note |  |
| `/api/device/pair` | panel | start_pairing, confirm_pairing |  |
| `/api/device/purge` | panel | purge_device |  |
| `/api/device/reject` | panel | reject_pairing |  |
| `/api/device/restore` | panel | restore_device |  |
| `/api/device/test` | panel | test_device |  |
| `/api/device/unpair` | panel | unpair_device |  |
| `/api/devices` | panel | list_devices |  |
| `/api/devices/certs` | panel | device_certs |  |
| `/api/diagnostics` | both | diagnostics_report | one check in the report is the log *path* (is it writable); the report carries no log content, and reading logs is `/api/logs` |
| `/api/diagnostics/request` | both | diagnostics_request |  |
| `/api/dialog-response` | panel | front-end | only the answer channel: the legacy host raised four round-trip dialogs and this is where the answer came back.  Each one is a command of its own elsewhere in this table — the incoming transfer (`transfer_action`), the URL prompt and the peer picker (the window's own inputs, then `send_url`), and the certificate-change prompt (`device_retrust`, which is named **nowhere else** — it reaches the user through a WebSocket event and so has no route to be listed under).  The window renders its own dialogs and sends the answer as that dialog's command |
| `/api/discovery/toggle` | panel | set_discovery_enabled | the browsing half of the pair below: `start_browsing` / `stop_browsing` (`src/main.py:7445`) |
| `/api/download` | phone | companion only | the phone's download route; the window opens a received file instead |
| `/api/export` | panel | export_history |  |
| `/api/favorites` | both | list_favorites, get_favorite, add_favorite, update_favorite |  |
| `/api/favorites/export` | both | export_favorites |  |
| `/api/file/open` | panel | open_chat_file, transfer_action | a saved attachment or a landed transfer, named by what it is rather than by a path |
| `/api/file/reveal` | panel | reveal_chat_file, transfer_action |  |
| `/api/files` | both | share_file_to_phone | the phone's shared directory.  `js/api.js:284` has the client method and no control in the panel calls it; it is live for the companion, which is why it is not a dead route |
| `/api/history` | both | list_history |  |
| `/api/history/clear` | both | clear_history |  |
| `/api/history/item` | both | read_history_text | a GET that returns one entry's full `types`, which is how the old panel got the formats to copy.  The window is never handed the raw formats — copying with them is the host's job (`copy_history`, reached from `/api/paste-rich`) — so what the window reads back is the text.  Deleting is `/api/delete`, opening a link is `/api/nav` |
| `/api/import` | panel | import_history |  |
| `/api/internetdelivery` | panel | relay_delivery_status |  |
| `/api/internetdelivery/counts` | none | relay_delivery_status | no page calls it: it is served and covered by the companion tests, and the panel reads the same counts from the delivery route |
| `/api/internetpair/enter` | panel | internet_pairing_enter |  |
| `/api/internetpair/generate` | panel | internet_pairing_generate |  |
| `/api/internetpair/rename` | panel | internet_pairing_rename |  |
| `/api/internetpair/status` | panel | internet_pairing_status |  |
| `/api/internetpair/test` | panel | internet_pairing_test |  |
| `/api/internetpair/unpair` | panel | internet_pairing_unpair |  |
| `/api/logs` | panel | read_logs |  |
| `/api/nav` | panel | open_history_link | the history row's context menu, 在浏览器打开 (`context-menu.js:316`): the panel reads a clip's text and hands it to the browser.  The window does the same through `history.open_link`, which is the one place a link is opened and the only one that may hold a URL — a row names a row, never a URL.  The route's other half, `device_id`, forwards the URL to a peer (`on_nav_url`, `src/main.py:1074`); no caller in either front passes one |
| `/api/overview` | panel | get_overview |  |
| `/api/paste` | panel | copy_history, copy_text | the panel reported a paste so the host could count it; the new page copies through the command that knows the entry |
| `/api/paste-rich` | both | copy_history, copy_text |  |
| `/api/pin` | both | set_history_pinned |  |
| `/api/push` | both | push_text |  |
| `/api/restart` | panel | restart_app | the callback spawns a fresh instance and exits this one (`_restart_app`, `src/main.py:3590`) — an app restart, not a sidecar one.  The window has both, and `restart_sidecar` is the one this route did **not** offer |
| `/api/restore` | panel | restore_backup |  |
| `/api/send_url` | panel | send_url |  |
| `/api/settings` | both | get_settings, update_settings |  |
| `/api/show_qr` | panel | companion_qr | the panel asked the host to put the QR on screen; the window has the pairing dialog |
| `/api/speed-test` | panel | start_speed_test |  |
| `/api/status` | both | get_app_status |  |
| `/api/sync/pause` | panel | pause_sync |  |
| `/api/sync/resume` | panel | resume_sync |  |
| `/api/transfer` | both | list_transfers |  |
| `/api/transfer/accept` | phone | companion only | the phone's own accept; the window accepts from the transfer row |
| `/api/transfer/cancel` | both | transfer_action |  |
| `/api/transfer/cancel-all` | both | cancel_all_transfers |  |
| `/api/transfer/history/delete` | both | clear_transfer_history |  |
| `/api/transfer/pause` | both | transfer_action |  |
| `/api/transfer/reject` | phone | companion only | the phone's own reject; the window rejects from the transfer row |
| `/api/transfer/resume` | both | transfer_action |  |
| `/api/transfer/retry` | both | transfer_action |  |
| `/api/translate` | panel | translate_text |  |
| `/api/update/check` | panel | update_check |  |
| `/api/update/download` | panel | update_download |  |
| `/api/update/open-folder` | panel | update_open_folder |  |
| `/api/update/status` | panel | update_status |  |
| `/api/upload` | both | share_file_to_phone, send_files, send_chat_file | three callers, and the route's name fits only the first.  The phone uploads into the shared directory.  The panel's 发送文件到手机 does the same (`dialog-modal.js:306`).  The panel's **chat** panel uploads an attachment to get back an absolute path to put in the message (`chat-panel.js:903`) — the window attaches through `send_chat_file` instead, so no path crosses.  And a `target_device` forwards the file to a peer (`server.py:1890`), which is `send_files` |
| `/api/visibility/toggle` | panel | set_discovery_visible | **not window visibility**, which the name invites: the callback is `start_advertising` / `stop_advertising` (`src/main.py:7450`) — whether this machine announces itself on the LAN.  Its partner is `/api/discovery/toggle` above (browsing), and the panel offers both as two switches (`overview-panel.js:333`, `:352`).  The window's own show/hide is its window and the tray, and needs no command |
| `/api/window` | panel | front-end | closing the window is the window's own; ending the session is `app.shutdown`, reached from the tray |
