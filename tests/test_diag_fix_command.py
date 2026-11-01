"""A failed check hands the reader a command, where one command repairs it.

U192's own judgement is "只做复制修复命令，不加按钮": the phone cannot run a
firewall change or start avahi, and a one-tap fix that only covers two of the
checks left the rest looking like problems with no next step.  So the report
carries the command, the phone shows it and offers to copy it, and this file
pins the two places it could go missing:

  * the probe, which has to produce a command that actually matches the firewall
    it found — `ufw` and `firewalld` do not take the same arguments;
  * `build_checks`, whose entries are a whitelist: a field added to a probe and
    not to the assembly reaches nobody.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from internal.diagnostics import report
from internal.diagnostics.report import build_checks, mdns_probe


class Cfg:
    port = 19990
    web_port = 19991
    web_enabled = False
    internet_sync_enabled = False
    relay_brokers: list[str] = []
    netpair_secrets: dict[str, str] = {}
    device_id = "a"
    device_name = "NC"


class Run:
    """`subprocess.run` as the Linux probes call it: stdout only, no side effects."""

    def __init__(self, by_argv):
        self.by_argv = by_argv

    def __call__(self, argv, **kwargs):
        for needle, stdout in self.by_argv.items():
            if needle in argv:
                return SimpleNamespace(stdout=stdout)
        raise FileNotFoundError(argv[0])


def test_ufw_gets_the_command_ufw_accepts(monkeypatch):
    monkeypatch.setattr(report.platform, "system", lambda: "Linux")
    monkeypatch.setattr(report.subprocess, "run", Run({"ufw": "Status: active\n"}))
    probe = report.firewall_probe(Cfg(), discovery_running=False, web=None)
    assert probe["ok"] is False
    # Both ports, both rules: the sync port and the companion port are separate
    # and a rule for one of them leaves the other blocked.
    assert probe["command"] == (
        "sudo ufw allow 19990/tcp && sudo ufw allow 19991/tcp"
    )


def test_firewalld_gets_its_own_dialect(monkeypatch):
    """`ufw allow 1/tcp` is not a firewalld command, and the copy must not be."""
    monkeypatch.setattr(report.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        report.subprocess, "run", Run({"systemctl": "active\n"})
    )
    probe = report.firewall_probe(Cfg(), discovery_running=False, web=None)
    assert probe["ok"] is False
    assert probe["command"].startswith("sudo firewall-cmd --permanent --add-port=19990/tcp")
    assert "--reload" in probe["command"]


def test_a_passing_check_offers_no_command(monkeypatch):
    """Nothing to repair is not "an empty command to copy"."""
    monkeypatch.setattr(report.platform, "system", lambda: "Linux")
    monkeypatch.setattr(report.subprocess, "run", Run({"ufw": "Status: inactive\n"}))
    probe = report.firewall_probe(Cfg(), discovery_running=False, web=None)
    assert probe["ok"] is True
    assert probe["command"] == ""


def test_mdns_probe_keeps_the_install_step(monkeypatch):
    """The sentence names two steps; a copyable form with one would fail.

    Starting a daemon that is not installed is a command that reports "unit not
    found" — a repair that cannot work on the machine it is offered to.
    """
    monkeypatch.setattr(report.platform, "system", lambda: "Linux")
    monkeypatch.setattr(report.subprocess, "run", Run({"systemctl": "inactive\n"}))
    probe = mdns_probe()
    assert probe["ok"] is False
    assert probe["command"] == (
        "sudo apt install avahi-daemon && sudo systemctl start avahi-daemon"
    )


def test_the_assembly_carries_the_command_through(monkeypatch):
    """`build_checks` names every field it keeps, so a new one has to be named.

    That whitelist is the seam: the probes are shared with the grouped v2
    report, and a field added there and forgotten here is a field the phone
    never sees — with nothing failing to say so.
    """
    monkeypatch.setattr(
        report,
        "firewall_probe",
        lambda *a, **k: {
            "ok": False, "detail": "d", "detail_key": None, "detail_params": {},
            "guidance": "g", "guidance_key": None, "guidance_params": {},
            "command": "sudo ufw allow 19990/tcp",
        },
    )
    monkeypatch.setattr(
        report,
        "mdns_probe",
        lambda: {
            "ok": False, "detail": "d", "detail_key": None,
            "guidance": "g", "guidance_key": None,
            "command": "sudo systemctl start avahi-daemon",
        },
    )
    checks, _firewall = build_checks(
        Cfg(), server_running=True, discovery_running=True,
        advertising=True, web_running=False, lan_ip="192.168.1.20",
    )
    by_id = {check["id"]: check for check in checks}
    assert by_id["firewall"]["command"] == "sudo ufw allow 19990/tcp"
    assert by_id["mdns"]["command"] == "sudo systemctl start avahi-daemon"


def test_a_check_without_a_command_says_so_with_an_empty_string(monkeypatch):
    """The page reads `chk.command` and draws a button when it is truthy.

    A missing key would work too, but an explicit "" is what keeps the payload
    the same shape for every check — and what this file can hold still.
    """
    monkeypatch.setattr(
        report,
        "firewall_probe",
        lambda *a, **k: {
            "ok": True, "detail": "d", "detail_key": None, "detail_params": {},
            "guidance": None, "guidance_key": None, "guidance_params": {},
        },
    )
    checks, _firewall = build_checks(
        Cfg(), server_running=True, discovery_running=True,
        advertising=True, web_running=False, lan_ip="192.168.1.20",
    )
    by_id = {check["id"]: check for check in checks}
    assert by_id["firewall"]["command"] == ""


@pytest.mark.parametrize(
    ("status", "expected_ok"),
    [
        # "inactive" contains "active"; a substring test read a stopped daemon
        # as a running one, which is why this is pinned by the whole word.
        ("active\n", True),
        ("activating\n", False),
        ("inactive\n", False),
        ("failed\n", False),
        ("unknown\n", False),
        ("", False),
    ],
)
def test_the_mdns_probe_reads_the_whole_word(monkeypatch, status, expected_ok):
    monkeypatch.setattr(report.platform, "system", lambda: "Linux")
    monkeypatch.setattr(report.subprocess, "run", Run({"systemctl": status}))
    probe = mdns_probe()
    assert probe["ok"] is expected_ok
