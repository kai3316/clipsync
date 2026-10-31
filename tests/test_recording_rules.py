"""Clipboard recording rules (1.3.0) and the config fields behind them.

Four rules act on the LOCAL capture path only: a password manager's "do not
record" marker, the content that was already on the clipboard before launch, a
cap on the size of a single non-file capture, and source-application substrings
(plus the paste-behaviour switch that shares their config block).  Pinned here:
the documented defaults and the loader/settings validation, the one-way v3→v4
migration, the substring matcher, and the drop decisions themselves — against
fake monitor/reader doubles, so nothing in this file touches a real clipboard,
a real config file or the network.
"""

from __future__ import annotations

import json
import sys

import pytest

import internal.config.config as config_module
from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.clipboard.source_tracker import is_app_allowed
from internal.config.config import (
    _SKIP_FIELD,
    DEFAULT_HOTKEYS,
    FIELD_RULES,
    Config,
    _validate_field,
    default_hotkeys,
)
from internal.sync.manager import SyncManager
from internal.web.api import settings as settings_api

DEFAULTS = {
    "auto_paste_enabled": True,
    "skip_password_manager_clips": True,
    "skip_prelaunch_clipboard": True,
    "max_non_file_paste_mb": 8.0,
    "source_exclusion_patterns": [],
}


# ── helpers ────────────────────────────────────────────────────────────


class _Monitor:
    """A platform monitor double: it reports no poll gap and holds the two
    per-event facts the real monitors capture (source app, concealed marker)."""

    max_poll_interval = 0.0

    def __init__(self):
        self.callback = None
        self.last_source_app = None
        self.last_concealed = False

    def start(self, callback):
        self.callback = callback

    def stop(self):
        self.callback = None

    def suppress_for(self, seconds):
        pass


class _Reader:
    def __init__(self, content):
        self.content = content

    def read(self):
        return self.content


class _Writer:
    def write(self, content):
        return True


class _History:
    def __init__(self):
        self.rows = []

    def add(self, content, source_app=None):
        self.rows.append((content, source_app))
        return f"row-{len(self.rows)}"


def _text(value: str) -> ClipboardContent:
    return ClipboardContent(types={ContentType.TEXT: value.encode("utf-8")})


def _capture_manager(
    cfg,
    content,
    *,
    concealed=False,
    source_app=None,
    retry_enabled=False,
):
    """A SyncManager wired to doubles, with a live config and a send recorder."""
    monitor = _Monitor()
    monitor.last_concealed = concealed
    monitor.last_source_app = source_app
    reader = _Reader(content)
    history = _History()
    sent = []
    manager = SyncManager(
        "device",
        "Device",
        reader=reader,
        writer=_Writer(),
        monitor=monitor,
        history=history,
        sync_debounce=0.0,
        retry_enabled=retry_enabled,
    )
    manager.set_config(cfg)
    manager.on_send = sent.append
    return manager, reader, monitor, history, sent


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point the config module at a throwaway config.json."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    monkeypatch.setattr(config_module, "_config_dir", lambda: config_dir)
    monkeypatch.setattr(config_module, "_config_path", lambda: config_path)
    return config_path


@pytest.fixture
def settings_cfg(monkeypatch):
    """A real Config whose saves are no-ops (nothing leaves the test process)."""
    monkeypatch.setattr(
        settings_api, "_persist_preserving_at_rest_private_key", lambda cfg: None
    )
    return Config()


# ── 1. defaults and validation ────────────────────────────────────────


def test_the_five_fields_default_to_what_the_contract_says():
    cfg = Config()
    assert cfg.auto_paste_enabled is True
    assert cfg.skip_password_manager_clips is True
    assert cfg.skip_prelaunch_clipboard is True
    assert cfg.max_non_file_paste_mb == 8.0
    assert cfg.source_exclusion_patterns == []
    assert cfg.config_version == 6


def test_the_schema_table_describes_the_new_fields():
    assert FIELD_RULES["auto_paste_enabled"] == ("bool",)
    assert FIELD_RULES["skip_password_manager_clips"] == ("bool",)
    assert FIELD_RULES["skip_prelaunch_clipboard"] == ("bool",)
    assert FIELD_RULES["max_non_file_paste_mb"] == ("float", 0, 1024)
    assert FIELD_RULES["source_exclusion_patterns"] == ("strlist_nonnull",)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("auto_paste_enabled", "true"),
        ("skip_password_manager_clips", 1),
        ("skip_prelaunch_clipboard", None),
        ("max_non_file_paste_mb", "8"),
        ("max_non_file_paste_mb", float("nan")),
        ("source_exclusion_patterns", None),
        ("source_exclusion_patterns", [1]),
        ("source_exclusion_patterns", "keepass"),
    ],
)
def test_the_loader_refuses_a_value_of_the_wrong_type(key, value):
    """A hand-edited config keeps the default instead of the bad value."""
    assert _validate_field(key, value) is _SKIP_FIELD


def test_the_loader_clamps_the_size_cap_into_range():
    assert _validate_field("max_non_file_paste_mb", 4) == 4.0
    assert isinstance(_validate_field("max_non_file_paste_mb", 4), float)
    assert _validate_field("max_non_file_paste_mb", -3) == 0
    assert _validate_field("max_non_file_paste_mb", 5000) == 1024


# ── 2. hotkeys and the v3 → v4 migration ──────────────────────────────


def test_the_default_hotkeys_follow_the_platform_and_leave_the_paste_slots_free():
    mac = default_hotkeys("Darwin")
    pc = default_hotkeys("Windows")
    # The same actions on every platform, with the modifier each one calls its
    # own: Command on macOS, Ctrl on Windows/Linux.
    assert mac["show_quick_paste"] == "Cmd+Shift+V"
    assert pc["show_quick_paste"] == "Ctrl+Shift+V"
    assert mac["paste_plain"] == "Cmd+Alt+V"
    assert pc["paste_plain"] == "Ctrl+Alt+V"
    assert mac["show_window"] == "Cmd+Shift+Space"
    assert pc["show_window"] == "Ctrl+Shift+Space"
    # Ctrl+1…9 / Cmd+1…9 switch tabs everywhere else; a default here would take
    # those chords from every other application for the sake of a shortcut the
    # paste panel already covers.
    for index in range(1, 10):
        assert mac[f"paste_{index}"] == ""
        assert pc[f"paste_{index}"] == ""
        assert DEFAULT_HOTKEYS[f"paste_{index}"] == ""
    # Deliberately unbound: which clip is the newest remote one is not known in
    # advance.
    assert DEFAULT_HOTKEYS["paste_remote_last"] == ""
    assert Config().hotkeys == DEFAULT_HOTKEYS
    # On by default, with only the safe chords above registered.
    assert Config().hotkeys_enabled is True


def test_a_v4_config_is_rewritten_to_the_platform_modifier(isolated_config, monkeypatch):
    # The migration asks the platform for its own defaults at load time, so a
    # Mac upgrading from the Ctrl-based v4 defaults gets Command:
    # Control+Shift+V would be a chord Mac users do not press.
    monkeypatch.setattr(config_module.platform, "system", lambda: "Darwin")
    isolated_config.write_text(
        json.dumps(
            {
                "config_version": 4,
                "hotkeys": {
                    "paste_1": "Ctrl+1",
                    "paste_plain": "Ctrl+Alt+V",
                    "show_quick_paste": "Ctrl+Shift+V",
                    "show_window": "Ctrl+Shift+Space",
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = config_module.load()

    assert cfg.hotkeys["paste_plain"] == "Cmd+Alt+V"
    assert cfg.hotkeys["show_quick_paste"] == "Cmd+Shift+V"
    assert cfg.hotkeys["show_window"] == "Cmd+Shift+Space"
    assert cfg.hotkeys["paste_1"] == ""
    assert cfg.config_version == 6


def test_a_v5_config_turns_on_launch_at_login(isolated_config):
    isolated_config.write_text(
        json.dumps({"config_version": 5, "auto_start": False}),
        encoding="utf-8",
    )

    cfg = config_module.load()

    assert cfg.auto_start is True
    assert cfg.config_version == 6
    # A save keeps the migrated version, so the flip happens once.
    config_module.save(cfg)
    assert json.loads(isolated_config.read_text(encoding="utf-8"))["config_version"] == 6


def test_a_v3_config_is_migrated_to_v5_and_saved_that_way(isolated_config):
    config_path = isolated_config
    config_path.write_text(
        json.dumps(
            {
                "config_version": 3,
                "hotkeys": {
                    "paste_1": "Ctrl+1",
                    "paste_plain": "Ctrl+Shift+V",
                    "toggle_monitor": "Ctrl+Shift+M",
                    "show_window": "Ctrl+Shift+Space",
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = config_module.load()

    assert cfg.config_version == 6
    # v4 moved plain-text paste to the new default...
    assert cfg.hotkeys["paste_plain"] == DEFAULT_HOTKEYS["paste_plain"]
    # ...and added the action that did not exist before.
    assert cfg.hotkeys["show_quick_paste"] == DEFAULT_HOTKEYS["show_quick_paste"]
    # v5 moved the untouched defaults to the platform's modifier and cleared
    # the paste slots (Ctrl+1 was the old default; this user never changed it).
    assert cfg.hotkeys["paste_1"] == ""
    assert cfg.hotkeys["toggle_monitor"] == DEFAULT_HOTKEYS["toggle_monitor"]
    assert cfg.hotkeys_enabled is True

    # The migration is one-way: the save below writes version 5, so a user who
    # binds paste_plain back to an old combination keeps it on the next start.
    old_plain = "Ctrl+Shift+V"
    cfg.hotkeys["paste_plain"] = old_plain
    config_module.save(cfg)

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["config_version"] == 6
    assert raw["hotkeys"]["paste_plain"] == old_plain
    assert config_module.load().hotkeys["paste_plain"] == old_plain


def test_a_v4_config_keeps_a_custom_paste_slot_but_loses_the_default_ones(isolated_config):
    isolated_config.write_text(
        json.dumps(
            {
                "config_version": 4,
                "hotkeys_enabled": False,
                "hotkeys": {
                    "paste_1": "Ctrl+Alt+1",
                    "paste_2": "Ctrl+2",
                    "show_window": "Ctrl+Shift+Space",
                    "paste_remote_last": "",
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = config_module.load()

    # A chord the user chose is a choice; a chord still spelled like the v4
    # default was never touched.
    assert cfg.hotkeys["paste_1"] == "Ctrl+Alt+1"
    assert cfg.hotkeys["paste_2"] == ""
    assert cfg.hotkeys["show_window"] == DEFAULT_HOTKEYS["show_window"]
    assert cfg.hotkeys["paste_remote_last"] == ""
    # The one-time v5 flip: hotkeys exist for new users without a trip to
    # settings; a user who turns them off again stays off.
    assert cfg.hotkeys_enabled is True
    assert cfg.config_version == 6


def test_a_custom_paste_plain_binding_is_not_rewritten(isolated_config):
    isolated_config.write_text(
        json.dumps(
            {
                "config_version": 3,
                "hotkeys": {"paste_plain": "Ctrl+Alt+P"},
            }
        ),
        encoding="utf-8",
    )

    cfg = config_module.load()

    assert cfg.hotkeys["paste_plain"] == "Ctrl+Alt+P"
    assert cfg.hotkeys["show_quick_paste"] == DEFAULT_HOTKEYS["show_quick_paste"]
    assert cfg.config_version == 6


def test_the_new_fields_round_trip_through_save_and_load(isolated_config):
    config_path = isolated_config
    cfg = Config()
    cfg.auto_paste_enabled = False
    cfg.skip_password_manager_clips = False
    cfg.skip_prelaunch_clipboard = False
    cfg.max_non_file_paste_mb = 2.5
    cfg.source_exclusion_patterns = ["keepass", "1password"]
    config_module.save(cfg)

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["config_version"] == 6
    assert raw["auto_paste_enabled"] is False
    assert raw["source_exclusion_patterns"] == ["keepass", "1password"]

    loaded = config_module.load()
    assert loaded.auto_paste_enabled is False
    assert loaded.skip_password_manager_clips is False
    assert loaded.skip_prelaunch_clipboard is False
    assert loaded.max_non_file_paste_mb == 2.5
    assert loaded.source_exclusion_patterns == ["keepass", "1password"]


# ── 3. source-exclusion substrings ────────────────────────────────────


def test_source_substrings_exclude_case_insensitively():
    cfg = Config()
    cfg.source_exclusion_patterns = ["1password", "KeePass"]

    assert not is_app_allowed(
        {"process": "1Password.exe", "name": "1Password", "title": "vault"}, cfg
    )
    assert not is_app_allowed(
        {"process": "keepassxc", "name": "KeePassXC", "title": ""}, cfg
    )
    assert is_app_allowed(
        {"process": "chrome.exe", "name": "Google Chrome", "title": "docs"}, cfg
    )


def test_source_substrings_match_any_label_not_only_the_process():
    """A password manager's window title is often the only place its name shows."""
    cfg = Config()
    cfg.source_exclusion_patterns = ["vault"]
    assert not is_app_allowed(
        {"process": "helper.exe", "name": "Helper", "title": "Vault — login"}, cfg
    )
    assert is_app_allowed(
        {"process": "helper.exe", "name": "Helper", "title": "notes"}, cfg
    )


def test_source_substrings_are_independent_of_the_app_filter():
    cfg = Config()
    cfg.app_filter_enabled = True
    cfg.app_filter_mode = "whitelist"
    cfg.app_filter_list = ["chrome.exe"]
    cfg.source_exclusion_patterns = ["chrome"]

    # The whitelist would admit Chrome; the exclusion still refuses it.
    assert not is_app_allowed({"process": "chrome.exe", "name": "", "title": ""}, cfg)
    assert not is_app_allowed({"process": "keepass.exe", "name": "", "title": ""}, cfg)

    # ...and the exclusion applies with the app filter switched off.
    cfg.app_filter_enabled = False
    assert not is_app_allowed({"process": "chrome.exe", "name": "", "title": ""}, cfg)


def test_unknown_source_app_and_no_patterns_are_allowed():
    cfg = Config()
    assert is_app_allowed(None, cfg)
    cfg.source_exclusion_patterns = ["secret"]
    # Nothing to match against: an unknown app is not excluded on a guess.
    assert is_app_allowed(None, cfg)


# ── 4. the capture-path drops ─────────────────────────────────────────


def test_a_concealed_copy_is_dropped_before_history_and_network():
    cfg = Config()
    manager, _reader, _monitor, history, sent = _capture_manager(
        cfg, _text("correct horse battery staple"), concealed=True
    )
    manager._last_local_hash = "stale"

    manager._do_read_and_send()

    assert history.rows == []
    assert sent == []
    # The drop clears stale loop-prevention state, so a later copy of the same
    # content is not suppressed against it.
    assert manager._last_local_hash is None


def test_the_concealed_switch_turns_the_rule_off():
    cfg = Config()
    cfg.skip_password_manager_clips = False
    manager, _reader, _monitor, history, sent = _capture_manager(
        cfg, _text("not a secret any more"), concealed=True
    )

    manager._do_read_and_send()

    assert len(history.rows) == 1
    assert len(sent) == 1


def test_prelaunch_content_is_skipped_exactly_once():
    cfg = Config()
    content = _text("already on the clipboard before launch")
    manager, _reader, _monitor, history, sent = _capture_manager(cfg, content)

    manager.start()
    assert manager._prelaunch_hash == content.hash_key()

    manager._do_read_and_send()
    assert history.rows == []
    assert sent == []
    assert manager._prelaunch_hash is None

    # One-shot: a second capture of the same content is a real copy now.
    manager._do_read_and_send()
    assert len(history.rows) == 1
    assert len(sent) == 1


def test_the_prelaunch_baseline_is_consumed_by_the_first_capture():
    cfg = Config()
    manager, reader, _monitor, history, _sent = _capture_manager(cfg, _text("old"))

    manager.start()
    reader.content = _text("new")
    manager._do_read_and_send()
    assert len(history.rows) == 1

    # The launch baseline was consumed by the capture above, so copying the old
    # content again now is a fresh copy and is recorded.
    reader.content = _text("old")
    manager._do_read_and_send()
    assert len(history.rows) == 2


def test_the_prelaunch_rule_can_be_switched_off():
    cfg = Config()
    cfg.skip_prelaunch_clipboard = False
    manager, _reader, _monitor, history, _sent = _capture_manager(cfg, _text("x"))

    manager.start()
    assert manager._prelaunch_hash is None

    manager._do_read_and_send()
    assert len(history.rows) == 1


def test_a_capture_over_the_size_cap_is_dropped():
    cfg = Config()
    cfg.max_non_file_paste_mb = 0.001  # ~1 KiB, so the fixture below is over it
    manager, _reader, _monitor, history, sent = _capture_manager(
        cfg, _text("x" * 4096)
    )

    manager._do_read_and_send()

    assert history.rows == []
    assert sent == []


def test_the_size_cap_counts_non_file_formats_only():
    """A file list is a list of paths, not the file: it must not trip the cap."""
    cfg = Config()
    cfg.max_non_file_paste_mb = 0.001
    file_list = ClipboardContent(
        types={ContentType.FILE: b"/some/long/path.pdf" * 500}
    )
    manager, _reader, _monitor, history, sent = _capture_manager(cfg, file_list)

    manager._do_read_and_send()

    assert len(history.rows) == 1
    assert len(sent) == 1


def test_zero_means_the_size_cap_is_off():
    cfg = Config()
    cfg.max_non_file_paste_mb = 0
    manager, _reader, _monitor, history, sent = _capture_manager(
        cfg, _text("x" * 4096)
    )

    manager._do_read_and_send()

    assert len(history.rows) == 1
    assert len(sent) == 1


def test_recording_rules_do_not_touch_the_remote_receive_path():
    """The rules are about what this machine *captured*, not what it received."""
    cfg = Config()
    cfg.max_non_file_paste_mb = 0.001
    manager, _reader, monitor, history, _sent = _capture_manager(
        cfg, _text("local"), concealed=True
    )
    message = SyncMessage(
        content=_text("x" * 4096), msg_id="m1", source_device="peer"
    )

    outcome = manager.handle_remote_message_ex(message)

    assert outcome["accepted"] is True
    assert outcome["clipboard"] is True
    assert len(history.rows) == 1
    # Reading the remote path does not consume or set the launch baseline.
    assert monitor.last_concealed is True


# ── 4b. the platform monitors' "do not record" detection ─────────────


def test_the_monitor_interface_defaults_to_not_concealed():
    """A platform that cannot tell must not be reported as concealed."""
    from internal.clipboard.clipboard import ClipboardMonitor

    assert ClipboardMonitor.last_concealed is False


def test_macos_marker_types_are_recognised(monkeypatch):
    from internal.clipboard import clipboard_darwin as darwin

    monkeypatch.setattr(
        darwin,
        "_pb_types",
        lambda: {b"public.utf8-plain-text", b"org.nspasteboard.ConcealedType"},
    )
    assert darwin._pb_concealed() is True

    monkeypatch.setattr(darwin, "_pb_types", lambda: {b"public.utf8-plain-text"})
    assert darwin._pb_concealed() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows clipboard API")
def test_windows_marker_formats_are_recognised(monkeypatch):
    from internal.clipboard import clipboard_windows as win

    monkeypatch.setattr(win.user32, "OpenClipboard", lambda _owner: 1)
    monkeypatch.setattr(win.user32, "CloseClipboard", lambda: 1)
    monkeypatch.setattr(win.user32, "EnumClipboardFormats", lambda last: 0)
    assert win._clipboard_concealed() is False

    # Either presence-only marker means "do not record".
    for marker in (win.CF_EXCLUDE_FROM_MONITOR, win.CF_CLIPBOARD_VIEWER_IGNORE):
        formats = iter([marker, 0])
        monkeypatch.setattr(win.user32, "EnumClipboardFormats", lambda last: next(formats))
        assert win._clipboard_concealed() is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows clipboard API")
def test_windows_can_include_marker_only_conceals_when_zero(monkeypatch):
    from internal.clipboard import clipboard_windows as win

    monkeypatch.setattr(win.user32, "OpenClipboard", lambda _owner: 1)
    monkeypatch.setattr(win.user32, "CloseClipboard", lambda: 1)
    formats = iter([win.CF_CAN_INCLUDE_IN_HISTORY, 0])
    monkeypatch.setattr(win.user32, "EnumClipboardFormats", lambda last: next(formats))

    monkeypatch.setattr(win, "_clipboard_dword", lambda fmt: 0)
    assert win._clipboard_concealed() is True

    formats = iter([win.CF_CAN_INCLUDE_IN_HISTORY, 0])
    monkeypatch.setattr(win.user32, "EnumClipboardFormats", lambda last: next(formats))
    monkeypatch.setattr(win, "_clipboard_dword", lambda fmt: 1)
    assert win._clipboard_concealed() is False


def test_linux_password_manager_hint_is_read_from_the_target_list(monkeypatch):
    from internal.clipboard import clipboard_linux as linux

    monkeypatch.setattr(
        linux,
        "_clipboard_targets",
        lambda: {b"TARGETS", b"UTF8_STRING", b"x-kde-passwordManagerHint"},
    )
    assert linux._password_manager_hint() is True

    monkeypatch.setattr(linux, "_clipboard_targets", lambda: {b"TARGETS", b"UTF8_STRING"})
    assert linux._password_manager_hint() is False


# ── 5. the settings API surface ───────────────────────────────────────


def test_settings_api_reads_and_writes_the_recorded_rules(settings_cfg):
    cfg = settings_cfg

    data, status = settings_api.get_settings(cfg)
    assert status == 200
    for key, default in DEFAULTS.items():
        assert data["settings"][key] == default

    payload = json.dumps(
        {
            "auto_paste_enabled": False,
            "skip_password_manager_clips": False,
            "skip_prelaunch_clipboard": False,
            "max_non_file_paste_mb": 2,
            "source_exclusion_patterns": ["keepass", "1password"],
        }
    ).encode("utf-8")
    result, status = settings_api.update_settings(payload, cfg, None)

    assert status == 200
    assert result["ok"] is True
    assert set(result["updated"]) == set(DEFAULTS)
    assert cfg.auto_paste_enabled is False
    assert cfg.skip_password_manager_clips is False
    assert cfg.skip_prelaunch_clipboard is False
    assert cfg.max_non_file_paste_mb == 2
    assert cfg.source_exclusion_patterns == ["keepass", "1password"]

    data, _status = settings_api.get_settings(cfg)
    assert data["settings"]["max_non_file_paste_mb"] == 2
    assert data["settings"]["source_exclusion_patterns"] == ["keepass", "1password"]


@pytest.mark.parametrize(
    "payload",
    [
        {"max_non_file_paste_mb": 1024.5},  # over the documented ceiling
        {"max_non_file_paste_mb": -1},
        {"max_non_file_paste_mb": "8"},  # wrong type
        {"auto_paste_enabled": "false"},
        {"source_exclusion_patterns": None},
        {"source_exclusion_patterns": "keepass"},
    ],
)
def test_settings_api_rejects_an_invalid_recording_rule(settings_cfg, payload):
    cfg = settings_cfg
    before = dict(DEFAULTS)

    result, status = settings_api.update_settings(
        json.dumps(payload).encode("utf-8"), cfg, None
    )

    assert status == 400
    assert result["ok"] is False
    for key, default in before.items():
        assert getattr(cfg, key) == default
