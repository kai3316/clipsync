"""The shared update lifecycle: staging, verification and the silent check."""

import os
import threading
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


def test_stop_waits_for_a_request_already_in_flight(published, monkeypatch):
    """A request in flight at shutdown has to be waited for, not abandoned.


    The periodic tick hands its lookup to a second thread so that a slow answer
    cannot delay the next tick.  That thread was never held anywhere, so stop()
    joined only the tick loop -- which returns at once -- and the process went
    on to finalize its interpreter underneath a live urlopen.  On CI that is not
    a leak but a crash: the sidecar test that starts this mode, asks for status
    and closes stdin died of SIGSEGV (returncode -11) instead of exiting 0.
    """
    entered = threading.Event()
    release = threading.Event()

    def slow_check(timeout=None):
        entered.set()
        release.wait(5.0)
        return {"available": False, "latest": "", "current": "", "url": ""}

    monkeypatch.setattr(updater, "check_for_update", slow_check)
    service = UpdateService(publish=lambda n, d: published.append((n, d)),
                            config=lambda: SimpleNamespace(auto_update_check=True))
    assert service.maybe_auto_check() is True
    assert entered.wait(5.0), "the check never started"

    stopper = threading.Thread(target=service.stop, daemon=True)
    stopper.start()
    # stop() must still be blocked: the request it started has not answered.
    stopper.join(timeout=0.3)
    assert stopper.is_alive(), "stop() returned with the request still in flight"

    release.set()
    stopper.join(timeout=5.0)
    assert not stopper.is_alive(), "stop() never returned"
    assert service._worker is None


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


def test_stage_ready_archive_moves_into_downloads(tmp_path):
    source = _write_asset(str(tmp_path / "tmp"))
    dest = stage_ready_archive(source, str(tmp_path))
    assert dest == str(ready_archive_dir(str(tmp_path)) / "clipsync-windows.zip")
    assert Path(dest).exists()
    assert not Path(source).exists()
    assert ready_archive_dir().name == "clipsync-update"
