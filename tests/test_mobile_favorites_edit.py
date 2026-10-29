"""The phone can rename a favourite: the sheet, the row's way into it, the call.

`PATCH /api/favorites` has been there all along (``update_favorites``, and its
own test in ``test_web_api.py``); what was missing was any surface on the phone
that reached it.  These three read the page itself, because that is where the
gap was — the route, the labels and the wiring all live in one 6 800-line file
with no module of its own, and the failure this guards against is a sheet that
exists but is never opened, or an open sheet whose save button sends the wrong
shape.

The assertions are text-level and deliberately narrow: the page is vanilla JS
in a string of HTML, so a "does it work" test would need a browser.  What is
checkable here is that the pieces are all present and joined — which is exactly
the seam that was missing.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "internal" / "web" / "static" / "mobile.html"


def source() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_sheet_exists_with_a_label_for_every_control():
    text = source()
    sheet = re.search(
        r'<div class="modal-mask" id="favEdit".*?</div>\s*</div>', text, re.S
    )
    assert sheet, "the rename sheet is gone"
    block = sheet.group(0)
    # Both fields are labelled from the word table rather than carrying a
    # hardcoded sentence: this page ships two languages from one file.
    assert 'id="favEditNameLabel"' in block
    assert 'id="favEditGroupLabel"' in block
    assert 'id="favEditName"' in block
    assert 'id="favEditGroup"' in block
    # A datalist, so a group that already exists can be picked instead of
    # spelled: typing a new one is how a group is created, and a picker alone
    # could not do that.
    assert 'id="favEditGroupOptions"' in block
    # And the two answers.
    assert 'id="favEditSave"' in block
    assert 'id="favEditCancel"' in block


def test_the_row_can_open_it_without_also_pushing_the_clip():
    """A row copies on a tap, so an action inside it has to stop that tap.

    Without the `stopPropagation` the reader renaming a favourite also sends it
    to the desktop clipboard — an edit with a side effect nobody asked for.
    """
    text = source()
    row = re.search(r"var editButton = el\.querySelector\('\.f-item__edit'\);.*?\}\);", text, re.S)
    assert row, "the row's edit button is not wired"
    block = row.group(0)
    assert "ev.stopPropagation()" in block
    assert "favEditShow(f, editButton)" in block


def test_saving_sends_the_shape_the_route_reads():
    text = source()
    submit = re.search(r"function favEditSubmit\(\).*?\.then\(function \(\) \{ save\.disabled = false; \}\);", text, re.S)
    assert submit, "the save handler is gone"
    block = submit.group(0)
    assert "method: 'PATCH'" in block
    assert "'/api/favorites'" in block
    # One item, three fields — the single-favourite shape, not the batch one.
    assert "id: f.id, title:" in block
    assert "group:" in block
    # A renamed group changes the chip rail, which is drawn from the list.
    assert "reloadFavorites()" in block


def test_the_sheet_gets_out_the_same_ways_as_the_others():
    """Esc, a tap on the mask, and the cancel button — the row menu's three.

    One more surface that traps focus and can only be left with a button is a
    surface a phone user gets stuck in.
    """
    text = source()
    wiring = re.search(r"if \(favEditEl\) \{.*?\n  \}", text, re.S)
    assert wiring, "the sheet's listeners are gone"
    block = wiring.group(0)
    assert "event.key === 'Escape'" in block
    assert "event.target === favEditEl" in block
    assert "trapTab(event, favEditEl, favEditBox)" in block
    assert "favEditCancel').addEventListener('click', favEditHide)" in block
