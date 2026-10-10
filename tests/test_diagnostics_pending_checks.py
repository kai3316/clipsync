"""The overview's network chip: what makes it say 网络需注意 on the first screen.

Reported as "概览首屏显示'网络需注意'→点一下才变'网络正常'".  The page's chip is the
diagnostics `summary` (`OverviewView.vue` reads `report.summary`, and re-reads it every 8s), so the
question is which check was not ok and why.

The handoff guessed the network check — an empty `lan_ip` at startup — but that cannot produce this
word: `network` is in `SUMMARY_CRITICAL_IDS`, so an empty address reads as **fail** (网络异常), not
warn.  Every other check was eliminated against the reporting install's own configuration
(`port=19990`, `web_port=19991`, `web_enabled=true`, a private 192.168.31.x address, Windows) and
its log, which shows the installed firewall rule is `got 19990,19991` — i.e. matching:

    server_port   ok      the report is only asked for once the engine runs
    discovery     ok      browsing is up
    advertising   **not ok for the first ~0.5s**   <- the only transient one
    web_companion ok      enabled and listening
    network       ok      private LAN address
    firewall      ok      the rule matches this install's ports
    permissions   ok      macOS only, and discovery is up
    mdns          ok      Linux only
    clipboard_tool / pasteboard_bridge  not added on Windows at all

`advertising` is false because `Discovery.start()` opens the browser first and publishes this
device's own mDNS record from a worker thread — the docstring there measures that at "well over a
second", and the reporting install's log shows the window directly:

    11:36:47.916  Started browsing for peers
    11:36:47.935  Registering mDNS on 192.168.31.38
    11:36:48.401  Registered mDNS service on port 5337     <- ~485 ms of "not advertising"

`summarize` counted that as a warning, so the chip said 网络需注意 until the next read.  Nothing was
wrong with the network; the device had simply not finished saying where it was.

These pin the fix: a check whose input is still coming up reports `pending`, and `summarize` leaves
it out of the verdict — while a device that really is not advertising still fails.
"""

from __future__ import annotations

import pytest

from internal.diagnostics import localize
from internal.diagnostics.report import build_checks, build_report, summarize


class Off:
    """The reporting install's configuration as the report reads it.

    Remote access is ON here (`web_enabled = True`), which is the one state in which every other
    check on that machine passes — see `test_an_install_with_remote_access_off_is_not_warned_about`.
    """

    port = 19990
    web_port = 19991
    web_enabled = True
    internet_sync_enabled = False
    relay_brokers: list[str] = []
    netpair_secrets: dict[str, str] = {}
    device_id = "a"
    device_name = "NC"


class Discovery:
    """A discovery service in the middle of publishing its own record.

    The state `Discovery.start()` actually passes through: browsing is up (so the critical
    `discovery` check passes) and `is_advertising` is still false while the registration worker
    finishes.
    """

    def __init__(self, advertising: bool, registering: bool):
        self.is_browsing = True
        self.is_advertising = advertising
        self.is_registering = registering


class Transport:
    _running = True

    def get_connected_peers(self):
        return []


class Web:
    """A companion whose firewall rule is already in place.

    Injected rather than left to the real probe, which shells out to netsh/ufw/socketfilterfw: the
    cases here are about the verdict, and a test that reads this machine's firewall would answer
    differently on the next one.
    """

    def check_firewall_rule(self, ports):
        return True, ""


def checks_with(**overrides):
    """The flat check list for one machine state, with the defaults a working one has."""
    state = {
        "server_running": True,
        "discovery_running": True,
        "advertising": True,
        "web_running": True,
        "lan_ip": "192.168.31.38",
        "registering": False,
    }
    state.update(overrides)
    registering = state.pop("registering")
    return build_checks(Off(), web=Web(), registering=registering, **state)[0]


def by_id(checks):
    return {check["id"]: check for check in checks}


# ── the reported symptom, end to end ─────────────────────────────────────
def test_a_report_read_while_the_record_is_going_out_is_not_a_warning():
    """The chip's own value, built the way the running sidecar builds it."""
    report = build_report(
        cfg=Off(),
        lan_ip="192.168.31.38",
        web=Web(),
        web_running=True,
        transport=Transport(),
        discovery=Discovery(advertising=False, registering=True),
    )
    assert report["summary"] == "ok", "the first screen said the network needed attention"
    advertising = by_id(report["checks"])["advertising"]
    assert advertising["pending"] is True
    # It is still in the list: a reader can see what it is waiting on.
    assert advertising["detail_key"] == "diag.advertising.pending.detail"


def test_the_same_report_a_moment_later_is_ok_because_the_record_is_out():
    """The control for the line above, and the reason the chip used to change on its own."""
    report = build_report(
        cfg=Off(),
        lan_ip="192.168.31.38",
        web=Web(),
        web_running=True,
        transport=Transport(),
        discovery=Discovery(advertising=True, registering=False),
    )
    assert report["summary"] == "ok"
    assert by_id(report["checks"])["advertising"]["ok"] is True


def test_a_device_that_really_is_not_advertising_still_warns():
    """The other half: nothing may hide the state the check exists for.

    Registration is not in flight here, so this is the machine the entry is about — 可见 switched
    off, or a registration that failed — and it has to keep reading as a fault.
    """
    report = build_report(
        cfg=Off(),
        lan_ip="192.168.31.38",
        web=Web(),
        web_running=True,
        transport=Transport(),
        discovery=Discovery(advertising=False, registering=False),
    )
    assert report["summary"] == "warn"
    advertising = by_id(report["checks"])["advertising"]
    assert advertising["ok"] is False
    assert not advertising.get("pending")
    assert advertising["guidance_key"] == "diag.advertising.fail.guidance"


# ── summarize's own rule ─────────────────────────────────────────────────
def test_summarize_leaves_a_pending_check_out_of_both_readings():
    checks = checks_with(advertising=False, registering=True)
    assert by_id(checks)["advertising"]["pending"] is True
    assert summarize(checks) == "ok"


def test_summarize_leaves_a_pending_critical_check_out_too():
    """Pending is not "ignore the critical ones": it is "not decided yet"."""
    assert summarize([
        {"id": "network", "ok": False, "pending": True},
        {"id": "advertising", "ok": True},
    ]) == "ok"


def test_summarize_still_fails_and_warns_on_settled_checks():
    """The control: the two readings this function is for are untouched."""
    assert summarize([{"id": "network", "ok": False}, {"id": "advertising", "ok": True}]) == "fail"
    assert summarize([{"id": "network", "ok": True}, {"id": "advertising", "ok": False}]) == "warn"
    assert summarize([{"id": "network", "ok": True}, {"id": "advertising", "ok": True}]) == "ok"


def test_only_the_advertising_check_can_be_pending_in_a_starting_runtime():
    """A guard on the blast radius: every other check answers for a settled state.

    The handoff asked for the whole table to be read before touching one entry, so this says which
    entries the "still coming up" case can reach.  Growing this list is a decision, not an accident:
    each entry added is a fault the page can no longer report while it is pending.
    """
    pending = [
        check["id"] for check in checks_with(advertising=False, registering=True)
        if check.get("pending")
    ]
    assert pending == ["advertising"]


# ── the same chip, and the other check that used to light it ─────────────
def test_an_install_with_remote_access_off_is_not_warned_about():
    """The second cause of the same word, found while reading the whole table.

    `web_companion` reported a fault whenever the companion was not running — and remote access off
    is the *default* state of a working install, so the overview's chip read 网络需注意 for as long
    as it stayed off.  The network group's own item had already settled this ("remote access off is
    the default state of a working install, and a warning for it made the summary read 存在警告 on a
    machine with nothing wrong"); the flat entry was the one place the rule was not applied.
    """

    class NoRemote(Off):
        web_enabled = False

    checks = build_checks(
        NoRemote(), server_running=True, discovery_running=True, advertising=True,
        web_running=False, lan_ip="192.168.31.38", web=Web(),
    )[0]
    off = by_id(checks)["web_companion"]
    assert off["ok"] is True, "an optional feature being off is not a fault"
    assert summarize(checks) == "ok"


def test_remote_access_enabled_but_not_running_is_still_a_fault():
    """The half that must not be swallowed: the feature is on and is not up."""
    checks = checks_with(web_running=False)
    assert by_id(checks)["web_companion"]["ok"] is False
    assert summarize(checks) == "warn"


@pytest.mark.parametrize("language", ["en", "zh-CN"])
def test_the_switched_off_companion_has_words_in_both_catalogs(language):
    out = localize.localize({"checks": checks_with(web_running=False)}, language)
    entry = by_id(out["checks"])["web_companion"]
    assert entry.get("detail_text"), f"no detail in {language}"


# ── the word itself ──────────────────────────────────────────────────────
@pytest.mark.parametrize("language", ["en", "zh-CN"])
def test_the_pending_detail_resolves_in_both_catalogs(language):
    """A key in the payload and not in a catalog renders as nothing, which reads as a bug."""
    out = localize.localize({"checks": checks_with(advertising=False, registering=True)}, language)
    entry = by_id(out["checks"])["advertising"]
    assert entry.get("detail_text"), f"no detail in {language}: {entry.get('detail_key')}"
    # And it says the wait, not the failure.
    assert entry["detail_text"] != by_id(checks_with())["advertising"]["detail"]


def test_a_starting_discovery_says_its_record_is_still_going_out():
    """The signal itself, on the object the runtime really passes to `build_report`.

    Everything above drives a stub, and the report reads the flag through `getattr(..., False)` — so
    if `Discovery` stopped exposing it, every one of those tests would stay green while the overview
    went back to saying 网络需注意 on every launch.  This is the wiring, asserted where it lives.

    The flag is set and cleared directly rather than by calling `start()`: that opens real mDNS
    sockets and publishes a record for this machine, which is not something a unit test should do.
    """
    from internal.transport.discovery import Discovery

    discovery = Discovery("local", "Local", 19990, "_clipsync._tcp.local.")
    try:
        assert discovery.is_registering is False
        discovery._registering.set()
        assert discovery.is_registering is True
    finally:
        discovery._registering.clear()
