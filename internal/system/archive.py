"""Zip what was picked -- one folder, or several files -- so it can be sent.

The wire carries files, so a folder, or a handful of files the user picked in
one go, has to become one file before it can go: the legacy panel zipped it
into a temp file, sent that, and unlinked the archive when the transfer
finished, so these sends did not leak copies into the temp directory.  This
module is the archiving half; the caller keeps the returned path and owns the
unlink (see ``LanRuntime``).

Member names follow the panel's ``_zip_and_send_to_peer``: a picked file keeps
its own name, and a picked folder keeps itself as the archive's top level --
the panel's ``relative_to(p.parent)`` -- so extracting the archive gives back
what was picked rather than a loose pile of its files.  The archive's own name
follows the panel too: the folder's name for one folder, ``files-<N>.zip`` for
several picks.  That name is what the sender shows while it zips and what the
receiver's row reads.
"""

from __future__ import annotations

import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path


class ArchiveEmptyError(Exception):
    """What was picked holds no files, so there is nothing to send.

    A folder of empty subfolders is this case too: zipping it would produce an
    archive that transfers successfully and arrives with nothing in it, which
    reads to the receiver as a broken send rather than as an empty folder.
    """


def create_archive(
    paths: str | Path | Sequence[str | Path], *, dest_dir: str | Path | None = None
) -> tuple[Path, int]:
    """Write *paths* into a new zip and return ``(archive_path, file_count)``.

    *paths* is one picked path or a list of them; a list is the legacy picker's
    multi-select, and becomes one archive so the receiver gets one transfer
    rather than N.  *dest_dir* defaults to the system temp directory.  The
    archive is named after what was picked -- ``Documents`` becomes
    ``Documents-<random>.zip``, several picks become ``files-3-<random>.zip`` --
    and the random part is load-bearing: two sends of the same thing must not
    fight over one file, and a half-written archive from a failed send must not
    be picked up by the next.

    Raises :class:`ArchiveEmptyError` when there is nothing to zip, ``ValueError``
    when nothing was picked at all, and lets ``OSError`` through -- a path that
    is gone between the pick and the send, an unreadable file, no space: the
    caller reports those, and it is the only layer that knows what to tell the
    user.
    """
    picked = (
        [Path(paths)] if isinstance(paths, (str, Path)) else [Path(item) for item in paths]
    )
    if not picked:
        raise ValueError("No paths to archive")

    members: list[tuple[Path, str]] = []
    for item in picked:
        if item.is_file():
            members.append((item, item.name))
        elif item.is_dir():
            members.extend(
                (path, str(path.relative_to(item.parent)))
                for path in sorted(item.rglob("*"))
                if path.is_file()
            )
        else:
            # Picked and then gone.  Sending an archive that quietly holds less
            # than the user picked is the worse failure, so it is raised here
            # and the caller names the path in its message.
            raise FileNotFoundError(str(item))
    if not members:
        raise ArchiveEmptyError(str(picked[0]))

    destination = Path(dest_dir) if dest_dir else Path(tempfile.gettempdir())
    destination.mkdir(parents=True, exist_ok=True)
    base = picked[0].name if len(picked) == 1 else f"files-{len(picked)}"
    # Claimed here so two sends cannot collide, then closed: the with-block makes
    # the handle's lifetime explicit, and delete=False keeps the file for the zip.
    with tempfile.NamedTemporaryFile(
        suffix=".zip", prefix=f"{base}-", dir=str(destination), delete=False
    ) as handle:
        archive = Path(handle.name)

    try:
        with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_DEFLATED) as bundle:
            for path, name in members:
                bundle.write(str(path), name)
    except BaseException:
        # A failed zip leaves nothing worth keeping; the caller's error message
        # is about what was picked, not about a partial archive beside it.
        archive.unlink(missing_ok=True)
        raise

    return archive, len(members)
