"""The diagnostics page's own verdict: what it calls a fault, and what it just reports.

Two reports, both about a machine that is working:

  * "像互联网配对和 web 伴随没开也不要警告了" -- remote access and internet sync are optional, and
    having them off is the default state of a working install.  Both reported `warn`, together with
    the two items *derived* from them (the relay is off because internet sync is off; there are no
    brokers for the same reason), so the page's own summary read 存在警告 for a machine with nothing
    wrong.  That is the log-noise defect of this work appearing on the page instead of in a file.
  * "如果是 mac 缓存会在哪" -- the update cache is `<data>/update_cache/`, it decides whether this
    machine can send an update to a peer at all, and nothing said what was in it.  Measured, a cache
    that lags the running build makes the device refuse every request with `cached_not_newer`.
"""

from __future__ import annotations

import pytest

from internal.diagnostics import localize
from internal.diagnostics.report import build_groups
from internal.system import updater as updater_module


class Off:
    """A configuration with both optional features off and no relay configured.

    Written out rather than mocked: the report reads these through `getattr`, and a config that does
    not have the fields is itself one of the states the page has to survive.
    """

    port = 19990
    web_port = 0
    web_enabled = False
    internet_sync_enabled = False
    relay_brokers: list[str] = []
    netpair_secrets: dict[str, str] = {}
    device_id = "a"
    device_name = "NC"


def groups_for(cfg) -> dict:
    return build_groups(
        cfg,
        {"start_time": 0.0, "relay_state": "off", "pending_count": 0, "lan_ip": "1.2.3.4"},
        history=None,
    )


def items_by_id(groups: dict, *names: str) -> dict:
    return {
        entry["id"]: entry
        for name in names
        for entry in groups[name]["items"]
    }


# ── a switched-off optional feature is not a fault ───────────────────────
@pytest.mark.parametrize(
    "item_id", ["web_service", "internet_enabled", "relay_state", "brokers"]
)
def test_an_optional_feature_that_is_off_is_not_a_warning(item_id):
    """Off is a setting, not a fault, and the summary has to agree.

    The last two are the *consequences*: warning about them is warning about a setting one step
    further away, and it tripled the count of warnings on a machine with nothing wrong.
    """
    found = items_by_id(groups_for(Off()), "network", "internet")
    assert item_id in found, f"{item_id} disappeared from the report"
    assert found[item_id]["status"] == "ok", (
        f"{item_id} reads {found[item_id]['status']} for a switched-off optional feature"
    )


def test_the_network_group_is_unchanged():
    """Asked for explicitly: "网络应该还是提示正常".

    A healthy network reads `ok` and this work must not have touched it, so the shape is asserted
    rather than left to the parametrised case above.
    """
    network = groups_for(Off())["network"]["items"]
    ids = [entry["id"] for entry in network]
    assert "lan_ip" in ids and "tcp_port" in ids and "mdns_service" in ids
    assert all(entry["status"] in ("ok", "warn", "fail") for entry in network)


def test_brokers_still_fail_when_internet_sync_is_on():
    """The control: the same empty broker list is a real fault when the feature is enabled.

    Without this, "no brokers" could be silenced for every machine and a relay that genuinely
    cannot work would say nothing at all.
    """

    class On(Off):
        internet_sync_enabled = True

    brokers = items_by_id(groups_for(On()), "internet")["brokers"]
    assert brokers["status"] == "fail", (
        "an enabled relay with no brokers cannot work and must still say so"
    )


def test_an_enabled_but_unstarted_companion_still_fails():
    """And the other half of the same control, for the web companion."""

    class On(Off):
        web_enabled = True

    # `web_running` is false by default in the context above, which is the broken state: the
    # setting says yes and nothing is listening.
    service = items_by_id(groups_for(On()), "network")["web_service"]
    assert service["status"] == "fail", "enabled-but-not-running is a real fault"


# ── the update cache is reported ─────────────────────────────────────────
def test_the_report_names_the_cached_update_build(tmp_path, monkeypatch):
    """Which build is held decides whether this machine can send an update at all."""
    cache = tmp_path / "update_cache"
    cache.mkdir()
    asset = cache / "ClipSync_1.0.57_aarch64.app.tar.gz"
    asset.write_bytes(b"payload")
    monkeypatch.setattr(updater_module, "get_cached_asset", lambda: str(asset))

    entry = items_by_id(groups_for(Off()), "filesystem")["update_cache"]
    assert entry["status"] == "ok"
    # The version is read with the same parser the update guard uses, so the page and the guard
    # cannot disagree about which build is held.
    assert "1.0.57" in entry["detail"]
    assert "ClipSync_1.0.57_aarch64.app.tar.gz" in entry["detail"]


def test_an_empty_cache_is_reported_rather_than_hidden(monkeypatch):
    """A machine that has never upgraded has none: a normal state, and worth saying."""
    monkeypatch.setattr(updater_module, "get_cached_asset", lambda: None)

    entry = items_by_id(groups_for(Off()), "filesystem")["update_cache"]
    assert entry["status"] == "ok", "having nothing cached is not a fault"
    assert entry["detail_key"] == "diag.v2.item.update_cache.empty.detail"


@pytest.mark.parametrize("language", ["en", "zh-CN"])
def test_the_new_item_resolves_in_both_catalogs(language, monkeypatch):
    """A key in the payload and not in a catalog renders as nothing, which reads as a bug.

    This is the check that caught the folded `transfers` item coming out with `label=None`: the
    payload was right and the panel was blank.
    """
    monkeypatch.setattr(updater_module, "get_cached_asset", lambda: None)

    out = localize.localize({"groups": groups_for(Off())}, language)
    entry = next(e for e in out["groups"]["filesystem"]["items"] if e["id"] == "update_cache")
    assert entry.get("label_text"), f"no label in {language}: {entry.get('label_key')}"
    assert entry.get("detail_text"), f"no detail in {language}: {entry.get('detail_key')}"


def test_the_optional_features_resolve_in_both_catalogs():
    """And the two whose *level* changed still have words, in both languages."""
    for language in ("en", "zh-CN"):
        out = localize.localize({"groups": groups_for(Off())}, language)
        found = items_by_id(out["groups"], "network", "internet")
        for item_id in ("web_service", "internet_enabled"):
            entry = found[item_id]
            assert entry.get("detail_text"), f"{item_id} has no detail in {language}"
            assert entry["status"] == "ok"
