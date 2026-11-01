"""File-transfer size limits (1.3.1) and the history row a paste rewrites.

Two layers are pinned here: the protocol ceiling, which is *derived* from the
chunk count and the smallest chunk this build advertises, and the user policy
ceiling (`max_file_mb`) applied on top for the kinds users move.  Also the two
history-store operations the on-demand paste runs — read the offer a remote-file
row holds, and rewrite that row into a local file row in place — because those
are what make a second paste a plain local copy.
"""

from __future__ import annotations

import json

import pytest

import internal.config.config as config_module
from internal.clipboard import file_ref
from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.config.config import FIELD_RULES, Config
from internal.protocol.codec import decode_message
from internal.sync.file_transfer import (
    MAX_TOTAL_CHUNKS,
    FileTooLargeError,
    FileTransferManager,
    protocol_max_file_size,
)
from internal.sync.nearby_chat import (
    CHAT_MAX_FILE_SIZE,
    ChatManager,
)
from internal.transport.relay import frame_limit_for
from internal.web.api import settings as settings_api

# ── config surface ────────────────────────────────────────────────────


def test_the_file_limit_defaults_are_the_documented_ones():
    cfg = Config()
    assert cfg.max_file_mb == 2048
    assert cfg.confirm_large_file_mb == 512
    assert cfg.relay_paste_max_mb == 50
    # 0 is a real value for each, so the schema must say where it stops.
    assert FIELD_RULES["max_file_mb"] == ("int", 0, 65536)
    assert FIELD_RULES["confirm_large_file_mb"] == ("int", 0, 65536)
    assert FIELD_RULES["relay_paste_max_mb"] == ("int", 0, 65536)


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    monkeypatch.setattr(config_module, "_config_dir", lambda: config_dir)
    monkeypatch.setattr(config_module, "_config_path", lambda: config_path)
    return config_path


def test_the_file_limits_round_trip_and_back_up(isolated_config):
    config_path = isolated_config
    cfg = Config()
    cfg.max_file_mb = 0
    cfg.confirm_large_file_mb = 100
    cfg.relay_paste_max_mb = 10
    config_module.save(cfg)

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert (raw["max_file_mb"], raw["confirm_large_file_mb"], raw["relay_paste_max_mb"]) == (
        0,
        100,
        10,
    )
    loaded = config_module.load()
    assert loaded.max_file_mb == 0
    assert loaded.confirm_large_file_mb == 100
    assert loaded.relay_paste_max_mb == 10

    from internal.data.backup import backup_config_dict

    exported = backup_config_dict(Config())
    for name in ("max_file_mb", "confirm_large_file_mb", "relay_paste_max_mb"):
        assert name in exported, f"{name} would come back as its default after a restore"


def test_settings_api_exposes_and_writes_the_file_limits(monkeypatch):
    monkeypatch.setattr(
        settings_api, "_persist_preserving_at_rest_private_key", lambda cfg: None
    )
    cfg = Config()
    data, status = settings_api.get_settings(cfg)
    assert status == 200
    assert data["settings"]["max_file_mb"] == 2048
    assert data["settings"]["confirm_large_file_mb"] == 512
    assert data["settings"]["relay_paste_max_mb"] == 50

    result, status = settings_api.update_settings(
        json.dumps(
            {"max_file_mb": 0, "confirm_large_file_mb": 0, "relay_paste_max_mb": 100}
        ).encode("utf-8"),
        cfg,
        None,
    )
    assert status == 200 and result["ok"] is True
    assert (cfg.max_file_mb, cfg.confirm_large_file_mb, cfg.relay_paste_max_mb) == (0, 0, 100)


# ── the derived protocol ceiling ──────────────────────────────────────


def test_the_protocol_ceiling_is_derived_from_the_chunk_count():
    """Not a second magic number: the smallest chunk this build advertises times
    the most chunks a receiver will allocate bookkeeping for."""
    smallest_chunk = min(
        ChatManager.relay_chunk_for(32 * 1024),
        frame_limit_for(32 * 1024) - 46,  # BINARY_HEADER_SIZE
    )
    assert protocol_max_file_size() == MAX_TOTAL_CHUNKS * smallest_chunk
    # It is above the old hard-written 2 GiB, which is what lets max_file_mb=0
    # mean "as large as the protocol carries" rather than "the old number".
    assert protocol_max_file_size() > 2 * 1024**3
    # A LAN transfer still cuts at this build's own chunk size, so the number of
    # chunks for any accepted file stays inside the receiver's bound.
    assert protocol_max_file_size() // ChatManager.CHUNK_SIZE <= MAX_TOTAL_CHUNKS


def test_the_effective_ceiling_is_the_policy_inside_the_protocol(tmp_path):
    manager = FileTransferManager("device", output_dir=str(tmp_path))
    assert manager.max_file_bytes() == protocol_max_file_size()  # 0 = protocol
    manager.set_file_size_policy(10)
    assert manager.max_file_bytes() == 10 * 1024 * 1024
    manager.set_file_size_policy(1_000_000)  # past the protocol: clamped
    assert manager.max_file_bytes() == protocol_max_file_size()
    manager.set_file_size_policy(None)
    assert manager.max_file_bytes() == protocol_max_file_size()


def test_the_chat_attachment_ceiling_is_its_own_constant():
    """The file-transfer policy must not silently move what a chat can carry."""
    assert CHAT_MAX_FILE_SIZE == 2 * 1024**3


# ── the manager's two directions ──────────────────────────────────────


def _request_frame(transfer_id, *, name="a.bin", size=10, kind="file", entry="", chunk_size=0):
    payload = {
        "msg_type": "file_request",
        "transfer_id": transfer_id,
        "file_name": name,
        "file_size": size,
        "kind": kind,
    }
    if entry:
        payload["entry"] = entry
    if chunk_size:
        payload["chunk_size"] = chunk_size
    return payload


def _sent_payloads(sent: list[bytes]) -> list[dict]:
    return [getattr(decode_message(raw), "_raw_payload", {}) or {} for raw in sent]


def test_a_send_over_the_user_policy_is_refused_before_the_offer(tmp_path):
    manager = FileTransferManager("device", output_dir=str(tmp_path))
    manager.set_file_size_policy(1)
    path = tmp_path / "over.bin"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    sent: list[bytes] = []

    with pytest.raises(FileTooLargeError, match="file size limit"):
        manager.send_file(str(path), sent.append)
    assert sent == [], "an offer must not reach the wire for a file that is refused"


def test_a_send_within_the_policy_goes_out(tmp_path):
    manager = FileTransferManager("device", output_dir=str(tmp_path))
    manager.set_file_size_policy(1)
    path = tmp_path / "ok.bin"
    path.write_bytes(b"x" * 1024)
    sent: list[bytes] = []

    transfer_id = manager.send_file(str(path), sent.append)

    assert transfer_id
    assert _sent_payloads(sent)[0]["file_size"] == 1024


def test_the_receiver_refuses_a_file_over_its_policy(tmp_path):
    manager = FileTransferManager("device", output_dir=str(tmp_path))
    manager.set_file_size_policy(1)
    sent: list[bytes] = []

    manager.handle_message(
        "file_request",
        _request_frame("t1", size=2 * 1024 * 1024),
        sent.append,
        "peer",
    )

    payload = _sent_payloads(sent)[0]
    assert payload["msg_type"] == "file_reject"
    assert payload["reason"] == "too_large"
    assert "t1" not in manager._transfers


def test_the_receiver_refuses_a_file_past_the_protocol_not_only_the_policy(tmp_path):
    manager = FileTransferManager("device", output_dir=str(tmp_path))
    sent: list[bytes] = []

    manager.handle_message(
        "file_request",
        _request_frame("t1", size=protocol_max_file_size() + 1),
        sent.append,
        "peer",
    )

    assert _sent_payloads(sent)[0]["reason"] == "too_large"


def test_background_kinds_are_bounded_by_the_protocol_not_the_policy(tmp_path):
    """A small max_file_mb must not stop the app updating itself."""
    manager = FileTransferManager("device", output_dir=str(tmp_path))
    manager.set_file_size_policy(1)
    manager.set_file_open_to_all(True)
    sent: list[bytes] = []

    manager.handle_message(
        "file_request",
        _request_frame("t-upd", name="installer.exe", size=2 * 1024 * 1024, kind="update"),
        sent.append,
        "peer",
    )

    assert "t-upd" in manager._transfers, "an update blob over the file policy was refused"


# ── the history operations a paste uses ───────────────────────────────


def _remote_file_content(paths, remote_entry="h-1", device="remote"):
    items = [file_ref.describe(str(path)) for path in paths]
    return ClipboardContent(
        types={
            ContentType.FILE_REMOTE: file_ref.offer(
                remote_entry, [item for item in items if item]
            )
        },
        source_device=device,
    )


@pytest.fixture
def store(tmp_path):
    database = ClipboardHistoryDB(storage_path=str(tmp_path / "history.db"))
    yield database
    database.close()


def test_remote_file_offer_reads_back_the_pointer(store, tmp_path):
    first = tmp_path / "a.txt"
    first.write_text("hello", encoding="utf-8")
    entry = store.add(_remote_file_content([first], remote_entry="h-42"))

    offer = store.remote_file_offer(str(entry))

    assert offer is not None
    assert offer["entry_id"] == str(entry)
    assert offer["source_device"] == "remote"
    assert offer["offer_entry"] == "h-42"
    assert offer["files"][0]["name"] == "a.txt"
    assert offer["total"] == 1
    # A row that is not a remote-file offer answers None rather than guessing.
    plain = store.add(ClipboardContent(types={ContentType.TEXT: b"hi"}))
    assert store.remote_file_offer(str(plain)) is None


def test_the_row_is_rewritten_into_a_local_file_in_place(store, tmp_path):
    source = tmp_path / "report.txt"
    source.write_bytes(b"hello")
    entry = store.add(
        ClipboardContent(
            types={
                ContentType.FILE_REMOTE: file_ref.offer(
                    "h-1", [file_ref.describe(str(source))]
                ),
                ContentType.TEXT: b"report.txt",
            },
            source_device="remote",
        )
    )
    store.batch_set_pinned([entry], True)
    store.increment_paste(entry)
    store.increment_paste(entry)
    local = tmp_path / "downloads"
    local.mkdir()
    saved = local / "report.txt"
    saved.write_bytes(b"hello")

    updated = store.replace_file_remote_with_local(str(entry), [str(saved)])

    assert updated is not None
    assert updated["entry_id"] == entry, "the id a paste/peer refers to must not change"
    assert updated["source_device"] == "remote", "the row still says where the file came from"
    assert updated["pinned"] is True
    assert updated["paste_count"] == 2
    assert updated["content_type"] == "FILE"
    types = updated["types"]
    assert "FILE_REMOTE" not in types
    assert "FILE" in store.payloads_for_write(updated)
    # The name it carried as TEXT beside the offer survives.
    assert "TEXT" in types
    # The preview now reads as a local file row.
    assert "report.txt" in updated["text_preview"]
    # Not a remote row any more: the paste flow has nothing left to ask for.
    assert store.remote_file_offer(str(entry)) is None


def test_the_rewritten_row_survives_a_reload(store, tmp_path):
    source = tmp_path / "doc.txt"
    source.write_bytes(b"x")
    entry = store.add(_remote_file_content([source]))
    saved = tmp_path / "doc.txt"
    saved.write_bytes(b"x")
    store.replace_file_remote_with_local(str(entry), [str(saved)])
    database_path = store._db_path
    store.close()

    reopened = ClipboardHistoryDB(storage_path=str(database_path))
    try:
        _index, row = reopened.find_by_id(str(entry))
        assert row is not None
        assert row["content_type"] == "FILE"
        assert row["source_device"] == "remote"
        from internal.clipboard.dedup import labels_to_types

        formats = labels_to_types(row["types"])
        assert ContentType.FILE in formats
        assert ContentType.FILE_REMOTE not in formats
    finally:
        reopened.close()


def test_rewriting_a_row_that_is_not_a_remote_file_does_nothing(store):
    plain = store.add(ClipboardContent(types={ContentType.TEXT: b"hi"}))
    assert store.replace_file_remote_with_local(str(plain), ["/tmp/x"]) is None
    assert store.replace_file_remote_with_local("nobody", ["/tmp/x"]) is None
    assert store.replace_file_remote_with_local(str(plain), []) is None


def test_a_rewritten_row_is_servable_only_through_its_own_local_paths(store, tmp_path):
    """The row reports where it came from, so this machine no longer serves it."""
    saved = tmp_path / "local.bin"
    saved.write_bytes(b"x")
    entry = store.add(_remote_file_content([saved]))
    store.replace_file_remote_with_local(str(entry), [str(saved)])

    paths, reason = store.file_entry_paths(str(entry))

    assert paths == [] and reason == "not_local"
