"""Round-3 hotkey regression tests.

Pins the lifecycle / conflict-detection behaviour of
``internal/system/hotkey.py``:

  * shortcut parsing (pure logic, platform-independent)
  * in-manager combo conflict detection (two ids, one combination)
  * ``failed_shortcuts()`` as the single source of truth for dead hotkeys
    (parse-invalid, conflicting, factory-raising, OS-refused)
  * ``stop()`` keeps registrations so a later ``start()`` restores them
    (settings toggle off -> on)
  * ``unregister()`` drops bookkeeping before the platform hook runs and is
    a no-op for unknown ids
  * Windows re-binding releases the previous OS registration instead of
    leaving both combinations live (fake user32 -- no real hooks)

No real low-level hooks are registered; system calls are faked so the suite
runs on CI.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.system import hotkey as hk_module
from internal.system.hotkey import (
    DEFAULT_SHORTCUTS,
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    HotkeyManager,
)

# ═════════════════════════════════════════════════════════════════════════
# Shortcut parsing
# ═════════════════════════════════════════════════════════════════════════


def _expected_letter_vk(char):
    """The VK a letter key resolves to on the current platform.

    macOS uses Carbon (kVK_ANSI_*) keycodes — hardware-based and locale-
    independent — while Windows/Linux fall back to ASCII ``ord()``.
    """
    char = char.upper()
    if hk_module._platform() == "macos":
        return hk_module._MAC_LETTER_VK[char]
    return ord(char)


def test_parse_accepts_modifiers_and_alnum_keys():
    mgr = HotkeyManager()
    mods, vk = mgr._parse_shortcut("Ctrl+Shift+V")
    assert mods == (MOD_CONTROL | MOD_SHIFT)
    assert vk == _expected_letter_vk("V")

    mods, vk = mgr._parse_shortcut("alt+F5")
    assert mods == MOD_ALT


def test_parse_normalizes_tilde_to_backtick():
    """``Ctrl+~`` and ``Ctrl+`` `` must resolve to the same physical key."""
    mgr = HotkeyManager()
    _, vk_tilde = mgr._parse_shortcut("Ctrl+~")
    _, vk_grave = mgr._parse_shortcut("Ctrl+`")
    assert vk_tilde == vk_grave


@pytest.mark.parametrize(
    "shortcut",
    [
        1,  # non-string from hand-edited config
        None,
        "Ctrl",  # no key part
        "Hyper+A",  # unknown modifier
    ],
)
def test_parse_rejects_invalid_shortcuts(shortcut):
    """Failures that every platform's parser agrees on."""
    mgr = HotkeyManager()
    if not isinstance(shortcut, str):
        # register() must swallow it, never raise into the caller's loop
        assert mgr.register("x", shortcut, lambda: None) is False
    else:
        with pytest.raises(ValueError):
            mgr._parse_shortcut(shortcut)
        assert mgr.register("x", shortcut, lambda: None) is False


@pytest.mark.parametrize(
    "shortcut",
    [
        "+",  # empty modifier and key
        "Ctrl+",  # empty key
        "Ctrl+Ctrl",  # modifier used as key
    ],
)
def test_parse_rejects_bad_keys_on_vk_platforms(shortcut):
    """Windows/macOS resolve keys through VK tables and reject these; the
    Linux pynput backend intentionally accepts any named key."""
    if hk_module._platform() == "linux":
        pytest.skip("linux accepts arbitrary named keys")
    mgr = HotkeyManager()
    with pytest.raises(ValueError):
        mgr._parse_shortcut(shortcut)
    assert mgr.register("x", shortcut, lambda: None) is False


# ═════════════════════════════════════════════════════════════════════════
# In-manager conflict detection
# ═════════════════════════════════════════════════════════════════════════


def test_register_rejects_second_id_on_same_combo():
    """Two ids on one combination would fire BOTH callbacks on Windows."""
    mgr = HotkeyManager()
    assert mgr.register("a", "Ctrl+1", lambda: None) is True
    assert mgr.register("b", "Ctrl+1", lambda: None) is False
    # The first binding is untouched.
    assert "a" in mgr._hotkeys and "b" not in mgr._hotkeys
    assert "b" not in mgr._combo_index


def test_register_conflict_is_case_and_alias_insensitive():
    mgr = HotkeyManager()
    assert mgr.register("a", "Ctrl+`", lambda: None) is True
    # Same physical key spelled differently + alias modifiers.
    assert mgr.register("b", "Control+~", lambda: None) is False


def test_reregister_same_id_rebinds_without_self_conflict():
    mgr = HotkeyManager()
    assert mgr.register("a", "Ctrl+1", lambda: None) is True
    assert mgr.register("a", "Ctrl+2", lambda: None) is True
    assert mgr._combo_index["a"] == (MOD_CONTROL, "2")


def test_unregister_frees_the_combo():
    mgr = HotkeyManager()
    mgr.register("a", "Ctrl+1", lambda: None)
    mgr.unregister("a")
    assert mgr.register("b", "Ctrl+1", lambda: None) is True


def test_default_shortcuts_are_conflict_free():
    """A future default addition may never shadow an existing default."""
    seen = {}
    mgr = HotkeyManager()
    for hid, shortcut in DEFAULT_SHORTCUTS.items():
        mods, _vk = mgr._parse_shortcut(shortcut)
        combo = (mods, mgr._canonical_key(shortcut))
        assert combo not in seen, (
            f"{hid} ({shortcut}) conflicts with {seen[combo]}"
        )
        seen[combo] = hid


# ═════════════════════════════════════════════════════════════════════════
# failed_shortcuts(): single source of truth for hotkeys that never fire
# ═════════════════════════════════════════════════════════════════════════


def _noop_factory(hotkey_id):
    return lambda: None


def test_reload_reports_parse_invalid_shortcuts():
    """Invalid strings were previously only logged -- invisible to the user.

    Uses an unknown modifier, which every platform's parser rejects (the
    Linux VK fallback accepts arbitrary named keys, so a bogus key name is
    not a portable failure)."""
    mgr = HotkeyManager()
    mgr.reload_from_config(
        {"ok": "Ctrl+1", "bad": "Ctrl+Hyper+1"}, _noop_factory
    )
    failed = dict(mgr.failed_shortcuts())
    assert "bad" in failed
    assert failed["bad"] == "Ctrl+Hyper+1"
    assert "ok" not in failed
    assert "ok" in mgr._hotkeys and "bad" not in mgr._hotkeys


def test_reload_reports_conflicting_shortcuts():
    mgr = HotkeyManager()
    mgr.reload_from_config(
        {"first": "Ctrl+7", "second": "ctrl+7"}, _noop_factory
    )
    failed = dict(mgr.failed_shortcuts())
    assert "second" in failed
    assert "first" not in failed
    # The loser must not be able to fire.
    assert "second" not in mgr._hotkeys


def test_reload_survives_raising_callback_factory():
    """A factory crash used to abort the reload mid-flight, leaving a partial
    registry and -- when running -- a dead listener with running == True."""
    mgr = HotkeyManager()

    def factory(hotkey_id):
        if hotkey_id == "boom":
            raise ValueError(f"no callback for {hotkey_id}")
        return lambda: None

    mgr.reload_from_config({"good": "Ctrl+1", "boom": "Ctrl+2"}, factory)
    failed = dict(mgr.failed_shortcuts())
    assert "boom" in failed
    assert "good" in mgr._hotkeys


def test_failures_clear_between_reloads():
    mgr = HotkeyManager()
    mgr.reload_from_config({"bad": "Ctrl+Hyper+1"}, _noop_factory)
    assert mgr.failed_shortcuts()
    mgr.reload_from_config({"ok": "Ctrl+1"}, _noop_factory)
    assert mgr.failed_shortcuts() == []


def test_stop_keeps_registrations_for_restart():
    """The settings UI toggles hotkeys off then on via stop()/start() on the
    SAME manager -- stop() wiping the registry silently killed every hotkey
    until app restart."""
    mgr = HotkeyManager()
    mgr.register("a", "Ctrl+1", lambda: None)
    mgr.register("b", "Ctrl+2", lambda: None)
    # Simulate a started manager without spawning real OS listeners.
    mgr._running = True
    mgr.stop()
    assert mgr.running is False
    assert set(mgr._hotkeys) == {"a", "b"}
    assert mgr._thread is None


# ═════════════════════════════════════════════════════════════════════════
# unregister() ordering / idempotence
# ═════════════════════════════════════════════════════════════════════════


def test_unregister_drops_bookkeeping_before_platform_hook(monkeypatch):
    """Linux rebuilds its whole listener map inside the platform hook; the id
    must already be gone from _hotkeys or it keeps firing."""
    mgr = HotkeyManager()
    mgr.register("a", "Ctrl+1", lambda: None)

    seen = []
    monkeypatch.setattr(
        mgr,
        "_platform_unregister_one",
        lambda hid: seen.append((hid, hid in mgr._hotkeys)),
        raising=False,
    )
    mgr._running = True
    mgr.unregister("a")
    assert seen == [("a", False)]


def test_unregister_unknown_id_skips_platform_call(monkeypatch):
    mgr = HotkeyManager()
    calls = []
    monkeypatch.setattr(
        mgr, "_platform_unregister_one", calls.append, raising=False
    )
    mgr._running = True
    mgr.unregister("never_registered")
    assert calls == []


def test_fire_swallows_callback_exceptions():
    """A misbehaving callback must not take the listener down."""
    mgr = HotkeyManager()

    def bad():
        raise RuntimeError("boom")

    mgr.register("a", "Ctrl+1", bad)
    mgr._fire("a")  # must not raise
    mgr._fire("missing")  # unknown id: silent no-op


# ═════════════════════════════════════════════════════════════════════════
# Linux pynput conversion (pure string logic)
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    hk_module._platform() != "linux",
    reason="_to_pynput_shortcut only exists on the linux branch",
)
def test_to_pynput_shortcut_conversion():
    assert (
        hk_module._to_pynput_shortcut("Ctrl+Shift+V") == "<ctrl>+<shift>+v"
    )
    assert hk_module._to_pynput_shortcut("Alt+Space") == "<alt>+<space>"
    assert hk_module._to_pynput_shortcut("Ctrl+`") == "<ctrl>+<grave>"


# ═════════════════════════════════════════════════════════════════════════
# Windows-specific bookkeeping (fake user32 -- never touches real hooks)
# ═════════════════════════════════════════════════════════════════════════


class _FakeKernel32:
    def GetLastError(self):
        return 1409  # ERROR_HOTKEY_ALREADY_REGISTERED


class _FakeUser32:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.registered = []  # (hwnd, int_id, mods, vk)
        self.unregistered = []  # (hwnd, int_id)

    def RegisterHotKey(self, hwnd, int_id, mods, vk):
        if int_id in self.fail_ids:
            return 0
        self.registered.append((hwnd, int_id, mods, vk))
        return 1

    def UnregisterHotKey(self, hwnd, int_id):
        self.unregistered.append((hwnd, int_id))
        return 1


@pytest.fixture
def win_mgr(monkeypatch):
    if hk_module._platform() != "windows":
        pytest.skip("windows-only bookkeeping")
    fake_u32 = _FakeUser32()
    monkeypatch.setattr(hk_module, "_user32", fake_u32)
    monkeypatch.setattr(hk_module, "_kernel32", _FakeKernel32())
    mgr = HotkeyManager()
    mgr._win_hwnd = 12345  # fake message-only window
    yield mgr, fake_u32


def test_win_rebind_releases_previous_os_registration(win_mgr):
    """Re-binding an id used to leak the old RegisterHotKey AND leave the old
    combination firing alongside the new one."""
    mgr, u32 = win_mgr
    mgr._win_register_one("a", MOD_CONTROL, ord("1"))
    old_int_id = mgr._win_id_map["a"]
    assert u32.registered == [(12345, old_int_id, 0x0002, ord("1"))]

    mgr._win_register_one("a", MOD_ALT, ord("9"))
    new_int_id = mgr._win_id_map["a"]
    assert new_int_id != old_int_id
    # Old registration released, stale reverse mapping gone.
    assert (12345, old_int_id) in u32.unregistered
    assert old_int_id not in mgr._win_id_rev
    assert mgr._win_id_rev[new_int_id] == "a"


def test_win_register_failure_recorded_and_mapping_dropped(win_mgr):
    """An OS-refused combo surfaces through failed_shortcuts() instead of
    silently never firing."""
    mgr, u32 = win_mgr
    u32.fail_ids.add(1)
    mgr._shortcut_strings["claimed"] = "Ctrl+7"
    mgr._win_register_one("claimed", MOD_CONTROL, ord("7"))
    assert "claimed" not in mgr._win_id_map
    assert dict(mgr.failed_shortcuts())["claimed"] == "Ctrl+7"


def test_win_teardown_clears_all_bookkeeping(win_mgr):
    mgr, u32 = win_mgr
    mgr._win_class_name = "ClipSyncHotkey_test"
    for i in range(3):
        mgr._win_register_one(f"h{i}", MOD_CONTROL, ord(str(i + 1)))
    assert len(mgr._win_id_map) == 3

    mgr._win_teardown(12345)
    assert mgr._win_id_map == {} and mgr._win_id_rev == {}
    assert mgr._win_hwnd is None and mgr._win_class_name is None
    assert len(u32.unregistered) == 3


# ═════════════════════════════════════════════════════════════════════════
# VK table coverage (punctuation keys resolve per-platform, never wrong)
# ═════════════════════════════════════════════════════════════════════════


def test_punctuation_keys_resolve_via_tables_not_ord():
    """ord(',') == 44 which is NOT VK_OEM_COMMA (0xBC); punctuation singles
    must come from the platform table (or a Linux placeholder), never ord()."""
    mgr = HotkeyManager()
    expected = {"windows": 0xBC, "macos": 43}
    mods, vk = mgr._parse_shortcut("Ctrl+,")
    assert mods == MOD_CONTROL
    if mgr._platform in expected:
        assert vk == expected[mgr._platform]
    else:  # linux: placeholder hash, deliberately not an ord() value of ','
        assert vk != ord(",")


def test_letters_still_use_ord():
    mgr = HotkeyManager()
    _mods, vk = mgr._parse_shortcut("Ctrl+a")
    assert vk == _expected_letter_vk("A")
