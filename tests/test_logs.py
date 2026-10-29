"""The log export behind the native viewer's save dialog.

The export is a contract with the user and with support: the RAW log, copied
byte-for-byte and reported with the source's own size (redaction is the
viewer's job only), with the documented error codes coming back instead of an
exception.
"""

import contextlib
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.data import logs as logs_module
from internal.data.logs import export_log


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    from internal.config import config as config_module

    directory = tmp_path / "logs"
    directory.mkdir()
    monkeypatch.setattr(config_module, "_log_dir", lambda: directory)
    return directory


def test_export_log_copies_the_raw_log_and_reports_its_size(log_dir, tmp_path):
    (log_dir / "clipsync.log").write_text("line one\nline two\n", encoding="utf-8")
    dest = tmp_path / "out" / "clipsync_export.log"
    dest.parent.mkdir()
    result = export_log(str(dest))
    # Byte-for-byte: the reported size is the source's, not a re-encoding.
    assert result == {
        "ok": True, "path": str(dest), "bytes": (log_dir / "clipsync.log").stat().st_size,
    }
    # The export is the raw file: the redacted tail is only for the viewer.
    assert dest.read_text(encoding="utf-8") == "line one\nline two\n"


def test_export_log_reports_a_missing_log(log_dir, tmp_path):
    assert export_log(str(tmp_path / "out.log")) == {"ok": False, "error": "LOG_NOT_FOUND"}


def test_export_log_refuses_an_empty_or_directory_destination(log_dir, tmp_path):
    (log_dir / "clipsync.log").write_text("x\n", encoding="utf-8")
    assert export_log("") == {"ok": False, "error": "EXPORT_FAILED"}
    assert export_log(str(tmp_path)) == {"ok": False, "error": "EXPORT_FAILED"}


def test_export_log_maps_permission_denied(log_dir, tmp_path, monkeypatch):
    (log_dir / "clipsync.log").write_text("x\n", encoding="utf-8")

    def deny(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(logs_module.shutil, "copy2", deny)
    assert export_log(str(tmp_path / "out.log")) == {
        "ok": False, "error": "PERMISSION_DENIED",
    }


def test_share_copy_is_the_redacted_whole_log(log_dir):
    """The copy handed to a peer is redacted the way the viewer's tail is.

    The opposite rule from `export_log`, and the one that matters: this file
    leaves the machine on its own motion, to a device that may never have been
    paired with this one, so no path, token or pairing password goes with it.
    The whole log rather than the tail, because the reason the tail is capped —
    not loading an oversized file into memory — does not apply to a writer that
    streams.
    """
    cfg = SimpleNamespace(
        web_token="tok-abcdefgh", relay_password="relay-secret-1",
        netpair_password="netpair-secret",
    )
    home = os.path.expanduser("~")
    (log_dir / "clipsync.log").write_text(
        f"2026-09-20 10:00:00.000 [INFO    ] MainThread   a:1  opened {home}/Downloads\n"
        "2026-09-20 10:00:01.000 [WARNING ] MainThread   b:2  token tok-abcdefgh\n"
        "2026-09-20 10:00:02.000 [ERROR   ] MainThread   c:3  relay relay-secret-1\n",
        encoding="utf-8",
    )
    copy = logs_module.write_share_copy(cfg)
    assert copy is not None
    # Under the log directory's own `share/` folder, so the sweep and the
    # transfer both work from the directory the log already lives in.
    assert copy.parent == log_dir / logs_module.SHARE_DIR_NAME
    text = copy.read_text(encoding="utf-8")
    assert home not in text
    assert "tok-abcdefgh" not in text
    assert "relay-secret-1" not in text
    assert "netpair-secret" not in text
    # Every line survives: this is the whole log, not the error lines only.
    assert len(text.splitlines()) == 3


def test_share_copy_sweeps_copies_a_transfer_never_took(log_dir):
    """A copy left behind by a send that never finished does not accumulate.

    The share directory is written to and read from by the transfer, and a peer
    that disconnects mid-send leaves the file.  The sweep runs when the next
    copy is written rather than on a timer.
    """
    cfg = SimpleNamespace(
        web_token="", relay_password="", netpair_password="",
    )
    share = log_dir / logs_module.SHARE_DIR_NAME
    share.mkdir()
    stale = share / "clipsync-1000000000.log"
    stale.write_text("old\n", encoding="utf-8")
    os.utime(stale, (time.time() - logs_module.SHARE_MAX_AGE - 60,) * 2)
    (log_dir / "clipsync.log").write_text("now\n", encoding="utf-8")

    logs_module.write_share_copy(cfg)
    assert not stale.exists()


def test_share_copy_is_absent_when_there_is_no_log(log_dir):
    """A machine that has never written a log has nothing to answer with.

    A different answer from a device that has sharing switched off, which the
    runtime says with `log_denied` rather than by sending nothing.
    """
    assert logs_module.write_share_copy(SimpleNamespace(web_token="")) is None


def test_collected_logs_are_named_for_the_device_that_sent_them(tmp_path):
    """Several logs are collected at once, so the folder has to say whose is which.

    The name is the device's and the time it was filed, and it is built from a
    peer-supplied string: the separators, the reserved characters and the
    leading dots a path cannot start with are all taken out, and a name that is
    nothing but those still leaves a file rather than failing.
    """
    saved = tmp_path / "received.log"
    saved.write_text("came from a peer\n", encoding="utf-8")
    path = logs_module.stage_collected_log(str(saved), 'a/b:c"d', home=str(tmp_path))
    filed = Path(path)
    assert filed.parent == tmp_path / "Downloads" / "ClipSync-logs"
    assert filed.name.startswith("a-b-c-d-")
    assert filed.suffix == ".log"
    assert filed.read_text(encoding="utf-8") == "came from a peer\n"
    # The staged copy is moved, not copied: the receive directory is not left
    # holding a second copy of every log that arrives.
    assert not saved.exists()

    # A second collection of the same device at the same second gets its own
    # file rather than overwriting the first.
    again = tmp_path / "received.log"
    again.write_text("second\n", encoding="utf-8")
    second = logs_module.stage_collected_log(str(again), "a/b:c\"d", home=str(tmp_path))
    assert second != path
    assert Path(second).read_text(encoding="utf-8") == "second\n"


# —— the file handler's own level ————————————————————————————————————


def _file_handler():
    root = logging.getLogger()
    return next(
        (
            handler
            for handler in root.handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ),
        None,
    )


@pytest.fixture
def file_logging(tmp_path, monkeypatch):
    """Run setup_file_logging against a private directory, then clean up.

    The handler it adds lives on the process root logger, so the test puts the
    handler list and the root level back afterwards; otherwise every later test
    in the process inherits both.
    """
    from internal.config import config as config_module

    directory = tmp_path / "logs"
    directory.mkdir()
    monkeypatch.setattr(config_module, "_log_dir", lambda: directory)
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    # A handler another test started for real (the entrypoint tests call
    # setup_file_logging) would make setup_file_logging return early and point
    # the assertions at that file; put it aside and restore it afterwards.
    stale = [handler for handler in root.handlers if isinstance(handler, logging.FileHandler)]
    for handler in stale:
        root.removeHandler(handler)
    yield directory
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
    for handler in stale:
        root.addHandler(handler)
    root.setLevel(before_level)


def test_the_default_file_level_matches_the_config_default():
    """Two defaults, one meaning: a fresh config and a fresh handler are INFO."""
    from internal.config.config import Config

    assert logging.getLevelName(Config().log_level) == logs_module.DEFAULT_FILE_LEVEL


def test_setup_file_logging_starts_the_file_handler_at_info(file_logging):
    """DEBUG on the root must not mean DEBUG in the file.

    The root logger has to stay at DEBUG for a DEBUG selection to reach the
    handler at all, but the handler's own level decides what is written, and the
    config default is INFO.  Before this, both were DEBUG and selecting INFO in
    the settings page changed nothing.
    """
    assert logs_module.setup_file_logging()
    handler = _file_handler()
    assert handler is not None
    assert handler.level == logging.INFO
    assert logging.getLogger().level == logging.DEBUG


def test_setup_file_logging_leaves_the_handlers_it_found_alone(file_logging):
    """The file level is the file handler's; stderr keeps its own WARNING."""
    root = logging.getLogger()
    before = {id(handler): handler.level for handler in root.handlers}

    assert logs_module.setup_file_logging()

    for handler in root.handlers:
        if id(handler) in before:
            assert handler.level == before[id(handler)]
    added_streams = [
        handler
        for handler in root.handlers
        if id(handler) not in before
        and isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
    ]
    for handler in added_streams:
        assert handler.level == logging.WARNING


def test_the_default_file_level_keeps_debug_out_of_the_file(file_logging):
    assert logs_module.setup_file_logging()
    log = logging.getLogger("internal.data.logs.test")
    log.debug("debug-line-must-not-land-123")
    log.info("info-line-must-land-123")
    _file_handler().flush()

    text = (file_logging / logs_module.LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "info-line-must-land-123" in text
    assert "debug-line-must-not-land-123" not in text


def test_set_file_log_level_switches_and_falls_back_to_info(file_logging):
    assert logs_module.setup_file_logging()
    handler = _file_handler()

    assert logs_module.set_file_log_level("DEBUG") is True
    assert handler.level == logging.DEBUG
    logging.getLogger("internal.data.logs.test").debug("debug-line-now-lands-456")
    handler.flush()
    assert "debug-line-now-lands-456" in (
        file_logging / logs_module.LOG_FILE_NAME
    ).read_text(encoding="utf-8")

    assert logs_module.set_file_log_level(logging.ERROR) is True
    assert handler.level == logging.ERROR

    # A value the config should never hold still gets the documented default.
    assert logs_module.set_file_log_level("banana") is True
    assert handler.level == logging.INFO


def test_setup_file_logging_applies_the_level_it_was_given(file_logging):
    assert logs_module.setup_file_logging(file_level="DEBUG")
    assert _file_handler().level == logging.DEBUG
    # Idempotent, but the second call's level is not ignored.
    assert logs_module.setup_file_logging(file_level="WARNING")
    assert _file_handler().level == logging.WARNING


def test_saving_log_level_applies_it_to_the_live_handler(file_logging, monkeypatch):
    """The settings page's save has to reach the handler, not just the config.

    ``setup_file_logging`` opens the handler before the config exists, so its
    level starts at the default; the web API's ``update_settings`` is the one
    function both the HTTP and the sidecar-RPC settings paths run through, and
    this is the call that keeps a saved level from waiting for a restart.
    """
    from internal.config.config import Config
    from internal.web.api import settings as settings_api

    assert logs_module.setup_file_logging()
    handler = _file_handler()
    assert handler.level == logging.INFO

    monkeypatch.setattr(
        settings_api, "_persist_preserving_at_rest_private_key", lambda _cfg: None
    )
    cfg = Config()
    result, status = settings_api.update_settings(
        b'{"log_level": "DEBUG"}', cfg, on_settings_change=None, enc_mgr=None
    )

    assert (status, result["ok"]) == (200, True)
    assert cfg.log_level == "DEBUG"
    assert handler.level == logging.DEBUG


def test_set_file_log_level_reports_when_no_file_handler_exists():
    root = logging.getLogger()
    removed = [handler for handler in root.handlers if isinstance(handler, logging.FileHandler)]
    for handler in removed:
        root.removeHandler(handler)
    try:
        assert logs_module.set_file_log_level("DEBUG") is False
    finally:
        for handler in removed:
            root.addHandler(handler)


# —— collected logs are bounded ———————————————————————————————————


def _collected_copy(path: Path, when: float) -> Path:
    path.write_text("copy\n", encoding="utf-8")
    os.utime(path, (when, when))
    return path


def test_collected_logs_keep_the_newest_five_per_device(tmp_path):
    """One busy phone cannot evict a laptop's logs: the sweep is per device."""
    target = tmp_path / "ClipSync-logs"
    target.mkdir()
    now = time.time()
    laptop = [
        _collected_copy(target / f"laptop-2026010{index + 1}-101010.log", now - (8 - index) * 60)
        for index in range(7)
    ]
    phone = [
        _collected_copy(target / f"phone-2026010{index + 1}-101010.log", now - (3 - index) * 60)
        for index in range(3)
    ]
    # The same-second collision suffix is that device's newest copy, not a
    # device called "laptop-20260107-101010".
    collision = _collected_copy(target / "laptop-20260107-101010-2.log", now)

    logs_module._prune_collected(target)

    kept = {path.name for path in target.glob("laptop-*.log")}
    assert len(kept) == logs_module.COLLECTED_KEEP_PER_DEVICE
    assert collision.name in kept
    assert all(path.exists() for path in laptop[3:])
    assert all(not path.exists() for path in laptop[:3])
    assert all(path.exists() for path in phone)


def test_collected_logs_older_than_thirty_days_are_dropped(tmp_path):
    target = tmp_path / "ClipSync-logs"
    target.mkdir()
    now = time.time()
    old = _collected_copy(
        target / "laptop-20251201-101010.log",
        now - (logs_module.COLLECTED_MAX_AGE_DAYS + 1) * 86400,
    )
    fresh = _collected_copy(target / "laptop-20260101-101010.log", now)

    logs_module._prune_collected(target)

    assert not old.exists()
    assert fresh.exists()


def test_staging_a_collected_log_leaves_five_for_that_device(tmp_path):
    """The sweep runs on the way in, counting the file that is about to land.

    Five existing copies plus the new one would be six, so the incoming device
    keeps one slot free for it.
    """
    target = tmp_path / "Downloads" / "ClipSync-logs"
    target.mkdir(parents=True)
    now = time.time()
    existing = [
        _collected_copy(target / f"laptop-2026010{index + 1}-101010.log", now - (5 - index) * 60)
        for index in range(5)
    ]
    saved = tmp_path / "received.log"
    saved.write_text("came from a peer\n", encoding="utf-8")

    filed = Path(logs_module.stage_collected_log(str(saved), "laptop", home=str(tmp_path)))

    names = {path.name for path in target.glob("laptop-*.log")}
    assert len(names) == logs_module.COLLECTED_KEEP_PER_DEVICE
    assert filed.name in names
    assert existing[0].name not in names, "the oldest copy made room"
    assert not saved.exists()
