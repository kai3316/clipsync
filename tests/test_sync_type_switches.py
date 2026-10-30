"""Per-type sync switches (next 1.3.0 iteration) and the send-side filter.

Six booleans decide which kinds of clipboard content leave this machine:
``sync_text`` / ``sync_url`` / ``sync_html`` / ``sync_rtf`` / ``sync_image`` /
``sync_file``, all ON by default.  What is pinned here:

* the defaults and the schema (they are strict booleans, and the settings API
  reads and writes them like every other preference);
* the send-side filter: a disabled type is dropped from the outbound message —
  including the FILE payload that an offer is built from — while the local
  history keeps the whole capture and receiving is untouched;
* the regression that matters most for a switch nobody has touched yet: with
  every switch on, the message on the wire is exactly what it was before these
  switches existed.
"""

from __future__ import annotations

import json

import pytest

import internal.config.config as config_module
from internal.clipboard.format import ClipboardContent, ContentType
from internal.config.config import FIELD_RULES, Config
from internal.sync.manager import SyncManager
from internal.web.api import settings as settings_api

SWITCHES = {
    "sync_text": True,
    "sync_url": True,
    "sync_html": True,
    "sync_rtf": True,
    "sync_image": True,
    "sync_file": True,
}


# ── helpers: a manager on doubles, no real clipboard or network ───────


class _Monitor:
    max_poll_interval = 0.0

    def __init__(self):
        self.callback = None
        self.last_source_app = None
        self.last_concealed = False

    def start(self, callback):
        self.callback = callback

    def stop(self):
        self.callback = None

    def suppress_for(self, seconds):
        pass


class _Reader:
    def __init__(self, content):
        self.content = content

    def read(self):
        return self.content


class _Writer:
    def write(self, content):
        return True


class _History:
    def __init__(self):
        self.rows = []

    def add(self, content, source_app=None):
        self.rows.append(content)
        return f"row-{len(self.rows)}"


def _content(**types) -> ClipboardContent:
    return ClipboardContent(
        types={ContentType[name]: value for name, value in types.items()}
    )


def _capture(cfg, content):
    """One manager wired to a single capture, plus what it recorded and sent."""
    history = _History()
    sent: list = []
    manager = SyncManager(
        "device",
        "Device",
        reader=_Reader(content),
        writer=_Writer(),
        monitor=_Monitor(),
        history=history,
        sync_debounce=0.0,
        retry_enabled=False,
    )
    manager.set_config(cfg)
    manager.on_send = sent.append
    manager._do_read_and_send()
    return manager, history, sent


# ── the config surface ────────────────────────────────────────────────


def test_the_six_switches_default_to_on():
    cfg = Config()
    for name in SWITCHES:
        assert getattr(cfg, name) is True, f"{name} should default to True"
        assert FIELD_RULES[name] == ("bool",), f"{name} must be a strict boolean"


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    monkeypatch.setattr(config_module, "_config_dir", lambda: config_dir)
    monkeypatch.setattr(config_module, "_config_path", lambda: config_path)
    return config_path


def test_the_switches_round_trip_and_back_up(isolated_config):
    config_path = isolated_config
    cfg = Config()
    cfg.sync_text = False
    cfg.sync_image = False
    config_module.save(cfg)

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["sync_text"] is False
    assert raw["sync_image"] is False
    assert raw["sync_url"] is True
    loaded = config_module.load()
    assert loaded.sync_text is False
    assert loaded.sync_image is False

    from internal.data.backup import backup_config_dict

    exported = backup_config_dict(Config())
    for name in SWITCHES:
        assert name in exported, (
            f"{name} would come back as its default (on) after a restore, silently "
            "sending a type Its owner had switched off"
        )


def test_settings_api_reads_and_writes_the_switches(monkeypatch):
    monkeypatch.setattr(
        settings_api, "_persist_preserving_at_rest_private_key", lambda cfg: None
    )
    cfg = Config()
    data, status = settings_api.get_settings(cfg)
    assert status == 200
    for name in SWITCHES:
        assert data["settings"][name] is True

    result, status = settings_api.update_settings(
        json.dumps({"sync_text": False, "sync_file": False}).encode("utf-8"), cfg, None
    )
    assert status == 200 and result["ok"] is True
    assert cfg.sync_text is False
    assert cfg.sync_file is False
    assert cfg.sync_image is True

    data, _status = settings_api.get_settings(cfg)
    assert data["settings"]["sync_text"] is False
    assert data["settings"]["sync_file"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"sync_text": "false"},
        {"sync_image": None},
        {"sync_file": 1},
        {"sync_url": []},
    ],
)
def test_settings_api_refuses_a_switch_that_is_not_a_real_boolean(monkeypatch, payload):
    monkeypatch.setattr(
        settings_api, "_persist_preserving_at_rest_private_key", lambda cfg: None
    )
    cfg = Config()

    result, status = settings_api.update_settings(
        json.dumps(payload).encode("utf-8"), cfg, None
    )

    assert status == 400 and result["ok"] is False
    for name in SWITCHES:
        assert getattr(cfg, name) is True


# ── the send-side filter ──────────────────────────────────────────────


def test_all_switches_on_send_everything_unchanged():
    """The regression that protects every machine that never touches a switch."""
    content = _content(
        TEXT=b"hello",
        HTML=b"<b>hello</b>",
        RTF=b"{\\rtf1 hello}",
        URL=b"https://example.com/",
        IMAGE_PNG=b"\x89PNG\r\n\x1a\n",
        IMAGE_EMF=b"emf",
        FILE=b"/tmp/a.txt",
    )
    cfg = Config()

    _manager, history, sent = _capture(cfg, content)

    assert len(history.rows) == 1
    assert len(sent) == 1
    assert sent[0].content.types == content.types, (
        "with every switch on, the outbound message must be the capture itself"
    )


@pytest.mark.parametrize(
    ("switch", "target", "present"),
    [
        ("sync_text", ContentType.TEXT, (ContentType.HTML,)),
        ("sync_url", ContentType.URL, (ContentType.TEXT,)),
        ("sync_html", ContentType.HTML, (ContentType.TEXT,)),
        ("sync_rtf", ContentType.RTF, (ContentType.TEXT,)),
        ("sync_image", ContentType.IMAGE_PNG, (ContentType.TEXT,)),
        ("sync_image", ContentType.IMAGE_EMF, (ContentType.TEXT,)),
        ("sync_file", ContentType.FILE, (ContentType.TEXT,)),
    ],
)
def test_a_switch_drops_its_own_type_from_the_outgoing_clip(switch, target, present):
    cfg = Config()
    types = {target: b"payload"}
    for fmt in present:
        types[fmt] = b"payload"
    content = ClipboardContent(types=types)
    setattr(cfg, switch, False)

    _manager, history, sent = _capture(cfg, content)

    assert len(history.rows) == 1, "the local history still records the whole capture"
    assert history.rows[0].types == content.types
    assert len(sent) == 1
    assert target not in sent[0].content.types
    for fmt in present:
        assert fmt in sent[0].content.types


def test_switching_off_sync_file_stops_the_offer():
    """No FILE payload reaches the encoder, so no offer can be built."""
    cfg = Config()
    cfg.sync_file = False
    content = _content(FILE=b"/tmp/a.txt")

    _manager, history, sent = _capture(cfg, content)

    assert len(history.rows) == 1
    assert sent == [], "a file whose switch is off must not be offered to peers"


def test_an_image_switch_off_leaves_the_text_half_of_a_rich_copy():
    cfg = Config()
    cfg.sync_image = False
    content = _content(TEXT=b"caption", IMAGE_PNG=b"\x89PNG\r\n\x1a\n")

    _manager, _history, sent = _capture(cfg, content)

    assert len(sent) == 1
    assert sent[0].content.types == {ContentType.TEXT: b"caption"}


def test_every_switch_off_records_the_clip_but_sends_nothing():
    cfg = Config()
    for name in SWITCHES:
        setattr(cfg, name, False)
    content = _content(TEXT=b"hello", HTML=b"<b>hello</b>", IMAGE_PNG=b"png")

    _manager, history, sent = _capture(cfg, content)

    assert len(history.rows) == 1
    assert history.rows[0].types == content.types
    assert sent == []


def test_the_switches_are_read_live_not_once_at_startup():
    cfg = Config()
    manager = SyncManager(
        "device",
        "Device",
        reader=_Reader(_content(TEXT=b"first", IMAGE_PNG=b"png-1")),
        writer=_Writer(),
        monitor=_Monitor(),
        history=_History(),
        sync_debounce=0.0,
        retry_enabled=False,
    )
    manager.set_config(cfg)
    sent: list = []
    manager.on_send = sent.append

    manager._do_read_and_send()
    assert ContentType.IMAGE_PNG in sent[-1].content.types

    cfg.sync_image = False
    manager._reader.content = _content(TEXT=b"second", IMAGE_PNG=b"png-2")
    manager._do_read_and_send()

    assert len(sent) == 2
    assert sent[-1].content.types == {ContentType.TEXT: b"second"}


def test_receiving_is_untouched_by_the_switches():
    """A switch is about what this machine *sends*; inbound content never
    consults it.  The remote path writes what it was sent exactly as before."""
    cfg = Config()
    for name in SWITCHES:
        setattr(cfg, name, False)
    writer_calls: list = []

    class _RecordingWriter:
        def write(self, content):
            writer_calls.append(content)
            return True

    manager = SyncManager(
        "device",
        "Device",
        reader=_Reader(ClipboardContent()),
        writer=_RecordingWriter(),
        monitor=_Monitor(),
        history=_History(),
        sync_debounce=0.0,
        retry_enabled=False,
    )
    manager.set_config(cfg)
    from internal.clipboard.format import SyncMessage

    incoming = SyncMessage(
        content=ClipboardContent(types={ContentType.TEXT: b"from peer"}),
        msg_id="m-1",
        source_device="peer",
    )

    outcome = manager.handle_remote_message_ex(incoming)

    assert outcome["accepted"] is True and outcome["clipboard"] is True
    assert writer_calls and writer_calls[0].types == {ContentType.TEXT: b"from peer"}
