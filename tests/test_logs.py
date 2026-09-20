"""The log export behind the native viewer's save dialog.

The export is a contract with the user and with support: the RAW log, copied
byte-for-byte and reported with the source's own size (redaction is the
viewer's job only), with the documented error codes coming back instead of an
exception.
"""

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
