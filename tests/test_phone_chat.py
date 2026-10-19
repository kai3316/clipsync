"""The phone↔computer conversation store."""

from internal.application.phone_chat import PhoneChatStore


def test_messages_roundtrip_and_both_sides_mark_read(tmp_path):
    store = PhoneChatStore(tmp_path / "phone_chat.json")
    phone = store.append("phone", "hello")
    desktop = store.append("desktop", "hi back")
    assert phone["id"] == 1 and desktop["id"] == 2

    # Reading as the phone marks desktop messages read, not its own.
    data = store.messages(after_id=0, reader="phone")
    assert [m["text"] for m in data["messages"]] == ["hello", "hi back"]
    assert data["messages"][1]["read"] is True
    assert data["messages"][0]["read"] is False

    # A fresh store reads the same file (durability).
    again = PhoneChatStore(tmp_path / "phone_chat.json")
    assert [m["text"] for m in again.messages()["messages"]] == ["hello", "hi back"]


def test_empty_text_is_refused_and_after_id_filters(tmp_path):
    store = PhoneChatStore(tmp_path / "phone_chat.json")
    store.append("phone", "one")
    two = store.append("desktop", "two")
    try:
        store.append("phone", "   ")
    except ValueError:
        pass
    else:
        raise AssertionError("blank text should be refused")
    tail = store.messages(after_id=two["id"])
    assert tail["messages"] == []
    assert tail["last_id"] == 2


def test_a_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "phone_chat.json"
    path.write_text("{not json", encoding="utf-8")
    store = PhoneChatStore(path)
    assert store.messages()["messages"] == []
    store.append("phone", "recovered")
    assert store.messages()["messages"][0]["text"] == "recovered"
