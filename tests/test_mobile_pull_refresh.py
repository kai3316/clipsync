"""Pull to refresh on the two phone lists that grow from somewhere else.

Both lists are read-only mirrors: history is fed by the desktop and favourites by
both sides, while every other panel holds what the reader just did.  So the
gesture is limited to those two, its trigger is deliberately long (a short one
turns "scroll back to the top" into a refresh), and the chip reports progress
until the read actually lands rather than for a fixed moment.

Text-level again: the page is vanilla JS inside one HTML file.  What is checkable
here is the guard, the two thresholds, and the three things the chip says.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "internal" / "web" / "static" / "mobile.html"


def source() -> str:
    return PAGE.read_text(encoding="utf-8")


def block(pattern: str) -> str:
    found = re.search(pattern, source(), re.S)
    assert found, "missing: " + pattern
    return found.group(0)


def test_only_the_two_mirrored_lists_arm_the_gesture():
    start = block(r"contentEl\.addEventListener\('touchstart', function \(event\) \{.*?\}, \{ passive: true \}\);")
    assert "name !== 'history' && name !== 'favorites'" in start
    # At the top of the list only: anywhere else the finger belongs to the list.
    assert "contentEl.scrollTop > 0" in start
    assert "event.touches.length !== 1" in start


def test_the_trigger_is_long_enough_not_to_be_a_scroll():
    text = source()
    assert "var PULL_TRIGGER = 72;" in text
    move = block(r"contentEl\.addEventListener\('touchmove', function \(event\) \{.*?\}, \{ passive: false \}\);")
    # Damped travel, so the chip moves less than the finger and the gesture
    # reads as a held tension rather than a drag.
    assert "PULL_DAMPING" in move
    assert "Math.min(PULL_MAX, dy * PULL_DAMPING)" in move
    # `preventDefault` only works on a non-passive listener; on a passive one the
    # browser scrolls the list under the chip and the gesture fights itself.
    assert "if (event.cancelable) event.preventDefault();" in move
    # Going the other way is a scroll, and it gives the list back immediately.
    assert "if (dy <= 0) {" in move
    assert "pullReady = dy >= PULL_TRIGGER;" in move


def test_the_chip_says_what_the_finger_is_doing():
    dom = block(r'<div class="pull" id="pull".*?</div>')
    assert 'id="pullPill"' in dom
    # It reports a gesture that only exists as a gesture.
    assert 'aria-hidden="true"' in dom
    text = source()
    for key in ("pullDown", "pullRelease", "pullBusy"):
        assert re.search(r"\b%s: ZH \?" % key, text), key
    # Pull, then release, then work — the three states in that order.
    assert "pullLabel(pullReady ? T.pullRelease : T.pullDown);" in text
    assert "pullLabel(T.pullBusy);" in text


def test_refreshing_the_list_that_is_actually_showing():
    refresh = block(r"function pullRefresh\(\) \{.*?\n  \}")
    assert "loadFavorites(true)" in refresh
    assert "loadHistory(true)" in refresh
    # Silent: the rows stay, because the chip above them is what says the list is
    # being re-read.  A skeleton under a "refreshing" chip is two answers.
    assert "loadHistory()" not in refresh


def test_the_chip_stops_when_the_read_lands_not_on_a_timer():
    wait = block(r"function pullWhenSettled\(done\) \{.*?\n  \}")
    assert "favoritesLoading" in wait
    assert "historyLoading" in wait
    # With a ceiling, so a request that never answers cannot leave a chip on the
    # screen for good.
    assert "waited >= 4000" in wait


def test_the_drag_does_not_animate_but_the_return_does():
    """A transition on `transform` while the finger is down makes the chip lag."""
    paint = block(r"function pullPaint\(offset, animate\) \{.*?\n  \}")
    assert "animate ?" in paint
    assert "'none'" in paint
    assert "pullPaint(0, true);" in source()
    assert "pullPaint(PULL_RESTING, true);" in source()
