"""How a file that lives on another device is described, and asked back for.

A file clipboard entry is a list of absolute paths.  Those bytes mean nothing
on the device they arrive at: the same document sits at a different path there,
or under a different name, or does not exist at all.  So the paths stay where
they were copied and this is what crosses instead — the name, the size, and the
id of the history entry that published it.

**The entry id is what makes the exchange safe in one direction.**  A peer asks
for *an entry*, never for a path, and the machine that published the entry
resolves the paths out of its own history before it sends a byte.  A paired peer
therefore cannot name a file to read; the most it can do is ask again for
something this machine already put on its clipboard and chose to publish.  That
bound is a property of the shape below rather than a check bolted onto it, which
is why the id travels here and not alongside.

**Names are normalised to NFC on the way out.**  The file systems disagree about
how to spell a name with an accent: HFS+ and APFS hand back decomposed (NFD)
bytes, Windows and Linux compose (NFC).  Unnormalised, the same file would show
as two different names depending on which end is looking, and a name copied on a
Mac would arrive on Windows spelled in a way that nothing else on that machine
matches.  Normalising at the boundary — not in each platform reader — is what
keeps the wire's spelling independent of the sender's file system.
"""

import json
import os
import re
import unicodedata

# Bumped only if the shape below changes incompatibly.  A peer that does not
# know a version refuses the entry rather than guessing at it, which is the
# right failure: the id inside it is used to look a row up.
OFFER_VERSION = 1

# How many files one offer may describe.  A clipboard entry can carry a
# directory's worth of paths, and every one of them would go into a single
# JSON frame — with 255-byte names, a large enough drop would push the frame
# past the transport's 10 MB cap and cost the whole entry its place on the
# wire.  Past this many the offer is truncated and says so (``total``), so a
# peer shows the right file count and downloads what was advertised.
MAX_OFFER_ENTRIES = 512

MAX_NAME_CHARS = 255

_UNITS = ("B", "KB", "MB", "GB", "TB")

# A device-letter path in a URL slot: "C:\docs" or "C:/docs".
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def file_name(path: str) -> str:
    """The name a path presents to the other device.

    ``os.path`` rules are the local ones on purpose: these paths came off this
    machine's own clipboard, so its own separator convention is the only one
    that can read them.  A backslash separates on Windows and is an ordinary
    character in a name on POSIX, and only the local rule tells those apart.

    A folder copy often arrives with a trailing separator, which has no
    basename at all — the last real component is taken instead.
    """
    trimmed = path.rstrip("/\\") or path
    name = os.path.basename(trimmed)
    return unicodedata.normalize("NFC", name) or trimmed


def describe(path: str, max_name: int = MAX_NAME_CHARS) -> dict | None:
    """What the wire will say about one path, or None if it cannot be served.

    A path that has gone missing between the copy and the broadcast is left out
    rather than advertised: a peer would otherwise show a name it could never
    receive, and the failure would surface as a dead button at the far end
    instead of as an offer that simply names fewer files.

    Folders are described too.  Every file manager copies one the same way it
    copies a file, so leaving them out would make a folder copy silently
    undownloadable; the sender archives one before sending it, which is what
    the transfer page already does for a folder the user picks by hand.
    """
    try:
        stat = os.stat(path)
    except (OSError, ValueError):
        return None
    is_dir = os.path.isdir(path)
    return {
        "name": file_name(path)[:max_name],
        "size": 0 if is_dir else int(stat.st_size),
        "kind": "dir" if is_dir else "file",
    }


def offer(entry_id, items: list[dict], total: int | None = None) -> bytes:
    """The bytes a peer stores and can later ask back for by id."""
    body: dict = {"v": OFFER_VERSION, "entry": str(entry_id), "files": items}
    if total is not None and total > len(items):
        body["total"] = int(total)
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse(payload: bytes) -> dict | None:
    """The offer a peer sent, or None when it is not one this build knows.

    Anything malformed is refused rather than repaired.  The entry id comes back
    out of this structure and is what a request is matched against, so a payload
    that is not exactly the shape below has no id worth trusting — and the
    caller treats None as "this row cannot be downloaded", which is the honest
    reading of a payload nobody can interpret.
    """
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("v") != OFFER_VERSION:
        return None
    entry = body.get("entry")
    listed = body.get("files")
    if not isinstance(entry, str) or not entry or not isinstance(listed, list):
        return None

    files = []
    for item in listed:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        size = item.get("size")
        files.append(
            {
                "name": name[:MAX_NAME_CHARS],
                "size": int(size) if isinstance(size, int) and size > 0 else 0,
                "kind": "dir" if item.get("kind") == "dir" else "file",
            }
        )

    total = body.get("total")
    if not isinstance(total, int) or total < len(files):
        total = len(files)
    return {"entry": entry, "files": files, "total": total}


def entry_id_of(payload: bytes) -> str:
    """The entry a stored offer points at, or "" when there is none."""
    parsed = parse(payload)
    return parsed["entry"] if parsed else ""


def is_dir_only(payload: bytes) -> bool:
    """Whether every item in a stored offer is a folder.

    Nothing is served for one of these *by path*: the sender archives folders,
    so a download of a folder arrives as one archive and the receiving end
    reports the archive's own name.
    """
    parsed = parse(payload)
    if not parsed or not parsed["files"]:
        return False
    return all(item["kind"] == "dir" for item in parsed["files"])


def human_size(num_bytes: int) -> str:
    """A size written the way the window's own formatter writes one.

    Base 1024 with a one-decimal fraction past a kilobyte, matching
    ``desktop/src/i18n/format.ts`` so the same file is not described as
    "2.4 MB" in one place and "2.38 MB" in another.
    """
    if num_bytes < 0:
        return ""
    value = float(num_bytes)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            break
        value /= 1024
    # A byte count is a whole number by definition; anything above it is an
    # average over a unit and reads wrong without the fraction.
    return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"


def summary(files: list[dict], total: int | None = None) -> str:
    """The one line a remote row shows where a local row shows its path.

    Deliberately the shape the local ``FILE`` preview already uses — "name 等 N
    个文件" — so a file row reads the same whether the file sits on this machine
    or on another one, with the size added: that is the part a reader of a
    remote row, who can see neither the file nor its folder, cannot get any
    other way.
    """
    if not files:
        return ""
    first = files[0]
    if len(files) == 1:
        if first["kind"] == "dir":
            return f"{first['name']} · 文件夹"
        return f"{first['name']} · {human_size(first['size'])}"
    count = total if total and total > len(files) else len(files)
    listed = sum(int(item.get("size") or 0) for item in files if item["kind"] != "dir")
    return f"{first['name']} 等 {count} 个文件 · {human_size(listed)}"


def is_local_path_url(value: bytes | str) -> bool:
    """Whether a URL payload is really a path to a file on some machine.

    A plain file copy reaches the reader as *both* a FILE and a URL on two of
    the three platforms: macOS publishes the file's own ``file://`` address
    beside the file, and the Linux file managers put one in ``text/uri-list``.
    A ``file:`` URI is an absolute path with a scheme in front of it, so
    syncing one would put that path on the wire through the very type that was
    meant to keep paths off it.

    Only the unmistakable forms are refused — a bare protocol-relative URL
    (``//cdn.example.com/x.js``) is a real URL and is left alone, even though it
    is spelled very like a UNC path.
    """
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    text = text.strip()
    if not text:
        return False
    lowered = text.lower()
    return (
        lowered.startswith("file:")
        or text.startswith("\\\\")  # a UNC path, which no URL scheme spells
        or bool(_WINDOWS_DRIVE.match(text))
    )
