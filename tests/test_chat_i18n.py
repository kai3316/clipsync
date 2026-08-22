"""i18n + helper unit tests for the Nearby Chat desktop UI.

Covers:
* every ``T("chat.*")`` / ``T("nav.nearby_chat")`` literal used by
  dashboard.py and main.py is defined in BOTH locales,
* the ``chat.*`` key sets are identical across locales,
* the system ``text_key`` strings emitted by the ChatManager service are all
  translated,
* the FakeChatManager used by the UI wiring matches the real ChatManager's
  public method signatures (fails fast if the service API drifts),
* the module-level formatting / status helpers (``human_size``,
  ``_chat_status_key``) behave as the chat panel expects.
"""

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.i18n import _EN, _ZH
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

    def handle_message(self, msg_type, payload, sender_device_id,
                       sender_fp_short, send_fn):
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
    name for name in dir(FakeChatManager)
    if not name.startswith("_") and callable(getattr(FakeChatManager, name))
}


def _params_of(fn):
    return [p for p in inspect.signature(fn).parameters.values()
            if p.name != "self"]


def test_fake_matches_real_chat_manager_signatures():
    for name in sorted(_FAKE_PUBLIC):
        real_fn = getattr(ChatManager, name, None)
        assert real_fn is not None, (
            f"FakeChatManager.{name} has no real ChatManager counterpart"
        )
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
    statuses = ("pending", "await_accept", "sending", "done",
                "failed", "declined", "cancelled")
    for status in statuses:
        suffix = DashboardWindow._chat_file_status_key(status)
        key = f"chat.file.status.{suffix}"
        assert key in _EN, f"{key} missing from _EN"
        assert key in _ZH, f"{key} missing from _ZH"
