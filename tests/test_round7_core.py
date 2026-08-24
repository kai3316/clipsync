"""Round-7 desktop-core tests.

Covers:
1. Same-text dedup flavor preservation (history.py + history_db.py):
   a plain-text re-copy must never downgrade a rich entry, and a late
   rich write must upgrade a plain one.
2. Config save durability: temp-file fsync happens before os.replace.
3. NotificationManager hardening: send_pipe guard, no-tray fallback path.
4. Tray timed-pause helpers and menu structure.
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.clipboard.dedup import (
    adds_new_flavors,
    labels_to_types,
    merge_types,
    types_to_labels,
)
from internal.clipboard.format import ClipboardContent, ContentType
from internal.clipboard.history import ClipboardHistory
from internal.clipboard.history_db import ClipboardHistoryDB

RICH_HTML = b"<html><body><b>same body</b></body></html>"
BODY = b"same body"


def _content(types: dict) -> ClipboardContent:
    return ClipboardContent(types=types)


def _rich() -> ClipboardContent:
    return _content({ContentType.TEXT: BODY, ContentType.HTML: RICH_HTML})


def _plain() -> ClipboardContent:
    return _content({ContentType.TEXT: BODY})


def _age_last_add(h, seconds: float) -> None:
    """Back-date the coalesce bookkeeping so the next same-key add falls
    outside DEDUP_WINDOW but stays inside FLAVOR_MERGE_WINDOW."""
    h._last_dedup_time = time.time() - seconds
    h._entries[0]["timestamp"] = time.time() - seconds


# ── 1a. Flavor merge: JSON backend ────────────────────────────────────


class TestFlavorMergeJson:
    def _hist(self, tmp_path):
        return ClipboardHistory(storage_path=str(tmp_path / "h.json"))

    def test_plain_recopy_keeps_rich_flavor(self, tmp_path):
        """The round-6 quirk: re-copying the same text as plain-only within
        the merge window used to stack a plain duplicate over the rich
        entry; it must merge instead, keeping the HTML."""
        h = self._hist(tmp_path)
        h.add(_rich())
        _age_last_add(h, 30)
        h.add(_plain())

        entries = h.get_all()
        assert len(entries) == 1
        types = labels_to_types(entries[0]["types"])
        assert types[ContentType.HTML] == RICH_HTML
        assert types[ContentType.TEXT] == BODY

    def test_late_rich_write_upgrades_plain_entry(self, tmp_path):
        """A rich format written a beat later (within DEDUP_WINDOW) must
        upgrade the entry, not be dropped as a duplicate."""
        h = self._hist(tmp_path)
        h.add(_plain())
        h.add(_rich())  # same text body, <2s apart

        entries = h.get_all()
        assert len(entries) == 1
        types = labels_to_types(entries[0]["types"])
        assert types[ContentType.HTML] == RICH_HTML
        # The richer flavor also wins content_type classification.
        assert entries[0]["content_type"] == "HTML"

    def test_plain_recopy_within_window_stays_dropped(self, tmp_path):
        h = self._hist(tmp_path)
        h.add(_rich())
        h.add(_plain())  # immediate echo of what was just captured

        entries = h.get_all()
        assert len(entries) == 1
        assert ContentType.HTML in labels_to_types(entries[0]["types"])

    def test_merge_refreshes_timestamp_and_orders_first(self, tmp_path):
        h = self._hist(tmp_path)
        h.add(_rich())
        _age_last_add(h, 20)
        old_ts = h._entries[0]["timestamp"]
        h.add(_plain())
        assert h._entries[0]["timestamp"] > old_ts
        assert h.get_all()[0]["text_preview"] == "same body"

    def test_distinct_text_still_creates_new_entry(self, tmp_path):
        h = self._hist(tmp_path)
        h.add(_rich())
        h.add(_content({ContentType.TEXT: b"different"}))
        assert len(h.get_all()) == 2

    def test_merge_window_expiry_starts_fresh_entry(self, tmp_path):
        """Past FLAVOR_MERGE_WINDOW a re-copy is deliberate again and gets
        its own entry (existing dedup semantics unchanged)."""
        h = self._hist(tmp_path)
        h.add(_rich())
        h._last_dedup_time = time.time() - 200
        h._entries[0]["timestamp"] = time.time() - 200
        h.add(_plain())

        entries = h.get_all()
        assert len(entries) == 2
        # Newest entry is the plain re-copy; the original rich one survives.
        assert ContentType.HTML not in labels_to_types(entries[0]["types"])
        assert ContentType.HTML in labels_to_types(entries[-1]["types"])

    def test_image_keys_never_merge_across_entries(self, tmp_path):
        """Only text-body keys merge; different images stay distinct."""
        h = self._hist(tmp_path)
        h.add(_content({ContentType.IMAGE_PNG: b"\x89PNG-one"}))
        h._last_dedup_time = time.time() - 10
        h._entries[0]["timestamp"] = time.time() - 10
        h.add(_content({ContentType.IMAGE_PNG: b"\x89PNG-two"}))
        assert len(h.get_all()) == 2


# ── 1b. Flavor merge: SQLite backend ──────────────────────────────────


class TestFlavorMergeDb:
    def _hist(self, tmp_path):
        return ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))

    def test_plain_recopy_keeps_rich_flavor(self, tmp_path):
        db = self._hist(tmp_path)
        db.add(_rich())
        _age_last_add(db, 30)
        db.add(_plain())

        entries = db.get_all()
        assert len(entries) == 1
        types = labels_to_types(entries[0]["types"])
        assert types[ContentType.HTML] == RICH_HTML
        assert types[ContentType.TEXT] == BODY

    def test_merge_survives_restart(self, tmp_path):
        """The merged row (not just the in-memory list) keeps the flavor."""
        db = self._hist(tmp_path)
        db.add(_rich())
        _age_last_add(db, 30)
        db.add(_plain())

        reopened = ClipboardHistoryDB(storage_path=str(tmp_path / "h.db"))
        entries = reopened.get_all()
        assert len(entries) == 1
        assert ContentType.HTML in labels_to_types(entries[0]["types"])
        assert entries[0]["timestamp"] >= time.time() - 5

    def test_late_rich_write_upgrades_plain_entry(self, tmp_path):
        db = self._hist(tmp_path)
        db.add(_plain())
        db.add(_rich())
        entries = db.get_all()
        assert len(entries) == 1
        assert entries[0]["content_type"] == "HTML"
        assert ContentType.HTML in labels_to_types(entries[0]["types"])

    def test_merge_keeps_pinned_and_paste_count(self, tmp_path):
        db = self._hist(tmp_path)
        db.add(_rich())
        db.batch_set_pinned([db._entries[0]["entry_id"]], True)
        db.increment_paste(str(db._entries[0]["entry_id"]))
        _age_last_add(db, 30)
        db.add(_plain())

        entries = db.get_all()
        assert len(entries) == 1
        assert entries[0]["pinned"] is True
        assert entries[0]["paste_count"] == 1


# ── 1c. dedup.py primitives ──────────────────────────────────────────


class TestDedupPrimitives:
    def test_merge_types_incoming_wins_per_format(self):
        merged, changed = merge_types(
            {ContentType.TEXT: b"old", ContentType.HTML: b"<i>old</i>"},
            {ContentType.TEXT: b"new"},
        )
        assert changed is True
        assert merged[ContentType.TEXT] == b"new"       # newer bytes win
        assert merged[ContentType.HTML] == b"<i>old</i>"  # richer flavor kept

    def test_merge_types_noop_is_unchanged(self):
        t = {ContentType.TEXT: b"x"}
        merged, changed = merge_types(dict(t), dict(t))
        assert changed is False
        assert merged == t

    def test_label_roundtrip(self):
        labels = types_to_labels({ContentType.TEXT: b"x", ContentType.URL: b"http://y"})
        back = labels_to_types(labels)
        assert back == {ContentType.TEXT: b"x", ContentType.URL: b"http://y"}

    def test_labels_to_types_skips_garbage(self):
        out = labels_to_types({"TEXT": "!!!not-base64!!!", "BOGUS": "aa", "URL": None})
        assert out == {}

    def test_adds_new_flavors(self):
        existing = {"TEXT": base64.b64encode(b"x").decode()}
        assert adds_new_flavors(existing, {ContentType.TEXT: b"x"}) is False
        assert adds_new_flavors(existing, {ContentType.TEXT: b"x",
                                           ContentType.HTML: b"h"}) is True


# ── 2. Config save durability ────────────────────────────────────────


class TestConfigFsync:
    def test_save_fsyncs_before_replace(self, monkeypatch, tmp_path):
        import internal.config.config as config_module

        order = []
        real_fsync = os.fsync
        real_replace = os.replace

        def fake_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)

        def fake_replace(a, b):
            order.append("replace")
            return real_replace(a, b)

        monkeypatch.setattr(config_module.os, "fsync", fake_fsync)
        monkeypatch.setattr(config_module.os, "replace", fake_replace)
        monkeypatch.setattr(config_module, "_config_dir", lambda: tmp_path)
        monkeypatch.setattr(config_module, "_config_path",
                            lambda: tmp_path / "config.json")

        cfg = config_module.Config()
        cfg.device_name = "durability probe"
        config_module.save(cfg)

        # flush+fsync must land BEFORE the atomic rename, or a crash between
        # them would leave an empty/partial config.json in place of the old one.
        assert order == ["fsync", "replace"]
        # No stale temp files survive a clean save.
        assert list(tmp_path.glob(".config_tmp_*.json")) == []
        loaded = config_module.load()
        assert loaded.device_id == cfg.device_id
        assert loaded.device_name == "durability probe"


# ── 3. NotificationManager ───────────────────────────────────────────


class TestNotifyHardening:
    def test_send_pipe_without_pipe_is_noop(self):
        from internal.platform.notify import NotificationManager
        m = NotificationManager()
        m.send_pipe(("show_notification", "t", "m"))  # must not raise

    def test_show_without_tray_falls_back_without_raising(self):
        from internal.platform.notify import NotificationManager
        m = NotificationManager()
        m.show("title", "message")  # no pipe, no tray → fallback path

    def test_disabled_manager_shows_nothing(self):
        from internal.platform.notify import NotificationManager
        m = NotificationManager()
        m.enabled = False
        m.show("title", "message")

    def test_set_pipe_swap_stops_previous_sender(self):
        import queue as _queue
        from internal.platform.notify import NotificationManager
        m = NotificationManager()

        class FakePipe:
            def __init__(self):
                self.sent = []

            def send(self, msg):
                self.sent.append(msg)

        p1, p2 = FakePipe(), FakePipe()
        m.set_pipe(p1)
        q1 = m._send_queue
        m.set_pipe(p2)  # swapping pipes must stop the first sender thread
        m.show("t1", "m1")
        deadline = time.time() + 2
        while m._send_queue.qsize() and time.time() < deadline:
            time.sleep(0.01)
        # Whatever happens, both sender threads share nothing: q1 drains to
        # p1 at most, new messages go to p2 — never cross-wired.
        m.show("t2", "m2")
        time.sleep(0.1)
        texts = [s for p in (p1, p2) for s in p.sent]
        for s in texts:
            assert s[0] == "show_notification"


# ── 4. Tray timed pause ──────────────────────────────────────────────


class TestTrayPause:
    def test_pause_left_minutes_math(self):
        pytest.importorskip("pystray")
        from internal.ui.systray import SystrayApp

        now = 1_000_000.0
        app = SystrayApp(device_name="T")
        assert SystrayApp.pause_left_minutes(None, now) is None
        assert SystrayApp.pause_left_minutes(now + 15 * 60, now) == 15
        assert SystrayApp.pause_left_minutes(now + 15 * 60 + 59, now) == 16
        assert SystrayApp.pause_left_minutes(now + 5, now) == 1
        assert SystrayApp.pause_left_minutes(now - 10, now) == 0
        assert app._pause_deadline is None

    @pytest.fixture
    def tray_app(self):
        pystray = pytest.importorskip("pystray")
        from internal.ui.systray import SystrayApp
        return pystray, SystrayApp(device_name="T")

    def _texts(self, menu):
        out = []
        for item in menu.items:
            if item is None or item is getattr(type(menu), "SEPARATOR", object()):
                continue
            out.append(item.text)
        return out

    def test_menu_has_pause_submenu_when_wired(self, tray_app):
        pystray, app = tray_app
        app._on_pause_minutes = lambda m: None  # feature wired
        menu = app._build_full_menu()
        texts = self._texts(menu)
        assert any((t or "").startswith("⏸") for t in texts)
        # Submenu carries the three duration entries.
        for item in menu.items:
            if item is not None and (item.text or "").startswith("⏸"):
                assert len(list(item.submenu.items)) == 3
                break

    def test_paused_menu_shows_countdown_and_resume(self, tray_app):
        pystray, app = tray_app
        app._on_pause_minutes = lambda m: None  # feature wired
        app._on_resume_sync = lambda: None
        app.set_pause_deadline(time.time() + 20 * 60)
        texts = self._texts(app._build_full_menu())
        joined = " | ".join(t or "" for t in texts)
        assert "Paused" in joined          # countdown status line
        assert "Resume Sync Now" in joined
        assert "Pause Sync" not in joined  # submenu replaced by status

    def test_cleared_pause_restores_submenu(self, tray_app):
        pystray, app = tray_app
        app._on_pause_minutes = lambda m: None
        app.set_pause_deadline(time.time() + 60)
        app.set_pause_deadline(None)
        texts = self._texts(app._build_full_menu())
        assert any((t or "").startswith("⏸") for t in texts)

    def test_no_pause_submenu_without_callback(self, tray_app):
        """Backward compatibility: a SystrayApp constructed without the
        pause callbacks (e.g. older host wiring) renders no pause items."""
        pystray, app = tray_app
        texts = self._texts(app._build_full_menu())
        assert not any((t or "").startswith("⏸") for t in texts)


# ── 5. i18n keys exist in both locales ───────────────────────────────


def test_pause_i18n_complete():
    from internal.i18n import LOCALES
    keys = [
        "tray.pause_for", "tray.pause_15m", "tray.pause_30m", "tray.pause_1h",
        "tray.paused_left", "tray.resume_now",
    ]
    for locale, table in LOCALES.items():
        for key in keys:
            assert key in table, f"{key} missing in {locale}"
    # Placeholders render.
    from internal.i18n import T
    assert "7" in T("tray.paused_left", minutes=7)
