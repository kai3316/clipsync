"""The shared update lifecycle: state machine, staging and the silent check."""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.system import update_service, updater
from internal.system.update_service import (
    UpdateService,
    ready_archive_dir,
    stage_ready_archive,
)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def published():
    return []


@pytest.fixture
def service(published, tmp_path):
    instance = UpdateService(publish=lambda name, data: published.append((name, data)),
                             home=str(tmp_path))
    yield instance
    instance.stop()


def _write_asset(dest_dir, name="clipsync-windows.zip", payload=b"payload"):
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, name)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def test_status_is_idle_and_a_copy(service):
    first = service.status()
    assert first["state"] == {
        "phase": "idle", "fraction": 0, "downloaded": 0, "total": 0,
        "error": "", "version": "", "path": "",
    }
    first["state"]["phase"] = "tampered"
    assert service.status()["state"]["phase"] == "idle"


def test_check_returns_the_bounded_result(service, monkeypatch):
    monkeypatch.setattr(updater, "check_for_update", lambda timeout=None: {
        "available": True, "latest": "v2.0.0", "current": "1.0.0", "url": "https://x",
    })
    assert service.check() == {
        "available": True, "latest": "v2.0.0", "current": "1.0.0", "url": "https://x",
    }


def test_check_gives_up_at_the_wall_bound(service, monkeypatch):
    monkeypatch.setattr(update_service, "CHECK_WALL_BOUND", 0.05)

    def slow(timeout=None):
        time.sleep(1.0)
        return {"available": True, "latest": "v9", "current": "1.0.0", "url": ""}

    monkeypatch.setattr(updater, "check_for_update", slow)
    started = time.monotonic()
    assert service.check() == {"available": False, "latest": "", "current": "", "url": ""}
    assert time.monotonic() - started < 0.5


def test_check_survives_a_failing_lookup(service, monkeypatch):
    def boom(timeout=None):
        raise OSError("offline")

    monkeypatch.setattr(updater, "check_for_update", boom)
    assert service.check()["available"] is False


def test_download_stages_a_verified_archive(service, published, monkeypatch, tmp_path):
    cached = []
    monkeypatch.setattr(updater, "cache_asset", lambda path: cached.append(path))

    def fake_download(dest_dir, progress_cb=None):
        path = _write_asset(dest_dir)
        if progress_cb is not None:
            progress_cb(7, 7)
        return path, None, "2.0.0"

    monkeypatch.setattr(updater, "download_latest_release", fake_download)
    assert service.start_download() == {"ok": True, "started": True, "error": None}
    assert wait_for(lambda: service.status()["state"]["phase"] == "ready")

    state = service.status()["state"]
    assert state["version"] == "2.0.0"
    assert state["fraction"] == 1
    assert state["error"] == ""
    assert Path(state["path"]).parent == ready_archive_dir(str(tmp_path))
    assert Path(state["path"]).read_bytes() == b"payload"
    assert cached and cached[0].endswith("clipsync-windows.zip")
    phases = [data["state"]["phase"] for name, data in published if name == "update.state"]
    assert phases[0] == "downloading"
    assert phases[-1] == "ready"
    assert all(set(data) == {"state"} for _, data in published)


def test_download_failure_surfaces_the_reason(service, monkeypatch):
    monkeypatch.setattr(
        updater, "download_latest_release",
        lambda dest_dir, progress_cb=None: (None, "no asset for this platform", ""),
    )
    service.start_download()
    assert wait_for(lambda: service.status()["state"]["phase"] == "failed")
    assert service.status()["state"]["error"] == "no asset for this platform"


def test_a_rejected_blob_is_discarded(service, monkeypatch, tmp_path):
    path = _write_asset(str(tmp_path))
    monkeypatch.setattr(
        updater, "download_latest_release",
        lambda dest_dir, progress_cb=None: (path, None, "1.0.0"),
    )
    monkeypatch.setattr(updater, "verify_update_blob", lambda *a, **k: (False, "hash_mismatch"))
    service.start_download()
    assert wait_for(lambda: service.status()["state"]["phase"] == "failed")
    assert not Path(path).exists()
    assert service.status()["state"]["error"]


def test_a_peer_blob_is_verified_against_the_release_digest(service, monkeypatch, tmp_path):
    path = _write_asset(str(tmp_path))
    cached, seen = [], {}
    monkeypatch.setattr(
        updater, "fetch_latest_asset_info",
        lambda timeout=None: {"version": "2.0.0", "sha256": "abc"},
    )

    def verify(blob, release_info, current, source="p2p"):
        seen.update(blob=blob, release_info=release_info, source=source)
        return True, "ok"

    monkeypatch.setattr(updater, "verify_update_blob", verify)
    monkeypatch.setattr(updater, "cache_asset", lambda p: cached.append(p))
    service.finish_from_peer(path)
    assert wait_for(lambda: service.status()["state"]["phase"] == "ready")
    assert seen == {
        "blob": path,
        "release_info": {"version": "2.0.0", "sha256": "abc"},
        "source": "p2p",
    }
    # The digest pins the blob to that release, so the ready card can name it.
    assert service.status()["state"]["version"] == "2.0.0"
    assert cached == [path]


def test_a_peer_blob_without_a_release_reference_falls_back_to_the_server(
    service, monkeypatch, tmp_path
):
    path = _write_asset(str(tmp_path))
    downloads = []
    monkeypatch.setattr(updater, "fetch_latest_asset_info", lambda timeout=None: None)
    monkeypatch.setattr(
        updater, "download_latest_release",
        lambda dest_dir, progress_cb=None: (downloads.append(dest_dir), (None, "offline", ""))[1],
    )
    service.finish_from_peer(path)
    assert wait_for(lambda: service.status()["state"]["phase"] == "failed")
    assert len(downloads) == 1
    # Nothing authoritative could check the peer blob, so it is never staged —
    # and, as in the legacy path, it is left in the receive folder rather than
    # deleted behind the user's back.
    assert Path(path).exists()
    assert service.status()["state"]["error"] == "offline"


def test_a_peer_blob_that_fails_the_digest_is_discarded(service, monkeypatch, tmp_path):
    path = _write_asset(str(tmp_path))
    monkeypatch.setattr(
        updater, "fetch_latest_asset_info",
        lambda timeout=None: {"version": "2.0.0", "sha256": "abc"},
    )
    monkeypatch.setattr(updater, "verify_update_blob", lambda *a, **k: (False, "hash_mismatch"))
    service.finish_from_peer(path)
    assert service.status()["state"]["phase"] == "failed"
    assert not Path(path).exists()


def test_a_stash_failure_stays_retryable(service, monkeypatch, tmp_path):
    path = _write_asset(str(tmp_path))
    monkeypatch.setattr(
        updater, "download_latest_release",
        lambda dest_dir, progress_cb=None: (path, None, "2.0.0"),
    )
    monkeypatch.setattr(updater, "cache_asset", lambda _: None)
    monkeypatch.setattr(
        update_service, "stage_ready_archive",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    service.start_download()
    assert wait_for(lambda: service.status()["state"]["phase"] == "failed")
    assert service.status()["state"]["error"]
    assert service._installing is False
    assert Path(path).exists()


def test_progress_updates_state_but_is_throttled(service, published):
    service._on_progress(1, 4)
    service._on_progress(2, 4)
    service._on_progress(3, 4)
    assert service.status()["state"]["downloaded"] == 3
    assert service.status()["state"]["fraction"] == 0.75
    assert [name for name, _ in published] == ["update.state"]
    # Unknown total must not divide by zero.
    service._on_progress(10, 0)
    assert service.status()["state"]["fraction"] == 0


def test_start_download_refuses_a_second_run(service, monkeypatch):
    import threading

    release = threading.Event()

    def slow_download(dest_dir, progress_cb=None):
        release.wait(2)
        return None, "stopped", ""

    monkeypatch.setattr(updater, "download_latest_release", slow_download)
    assert service.start_download()["ok"] is True
    assert service.start_download() == {
        "ok": False, "started": False, "error": "update already in progress",
    }
    release.set()
    assert wait_for(lambda: service.status()["state"]["phase"] == "failed")


def test_shutdown_refuses_new_downloads_and_restart_clears_it(service):
    service.stop()
    assert service.start_download()["error"] == "app is shutting down"
    service.start()
    assert service.start_download()["started"] is True


def test_open_folder_requires_a_staged_archive(service, monkeypatch):
    assert service.open_folder() == {"ok": False, "error": "no ready update"}
    missing = str(ready_archive_dir("x") / "gone.zip")
    service._set_state(phase="ready", path=missing)
    assert service.open_folder() == {"ok": False, "error": "ready file missing"}


def test_open_folder_reveals_the_staged_archive(service, monkeypatch, tmp_path):
    revealed = []
    path = _write_asset(str(tmp_path))
    service._set_state(phase="ready", path=path)
    monkeypatch.setattr(
        update_service, "reveal_folder", lambda p: (revealed.append(p), (True, str(tmp_path)))[1]
    )
    assert service.open_folder() == {"ok": True}
    assert revealed == [path]


def test_open_folder_reports_a_failed_reveal(service, monkeypatch, tmp_path):
    path = _write_asset(str(tmp_path))
    service._set_state(phase="ready", path=path)
    monkeypatch.setattr(update_service, "reveal_folder", lambda p: (False, "open_failed"))
    result = service.open_folder()
    assert result["ok"] is False
    assert result["error"]


def test_auto_check_respects_the_setting_and_the_window(published, monkeypatch):
    checks = []
    monkeypatch.setattr(updater, "check_for_update", lambda timeout=None: checks.append(1) or {
        "available": True, "latest": "v2", "current": "1.0.0", "url": "",
    })
    cfg = SimpleNamespace(auto_update_check=False)
    service = UpdateService(publish=lambda n, d: published.append((n, d)), config=lambda: cfg)
    assert service.maybe_auto_check() is False
    assert service._last_auto_check is None  # disabled must not arm the throttle
    cfg.auto_update_check = True
    assert service.maybe_auto_check() is True
    assert wait_for(lambda: published and published[0][0] == "update.available")
    assert published[0][1]["latest"] == "v2"
    assert service.maybe_auto_check() is False  # inside the 6h window
    assert len(checks) == 1
    service.stop()


def test_auto_check_fires_when_uptime_is_below_the_window(published, monkeypatch):
    """A host up for a minute must still get its first automatic check.

    This is the condition every fresh CI runner is in and no long-running dev
    box ever is, which is why the bug below went unseen: ``_last_auto_check``
    was seeded with 0.0 to mean "never checked", but ``time.monotonic()`` counts
    from boot rather than the epoch.  On a host whose uptime is below
    AUTO_CHECK_INTERVAL the sentinel read as "checked a moment ago", so the
    throttle suppressed the very first check -- a laptop rebooted daily could
    never run one at all.
    """
    monkeypatch.setattr(updater, "check_for_update", lambda timeout=None: {
        "available": False, "latest": "1.0.1", "current": "1.0.1", "url": "",
    })
    service = UpdateService(publish=lambda n, d: published.append((n, d)),
                            config=lambda: SimpleNamespace(auto_update_check=True))
    assert service._last_auto_check is None  # not 0.0: see the docstring
    with monkeypatch.context() as scoped:
        # 42 seconds of uptime, far below the 6h window.
        scoped.setattr(update_service.time, "monotonic", lambda: 42.0)
        assert service.maybe_auto_check() is True
    service.stop()


def test_auto_check_is_silent_when_up_to_date(published, monkeypatch):
    monkeypatch.setattr(updater, "check_for_update", lambda timeout=None: {
        "available": False, "latest": "1.0.0", "current": "1.0.0", "url": "",
    })
    service = UpdateService(publish=lambda n, d: published.append((n, d)),
                            config=lambda: SimpleNamespace(auto_update_check=True))
    assert service.maybe_auto_check() is True
    time.sleep(0.1)
    assert published == []
    service.stop()


def test_no_config_means_no_auto_check(published):
    service = UpdateService(publish=lambda n, d: published.append((n, d)))
    assert service.maybe_auto_check() is False


def test_stage_ready_archive_moves_into_downloads(tmp_path):
    source = _write_asset(str(tmp_path / "tmp"))
    dest = stage_ready_archive(source, str(tmp_path))
    assert dest == str(ready_archive_dir(str(tmp_path)) / "clipsync-windows.zip")
    assert Path(dest).exists()
    assert not Path(source).exists()
    assert ready_archive_dir().name == "clipsync-update"
