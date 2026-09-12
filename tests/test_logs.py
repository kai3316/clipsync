"""The shared log helpers behind the native viewer and the log export."""

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


def test_export_log_overwrites_an_existing_destination(log_dir, tmp_path):
    (log_dir / "clipsync.log").write_text("fresh\n", encoding="utf-8")
    dest = tmp_path / "old.log"
    dest.write_text("stale content\n", encoding="utf-8")
    assert export_log(str(dest))["ok"] is True
    assert dest.read_text(encoding="utf-8") == "fresh\n"


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


def test_export_log_maps_other_os_errors(log_dir, tmp_path, monkeypatch):
    (log_dir / "clipsync.log").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        logs_module.shutil, "copy2",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert export_log(str(tmp_path / "out.log")) == {
        "ok": False, "error": "EXPORT_FAILED",
    }


def test_export_log_maps_a_failed_size_lookup_to_an_error(log_dir, tmp_path, monkeypatch):
    (log_dir / "clipsync.log").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        logs_module.os.path, "getsize",
        lambda _path: (_ for _ in ()).throw(OSError("size unavailable")),
    )
    assert export_log(str(tmp_path / "out.log")) == {
        "ok": False, "error": "EXPORT_FAILED",
    }
