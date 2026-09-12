"""Where a history row came from, and how often it has been pasted.

The legacy panel's row carried the device a clip synced from, the application it
was copied in, that window's title and a paste count.  The window shows the same
rows and has no other way to learn any of it: the list is the only payload it
sees, and a row's own text does not say which machine it came from.
"""

from unittest.mock import Mock

import pytest

from internal.application.bootstrap import SidecarApplication
from internal.application.use_cases.history import (
    SOURCE_TITLE_LIMIT,
    WEB_SOURCE,
    WEB_SOURCE_LABEL,
    HistoryUseCase,
    source_name,
)
from internal.config.config import Config, PeerInfo, save


def use_case(entries, label=None):
    """A history use case over fixed rows, with a recorder for the label."""
    repository = Mock()
    repository.get_all.return_value = entries
    return HistoryUseCase(repository, source_label=label)


def row(**overrides):
    entry = {
        "entry_id": 0, "timestamp": 1, "text_preview": "clip", "content_type": "TEXT",
        "pinned": False, "types": {}, "source_device": "", "source_app": "",
        "source_title": "", "paste_count": 0,
    }
    entry.update(overrides)
    return entry


def test_a_row_ships_the_provenance_and_count_the_legacy_row_showed():
    service = use_case(
        [row(source_app="chrome", source_title="Inbox", paste_count=3)],
        label=lambda source: source if source else "Laptop",
    )
    item = service.list()["items"][0]
    assert item["source_app"] == "chrome"
    assert item["source_title"] == "Inbox"
    assert item["source_name"] == "Laptop"
    assert item["paste_count"] == 3


def test_a_row_without_provenance_ships_empty_strings_and_a_zero():
    # Every key is present on every row: the window renders from these, and a
    # missing key would be a field it has to guard for no reason.
    item = use_case([row()]).list()["items"][0]
    assert item["source_app"] == ""
    assert item["source_title"] == ""
    assert item["source_name"] == ""
    assert item["paste_count"] == 0


def test_a_row_ships_the_route_it_arrived_on():
    """The route is the half of the pair a device name cannot give: a peer
    reachable both ways sends over whichever is up, and the phone's pushed rows
    are named "Web" without saying where that browser was."""
    service = use_case([
        row(transport="relay"),
        row(transport="lan"),
        row(transport="web"),
        # A row written before the route was recorded has no such field at all.
        row(),
    ])
    assert [item["transport"] for item in service.list()["items"]] == [
        "relay", "lan", "web", "",
    ]


def test_the_label_is_asked_about_the_row_and_not_the_caller():
    asked = []
    service = use_case(
        [row(source_device="peer-1"), row(source_device="")],
        label=lambda source: asked.append(source) or "name",
    )
    service.list()
    assert asked == ["peer-1", ""]


def test_a_title_longer_than_a_window_title_is_cut():
    # The title is chosen by whichever application was in front, so the row is
    # not the place to carry a pathological one in full.
    title = "t" * (SOURCE_TITLE_LIMIT + 50)
    item = use_case([row(source_title=title)]).list()["items"][0]
    assert len(item["source_title"]) == SOURCE_TITLE_LIMIT


@pytest.mark.parametrize(
    "source_device,names,local,expected",
    [
        # A clip captured here carries no source_device at all.
        ("", {"d1": "Laptop"}, "Laptop", "Laptop"),
        ("d1", {"d1": "Laptop"}, "Laptop", "Laptop"),
        # A peer this device still knows.
        ("p1", {"d1": "Laptop", "p1": "Studio"}, "Laptop", "Studio"),
        # A peer it no longer knows: the id it has, not a blank.
        ("gone", {"d1": "Laptop"}, "Laptop", "gone"),
        # The phone is not a paired peer and has no id to be named by, so the
        # caller's mapping is where its label comes from.
        (
            WEB_SOURCE,
            {"d1": "Laptop", WEB_SOURCE: WEB_SOURCE_LABEL},
            "Laptop",
            WEB_SOURCE_LABEL,
        ),
    ],
)
def test_the_source_label_rule(source_device, names, local, expected):
    assert source_name(source_device, names, local) == expected


def test_the_runtime_names_the_devices_it_knows(tmp_path, monkeypatch):
    """The window's own rows, labelled from the config the app is running on.

    What the running app answers for the three kinds of source a row can carry:
    a clip captured here, a clip from a paired peer, and one the phone pushed.
    """
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    config = Config(encryption_enabled=False)
    config.device_name = "This laptop"
    config.peers["peer-1"] = PeerInfo(
        device_id="peer-1", device_name="Studio", public_key_pem="", paired=True
    )
    save(config)

    app = SidecarApplication()
    app.lifecycle.start()
    try:
        label = app.require_history()._source_label
        assert label("") == "This laptop"
        assert label(config.device_id) == "This laptop"
        assert label("peer-1") == "Studio"
        assert label(WEB_SOURCE) == WEB_SOURCE_LABEL
        # A peer this device no longer knows: the id it holds, not a blank.
        assert label("unpaired") == "unpaired"
    finally:
        assert app.lifecycle.stop()


def test_a_peer_paired_after_startup_is_named_without_a_restart(tmp_path, monkeypatch):
    # The label is read on every call rather than captured at startup: a clip
    # from a peer paired a minute ago must not show a raw id until the next
    # restart, which is what a captured mapping would do.
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    save(Config(encryption_enabled=False, device_name="This laptop"))

    app = SidecarApplication()
    app.lifecycle.start()
    try:
        assert app.require_history()._source_label("late-peer") == "late-peer"
        app.config.peers["late-peer"] = PeerInfo(
            device_id="late-peer", device_name="Newcomer", public_key_pem="", paired=True
        )
        assert app.require_history()._source_label("late-peer") == "Newcomer"
    finally:
        assert app.lifecycle.stop()
