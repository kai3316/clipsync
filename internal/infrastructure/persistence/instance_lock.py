"""OS-released data-directory lock for sidecar instances."""

import json
import os
import uuid
from pathlib import Path

from internal.platform.process import pid_running


class DataInUseError(RuntimeError):
    """Another process or legacy lock owns the selected data directory."""


def _legacy_owner_running(marker: dict) -> bool:
    """Whether a marker naming a legacy process points at one still running.

    The legacy application writes {"pid": N} with an optional "tray_pid" and
    unlinks the file as it exits, so anything still on disk after a crash is an
    orphan.  The pids are the only way to tell the two apart.  Either pid
    counts: a live tray with a dead main process is still a legacy instance
    holding the directory.
    """
    return any(pid_running(marker.get(key)) for key in ("pid", "tray_pid"))


class InstanceLock:
    def __init__(self, directory: Path, *, check_legacy: bool = True):
        self.directory = directory
        self.check_legacy = check_legacy
        self._handle = None
        self._token = None

    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self._handle is not None:
            return
        self._handle = (self.directory / ".sidecar.lock").open("a+b")
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(self._handle.fileno()).st_size == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            if self.check_legacy:
                self._claim_legacy_marker()
        except (OSError, DataInUseError) as exc:
            self._handle.close()
            self._handle = None
            if isinstance(exc, DataInUseError):
                raise
            raise DataInUseError("Another sidecar owns this data") from exc

    def _claim_legacy_marker(self) -> None:
        path = self.directory / ".lock"
        if path.exists():
            try:
                marker = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                marker = {}
            if not isinstance(marker, dict):
                marker = {}
            if _legacy_owner_running(marker):
                raise DataInUseError("Close the legacy ClipSync application first")
            # Either the marker is this sidecar's own, or the legacy process
            # that wrote it is gone.  We hold the OS lock, so no compliant
            # sidecar can still own the directory, and a legacy marker outlives
            # a crash, a kill and a reboot -- refusing on one of those orphans
            # locked the application out of its own data until the file was
            # deleted by hand.
            path.unlink()
        token = uuid.uuid4().hex
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "owner": "tauri-sidecar-v1", "token": token}, stream)
        self._token = token

    def stop(self) -> None:
        if self._handle is not None:
            try:
                path = self.directory / ".lock"
                if self._token is not None and path.exists():
                    marker = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(marker, dict) and marker.get("token") == self._token:
                        path.unlink()
            except (OSError, ValueError):
                pass
            finally:
                self._token = None
                self._handle.close()
                self._handle = None
