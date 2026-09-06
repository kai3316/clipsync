"""Round 16 — chat + device internet fusion (web UI layer).

Static wiring assertions for fusing internet-paired peers into the chat
panel's "start a chat" target selector, the session list, and the display
labels, plus the one-line privacy note on the Devices page:

  1. chat-panel.js renders an "Internet devices" group for internet-ONLY
     peers and dedups a dual-online peer (LAN + internet) so it appears
     once with a 🌐 badge instead of twice.
  2. Starting a chat from either group goes through the existing
     ClipsyncAPI.chatInvite(peer_id, name) flow (peer_id is the key).
  3. Internet targets render an online dot; offline ones carry the
     "the peer may be offline" hint but stay startable.
  4. Session rows / conversation header prefer the internet alias
     (alias || name || peer_id) and derive the online dot from the live
     internet-pair state as well as the backend's own flag.
  5. device-panel.js's internet-pairing section carries a single-line
     privacy note reusing settings_window.internet_sync_hint.
  6. en / zh-CN key sets identical and every round-16 key present and
     non-empty in both.
  7. A node --check pass over every JS file this round touched (when
     node is available).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")


def _read(*parts) -> str:
    with open(os.path.join(_STATIC, *parts), encoding="utf-8") as f:
        return f.read()


# ── 1. Internet group + dedup in the chat target selector ───────────────


def test_chat_panel_has_internet_target_group():
    src = _read("components", "chat-panel.js")
    # The internet-ONLY list is a computed over store.internetPairPeers.
    assert "internetDeviceList: function" in src
    assert "this.store.internetPairPeers" in src
    # The group header + subheader are rendered in the device section.
    assert "chat.internet_devices_header" in src
    assert "chat-subheader" in src
    # Empty-state message only when BOTH lists are empty.
    assert "internetDeviceList.length === 0" in src


def test_dual_online_lan_row_carries_internet_badge():
    src = _read("components", "chat-panel.js")
    chunk = src.split("LAN targets")[1]
    assert "d.internet" in chunk
    assert "chat-badge--internet" in chunk
    assert "devices.netpair_also_internet" in chunk
    # The badge mirrors the Devices page's online/offline relay labels.
    assert "devices.netpair_online" in chunk
    assert "devices.netpair_offline" in chunk


def test_internet_group_dedups_lan_present_peers():
    src = _read("components", "chat-panel.js")
    # internetDeviceList skips a peer that is already a LAN chat target.
    chunk = src.split("internetDeviceList: function")[1].split("activeSession: function")[0]
    assert "lanIds[String(p.peer_id)]" in chunk
    assert "continue" in chunk
    # A peer whose paired flag is explicitly false is not a chat target.
    assert "p.paired === false" in chunk
    # deviceList augments a dual peer with the alias-priority name instead
    # of letting it appear twice (under LAN and under the internet group).
    dev = src.split("deviceList: function")[1].split("internetDeviceList: function")[0]
    assert "internet: true" in dev
    assert "name: netPeer.alias || d.name" in dev


# ── 2. Start chat goes through peer_id → chat_invite ────────────────────


def test_start_chat_uses_peer_id_through_existing_flow():
    src = _read("components", "chat-panel.js")
    assert "ClipsyncAPI.chatInvite(device.peer_id, device.name || device.peer_id)" in src
    # The internet-only Start button reuses the same startChat(peer) path.
    assert '@click="startChat(p)"' in src
    # Internet-targeted connecting toast uses the new key.
    assert "chat.internet_connecting" in src
    # Defensive: the existing LAN path is untouched (no new endpoint).
    assert "chatInvite(" in src


def test_internet_peers_fetched_on_mount_defensively():
    src = _read("components", "chat-panel.js")
    # created() fetches the pairing status so the group shows before the
    # Devices page is ever opened, guarded so an old backend is a no-op.
    assert "this.store.fetchInternetPairStatus" in src
    assert "if (this.store.fetchInternetPairStatus)" in src


# ── 3. Online dot + offline hint on internet targets ────────────────────


def test_internet_target_online_dot_and_offline_hint():
    src = _read("components", "chat-panel.js")
    chunk = src.split("Internet-only targets")[1]
    assert "chat-device-row__dot" in chunk
    assert "p.online" in chunk
    assert "chat-session-row__dot--on" in chunk
    assert "chat-session-row__dot--off" in chunk
    # Offline peers stay startable but carry the "may be offline" hint.
    assert "!p.online" in chunk
    assert "chat.internet_offline_hint" in chunk


# ── 4. Session title alias-priority + effective online state ────────────


def test_session_title_prefers_alias():
    src = _read("components", "chat-panel.js")
    assert "net.alias || net.name || fallback || peerId" in src
    assert "chatPeerName(s.peer_id, s.peer_name)" in src
    assert "chatPeerName(activeSession.peer_id, activeSession.peer_name)" in src
    assert "chatPeerName(activeSession.peer_id, activeSession.peer_name)" in src


def test_session_online_dot_uses_effective_state():
    src = _read("components", "chat-panel.js")
    assert "sessionOnline(s)" in src
    chunk = src.split("sessionOnline: function")[1].split("sessionStatusLabel: function")[0]
    assert "internetPeerFor(s.peer_id)" in chunk
    # Reachable when EITHER the LAN channel or the relay channel is up.
    assert "lanOn || netOn" in chunk
    # The session row shows a small 🌐 marker for internet-paired peers.
    assert "internetPeerFor(s.peer_id)" in src
    assert "chat-session-row__net" in src


# ── 5. Device page privacy note (round-16 optional, coordinator add-on) ──


def test_device_panel_privacy_note_reuses_internet_hint():
    src = _read("components", "device-panel.js")
    assert "netpair-privacy" in src
    assert "settings_window.internet_sync_hint" in src
    # The note sits right under the overview row, a single line.
    assert src.index("netpair-overview") < src.index("netpair-privacy")


def test_chat_internet_css_present():
    css = _read("index.html")
    for cls in (
        ".chat-badge--internet",
        ".chat-device-row__dot",
        ".chat-device-row__hint",
        ".chat-subheader",
        ".chat-session-row__net",
        ".netpair-privacy",
    ):
        assert cls in css, f"missing CSS rule: {cls}"


# ── 6. Locale parity ────────────────────────────────────────────────────

_NEW_KEYS = [
    "chat.internet_devices_header",
    "chat.internet_offline_hint",
    "chat.internet_connecting",
]


def _locales():
    with open(os.path.join(_STATIC, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def test_locale_key_sets_identical():
    en, zh = _locales()
    en_only = set(en) - set(zh)
    zh_only = set(zh) - set(en)
    assert not en_only, f"keys missing from zh-CN.json: {sorted(en_only)}"
    assert not zh_only, f"keys missing from en.json: {sorted(zh_only)}"


def test_new_round16_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
    for key in _NEW_KEYS:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key


def test_locale_json_files_still_parse():
    en, zh = _locales()
    assert len(en) > 1000 and len(zh) > 1000


# ── 7. JS syntax (node --check over every file this round touched) ──────


_TOUCHED_JS = [
    ("components", "chat-panel.js"),
    ("components", "device-panel.js"),
]


def test_touched_js_passes_node_check(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        proc = subprocess.run(
            [node, "--check", path],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"


def test_no_nul_bytes_in_touched_js():
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        with open(path, "rb") as f:
            data = f.read()
        assert b"\x00" not in data, f"NUL byte found in {os.path.join(*parts)}"


# ══════════════════════════════════════════════════
# merged from test_round5_desktop.py
# ══════════════════════════════════════════════════

import inspect
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.i18n import _EN, _ZH, T
from internal.sync.nearby_chat import ChatManager
from internal.ui.dashboard import DashboardWindow, _chat_text_resendable

PEER = "peer-a"

NEW_I18N_KEYS = (
    "chat.text_failed",
    "chat.resend",
    "chat.err_resend_failed",
)


# ══════════════════════════════════════════════════════════════════
# 1. The renderer's decision function
# ══════════════════════════════════════════════════════════════════


class TestResendableDecision:
    def test_failed_outgoing_text_is_resendable(self):
        assert (
            _chat_text_resendable(
                {"kind": "text", "outgoing": True, "status": "failed"},
            )
            is True
        )

    def test_other_statuses_are_not_resendable(self):
        for status in ("pending", "sending", "done", "declined", "cancelled"):
            assert not _chat_text_resendable(
                {"kind": "text", "outgoing": True, "status": status},
            ), status

    def test_incoming_never_resendable_even_if_failed(self):
        assert not _chat_text_resendable(
            {"kind": "text", "outgoing": False, "status": "failed"},
        )

    def test_non_text_kinds_never_resendable(self):
        for kind in ("file", "system"):
            assert not _chat_text_resendable(
                {"kind": kind, "outgoing": True, "status": "failed"},
            ), kind

    def test_missing_kind_defaults_to_text_bubble_semantics(self):
        # The transcript loop renders kind-less entries as text bubbles, so
        # the decision must treat a missing kind the same way.
        assert _chat_text_resendable({"outgoing": True, "status": "failed"}) is True

    def test_garbage_input_is_safe(self):
        assert not _chat_text_resendable({})
        assert not _chat_text_resendable(None)
        assert not _chat_text_resendable("not-a-dict")


# ══════════════════════════════════════════════════════════════════
# 2. Decision agrees with ChatManager.resend_text on real entries
# ══════════════════════════════════════════════════════════════════


def _active_mgr() -> tuple[str, ChatManager]:
    """A ChatManager with one ACTIVE session (incoming invite accepted)."""
    mgr = ChatManager("x", "X")
    mgr.handle_message(
        "chat_invite",
        {"session_id": "f" * 16, "from_name": "A", "fingerprint_short": "A1"},
        PEER,
        "A1",
        None,
    )
    sid = mgr.get_sessions()[0]["session_id"]
    assert mgr.accept_invitation(sid, lambda data: True)
    return sid, mgr


class TestDecisionMatchesBackend:
    def test_failed_done_and_incoming_entries(self):
        sid, mgr = _active_mgr()
        try:
            # Outgoing text over a refusing wire → status failed → resendable.
            assert mgr.send_text(sid, "doomed", lambda data: False) is False
            failed = mgr.get_messages(sid)[-1]
            assert failed["status"] == "failed"
            assert _chat_text_resendable(failed) is True

            # Incoming text: renderer hides the button, backend refuses.
            mgr.handle_message(
                "chat_text",
                {"session_id": sid, "text": "from them", "ts": time.time()},
                PEER,
                "A1",
                None,
            )
            incoming = mgr.get_messages(sid)[-1]
            assert _chat_text_resendable(incoming) is False
            assert mgr.resend_text(sid, incoming["entry_id"], lambda d: True) is False

            # Delivered outgoing text: button gone after resend succeeds.
            assert mgr.send_text(sid, "fine", lambda data: True) is True
            done = mgr.get_messages(sid)[-1]
            assert done["status"] == "done"
            assert _chat_text_resendable(done) is False
            assert mgr.resend_text(sid, done["entry_id"], lambda d: True) is False
        finally:
            mgr.shutdown()

    def test_button_disappears_after_successful_resend(self):
        sid, mgr = _active_mgr()
        try:
            assert mgr.send_text(sid, "retry me", lambda data: False) is False
            entry = mgr.get_messages(sid)[-1]
            assert _chat_text_resendable(entry) is True
            assert mgr.resend_text(sid, entry["entry_id"], lambda d: True) is True
            refreshed = next(e for e in mgr.get_messages(sid) if e["entry_id"] == entry["entry_id"])
            assert refreshed["status"] == "done"
            assert _chat_text_resendable(refreshed) is False
        finally:
            mgr.shutdown()


# ══════════════════════════════════════════════════════════════════
# 3. _chat_do_resend wiring + guards (no Tk root involved)
# ══════════════════════════════════════════════════════════════════


def _bare_dashboard(callback) -> tuple[DashboardWindow, list]:
    """A DashboardWindow shell without running __init__ (no Tk objects).

    Only the attributes ``_chat_do_resend`` touches are provided; the hint
    mechanism is replaced by a recorder so the failure-feedback path can be
    observed without a widget tree.
    """
    dash = object.__new__(DashboardWindow)
    dash._chat_resend_text = callback
    dash._chat_resend_busy_id = None
    hints: list[str] = []
    dash._chat_show_hint = hints.append
    return dash, hints


class TestChatDoResend:
    def test_success_invokes_callback_once_and_clears_busy(self):
        calls = []

        def cb(sid, eid):
            calls.append((sid, eid))
            return True

        dash, hints = _bare_dashboard(cb)
        ok = dash._chat_do_resend("sess1", "entry1")
        assert ok is True
        assert calls == [("sess1", "entry1")]
        assert dash._chat_resend_busy_id is None
        assert hints == []

    def test_failure_returns_false_and_shows_hint(self):
        dash, hints = _bare_dashboard(lambda sid, eid: False)
        ok = dash._chat_do_resend("sess1", "entry1")
        assert ok is False
        assert hints == [T("chat.err_resend_failed")]
        assert dash._chat_resend_busy_id is None

    def test_callback_exception_is_swallowed_and_reported(self):
        def boom(sid, eid):
            raise RuntimeError("wire exploded")

        dash, hints = _bare_dashboard(boom)
        assert dash._chat_do_resend("sess1", "entry1") is False
        assert hints == [T("chat.err_resend_failed")]
        assert dash._chat_resend_busy_id is None

    def test_busy_guard_blocks_reentry(self):
        calls = []
        dash, _ = _bare_dashboard(lambda sid, eid: calls.append((sid, eid)))
        dash._chat_resend_busy_id = "other-entry"
        assert dash._chat_do_resend("sess1", "entry1") is False
        assert calls == [], "a second click during an in-flight resend must not send"

    def test_empty_ids_refused_without_callback_call(self):
        called = []
        dash, _ = _bare_dashboard(lambda sid, eid: called.append(1))
        assert dash._chat_do_resend("", "e") is False
        assert dash._chat_do_resend("s", "") is False
        assert called == []

    def test_missing_callback_is_noop(self):
        dash = object.__new__(DashboardWindow)
        dash._chat_resend_text = None
        dash._chat_resend_busy_id = None
        dash._chat_show_hint = lambda *a, **k: None
        assert dash._chat_do_resend("s", "e") is False


# ══════════════════════════════════════════════════════════════════
# 4. Constructor hook + glue shape
# ══════════════════════════════════════════════════════════════════


class TestWiringShape:
    def test_dashboard_takes_chat_resend_text_kwarg(self):
        params = inspect.signature(DashboardWindow.__init__).parameters
        assert "chat_resend_text" in params
        default = params["chat_resend_text"].default
        assert default is None or callable(default)

    def test_hook_arity_matches_host_glue_shape(self):
        # The dashboard hook receives (session_id, entry_id); main.py owns the
        # send_fn closure, mirroring how chat_send_text wraps manager args.
        params = inspect.signature(ChatManager.resend_text).parameters.values()
        names = [p.name for p in params if p.name != "self"]
        assert names[:2] == ["session_id", "entry_id"]
        assert names[2] == "send_fn"


# ══════════════════════════════════════════════════════════════════
# 5. i18n parity
# ══════════════════════════════════════════════════════════════════


class TestI18nParity:
    def test_new_keys_defined_in_both_locales(self):
        for key in NEW_I18N_KEYS:
            assert key in _EN, f"{key} missing from _EN"
            assert key in _ZH, f"{key} missing from _ZH"
            assert _EN[key].strip(), f"{key} has an empty en string"
            assert _ZH[key].strip(), f"{key} has an empty zh-CN string"
            assert _EN[key] != _ZH[key], f"{key}: zh-CN must not reuse en copy"

    def test_keys_resolve_via_T(self):  # noqa: N802
        for key in NEW_I18N_KEYS:
            value = T(key)
            assert isinstance(value, str) and value.strip()


# ══════════════════════════════════════════════════
# merged from test_chat_i18n.py
# ══════════════════════════════════════════════════

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.sync.nearby_chat import ChatManager
from internal.ui.dashboard import _chat_status_key, human_size


def _source_of(rel_path: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _scan_chat_t_keys() -> set[str]:
    keys: set[str] = set()
    for rel in ("internal/ui/dashboard.py", "src/main.py"):
        src = _source_of(rel)
        for m in re.finditer(r'T\(\s*"(chat\.[A-Za-z0-9_.]+)"', src):
            keys.add(m.group(1))
        for m in re.finditer(r'T\(\s*"(nav\.nearby_chat)"', src):
            keys.add(m.group(1))
    return keys


def test_literal_chat_keys_defined_in_both_locales():
    keys = _scan_chat_t_keys()
    assert keys, "no chat.* / nav.nearby_chat T() keys found in sources"
    for key in sorted(keys):
        assert key in _EN, f"{key} missing from _EN"
        assert key in _ZH, f"{key} missing from _ZH"


def test_chat_key_sets_parity():
    en_chat = {k for k in _EN if k.startswith("chat.")}
    zh_chat = {k for k in _ZH if k.startswith("chat.")}
    assert en_chat == zh_chat, (
        "chat.* key sets differ",
        sorted(en_chat - zh_chat),
        sorted(zh_chat - en_chat),
    )


def test_nav_key_present():
    assert "nav.nearby_chat" in _EN
    assert "nav.nearby_chat" in _ZH


def test_service_system_keys_localized():
    """Every system text_key the ChatManager emits must be translatable."""
    src = _source_of("internal/sync/nearby_chat.py")
    keys = set(re.findall(r'text_key\s*=\s*"([^"]+)"', src))
    assert keys, "no text_key literals found in nearby_chat.py"
    for key in sorted(keys):
        assert key in _EN, f"{key} missing from _EN"
        assert key in _ZH, f"{key} missing from _ZH"


# ── FakeChatManager signature-drift guard ───────────────────────────


class FakeChatManager:
    """Stub matching ChatManager's public API (signatures checked below)."""

    def start_session(self, peer_id, peer_name, fingerprint_short, send_fn):
        return None

    def accept_invitation(self, session_id, send_fn):
        return False

    def decline_invitation(self, session_id, send_fn, reason=""):
        return False

    def close_session(self, session_id, notify_peer=True):
        return False

    def send_text(self, session_id, text, send_fn):
        return False

    def resend_text(self, session_id, entry_id, send_fn):
        return False

    def send_file(self, session_id, file_path, send_fn):
        return None

    def accept_file(self, session_id, transfer_id, send_fn):
        return False

    def decline_file(self, session_id, transfer_id, send_fn):
        return False

    def cancel_file(self, session_id, entry_id):
        return False

    def mark_session_read(self, session_id):
        return None

    def set_own_fingerprint(self, fingerprint):
        return None

    def set_receive_dir(self, path):
        return None

    def shutdown(self):
        return None

    def handle_message(self, msg_type, payload, sender_device_id, sender_fp_short, send_fn):
        return True

    def handle_binary_chunk(self, raw_payload, sender_device_id, send_fn):
        return False

    def mark_peer_disconnected(self, peer_id):
        return None

    def get_sessions(self):
        return []

    def get_messages(self, session_id):
        return []

    def set_on_incoming_invite(self, cb):
        return None

    def set_on_invite_response(self, cb):
        return None

    def set_on_sessions_changed(self, cb):
        return None

    def set_on_message(self, cb):
        return None

    def set_on_file_progress(self, cb):
        return None

    def set_on_file_done(self, cb):
        return None


_FAKE_PUBLIC = {
    name
    for name in dir(FakeChatManager)
    if not name.startswith("_") and callable(getattr(FakeChatManager, name))
}


def _params_of(fn):
    return [p for p in inspect.signature(fn).parameters.values() if p.name != "self"]


def test_fake_matches_real_chat_manager_signatures():
    for name in sorted(_FAKE_PUBLIC):
        real_fn = getattr(ChatManager, name, None)
        assert real_fn is not None, f"FakeChatManager.{name} has no real ChatManager counterpart"
        real_params = _params_of(real_fn)
        fake_params = _params_of(getattr(FakeChatManager, name))
        assert [p.name for p in real_params] == [p.name for p in fake_params], (
            f"{name}: parameter names drifted"
        )
        assert [p.default for p in real_params] == [p.default for p in fake_params], (
            f"{name}: parameter defaults drifted"
        )


def test_main_only_uses_known_chat_api():
    src = _source_of("src/main.py")
    calls = set(re.findall(r"(?:self\.)?chat_mgr\.(\w+)\(", src))
    calls.discard("chat_mgr")
    assert calls <= _FAKE_PUBLIC, sorted(calls - _FAKE_PUBLIC)


# ── Helper unit tests ───────────────────────────────────────────────


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1023) == "1023 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(1536) == "1.5 KB"
    assert human_size(1024 * 1024) == "1.0 MB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"
    assert human_size(1024 * 1024 * 1024) == "1.0 GB"
    assert human_size(2 * 1024 * 1024 * 1024 + 512 * 1024 * 1024) == "2.5 GB"


def test_chat_status_key_mapping():
    assert _chat_status_key("inviting") == "chat.status.inviting"
    assert _chat_status_key("invited") == "chat.status.pending"
    assert _chat_status_key("active", online=True) == "chat.status.connected"
    assert _chat_status_key("active", online=False) == "chat.status.offline"
    assert _chat_status_key("declined_remote") == "chat.status.declined"
    assert _chat_status_key("closed") == "chat.status.closed"


def test_chat_status_keys_localized():
    statuses = ("inviting", "invited", "active", "declined_remote", "closed")
    for status in statuses:
        for online in (True, False):
            key = _chat_status_key(status, online)
            assert key in _EN, f"{key} missing from _EN"
            assert key in _ZH, f"{key} missing from _ZH"


def test_file_status_keys_localized():
    from internal.ui.dashboard import DashboardWindow

    statuses = ("pending", "await_accept", "sending", "done", "failed", "declined", "cancelled")
    for status in statuses:
        suffix = DashboardWindow._chat_file_status_key(status)
        key = f"chat.file.status.{suffix}"
        assert key in _EN, f"{key} missing from _EN"
        assert key in _ZH, f"{key} missing from _ZH"


# ── 8. Picker lists only reachable targets ──────────────────────────────
# The chat tab used to offer "start chat" on every row /api/chat/devices
# returned, paired-but-offline devices included — an invite that could only
# spend 15s on a stale last_ip and end in a timeout toast.  lanTargets keeps
# the rows a frame can actually reach; the internet group keeps the rest of
# the paired internet peers.


def test_lan_targets_filters_unreachable_rows():
    src = _read("components", "chat-panel.js")
    assert "lanTargets: function" in src
    chunk = src.split("lanTargets: function")[1].split("deviceList: function")[0]
    # The three flags the host stamps on each row (src/main._get_chat_devices).
    assert "d.connected || d.discovered || d.relay_reachable" in chunk
    # A host predating the flags sends none of them: keep the row rather than
    # rendering an empty picker.
    assert "d.connected === undefined" in chunk
    assert "d.relay_reachable === undefined" in chunk


def test_both_device_groups_build_on_the_filtered_lan_list():
    src = _read("components", "chat-panel.js")
    dev = src.split("deviceList: function")[1].split("internetDeviceList: function")[0]
    assert "var lan = this.lanTargets;" in dev
    assert "this.chatDevices" not in dev
    # The internet group dedups against the FILTERED list, so an internet-paired
    # peer that went LAN-offline moves into that group instead of vanishing.
    net = src.split("internetDeviceList: function")[1].split("activeSession: function")[0]
    assert "var lan = this.lanTargets;" in net
    assert "this.chatDevices" not in net
