"""AI-config sync (refactor round 1) — tool profiles + metadata inventory +
selective file/folder pull.

Each device advertises the AI-tool config files and skills / commands
directories from its ENABLED TOOL PROFILES (internal/sync/ai_profiles.py)
plus its user custom paths.  Paired devices exchange *metadata only* —
relative path, tool key, sha256 prefix, size, mtime — via ``aiconfig_inv``;
either side may then pull an individual file (``aiconfig_req`` →
``aiconfig_data``) or an entire folder, which the receiving side expands from
the peer's cached inventory and pulls file-by-file under one ``batch_id``.

Design constraints:
- Paired peers only.  The transport's unpaired gate already drops these
  frame types from unpaired connections; every handler re-checks pairing as
  defense in depth.
- Never silently overwrite.  The receiver lands pulled content according to
  an explicit ``mode`` chosen in the web UI ("overwrite" | "copy" | "append");
  the manager never picks a destructive default itself.
- Bounded.  Collection skips files > 1 MB and temp junk, caps at
  MAX_ENTRIES per device; served/pulled content is capped at MAX_CONFIG_FILE_SIZE;
  b64 decode failures are discarded.

Protocol v3: inventory entries carry a ``tool`` key AND the stable ``root`` id
of the profile entry they came from, both from the deterministic profile table,
so a receiving device resolves the landing target through the SAME profile.
The ``root`` id is what removes the ambiguity: a ``rel_path`` is relative to
its own root, so ``tool`` alone cannot tell two dir roots of one tool apart
(Claude Code's ``skills`` / ``commands`` / ``agents``).  Positional root
indices stay internal — they shift whenever the table grows, which would land
a peer's file in the wrong folder.

v2 peers (``tool`` only, no ``root``) stay fully supported but degrade to a
best-effort root guess, so a v2 inventory can still be browsed and pulled.
Legacy (pre-v2) peers remain read-only: their root_index inventories are cached
and previewed / pulled via the old request shape.

Wire payloads (JSON):
  aiconfig_inv  {"msg_type","v":3,"device_name": str,"entries": [entry...]}
      entry = {"tool": str, "root": str, "rel_path": str(rel posix),
               "sha256": 16 hex, "size": int, "mtime": float, "is_dir": bool}
      v2 entry ("v":2): same without "root"
      legacy entry (no "v"): {"root_index": int, "path": str, ...}
  aiconfig_req  {"msg_type","tool": str,"root": str,"rel_path": str}   (v3)
                {"msg_type","tool": str,"rel_path": str}               (v2)
                {"msg_type","root_index": int,"rel_path": str}    (legacy)
                {"msg_type","inventory_refresh": true}
  aiconfig_data {"msg_type","tool","root","rel_path","sha256","b64_content",
                 "truncated"}  (or the v2 / legacy forms)
"""

import base64
import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath

from internal.protocol.codec import encode_frame
from internal.sync import ai_profiles

logger = logging.getLogger(__name__)

# Files larger than this are invisible to the feature (never inventoried,
# never served, never landed).  Mirrors the spec's 1 MB cap and keeps every
# aiconfig_data frame far below the transport's 10 MB frame limit even after
# base64 expansion (~1.37x).
MAX_CONFIG_FILE_SIZE = 1024 * 1024
# Hard cap on inventory entries advertised per device — protects both the
# collector (a runaway directory tree) and the wire (inv frame size).
MAX_ENTRIES = 2000
# rel_path / tool / device-name string bounds on the wire.
MAX_PATH_LEN = 512
# Preview responses are truncated to this many bytes of text.
PREVIEW_MAX_BYTES = 64 * 1024
# A peer asking for a fresh inventory makes us re-walk the watch roots, which
# touches the disk.  Peers ask on every panel open, so collapse bursts: within
# this window the cached entry list is good enough to answer with.
REFRESH_COLLECT_THROTTLE = 2.0
# Local-only file manager: reads are capped at the same 64 KB as previews;
# saves are capped at 256 KB — a generous ceiling for an AI-config text file —
# and refuse NUL bytes (binary content).
LOCAL_READ_MAX_BYTES = 64 * 1024
LOCAL_SAVE_MAX_BYTES = 256 * 1024
# Recoverable trash directory, created under the app data dir.  Files are
# MOVED here, never physically deleted, so a mis-click is undoable.
TRASH_DIR_NAME = "aiconfig_trash"
# A pending pull/preview entry expires after this long; late data is dropped.
PENDING_TTL = 60.0
# How long preview() blocks waiting for the peer's aiconfig_data.
PREVIEW_TIMEOUT = 5.0
# Extensions for which "append" mode is allowed (text/markdown only).
APPEND_EXTS = {".txt", ".md", ".markdown"}
_SHA16_RE = re.compile(r"^[0-9a-f]{16}$")
# Filename characters allowed to survive into "<name>.from.<device>.<ext>".
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Collector skip-list: editor/OS temp noise never belongs in an AI-config
# inventory.  Deliberately does NOT skip dotfiles/dot-directories — AI tool
# config routinely lives under paths like ~/.claude/.
_TEMP_SUFFIXES = (".tmp", ".swp")
_TEMP_PREFIXES = ("~$",)
_TEMP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def expand_root(path_str: str, home: str | Path | None = None) -> Path | None:
    """Expand one watch-root entry to an absolute Path, or None if unusable.

    Leading "~" expands against *home* (the caller's real home when None so
    tests can inject a fake one).  No environment-variable expansion: watch
    entries are plain user-typed paths.
    """
    if not isinstance(path_str, str):
        return None
    s = path_str.strip()
    if not s:
        return None
    try:
        if home is not None:
            if s == "~":
                return Path(home)
            if s.startswith("~/") or s.startswith("~\\"):
                return Path(home) / s[2:]
            # "~user" forms are not supported — treat literally.
            return Path(s)
        return Path(s).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None


def is_temp_name(name: str) -> bool:
    """True for editor/OS temp junk excluded from inventories."""
    low = name.lower()
    return low.endswith(_TEMP_SUFFIXES) or name.startswith(_TEMP_PREFIXES) or name in _TEMP_NAMES


def _hash_file(path: Path, max_bytes: int = MAX_CONFIG_FILE_SIZE) -> tuple[str, int] | None:
    """(sha256[:16], size) of up to *max_bytes* of the file, else None."""
    h = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()[:16], total


def collect_roots(
    roots,
    home: str | Path | None = None,
    max_bytes: int = MAX_CONFIG_FILE_SIZE,
    max_entries: int = MAX_ENTRIES,
    include_dirs: bool = False,
) -> list[dict]:
    """Collect inventory entries across watch roots (pure function).

    *roots* is a list of ``(tool, root_id, kind, path)`` tuples as produced by
    ``ai_profiles.effective_roots`` (kind is "file" for a single-config-file
    root — the file itself is the only advertised entry — or "dir" for a
    recursive walk).  Entries are emitted relative to their root using forward
    slashes so they are stable across platforms, each carrying its ``tool`` key
    and the root's stable ``root`` id, plus an internal ``root_index`` (index
    into *roots*, used only to trace a served file back to its root and to
    answer legacy peers).  Output is deterministic (sorted within each root)
    and capped at *max_entries*.

    Duplicates are keyed on (tool, root, rel_path): the same rel under two
    different roots of one tool (``skills/x.md`` and ``commands/x.md``) is two
    distinct files and both are advertised.  Keying on (tool, rel) alone used
    to drop the second one, making it invisible to sync entirely.

    When *include_dirs* is true, subdirectories are also emitted as ``is_dir``
    entries (e.g. a Claude Code skill folder ``my-skill/``) so a file-manager
    UI can show the folder tree and count skills.
    """
    entries: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(roots):
        if len(entries) >= max_entries:
            break
        if not (isinstance(raw, (tuple, list)) and len(raw) == 4):
            continue
        tool, root_id, kind, path_str = raw[0], raw[1], raw[2], raw[3]
        if (
            not isinstance(tool, str)
            or kind not in ("file", "dir")
            or not isinstance(path_str, str)
            or not ai_profiles.valid_root_id(root_id)
        ):
            continue
        root = expand_root(path_str, home=home)
        if root is None:
            continue
        entry_def = ai_profiles.root_entry(tool, root_id)
        excludes = ai_profiles.entry_excludes(entry_def) if entry_def else []

        def excluded(rel: str, excludes: list = excludes) -> bool:
            """Whether *rel* matches an entry's never-collect patterns."""
            if not excludes:
                return False
            pure = PurePosixPath(rel.rstrip("/"))
            for pat in excludes:
                if pure.match(pat) or rel == pat or rel.startswith(pat.rstrip("/") + "/"):
                    return True
            return False

        def add_entry(
            rel: str,
            *,
            is_dir: bool = False,
            size=None,
            mtime=None,
            sha: str = "",
            tool: str = tool,
            root_id: str = root_id,
            index: int = index,
        ) -> bool:
            key = (tool, root_id, rel)
            if key in seen:
                return False  # same tool + root + rel — keep the first
            seen.add(key)
            entry: dict = {
                "tool": tool,
                "root": root_id,
                "path": rel,
                "root_index": index,
                "is_dir": is_dir,
            }
            if is_dir:
                entry["size"] = None
                entry["mtime"] = float(mtime) if mtime is not None else 0.0
                entry["sha256"] = ""
            else:
                entry["size"] = size
                entry["mtime"] = float(mtime)
                entry["sha256"] = sha
            entries.append(entry)
            return True

        if kind == "file":
            # Single-file root (a profile file entry or an existing custom
            # file path): advertises exactly itself.
            try:
                if root.is_symlink():
                    continue
                st = root.stat()
            except OSError:
                continue
            if st.st_size > max_bytes:
                continue
            hashed = _hash_file(root, max_bytes=max_bytes)
            if hashed is None:
                continue
            digest, size = hashed
            add_entry(root.name, size=size, mtime=float(st.st_mtime), sha=digest)
            continue
        if not root.is_dir():
            continue
        found: list[tuple[str, Path, os.stat_result]] = []
        try:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = sorted(
                    d
                    for d in dirnames
                    if not is_temp_name(d)
                    and not (Path(dirpath) / d).is_symlink()
                    and not excluded((Path(dirpath) / d).relative_to(root).as_posix() + "/")
                )
                if include_dirs:
                    for dname in dirnames:
                        if len(entries) >= max_entries:
                            break
                        dpath = Path(dirpath) / dname
                        try:
                            dstat = dpath.stat()
                        except OSError:
                            continue
                        rel = dpath.relative_to(root).as_posix() + "/"
                        add_entry(rel, is_dir=True, mtime=float(dstat.st_mtime))
                for fname in sorted(filenames):
                    if len(found) >= max_entries or len(entries) >= max_entries:
                        break
                    if is_temp_name(fname):
                        continue
                    full = Path(dirpath) / fname
                    try:
                        if full.is_symlink():
                            continue
                        st = full.stat()
                    except OSError:
                        continue
                    if st.st_size > max_bytes:
                        continue
                    rel = full.relative_to(root).as_posix()
                    if excluded(rel):
                        continue
                    found.append((rel, full, st))
                if len(found) >= max_entries or len(entries) >= max_entries:
                    break
        except OSError:
            continue
        for rel, full, st in found:
            if len(entries) >= max_entries:
                break
            hashed = _hash_file(full, max_bytes=max_bytes)
            if hashed is None:
                continue
            digest, size = hashed
            add_entry(rel, size=size, mtime=float(st.st_mtime), sha=digest)
    return entries


def resolve_safe(root: Path, rel: str) -> Path | None:
    """Resolve *rel* inside *root*, refusing any escape attempt.

    Rejects absolute paths, drive prefixes, ".." segments (after normalizing
    both separator styles) and anything whose resolved location falls outside
    the resolved root.  Returns an absolute Path on success — including for a
    not-yet-existing landing target whose parents resolve inside *root*.
    """
    if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH_LEN:
        return None
    if "\x00" in rel:
        return None
    norm = rel.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", norm) or norm.startswith("/") or rel.startswith("\\\\"):
        return None
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except OSError:
        return None
    if resolved != root_resolved and root_resolved not in resolved.parents:
        return None
    return resolved


def open_with_default_app(path: str) -> bool:
    """Open *path* (a file or directory) with the OS default application.

    win32 uses ``os.startfile``, macOS uses ``open``, everything else uses
    ``xdg-open``.  Returns True on success so the REST layer can map failures
    to a 400.
    """
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=True)
        else:
            subprocess.run(["xdg-open", path], check=True)
        return True
    except Exception as exc:
        logger.warning("open_with_default_app(%s) failed: %s", path, exc)
        return False


class AIConfigManager:
    """Owns the local AI-config inventory, peer inventory cache, request
    routing and landing logic.  All public methods are thread-safe."""

    def __init__(self, cfg, send_fn, connected_fn=None, event_fn=None, save_fn=None):
        """*cfg* must expose ``device_id``, ``device_name``,
        ``ai_config_tools``, ``ai_config_custom_paths`` and ``peers`` (dict of
        PeerInfo-like objects with ``paired``/``device_name``); it is read
        live so setting changes take effect without re-wiring.

        *send_fn(peer_id, frame_bytes)* transmits one frame;
        *connected_fn()* returns currently connected peer ids;
        *event_fn(dict)* receives WS-bound events (e.g. aiconfig_file);
        *save_fn()* persists the config after profile changes.
        """
        self._cfg = cfg
        self._send_fn = send_fn
        self._connected_fn = connected_fn
        self._event_fn = event_fn
        self._save_fn = save_fn
        self._lock = threading.RLock()
        # Last locally-collected inventory + timestamp.
        self._local_entries: list[dict] = []
        self._local_collected_at: float = 0.0
        # Last time a *peer-driven* refresh made us re-scan the roots.  Peers
        # ask on every panel open, so the scan is throttled (see REFRESH_*).
        self._last_peer_refresh: float = 0.0
        # peer_id -> {"name", "entries", "fetched_at", "legacy": bool}
        self._peer_inventories: dict[str, dict] = {}
        # (peer_id, proto, tool_or_ri, rel_path) -> pending record.  A pending
        # record carries the landing mode + batch_id; preview records
        # additionally hold a threading.Event + result slot.
        self._pending: dict[tuple, dict] = {}

    # ------------------------------------------------------------ helpers

    def _is_paired(self, peer_id: str | None) -> bool:
        if not peer_id:
            return False
        peer = getattr(self._cfg, "peers", {}).get(peer_id)
        return bool(peer is not None and getattr(peer, "paired", False))

    def _connected_peers(self) -> list[str]:
        try:
            return list(self._connected_fn()) if self._connected_fn else []
        except Exception:
            return []

    def _emit(self, event: dict) -> None:
        if self._event_fn is None:
            return
        try:
            self._event_fn(event)
        except Exception:
            logger.debug("aiconfig event callback failed", exc_info=True)

    def _send_frame(self, payload: dict, peer_id: str) -> bool:
        try:
            frame = encode_frame(payload, source_device=self._cfg.device_id)
            return bool(self._send_fn(peer_id, frame))
        except Exception:
            logger.debug("aiconfig send to %s failed", peer_id[:12], exc_info=True)
            return False

    # ------------------------------------------- effective roots / landing

    def _roots(self) -> list[tuple[str, str, str, str]]:
        """Effective (tool, root_id, kind, path) watch roots from cfg, live.

        Custom paths default to "dir" roots; an existing custom FILE path is
        promoted to a "file" root so it advertises exactly itself.
        """
        roots = ai_profiles.effective_roots(
            list(getattr(self._cfg, "ai_config_tools", [])),
            list(getattr(self._cfg, "ai_config_custom_paths", [])),
        )
        out: list[tuple[str, str, str, str]] = []
        for tool, root_id, kind, path in roots:
            if tool == ai_profiles.CUSTOM_KEY and kind == "dir":
                r = expand_root(path)
                if r is not None and r.is_file():
                    kind = "file"
            out.append((tool, root_id, kind, path))
        return out

    def _local_roots_for(self, tool: str) -> list[tuple[str, str, str, str]]:
        return [rt for rt in self._roots() if rt[0] == tool]

    def _resolve_local_target(
        self, tool: str, rel: str, require_exists: bool, root_id: str | None = None
    ) -> tuple[Path | None, str | None]:
        """The local file a (tool, root_id, rel) maps to, or a clear reason.

        With *root_id* (protocol v3) this is an exact lookup: the peer named the
        profile entry its rel_path is relative to, and the same entry exists on
        this device, so exactly one root can own it and no guessing happens.
        This works because a built-in entry's id comes from the SHARED profile
        table, so "skills" means the same root on every device.

        A CUSTOM path's id is the path itself, which is per-device text and
        therefore not portable: a peer's custom root id will not exist here.
        So an unmatched custom id degrades to the tool-only resolution below
        (exactly what custom paths did before root ids existed) instead of
        failing the pull.  For a real tool an unknown id is a genuine error —
        the peer watches a profile entry this device does not have.

        Without *root_id* (a v2 peer) the tool alone has to identify the root,
        which is ambiguous as soon as the tool has several dir roots.  The
        fallbacks below narrow by what is on disk; anything still tied stays an
        explicit error rather than a silent write into the wrong folder.

        A file-type root owns only its own basename and IS the target; a
        dir-type root owns any rel that resolves inside it.  ``require_exists``
        distinguishes serving (the file must exist) from landing (the target may
        be created by the write).
        """

        def target_in(kind: str, root: Path) -> Path | None:
            norm = rel.replace("\\", "/")
            try:
                if kind == "file":
                    return root if norm == root.name else None
                return resolve_safe(root, rel)
            except OSError:
                return None

        def usable(target: Path) -> bool:
            if not require_exists:
                return True
            try:
                return target.is_file() and not target.is_symlink()
            except OSError:
                return False

        local = self._local_roots_for(tool)

        # v3: the peer named its root, so resolve that one and only that one.
        if root_id is not None:
            known_id = any(rid == root_id for _t, rid, _k, _r in local)
            for _tool, rid, kind, raw in local:
                if rid != root_id:
                    continue
                root = expand_root(raw)
                if root is None:
                    continue
                target = target_in(kind, root)
                if target is None or not usable(target):
                    continue
                return target, None
            # A custom root id is a per-device path and never matches across
            # devices — fall through to the tool-only resolution rather than
            # refusing every custom-path pull.  Any other unmatched id is real.
            if known_id or tool != ai_profiles.CUSTOM_KEY:
                return None, "no_local_root"

        matches: list[tuple[Path, str, Path]] = []  # (target, root kind, root)
        for _tool, _rid, kind, raw in local:
            root = expand_root(raw)
            if root is None:
                continue
            target = target_in(kind, root)
            if target is None or not usable(target):
                continue
            matches.append((target, kind, root))
        if len(matches) == 1:
            return matches[0][0], None
        # An exact file-root name match beats a dir-root path match: a peer's
        # rel_path that names one of OUR file roots can only have come from that
        # same file root on the peer (dir roots advertise paths relative to
        # themselves, never "their own name").  Fall back to the strict
        # ambiguity error only when several file roots tie or none claims it.
        named = [t for t, k, _ in matches if k == "file"]
        if len(named) == 1:
            return named[0], None
        # Several dir roots under one tool all "own" the same relative path.
        # The root that already holds this rel's parent is the one the peer's
        # copy mirrors, and failing that a single existing root is the only
        # possible landing site.
        dirs = [(t, r) for t, k, r in matches if k == "dir"]
        if len(dirs) > 1:

            def _exists(p: Path) -> bool:
                try:
                    return p.is_dir()
                except OSError:
                    return False

            holding = [t for t, _ in dirs if _exists(t.parent)]
            if len(holding) == 1:
                return holding[0], None
            live = [t for t, r in dirs if _exists(r)]
            if len(live) == 1:
                return live[0], None
        if matches:
            return None, "ambiguous_tool_root"
        return None, "no_local_root"

    # ---------------------------------------------------------- inventory

    def collect(self) -> list[dict]:
        """Re-scan the watch roots and remember the result."""
        entries = collect_roots(self._roots(), include_dirs=True)
        with self._lock:
            self._local_entries = entries
            self._local_collected_at = time.time()
        return entries

    def build_inv_payload(self) -> dict:
        with self._lock:
            entries = [dict(e) for e in self._local_entries]
        wire_entries = []
        for e in entries:
            item = {
                "tool": e.get("tool", ai_profiles.CUSTOM_KEY),
                "root": e.get("root", ""),
                "rel_path": e["path"],
                "sha256": e.get("sha256", ""),
                "size": e.get("size"),
                "mtime": e.get("mtime"),
            }
            if e.get("is_dir"):
                item["is_dir"] = True
            wire_entries.append(item)
        return {
            "msg_type": "aiconfig_inv",
            "v": 3,
            "device_name": str(getattr(self._cfg, "device_name", "")),
            "entries": wire_entries,
        }

    def send_inventory_to(self, peer_id: str) -> bool:
        """Push our inventory to one paired, connected peer."""
        if not self._is_paired(peer_id):
            return False
        return self._send_frame(self.build_inv_payload(), peer_id)

    def refresh_and_broadcast(self) -> int:
        """Collect + push the inventory to every connected paired peer.

        Returns the number of peers the frame was handed to."""
        self.collect()
        sent = 0
        for pid in self._connected_peers():
            if self.send_inventory_to(pid):
                sent += 1
        return sent

    def on_watch_list_changed(self) -> int:
        """Watch roots were edited (web settings / API) — recollect + rebroadcast."""
        return self.refresh_and_broadcast()

    # --------------------------------------------------- message handling

    def handle_message(self, msg_type: str, payload, peer_id: str | None) -> None:
        """Route one decoded aiconfig frame.  Unpaired senders are ignored."""
        if msg_type not in ("aiconfig_inv", "aiconfig_req", "aiconfig_data", "aiconfig_err"):
            return
        if not isinstance(payload, dict):
            return
        if not self._is_paired(peer_id):
            logger.debug("Ignoring %s from unpaired/unknown sender %.12s", msg_type, peer_id or "?")
            return
        if msg_type == "aiconfig_inv":
            self._handle_inv(payload, peer_id or "")
        elif msg_type == "aiconfig_req":
            self._handle_req(payload, peer_id or "")
        elif msg_type == "aiconfig_err":
            self._handle_err(payload, peer_id or "")
        else:
            self._handle_data(payload, peer_id or "")

    def _sanitize_entry(self, entry) -> dict | None:
        """Validate one wire inventory entry; None = drop it.

        ``root`` is the v3 root id.  A v2 peer omits it, which is legal — the
        entry is kept with an empty root and resolved by the tool-only
        fallback, so a v2 inventory stays browsable and pullable.
        """
        if not isinstance(entry, dict):
            return None
        rel = entry.get("rel_path") or entry.get("path")
        tool = entry.get("tool")
        root = entry.get("root")
        sha = entry.get("sha256")
        size = entry.get("size")
        mtime = entry.get("mtime")
        is_dir = entry.get("is_dir") is True
        if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH_LEN:
            return None
        if ".." in rel.replace("\\", "/").split("/"):
            return None
        if isinstance(tool, bool) or not isinstance(tool, str) or not tool or len(tool) > 64:
            return None
        if root is None or root == "":
            root = ""
        elif not ai_profiles.valid_root_id(root):
            return None
        if (
            isinstance(mtime, bool)
            or not isinstance(mtime, (int, float))
            or mtime != mtime
            or mtime in (float("inf"), float("-inf"))
        ):
            return None
        if is_dir:
            if not rel.endswith("/"):
                return None
            return {
                "tool": tool,
                "root": root,
                "rel_path": rel,
                "is_dir": True,
                "size": None,
                "mtime": float(mtime),
                "sha256": "",
            }
        if not isinstance(sha, str) or not _SHA16_RE.match(sha):
            return None
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not (0 <= size <= MAX_CONFIG_FILE_SIZE)
        ):
            return None
        return {
            "tool": tool,
            "root": root,
            "rel_path": rel,
            "sha256": sha,
            "size": size,
            "mtime": float(mtime),
            "is_dir": False,
        }

    def _sanitize_legacy_entry(self, entry) -> dict | None:
        """Validate a legacy (pre-refactor) inv entry carrying root_index."""
        if not isinstance(entry, dict):
            return None
        path = entry.get("path")
        ri = entry.get("root_index")
        sha = entry.get("sha256")
        size = entry.get("size")
        mtime = entry.get("mtime")
        is_dir = entry.get("is_dir") is True
        if not isinstance(path, str) or not path or len(path) > MAX_PATH_LEN:
            return None
        if ".." in path.replace("\\", "/").split("/"):
            return None
        if isinstance(ri, bool) or not isinstance(ri, int) or not (0 <= ri <= 2000):
            return None
        if isinstance(mtime, bool) or not isinstance(mtime, (int, float)):
            return None
        if is_dir:
            if not path.endswith("/"):
                return None
            return {
                "root_index": ri,
                "path": path,
                "is_dir": True,
                "size": None,
                "mtime": float(mtime),
                "sha256": "",
            }
        if not isinstance(sha, str) or not _SHA16_RE.match(sha):
            return None
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not (0 <= size <= MAX_CONFIG_FILE_SIZE)
        ):
            return None
        return {
            "root_index": ri,
            "path": path,
            "sha256": sha,
            "size": size,
            "mtime": float(mtime),
            "is_dir": False,
        }

    def _handle_inv(self, payload: dict, peer_id: str) -> None:
        name = payload.get("device_name")
        if not isinstance(name, str):
            name = ""
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            logger.debug("aiconfig_inv from %s has invalid entries", peer_id[:12])
            return
        legacy = payload.get("v") not in (2, 3)
        cleaned: list[dict] = []
        for entry in raw_entries[:MAX_ENTRIES]:
            clean = self._sanitize_legacy_entry(entry) if legacy else self._sanitize_entry(entry)
            if clean is not None:
                cleaned.append(clean)
        version = payload.get("v") if not legacy else None
        with self._lock:
            self._peer_inventories[peer_id] = {
                "name": name[:128],
                "entries": cleaned,
                "fetched_at": time.time(),
                "legacy": legacy,
                "v": version,
            }
        logger.info(
            "Stored aiconfig inventory from %s (%d entries, %s)",
            peer_id[:12],
            len(cleaned),
            "legacy" if legacy else f"v{version}",
        )
        # Tell the UI the inventory landed.  Without this the migrate panel has
        # no way to know its refresh arrived -- it asks, the reply is stored
        # silently, and the diff on screen stays stale until the user reloads.
        self._emit(
            {
                "type": "aiconfig_inventory",
                "peer_id": peer_id,
                "device_name": name[:128],
                "count": len(cleaned),
                "legacy": legacy,
            }
        )

    def _peer_legacy(self, peer_id: str) -> bool:
        with self._lock:
            inv = self._peer_inventories.get(peer_id)
        return bool(inv and inv.get("legacy"))

    def _handle_req(self, payload: dict, peer_id: str) -> None:
        # Refresh form: the peer wants our current inventory again.
        if payload.get("inventory_refresh"):
            # "Current" has to mean current: answering from the cached entry
            # list makes the remote panel's refresh button a no-op whenever the
            # file actually changed since our last scan.  Throttled so a peer
            # cannot drive back-to-back disk walks.
            now = time.time()
            with self._lock:
                due = now - self._last_peer_refresh >= REFRESH_COLLECT_THROTTLE
                if due:
                    self._last_peer_refresh = now
            if due:
                try:
                    self.collect()
                except Exception:
                    logger.debug("aiconfig refresh collect failed", exc_info=True)
            self.send_inventory_to(peer_id)
            return
        if "tool" in payload:
            # v3 form — (tool, root, rel_path); v2 omits root and is resolved
            # by the tool-only fallback.
            tool = payload.get("tool")
            rel = payload.get("rel_path")
            root_id = payload.get("root")
            if not isinstance(tool, str) or not tool:
                return
            if not isinstance(rel, str) or not rel:
                return
            if root_id is None or root_id == "":
                root_id = None
            elif not ai_profiles.valid_root_id(root_id):
                logger.debug("aiconfig_req bad root id from %s", peer_id[:12])
                return
            norm = rel.replace("\\", "/")
            known = None
            with self._lock:
                for e in self._local_entries:
                    if e["tool"] != tool or e["path"] != norm:
                        continue
                    if root_id is not None and e.get("root") != root_id:
                        continue
                    known = e
                    break
            if known is None:
                logger.debug("aiconfig_req for unadvertised path from %s", peer_id[:12])
                self._refuse_req(
                    peer_id, "not_advertised", tool=tool, root_id=root_id or "", rel=norm
                )
                return
            target, reason = self._resolve_local_target(
                tool, rel, require_exists=True, root_id=root_id
            )
            if target is None:
                logger.info("aiconfig_req unresolved (%s) from %s: %s", rel, peer_id[:12], reason)
                self._refuse_req(
                    peer_id, reason or "unresolved", tool=tool, root_id=root_id or "", rel=norm
                )
                return
            # Echo the root the REQUESTER named (not ours): the reply has to
            # match the pending record on the requester's side, and a v2
            # requester that sent no root must get none back or its key misses.
            self._serve_file(tool, rel, target, known, peer_id, root_id=root_id or "")
            return
        # Legacy form — root_index + rel_path.
        ri = payload.get("root_index")
        rel = payload.get("rel_path")
        if isinstance(ri, bool) or not isinstance(ri, int):
            return
        if not isinstance(rel, str) or not rel:
            return
        roots = [rt for rt in self._roots()]
        if not (0 <= ri < len(roots)):
            logger.debug("aiconfig_req bad root_index %s from %s", ri, peer_id[:12])
            self._refuse_req(peer_id, "bad_root_index", rel=rel, root_index=ri)
            return
        tool, root_id, kind, raw = roots[ri]
        root = expand_root(raw)
        if root is None:
            self._refuse_req(peer_id, "no_local_root", rel=rel, root_index=ri)
            return
        if kind == "file":
            if rel.replace("\\", "/") != root.name:
                self._refuse_req(peer_id, "not_advertised", rel=rel, root_index=ri)
                return
            target = root
        else:
            if not root.is_dir():
                self._refuse_req(peer_id, "no_local_root", rel=rel, root_index=ri)
                return
            target = resolve_safe(root, rel)
            if target is None:
                logger.info("Rejected legacy aiconfig_req with unsafe path from %s", peer_id[:12])
                self._refuse_req(peer_id, "unsafe_path", rel=rel, root_index=ri)
                return
        known = None
        with self._lock:
            for e in self._local_entries:
                if e["root_index"] == ri and e["path"] == rel.replace("\\", "/"):
                    known = e
                    break
        if known is None:
            logger.debug("aiconfig_req legacy for unadvertised path from %s", peer_id[:12])
            self._refuse_req(peer_id, "not_advertised", rel=rel, root_index=ri)
            return
        self._serve_file(tool, rel, target, known, peer_id, root_id=root_id)

    def _refuse_req(
        self,
        peer_id: str,
        reason: str,
        tool=None,
        root_id: str = "",
        rel: str = "",
        root_index=None,
    ) -> None:
        """Tell the requester we will not serve this file.

        Every refusal path used to just ``return``, leaving the requester to
        wait out PENDING_TTL with no result -- so a batch stalled at N-1/N and
        a preview spun for the full 5 s.  ``aiconfig_err`` is a new msg_type,
        which older peers drop in handle_message's vocabulary check, so this is
        safe to send to any peer; those peers still recover via _expire_pending.
        """
        payload = {"msg_type": "aiconfig_err", "reason": reason, "rel_path": rel}
        if root_index is not None:
            payload["root_index"] = root_index
        else:
            payload["tool"] = tool or ""
            if root_id:
                payload["root"] = root_id
        self._send_frame(payload, peer_id)

    def _handle_err(self, payload: dict, peer_id: str) -> None:
        """A peer refused one of our requests -- finish that pending now."""
        rel = payload.get("rel_path")
        if not isinstance(rel, str) or not rel:
            return
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = "refused"
        reason = reason[:64]
        norm = rel.replace("\\", "/")
        ri = payload.get("root_index")
        if "tool" in payload:
            tool = payload.get("tool")
            root = payload.get("root")
            if not isinstance(tool, str) or not tool:
                return
            if root is None or root == "":
                root = ""
            elif not ai_profiles.valid_root_id(root):
                return
            key = (peer_id, "v3", tool, root, norm)
        elif not isinstance(ri, bool) and isinstance(ri, int):
            key = (peer_id, "legacy", ri, norm)
        else:
            return
        with self._lock:
            pending = self._pending.pop(key, None)
        if pending is None:
            return
        self._finish_pending(pending, key, "error", reason=reason, batch_id=pending.get("batch_id"))

    def _serve_file(
        self, tool: str, rel: str, target: Path, known: dict, peer_id: str, root_id: str = ""
    ) -> None:
        legacy = self._peer_legacy(peer_id)
        ri = known.get("root_index") if legacy else None

        def refuse(reason: str) -> None:
            self._refuse_req(
                peer_id,
                reason,
                tool=tool,
                root_id=root_id,
                rel=known.get("path", rel),
                root_index=ri,
            )

        if not target.is_file() or target.is_symlink():
            refuse("not_a_file")
            return
        try:
            size = target.stat().st_size
        except OSError:
            refuse("stat_failed")
            return
        truncated = size > MAX_CONFIG_FILE_SIZE
        try:
            with open(target, "rb") as f:
                data = f.read(MAX_CONFIG_FILE_SIZE)
        except OSError:
            refuse("read_failed")
            return
        digest = hashlib.sha256(data).hexdigest()[:16]
        if not truncated and digest != known["sha256"]:
            # File changed since collection — refuse until the next
            # inventory refresh rather than serving unverified content.
            logger.info("aiconfig_req hash drift for %s — refusing", rel)
            refuse("hash_drift")
            return
        payload = {
            "msg_type": "aiconfig_data",
            "sha256": digest,
            "b64_content": base64.b64encode(data).decode("ascii"),
            "truncated": truncated,
            "rel_path": known["path"],
        }
        if legacy:
            payload["root_index"] = known["root_index"]
        else:
            payload["tool"] = tool
            # Echo the root the requester named so it can match its own pending
            # record; a v2 requester sent none and gets none back.
            if root_id:
                payload["root"] = root_id
        self._send_frame(payload, peer_id)

    def _handle_data(self, payload: dict, peer_id: str) -> None:
        sha = payload.get("sha256")
        b64 = payload.get("b64_content")
        if not isinstance(sha, str) or not isinstance(b64, str):
            return
        truncated = bool(payload.get("truncated"))
        if "tool" in payload:
            tool = payload.get("tool")
            rel = payload.get("rel_path")
            root = payload.get("root")
            if not isinstance(tool, str) or not tool:
                return
            if not isinstance(rel, str) or not rel:
                return
            if root is None or root == "":
                root = ""
            elif not ai_profiles.valid_root_id(root):
                return
            key = (peer_id, "v3", tool, root, rel.replace("\\", "/"))
            pending = self._pop_pending(key)
            if pending is None:
                logger.debug("Unsolicited aiconfig_data from %s dropped", peer_id[:12])
                return
            self._process_data(key, pending, sha, b64, truncated)
            return
        ri = payload.get("root_index")
        rel = payload.get("rel_path")
        if isinstance(ri, bool) or not isinstance(ri, int):
            return
        if not isinstance(rel, str) or not rel:
            return
        key = (peer_id, "legacy", ri, rel.replace("\\", "/"))
        pending = self._pop_pending(key)
        if pending is None:
            logger.debug("Unsolicited aiconfig_data (legacy) from %s dropped", peer_id[:12])
            return
        self._process_data(key, pending, sha, b64, truncated)

    def _pop_pending(self, key: tuple) -> dict | None:
        with self._lock:
            return self._pending.pop(key, None)

    def _process_data(
        self, key: tuple, pending: dict, sha: str, b64: str, truncated: bool = False
    ) -> None:
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            logger.info("aiconfig_data b64 decode failed from %s", key[0][:12])
            self._finish_pending(pending, key, "error", reason="b64_decode")
            return
        if len(data) > MAX_CONFIG_FILE_SIZE:
            self._finish_pending(pending, key, "error", reason="too_large")
            return
        digest = hashlib.sha256(data).hexdigest()[:16]
        if digest != sha:
            self._finish_pending(pending, key, "error", reason="hash_mismatch")
            return
        if pending.get("mode") == "preview":
            text = self._to_preview_text(data)
            with self._lock:
                # Either kind of truncation is worth flagging: the sender cut
                # the file at MAX_CONFIG_FILE_SIZE, or the preview itself hit
                # PREVIEW_MAX_BYTES.
                pending["result"] = {
                    "ok": True,
                    "content": text,
                    "truncated": bool(truncated) or len(text) >= PREVIEW_MAX_BYTES,
                }
                event = pending.get("event")
            if event is not None:
                event.set()
            return
        if truncated:
            # The sender only read the first MAX_CONFIG_FILE_SIZE bytes and
            # skipped its own hash check, so these bytes are a prefix, not the
            # file.  Landing them would silently truncate the user's config --
            # the flag used to be carried across the wire and then ignored by
            # every landing path.
            logger.info(
                "Refusing to land truncated aiconfig file from %s (%s)",
                key[0][:12],
                key[-1],
            )
            self._finish_pending(pending, key, "error", reason="source_truncated")
            return
        # Landing only ever happens for protocol peers (legacy pulls are blocked
        # at the pull() door), but read the key through the one decoder so the
        # key layout lives in exactly one place.
        tool, _root, rel = self._describe_pending_key(key)
        status, reason = self._land_file(
            tool,
            rel,
            data,
            pending.get("mode", "copy"),
            key[0],
            root_id=pending.get("root") or None,
        )
        self._finish_pending(pending, key, status, reason=reason, batch_id=pending.get("batch_id"))

    # ------------------------------------------------------------ landing

    def _land_target(
        self, tool: str, rel: str, root_id: str | None = None
    ) -> tuple[Path | None, str | None]:
        """Exact local file a pulled (tool, root_id, rel) should be written to.

        Resolution goes through THIS device's tool profiles (deterministic), so
        no peer root indices are involved.  With a v3 *root_id* the landing root
        is named outright; without one (v2 peer) the tool-only fallback applies.
        The target need not exist yet — the write creates missing parents.
        ``ambiguous_tool_root`` / ``no_local_root`` report the two unambiguous
        failure cases.
        """
        return self._resolve_local_target(tool, rel, require_exists=False, root_id=root_id)

    def _land_file(
        self, tool: str, rel: str, data: bytes, mode: str, peer_id: str, root_id: str | None = None
    ) -> tuple[str, str | None]:
        """Write pulled bytes per *mode*.  Returns (status, reason)."""
        target, reason = self._land_target(tool, rel, root_id=root_id)
        if target is None:
            return "error", reason or "no_local_root"
        try:
            if mode == "overwrite":
                target.parent.mkdir(parents=True, exist_ok=True)
                # Keep one generation of the file we are about to replace.
                # Overwriting a hand-tuned AGENTS.md / settings.json with a
                # peer's copy used to be unrecoverable: os.replace() left
                # nothing behind, and this is the one landing mode that
                # destroys local content.  Same ".bak" convention as
                # local_save() so there is a single thing for the user to look
                # for.
                if target.exists():
                    try:
                        shutil.copy2(target, target.with_name(target.name + ".bak"))
                    except OSError as exc:
                        logger.warning(
                            "aiconfig overwrite aborted: cannot back up %s (%s)",
                            rel,
                            exc,
                        )
                        return "error", "backup_failed"
                tmp = target.with_name(target.name + ".clipsync.tmp")
                tmp.write_bytes(data)
                os.replace(tmp, target)
                return "saved", None
            if mode == "copy":
                stem, ext = os.path.splitext(target.name)
                # "copy" means "do not clobber what is already here".  When the
                # file does not exist locally there is nothing to protect, so
                # write the real name: the migrate panel's "only missing files"
                # strategy sends mode=copy, and renaming to
                # "AGENTS.from.Laptop.md" left the tool unable to see the file
                # the user thought they had just brought over.
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    # Still reported as "copied", not "saved": nothing local was
                    # replaced, and the status vocabulary the WS client accepts
                    # is fixed (saved/copied/appended/error).
                    return "copied", None
                dev = self._peer_display_name(peer_id)
                safe_dev = (_SANITIZE_RE.sub("_", dev)[:40]) or "peer"
                candidate = target.with_name(f"{stem}.from.{safe_dev}{ext}")
                n = 2
                while candidate.exists():
                    candidate = target.with_name(f"{stem}.from.{safe_dev}-{n}{ext}")
                    n += 1
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_bytes(data)
                return "copied", None
            if mode == "append":
                if target.suffix.lower() not in APPEND_EXTS:
                    return "error", "append_not_text"
                target.parent.mkdir(parents=True, exist_ok=True)
                prefix = b""
                if target.exists() and target.stat().st_size > 0:
                    with open(target, "rb") as f:
                        f.seek(-1, os.SEEK_END)
                        if f.read(1) != b"\n":
                            prefix = b"\n"
                tail = b"" if data.endswith(b"\n") else b"\n"
                with open(target, "ab") as f:
                    f.write(prefix + data + tail)
                return "appended", None
            return "error", "bad_mode"
        except OSError as exc:
            logger.warning("aiconfig landing failed for %s: %s", rel, exc)
            return "error", "io_error"

    def _finish_pending(
        self, pending: dict, key: tuple, status: str, reason: str | None = None, batch_id=None
    ) -> None:
        if pending.get("mode") == "preview":
            with self._lock:
                pending["result"] = {"ok": False, "error": reason or status}
                event = pending.get("event")
            if event is not None:
                event.set()
            return
        tool, root, rel = self._describe_pending_key(key)
        ws_event = {
            "type": "aiconfig_file",
            "peer_id": key[0],
            "tool": tool,
            "root": root,
            "rel_path": rel,
            "status": status,
        }
        if reason:
            ws_event["reason"] = reason
        if batch_id:
            ws_event["batch_id"] = batch_id
        self._emit(ws_event)
        logger.debug("aiconfig_file status=%s reason=%s path=%s", status, reason, rel)

    @staticmethod
    def _describe_pending_key(key: tuple) -> tuple[str, str, str]:
        """Split a pending key into (tool, root, rel) for the WS result event.

        Pending keys come in two shapes and the caller must not care which:
        ``(peer_id, "v3", tool, root, rel)`` for protocol peers and
        ``(peer_id, "legacy", root_index, rel)`` for pre-v2 ones, where no tool
        or root id exists on the wire.  The UI needs (tool, root, rel) to match
        the result back to the row it pulled, so a legacy key reports the
        sentinel tool "legacy" and an empty root.
        """
        if len(key) >= 5 and key[1] == "v3":
            return str(key[2]), str(key[3]), str(key[4])
        return "legacy", "", str(key[-1])

    def _peer_display_name(self, peer_id: str) -> str:
        peer = getattr(self._cfg, "peers", {}).get(peer_id)
        if peer is not None and getattr(peer, "device_name", ""):
            return peer.device_name
        with self._lock:
            inv = self._peer_inventories.get(peer_id)
        if inv and inv.get("name"):
            return inv["name"]
        return peer_id

    def _to_preview_text(self, data: bytes) -> str:
        return data[:PREVIEW_MAX_BYTES].decode("utf-8", errors="replace")

    # -------------------------------------------------------------- pulls

    def _prune_pending(self, now: float | None = None) -> list[tuple]:
        """Drop timed-out pending records.  Call with the lock held.

        Returns the expired *pull* records as ``[(key, pending), ...]`` for the
        caller to report once the lock is released -- reporting means running
        the UI event callback, which must never happen under our lock.
        """
        now = time.time() if now is None else now
        expired = [k for k, p in self._pending.items() if now - p.get("ts", now) > PENDING_TTL]
        orphans: list[tuple] = []
        for k in expired:
            pending = self._pending.pop(k)
            event = pending.get("event")
            if event is not None:
                pending.setdefault("result", {"ok": False, "error": "timeout"})
                event.set()
            else:
                orphans.append((k, pending))
        return orphans

    def _expire_pending(self) -> None:
        """Prune expired pendings and tell the UI about abandoned pulls.

        A peer that refuses a request (hash drift, path no longer advertised)
        or simply goes away leaves the pending record to expire.  Without a
        report the batch tracker never reaches N/N and the panel shows a pull
        as in-progress forever.
        """
        with self._lock:
            orphans = self._prune_pending()
        for key, pending in orphans:
            self._finish_pending(
                pending, key, "error", reason="no_reply", batch_id=pending.get("batch_id")
            )

    def pull(self, peer_id: str, items, mode: str = "copy", batch_id: str = "") -> dict:
        """Request files (and/or folders) from a paired peer.

        *items* is a list of v2 dicts, each ``{"tool", "rel_path"}``.  A folder
        item carries ``"is_dir": true`` and is expanded server-side from the
        peer's cached inventory into its file entries.  Returns
        ``{"requested": N, "errors": [...]}`` without waiting for the async
        aiconfig_data replies — those land via _handle_data and are reported
        through the aiconfig_file WS event (echoing *batch_id*).
        """
        if mode not in ("overwrite", "copy", "append"):
            mode = "copy"  # never let a malformed request turn destructive
        if not self._is_paired(peer_id):
            return {"requested": 0, "errors": ["peer_not_paired"]}
        if peer_id not in self._connected_peers():
            return {"requested": 0, "errors": ["peer_offline"]}
        if not isinstance(items, list):
            return {"requested": 0, "errors": ["items_required"]}
        # Legacy peers are browse/preview-only (their root_index inventories
        # cannot resolve to THIS device's tool-profile targets deterministically).
        if self._peer_legacy(peer_id):
            return {"requested": 0, "errors": ["legacy_peer_read_only"]}
        expanded = self._expand_items(peer_id, items)
        requested = 0
        errors: list[str] = []
        self._expire_pending()
        for item in expanded[:MAX_ENTRIES]:
            if not isinstance(item, dict):
                errors.append("invalid_item")
                continue
            tool = item.get("tool")
            rel = item.get("rel_path")
            root = item.get("root")
            if (
                not isinstance(tool, str)
                or not tool
                or not isinstance(rel, str)
                or not rel
                or len(rel) > MAX_PATH_LEN
            ):
                errors.append("invalid_item")
                continue
            if root is None or root == "":
                root = ""
            elif not ai_profiles.valid_root_id(root):
                errors.append("invalid_item")
                continue
            key = (peer_id, "v3", tool, root, rel.replace("\\", "/"))
            req = {"msg_type": "aiconfig_req", "tool": tool, "rel_path": rel}
            if root:
                req["root"] = root
            with self._lock:
                self._pending[key] = {
                    "mode": mode,
                    "ts": time.time(),
                    "batch_id": batch_id,
                    "root": root,
                }
            if self._send_frame(req, peer_id):
                requested += 1
            else:
                with self._lock:
                    self._pending.pop(key, None)
                errors.append("send_failed")
        return {"requested": requested, "errors": errors, "expanded": len(expanded)}

    def _expand_items(self, peer_id: str, items) -> list[dict]:
        """Expand folder items into file items from the peer's cached inventory.

        A folder item is ``{"tool", "rel_path" (trailing '/'), "is_dir": true}``.
        Every cached v2 file entry whose rel_path starts with the folder's
        rel_path is pulled; the folder entry itself is dropped.  Plain file
        items pass through unchanged.
        """
        out: list[dict] = []
        with self._lock:
            inv = self._peer_inventories.get(peer_id, {})
            cache = inv.get("entries", [])
        for item in items:
            if not isinstance(item, dict):
                out.append(item)
                continue
            if not item.get("is_dir"):
                out.append(item)
                continue
            folder_rel = item.get("rel_path")
            if not isinstance(folder_rel, str) or not folder_rel:
                out.append(item)
                continue
            prefix = folder_rel.replace("\\", "/")
            if not prefix.endswith("/"):
                prefix += "/"
            tool = item.get("tool")
            root = item.get("root")
            matched = 0
            for e in cache:
                if e.get("is_dir"):
                    continue
                rel = e.get("rel_path") or e.get("path")
                if not rel:
                    continue
                if tool is not None and e.get("tool") != tool:
                    continue
                # A folder belongs to one root, so only that root's files are
                # inside it — otherwise a same-named subfolder under a sibling
                # root (skills/x/ vs commands/x/) would be swept in too.
                if root and e.get("root", "") != root:
                    continue
                if rel.startswith(prefix):
                    out.append(e)
                    matched += 1
            if matched == 0:
                out.append(item)  # unknown folder — let the peer refuse cleanly
        return out

    def _preview_wait(self, peer_id: str, req: dict, key: tuple, timeout: float) -> dict:
        """Register a display-only pending, send the request, block for data.

        Blocks up to *timeout* seconds waiting for the peer's aiconfig_data
        (the HTTP worker thread can afford this; ThreadingHTTPServer keeps
        serving other requests meanwhile)."""
        event = threading.Event()
        record: dict = {
            "mode": "preview",
            "ts": time.time(),
            "event": event,
            "result": None,
        }
        self._expire_pending()
        with self._lock:
            self._pending[key] = record
        if not self._send_frame(req, peer_id):
            with self._lock:
                self._pending.pop(key, None)
            return {"ok": False, "error": "send_failed"}
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(key, None)
            return {"ok": False, "error": "timeout"}
        result = record.get("result")
        return result or {"ok": False, "error": "no_data"}

    def preview(
        self, peer_id: str, tool: str, rel: str, timeout: float = PREVIEW_TIMEOUT, root: str = ""
    ) -> dict:
        """Fetch one file's content for display only — nothing touches disk.

        Returns {"ok", "content" (≤64 KB), "truncated"}; a legacy peer must be
        previewed via ``preview_legacy`` instead."""
        if not self._is_paired(peer_id):
            return {"ok": False, "error": "peer_not_paired"}
        if peer_id not in self._connected_peers():
            return {"ok": False, "error": "peer_offline"}
        if not isinstance(tool, str) or not tool or not isinstance(rel, str) or not rel:
            return {"ok": False, "error": "invalid_item"}
        if self._peer_legacy(peer_id):
            return {"ok": False, "error": "legacy_peer"}
        if root is None or root == "":
            root = ""
        elif not ai_profiles.valid_root_id(root):
            return {"ok": False, "error": "invalid_item"}
        key = (peer_id, "v3", tool, root, rel.replace("\\", "/"))
        req = {"msg_type": "aiconfig_req", "tool": tool, "rel_path": rel}
        if root:
            req["root"] = root
        return self._preview_wait(peer_id, req, key, timeout)

    def preview_legacy(
        self, peer_id: str, root_index, rel: str, timeout: float = PREVIEW_TIMEOUT
    ) -> dict:
        """Legacy-peer variant: root_index-based request, same display-only
        guarantee.  *root_index* is the int from the peer's cached entry."""
        if not self._is_paired(peer_id):
            return {"ok": False, "error": "peer_not_paired"}
        if peer_id not in self._connected_peers():
            return {"ok": False, "error": "peer_offline"}
        if (
            isinstance(root_index, bool)
            or not isinstance(root_index, int)
            or not isinstance(rel, str)
            or not rel
        ):
            return {"ok": False, "error": "invalid_item"}
        key = (peer_id, "legacy", root_index, rel.replace("\\", "/"))
        return self._preview_wait(
            peer_id,
            {
                "msg_type": "aiconfig_req",
                "root_index": root_index,
                "rel_path": rel,
            },
            key,
            timeout,
        )

    # ---------------------------------------------------------------- REST

    def get_peer_inventories(self) -> dict:
        """Snapshot of cached peer inventories for GET /api/aiconfig/inventory."""
        with self._lock:
            out = {}
            for pid, inv in self._peer_inventories.items():
                out[pid] = {
                    "name": inv.get("name", ""),
                    "legacy": bool(inv.get("legacy")),
                    "entries": [dict(e) for e in inv.get("entries", [])],
                    "fetched_at": inv.get("fetched_at", 0.0),
                }
            return out

    def request_inventory(self, peer_id: str) -> bool:
        """Ask a paired peer to re-send its aiconfig_inv (?refresh=1)."""
        if not self._is_paired(peer_id):
            return False
        return self._send_frame(
            {
                "msg_type": "aiconfig_req",
                "inventory_refresh": True,
            },
            peer_id,
        )

    def local_summary(self) -> dict:
        with self._lock:
            return {
                "collected_at": self._local_collected_at,
                "entry_count": len(self._local_entries),
                "tools": list(getattr(self._cfg, "ai_config_tools", [])),
                "custom_paths": list(getattr(self._cfg, "ai_config_custom_paths", [])),
            }

    def set_profiles(self, tool_keys, custom_paths) -> dict:
        """Normalize + persist the enabled tool keys and custom paths."""
        tools = ai_profiles.validate_tool_keys(tool_keys)
        custom = ai_profiles.validate_custom_paths(custom_paths)
        self._cfg.ai_config_tools = tools
        self._cfg.ai_config_custom_paths = custom
        if self._save_fn is not None:
            try:
                self._save_fn()
            except Exception:
                logger.debug("aiconfig profile persist failed", exc_info=True)
        return {"ok": True, "tools": tools, "custom_paths": custom}

    # ------------------------------------------------- local file manager

    def _trash_base(self) -> Path:
        """Absolute directory that holds the recoverable trash.

        ``cfg.data_dir`` (a user-custom data directory) wins when set;
        otherwise the app's default data/config directory is used.  Mirrors
        how backups/ and the config.json itself resolve their base dir.
        """
        custom = str(getattr(self._cfg, "data_dir", "") or "").strip()
        if custom:
            return Path(custom) / TRASH_DIR_NAME
        from internal.config.config import _config_dir

        return _config_dir() / TRASH_DIR_NAME

    def local_listing(self) -> dict:
        """Fresh local inventory for GET /api/aiconfig/local.

        Reuses the same collector as the paired-peer exchange, grouped by tool
        profile for the UI.  Directories are included as folder entries so
        skill / command folders show up as openable items.
        """
        entries = collect_roots(self._roots(), include_dirs=True)
        with self._lock:
            # Keep the scan, don't just timestamp it.  This is a full
            # collect_roots() walk -- the same one collect() does -- and
            # throwing it away meant build_inv_payload() kept answering peers
            # from whatever _local_entries held at process start, so a peer's
            # view of us could stay stale indefinitely while the dashboard
            # showed the truth.
            self._local_entries = entries
            self._local_collected_at = time.time()
        roots = []
        for index, (tool, root_id, kind, raw) in enumerate(self._roots()):
            roots.append(
                {
                    "root_index": index,
                    "tool": tool,
                    "root": root_id,
                    "kind": kind,
                    "path": raw,
                    "count": sum(1 for e in entries if e["root_index"] == index),
                }
            )
        listing = [
            {
                "tool": e["tool"],
                "root": e.get("root", ""),
                "rel_path": e["path"],
                "size": e.get("size"),
                "mtime": e.get("mtime"),
                "sha256": e.get("sha256"),
                "is_dir": bool(e.get("is_dir")),
            }
            for e in entries
        ]
        with self._lock:
            collected_at = self._local_collected_at
        return {
            "collected_at": collected_at,
            "tools": [
                t
                for t in ai_profiles.TOOLS
                if t["key"] in set(getattr(self._cfg, "ai_config_tools", []))
            ],
            "custom_paths": list(getattr(self._cfg, "ai_config_custom_paths", [])),
            "roots": roots,
            "entries": listing,
        }

    def local_read(self, tool, rel, root: str = "") -> dict:
        """Read one file's text content (local only, ≤64 KB).

        Returns {"ok", "content", "truncated"}; binary files (any NUL byte
        in the leading window) are refused with error "binary".
        """
        if not isinstance(tool, str) or not isinstance(rel, str):
            return {"ok": False, "error": "invalid_item"}
        target, reason = self._land_target(tool, rel, root_id=root or None)
        if target is None:
            return {"ok": False, "error": reason or "no_local_root"}
        if target.is_symlink() or not target.is_file():
            return {"ok": False, "error": "not_found"}
        try:
            size = target.stat().st_size
            with open(target, "rb") as f:
                data = f.read(LOCAL_READ_MAX_BYTES)
        except OSError:
            return {"ok": False, "error": "io_error"}
        if b"\x00" in data:
            return {"ok": False, "error": "binary"}
        # A bounded preview may end inside a UTF-8 sequence. Defer only that
        # incomplete tail; malformed bytes must never become editable replacements.
        import codecs
        try:
            content = codecs.getincrementaldecoder("utf-8")().decode(
                data, final=size <= LOCAL_READ_MAX_BYTES
            )
        except UnicodeDecodeError:
            return {"ok": False, "error": "invalid_encoding"}
        return {
            "ok": True,
            "content": content,
            "truncated": size > LOCAL_READ_MAX_BYTES,
        }

    def local_save(self, tool, rel, content, root: str = "") -> dict:
        """Write text content back to a watched file (local only).

        Safety net: ① resolve_safe rejects traversal; ② the pre-save original
        is copied to ``<rel_path>.bak`` in the same directory (overwriting a
        stale .bak, which doubles as an "edited here" marker); ③ NUL bytes are
        refused (text only); ④ content is capped at LOCAL_SAVE_MAX_BYTES.  The
        write itself is atomic — temp file + os.replace — and serialized under
        the manager lock so two concurrent saves cannot interleave.
        """
        if not isinstance(tool, str) or not isinstance(rel, str):
            return {"ok": False, "error": "invalid_item"}
        if not isinstance(content, str):
            return {"ok": False, "error": "content_required"}
        data = content.encode("utf-8")
        if len(data) > LOCAL_SAVE_MAX_BYTES:
            return {"ok": False, "error": "too_large"}
        if b"\x00" in data:
            return {"ok": False, "error": "binary"}
        target, reason = self._land_target(tool, rel, root_id=root or None)
        if target is None:
            return {"ok": False, "error": reason or "no_local_root"}
        with self._lock:
            backup_overwrote = False
            try:
                if target.exists():
                    if target.is_symlink():
                        return {"ok": False, "error": "unsafe_path"}
                    bak = target.with_name(target.name + ".bak")
                    backup_overwrote = bak.exists()
                    shutil.copy2(target, bak)
            except OSError:
                return {"ok": False, "error": "backup_failed"}
            tmp = target.with_name(f".{target.name}.clipsync.tmp")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                os.replace(tmp, target)
            except OSError:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
                return {"ok": False, "error": "io_error"}
        result: dict = {"ok": True}
        if backup_overwrote:
            result["backup_overwrote"] = True
        return result

    def local_trash(self, tool, rel, root: str = "") -> dict:
        """Move a watched file OR directory into the recoverable trash.

        Destination is ``<data_dir>/aiconfig_trash/<original subpath>/
        <timestamp>_<name>`` — the relative directory structure is preserved
        so same-named files/folders in different places never collide, and the
        timestamp prefix keeps trashed copies sortable.  A directory is moved
        whole (recursively).  Collisions get an incremented ``-N`` suffix.
        Returns {"ok", "trashed_to"}.
        """
        if not isinstance(tool, str) or not isinstance(rel, str):
            return {"ok": False, "error": "invalid_item"}
        target, reason = self._land_target(tool, rel, root_id=root or None)
        if target is None:
            return {"ok": False, "error": reason or "no_local_root"}
        if target.is_symlink() or not (target.is_file() or target.is_dir()):
            return {"ok": False, "error": "not_found"}
        base = self._trash_base()
        rel_dir = os.path.dirname(rel.replace("\\", "/"))
        dest_dir = base.joinpath(*[p for p in rel_dir.split("/") if p]) if rel_dir else base
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return {"ok": False, "error": "trash_dir_failed"}
        stamp = time.strftime("%Y%m%d_%H%M%S")
        stem, ext = os.path.splitext(target.name)
        candidate = dest_dir / f"{stamp}_{target.name}"
        n = 2
        while candidate.exists():
            candidate = dest_dir / f"{stamp}_{stem}-{n}{ext}"
            n += 1
        try:
            shutil.move(str(target), str(candidate))
        except OSError as exc:
            logger.warning("aiconfig trash move failed for %s: %s", rel, exc)
            return {"ok": False, "error": "move_failed"}
        return {"ok": True, "trashed_to": str(candidate)}

    def local_open(self, tool, rel, root: str = "") -> dict:
        """Open a watched file (or directory) with the OS default app."""
        if not isinstance(tool, str) or not isinstance(rel, str):
            return {"ok": False, "error": "invalid_item"}
        target, reason = self._land_target(tool, rel, root_id=root or None)
        if target is None:
            return {"ok": False, "error": reason or "no_local_root"}
        if target.is_symlink() or not (target.is_file() or target.is_dir()):
            return {"ok": False, "error": "not_found"}
        if open_with_default_app(str(target)):
            return {"ok": True}
        return {"ok": False, "error": "open_failed"}
