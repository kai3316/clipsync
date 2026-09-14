"""Hotkey behaviour that a user can see go wrong.

Kept from a larger round-3 regression file:

  * two ids may not share one combination (on Windows both would fire), and
    the shipped defaults may never collide with each other
  * a shortcut the parser rejects surfaces through ``failed_shortcuts()``
    instead of silently never firing
  * ``stop()`` keeps the registrations, so the settings toggle off -> on
    restores them on the same manager
  * re-binding an id on Windows releases the previous OS registration rather
    than leaving both combinations live
  * punctuation singles come from the platform VK tables, never ``ord()``
  * the Linux backend's pynput string conversion

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
    HotkeyManager,
)


def test_register_rejects_second_id_on_same_combo():
    """Two ids on one combination would fire BOTH callbacks on Windows."""
    mgr = HotkeyManager()
    assert mgr.register("a", "Ctrl+1", lambda: None) is True
    assert mgr.register("b", "Ctrl+1", lambda: None) is False
    # The first binding is untouched.
    assert "a" in mgr._hotkeys and "b" not in mgr._hotkeys
    assert "b" not in mgr._combo_index


def test_default_shortcuts_are_conflict_free():
    """A future default addition may never shadow an existing default."""
    seen = {}
    mgr = HotkeyManager()
    for hid, shortcut in DEFAULT_SHORTCUTS.items():
        mods, _vk = mgr._parse_shortcut(shortcut)
        combo = (mods, mgr._canonical_key(shortcut))
        assert combo not in seen, f"{hid} ({shortcut}) conflicts with {seen[combo]}"
        seen[combo] = hid


def _noop_factory(hotkey_id):
    return lambda: None


def test_reload_reports_parse_invalid_shortcuts():
    """Invalid strings were previously only logged -- invisible to the user.

    Uses an unknown modifier, which every platform's parser rejects (the
    Linux VK fallback accepts arbitrary named keys, so a bogus key name is
    not a portable failure)."""
    mgr = HotkeyManager()
    mgr.reload_from_config({"ok": "Ctrl+1", "bad": "Ctrl+Hyper+1"}, _noop_factory)
    failed = dict(mgr.failed_shortcuts())
    assert "bad" in failed
    assert failed["bad"] == "Ctrl+Hyper+1"
    assert "ok" not in failed
    assert "ok" in mgr._hotkeys and "bad" not in mgr._hotkeys


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


@pytest.mark.skipif(
    hk_module._platform() != "linux",
    reason="_to_pynput_shortcut only exists on the linux branch",
)
def test_to_pynput_shortcut_conversion():
    assert hk_module._to_pynput_shortcut("Ctrl+Shift+V") == "<ctrl>+<shift>+v"
    assert hk_module._to_pynput_shortcut("Alt+Space") == "<alt>+<space>"
    assert hk_module._to_pynput_shortcut("Ctrl+`") == "<ctrl>+<grave>"


class _FakeKernel32:
    def GetLastError(self):  # noqa: N802
        return 1409  # ERROR_HOTKEY_ALREADY_REGISTERED


class _FakeUser32:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.registered = []  # (hwnd, int_id, mods, vk)
        self.unregistered = []  # (hwnd, int_id)

    def RegisterHotKey(self, hwnd, int_id, mods, vk):  # noqa: N802
        if int_id in self.fail_ids:
            return 0
        self.registered.append((hwnd, int_id, mods, vk))
        return 1

    def UnregisterHotKey(self, hwnd, int_id):  # noqa: N802
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
