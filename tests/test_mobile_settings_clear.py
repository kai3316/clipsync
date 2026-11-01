"""Clear-all history lives behind the phone's Settings tab, not above the list.

The history tab is what a phone user opens to read and copy clips; a destructive
control beside that list is one mis-tap away from wiping it.  Settings is the
deliberate place for it, and the action still asks for confirmation.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "internal" / "web" / "static" / "mobile.html"


def source() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_history_page_no_longer_carries_the_clear_button():
    text = source()
    assert 'id="histClearBtn"' not in text
    assert 'id="histBar"' not in text


def test_settings_renders_the_clear_button_and_binds_it():
    text = source()
    assert 'id="settingsHistClearBtn"' in text
    assert "T.setDangerTitle" in text
    assert "T.setDangerHint" in text
    assert "clearBtn.addEventListener('click', clearAllHistory)" in text


def test_clear_still_confirms_and_does_not_require_a_loaded_list():
    """Settings can be opened before History, so the guard cannot be
    `historyItems.length`: it would make the button silently do nothing."""
    text = source()
    handler = re.search(r"function clearAllHistory\(\) \{.*?\n  \}", text, re.S)
    assert handler, "the clear handler is gone"
    block = handler.group(0)
    assert "window.confirm(T.histClearConfirm)" in block
    assert "historyItems.length === 0" not in block
    assert "apiFetch('/api/history/clear'" in block
