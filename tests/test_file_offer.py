"""A file that lives on another device, from the offer to the bytes.

The feature's whole safety argument rests on one property: **a path never
crosses**.  What a peer receives is a name, a size and the id of the entry that
published it, and a request can therefore only name an *entry* — the machine
that holds the file decides which paths that entry reaches, out of its own
history.  Most of what is asserted below is that property, from whichever
direction it can be observed: the payload a copy produces, the frame the
encoder emits, what a request is allowed to resolve to, and who may skip the
consent prompt.

The rest is the set of consumers that had to learn the new type.  Each one is a
place where a row could be stored and then silently misplaced — a clip with no
best format is dropped by the history store, a preview with no branch reads as
empty, a filter that does not inspect an offer's names lets them leave
unredacted — so each has a test rather than a comment.
"""

from internal.clipboard import file_ref
from internal.clipboard.clipboard import strip_rich_formats
from internal.clipboard.filter import ContentFilter
from internal.clipboard.format import (
    HISTORY_ONLY_TYPES,
    ClipboardContent,
    ContentType,
    SyncMessage,
    split_paths,
)
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.protocol.codec import (
    CLIP_FILE_MSG_TYPES,
    RELAY_MSG_TYPES,
    UNPAIRED_GATE_MSG_TYPES,
    decode_message,
    encode_message,
    has_syncable_types,
)
from internal.sync.file_transfer import FileTransferManager

# ═════════════════════════════════════════════════════════════════════════
# The offer itself
# ═════════════════════════════════════════════════════════════════════════


class TestOfferShape:
    def test_a_round_trip_returns_the_entry_the_names_and_the_sizes(self, tmp_path):
        path = tmp_path / "report.docx"
        path.write_bytes(b"x" * 4096)

        payload = file_ref.offer("h-7", [file_ref.describe(str(path))], total=1)
        parsed = file_ref.parse(payload)

        assert parsed["entry"] == "h-7"
        assert parsed["files"] == [{"name": "report.docx", "size": 4096, "kind": "file"}]
        assert parsed["total"] == 1

    def test_the_payload_carries_no_path(self, tmp_path):
        """The property the whole design rests on, asserted directly.

        Not "the path is hidden" — the path is *not there*.  If this ever fails,
        the offer has become a directory listing of the publishing machine.
        """
        path = tmp_path / "secret" / "budget.xlsx"
        path.parent.mkdir()
        path.write_bytes(b"x")

        payload = file_ref.offer("h-1", [file_ref.describe(str(path))], total=1)

        assert str(tmp_path).encode() not in payload
        assert str(path).encode() not in payload
        assert b"budget.xlsx" in payload

    def test_a_missing_path_is_left_out_rather_than_advertised(self, tmp_path):
        assert file_ref.describe(str(tmp_path / "gone.txt")) is None

    def test_a_folder_is_described_with_no_size(self, tmp_path):
        folder = tmp_path / "quarterly"
        folder.mkdir()
        (folder / "a.txt").write_bytes(b"12345")

        described = file_ref.describe(str(folder))

        assert described == {"name": "quarterly", "size": 0, "kind": "dir"}

    def test_a_trailing_separator_still_yields_a_name(self, tmp_path):
        folder = tmp_path / "quarterly"
        folder.mkdir()
        assert file_ref.describe(str(folder) + "/")["name"] == "quarterly"

    def test_an_unknown_version_is_refused_rather_than_repaired(self):
        """The id comes back out of this structure, so a payload that is not
        exactly the shape below has no id worth trusting."""
        assert file_ref.parse(b'{"v":99,"entry":"h-1","files":[]}') is None
        assert file_ref.parse(b"not json") is None
        assert file_ref.parse(b'{"v":1,"files":[]}') is None

    def test_an_offer_with_no_files_lists_none_and_says_so(self):
        parsed = file_ref.parse(file_ref.offer("h-1", [], total=0))
        assert parsed == {"entry": "h-1", "files": [], "total": 0}

    def test_a_total_larger_than_the_list_survives_and_a_smaller_one_does_not(self):
        """``total`` is what a truncated offer shows.  A total below the list it
        arrived with is a payload contradicting itself, and the list is the half
        that can be checked."""
        items = [{"name": "a", "size": 1, "kind": "file"}, {"name": "b", "size": 2, "kind": "file"}]
        assert file_ref.parse(file_ref.offer("h", items, total=900))["total"] == 900
        assert file_ref.parse(file_ref.offer("h", items, total=1))["total"] == 2

    def test_entry_id_of_reads_only_a_known_offer(self):
        assert file_ref.entry_id_of(file_ref.offer("h-9", [], total=0)) == "h-9"
        assert file_ref.entry_id_of(b"[]") == ""


class TestNamesAreNormalised:
    """The file systems disagree about how to spell an accent, so the wire takes
    one spelling: composed.  Unnormalised, a name copied on a Mac arrives on
    Windows spelled in a way nothing there matches."""

    def test_a_decomposed_name_is_composed_on_the_wire(self, tmp_path):
        # "café" spelled the way HFS+/APFS hand it back: e + combining acute.
        decomposed = "cafe\u0301.txt"
        path = tmp_path / decomposed
        path.write_bytes(b"x")

        name = file_ref.describe(str(path))["name"]

        assert name == "caf\u00e9.txt"
        assert len(name) == len("caf\u00e9.txt")

    def test_a_name_that_is_already_composed_is_unchanged(self, tmp_path):
        path = tmp_path / "caf\u00e9.txt"
        path.write_bytes(b"x")
        assert file_ref.describe(str(path))["name"] == "caf\u00e9.txt"


class TestSummary:
    def test_one_file_shows_its_name_and_size(self):
        one = [{"name": "a.pdf", "size": 2048, "kind": "file"}]

        assert file_ref.summary(one) == "a.pdf · 2.0 KB"

    def test_one_folder_says_so_rather_than_showing_a_size(self):
        assert file_ref.summary([{"name": "docs", "size": 0, "kind": "dir"}]) == "docs · 文件夹"

    def test_several_files_are_named_by_the_first_and_counted(self):
        files = [
            {"name": "a.txt", "size": 100, "kind": "file"},
            {"name": "b.txt", "size": 100, "kind": "file"},
        ]

        assert file_ref.summary(files) == "a.txt 等 2 个文件 · 200 B"

    def test_a_truncated_offer_counts_what_the_sender_had_not_what_it_listed(self):
        """The row has to say how many files there are.  A drop of twenty named
        by two of them would read as a drop of two, and past the offer cap that
        count is the only thing that says the list is partial."""
        files = [
            {"name": "a.txt", "size": 10, "kind": "file"},
            {"name": "b.txt", "size": 10, "kind": "file"},
        ]
        assert "等 40 个文件" in file_ref.summary(files, total=40)

    def test_no_files_is_no_line(self):
        assert file_ref.summary([]) == ""


class TestIsLocalPathUrl:
    """A `file:` URI is an absolute path with a scheme in front of it, so
    syncing one would put a path on the wire through the type that exists to
    keep paths off it."""

    def test_the_forms_that_are_paths_are_refused(self):
        assert file_ref.is_local_path_url("file:///Users/kai/a.txt")
        assert file_ref.is_local_path_url(b"file:///C:/Users/kai/a.txt")
        assert file_ref.is_local_path_url("C:\\Users\\kai\\a.txt")
        assert file_ref.is_local_path_url("D:/work/a.txt")
        assert file_ref.is_local_path_url("\\\\nas\\share\\a.txt")

    def test_a_real_url_that_is_spelled_like_one_is_left_alone(self):
        # A protocol-relative URL is a real URL, not a UNC path, and refusing it
        # would drop a link the user meant to share.
        assert not file_ref.is_local_path_url("//cdn.example.com/app.js")
        assert not file_ref.is_local_path_url("https://example.com/a.txt")
        assert not file_ref.is_local_path_url("/home/kai/a.txt")
        assert not file_ref.is_local_path_url("")
        assert not file_ref.is_local_path_url(b"")


# ═════════════════════════════════════════════════════════════════════════
# The wire
# ═════════════════════════════════════════════════════════════════════════


class TestTheEncoderDerivesTheOffer:
    def test_a_local_file_copy_keeps_its_paths_here_and_advertises_an_offer_there(self, tmp_path):
        """One clip, two readings: the FILE payload stays on this machine (it is
        history, and the paths mean nothing anywhere else), and what goes out is
        the offer derived from it."""
        path = tmp_path / "notes.md"
        path.write_bytes(b"hello")

        content = ClipboardContent(
            types={ContentType.FILE: f"{path}".encode()},
            entry_id="h-3",
        )
        frame = decode_message(encode_message(SyncMessage(content, "m1", "dev-a")))

        raw = frame.content.types
        assert ContentType.FILE not in raw
        parsed = file_ref.parse(raw[ContentType.FILE_REMOTE])
        assert parsed["entry"] == "h-3"
        assert parsed["files"] == [{"name": "notes.md", "size": 5, "kind": "file"}]
        assert str(tmp_path).encode() not in raw[ContentType.FILE_REMOTE]

    def test_a_content_with_no_entry_id_encodes_to_no_frame(self, tmp_path):
        """The id is what a request names, so an offer without one is a row
        whose 下载 could never be answered — the encoder does not invent one, and
        with nothing left to send it emits no frame at all.

        It passes `has_syncable_types`, which is deliberately the cheap
        type-level gate; the caller is what has to notice the difference.
        """
        path = tmp_path / "notes.md"
        path.write_bytes(b"hello")

        content = ClipboardContent(types={ContentType.FILE: f"{path}".encode()}, entry_id="")

        assert has_syncable_types(content)
        assert encode_message(SyncMessage(content, "m1", "dev-a")) == b""

    def test_a_file_url_is_dropped_from_the_url_slot(self, tmp_path):
        """macOS publishes the file's own `file://` address beside the file, and
        the Linux file managers put one in `text/uri-list`.  Encoding it would
        send the path through the one type meant to keep paths off the wire."""
        path = tmp_path / "notes.md"
        path.write_bytes(b"hello")

        content = ClipboardContent(
            types={
                ContentType.FILE: f"{path}".encode(),
                ContentType.URL: f"file://{path}".encode(),
            },
            entry_id="h-3",
        )
        frame = decode_message(encode_message(SyncMessage(content, "m1", "dev-a")))

        assert ContentType.URL not in frame.content.types
        assert ContentType.FILE_REMOTE in frame.content.types

    def test_a_real_url_beside_a_file_still_travels(self, tmp_path):
        path = tmp_path / "notes.md"
        path.write_bytes(b"hello")

        content = ClipboardContent(
            types={
                ContentType.FILE: f"{path}".encode(),
                ContentType.URL: b"https://example.com/notes",
            },
            entry_id="h-3",
        )
        frame = decode_message(encode_message(SyncMessage(content, "m1", "dev-a")))

        assert frame.content.types[ContentType.URL] == b"https://example.com/notes"

    def test_a_clip_of_only_an_offer_is_still_worth_sending(self):
        """Otherwise the peer's row never appears, and the file the user copied
        is invisible on the other machine."""
        offer = file_ref.offer("h-1", [{"name": "a", "size": 1, "kind": "file"}], 1)
        content = ClipboardContent(
            types={ContentType.FILE_REMOTE: offer},
            entry_id="h-1",
        )
        assert has_syncable_types(content)

    def test_the_message_type_sets_do_not_overlap(self):
        """`clip_file` is the one pull in the protocol and is deliberately
        outside both gates: a request from an unpaired peer must be dropped at
        the transport, and a request that arrived over the relay names a device
        that could never serve it."""
        assert not CLIP_FILE_MSG_TYPES & UNPAIRED_GATE_MSG_TYPES
        assert not CLIP_FILE_MSG_TYPES & RELAY_MSG_TYPES


# ═════════════════════════════════════════════════════════════════════════
# The consumers that had to learn the type
# ═════════════════════════════════════════════════════════════════════════


class TestHistoryOnly:
    def test_an_offer_is_a_history_only_type(self):
        assert ContentType.FILE_REMOTE in HISTORY_ONLY_TYPES


class TestBestFormat:
    def test_an_offer_only_clip_has_a_best_format(self):
        """Without one the history store drops the row entirely — `add` returns
        on a None best format — so the row the user is meant to download from
        would never exist."""
        content = ClipboardContent(types={ContentType.FILE_REMOTE: b"{}"})
        assert content.best_format() == (ContentType.FILE_REMOTE, b"{}")

    def test_an_offer_beside_a_real_format_does_not_win(self):
        content = ClipboardContent(
            types={ContentType.TEXT: b"hi", ContentType.FILE_REMOTE: b"{}"}
        )
        assert content.best_format()[0] == ContentType.TEXT


class TestWithout:
    def test_a_clip_with_nothing_to_drop_comes_back_as_itself(self):
        """The receiver compares the object it wrote against the message it came
        from, and this runs on every remote clip — where the ordinary case drops
        nothing."""
        content = ClipboardContent(types={ContentType.TEXT: b"hi"})
        assert content.without(HISTORY_ONLY_TYPES) is content

    def test_an_offer_is_dropped_and_every_other_field_survives(self):
        content = ClipboardContent(
            types={ContentType.FILE_REMOTE: b"{}", ContentType.TEXT: b"hi"},
            source_device="peer-b",
            transport="lan",
            entry_id="h-1",
            image_fmt="image/bmp",
        )
        writable = content.without(HISTORY_ONLY_TYPES)

        assert ContentType.FILE_REMOTE not in writable.types
        assert writable.entry_id == "h-1"
        assert writable.image_fmt == "image/bmp"
        assert writable.source_device == "peer-b"
        assert writable.transport == "lan"


class TestStripRichFormats:
    def test_the_entry_id_survives_the_strip(self):
        """A hand-built rebuild dropped whichever field was added last, and this
        one is what a file offer names."""
        content = ClipboardContent(
            types={ContentType.HTML: b"<b>hi</b>", ContentType.TEXT: b"hi"},
            entry_id="h-1",
            image_fmt="image/bmp",
            transport="relay",
        )
        stripped = strip_rich_formats(content)

        assert ContentType.HTML not in stripped.types
        assert stripped.entry_id == "h-1"
        assert stripped.image_fmt == "image/bmp"
        assert stripped.transport == "relay"


class TestTheFilterReadsAnOffer:
    """An offer carries no payload that can be redacted — the file arrives under
    its real name whatever the offer says — so a matching name takes the whole
    offer off the wire rather than travelling unredacted beside a [FILTERED]
    text."""

    def _filter(self):
        # Redaction is on by default except for the opt-in email category, so
        # the default instance is the one a fresh install runs.
        return ContentFilter()

    def test_a_url_is_inspected_like_text(self):
        content = ClipboardContent(
            types={ContentType.URL: b"https://example.com/4111111111111111"}
        )
        assert self._filter().is_sensitive(content)

    def test_an_offer_whose_name_matches_is_sensitive(self):
        content = ClipboardContent(
            types={ContentType.FILE_REMOTE: file_ref.offer(
                "h-1", [{"name": "4111111111111111.pdf", "size": 1, "kind": "file"}], 1
            )}
        )
        assert self._filter().is_sensitive(content)

    def test_an_offer_whose_name_matches_is_dropped_whole(self):
        content = ClipboardContent(
            types={
                ContentType.FILE_REMOTE: file_ref.offer(
                    "h-1", [{"name": "4111111111111111.pdf", "size": 1, "kind": "file"}], 1
                ),
                ContentType.TEXT: b"see 4111111111111111",
            }
        )
        filtered = self._filter().filter_content(content)

        assert ContentType.FILE_REMOTE not in filtered.types
        assert b"[FILTERED]" in filtered.types[ContentType.TEXT]

    def test_a_clean_offer_survives_redaction_of_its_text(self):
        """A clip whose *text* matches still has a row, and its file is not the
        thing that matched — dropping the offer too would make a careless paste
        silently un-downloadable."""
        offer = file_ref.offer("h-1", [{"name": "notes.md", "size": 1, "kind": "file"}], 1)
        content = ClipboardContent(
            types={ContentType.FILE_REMOTE: offer, ContentType.TEXT: b"4111111111111111"}
        )
        filtered = self._filter().filter_content(content)

        assert filtered.types[ContentType.FILE_REMOTE] == offer

    def test_redaction_preserves_the_entry_id(self):
        content = ClipboardContent(
            types={ContentType.TEXT: b"4111111111111111"}, entry_id="h-1"
        )
        assert self._filter().filter_content(content).entry_id == "h-1"


class TestHistoryDbStoresAnOffer:
    def _store(self, tmp_path):
        return ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))

    def test_an_offer_only_clip_gets_a_row(self, tmp_path):
        store = self._store(tmp_path)
        payload = file_ref.offer("h-1", [{"name": "a.docx", "size": 2048, "kind": "file"}], 1)
        row_id = store.add(ClipboardContent(
            types={ContentType.FILE_REMOTE: payload}, source_device="peer-b"
        ))

        # Ids start at 0, so this is `is not None`, not truthiness.
        assert row_id is not None
        row = store.find_by_id(row_id)[1]
        assert row["text_preview"] == "a.docx · 2.0 KB"

    def test_a_locally_captured_row_resolves_to_its_paths(self, tmp_path):
        store = self._store(tmp_path)
        path = tmp_path / "a.txt"
        path.write_bytes(b"x")
        row_id = store.add(ClipboardContent(types={ContentType.FILE: f"{path}".encode()}))

        paths, reason = store.file_entry_paths(row_id)

        assert paths == [str(path)]
        assert reason == ""

    def test_a_row_from_a_peer_resolves_to_nothing(self, tmp_path):
        """The paths in a synced row name the *sending* machine's disk.  Serving
        them here would send this machine's unrelated file, or nothing."""
        store = self._store(tmp_path)
        row_id = store.add(ClipboardContent(
            types={ContentType.FILE: b"C:\\Users\\them\\a.txt"}, source_device="peer-b"
        ))

        assert store.file_entry_paths(row_id) == ([], "not_local")

    def test_a_row_whose_file_is_gone_says_so(self, tmp_path):
        store = self._store(tmp_path)
        path = tmp_path / "a.txt"
        path.write_bytes(b"x")
        row_id = store.add(ClipboardContent(types={ContentType.FILE: f"{path}".encode()}))
        path.unlink()

        assert store.file_entry_paths(row_id) == ([], "gone")

    def test_a_row_that_is_not_a_file_says_so(self, tmp_path):
        store = self._store(tmp_path)
        row_id = store.add(ClipboardContent(types={ContentType.TEXT: b"hi"}))

        assert store.file_entry_paths(row_id) == ([], "not_a_file")

    def test_an_unknown_entry_says_so(self, tmp_path):
        assert self._store(tmp_path).file_entry_paths("nobody") == ([], "not_found")

    def test_a_file_row_carries_its_distance(self, tmp_path):
        """The window decides 下载 versus 复制 on this, and a name is not enough:
        a device can be renamed on either side."""
        store = self._store(tmp_path)
        path = tmp_path / "a.txt"
        path.write_bytes(b"x")
        row_id = store.add(ClipboardContent(types={ContentType.FILE: f"{path}".encode()}))

        assert store.find_by_id(row_id)[1]["source_device"] == ""


# ═════════════════════════════════════════════════════════════════════════
# Who may skip the consent prompt
# ═════════════════════════════════════════════════════════════════════════


class _Recorder:
    """Stands in for the runtime's transport, and answers for the user."""

    def __init__(self):
        self.requested: list[str] = []
        self.accepted: list[str] = []

    def on_request(self, transfer_id, file_name, file_size, mime, send_fn):
        self.requested.append(file_name)

    def accept(self, manager, transfer_id, send_fn):
        self.accepted.append(transfer_id)
        manager.accept_transfer(transfer_id, send_fn)


def _request(manager, kind, entry="", sender="peer-b", size=4):
    manager.handle_message(
        "file_request",
        {
            "transfer_id": "t-1",
            "file_name": "a.txt",
            "file_size": size,
            "kind": kind,
            "entry": entry,
        },
        lambda data: None,
        sender,
    )


class TestTheConsentGateIsOurs:
    """`clip_file` is the one kind that skips the prompt every other inbound
    file passes through.  Read as an instruction from the sender it would be a
    hole — any paired peer could label an arbitrary push `clip_file` and have
    files written to this disk with nobody asked — so what is asserted here is
    that the label alone is not enough."""

    def test_a_clip_file_we_asked_for_is_taken_without_a_prompt(self, tmp_path):
        manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
        gate = _Recorder()
        manager.set_on_transfer_request(gate.on_request)
        manager.set_clip_file_guard(lambda sender, entry: entry == "h-1" and sender == "peer-b")

        _request(manager, "clip_file", entry="h-1")

        assert gate.requested == []

    def test_a_clip_file_we_did_not_ask_for_is_prompted_like_any_other_file(self, tmp_path):
        manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
        gate = _Recorder()
        manager.set_on_transfer_request(gate.on_request)
        manager.set_clip_file_guard(lambda sender, entry: False)

        _request(manager, "clip_file", entry="h-1")

        assert gate.requested == ["a.txt"]

    def test_a_decision_about_a_different_entry_does_not_license_this_one(self, tmp_path):
        """One ask yields many files, so the entry cannot be consumed on first
        use — but matching only the device would leave a window in which
        anything that peer pushed went through unprompted."""
        manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
        gate = _Recorder()
        manager.set_on_transfer_request(gate.on_request)
        seen: list[tuple[str, str]] = []

        def guard(sender, entry):
            seen.append((sender, entry))
            return entry == "h-1"

        manager.set_clip_file_guard(guard)
        _request(manager, "clip_file", entry="h-2")

        assert seen == [("peer-b", "h-2")]
        assert gate.requested == ["a.txt"]

    def test_with_no_guard_registered_the_label_earns_nothing(self, tmp_path):
        """Headless operation auto-accepts an ordinary file for want of a UI,
        but that fallback must not be reachable by choosing a kind."""
        manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
        _request(manager, "clip_file", entry="h-1")

        # Accepted for want of anyone to ask, not on the strength of the label:
        # with a prompt registered and no guard the same frame prompts, which is
        # the test above.
        states = {t["transfer_id"]: t["state"] for t in manager.get_transfers()}
        assert states.get("t-1") not in (None, "rejected")

    def test_an_update_blob_still_takes_no_prompt(self, tmp_path):
        """Its kind is set by the download path rather than by a peer, so it
        needs no second check — asserted so the guard above is not widened to
        it by accident."""
        manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
        gate = _Recorder()
        manager.set_on_transfer_request(gate.on_request)
        manager.set_clip_file_guard(lambda sender, entry: False)

        _request(manager, "update", entry="")

        assert gate.requested == []

    def test_an_ordinary_file_still_prompts(self, tmp_path):
        manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
        gate = _Recorder()
        manager.set_on_transfer_request(gate.on_request)

        _request(manager, "file", entry="h-1")

        assert gate.requested == ["a.txt"]


class TestTheSenderCarriesTheEntry:
    """A guard can only check the entry if the frame names one, and only for the
    transfers that need it — an ordinary send must keep its old shape."""

    def _frame(self, manager, **kwargs):
        sent: list[dict] = []
        manager._send_as_frame = lambda payload, fn=None: sent.append(payload)
        manager.send_file(__file__, lambda data: None, **kwargs)
        return sent[-1]

    def test_a_clip_file_send_names_the_entry_it_answers(self, tmp_path):
        manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
        frame = self._frame(manager, kind="clip_file", entry_id="h-1")
        assert frame["entry"] == "h-1"
        assert frame["kind"] == "clip_file"

    def test_an_ordinary_send_is_unchanged(self, tmp_path):
        manager = FileTransferManager("self-dev", output_dir=str(tmp_path))
        frame = self._frame(manager)
        assert "entry" not in frame
        assert frame["kind"] == "file"


# ═════════════════════════════════════════════════════════════════════════
# Reading a stored file list
# ═════════════════════════════════════════════════════════════════════════


def test_split_paths_is_the_stored_shape():
    assert split_paths("a\nb\n") == ["a", "b"]
    assert split_paths("  a  \n\n b ") == ["a", "b"]
    assert split_paths("") == []


def test_the_file_ref_module_never_imports_the_wire_format():
    """A guard on the layering the docstring claims: the offer is built from
    paths and knows nothing about clipboard types, so it can be reasoned about
    without the rest of the pipeline."""
    import internal.clipboard.file_ref as module

    assert not hasattr(module, "ContentType")
