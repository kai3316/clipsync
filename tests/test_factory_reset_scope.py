"""What a factory reset actually removes, and what it says when it cannot.

The failure this holds shut is silent by construction.  ``reset_data_dir``
unlinked a fixed list of files, logged whatever it could not unlink, and the
caller answered ``ok: True`` — so a reset could leave ``phone_chat.json`` (the
phone's conversation), ``relay_pending.json`` (frames that are republished to
the peers on the next start) and ``aiconfig_trash/`` (deleted config files,
usually holding the API keys that made them worth deleting) on disk while the
UI said the data was gone.

Two kinds of test here: the registry check, which walks every ``config_dir() /
"..."`` in the source so the *next* file cannot be forgotten, and the behaviour
checks for the in-memory copies that made deleting the file insufficient.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from internal.application.phone_chat import PhoneChatStore, reset_phone_chat_store
from internal.config.config import Config, save
from internal.data.reset import (
    DATA_DIRS,
    DATA_FILES,
    KEPT_DIRS,
    MARKERS,
    reset_data_dir,
    retry_failed,
)
from internal.infrastructure.persistence.relay_delivery import RelayDeliveryQueue
from internal.infrastructure.runtime.relay_delivery import RelayDelivery

ROOT = Path(__file__).resolve().parents[1]

# ``config_dir() / "name"`` and ``_config_dir() / "name"`` are how every
# user-data path in this codebase is spelled; a new one written that way is
# picked up here without anyone remembering to add it.
_PATH_LITERAL = re.compile(r"_?config_dir\(\)\s*/\s*\"([^\"]+)\"")


def _declared() -> set[str]:
    return set(DATA_FILES) | set(DATA_DIRS) | set(KEPT_DIRS) | set(MARKERS)


def test_every_declared_path_is_scanned_from_real_code():
    """Guard the registry check itself: an empty scan would always pass."""
    found = set()
    for path in (ROOT / "internal").rglob("*.py"):
        found.update(_PATH_LITERAL.findall(path.read_text(encoding="utf-8")))
    assert {"config.json", "phone_chat.json", "relay_pending.json"} <= found


def test_every_user_data_path_is_in_scope_or_named_as_kept():
    missing = []
    for path in (ROOT / "internal").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for name in _PATH_LITERAL.findall(source):
            if name not in _declared():
                missing.append(f"{path.relative_to(ROOT)}: {name}")
    assert not missing, (
        "these files live in the data directory and the factory reset does not "
        f"mention them, so a reset would leave them behind: {sorted(set(missing))}"
    )


def test_the_kept_directory_carries_a_reason():
    assert KEPT_DIRS, "a kept directory needs a stated reason"
    assert all(reason.strip() for reason in KEPT_DIRS.values())


def test_the_phone_conversation_is_removed_and_its_memory_emptied(tmp_path, monkeypatch):
    """Deleting the file is not enough: the store writes its memory back."""
    import internal.application.phone_chat as phone_chat

    store = PhoneChatStore(tmp_path / "phone_chat.json")
    store.append("phone", "the message the reset must destroy")
    assert (tmp_path / "phone_chat.json").exists()

    monkeypatch.setattr(phone_chat, "_store", store, raising=False)
    reset_phone_chat_store()

    assert not (tmp_path / "phone_chat.json").exists()
    assert store.messages()["messages"] == []
    # A poll that arrives after the unlink — the window the old code left open —
    # must not carry the deleted conversation back to disk.
    store.append("phone", "after the reset")
    on_disk = json.loads((tmp_path / "phone_chat.json").read_text(encoding="utf-8"))
    assert [message["text"] for message in on_disk["messages"]] == ["after the reset"]


def test_the_relay_queue_cannot_republish_after_a_reset(tmp_path):
    """A queued frame is republished by the retry sweep, so memory must go too."""
    queue = RelayDeliveryQueue(tmp_path / "relay_pending.json")
    queue.enqueue("peer-1", "m1", b"frame", kind="clipboard", preview="old secret")
    published: list[bytes] = []
    delivery = RelayDelivery(
        queue,
        lambda frame, peer_id: published.append(frame) or True,
        lambda *_args: None,
    )
    assert queue.counts() == {"peer-1": 1}
    assert delivery.status()["pending"] == 1

    delivery.clear_all()

    assert queue.counts() == {}
    assert delivery.status() == {"pending": 0, "items": []}
    assert published == []
    assert not (tmp_path / "relay_pending.json").exists()
    # And it stays gone: persisting an empty queue must not recreate the file.
    queue.persist()
    assert not (tmp_path / "relay_pending.json").exists()


def test_a_surviving_conversation_is_reported_not_swallowed(tmp_path, monkeypatch):
    (tmp_path / "phone_chat.json").write_text("{}", encoding="utf-8")
    real_unlink = Path.unlink

    def refuse(self, *args, **kwargs):
        if self.name == "phone_chat.json":
            raise PermissionError("in use")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    report = reset_data_dir(tmp_path)

    assert report.ok is False
    assert report.failures == ["phone_chat.json"]
    payload = report.as_dict()
    assert payload["ok"] is False
    assert payload["failed"] == [{"name": "phone_chat.json", "reason": "in use"}]


def test_a_retry_removes_what_the_first_pass_could_not(tmp_path, monkeypatch):
    """Windows holds a released handle for a moment; one retry is the difference."""
    (tmp_path / "clipboard_history.db").write_text("db", encoding="utf-8")
    real_unlink = Path.unlink
    state = {"refused": False}

    def refuse_once(self, *args, **kwargs):
        if self.name == "clipboard_history.db" and not state["refused"]:
            state["refused"] = True
            raise PermissionError("still open")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_once)
    first = reset_data_dir(tmp_path)
    assert first.ok is False
    second = retry_failed(first, tmp_path)
    assert second.ok is True
    assert second.failed == ()
    assert "clipboard_history.db" in second.deleted


def test_the_log_file_surviving_is_a_warning_not_a_failed_reset(tmp_path, monkeypatch):
    """A log tail is not user content — but it is still reported."""
    (tmp_path / "clipsync.log").write_text("log", encoding="utf-8")
    real_unlink = Path.unlink

    def refuse(self, *args, **kwargs):
        if self.name == "clipsync.log":
            raise PermissionError("held by the handler")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    report = reset_data_dir(tmp_path)
    assert report.ok is True
    assert report.failures == ["clipsync.log"]


def test_a_custom_data_dir_loses_only_the_app_owned_subdirectory(tmp_path):
    custom = tmp_path / "custom"
    (custom / "aiconfig_trash").mkdir(parents=True)
    (custom / "aiconfig_trash" / "deleted.json").write_text("{}", encoding="utf-8")
    (custom / "keep-me.txt").write_text("someone else's file", encoding="utf-8")
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()

    report = reset_data_dir(config_dir, data_dir=custom)

    assert report.ok is True
    assert not (custom / "aiconfig_trash").exists()
    # The custom directory is the user's; only the app's own subdirectory goes.
    assert (custom / "keep-me.txt").read_text(encoding="utf-8") == "someone else's file"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    save(Config(encryption_enabled=False))
    from internal.application.bootstrap import SidecarApplication

    application = SidecarApplication()
    application.lifecycle.start()
    yield application
    application.lifecycle.stop()


def test_the_reset_response_names_everything_it_removed(app, tmp_path):
    """End to end: the files the review found surviving are gone, and say so."""
    from internal.adapters.sidecar.rpc import Dispatcher

    (tmp_path / "phone_chat.json").write_text(
        json.dumps({"next_id": 2, "messages": [{"id": 1, "sender": "phone", "text": "hi"}]}),
        encoding="utf-8",
    )
    (tmp_path / "relay_pending.json").write_text(
        json.dumps({"version": 1, "peers": {"peer-1": {"m1": {"msg_id": "m1"}}}}),
        encoding="utf-8",
    )
    (tmp_path / "aiconfig_trash").mkdir(exist_ok=True)
    (tmp_path / "aiconfig_trash" / "keys.json").write_text("{}", encoding="utf-8")
    (tmp_path / "backups").mkdir(exist_ok=True)
    (tmp_path / "backups" / "archive.zip").write_text("zip", encoding="utf-8")

    result = Dispatcher(app).call("app.factory_reset", {})

    assert result["ok"] is True
    assert result["factory_reset"] is True
    assert result["failed"] == []
    for name in ("phone_chat.json", "relay_pending.json"):
        assert not (tmp_path / name).exists(), f"{name} survived the reset"
    assert not (tmp_path / "aiconfig_trash").exists()
    # Explicitly kept: the archive the reset dialog offers to make first.
    assert (tmp_path / "backups" / "archive.zip").exists()
    assert result["kept"] == list(KEPT_DIRS)
