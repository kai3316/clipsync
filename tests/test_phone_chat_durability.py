"""The phone conversation is on disk, or it is not there at all.

The failure this holds shut: ``append`` incremented the id and appended to the
list *before* writing, and ``messages(reader=...)`` flipped the read flags
before writing.  A failed write therefore left memory one message ahead of the
disk — the sender was told the send failed, retried, and the retry became the
second copy; the read badges showed "read" until the next restart put them all
back.  The file was also a fixed ``.tmp`` next to a load path that replaced an
unreadable conversation with an empty one, so the first successful append after
a corrupt read destroyed what was left of it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import internal.application.phone_chat as phone_chat
from internal.application.phone_chat import MAX_MESSAGES, PhoneChatStore


@pytest.fixture
def store(tmp_path):
    return PhoneChatStore(tmp_path / "phone_chat.json")


def _on_disk(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_failed_append_leaves_the_conversation_where_it_was(store, monkeypatch, tmp_path):
    store.append("phone", "first")
    path = tmp_path / "phone_chat.json"
    before = _on_disk(path)

    def refuse(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(phone_chat.os, "replace", refuse)
    with pytest.raises(OSError):
        store.append("phone", "the message that never lands")

    # Memory did not move: no second message, and the id was not spent.
    assert store.messages()["messages"] == [dict(row) for row in before["messages"]]
    assert _on_disk(path) == before

    # With the disk back, the retry is the *first* copy — id 2, not 3.
    monkeypatch.undo()
    sent = store.append("phone", "retry")
    assert sent["id"] == before["next_id"]
    assert [row["text"] for row in _on_disk(path)["messages"]] == ["first", "retry"]


def test_a_failed_read_mark_does_not_report_anything_as_read(store, monkeypatch):
    store.append("phone", "unread")

    def refuse(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(phone_chat.os, "replace", refuse)
    with pytest.raises(OSError):
        store.messages(reader="desktop")

    monkeypatch.undo()
    # Nothing was persisted, so nothing may read as read — the badge the user
    # is looking at has to match what the next launch will show.
    assert [row["read"] for row in store.messages()["messages"]] == [False]


def test_the_write_leaves_no_scratch_file_behind(store, tmp_path):
    store.append("phone", "hello")
    assert (tmp_path / "phone_chat.json").exists()
    assert not (tmp_path / "phone_chat.json.tmp").exists()


def test_an_unreadable_conversation_is_kept_aside_not_overwritten(tmp_path):
    path = tmp_path / "phone_chat.json"
    path.write_text("{ this is not json", encoding="utf-8")

    store = PhoneChatStore(path)
    assert store.messages()["messages"] == []

    quarantined = list(tmp_path.glob("phone_chat.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{ this is not json"

    # The next append starts a new file; the old bytes are still in the copy.
    store.append("phone", "after the corruption")
    assert [row["text"] for row in _on_disk(path)["messages"]] == ["after the corruption"]
    assert quarantined[0].read_text(encoding="utf-8") == "{ this is not json"


def test_a_file_without_a_message_list_counts_as_unreadable(tmp_path):
    """A JSON document that is not this file's shape is not "an empty chat"."""
    path = tmp_path / "phone_chat.json"
    path.write_text(json.dumps({"messages": "nope"}), encoding="utf-8")

    store = PhoneChatStore(path)
    assert store.messages() == {"messages": [], "last_id": 0}
    assert list(tmp_path.glob("phone_chat.json.corrupt-*"))


def test_the_cap_trims_on_append_and_the_trim_is_persisted(tmp_path):
    store = PhoneChatStore(tmp_path / "phone_chat.json")
    for index in range(MAX_MESSAGES + 5):
        store.append("phone", f"message {index}")
    rows = _on_disk(tmp_path / "phone_chat.json")["messages"]
    assert len(rows) == MAX_MESSAGES
    assert rows[-1]["text"] == f"message {MAX_MESSAGES + 4}"
    # The live state and the file agree — the trim is not memory-only.
    assert store.messages()["messages"] == rows


def test_reading_marks_only_the_other_side(store):
    store.append("phone", "from the phone")
    store.append("desktop", "from the desktop")
    store.messages(reader="desktop")
    rows = {row["text"]: row["read"] for row in store.messages()["messages"]}
    assert rows["from the phone"] is True
    # The desktop's own message is not "read by the desktop".
    assert rows["from the desktop"] is False


def test_the_replace_target_is_the_real_file(store, tmp_path):
    """Both the write and the clear name the same scratch path."""
    store.append("phone", "hello")
    assert (tmp_path / "phone_chat.json").exists()
    store.clear()
    assert not (tmp_path / "phone_chat.json").exists()
    assert not (tmp_path / "phone_chat.json.tmp").exists()
    assert os.path.exists(tmp_path)  # the directory is left alone
