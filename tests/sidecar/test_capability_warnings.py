"""`app.status` carries what the application cannot do on this machine.

The condition this exists for is Linux's clipboard, and the reason it is here
rather than only in the diagnostics report is the shape of that failure: without
`xclip` or `wl-paste` ClipSync starts, pairs, lists devices and shows an empty
history, because it never sees a copy.  The user has no reason to open a
diagnostics page for an application that appears to be working, so the answer has
to arrive where the window already looks.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from internal.application import bootstrap  # noqa: E402
from internal.diagnostics import report as diagnostics  # noqa: E402


def _pretend_linux(monkeypatch):
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")


def _probe(monkeypatch, result):
    monkeypatch.setattr(diagnostics, "clipboard_tool_probe", lambda: result)


def test_a_non_linux_host_is_never_warned_at(monkeypatch):
    """macOS and Windows read their clipboard natively: nothing to install."""
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Windows")
    _probe(monkeypatch, {"ok": False, "detail": "should not be consulted"})
    assert bootstrap.capability_warnings() == []


def test_a_linux_host_with_the_tool_is_not_warned_at(monkeypatch):
    _pretend_linux(monkeypatch)
    _probe(monkeypatch, {"ok": True, "detail": "clipboard tool present"})
    assert bootstrap.capability_warnings() == []


def test_a_linux_host_without_the_tool_is_told_what_to_install(monkeypatch):
    _pretend_linux(monkeypatch)
    _probe(
        monkeypatch,
        {
            "ok": False,
            "detail": "no xclip / wl-paste",
            "detail_key": "diag.clipboard_tool.fail.detail",
            "guidance": "Install one: 'sudo apt install xclip'",
            "guidance_key": "diag.clipboard_tool.fail.guidance",
        },
    )
    warnings = bootstrap.capability_warnings()
    assert [w["code"] for w in warnings] == ["clipboard_tool_missing"]
    assert warnings[0]["guidance_key"] == "diag.clipboard_tool.fail.guidance"
    assert "xclip" in warnings[0]["guidance"]


def test_the_wording_comes_from_the_diagnostics_probe(monkeypatch):
    """One source for both surfaces, so they cannot describe different problems.

    The item in the diagnostics report and the warning on the status bar are the
    same fact.  If this function worded its own sentence, the two would drift and
    a user comparing them would have to decide which one to believe.
    """
    _pretend_linux(monkeypatch)
    _probe(
        monkeypatch,
        {
            "ok": False,
            "detail": "no xclip / wl-paste",
            "detail_key": "diag.clipboard_tool.fail.detail",
            "guidance": "sentinel guidance",
            "guidance_key": "diag.clipboard_tool.fail.guidance",
        },
    )
    warning = bootstrap.capability_warnings()[0]
    assert warning["detail"] == "no xclip / wl-paste"
    assert warning["guidance"] == "sentinel guidance"


def test_a_probe_that_raises_is_reported_as_nothing_rather_than_failing(monkeypatch):
    """An unanswerable probe means "unknown", and a status read must still answer.

    `app.status` is what the window calls to decide whether it can draw anything
    at all; letting a subprocess probe take it down would turn a missing clipboard
    into an application that will not open.
    """

    def explode():
        raise OSError("no /bin/sh here")

    _pretend_linux(monkeypatch)
    monkeypatch.setattr(diagnostics, "clipboard_tool_probe", explode)
    assert bootstrap.capability_warnings() == []


def test_the_status_payload_carries_the_key(monkeypatch):
    """The helper is worthless if it is not wired into what the window reads."""
    _pretend_linux(monkeypatch)
    _probe(monkeypatch, {"ok": False, "detail": "no xclip", "guidance": "install xclip"})

    class Locked:
        config = None
        runtime = None
        _health = "locked"

    payload = bootstrap.SidecarApplication.status(Locked())
    assert "warnings" in payload
    assert [w["code"] for w in payload["warnings"]] == ["clipboard_tool_missing"]


# ── The macOS pasteboard bridge diagnosis ─────────────────────────────────────

def test_a_non_macos_host_has_no_pasteboard_check(monkeypatch):
    """Windows and Linux read their clipboard by other means; nothing to report."""
    monkeypatch.setattr(diagnostics.platform, "system", lambda: "Windows")
    assert diagnostics.pasteboard_bridge_probe() is None


def test_the_report_names_every_step(monkeypatch):
    """The first step that is False is the answer, so each must be visible.

    A detail string saying only "bridge unavailable" would leave the log exactly where it
    started -- which is the whole reason this probe exists.
    """
    monkeypatch.setattr(diagnostics.platform, "system", lambda: "Darwin")
    monkeypatch.setitem(
        __import__("sys").modules,
        "internal.clipboard.clipboard_darwin",
        type(
            "M",
            (),
            {
                "probe_pasteboard_bridge": staticmethod(
                    lambda: {
                        "objc_path": "/usr/lib/libobjc.A.dylib",
                        "objc_loadable": True,
                        "classes": {"NSPasteboard": True, "NSApplication": True},
                        "pasteboard_instance": False,
                        "after_nsapplicationload": True,
                        "pbpaste_works": True,
                    }
                )
            },
        ),
    )

    check = diagnostics.pasteboard_bridge_probe()

    assert check is not None
    assert check["ok"] is False
    assert check["id"] == "pasteboard_bridge"
    for fragment in (
        "libobjc=/usr/lib/libobjc.A.dylib",
        "loaded=True",
        "NSPasteboard=True",
        "pasteboard=False",
        "pbpaste=True",
        # The experiment, which is what names the fault when it flips.
        "after_NSApplicationLoad=True",
    ):
        assert fragment in check["detail"], f"{fragment!r} missing from {check['detail']!r}"


def test_a_working_bridge_is_not_a_warning(monkeypatch):
    """A check that always complains is a check nobody reads."""
    monkeypatch.setattr(diagnostics.platform, "system", lambda: "Darwin")
    monkeypatch.setitem(
        __import__("sys").modules,
        "internal.clipboard.clipboard_darwin",
        type(
            "M",
            (),
            {
                "probe_pasteboard_bridge": staticmethod(
                    lambda: {
                        "objc_path": "/usr/lib/libobjc.A.dylib",
                        "objc_loadable": True,
                        "classes": {"NSPasteboard": True},
                        "pasteboard_instance": True,
                        "after_nsapplicationload": None,
                        "pbpaste_works": True,
                    }
                )
            },
        ),
    )

    check = diagnostics.pasteboard_bridge_probe()

    assert check is not None
    assert check["ok"] is True
    assert check["guidance"] is None


def test_a_probe_that_cannot_run_still_reports(monkeypatch):
    """A diagnosis that fails is a fact worth carrying, not an exception to raise."""
    monkeypatch.setattr(diagnostics.platform, "system", lambda: "Darwin")

    def explode():
        raise RuntimeError("no ctypes here")

    monkeypatch.setitem(
        __import__("sys").modules,
        "internal.clipboard.clipboard_darwin",
        type("M", (), {"probe_pasteboard_bridge": staticmethod(explode)}),
    )

    check = diagnostics.pasteboard_bridge_probe()

    assert check is not None
    assert check["ok"] is False
    assert "RuntimeError" in check["detail"] and "no ctypes here" in check["detail"]
