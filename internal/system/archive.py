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

The run also *reports*: ``create_archive`` takes an optional callback and calls
it with how many files and bytes there are before it starts, then with how far
it has got.  A folder of several gigabytes used to be one sentence -- 正在打包 --
with nothing behind it and no way to tell work from a hang; counting the
members, which the walk already does, is what makes the totals free.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from stat import S_ISREG

# A folder pasted from a peer is unpacked on this machine before it reaches the
# clipboard.  These are the bounds that keep a hostile or corrupt archive from
# turning one paste into a disk-filling operation: the sender builds the zip, but
# the receiver is the side that has to survive it.
MAX_FOLDER_FILES = 20_000
MAX_FOLDER_BYTES = 8 * 1024**3

# What an archiving run reports: ``(files_done, files_total, bytes_done,
# bytes_total)``.  Four numbers rather than a fraction, because the front ends
# say "12/340" — a progress bar alone is what made a multi-gigabyte folder look
# like a hang.
ProgressCallback = Callable[[int, int, int, int], None]


class ArchiveEmptyError(Exception):
    """What was picked holds no files, so there is nothing to send.

    A folder of empty subfolders is this case too: zipping it would produce an
    archive that transfers successfully and arrives with nothing in it, which
    reads to the receiver as a broken send rather than as an empty folder.
    """


def _regular_file_size(path: Path) -> int | None:
    """The byte size of *path* when it is a regular file, else ``None``.

    One ``stat``, where ``is_file()`` and then ``stat().st_size`` would be two:
    for a picked folder of tens of thousands of files that extra call is paid on
    the very wait the progress below exists to report on.

    A path that cannot be stat'ed comes back as "not a file", which is exactly
    what ``is_file()`` said about it before sizes were counted -- a member that
    vanishes between the walk and the write is skipped, and the zip that then
    cannot read it is what reports the loss (see ``create_archive``).
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_size if S_ISREG(info.st_mode) else None


def create_archive(
    paths: str | Path | Sequence[str | Path],
    *,
    dest_dir: str | Path | None = None,
    on_progress: ProgressCallback | None = None,
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

    *on_progress* is called with ``(files_done, files_total, bytes_done,
    bytes_total)``: once with zero done before the first member is written, so
    the caller can say how much work a several-gigabyte folder is *before* it
    starts, and then after every member.  The totals come from the same walk
    that already had to list the members, which is why they are free; the caller
    spaces the calls out, because this layer has no clock and no idea how many
    of them a surface can take.

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

    members: list[tuple[Path, str, int]] = []
    for item in picked:
        size = _regular_file_size(item)
        if size is not None:
            members.append((item, item.name, size))
        elif item.is_dir():
            for path in sorted(item.rglob("*")):
                inner = _regular_file_size(path)
                if inner is not None:
                    members.append((path, str(path.relative_to(item.parent)), inner))
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

    total_bytes = sum(size for _, _, size in members)
    done_bytes = 0
    if on_progress is not None:
        on_progress(0, len(members), 0, total_bytes)
    try:
        with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_DEFLATED) as bundle:
            for written, (path, name, size) in enumerate(members, start=1):
                bundle.write(str(path), name)
                done_bytes += size
                if on_progress is not None:
                    on_progress(written, len(members), done_bytes, total_bytes)
    except BaseException:
        # A failed zip leaves nothing worth keeping; the caller's error message
        # is about what was picked, not about a partial archive beside it.
        archive.unlink(missing_ok=True)
        raise

    return archive, len(members)


def _member_parts(name: str) -> tuple[str, ...]:
    """The safe path components of one zip member, or ValueError.

    A zip written on Windows may spell its separators as backslashes; the
    archive is otherwise valid, so they are normalized before anything is
    resolved.  Absolute names, drive letters, ``.``/``..`` and empty components
    are refused rather than repaired: a member that wants to escape the
    extraction root is not a member this feature has to support.
    """
    normalized = str(name or "").replace("\\", "/")
    if not normalized or normalized.startswith("/") or "\x00" in normalized:
        raise ValueError("unsafe archive member")
    parts = PurePosixPath(normalized).parts
    if not parts:
        raise ValueError("empty archive member")
    for part in parts:
        if part in ("", ".", "..") or part.endswith(":") or ":" in part:
            raise ValueError("unsafe archive member")
    return tuple(parts)


def _unique_folder_path(parent: Path, name: str) -> Path:
    """A path under *parent* that does not exist yet, ``name (1)`` and so on."""
    candidate = parent / name
    counter = 1
    while candidate.exists():
        candidate = parent / f"{name} ({counter})"
        counter += 1
    return candidate


def extract_folder_archive(
    archive_path: str | Path,
    dest_dir: str | Path,
    folder_name: str = "",
) -> Path:
    """Unpack a single-folder zip below *dest_dir* and return the folder.

    The wire sends a folder as a zip whose top level is the folder itself.
    ``folder_name`` is the name the sender advertised, which is the name the
    user expects to see even when the temporary archive was named differently;
    the zip's own top level is the fallback.

    Extraction happens into a private directory beside the final target, is
    fully validated first, and is only moved into place once every member has
    been written.  A failure removes the private directory and leaves nothing
    half-unpacked in the receive folder.  ``ValueError`` covers an archive this
    function refuses (multiple roots, unsafe names, symlinks, too many bytes);
    zip/OS errors are passed through for the caller to fall back on.
    """
    archive = Path(archive_path)
    root = Path(dest_dir)
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(archive), "r") as bundle:
        infos = [info for info in bundle.infolist() if info.filename]
        if not infos:
            raise ValueError("empty archive")
        if len(infos) > MAX_FOLDER_FILES:
            raise ValueError("archive has too many members")
        total = sum(max(int(info.file_size or 0), 0) for info in infos)
        if total > MAX_FOLDER_BYTES:
            raise ValueError("archive unpacks too large")

        roots: set[str] = set()
        plans: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for info in infos:
            parts = _member_parts(info.filename)
            raw_mode = int(info.external_attr or 0) >> 16
            if stat.S_ISLNK(raw_mode):
                raise ValueError("archive contains a symbolic link")
            roots.add(parts[0])
            plans.append((info, parts))
        if len(roots) != 1:
            raise ValueError("archive does not hold exactly one folder")
        archive_root = next(iter(roots))

        staging = Path(tempfile.mkdtemp(prefix=".clipsync-unpack-", dir=str(root)))
        try:
            written = 0
            for info, parts in plans:
                target = staging.joinpath(*parts)
                target_parent = target.parent
                target_parent.mkdir(parents=True, exist_ok=True)
                if not target_parent.resolve().is_relative_to(staging.resolve()):
                    raise ValueError("archive member escapes the staging folder")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                with bundle.open(info, "r") as source, target.open("wb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_FOLDER_BYTES:
                            raise ValueError("archive unpacks too large")
                        output.write(chunk)
                mode = int(info.external_attr or 0) >> 16
                if os.name == "posix" and mode & 0o777:
                    os.chmod(target, mode & 0o777)

            unpacked = staging / archive_root
            if not unpacked.is_dir():
                raise ValueError("archive has no folder root")

            wanted = os.path.basename(str(folder_name or "").strip().rstrip("/\\"))
            if not wanted or wanted in (".", ".."):
                wanted = archive_root
            final = _unique_folder_path(root, wanted)
            os.replace(unpacked, final)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return final


