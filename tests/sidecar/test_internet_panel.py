"""The host object behind the phone's internet-pairing and delivery routes.

``internal/web/api/internetpair.py`` and ``internetdelivery.py`` are
bind-style branches: they hand every request to whatever host object ``bind``
was given and have no error handling of their own beyond a bare
``except Exception`` → 500.  These tests pin the adapter's side of that
contract — the legacy method surface, the status codes the panel branches on,
and that it answers instead of raising when the runtime has no internet
subsystem at all.
"""

from types import SimpleNamespace

from internal.config.config import Config
from internal.infrastructure.runtime.internet_pairing import InternetPairingService
from internal.infrastructure.runtime.internet_panel import InternetPanelHost
from internal.transport import relay as relay_transport


def panel(runtime):
    return InternetPanelHost(runtime)


def runtime_without_internet(**extra):
    """A runtime whose internet subsystem is missing or never configured."""
    return SimpleNamespace(config=Config(encryption_enabled=False), **extra)


# --------------------------------------------------------------- the contract


def test_a_runtime_without_internet_pairing_answers_503_not_an_exception():
    host = panel(runtime_without_internet())

    assert host._netpair_status() == (
        {"ok": False, "error": "internet pairing unavailable"}, 503,
    )
    assert host._netpair_generate() == (
        {"ok": False, "error": "internet pairing unavailable"}, 503,
    )


def test_a_failure_that_is_not_in_the_legacy_table_is_a_500_with_its_message():
    """An unexpected storage failure must not escape as a crashed handler.

    The legacy handler logged a failed save and still answered ``ok`` — the
    sidecar reports it instead, so the panel cannot show an alias that never
    reached the disk.
    """
    cfg = Config(encryption_enabled=False)
    cfg.netpair_secrets = {"remote": "secret"}

    def save_config():
        raise OSError("disk full")

    service = InternetPairingService(cfg, save_config)
    body, status = panel(runtime_without_internet(internet_pairing=service))._netpair_rename(
        "remote", "Desk"
    )
    assert status == 500
    assert body["ok"] is False and body["error"]


def test_a_probe_that_raises_does_not_take_the_route_with_it():
    def probe(brokers):
        raise RuntimeError("no sockets left")

    host = InternetPanelHost(runtime_without_internet(), probe=probe)
    # A crashing probe is a failed test with a body to render — never an
    # exception escaping into the route's bare `except Exception` → 500.
    assert host._netpair_test({"brokers": ["mqtt://a:1"]}) == (
        {"ok": False, "error": "relay test failed"}, 500,
    )


# -------------------------------------------------------- the panel's outputs


def test_delivery_rows_are_reported_under_the_panels_key():
    rows = [{"msg_id": "m1", "status": "queued", "ts": 1.0}]
    seen = []
    runtime = runtime_without_internet(
        relay_delivery_status=lambda peer_id="": (
            seen.append(peer_id) or {"pending": 3, "items": rows}
        ),
        delivery_counts=lambda: {"peers": {"peer": 3}},
    )
    host = panel(runtime)

    assert host._delivery_status("peer") == {"pending": 3, "sends": rows}
    assert host._delivery_counts() == {"peers": {"peer": 3}}
    # The peer filter the panel passed is forwarded, not dropped.
    assert seen == ["peer"]


def test_the_pairing_status_keeps_the_shape_the_panel_reads():
    cfg = Config(encryption_enabled=False)
    cfg.netpair_secrets = {"remote": "secret"}
    service = InternetPairingService(cfg, lambda: None)
    service.note_hello("remote", "Remote")
    host = panel(runtime_without_internet(internet_pairing=service))

    body, status = host._netpair_status()
    assert status == 200
    assert body["generated_code"] is None
    peer = body["peers"][0]
    assert (peer["peer_id"], peer["name"], peer["alias"], peer["paired"]) == (
        "remote", "Remote", "", True,
    )
    assert peer["online"] is True and isinstance(peer["last_seen"], float)


# ------------------------------------------------------------- broker probing


def test_brokers_are_probed_together_and_ranked_reachable_first(monkeypatch):
    def probe(endpoint, timeout=4.0):
        rows = {
            "mqtt://slow:1883": {"ok": True, "latency_ms": 120.0},
            "mqtt://dead:1883": {"ok": False, "latency_ms": None},
            "mqtt://fast:1883": {"ok": True, "latency_ms": 4.0},
        }
        return {"endpoint": endpoint, "detail": "x", **rows[endpoint]}

    monkeypatch.setattr(relay_transport, "probe_relay_endpoint", probe)
    rows = relay_transport.probe_relay_endpoints(
        ["mqtt://slow:1883", "mqtt://dead:1883", "mqtt://fast:1883", "", None, 7]
    )

    assert [row["endpoint"] for row in rows] == [
        "mqtt://fast:1883", "mqtt://slow:1883", "mqtt://dead:1883",
    ]
    assert [row["ok"] for row in rows] == [True, True, False]


def test_a_probe_that_crashes_reports_itself_instead_of_the_whole_batch(monkeypatch):
    def probe(endpoint, timeout=4.0):
        if endpoint == "mqtt://boom:1883":
            raise RuntimeError("socket exploded")
        return {"endpoint": endpoint, "ok": True, "latency_ms": 1.0, "detail": "reachable"}

    monkeypatch.setattr(relay_transport, "probe_relay_endpoint", probe)
    rows = relay_transport.probe_relay_endpoints(["mqtt://boom:1883", "mqtt://ok:1883"])

    assert [row["endpoint"] for row in rows] == ["mqtt://ok:1883", "mqtt://boom:1883"]
    boom = rows[1]
    assert boom["ok"] is False and boom["latency_ms"] is None and boom["detail"]


def test_an_unparseable_endpoint_is_reported_not_raised():
    rows = relay_transport.probe_relay_endpoints([":::not-an-endpoint:::"])

    assert len(rows) == 1
    assert rows[0]["ok"] is False and rows[0]["detail"]


def test_probing_an_empty_list_asks_for_nothing():
    assert relay_transport.probe_relay_endpoints([]) == []
    assert relay_transport.probe_relay_endpoints(None) == []
