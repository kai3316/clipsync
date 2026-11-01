"""Filesystem helpers shared across modules.

These consolidate near-identical implementations that used to live in
``sync.file_transfer``, ``sync.nearby_chat`` and ``main``.  They are
deliberately behaviour-preserving: no caller changes what it writes.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path


def safe_remove(path: Path | None) -> None:
    """Best-effort removal of a file/temp path; never raises.

    Accepts ``None`` (a no-op) and tolerates already-missing paths
    (``missing_ok``) as well as permission/other OSErrors.
    """
    if path is None:
        return
    try:  # noqa: SIM105
        Path(path).unlink(missing_ok=True)
    except OSError:
        # Best-effort only — a leftover temp file must not crash the caller.
        pass


def mask_file_name(file_name: str) -> str:
    """Return a privacy-safe file name: only the extension is preserved."""
    if not file_name or file_name == "?":
        return file_name
    ext = os.path.splitext(file_name)[1]
    return f"*{ext}" if ext else "*"


def mask_path(path: str) -> str:
    """Return a privacy-safe path: only the parent directory name is shown."""
    parent = os.path.basename(os.path.dirname(path))
    return f"{parent}/***" if parent else "***"


def unique_export_path(directory: Path, stem: str, suffix: str, *, sep: str = "_") -> Path:
    """Reserve a file name no other export holds, and return it.

    Every export — history JSON/CSV/Markdown, favorites Markdown/text, the
    backup zip — used a second-precision timestamp and then ``open(..., "w")``,
    so two exports of the same format in the same second silently became one:
    the second truncated the first, and the user kept a file that looked fine
    and held half the data they had exported twice.

    The name carries microseconds and a short random tail, and the file itself
    is created with ``O_EXCL`` — so "did some other process take this name in
    the microsecond between choosing it and opening it" is answered by the
    filesystem rather than assumed.  The caller receives an empty file it owns.

    ``sep`` keeps each family's existing naming habit: the backup archive has
    always been ``clipsync_backup_...`` while history and favorites exports have
    always been ``clipsync-history-...``.  A collision fix is no reason to
    rename files the user already has in their Downloads folder.
    """
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = datetime.now(UTC).strftime(f"%Y%m%d{sep}%H%M%S{sep}%f")
        candidate = directory / f"{stem}{sep}{stamp}{sep}{uuid.uuid4().hex[:6]}{suffix}"
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return candidate
        except FileExistsError:
            continue
