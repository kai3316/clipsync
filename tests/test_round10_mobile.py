"""Round 10 — mobile page alignment with the desktop dashboard.

mobile.html is the phone companion; its features had drifted behind the
desktop dashboard (internal/web/static/components/*).  This round closes the
verified gaps using ONLY existing REST endpoints:

1. Transfers: "cancel all" button on the active section (POST
   /api/transfer/cancel-all) and Retry on failed OUTGOING history rows (POST
   /api/transfer/retry) — desktop transfer-panel parity.
2. Chat: per-session mute bell (POST /api/chat/mute), close-session button in
   the conversation header (POST /api/chat/close), and a 📎 file attachment
   flow (purpose=chat upload -> POST /api/chat/file) — desktop chat-panel
   parity.
3. History: pin/unpin (POST /api/pin), push-to-computer clipboard (POST
   /api/paste-rich), favorite (POST /api/favorites), delete (POST /api/delete)
   in the detail modal, plus clear-all (POST /api/history/clear) — desktop
   context-menu / panel parity.
4. Settings: compact diagnostics card (GET /api/diagnostics + POST
   /api/diagnostics/request) mirroring diagnostics-panel.js.

The mobile page keeps its own inline bilingual T() dictionary (the shared
locales/*.json are dashboard-only), so every new string must exist in both
languages inline.  These tests are static by nature — they pin the endpoint
wiring, the interaction guards, and the bilingual strings — plus a node
--check pass over every extracted inline <script> block when node is
available.
"""

import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_STATIC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "internal", "web", "static",
)


def _mobile_path():
    return os.path.join(_STATIC, "mobile.html")


@pytest.fixture(scope="module")
def html():
    with open(_mobile_path(), encoding="utf-8") as f:
        return f.read()


def _scripts(html_text):
    return re.findall(r"<script>(.*?)</script>", html_text, re.S)


# ═════════════════════════════════════════════════════════════════════════
# 1. Transfers — cancel all + retry failed outgoing
# ═════════════════════════════════════════════════════════════════════════


class TestTransfersParity:
    def test_cancel_all_button_wired_to_endpoint(self, html):
        # Button anchor rendered into the Active section header...
        assert 'data-act="cancelall"' in html
        assert "tr-cancelall" in html
        # ...handled separately from the single-transfer actions (it takes no
        # transfer_id and hits its own endpoint).
        assert "apiFetch('/api/transfer/cancel-all'" in html
        assert "act === 'cancelall'" in html
        # Double-tap guard while the request is in flight.
        assert "transfersCancellingAll" in html

    def test_cancel_all_bilingual_strings(self, html):
        assert "trCancelAll: ZH ? '取消全部' : 'Cancel all'," in html
        assert "trCancelAllDone: ZH ? '✓ 已取消 {n} 个传输' : '✓ Cancelled {n} transfers'," in html
        # The count placeholder is actually substituted.
        assert "trCancelAllDone.replace('{n}'" in html

    def test_retry_only_on_failed_outgoing_history_rows(self, html):
        assert "function canRetryTransfer(h)" in html
        # Same eligibility rule as the desktop panel's canRetry: outgoing,
        # not completed, not cancelled — otherwise the host cannot re-send.
        assert "h.direction === 'up'" in html
        assert "h.status !== 'completed'" in html
        assert "h.status === 'cancelled' || h.cancelled" in html
        assert "apiFetch('/api/transfer/retry'" in html
        assert "data-retry-id=" in html

    def test_retry_double_tap_guard_is_per_row(self, html):
        assert "retryBusyId" in html
        assert "retryBusyId === h.id ? ' disabled'" in html

    def test_transfer_actions_surface_stale_token(self, html):
        # New control paths must keep the shared 403 -> re-scan-QR behavior.
        assert "notifyTokenExpired(); renderTransfers(); return;" in html


# ═════════════════════════════════════════════════════════════════════════
# 2. Chat — mute bell / close session / file attachment
# ═════════════════════════════════════════════════════════════════════════


class TestChatParity:
    def test_sessions_response_mute_set_captured(self, html):
        # GET /api/chat/sessions carries {sessions, muted}; the page must
        # read both (it used to drop `muted`).
        assert "(out[1] && out[1].sessions) || []" in html
        assert "(out[1] && out[1].muted) || []" in html
        assert "chatMutedSet" in html

    def test_mute_bell_on_session_rows(self, html):
        assert "data-mute-peer=" in html
        assert "apiFetch('/api/chat/mute'" in html
        # Payload shape matches the backend contract {peer_id, muted}.
        assert "{ peer_id: peerId, muted: newMuted }" in html
        # Bell tap must never open the conversation: the mute branch is
        # checked BEFORE the session-row branch.
        assert html.index("data-mute-peer") < html.index("data-session-id")
        # Bilingual tooltips + both bell states.
        assert "chatMute: ZH ? '静音' : 'Mute'," in html
        assert "chatUnmute: ZH ? '取消静音' : 'Unmute'," in html
        assert "'🔕' : '🔔'" in html

    def test_close_session_with_confirm_and_back(self, html):
        assert "id=\"chatCloseBtn\"" in html
        assert "apiFetch('/api/chat/close'" in html
        # Destructive-ish action gated behind a confirm, and success returns
        # to the session list instead of leaving a dead conversation open.
        assert "window.confirm(T.chatCloseConfirm)" in html
        assert "chatCloseConfirm: ZH ? '关闭并移除这个会话？' : 'Close and remove this conversation?'," in html
        assert "chatBack();" in html

    def test_attachment_uses_chat_purpose_upload_then_send(self, html):
        # Hidden picker + composer button.
        assert 'id="chatFileInput"' in html
        assert "id=\"chatAttachBtn\"" in html
        # Step 1: purpose=chat upload (lands in the server temp dir, skips
        # receive notification/Files record).
        assert "form.append('purpose', 'chat');" in html
        # Step 2: hand the returned absolute temp path to /api/chat/file.
        assert "apiFetch('/api/chat/file'" in html
        assert "file_path: data.path" in html
        # The bare-name legacy flow resolves against the received-files dir,
        # which is WRONG for purpose=chat uploads — require the path field.
        assert "data.ok && data.path" in html

    def test_attachment_size_preflight_and_xhr(self, html):
        # Same 128 MB cap (minus multipart headroom) as the Files tab.
        assert "MAX_UPLOAD_BYTES - 64 * 1024" in html
        # Large uploads must not go through the 8s fetch timeout — plain XHR.
        assert "new XMLHttpRequest();" in html

    def test_attachment_busy_state_survives_poll_rerenders(self, html):
        # The composer template renders the busy state so a poll landing
        # mid-upload doesn't resurrect an enabled 📎 button.
        assert "(chatFileBusy ? ' disabled' : '')" in html
        assert "(chatFileBusy ? '…' : '📎')" in html


# ═════════════════════════════════════════════════════════════════════════
# 3. History — pin / push / favorite / delete / clear all
# ═════════════════════════════════════════════════════════════════════════


class TestHistoryParity:
    def test_pin_toggle_updates_local_order(self, html):
        assert "apiFetch('/api/pin'" in html
        assert "{ entry_id: modalEntryId }" in html
        # Pinned rows carry a visible marker in the list...
        assert "item.pinned ? '📌 '" in html
        # ...and the local list is re-partitioned pinned-first like the
        # server returns it (stable partition, not sort).
        assert "function reorderHistoryPinnedFirst()" in html
        assert "histPinBtn.textContent = item.pinned ? T.histUnpin : T.histPin;" in html

    def test_push_to_computer_clipboard(self, html):
        # paste-rich writes ALL stored formats (incl. image bytes) to the PC
        # clipboard — the phone-side copy button only writes the phone's.
        assert "apiFetch('/api/paste-rich'" in html
        assert "histPushBtn" in html
        assert "histPushOk: ZH ? '✓ 已写入电脑剪贴板' : '✓ Written to the computer clipboard'," in html

    def test_favorite_fetches_full_text_first(self, html):
        # List previews are truncated — favorites must come from the detail
        # payload, same as the desktop context menu.
        assert "getJson('/api/history/item?entry_id='" in html
        assert "apiFetch('/api/favorites'" in html
        assert "{ title: titleLine, content: content, group: '' }" in html

    def test_delete_shrinks_pagination_cursor(self, html):
        assert "apiFetch('/api/delete'" in html
        assert "window.confirm(T.histDeleteConfirm)" in html
        # Cursor must shrink with the array or "load more" skips an entry
        # (same contract as store.historyOffset on the desktop).
        assert "historyOffset = Math.max(0, historyOffset - 1);" in html

    def test_modal_actions_resolve_by_entry_id_not_index(self, html):
        # List indices shift between polls; the modal tracks entry_id.
        assert "modalEntryId = (item.entry_id !== undefined && item.entry_id !== null) ? item.entry_id : null;" in html
        assert "function findHistById(eid)" in html

    def test_favorite_disabled_for_image_only_clips(self, html):
        # Image-only entries have no TEXT payload to favorite.
        assert "isImageOnly" in html
        assert "types.IMAGE || types.IMAGE_EMF) && !types.TEXT" in html

    def test_clear_all_gated_and_resets_cursor(self, html):
        assert "apiFetch('/api/history/clear'" in html
        assert "window.confirm(T.histClearConfirm)" in html
        assert "historyItems.splice(0, historyItems.length);" in html
        assert "historyHasMore = false;" in html
        # The affordance is only visible when there is something to clear.
        assert "histBar.style.display = historyItems.length > 0 ? 'flex' : 'none';" in html

    def test_one_inflight_modal_action_guard(self, html):
        # A double-tap on any modal action must not fire two POSTs.
        assert "function modalAction(fn)" in html
        assert "modalActionBusy" in html


# ═════════════════════════════════════════════════════════════════════════
# 4. Settings — diagnostics card
# ═════════════════════════════════════════════════════════════════════════


class TestDiagnosticsCard:
    def test_scan_and_fix_endpoints_wired(self, html):
        assert "getJson('/api/diagnostics')" in html
        assert "apiFetch('/api/diagnostics/request'" in html
        # Same action mapping as diagnostics-panel.js.
        assert "'local_network'" in html
        assert "'firewall'" in html

    def test_check_labels_cover_server_ids(self, html):
        for check_id in ("server_port", "discovery", "advertising",
                         "web_companion", "network", "firewall",
                         "permissions", "mdns", "clipboard_tool"):
            assert f"{check_id}: T.diag" in html

    def test_summary_states_bilingual(self, html):
        assert "diagAllOk: ZH ? '✓ 全部正常' : '✓ All good'," in html
        assert "diagWarn: ZH ? '⚠ 存在警告' : '⚠ Warnings found'," in html
        assert "diagFail: ZH ? '✕ 发现问题' : '✕ Problems found'," in html
        # Summary class carries the server verdict (ok / warn / fail).
        assert "diag-summary--' + esc(diagState.summary)" in html

    def test_results_survive_poll_rerenders(self, html):
        # Scan state lives outside the HTML and is re-applied after each
        # rebuild (loadSettings renders every poll tick).
        assert "var diagState =" in html
        assert "renderDiag();" in html
        assert "function renderSettingsView()" in html

    def test_firewall_fix_triggers_rescan(self, html):
        assert "setTimeout(runDiagnostics, 2500);" in html


# ═════════════════════════════════════════════════════════════════════════
# 5. i18n completeness + structural regressions
# ═════════════════════════════════════════════════════════════════════════

_NEW_T_KEYS = [
    "histClear", "histClearConfirm", "histCleared", "histClearFail",
    "histPin", "histUnpin", "histPinnedToast", "histUnpinnedToast",
    "histPinFail", "histPush", "histPushOk", "histPushFail",
    "histFav", "histFavOk", "histFavFail",
    "histDel", "histDeleteConfirm", "histDeleted", "histDelFail",
    "trCancelAll", "trCancelAllDone", "trRetry", "trRetryFail",
    "chatMute", "chatUnmute", "chatMuteFail",
    "chatCloseSession", "chatCloseConfirm",
    "chatAttach", "chatFileSentOk", "chatFileSendFail",
    "diagTitle", "diagHint", "diagRun", "diagScanning", "diagScanFail",
    "diagAllOk", "diagWarn", "diagFail",
    "diagFix", "diagFixDone", "diagFixFail",
    "diagServerPort", "diagDiscovery", "diagAdvertising", "diagWeb",
    "diagNetwork", "diagFirewall", "diagPermissions", "diagMdns",
    "diagClipboardTool",
]


class TestInlineI18n:
    @pytest.mark.parametrize("key", _NEW_T_KEYS)
    def test_every_new_string_exists_inline(self, html, key):
        # The mobile page owns its translations — each key must appear in
        # the T dictionary (both languages live in the single entry).
        pattern = rf"^\s*{key}: ZH \? '.+' : '.+',\s*$"
        assert re.search(pattern, html, re.M), f"missing inline T entry: {key}"

    def test_existing_round7_export_parity_untouched(self, html):
        assert 'id="favExportBtn"' in html
        assert "apiFetch('/api/favorites/export'" in html
        assert "{ format: 'markdown' }" in html

    def test_typing_indicator_still_present(self, html):
        assert "chatTypingRow" in html
        assert "chatPeerTypingUntil" in html


# ═════════════════════════════════════════════════════════════════════════
# 6. Inline script syntax (node --check over each <script> block)
# ═════════════════════════════════════════════════════════════════════════


class TestScriptSyntax:
    def test_all_inline_scripts_parse(self, html, tmp_path):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available")
        blocks = _scripts(html)
        assert len(blocks) >= 3, "expected the head helpers + main app script"
        for i, block in enumerate(blocks):
            path = os.path.join(str(tmp_path), f"block_{i}.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(block)
            proc = subprocess.run(
                [node, "--check", path],
                capture_output=True, text=True, timeout=30,
            )
            assert proc.returncode == 0, (
                f"inline <script> block {i} fails node --check:\n{proc.stderr}"
            )
