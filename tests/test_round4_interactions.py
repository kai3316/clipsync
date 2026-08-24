"""Round-4 interaction regression tests: cross-feature combinations.

Pins the interaction matrix across rounds 1-3 (each cell verified by code
trace and/or nailed here):

  1. plain_text_only strip x sensitive filter — the SEND pipeline runs
     strip -> filter (src/main.py _on_local_sync); the RECEIVE pipeline
     runs strip only (_on_peer_message).  These tests run both pipelines
     end-to-end: secrets never resurrect, [FILTERED] markers survive the
     receiver-side strip, and the peer's final bytes are identical no
     matter which side had plain_text_only enabled.
  2. [FILTERED] x dedup — two DIFFERENT secrets redact to the same
     placeholder, so the PEER legitimately coalesces them into one
     history entry (dedup keys are post-redaction bytes only; the
     original length/hash must NOT leak into the key).  The local device
     keeps both originals.  Documented limitation, pinned here.
  3. history_max_age_days x pinned/favorites x id types — the SQL prune
     (`pinned = 0`) and the type-tolerant batch APIs must agree: an
     entry pinned via a STRING id (web query param shape) survives age
     pruning driven through INT ids.
  4. plain_text_only x capture loop-back — a received (stripped) clip
     lands on writer AND history byte-identically, its read-back is
     never re-broadcast, and an exact duplicate delivery is dropped.
  5. retry capture x pause — disabling sync while capture_with_retry is
     mid-flight blocks both the broadcast and the history insert.
  6. Linux/macOS capture shapes x RTF filter branch — non-Windows
     readers really do emit ContentType.RTF (xclip -t text/rtf /
     pbpaste RTF class); clean multi-format clips pass the filter
     byte-identical, sensitive ones lose ONLY the RTF payload.
  7. Corrupt-row robustness x capture persistence — a single corrupt
     `types` JSON row degrades to {} without aborting the load, and
     captures made afterwards still persist (the pre-fix failure mode
     was permanent insert collisions).
  8. Legacy ClipboardHistory (JSON) shares the type-tolerant id fixes.
  9. Darwin source-tracker split fix — window titles containing ", "
     stay intact.
"""

import base64
import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.clipboard import strip_rich_formats
from internal.clipboard.filter import ContentFilter
from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.clipboard.history import ClipboardHistory
from internal.clipboard.history_db import (
    ClipboardHistoryDB,
    _make_dedup_key,
    set_max_age_days,
)
from internal.clipboard import source_tracker
from internal.sync.manager import SyncManager

# ── Helpers ────────────────────────────────────────────────────────────

CARD_TEXT = "Pay 4111 1111 1111 1111 today"
CARD_HTML = b"<html><body><p>Pay 4111 1111 1111 1111 today</p></body></html>"
RTF_CARD = (
    b"{\\rtf1\\ansi\\deff0 {\\*\\generator ClipSync}"
    b"\\par Pay 4111 1111 1111 1111 today}"
)
PNG_BYTES = b"\x89PNG-fake-image-bytes"


def _send_pipeline(content: ClipboardContent, plain_text_only: bool,
                   filter_on: bool) -> ClipboardContent:
    """Mirror src/main.py _on_local_sync: strip FIRST, then filter."""
    msg_content = strip_rich_formats(content) if plain_text_only else content
    if filter_on:
        f = ContentFilter()
        if f.is_active and f.is_sensitive(msg_content):
            msg_content = f.filter_content(msg_content)
    return msg_content


def _receive_pipeline(content: ClipboardContent,
                      plain_text_only: bool = True) -> ClipboardContent:
    """Mirror src/main.py _on_peer_message: strip only, before the manager."""
    if plain_text_only:
        return strip_rich_formats(content)
    return content


@pytest.fixture(autouse=True)
def _default_max_age_disabled():
    """Keep the module-global age limit at 0 unless a test opts in."""
    set_max_age_days(0)
    yield
    set_max_age_days(0)


# ── 1. strip x filter pipelines ────────────────────────────────────────

def _rich_clip() -> ClipboardContent:
    return ClipboardContent(types={
        ContentType.TEXT: CARD_TEXT.encode("utf-8"),
        ContentType.HTML: CARD_HTML,
        ContentType.RTF: RTF_CARD,
        ContentType.IMAGE_PNG: PNG_BYTES,
    })


def test_peer_result_identical_regardless_of_sender_plain_text_toggle():
    """Both sender settings converge on the same peer-side bytes."""
    # Sender A: plain_text_only ON + filter ON.
    a = _receive_pipeline(_send_pipeline(_rich_clip(), True, True))
    # Sender B: plain_text_only OFF + filter ON; receiver strips instead.
    b = _receive_pipeline(_send_pipeline(_rich_clip(), False, True), True)

    assert a.types == b.types
    assert set(a.types) == {ContentType.TEXT, ContentType.IMAGE_PNG}
    assert a.types[ContentType.TEXT] == b"Pay [FILTERED] today"
    assert a.types[ContentType.IMAGE_PNG] == PNG_BYTES


def test_no_secret_bytes_survive_any_pipeline_stage():
    for plain in (True, False):
        stage0 = _rich_clip()
        stage1 = _send_pipeline(stage0, plain, True)
        stage2 = _receive_pipeline(stage1, True)
        for stage in (stage0, stage1, stage2):
            if stage is stage0:
                continue  # the local original intentionally keeps the card
            for data in stage.types.values():
                assert b"4111" not in data
        assert b"[FILTERED]" in stage2.types[ContentType.TEXT]


def test_rtf_only_sensitive_clip_survives_double_processing():
    """RTF-only clip: strip passes it through unchanged, the filter down-
    grades it to redacted plain TEXT, and the receiver-side strip must not
    damage that synthesized TEXT."""
    clip = ClipboardContent(types={ContentType.RTF: RTF_CARD})
    final = _receive_pipeline(_send_pipeline(clip, True, True), True)

    assert set(final.types) == {ContentType.TEXT}
    body = final.types[ContentType.TEXT]
    assert body == b"Pay [FILTERED] today"
    assert b"{" not in body and b"\\" not in body  # no brace garbage


# ── 2. [FILTERED] x dedup ──────────────────────────────────────────────

def test_two_secrets_local_two_entries_but_peer_one(tmp_path):
    db = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
    secret1 = ("sk-" + "a" * 24).encode("utf-8")
    secret2 = ("sk-" + "b" * 24).encode("utf-8")
    orig1 = ClipboardContent(types={ContentType.TEXT: secret1})
    orig2 = ClipboardContent(types={ContentType.TEXT: secret2})

    # Locally the originals differ -> two entries (the local history keeps
    # unredacted clips by design).
    assert _make_dedup_key(orig1) != _make_dedup_key(orig2)
    db.add(orig1)
    db.add(orig2)
    assert len(db.get_all()) == 2

    # On the wire both redact to the same placeholder -> identical dedup
    # key (post-redaction bytes only; no original length/hash may leak
    # into the key) -> the peer coalesces them into ONE entry.
    f = ContentFilter()
    red1 = f.filter_content(orig1)
    red2 = f.filter_content(orig2)
    assert red1.types[ContentType.TEXT] == b"[FILTERED]"
    assert _make_dedup_key(red1) == _make_dedup_key(red2)
    db.add(red1)
    db.add(red2)
    assert len(db.get_all()) == 3  # 2 originals + 1 coalesced peer entry


# ── 3. max-age x pinned x id types ─────────────────────────────────────

def test_string_id_pin_protects_entry_from_age_prune(tmp_path):
    db = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
    old = time.time() - 5 * 86400
    db.add(ClipboardContent(types={ContentType.TEXT: b"keep me"},
                            timestamp=old))
    db.add(ClipboardContent(types={ContentType.TEXT: b"drop me"},
                            timestamp=old - 10))
    by_preview = {e["text_preview"]: e["entry_id"] for e in db.get_all()}
    keep_id, drop_id = by_preview["keep me"], by_preview["drop me"]

    # Web query-param shape: the id arrives as a STRING.
    assert db.batch_set_pinned([str(keep_id)], True) == 1

    set_max_age_days(1)
    try:
        db._prune_by_age()
    finally:
        set_max_age_days(0)

    remaining = {e["entry_id"] for e in db.get_all()}
    assert remaining == {keep_id}
    assert drop_id not in remaining


# ── 4. remote apply x loop-back ────────────────────────────────────────

class _FakeMonitor:
    _poll_interval = 0.0

    def __init__(self):
        self.suppress_until = 0.0

    def start(self, cb):
        pass

    def stop(self):
        pass

    def suppress_for(self, seconds):
        self.suppress_until = time.time() + seconds


class _FakeWriter:
    def __init__(self):
        self.written = []

    def write(self, content):
        self.written.append(content)


class _FakeHistory:
    def __init__(self):
        self.added = []

    def add(self, content, **kwargs):
        self.added.append(content)


class _StaticReader:
    def __init__(self, content):
        self.content = content

    def read(self):
        return self.content


def test_remote_clip_lands_identically_without_echo_or_duplicate():
    text = b"shared plain note"
    stripped = ClipboardContent(types={ContentType.TEXT: text})
    reader = _StaticReader(stripped)  # what a read-back of the write returns
    writer, history = _FakeWriter(), _FakeHistory()
    mgr = SyncManager(device_id="dev-a", device_name="A", reader=reader,
                      writer=writer, monitor=_FakeMonitor(),
                      history=history, retry_enabled=False)
    sent = []
    mgr.on_send = lambda msg: sent.append(msg)

    msg = SyncMessage(content=stripped, source_device="dev-b")
    mgr.handle_remote_message(msg)
    assert len(writer.written) == 1
    # message bytes == written bytes == history bytes (dedup bookkeeping
    # is consistent with what actually landed on the clipboard).
    assert writer.written[0].types == {ContentType.TEXT: text}
    assert history.added[0].types == {ContentType.TEXT: text}

    # The read-back of our own write must not re-broadcast...
    mgr._do_read_and_send_locked()
    assert sent == []
    # ...and an exact duplicate delivery is dropped by loop prevention.
    mgr.handle_remote_message(SyncMessage(content=stripped,
                                          source_device="dev-b"))
    assert len(writer.written) == 1


# ── 5. retry capture x pause ───────────────────────────────────────────

def test_pause_mid_retry_capture_blocks_broadcast_and_history():
    writer, history = _FakeWriter(), _FakeHistory()

    class _PausingReader:
        def __init__(self):
            self.mgr = None

        def read(self):
            self.mgr.set_enabled(False)  # pause lands DURING the capture
            return ClipboardContent(types={ContentType.TEXT: b"mid-pause copy"})

    reader = _PausingReader()
    mgr = SyncManager(device_id="dev-a", device_name="A", reader=reader,
                      writer=writer, monitor=_FakeMonitor(),
                      history=history, retry_enabled=True)
    reader.mgr = mgr
    sent = []
    mgr.on_send = lambda msg: sent.append(msg)

    mgr._do_read_and_send_locked()

    assert sent == []
    assert history.added == []


# ── 6. Linux/macOS capture shapes x RTF filter branch ──────────────────

def _linux_shaped_clip(card_in_body: bool) -> ClipboardContent:
    body = CARD_TEXT if card_in_body else "release notes body"
    return ClipboardContent(types={
        ContentType.TEXT: body.encode("utf-8"),
        ContentType.HTML: ("<p>%s</p>" % body).encode("utf-8"),
        # xclip -t text/rtf / pbpaste RTF really emit this off-Windows.
        ContentType.RTF: RTF_CARD if card_in_body
        else b"{\\rtf1\\ansi\\deff0 Release notes}",
        ContentType.FILE: "/home/u/a.txt\n/home/u/b.txt".encode("utf-8"),
        ContentType.URL: b"https://example.com/x",
        ContentType.IMAGE_PNG: PNG_BYTES,
    })


def test_clean_multiformat_clip_passes_filter_byte_identical():
    clip = _linux_shaped_clip(card_in_body=False)
    out = ContentFilter().filter_content(clip)
    assert out.types == clip.types  # every format untouched, clean RTF too


def test_sensitive_multiformat_clip_drops_only_rtf():
    clip = _linux_shaped_clip(card_in_body=True)
    out = ContentFilter().filter_content(clip)
    assert ContentType.RTF not in out.types  # unredactable payload gone
    assert out.types[ContentType.TEXT] == b"Pay [FILTERED] today"
    assert out.types[ContentType.HTML] == b"<p>Pay [FILTERED] today</p>"
    for ct in (ContentType.FILE, ContentType.URL, ContentType.IMAGE_PNG):
        assert out.types[ct] == clip.types[ct]


# ── 7. corrupt row x capture persistence ───────────────────────────────

def test_corrupt_types_row_degrades_and_captures_still_persist(tmp_path):
    path = str(tmp_path / "h.db")
    conn = sqlite3.connect(path)
    conn.executescript(ClipboardHistoryDB._SCHEMA)
    good = json.dumps({"TEXT": base64.b64encode(b"good row").decode("ascii")})
    conn.execute(
        "INSERT INTO history (entry_id, timestamp, content_type, "
        "text_preview, types) VALUES (1, ?, 'TEXT', 'good row', ?)",
        (time.time(), good),
    )
    conn.execute(
        "INSERT INTO history (entry_id, timestamp, content_type, "
        "text_preview, types) VALUES (2, ?, 'TEXT', 'bad row', '{corrupt')",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    db = ClipboardHistoryDB(storage_path=path)
    assert len(db.get_all()) == 2  # load did NOT abort on the corrupt row
    bad = next(e for e in db.get_all() if e["text_preview"] == "bad row")
    assert bad["types"] == {}

    db.add(ClipboardContent(types={ContentType.TEXT: b"fresh capture"}))
    fresh = ClipboardHistoryDB(storage_path=path)
    previews = {e["text_preview"] for e in fresh.get_all()}
    assert previews == {"good row", "bad row", "fresh capture"}


# ── 8. legacy JSON history parity ──────────────────────────────────────

def test_legacy_json_history_type_tolerant_batch_ops(tmp_path):
    h = ClipboardHistory(storage_path=str(tmp_path / "legacy.json"))
    h.add(ClipboardContent(types={ContentType.TEXT: b"a note"}))
    eid = h.get_all()[0]["entry_id"]
    assert isinstance(eid, int)

    assert h.batch_set_pinned([str(eid)], True) == 1
    _, entry = h.find_by_id(str(eid))
    assert entry is not None and entry["pinned"] is True

    assert h.batch_delete([str(eid)]) == 1
    assert h.get_all() == []


# ── 9. darwin source tracker title split ───────────────────────────────

def test_darwin_window_title_with_commas_stays_intact(monkeypatch):
    class _Result:
        returncode = 0
        stdout = b"Finder, 123, My Doc, final v2"

    monkeypatch.setattr(source_tracker.subprocess, "run",
                        lambda *a, **k: _Result())
    info = source_tracker._get_active_app_info_darwin()
    assert info["name"] == "Finder"
    assert info["title"] == "My Doc, final v2"  # split(", ", 2) kept it whole
