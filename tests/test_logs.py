"""The log export behind the native viewer's save dialog.

The export is a contract with the user and with support: the RAW log, copied
byte-for-byte and reported with the source's own size (redaction is the
viewer's job only), with the documented error codes coming back instead of an
exception.
"""

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
