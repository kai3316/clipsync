"""The grouped diagnostics payload (``v2: true`` + ``groups``) the shell draws.

  1. Response shape: every expected group present, every item has a valid
     ``status`` (ok/warn/fail) and a non-empty ``detail``.
  2. Backward compatibility: the legacy flat ``checks`` + top-level summary
     fields survive untouched so the mobile card / overview health hint keep
     working.
  3. Defensive probing: a minimal empty-app stub (every manager None) still
     produces a full renderable payload — no exceptions.
  4. Happy path: with fake managers wired up, the network/internet groups
     report ok where appropriate and the transfer group flags failures.

The web panel's own markup and locale keys are view detail and are not asserted
here.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.sync.ai_profiles import DEFAULT_TOOL_KEYS
from src.main import Application  # noqa: E402

_GROUPS = ("system", "network", "internet", "ai_config", "chat", "transfer", "filesystem")
_STATUSES = ("ok", "warn", "fail")


def _base_cfg(**over):
    """A Config-shaped SimpleNamespace with every field the probes read."""
    values = dict(
        port=19990,
        web_port=19991,
        web_enabled=False,
        internet_sync_enabled=False,
        relay_brokers=["wss://broker.emqx.io:8084/mqtt"],
        netpair_secrets={},
        ai_config_tools=list(DEFAULT_TOOL_KEYS),
        ai_config_custom_paths=[],
        data_dir="",
        device_id="a1b2c3d4e5f6",
        device_name="DevA",
        peers={},
    )
    values.update(over)
    return types.SimpleNamespace(**values)


def _minimal_app(**attrs):
    """Empty-app stub: cfg set, every manager None (worst-case degraded app)."""
    app = object.__new__(Application)
    app.cfg = _base_cfg(**attrs.get("cfg", {}))
    app.transport_mgr = None
    app.discovery = None
    app.web_server = None
    app.pairing_mgr = None
    app.chat_mgr = None
    app.aicfg_mgr = None
    app.file_transfer_mgr = None
    app.clipboard_history = None
    app._start_time = 1000
    # _relay deliberately NOT set — the accessor must be defensive.
    return app


def _assert_shape(data):
    assert data.get("v2") is True
    groups = data.get("groups")
    assert isinstance(groups, dict), "groups must be a dict"
    assert set(groups.keys()) == set(_GROUPS), (
        f"expected groups {_GROUPS}, got {sorted(groups.keys())}"
    )
    for gid in _GROUPS:
        g = groups[gid]
        assert isinstance(g, dict) and "items" in g, gid
        assert g.get("label_key"), f"{gid} missing label_key"
        assert g["items"], f"{gid} has no items"
        for it in g["items"]:
            assert it["status"] in _STATUSES, (gid, it["id"], it["status"])
            assert isinstance(it["detail"], str) and it["detail"], (gid, it)
            # hint is optional but must be a string or None when present
            assert "hint" in it and (it["hint"] is None or isinstance(it["hint"], str)), it
            # if a detail_key is advertised the params must be a dict
            if it.get("detail_key"):
                assert isinstance(it.get("detail_params") or {}, dict), it
    return groups


# ── 1. response shape + backward-compatible top-level fields ─────────────


def test_minimal_stub_returns_full_group_shape(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    data = _minimal_app()._get_diagnostics()
    _assert_shape(data)


def test_legacy_top_level_fields_preserved(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    data = _minimal_app()._get_diagnostics()
    for key in (
        "summary",
        "checks",
        "discovery_running",
        "server_running",
        "connected_count",
        "paired_count",
        "web_companion_running",
        "web_port",
        "lan_ip",
        "os",
        "version",
    ):
        assert key in data, f"legacy field missing: {key}"
    assert isinstance(data["checks"], list)
    assert isinstance(data["summary"], str) and data["summary"] in _STATUSES


# ── 2. defensive: a degraded app still renders ───────────────────────────


def test_get_diagnostics_never_raises_on_bare_app(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    # Nothing at all except cfg — no _start_time, no managers.
    app = object.__new__(Application)
    app.cfg = _base_cfg()
    data = app._get_diagnostics()  # must not raise
    assert data.get("v2") is True


# ── 3. happy path with fake managers ─────────────────────────────────────


def _full_app(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    app = object.__new__(Application)
    app.cfg = _base_cfg(
        web_enabled=True,
        internet_sync_enabled=True,
        netpair_secrets={"peer1": "secret"},
        ai_config_custom_paths=["~/claude"],
    )

    class Relay:
        state = "online"

    app._relay = Relay()
    app._delivery_lock = __import__("threading").RLock()
    app._delivery_queue = {}
    app._delivery_ledger = {}
    app._start_time = 1000

    class Transport:
        _running = True

        def get_connected_peers(self):
            return []

        def get_connected_peers_with_names(self):
            return []

        def get_reconnect_states(self):
            return {}

    app.transport_mgr = Transport()

    class Discovery:
        is_browsing = True
        is_advertising = True

    app.discovery = Discovery()

    class WebServer:
        is_running = True

        @staticmethod
        def _get_lan_ip():
            return "192.168.1.5"

        @staticmethod
        def check_firewall_rule(ports):
            return True, "No firewall blockage detected"

    app.web_server = WebServer()

    class Pairing:
        def get_paired_peers(self):
            return []

    app.pairing_mgr = Pairing()

    class Chat:
        def get_sessions(self):
            return []

    app.chat_mgr = Chat()

    class AICfg:
        def local_summary(self):
            import time

            return {"collected_at": time.time(), "entry_count": 4, "paths": ["~/claude"]}

        def _trash_base(self):
            return tmp_path / "aiconfig_trash"

    app.aicfg_mgr = AICfg()

    class FileTransfer:
        def get_transfers(self):
            return [{"status": "in_progress"}]

        def get_history(self):
            return [{"success": True}, {"success": False}]

    app.file_transfer_mgr = FileTransfer()

    class History:
        _db_path = tmp_path / "clipboard_history.db"

    (tmp_path / "clipboard_history.db").write_bytes(b"x" * 2048)
    app.clipboard_history = History()

    return app


def test_happy_path_group_statuses(tmp_path, monkeypatch):
    data = _full_app(tmp_path, monkeypatch)._get_diagnostics()
    groups = _assert_shape(data)

    by_id = {it["id"]: it["status"] for it in groups["network"]["items"]}
    assert by_id["lan_ip"] == "ok"
    assert by_id["tcp_port"] == "ok"
    assert by_id["mdns_service"] == "ok"
    assert by_id["web_service"] == "ok"
    assert by_id["firewall"] == "ok"

    internet = {it["id"]: it["status"] for it in groups["internet"]["items"]}
    assert internet["relay_state"] == "ok"
    assert internet["brokers"] == "ok"
    assert internet["pending_count"] == "ok"
    assert internet["internet_enabled"] == "ok"

    ai = {it["id"]: it["status"] for it in groups["ai_config"]["items"]}
    assert ai["watch_roots"] == "ok"
    assert ai["local_entries"] == "ok"
    assert ai["last_collected"] == "ok"

    transfer = {it["id"]: it["status"] for it in groups["transfer"]["items"]}
    assert transfer["active_transfers"] == "ok"
    # One history row has success=False → warn.
    assert transfer["transfer_failures"] == "warn"


def test_item_ids_unique_within_group(tmp_path, monkeypatch):
    data = _full_app(tmp_path, monkeypatch)._get_diagnostics()
    for gid, g in data["groups"].items():
        ids = [it["id"] for it in g["items"]]
        assert len(ids) == len(set(ids)), f"duplicate item ids in {gid}: {ids}"
