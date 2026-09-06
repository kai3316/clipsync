"""Tests for SyncManager — dedup, loop prevention, throttle, enable/disable."""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.sync.manager import DEDUP_RING_SIZE, SyncManager


class MockClipboardMonitor:
    def __init__(self):
        self._callback = None
        self._running = False
        self.suppress_until = 0.0

    def start(self, callback):
        self._callback = callback
        self._running = True

    def stop(self):
        self._running = False
        self._callback = None

    def suppress_for(self, duration_seconds):
        """Mirror the real monitor: drop callbacks until the window passes."""
        self.suppress_until = time.time() + duration_seconds

    def fire(self):
        if self._callback and time.time() >= self.suppress_until:
            self._callback()


class MockClipboardReader:
    def __init__(self):
        self.content = ClipboardContent()

    def read(self) -> ClipboardContent:
        return self.content


class MockClipboardWriter:
    def __init__(self):
        self.last_written: ClipboardContent | None = None
        self.write_count = 0

    def write(self, content: ClipboardContent):
        self.last_written = content
        self.write_count += 1
        return True


class TestSyncManager:
    def setup_method(self):
        self.monitor = MockClipboardMonitor()
        self.reader = MockClipboardReader()
        self.writer = MockClipboardWriter()
        self.sent: list[SyncMessage] = []
        # Use dependency injection — bypasses platform-specific factories
        self.mgr = SyncManager(
            "test-device",
            "Test Device",
            reader=self.reader,
            writer=self.writer,
            monitor=self.monitor,
        )
        self.mgr.on_send = lambda msg: self.sent.append(msg)

    def teardown_method(self):
        self.mgr.stop()

    def _wait_sent(self, count: int, timeout: float = 3.0) -> bool:
        """Deadline-poll until len(self.sent) reaches `count`.

        The debounce timer (SYNC_DEBOUNCE = 0.5s) fires on a background thread,
        so a fixed ``time.sleep(0.5)`` right at the boundary races it on a
        loaded CI runner (timer delayed past the boundary → a later fire
        cancels the pending timer). Polling for the observed outcome instead
        is robust under load.
        """
        deadline = time.time() + timeout
        while len(self.sent) < count and time.time() < deadline:
            time.sleep(0.05)
        return len(self.sent) >= count

    def test_local_change_broadcasts(self):
        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"hello"},
        )
        self.mgr.start()
        self.monitor.fire()

        assert self._wait_sent(1)
        assert self.sent[0].source_device == "test-device"
        assert self.sent[0].content.types[ContentType.TEXT] == b"hello"

    def test_dedup_identical_content(self):
        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"dup"},
        )
        self.mgr.start()
        self.monitor.fire()
        self.monitor.fire()  # second fire cancels timer, restarts — coalesced

        assert self._wait_sent(1)
        assert len(self.sent) == 1

    def test_different_content_sends_both(self):
        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"first"},
        )
        self.mgr.start()
        self.monitor.fire()

        # Wait for the FIRST send to actually complete before firing the second
        # — otherwise the second fire cancels the first's pending timer and
        # "first" is coalesced away (a real race at the debounce boundary).
        assert self._wait_sent(1)

        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"second"},
        )
        self.monitor.fire()

        assert self._wait_sent(2)
        assert len(self.sent) == 2

    def test_throttle_rapid_changes(self):
        self.mgr.start()

        # Fire 5 changes as fast as possible — coalescing timer keeps resetting
        for i in range(5):
            self.reader.content = ClipboardContent(
                types={ContentType.TEXT: f"rapid {i}".encode()},
            )
            self.monitor.fire()

        # Rapid changes coalesce into a single send (only the last one)
        assert self._wait_sent(1)
        assert len(self.sent) == 1, f"Expected coalescing to 1, got {len(self.sent)}"
        assert self.sent[0].content.types[ContentType.TEXT] == b"rapid 4"

    def test_disabled_does_not_broadcast(self):
        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"should not send"},
        )
        self.mgr.set_enabled(False)
        self.mgr.start()
        self.monitor.fire()
        time.sleep(0.1)

        assert len(self.sent) == 0

    def test_disabled_does_not_receive(self):
        self.mgr.set_enabled(False)
        self.mgr.start()

        msg = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"remote"}),
            msg_id="remote-1",
            source_device="peer",
        )
        self.mgr.handle_remote_message(msg)
        assert self.writer.write_count == 0

    def test_remote_message_writes_locally(self):
        msg = SyncMessage(
            content=ClipboardContent(
                types={ContentType.TEXT: b"from peer", ContentType.HTML: b"<p>peer</p>"},
            ),
            msg_id="r1",
            source_device="peer-device",
        )
        self.mgr.start()
        self.mgr.handle_remote_message(msg)

        assert self.writer.write_count == 1
        assert self.writer.last_written is not None
        assert self.writer.last_written.types[ContentType.TEXT] == b"from peer"
        assert self.writer.last_written.types[ContentType.HTML] == b"<p>peer</p>"

    def test_loop_prevention(self):
        self.reader.content = ClipboardContent(
            types={ContentType.TEXT: b"loop-test"},
        )
        self.mgr.start()
        self.monitor.fire()

        assert self._wait_sent(1)
        write_count_before = self.writer.write_count

        # Simulate the remote reflecting this back
        reflected = SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: b"loop-test"}),
            msg_id="reflected",
            source_device="peer",
        )
        self.mgr.handle_remote_message(reflected)
        assert self.writer.write_count == write_count_before, "Should not write reflected content"

    def test_empty_content_ignored(self):
        self.reader.content = ClipboardContent()  # empty
        self.mgr.start()
        self.monitor.fire()
        time.sleep(0.1)
        assert len(self.sent) == 0

    def test_dedup_ring_limits(self):
        self.mgr.start()
        # Send many different clips
        for i in range(DEDUP_RING_SIZE + 10):
            self.reader.content = ClipboardContent(
                types={ContentType.TEXT: f"clip-{i}".encode()},
            )
            self.monitor.fire()
            # Wait for this clip to be observed before sending the next one.
            # A fixed sleep can be flaky on slower CI runners.
            deadline = time.time() + 2.0
            while len(self.sent) < i + 1 and time.time() < deadline:
                time.sleep(0.05)

        # All should have been sent (each is different)
        assert len(self.sent) == DEDUP_RING_SIZE + 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ══════════════════════════════════════════════════
# merged from test_round4_interactions.py
# ══════════════════════════════════════════════════

import base64
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard import source_tracker
from internal.clipboard.clipboard import strip_rich_formats
from internal.clipboard.filter import ContentFilter
from internal.clipboard.format import ClipboardContent
from internal.clipboard.history_db import (
    ClipboardHistoryDB,
    _make_dedup_key,
    set_max_age_days,
)

# ── Helpers ────────────────────────────────────────────────────────────

CARD_TEXT = "Pay 4111 1111 1111 1111 today"
CARD_HTML = b"<html><body><p>Pay 4111 1111 1111 1111 today</p></body></html>"
RTF_CARD = b"{\\rtf1\\ansi\\deff0 {\\*\\generator ClipSync}\\par Pay 4111 1111 1111 1111 today}"
PNG_BYTES = b"\x89PNG-fake-image-bytes"


def _send_pipeline(
    content: ClipboardContent, plain_text_only: bool, filter_on: bool
) -> ClipboardContent:
    """Mirror src/main.py _on_local_sync: strip FIRST, then filter."""
    msg_content = strip_rich_formats(content) if plain_text_only else content
    if filter_on:
        f = ContentFilter()
        if f.is_active and f.is_sensitive(msg_content):
            msg_content = f.filter_content(msg_content)
    return msg_content


def _receive_pipeline(content: ClipboardContent, plain_text_only: bool = True) -> ClipboardContent:
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
    return ClipboardContent(
        types={
            ContentType.TEXT: CARD_TEXT.encode("utf-8"),
            ContentType.HTML: CARD_HTML,
            ContentType.RTF: RTF_CARD,
            ContentType.IMAGE_PNG: PNG_BYTES,
        }
    )


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
    from unittest.mock import patch

    db = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
    # The DB stamps local receipt time, so inject genuine age by freezing
    # the clock back 5 days while the entries are added.
    old = time.time() - 5 * 86400
    counter = {"n": 0}

    def fake_time():
        counter["n"] += 1
        return old + counter["n"] * 100.0  # > FLAVOR_MERGE_WINDOW apart

    with patch("internal.clipboard.history_db.time.time", side_effect=fake_time):
        db.add(ClipboardContent(types={ContentType.TEXT: b"keep me"}))
        db.add(ClipboardContent(types={ContentType.TEXT: b"drop me"}))
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
        return True


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


class _IdleBackoffMonitor:
    """Monitor that backs off to a longer idle poll interval (like Linux)."""

    _poll_interval = 1.0
    _idle_poll_interval = 2.5

    def __init__(self):
        self.suppress_until = 0.0
        self.last_suppress_duration = 0.0

    def start(self, cb):
        pass

    def stop(self):
        pass

    def suppress_for(self, seconds):
        self.suppress_until = time.time() + seconds
        self.last_suppress_duration = seconds


def test_suppression_covers_idle_poll_interval():
    # A re-encoded read-back (BMP/TIFF -> PNG on Linux) must not be re-detected
    # as a fresh local copy: the write-suppression window has to outlast the
    # monitor's IDLE poll interval, not just the active one.
    mon = _IdleBackoffMonitor()
    mgr = SyncManager(
        device_id="dev-a",
        device_name="A",
        reader=_StaticReader(ClipboardContent(types={ContentType.TEXT: b"x"})),
        writer=_FakeWriter(),
        monitor=mon,
        retry_enabled=False,
    )
    assert mgr._max_poll_interval == 2.5

    mgr.handle_remote_message(
        SyncMessage(content=ClipboardContent(types={ContentType.TEXT: b"x"}), source_device="dev-b")
    )
    # Window = debounce + idle interval + 0.2 buffer.
    assert mon.last_suppress_duration >= mgr._sync_debounce + 2.5 + 0.2


def test_remote_clip_lands_identically_without_echo_or_duplicate():
    text = b"shared plain note"
    stripped = ClipboardContent(types={ContentType.TEXT: text})
    reader = _StaticReader(stripped)  # what a read-back of the write returns
    writer, history = _FakeWriter(), _FakeHistory()
    mgr = SyncManager(
        device_id="dev-a",
        device_name="A",
        reader=reader,
        writer=writer,
        monitor=_FakeMonitor(),
        history=history,
        retry_enabled=False,
    )
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
    mgr.handle_remote_message(SyncMessage(content=stripped, source_device="dev-b"))
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
    mgr = SyncManager(
        device_id="dev-a",
        device_name="A",
        reader=reader,
        writer=writer,
        monitor=_FakeMonitor(),
        history=history,
        retry_enabled=True,
    )
    reader.mgr = mgr
    sent = []
    mgr.on_send = lambda msg: sent.append(msg)

    mgr._do_read_and_send_locked()

    assert sent == []
    assert history.added == []


# ── 6. Linux/macOS capture shapes x RTF filter branch ──────────────────


def _linux_shaped_clip(card_in_body: bool) -> ClipboardContent:
    body = CARD_TEXT if card_in_body else "release notes body"
    return ClipboardContent(
        types={
            ContentType.TEXT: body.encode("utf-8"),
            ContentType.HTML: (f"<p>{body}</p>").encode(),
            # xclip -t text/rtf / pbpaste RTF really emit this off-Windows.
            ContentType.RTF: RTF_CARD if card_in_body else b"{\\rtf1\\ansi\\deff0 Release notes}",
            ContentType.FILE: b"/home/u/a.txt\n/home/u/b.txt",
            ContentType.URL: b"https://example.com/x",
            ContentType.IMAGE_PNG: PNG_BYTES,
        }
    )


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
    h = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
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

    monkeypatch.setattr(source_tracker.subprocess, "run", lambda *a, **k: _Result())
    info = source_tracker._get_active_app_info_darwin()
    assert info["name"] == "Finder"
    assert info["title"] == "My Doc, final v2"  # split(", ", 2) kept it whole


# ══════════════════════════════════════════════════
# merged from test_round9_interactions.py
# ══════════════════════════════════════════════════

import inspect
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import internal.config.config as config_module
from internal.clipboard import history_db as history_db_mod
from internal.clipboard.format import ClipboardContent
from internal.config.config import Config
from internal.data import backup as backup_mod
from internal.sync.file_transfer import CHUNK_SIZE, FileTransferManager

BODY = b"same body"
RICH_HTML = b"<html><body><b>same body</b></body></html>"


def _rich() -> ClipboardContent:
    return ClipboardContent(
        types={
            ContentType.TEXT: BODY,
            ContentType.HTML: RICH_HTML,
        }
    )


def _plain(device: str = "") -> ClipboardContent:
    c = ClipboardContent(types={ContentType.TEXT: BODY})
    c.source_device = device
    return c


def _backdate_coalesce(h: ClipboardHistoryDB, seconds: float) -> None:
    """Push the last-add bookkeeping outside DEDUP_WINDOW (2s) but keep the
    top entry's timestamp at *seconds* ago for merge-window control."""
    h._last_dedup_time = time.time() - seconds - 1


# ═════════════════════════════════════════════════════════════════════════
# Pair 1 — flavor merge x sync send/receive
# ═════════════════════════════════════════════════════════════════════════


class TestMergeXSync:
    def test_remote_plain_variant_merges_into_rich_entry(self, tmp_path):
        """A remote plain-text copy of a rich local clip (within 90s) must
        merge into the existing entry — not stack a poorer duplicate — and
        re-attribute it to the sending device."""
        h = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        h.add(_rich())
        _backdate_coalesce(h, 5)
        h._entries[0]["timestamp"] = time.time() - 5

        remote = _plain(device="peerB")
        remote.timestamp = time.time() - 3  # sender clock slightly behind
        h.add(remote)

        assert len(h._entries) == 1
        top = h._entries[0]
        assert "HTML" in top["types"], "rich flavor must survive the merge"
        assert top["source_device"] == "peerB"

    def test_remote_different_text_stacks_normally(self, tmp_path):
        h = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        h.add(_rich())
        _backdate_coalesce(h, 5)
        other = ClipboardContent(types={ContentType.TEXT: b"other text"})
        other.source_device = "peerC"
        h.add(other)
        assert len(h._entries) == 2

    def test_merge_never_mutates_the_broadcast_content(self, tmp_path):
        """The sender reads/broadcasts its clipboard bytes; the history-level
        merge builds a NEW format map and must leave the captured object (the
        one handed to on_send) byte-identical."""
        h = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        h.add(_rich())
        _backdate_coalesce(h, 5)

        outgoing = _plain()
        h.add(outgoing)
        assert set(outgoing.types.keys()) == {ContentType.TEXT}
        assert outgoing.types[ContentType.TEXT] == BODY

    def test_plain_text_only_strip_composes_with_merge(self, tmp_path):
        """plain_text_only strips HTML/RTF from the message only; the receiver
        still unifies the stripped variant with any rich entry it already
        holds."""
        stripped = strip_rich_formats(_rich())
        assert ContentType.HTML not in stripped.types
        assert stripped.types[ContentType.TEXT] == BODY

        h = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        h.add(_rich())
        _backdate_coalesce(h, 5)
        h._entries[0]["timestamp"] = time.time() - 5
        h.add(stripped)
        assert len(h._entries) == 1
        assert "HTML" in h._entries[0]["types"]


class _MockMonitor:
    def __init__(self):
        self._callback = None
        self.suppress_until = 0.0
        self.last_source_app = None

    def start(self, cb):
        self._callback = cb

    def stop(self):
        self._callback = None

    def suppress_for(self, seconds):
        self.suppress_until = time.time() + seconds

    def fire(self):
        if self._callback and time.time() >= self.suppress_until:
            self._callback()


class _MockReader:
    def __init__(self):
        self.content = ClipboardContent()

    def read(self):
        return self.content


class _MockWriter:
    def __init__(self):
        self.last_written = None
        self.count = 0

    def write(self, content):
        self.last_written = content
        self.count += 1
        return True


class TestRemoteSameTextReachesReceiver:
    """Full sync-manager path: the loop-prevention dedup hashes ALL formats,
    so a plain variant of a locally-known rich clip is NOT swallowed — it is
    written to the clipboard and merged into history."""

    def test_remote_plain_variant_passes_loop_checks(self, tmp_path):
        monitor, reader, writer = _MockMonitor(), _MockReader(), _MockWriter()
        hist = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        mgr = SyncManager(
            "self-dev", "Self", reader=reader, writer=writer, monitor=monitor, history=hist
        )
        sent = []
        mgr.on_send = lambda msg: sent.append(msg)

        # Local rich copy first (broadcast path only — the writer is not
        # involved in local captures).
        mgr.start()
        reader.content = _rich()
        monitor.fire()
        deadline = time.time() + 4
        while not sent and time.time() < deadline:
            time.sleep(0.02)
        assert len(sent) == 1, "local rich copy must broadcast"

        # Peer re-copies the same text as plain text and syncs it over.
        # Arriving within the history's 2 s coalesce window of the local
        # capture it would be absorbed with local attribution kept (by
        # design — the echo adds nothing); backdate that window so the copy
        # lands in the 90 s flavor-merge band instead, where it must merge
        # AND re-attribute to the sender.
        hist._last_dedup_time -= 3.0
        msg = SyncMessage(content=_plain(), msg_id="m1", source_device="peerB")
        mgr.handle_remote_message(msg)
        mgr.stop()

        assert writer.count == 1, "remote variant must reach the clipboard"
        assert writer.last_written is msg.content
        assert len(hist.get_all()) == 1, "history must merge, not duplicate"
        entry = hist.get_all()[0]
        assert "HTML" in entry["types"]
        assert entry["source_device"] == "peerB"


# ═════════════════════════════════════════════════════════════════════════
# Pair 2 — age prune x pinned x merge timestamp refresh
# ═════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def max_age():
    """Wire a small age limit, restore the module global afterwards."""
    prev = history_db_mod.MAX_AGE_DAYS
    yield history_db_mod
    history_db_mod.set_max_age_days(prev if prev else 0)


class TestAgePruneXPinnedXMerge:
    def test_old_unpinned_pruned_pinned_survives(self, tmp_path, max_age):
        max_age.set_max_age_days(60.0 / 86400.0)  # ~60 s retention
        h = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        h.add(ClipboardContent(types={ContentType.TEXT: b"ancient"}))
        ancient_id = h._entries[0]["entry_id"]

        _backdate_coalesce(h, 5)
        keeper = ClipboardContent(types={ContentType.TEXT: b"keeper pin"})
        h.add(keeper)
        h.pin(0)  # display index 0 == newest == keeper

        # Age BOTH rows past the cutoff (memory + DB).
        old_ts = time.time() - 120
        for entry_id, idx in ((ancient_id, 1), (h._entries[0]["entry_id"], 0)):
            h._entries[idx]["timestamp"] = old_ts
            h._update_row(entry_id, timestamp=old_ts)

        trigger = ClipboardContent(types={ContentType.TEXT: b"trigger prune"})
        _backdate_coalesce(h, 5)
        h.add(trigger)

        texts = [e["text_preview"] for e in h._entries]
        assert "ancient" not in texts, "unpinned + old must be pruned"
        assert h.find_by_id(str(ancient_id))[0] is None
        assert "keeper pin" in texts, "pinned rows are never age-pruned"
        assert "trigger prune" in texts

    def test_merge_refresh_bounded_by_90s_window(self, tmp_path, max_age):
        """The 'timestamps only move forward' merge cannot immortalize an old
        entry: beyond FLAVOR_MERGE_WINDOW a re-copy starts a NEW row instead
        of refreshing the stale one — and the age prune that runs on that very
        capture removes the stale row, even though its text was just
        re-copied."""
        max_age.set_max_age_days(1.0)  # 1 day retention
        h = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        h.add(ClipboardContent(types={ContentType.TEXT: b"recopied"}))
        _backdate_coalesce(h, 5)
        # Age the single entry to 3 days — far beyond merge window AND limit.
        old_ts = time.time() - 3 * 86400
        stale_id = h._entries[0]["entry_id"]
        h._entries[0]["timestamp"] = old_ts
        h._update_row(stale_id, timestamp=old_ts)

        # Deliberate re-copy of the same text now.
        again = ClipboardContent(types={ContentType.TEXT: b"recopied"})
        h.add(again)

        previews = [e["text_preview"] for e in h._entries]
        assert previews.count("recopied") == 1, (
            "stale row must be gone despite the same-text re-copy"
        )
        assert h.find_by_id(str(stale_id))[0] is None, (
            "the surviving 'recopied' row is the fresh one, not the stale one"
        )
        assert time.time() - h._entries[0]["timestamp"] < 90, (
            "the fresh copy carries its own new timestamp"
        )


# ═════════════════════════════════════════════════════════════════════════
# Pair 3 — timed pause x restart / shutdown / manual toggles
# ═════════════════════════════════════════════════════════════════════════


def _point_config_at(tmp_path, monkeypatch, data=None):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "config.json"
    if data is not None:
        cfg_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config_module, "_config_dir", lambda: cfg_dir)
    monkeypatch.setattr(config_module, "_config_path", lambda: cfg_path)
    return cfg_path


class TestTimedPausePersistenceConfig:
    def test_roundtrip_preserves_deadline(self, tmp_path, monkeypatch):
        _point_config_at(tmp_path, monkeypatch)
        cfg = Config()
        cfg.timed_pause_until = 1770000000.5
        config_module.save(cfg)
        loaded = config_module.load()
        assert loaded.timed_pause_until == pytest.approx(1770000000.5)

    def test_invalid_type_keeps_default(self, tmp_path, monkeypatch):
        _point_config_at(
            tmp_path,
            monkeypatch,
            {
                "timed_pause_until": "soon",
                "sync_enabled": False,
            },
        )
        cfg = config_module.load()
        assert cfg.timed_pause_until == 0.0
        assert cfg.sync_enabled is False, "other fields still load"


class _StubSyncMgr:
    def __init__(self):
        self._enabled = True
        self.calls = []

    def set_enabled(self, enabled):
        self.calls.append(enabled)
        self._enabled = enabled


def _bare_app(monkeypatch):
    """Application instance via __new__ with stubbed collaborators."""
    from src.main import Application

    app = Application.__new__(Application)
    app.cfg = Config()
    app._shutting_down = False
    app._pause_timer = None
    app._pause_deadline = None
    app.systray = None
    app.root = None
    app.sync_mgr = _StubSyncMgr()
    saves = []

    def _fake_save():
        saves.append(app.cfg.timed_pause_until)

    monkeypatch.setattr(app, "_save_cfg_encrypted", _fake_save)
    monkeypatch.setattr(app, "_set_systray_syncing", lambda e: None)
    monkeypatch.setattr(app, "_notify", lambda *a, **k: None)
    app._pause_saves = saves
    return app


class TestTimedPauseLifecycle:
    def test_pause_persists_deadline_clear_drops_it(self, monkeypatch):
        app = _bare_app(monkeypatch)
        try:
            app._pause_sync_for_minutes(15)
            assert app.cfg.timed_pause_until > time.time()
            assert app.cfg.sync_enabled is False
            assert app._pause_saves[-1] > time.time()

            app._clear_pause_state()
            assert app.cfg.timed_pause_until == 0.0
            assert app._pause_saves[-1] == 0.0
        finally:
            if app._pause_timer is not None:
                app._pause_timer.cancel()

    def test_rapid_double_pause_replaces_timer(self, monkeypatch):
        app = _bare_app(monkeypatch)
        timers = []
        try:
            app._pause_sync_for_minutes(30)
            first = app._pause_timer
            timers.append(first)
            app._pause_sync_for_minutes(10)  # quick re-click of another option
            assert first.finished.is_set(), "old resume timer must be cancelled"
            assert app._pause_deadline is not None
            assert app.cfg.timed_pause_until > time.time()
        finally:
            for t in timers:
                t.cancel()
            if app._pause_timer is not None:
                app._pause_timer.cancel()

    def test_shutdown_keeps_deadline_for_next_launch(self, monkeypatch):
        """Quitting mid-pause must NOT wipe the persisted deadline: the next
        launch resumes the remaining countdown (mirrors sync_enabled=False)."""
        app = _bare_app(monkeypatch)
        app._shutting_down = True
        app.cfg.timed_pause_until = time.time() + 500
        before = len(app._pause_saves)
        app._clear_pause_state()
        assert app.cfg.timed_pause_until > time.time(), (
            "shutdown clear keeps the persisted deadline"
        )
        assert len(app._pause_saves) == before, "no extra save during shutdown"

    def test_restore_rearms_remaining_and_expired_resumes(self, tmp_path, monkeypatch):
        app = _bare_app(monkeypatch)

        # Expired while closed → sync returns ON, deadline cleared.
        app.cfg.timed_pause_until = time.time() - 60
        app.cfg.sync_enabled = False
        app._restore_timed_pause()
        assert app.cfg.timed_pause_until == 0.0
        assert app.cfg.sync_enabled is True
        assert app.sync_mgr.calls[-1] is True
        if app._pause_timer is not None:
            app._pause_timer.cancel()

        # Restart inside the window → re-armed with the remaining minutes.
        app2 = _bare_app(monkeypatch)
        try:
            app2.cfg.timed_pause_until = time.time() + 20 * 60
            app2.cfg.sync_enabled = False
            app2._restore_timed_pause()
            assert app2.cfg.timed_pause_until > time.time(), (
                "deadline must be re-persisted for the next potential restart"
            )
            assert app2.cfg.sync_enabled is False
            assert app2._pause_deadline is not None
            left = _minutes_left(app2._pause_deadline)
            assert 19 <= left <= 21
        finally:
            if app2._pause_timer is not None:
                app2._pause_timer.cancel()

        # No pending deadline → restore is a no-op.
        app3 = _bare_app(monkeypatch)
        app3._restore_timed_pause()
        assert app3.sync_mgr.calls == []


def _minutes_left(deadline):
    from internal.ui.systray import SystrayApp

    return SystrayApp.pause_left_minutes(deadline)


# ═════════════════════════════════════════════════════════════════════════
# Pair 4 — file_request idempotency x reject-then-retry
# ═════════════════════════════════════════════════════════════════════════

TID = "b" * 32


class TestFileRequestIdempotencyXReject:
    def _request(self, size=CHUNK_SIZE * 3):
        return {
            "msg_type": "file_request",
            "transfer_id": TID,
            "file_name": "retry.txt",
            "file_size": size,
            "mime_type": "text/plain",
            "kind": "file",
        }

    def test_reject_kills_id_retry_registers_fresh(self, tmp_path):
        """After a user rejection the id must be dead — and a retried request
        carrying the same id must NOT be swallowed by the duplicate guard:
        it registers a fresh pending offer and prompts again."""
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))
        dialogs = []
        mgr.set_on_transfer_request(lambda tid, n, s, m, fn: dialogs.append(tid))
        sends = []
        send_fn = lambda data: sends.append(data)  # noqa: E731

        mgr.handle_message("file_request", self._request(), send_fn, "peerA")
        assert dialogs == [TID]
        mgr.reject_transfer(TID, send_fn)
        assert TID not in mgr._transfers
        assert any(b"file_reject" in d for d in sends), "peer told of reject"

        # Same-id retry (sender resend / broadcast replay after reject).
        sends.clear()
        mgr.handle_message("file_request", dict(self._request()), send_fn, "peerA")
        assert dialogs == [TID, TID], "legitimate retry must prompt again"
        assert mgr._transfers[TID]["state"] == "pending"

        mgr.reject_transfer(TID, send_fn)

    def test_duplicate_request_mid_receive_resets_nothing(self, tmp_path):
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))
        mgr.handle_message("file_request", self._request(), lambda d: True, "peerA")
        mgr.accept_transfer(TID, lambda d: True)
        chunk0 = {
            "msg_type": "file_chunk",
            "transfer_id": TID,
            "chunk_index": 0,
            "total_chunks": 3,
            "_raw_data": b"\x01" * CHUNK_SIZE,
        }
        mgr.handle_message("file_chunk", chunk0, lambda d: True, "peerA")
        before = (mgr._transfers[TID]["received_bytes"], mgr._transfers[TID]["received_chunks"])

        # Replay of the request mid-transfer: must not zero the receive window
        # nor reopen a second temp handle.
        mgr.handle_message("file_request", dict(self._request()), lambda d: True, "peerA")
        t = mgr._transfers[TID]
        assert (t["received_bytes"], t["received_chunks"]) == before
        assert t["state"] == "receiving"
        assert t["received_chunks"] == 1

        mgr.cancel_transfer(TID)


# ═════════════════════════════════════════════════════════════════════════
# Pair 5 — speed sampling x cancel-all / pause / completion
# ═════════════════════════════════════════════════════════════════════════


class TestRateSampleLifecycle:
    def test_cancel_all_leaves_no_transfer_state_behind(self, tmp_path):
        """The web/desktop 'cancel all' pattern: snapshot get_transfers() and
        cancel each row.  Samples ride the transfer dict, so every terminal
        path must remove the dict itself — nothing may linger."""
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))
        ids = []
        for i in range(2):
            src = tmp_path / f"up{i}.bin"
            src.write_bytes(bytes([i]) * (CHUNK_SIZE + 7))
            tid = mgr.send_file(str(src), lambda data: True)
            ids.append(tid)
            t = mgr._transfers[tid]
            FileTransferManager._record_rate_sample(t, 1000)
            FileTransferManager._record_rate_sample(t, 2000)
        assert all(r["speed_bytes_per_sec"] >= 0 for r in mgr.get_transfers())

        # cancel-all: iterate the SNAPSHOT (ids), cancel each.
        for tid in list(ids):
            assert mgr.cancel_transfer(tid, lambda data: True)
        assert mgr._transfers == {}, "no sample state may outlive its transfer"
        assert mgr.get_transfers() == []
        assert not list(tmp_path.glob(".*.part"))

    def test_paused_receiver_records_no_samples_then_completes(self, tmp_path):
        total = 3
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))
        req = {
            "msg_type": "file_request",
            "transfer_id": TID,
            "file_name": "paused.bin",
            "file_size": CHUNK_SIZE * total,
            "mime_type": "application/octet-stream",
            "kind": "file",
        }
        done = []
        mgr.set_on_transfer_complete(lambda tid, ok, canc, status: done.append(status))
        mgr.handle_message("file_request", req, lambda d: True, "peerA")
        mgr.accept_transfer(TID, lambda d: True)

        mgr.pause_transfer(TID, lambda d: True)
        chunk = {
            "msg_type": "file_chunk",
            "transfer_id": TID,
            "chunk_index": 0,
            "total_chunks": total,
            "_raw_data": b"\x05" * CHUNK_SIZE,
        }
        mgr.handle_message("file_chunk", chunk, lambda d: True, "peerA")
        t = mgr._transfers[TID]
        assert t["received_chunks"] == 0, "paused chunks are dropped"
        assert not t.get("_rate_samples"), "no progress => no samples"
        assert FileTransferManager._instant_speed(t) == 0.0

        mgr.resume_transfer(TID, lambda d: True)
        for i in range(total):
            mgr.handle_message("file_chunk", {**chunk, "chunk_index": i}, lambda d: True, "peerA")

        deadline = time.time() + 5
        while TID in mgr._transfers and time.time() < deadline:
            time.sleep(0.05)
        assert done and done[-1] == "success"
        assert mgr._transfers == {}, "completion cleans up samples too"


# ═════════════════════════════════════════════════════════════════════════
# Pair 6 — backup hotkeys x factory reset x old backups
# ═════════════════════════════════════════════════════════════════════════


class TestBackupHotkeysXMigration:
    def test_hotkeys_roundtrip_through_backup(self, tmp_path):
        hist = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        cfg = Config()
        cfg.hotkeys = {"paste_1": "F2"}
        cfg.hotkeys_enabled = True
        zip_path = backup_mod.create_backup(cfg, hist, backup_dir=str(tmp_path / "bk"))
        assert zipfile.is_zipfile(zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            exported = json.loads(zf.read("config.json").decode("utf-8"))
        assert exported["hotkeys"] == {"paste_1": "F2"}
        assert exported["hotkeys_enabled"] is True

        target = Config()
        summary = backup_mod.restore_backup(zip_path, target, hist)
        assert summary["config"] is True
        assert target.hotkeys == {"paste_1": "F2"}
        assert target.hotkeys_enabled is True

    def test_old_backup_without_hotkeys_keeps_defaults(self, tmp_path):
        """A pre-hotkeys backup (no such key) must restore cleanly and leave
        the current default bindings intact — never empty them."""
        legacy = tmp_path / "legacy.zip"
        config_payload = {"device_name": "OldBackupBox", "port": 19990}
        with zipfile.ZipFile(legacy, "w") as zf:
            zf.writestr("config.json", json.dumps(config_payload))
        target = Config()
        summary = backup_mod.restore_backup(
            str(legacy), target, ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        )
        assert summary["config"] is True
        assert target.device_name == "OldBackupBox"
        assert target.hotkeys.get("paste_1") == "Ctrl+1", "defaults must survive a keyless backup"
        assert target.hotkeys_enabled is False

    def test_malformed_hotkey_pairs_filtered_on_restore(self, tmp_path):
        crafted = tmp_path / "bad.zip"
        # JSON object keys are always strings; non-string VALUES are what the
        # strdict filter must drop.
        payload = {"hotkeys": {"paste_1": "F9", "evil": 123, "bad2": None}}
        with zipfile.ZipFile(crafted, "w") as zf:
            zf.writestr("config.json", json.dumps(payload))
        target = Config()
        backup_mod.restore_backup(
            str(crafted), target, ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        )
        assert target.hotkeys.get("paste_1") == "F9"
        assert "evil" not in target.hotkeys
        assert "bad2" not in target.hotkeys

    def test_factory_reset_removes_wal_sidecars(self):
        """Tripwire: the history DB runs in WAL mode with a long-lived
        connection, so deleting only the .db at factory reset leaves a stale
        -wal that SQLite replays into the freshly-created empty DB —
        resurrecting the very history the reset was meant to destroy.
        (The method needs a live app, so pin its source instead.)"""
        from src.main import Application

        src = inspect.getsource(Application._do_factory_reset)
        for needle in (
            '"clipboard_history.db-wal"',
            '"clipboard_history.db-shm"',
            '"clipboard_history.db"',
        ):
            assert needle in src, (
                f"factory reset must delete {needle} (stale WAL resurrects the deleted history)"
            )
        assert ".corrupt-*" in src, (
            "quarantine copies keep the old identity — reset must sweep them"
        )


# ═════════════════════════════════════════════════════════════════════════
# Pair 7/8 conclusions are documented in the round report (settings-search
# highlight restore mutates view attributes only — no Variable writes, no
# command callbacks; dashboard LAN-IP cache is display-only with a 30 s TTL
# refreshed off-thread).  Neither exposes testable logic headlessly without
# a full CTk/Tk display harness.
# ═════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════
# Stage 6 — receive path: no ghost history, no clipboard lost mid-capture
# ═════════════════════════════════════════════════════════════════════════


class _FailingWriter:
    """Clipboard writer that reports (or raises) failure."""

    def __init__(self, mode="false"):
        self.mode = mode
        self.calls = 0

    def write(self, content):
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("clipboard busy")
        return False


class _RecordingHistory:
    def __init__(self):
        self.added = []

    def add(self, content):
        self.added.append(content)


class TestFailedRemoteWriteLeavesNoGhost:
    """A remote message whose clipboard write fails must leave no trace: the
    user cannot tell a history row that landed from one that didn't, and
    clicking the ghost pastes something else entirely."""

    def _mgr(self, writer):
        hist = _RecordingHistory()
        notified = []
        mgr = SyncManager(
            "test-device",
            "Test Device",
            reader=MockClipboardReader(),
            writer=writer,
            monitor=MockClipboardMonitor(),
            history=hist,
        )
        mgr.on_history_change = lambda: notified.append(True)
        return mgr, hist, notified

    def _msg(self, text=b"remote text"):
        return SyncMessage(
            content=ClipboardContent(types={ContentType.TEXT: text}),
            msg_id="m-ghost",
            source_device="peerX",
        )

    def test_write_returning_false_writes_no_history(self):
        writer = _FailingWriter("false")
        mgr, hist, notified = self._mgr(writer)
        assert mgr.handle_remote_message(self._msg()) is False
        assert writer.calls == 1
        assert hist.added == [], "no clipboard, no history row"
        assert notified == [], "and no history_updated push to the web UI"

    def test_write_raising_writes_no_history(self):
        writer = _FailingWriter("raise")
        mgr, hist, notified = self._mgr(writer)
        assert mgr.handle_remote_message(self._msg()) is False
        assert hist.added == []
        assert notified == []

    def test_successful_write_still_records_history(self):
        writer = MockClipboardWriter()
        mgr, hist, notified = self._mgr(writer)
        assert mgr.handle_remote_message(self._msg()) is True
        assert writer.write_count == 1
        assert len(hist.added) == 1
        assert notified == [True]

    def test_clipboard_is_written_before_history(self):
        """Ordering, not just the outcome: history must follow the write."""
        order = []

        class _OrderWriter:
            def write(self, content):
                order.append("clipboard")
                return True

        class _OrderHistory:
            def add(self, content):
                order.append("history")

        mgr = SyncManager(
            "test-device",
            "Test Device",
            reader=MockClipboardReader(),
            writer=_OrderWriter(),
            monitor=MockClipboardMonitor(),
            history=_OrderHistory(),
        )
        assert mgr.handle_remote_message(self._msg()) is True
        assert order == ["clipboard", "history"]


class TestRemoteMessageLosesToAnInFlightLocalCapture:
    """capture_with_retry can spend ~1.4s on rich content, and the debounce
    window is measured from the change event — long expired by then.  A
    remote message arriving in that gap used to overwrite the copy the user
    had just made, so their clipboard content vanished."""

    def test_remote_write_dropped_while_capture_runs(self):
        monitor, reader, writer = (
            MockClipboardMonitor(),
            MockClipboardReader(),
            MockClipboardWriter(),
        )
        mgr = SyncManager(
            "test-device", "Test Device", reader=reader, writer=writer, monitor=monitor
        )

        capture_started = threading.Event()
        release = threading.Event()
        verdicts = []

        real_locked = mgr._do_read_and_send_locked

        def _slow_capture():
            capture_started.set()
            release.wait(timeout=5)
            return real_locked()

        mgr._do_read_and_send_locked = _slow_capture
        reader.content = ClipboardContent(types={ContentType.TEXT: b"local"})
        mgr.on_send = lambda msg: None
        mgr.start()
        try:
            monitor.fire()
            assert capture_started.wait(timeout=5), "capture should have begun"
            # The debounce window from the change event has already elapsed —
            # only the in-flight flag stands between the peer and the user's
            # clipboard.
            msg = SyncMessage(
                content=ClipboardContent(types={ContentType.TEXT: b"remote"}),
                msg_id="m-race",
                source_device="peerX",
            )
            verdicts.append(mgr.handle_remote_message(msg))
        finally:
            release.set()
            mgr.stop()

        assert verdicts == [False], "the local copy in progress must win"
        assert writer.write_count == 0, "the user's clipboard must be untouched"

    def test_flag_is_released_even_when_the_capture_raises(self):
        """A capture that blows up must not wedge the receive path shut."""
        mgr = SyncManager(
            "test-device",
            "Test Device",
            reader=MockClipboardReader(),
            writer=MockClipboardWriter(),
            monitor=MockClipboardMonitor(),
        )

        def _boom():
            raise RuntimeError("reader exploded")

        mgr._do_read_and_send_locked = _boom
        with pytest.raises(RuntimeError):
            mgr._do_read_and_send()
        assert mgr._local_capture_active == 0
