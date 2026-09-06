"""Filesystem helpers shared across modules.

These consolidate near-identical implementations that used to live in
``sync.file_transfer``, ``sync.nearby_chat`` and ``main``.  They are
deliberately behaviour-preserving: no caller changes what it writes.
"""

from __future__ import annotations

import os
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
