"""The phone's diagnostics card offers the repair it cannot perform.

U192's judgement was "只做复制修复命令，不加按钮": the phone cannot change a
firewall or start a daemon, and a one-tap fix covering two of nine checks left
the rest reading as problems with no next step.  The report now carries a
`command` (see `test_diag_fix_command.py`); this file is about the page that
shows it — a card rebuilt from a string on every poll, so the pieces are only
verifiable by reading it.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "internal" / "web" / "static" / "mobile.html"


def source() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_a_failed_check_with_a_command_shows_it_copyable():
    text = source()
    block = re.search(r"if \(shown && !chk\.ok && chk\.command\) \{.*?\n      \}", text, re.S)
    assert block, "the card no longer renders a repair command"
    html = block.group(0)
    # The line itself, on screen: a copy button for a command nobody can read
    # is asking for trust instead of showing the work.
    assert "chk.command" in html
    assert 'class="diag-cmd__text"' in html
    assert 'data-diag-copy="' in html
    # And a sentence saying where to run it — this page cannot.
    assert "T.diagCmdLabel" in html


def test_a_passing_check_offers_no_command():
    """`!chk.ok` is part of the condition, not just `chk.command`.

    A probe that leaves its command set after the problem is gone would offer a
    repair for a fault that is no longer there.
    """
    text = source()
    assert "if (shown && !chk.ok && chk.command)" in text


def test_the_copy_reads_the_command_from_the_state_not_the_dom():
    """The card is re-rendered from a string on every poll tick.

    Embedding the command in an attribute would survive nothing: the row is
    rebuilt, and an escaped `&&` in a data attribute is one decode away from
    being the wrong string.  The handler looks the check up instead.
    """
    text = source()
    handler = re.search(r"function copyDiagCommand\(checkId\) \{.*?\n  \}", text, re.S)
    assert handler, "the copy handler is gone"
    block = handler.group(0)
    assert "diagState.checks" in block
    assert "navigator.clipboard.writeText(cmd)" in block
    # Plain-HTTP LAN access is not a secure context, so the API can be absent —
    # and a copy button that does nothing reads as broken.
    assert "if (!navigator.clipboard" in block
    assert "T.diagCopyFail" in block
    assert "T.diagCopied" in block


def test_the_card_delegates_both_buttons():
    """One listener, two actions: the card is rebuilt by renders and polls."""
    text = source()
    wire = re.search(r"settingsView\.addEventListener\('click'.*?\n  \}\);", text, re.S)
    assert wire, "the card's click delegation is gone"
    block = wire.group(0)
    assert "[data-diag-fix]" in block
    assert "[data-diag-copy]" in block
    assert "copyDiagCommand(" in block
