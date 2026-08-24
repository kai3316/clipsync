"""Round 9 — cross-feature interaction matrix tests.

Pins the interaction behavior between the round-7/8 changes and their
neighbors, pair by pair:

1. 90s flavor-merge x sync send/receive: a remote same-text clip must not be
   swallowed by the sync-layer dedup, must merge (not stack) in history with
   flavors preserved, and merging must never mutate the content object that
   is broadcast/written.  Plain-text-only stripping composes cleanly.
2. History age-prune x pinned x merge timestamp refresh: unpinned rows expire,
   pinned survive, and a merge can only refresh an entry within the 90s
   window — an ancient entry cannot immortalize itself by being re-copied.
3. Timed pause x restart/shutdown/manual toggles: the pause deadline is
   persisted, re-armed with its remaining time on startup, dropped by every
   explicit toggle, and kept across shutdown so it survives an auto-update
   relaunch instead of leaving sync_enabled=False stuck forever.
4. file_request idempotency x reject-then-retry: rejection kills the transfer
   id; a replayed/retried request for that id registers fresh (legitimate
   retries are not swallowed); duplicates mid-receive reset nothing.
5. Speed sampling x cancel-all/pause/completion: samples live on the transfer
   dict itself so every terminal path removes them; a paused receiver records
   no samples.
6. Backup-with-hotkeys x factory reset x old backups: hotkeys round-trip,
   backups without the key keep defaults, malformed pairs are filtered, and
   factory reset removes the WAL sidecars (a stale WAL would resurrect the
   "deleted" history DB).
"""

import inspect
import json
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from internal.clipboard.clipboard import strip_rich_formats
from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.clipboard import history_db as history_db_mod
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.config.config import Config
import internal.config.config as config_module
from internal.data import backup as backup_mod
from internal.sync.file_transfer import CHUNK_SIZE, FileTransferManager
from internal.sync.manager import SyncManager

BODY = b"same body"
RICH_HTML = b"<html><body><b>same body</b></body></html>"


def _rich() -> ClipboardContent:
    return ClipboardContent(types={
        ContentType.TEXT: BODY,
        ContentType.HTML: RICH_HTML,
    })


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


class TestRemoteSameTextReachesReceiver:
    """Full sync-manager path: the loop-prevention dedup hashes ALL formats,
    so a plain variant of a locally-known rich clip is NOT swallowed — it is
    written to the clipboard and merged into history."""

    def test_remote_plain_variant_passes_loop_checks(self, tmp_path):
        monitor, reader, writer = _MockMonitor(), _MockReader(), _MockWriter()
        hist = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        mgr = SyncManager("self-dev", "Self", reader=reader, writer=writer,
                          monitor=monitor, history=hist)
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
        assert previews.count("recopied") == 1, \
            "stale row must be gone despite the same-text re-copy"
        assert h.find_by_id(str(stale_id))[0] is None, \
            "the surviving 'recopied' row is the fresh one, not the stale one"
        assert time.time() - h._entries[0]["timestamp"] < 90, \
            "the fresh copy carries its own new timestamp"


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
        _point_config_at(tmp_path, monkeypatch, {
            "timed_pause_until": "soon",
            "sync_enabled": False,
        })
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
        assert app.cfg.timed_pause_until > time.time(), \
            "shutdown clear keeps the persisted deadline"
        assert len(app._pause_saves) == before, "no extra save during shutdown"

    def test_restore_rearms_remaining_and_expired_resumes(
            self, tmp_path, monkeypatch):
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
            assert app2.cfg.timed_pause_until > time.time(), \
                "deadline must be re-persisted for the next potential restart"
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
        mgr.set_on_transfer_request(
            lambda tid, n, s, m, fn: dialogs.append(tid))
        sends = []
        send_fn = lambda data: sends.append(data)  # noqa: E731

        mgr.handle_message("file_request", self._request(), send_fn, "peerA")
        assert dialogs == [TID]
        mgr.reject_transfer(TID, send_fn)
        assert TID not in mgr._transfers
        assert any(b"file_reject" in d for d in sends), "peer told of reject"

        # Same-id retry (sender resend / broadcast replay after reject).
        sends.clear()
        mgr.handle_message("file_request", dict(self._request()), send_fn,
                           "peerA")
        assert dialogs == [TID, TID], "legitimate retry must prompt again"
        assert mgr._transfers[TID]["state"] == "pending"

        mgr.reject_transfer(TID, send_fn)

    def test_duplicate_request_mid_receive_resets_nothing(self, tmp_path):
        mgr = FileTransferManager(device_id="self", output_dir=str(tmp_path))
        mgr.handle_message("file_request", self._request(),
                           lambda d: True, "peerA")
        mgr.accept_transfer(TID, lambda d: True)
        chunk0 = {"msg_type": "file_chunk", "transfer_id": TID,
                  "chunk_index": 0, "total_chunks": 3,
                  "_raw_data": b"\x01" * CHUNK_SIZE}
        mgr.handle_message("file_chunk", chunk0, lambda d: True, "peerA")
        before = (mgr._transfers[TID]["received_bytes"],
                  mgr._transfers[TID]["received_chunks"])

        # Replay of the request mid-transfer: must not zero the receive window
        # nor reopen a second temp handle.
        mgr.handle_message("file_request", dict(self._request()),
                           lambda d: True, "peerA")
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
        req = {"msg_type": "file_request", "transfer_id": TID,
               "file_name": "paused.bin",
               "file_size": CHUNK_SIZE * total,
               "mime_type": "application/octet-stream", "kind": "file"}
        done = []
        mgr.set_on_transfer_complete(
            lambda tid, ok, canc, status: done.append(status))
        mgr.handle_message("file_request", req, lambda d: True, "peerA")
        mgr.accept_transfer(TID, lambda d: True)

        mgr.pause_transfer(TID, lambda d: True)
        chunk = {"msg_type": "file_chunk", "transfer_id": TID,
                 "chunk_index": 0, "total_chunks": total,
                 "_raw_data": b"\x05" * CHUNK_SIZE}
        mgr.handle_message("file_chunk", chunk, lambda d: True, "peerA")
        t = mgr._transfers[TID]
        assert t["received_chunks"] == 0, "paused chunks are dropped"
        assert not t.get("_rate_samples"), "no progress => no samples"
        assert FileTransferManager._instant_speed(t) == 0.0

        mgr.resume_transfer(TID, lambda d: True)
        for i in range(total):
            mgr.handle_message("file_chunk", {**chunk, "chunk_index": i},
                               lambda d: True, "peerA")

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
        cfg.hotkeys = {"quick_paste": "Ctrl+Alt+P", "paste_1": "F2"}
        cfg.hotkeys_enabled = True
        zip_path = backup_mod.create_backup(cfg, hist,
                                            backup_dir=str(tmp_path / "bk"))
        assert zipfile.is_zipfile(zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            exported = json.loads(zf.read("config.json").decode("utf-8"))
        assert exported["hotkeys"] == {"quick_paste": "Ctrl+Alt+P",
                                       "paste_1": "F2"}
        assert exported["hotkeys_enabled"] is True

        target = Config()
        summary = backup_mod.restore_backup(zip_path, target, hist)
        assert summary["config"] is True
        assert target.hotkeys == {"quick_paste": "Ctrl+Alt+P", "paste_1": "F2"}
        assert target.hotkeys_enabled is True

    def test_old_backup_without_hotkeys_keeps_defaults(self, tmp_path):
        """A pre-hotkeys backup (no such key) must restore cleanly and leave
        the current default bindings intact — never empty them."""
        legacy = tmp_path / "legacy.zip"
        config_payload = {"device_name": "OldBackupBox", "port": 19990}
        with zipfile.ZipFile(legacy, "w") as zf:
            zf.writestr("config.json", json.dumps(config_payload))
        target = Config()
        summary = backup_mod.restore_backup(str(legacy), target,
                                            ClipboardHistoryDB(
                                                storage_path=str(
                                                    tmp_path / "h.db")))
        assert summary["config"] is True
        assert target.device_name == "OldBackupBox"
        assert target.hotkeys.get("quick_paste") == "Ctrl+`", \
            "defaults must survive a keyless backup"
        assert target.hotkeys_enabled is False

    def test_malformed_hotkey_pairs_filtered_on_restore(self, tmp_path):
        crafted = tmp_path / "bad.zip"
        # JSON object keys are always strings; non-string VALUES are what the
        # strdict filter must drop.
        payload = {"hotkeys": {"paste_1": "F9", "evil": 123,
                               "bad2": None}}
        with zipfile.ZipFile(crafted, "w") as zf:
            zf.writestr("config.json", json.dumps(payload))
        target = Config()
        backup_mod.restore_backup(str(crafted), target,
                                  ClipboardHistoryDB(
                                      storage_path=str(tmp_path / "h.db")))
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
        for needle in ('"clipboard_history.db-wal"',
                       '"clipboard_history.db-shm"',
                       '"clipboard_history.db"'):
            assert needle in src, (
                f"factory reset must delete {needle} (stale WAL resurrects "
                "the deleted history)")
        assert ".corrupt-*" in src, \
            "quarantine copies keep the old identity — reset must sweep them"


# ═════════════════════════════════════════════════════════════════════════
# Pair 7/8 conclusions are documented in the round report (settings-search
# highlight restore mutates view attributes only — no Variable writes, no
# command callbacks; dashboard LAN-IP cache is display-only with a 30 s TTL
# refreshed off-thread).  Neither exposes testable logic headlessly without
# a full CTk/Tk display harness.
# ═════════════════════════════════════════════════════════════════════════
