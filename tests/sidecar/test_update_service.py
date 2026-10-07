"""The shared update lifecycle: staging, verification and the silent check."""

import hashlib
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.i18n import T
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

    def verify(blob, release_info, current, source="p2p", peer_digest=""):
        seen.update(blob=blob, release_info=release_info, source=source, peer_digest=peer_digest)
        return True, "ok"

    monkeypatch.setattr(updater, "verify_update_blob", verify)
    monkeypatch.setattr(updater, "cache_asset", lambda p: cached.append(p))
    service.finish_from_peer(path, sha256="sender-digest")
    assert wait_for(lambda: service.status()["state"]["phase"] == "ready")
    assert seen == {
        "blob": path,
        "release_info": {"version": "2.0.0", "sha256": "abc"},
        "source": "p2p",
        "peer_digest": "sender-digest",
    }
    # The digest pins the blob to that release, so the ready card can name it.
    assert service.status()["state"]["version"] == "2.0.0"
    assert cached == [path]


def test_a_peer_blob_without_a_release_reference_is_refused_not_re_downloaded(
    service, monkeypatch, tmp_path
):
    # No published digest and no digest from the sender: the one arrival with
    # nothing to check it against.  It used to be answered with a release-server
    # download, which is the fetch that had just been shown not to work, and the
    # card sat on a progress bar for a file that was already here — so the
    # refusal is the answer, in its own words, and the blob goes with it.
    path = _write_asset(str(tmp_path))
    downloads = []
    monkeypatch.setattr(updater, "fetch_latest_asset_info", lambda timeout=None: None)
    monkeypatch.setattr(
        updater, "download_latest_release",
        lambda dest_dir, progress_cb=None: (downloads.append(dest_dir), (None, "offline", ""))[1],
    )
    service.finish_from_peer(path)
    assert wait_for(lambda: service.status()["state"]["phase"] == "failed")
    state = service.status()["state"]
    assert downloads == []
    assert not Path(path).exists()
    assert state["error"] == T("notify.update_unverifiable")
    assert state["source"] == "p2p"


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


# ── verify_update_blob: the real branches, not a monkeypatched verifier ──

def _release(version="2.0.0", sha256=""):
    return {"version": version, "sha256": sha256, "asset": "ClipSync_2.0.0_x64-setup.exe"}


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_verify_blob_refuses_a_release_that_is_not_newer(tmp_path):
    path = _write_asset(str(tmp_path))
    digest = _digest(path)
    assert updater.verify_update_blob(
        path, _release(version="1.0.0", sha256=digest), "1.0.0",
        source="p2p", peer_digest=digest,
    ) == (False, "not_newer")


def test_verify_blob_refuses_bytes_that_do_not_match_the_published_digest(tmp_path):
    path = _write_asset(str(tmp_path))
    assert updater.verify_update_blob(
        path, _release(version="2.0.0", sha256="0" * 64), "1.0.0",
        source="p2p", peer_digest="0" * 64,
    ) == (False, "hash_mismatch")


def test_verify_blob_accepts_the_published_digest(tmp_path):
    path = _write_asset(str(tmp_path))
    assert updater.verify_update_blob(
        path, _release(version="2.0.0", sha256=_digest(path)), "1.0.0",
        source="p2p", peer_digest="",
    ) == (True, "ok")


def test_verify_blob_without_release_info_falls_back_to_the_sender_digest(tmp_path):
    path = _write_asset(str(tmp_path))
    assert updater.verify_update_blob(
        path, None, "1.0.0", source="p2p", peer_digest=_digest(path)
    ) == (True, "peer_verified")


def test_verify_blob_without_release_info_or_sender_digest_is_refused(tmp_path):
    path = _write_asset(str(tmp_path))
    assert updater.verify_update_blob(
        path, None, "1.0.0", source="p2p", peer_digest=""
    ) == (False, "no_release_info")


def test_verify_blob_github_download_passes_without_release_info(tmp_path):
    path = _write_asset(str(tmp_path))
    assert updater.verify_update_blob(
        path, None, "1.0.0", source="github"
    ) == (True, "ok")


def test_verify_blob_release_without_a_digest_falls_back_to_the_sender(tmp_path):
    path = _write_asset(str(tmp_path))
    assert updater.verify_update_blob(
        path, _release(version="2.0.0", sha256=""), "1.0.0",
        source="p2p", peer_digest=_digest(path),
    ) == (True, "peer_verified")


# ── release-asset matchers: Linux ARM64 spells the two bundles differently ──

def test_linux_arm64_assets_use_each_bundlers_own_arch_spelling(monkeypatch):
    monkeypatch.setattr(updater, "_machine", lambda: ("Linux", True))
    monkeypatch.setattr(updater, "running_shell", lambda: updater.SHELL_TAURI)
    patterns = updater._asset_matchers()
    assert [p.pattern for p in patterns] == [
        r"^ClipSync_.*_aarch64\.AppImage$",
        r"^ClipSync_.*_arm64\.deb$",
    ]
    assert patterns[0].match("ClipSync_1.0.55_aarch64.AppImage")
    assert patterns[1].match("ClipSync_1.0.55_arm64.deb")
    assert not patterns[0].match("ClipSync_1.0.55_arm64.AppImage")
    assert not patterns[1].match("ClipSync_1.0.55_aarch64.deb")


def test_linux_x64_assets_keep_one_pattern_for_both_bundles(monkeypatch):
    monkeypatch.setattr(updater, "_machine", lambda: ("Linux", False))
    monkeypatch.setattr(updater, "running_shell", lambda: updater.SHELL_TAURI)
    pattern = updater._asset_matchers()[0]
    assert pattern.match("ClipSync_1.0.55_amd64.AppImage")
    assert pattern.match("ClipSync_1.0.55_amd64.deb")
    assert not pattern.match("ClipSync_1.0.55_aarch64.AppImage")


# ── get_cached_asset: a newer .dmg beats the older .app.tar.gz ──

def test_cached_asset_ranks_version_before_pattern_and_mtime(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = cache / "ClipSync_1.0.10_aarch64.app.tar.gz"
    dmg = cache / "ClipSync_1.0.11_aarch64.dmg"
    payload.write_bytes(b"old payload")
    dmg.write_bytes(b"new dmg")
    old = time.time() - 100
    now = time.time()
    # The payload is the preferred pattern *and* newer on disk; the .dmg is the
    # newer release, and that is what has to decide.
    os.utime(dmg, (old, old))
    os.utime(payload, (now, now))
    monkeypatch.setattr(updater, "_machine", lambda: ("Darwin", True))
    monkeypatch.setattr(updater, "running_shell", lambda: updater.SHELL_TAURI)
    monkeypatch.setattr(updater, "_cache_dir", lambda: str(cache))

    assert updater.get_cached_asset() == str(dmg)

    # Same version: the updater payload wins over the .dmg even when the .dmg
    # carries the newer mtime.
    same = cache / "ClipSync_1.0.11_aarch64.app.tar.gz"
    same.write_bytes(b"new payload")
    os.utime(same, (old, old))
    os.utime(dmg, (now, now))
    assert updater.get_cached_asset() == str(same)


# ── a peer's signature: cached, staged, and in the state ─────────────────

def test_cache_signature_round_trips_beside_the_asset(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(updater, "_machine", lambda: ("Windows", False))
    monkeypatch.setattr(updater, "running_shell", lambda: updater.SHELL_TAURI)
    monkeypatch.setattr(updater, "_cache_dir", lambda: str(cache))
    name = "ClipSync_9.9.9_x64-setup.exe"

    assert updater.cache_signature(name, "minisign-text") is True
    assert (cache / f"{name}.sig").read_text(encoding="utf-8") == "minisign-text"
    assert updater.get_cached_signature(name) == "minisign-text"

    # A name that is not one of this shell's installers must not become a path,
    # and an empty signature is nothing to keep.
    assert updater.cache_signature("notes.txt", "minisign-text") is False
    assert updater.cache_signature(name, "   ") is False
    assert updater.get_cached_signature("missing-setup.exe") == ""


def test_a_peer_signature_is_staged_beside_the_archive_and_in_the_state(
    service, monkeypatch, tmp_path
):
    monkeypatch.setattr(updater, "fetch_latest_asset_info", lambda timeout=None: None)
    monkeypatch.setattr(updater, "cache_asset", lambda p: None)
    monkeypatch.setattr(updater, "cache_signature", lambda name, sig: True)
    path = _write_asset(
        str(tmp_path), name="ClipSync_9.9.9_x64-setup.exe", payload=b"release-bytes"
    )
    digest = _digest(path)

    service.finish_from_peer(path, sha256=digest, signature="minisign-text")

    state = service.status()["state"]
    assert state["phase"] == "ready"
    assert state["verified"] == "peer_verified"
    assert state["signature"] == "minisign-text"
    assert Path(f"{state['path']}.sig").read_text(encoding="utf-8") == "minisign-text"


def test_a_peer_blob_without_a_signature_has_no_sig_file_and_an_empty_state(
    service, monkeypatch, tmp_path
):
    monkeypatch.setattr(updater, "fetch_latest_asset_info", lambda timeout=None: None)
    monkeypatch.setattr(updater, "cache_asset", lambda p: None)
    monkeypatch.setattr(updater, "cache_signature", lambda name, sig: True)
    path = _write_asset(str(tmp_path), name="ClipSync_9.9.9_x64-setup.exe")
    digest = _digest(path)

    service.finish_from_peer(path, sha256=digest)

    state = service.status()["state"]
    assert state["phase"] == "ready"
    assert state["verified"] == "peer_verified"
    assert state["signature"] == ""
    assert not Path(f"{state['path']}.sig").exists()
