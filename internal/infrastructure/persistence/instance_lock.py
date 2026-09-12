"""OS-released data-directory lock for sidecar instances."""

import json
import os
import uuid
from pathlib import Path


class DataInUseError(RuntimeError):
    """Another process or legacy lock owns the selected data directory."""


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
            if not isinstance(marker, dict) or marker.get("owner") != "tauri-sidecar-v1":
                raise DataInUseError("Close the legacy ClipSync application first")
            # We hold the OS lock, so no compliant sidecar can still own this
            # marker. Unknown legacy markers are never removed here.
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
