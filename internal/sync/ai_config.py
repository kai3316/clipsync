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

Protocol v2: inventory entries carry a ``tool`` key from the deterministic
profile table, so a receiving device resolves the landing target through the
SAME profile — root indices never cross the wire and the old ambiguous_root /
no_local_root mapping errors are gone.  Legacy (pre-refactor) peers remain
read-only: their root_index inventories are cached and previewed / pulled via
the old request shape.

Wire payloads (JSON):
  aiconfig_inv  {"msg_type","v":2,"device_name": str,"entries": [entry...]}
      entry = {"tool": str, "rel_path": str(rel posix), "sha256": 16 hex,
               "size": int, "mtime": float, "is_dir": bool}
      legacy entry (no "v"): {"root_index": int, "path": str, ...}
  aiconfig_req  {"msg_type","tool": str,"rel_path": str}          (v2)
                {"msg_type","root_index": int,"rel_path": str}    (legacy)
                {"msg_type","inventory_refresh": true}
  aiconfig_data {"msg_type","tool","rel_path","sha256","b64_content","truncated"}
                (or the legacy root_index form)
"""

import base64
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

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
    return (
        low.endswith(_TEMP_SUFFIXES)
        or name.startswith(_TEMP_PREFIXES)
        or name in _TEMP_NAMES
    )


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

    *roots* is a list of ``(tool, kind, path)`` tuples as produced by
    ``ai_profiles.effective_roots`` (kind is "file" for a single-config-file
    root — the file itself is the only advertised entry — or "dir" for a
    recursive walk).  Entries are emitted relative to their root using forward
    slashes so they are stable across platforms, each carrying its ``tool``
    key and an internal ``root_index`` (index into *roots*, used only to trace
    a served file back to its root).  Output is deterministic (sorted within
    each root) and capped at *max_entries*; duplicate (tool, rel_path) pairs
    keep the first occurrence so pulls are never ambiguous.

    When *include_dirs* is true, subdirectories are also emitted as ``is_dir``
    entries (e.g. a Claude Code skill folder ``my-skill/``) so a file-manager
    UI can show the folder tree and count skills.
    """
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(roots):
        if len(entries) >= max_entries:
            break
        if not (isinstance(raw, (tuple, list)) and len(raw) == 3):
            continue
        tool, kind, path_str = raw[0], raw[1], raw[2]
        if not isinstance(tool, str) or kind not in ("file", "dir") \
                or not isinstance(path_str, str):
            continue
        root = expand_root(path_str, home=home)
        if root is None:
            continue

        def add_entry(rel: str, *, is_dir: bool = False, size=None,
                      mtime=None, sha: str = "") -> bool:
            key = (tool, rel)
            if key in seen:
                return False  # same tool + rel — keep the first
            seen.add(key)
            entry: dict = {
                "tool": tool,
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
            add_entry(root.name, size=size, mtime=float(st.st_mtime),
                      sha=digest)
            continue
        if not root.is_dir():
            continue
        found: list[tuple[str, Path, os.stat_result]] = []
        try:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not is_temp_name(d) and not (Path(dirpath) / d).is_symlink()
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
    if re.match(r"^[A-Za-z]:", norm) or norm.startswith("/") \
            or rel.startswith("\\\\"):
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

    def __init__(self, cfg, send_fn, connected_fn=None, event_fn=None,
                 save_fn=None):
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
            logger.debug("aiconfig send to %s failed", peer_id[:12],
                         exc_info=True)
            return False

    # ------------------------------------------- effective roots / landing

    def _roots(self) -> list[tuple[str, str, str]]:
        """Effective (tool, kind, path) watch roots from cfg, live.

        Custom paths default to "dir" roots; an existing custom FILE path is
        promoted to a "file" root so it advertises exactly itself.
        """
        roots = ai_profiles.effective_roots(
            list(getattr(self._cfg, "ai_config_tools", [])),
            list(getattr(self._cfg, "ai_config_custom_paths", [])),
        )
        out: list[tuple[str, str, str]] = []
        for tool, kind, path in roots:
            if tool == ai_profiles.CUSTOM_KEY and kind == "dir":
                r = expand_root(path)
                if r is not None and r.is_file():
                    kind = "file"
            out.append((tool, kind, path))
        return out

    def _local_roots_for(self, tool: str) -> list[tuple[str, str, str]]:
        return [rt for rt in self._roots() if rt[0] == tool]

    def _resolve_local_target(self, tool: str, rel: str,
                              require_exists: bool) -> tuple[Path | None, str | None]:
        """The local file a (tool, rel) maps to, or a clear reason.

        The peer's entry carries a ``tool`` that resolves through THIS device's
        own profile table (deterministic on every device), so no root-index
        remapping is needed.  A file-type root owns only its own basename and
        IS the target; a dir-type root owns any rel that resolves inside it.
        ``require_exists`` distinguishes serving (the file must exist) from
        landing (the target may be created by the write).
        """
        matches: list[tuple[Path, str]] = []  # (target, root kind)
        for _tool, kind, raw in self._local_roots_for(tool):
            root = expand_root(raw)
            if root is None:
                continue
            norm = rel.replace("\\", "/")
            try:
                if kind == "file":
                    target = root if norm == root.name else None
                else:
                    target = resolve_safe(root, rel)
            except OSError:
                continue
            if target is None:
                continue
            if require_exists:
                try:
                    if not target.is_file() or target.is_symlink():
                        continue
                except OSError:
                    continue
            matches.append((target, kind))
        if len(matches) == 1:
            return matches[0][0], None
        # An exact file-root name match beats a dir-root path match: a peer's
        # rel_path that names one of OUR file roots can only have come from that
        # same file root on the peer (dir roots advertise paths relative to
        # themselves, never "their own name").  Fall back to the strict
        # ambiguity error only when several file roots tie or none claims it.
        named = [t for t, k in matches if k == "file"]
        if len(named) == 1:
            return named[0], None
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
            "v": 2,
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
        if msg_type not in ("aiconfig_inv", "aiconfig_req", "aiconfig_data"):
            return
        if not isinstance(payload, dict):
            return
        if not self._is_paired(peer_id):
            logger.debug("Ignoring %s from unpaired/unknown sender %.12s",
                         msg_type, peer_id or "?")
            return
        if msg_type == "aiconfig_inv":
            self._handle_inv(payload, peer_id or "")
        elif msg_type == "aiconfig_req":
            self._handle_req(payload, peer_id or "")
        else:
            self._handle_data(payload, peer_id or "")

    def _sanitize_entry(self, entry) -> dict | None:
        """Validate one wire inventory entry; None = drop it."""
        if not isinstance(entry, dict):
            return None
        rel = entry.get("rel_path") or entry.get("path")
        tool = entry.get("tool")
        sha = entry.get("sha256")
        size = entry.get("size")
        mtime = entry.get("mtime")
        is_dir = entry.get("is_dir") is True
        if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH_LEN:
            return None
        if ".." in rel.replace("\\", "/").split("/"):
            return None
        if isinstance(tool, bool) or not isinstance(tool, str) \
                or not tool or len(tool) > 64:
            return None
        if isinstance(mtime, bool) or not isinstance(mtime, (int, float)) \
                or mtime != mtime or mtime in (float("inf"), float("-inf")):
            return None
        if is_dir:
            if not rel.endswith("/"):
                return None
            return {
                "tool": tool, "rel_path": rel, "is_dir": True,
                "size": None, "mtime": float(mtime), "sha256": "",
            }
        if not isinstance(sha, str) or not _SHA16_RE.match(sha):
            return None
        if isinstance(size, bool) or not isinstance(size, int) \
                or not (0 <= size <= MAX_CONFIG_FILE_SIZE):
            return None
        return {
            "tool": tool, "rel_path": rel, "sha256": sha,
            "size": size, "mtime": float(mtime), "is_dir": False,
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
        if isinstance(ri, bool) or not isinstance(ri, int) \
                or not (0 <= ri <= 2000):
            return None
        if isinstance(mtime, bool) or not isinstance(mtime, (int, float)):
            return None
        if is_dir:
            if not path.endswith("/"):
                return None
            return {"root_index": ri, "path": path, "is_dir": True,
                    "size": None, "mtime": float(mtime), "sha256": ""}
        if not isinstance(sha, str) or not _SHA16_RE.match(sha):
            return None
        if isinstance(size, bool) or not isinstance(size, int) \
                or not (0 <= size <= MAX_CONFIG_FILE_SIZE):
            return None
        return {"root_index": ri, "path": path, "sha256": sha,
                "size": size, "mtime": float(mtime), "is_dir": False}

    def _handle_inv(self, payload: dict, peer_id: str) -> None:
        name = payload.get("device_name")
        if not isinstance(name, str):
            name = ""
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            logger.debug("aiconfig_inv from %s has invalid entries", peer_id[:12])
            return
        legacy = not payload.get("v") == 2
        cleaned: list[dict] = []
        for entry in raw_entries[:MAX_ENTRIES]:
            clean = (self._sanitize_legacy_entry(entry) if legacy
                     else self._sanitize_entry(entry))
            if clean is not None:
                cleaned.append(clean)
        with self._lock:
            self._peer_inventories[peer_id] = {
                "name": name[:128],
                "entries": cleaned,
                "fetched_at": time.time(),
                "legacy": legacy,
            }
        logger.info("Stored aiconfig inventory from %s (%d entries, %s)",
                    peer_id[:12], len(cleaned),
                    "legacy" if legacy else "v2")

    def _peer_legacy(self, peer_id: str) -> bool:
        with self._lock:
            inv = self._peer_inventories.get(peer_id)
        return bool(inv and inv.get("legacy"))

    def _handle_req(self, payload: dict, peer_id: str) -> None:
        # Refresh form: the peer wants our current inventory again.
        if payload.get("inventory_refresh"):
            self.send_inventory_to(peer_id)
            return
        if "tool" in payload:
            # v2 form — find the local file by (tool, rel_path).
            tool = payload.get("tool")
            rel = payload.get("rel_path")
            if not isinstance(tool, str) or not tool:
                return
            if not isinstance(rel, str) or not rel:
                return
            known = None
            with self._lock:
                for e in self._local_entries:
                    if e["tool"] == tool and e["path"] == rel.replace("\\", "/"):
                        known = e
                        break
            if known is None:
                logger.debug("aiconfig_req v2 for unadvertised path from %s",
                             peer_id[:12])
                return
            target, reason = self._resolve_local_target(tool, rel,
                                                        require_exists=True)
            if target is None:
                logger.info("aiconfig_req v2 unresolved (%s) from %s: %s",
                            rel, peer_id[:12], reason)
                return
            self._serve_file(tool, rel, target, known, peer_id)
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
            logger.debug("aiconfig_req bad root_index %s from %s", ri,
                         peer_id[:12])
            return
        tool, kind, raw = roots[ri]
        root = expand_root(raw)
        if root is None:
            return
        if kind == "file":
            if rel.replace("\\", "/") != root.name:
                return
            target = root
        else:
            if not root.is_dir():
                return
            target = resolve_safe(root, rel)
            if target is None:
                logger.info("Rejected legacy aiconfig_req with unsafe path from %s",
                            peer_id[:12])
                return
        known = None
        with self._lock:
            for e in self._local_entries:
                if e["root_index"] == ri and e["path"] == rel.replace("\\", "/"):
                    known = e
                    break
        if known is None:
            logger.debug("aiconfig_req legacy for unadvertised path from %s",
                         peer_id[:12])
            return
        self._serve_file(tool, rel, target, known, peer_id)

    def _serve_file(self, tool: str, rel: str, target: Path, known: dict,
                    peer_id: str) -> None:
        if not target.is_file() or target.is_symlink():
            return
        try:
            size = target.stat().st_size
        except OSError:
            return
        truncated = size > MAX_CONFIG_FILE_SIZE
        try:
            with open(target, "rb") as f:
                data = f.read(MAX_CONFIG_FILE_SIZE)
        except OSError:
            return
        digest = hashlib.sha256(data).hexdigest()[:16]
        if not truncated and digest != known["sha256"]:
            # File changed since collection — refuse until the next
            # inventory refresh rather than serving unverified content.
            logger.info("aiconfig_req hash drift for %s — refusing", rel)
            return
        is_v2 = not self._peer_legacy(peer_id)
        payload = {
            "msg_type": "aiconfig_data",
            "sha256": digest,
            "b64_content": base64.b64encode(data).decode("ascii"),
            "truncated": truncated,
        }
        if is_v2:
            payload["tool"] = tool
            payload["rel_path"] = known["path"]
        else:
            payload["root_index"] = known["root_index"]
            payload["rel_path"] = known["path"]
        self._send_frame(payload, peer_id)

    def _handle_data(self, payload: dict, peer_id: str) -> None:
        sha = payload.get("sha256")
        b64 = payload.get("b64_content")
        if not isinstance(sha, str) or not isinstance(b64, str):
            return
        if "tool" in payload:
            tool = payload.get("tool")
            rel = payload.get("rel_path")
            if not isinstance(tool, str) or not tool:
                return
            if not isinstance(rel, str) or not rel:
                return
            key = (peer_id, "v2", tool, rel.replace("\\", "/"))
            pending = self._pop_pending(key)
            if pending is None:
                logger.debug("Unsolicited aiconfig_data (v2) from %s dropped",
                             peer_id[:12])
                return
            self._process_data(key, pending, sha, b64)
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
            logger.debug("Unsolicited aiconfig_data (legacy) from %s dropped",
                         peer_id[:12])
            return
        self._process_data(key, pending, sha, b64)

    def _pop_pending(self, key: tuple) -> dict | None:
        with self._lock:
            return self._pending.pop(key, None)

    def _process_data(self, key: tuple, pending: dict, sha: str, b64: str) -> None:
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
                pending["result"] = {"ok": True, "content": text,
                                     "truncated": len(text) >= PREVIEW_MAX_BYTES}
                event = pending.get("event")
            if event is not None:
                event.set()
            return
        # Landing is v2-only (legacy pulls are blocked at the pull() door), so
        # key[2] is always the tool key here.
        tool, rel = key[2], key[3]
        status, reason = self._land_file(tool, rel, data,
                                         pending.get("mode", "copy"), key[0])
        self._finish_pending(pending, key, status, reason=reason,
                             batch_id=pending.get("batch_id"))

    # ------------------------------------------------------------ landing

    def _land_target(self, tool: str, rel: str) -> tuple[Path | None, str | None]:
        """Exact local file a pulled (tool, rel) should be written to.

        Resolution goes through THIS device's tool profiles (deterministic), so
        no peer root indices are involved.  The target need not exist yet —
        the write creates missing parents.  ``ambiguous_tool_root`` /
        ``no_local_root`` report the two unambiguous failure cases.
        """
        return self._resolve_local_target(tool, rel, require_exists=False)

    def _land_file(self, tool: str, rel: str, data: bytes, mode: str,
                   peer_id: str) -> tuple[str, str | None]:
        """Write pulled bytes per *mode*.  Returns (status, reason)."""
        target, reason = self._land_target(tool, rel)
        if target is None:
            return "error", reason or "no_local_root"
        try:
            if mode == "overwrite":
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".clipsync.tmp")
                tmp.write_bytes(data)
                os.replace(tmp, target)
                return "saved", None
            if mode == "copy":
                stem, ext = os.path.splitext(target.name)
                dev = self._peer_display_name(peer_id)
                safe_dev = (_SANITIZE_RE.sub("_", dev)[:40]) or "peer"
                candidate = target.with_name(f"{stem}.from.{safe_dev}{ext}")
                n = 2
                while candidate.exists():
                    candidate = target.with_name(
                        f"{stem}.from.{safe_dev}-{n}{ext}")
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

    def _finish_pending(self, pending: dict, key: tuple, status: str,
                        reason: str | None = None, batch_id=None) -> None:
        if pending.get("mode") == "preview":
            with self._lock:
                pending["result"] = {"ok": False, "error": reason or status}
                event = pending.get("event")
            if event is not None:
                event.set()
            return
        ws_event = {
            "type": "aiconfig_file",
            "peer_id": key[0],
            "tool": key[2] if key[1] == "v2" else "legacy",
            "rel_path": key[3],
            "status": status,
        }
        if reason:
            ws_event["reason"] = reason
        if batch_id:
            ws_event["batch_id"] = batch_id
        self._emit(ws_event)
        logger.debug("aiconfig_file status=%s reason=%s path=%s",
                     status, reason, key[3])

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

    def _prune_pending(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        expired = [k for k, p in self._pending.items()
                   if now - p.get("ts", now) > PENDING_TTL]
        for k in expired:
            pending = self._pending.pop(k)
            event = pending.get("event")
            if event is not None:
                with self._lock:
                    pending.setdefault(
                        "result", {"ok": False, "error": "timeout"})
                event.set()

    def pull(self, peer_id: str, items, mode: str = "copy",
             batch_id: str = "") -> dict:
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
        with self._lock:
            self._prune_pending()
        for item in expanded[:MAX_ENTRIES]:
            if not isinstance(item, dict):
                errors.append("invalid_item")
                continue
            tool = item.get("tool")
            rel = item.get("rel_path")
            if not isinstance(tool, str) or not tool \
                    or not isinstance(rel, str) or not rel \
                    or len(rel) > MAX_PATH_LEN:
                errors.append("invalid_item")
                continue
            key = (peer_id, "v2", tool, rel.replace("\\", "/"))
            req = {"msg_type": "aiconfig_req", "tool": tool, "rel_path": rel}
            with self._lock:
                self._pending[key] = {"mode": mode, "ts": time.time(),
                                      "batch_id": batch_id}
            if self._send_frame(req, peer_id):
                requested += 1
            else:
                with self._lock:
                    self._pending.pop(key, None)
                errors.append("send_failed")
        return {"requested": requested, "errors": errors,
                "expanded": len(expanded)}

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
            matched = 0
            for e in cache:
                if e.get("is_dir"):
                    continue
                rel = e.get("rel_path") or e.get("path")
                if not rel:
                    continue
                if tool is not None and e.get("tool") != tool:
                    continue
                if rel.startswith(prefix):
                    out.append(e)
                    matched += 1
            if matched == 0:
                out.append(item)  # unknown folder — let the peer refuse cleanly
        return out

    def _preview_wait(self, peer_id: str, req: dict, key: tuple,
                      timeout: float) -> dict:
        """Register a display-only pending, send the request, block for data.

        Blocks up to *timeout* seconds waiting for the peer's aiconfig_data
        (the HTTP worker thread can afford this; ThreadingHTTPServer keeps
        serving other requests meanwhile)."""
        event = threading.Event()
        record: dict = {
            "mode": "preview", "ts": time.time(),
            "event": event, "result": None,
        }
        with self._lock:
            self._prune_pending()
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

    def preview(self, peer_id: str, tool: str, rel: str,
                timeout: float = PREVIEW_TIMEOUT) -> dict:
        """Fetch one v2 file's content for display only — nothing touches disk.

        Returns {"ok", "content" (≤64 KB), "truncated"}; a legacy peer must be
        previewed via ``preview_legacy`` instead."""
        if not self._is_paired(peer_id):
            return {"ok": False, "error": "peer_not_paired"}
        if peer_id not in self._connected_peers():
            return {"ok": False, "error": "peer_offline"}
        if not isinstance(tool, str) or not tool \
                or not isinstance(rel, str) or not rel:
            return {"ok": False, "error": "invalid_item"}
        if self._peer_legacy(peer_id):
            return {"ok": False, "error": "legacy_peer"}
        key = (peer_id, "v2", tool, rel.replace("\\", "/"))
        return self._preview_wait(peer_id, {
            "msg_type": "aiconfig_req", "tool": tool, "rel_path": rel,
        }, key, timeout)

    def preview_legacy(self, peer_id: str, root_index, rel: str,
                       timeout: float = PREVIEW_TIMEOUT) -> dict:
        """Legacy-peer variant: root_index-based request, same display-only
        guarantee.  *root_index* is the int from the peer's cached entry."""
        if not self._is_paired(peer_id):
            return {"ok": False, "error": "peer_not_paired"}
        if peer_id not in self._connected_peers():
            return {"ok": False, "error": "peer_offline"}
        if isinstance(root_index, bool) or not isinstance(root_index, int) \
                or not isinstance(rel, str) or not rel:
            return {"ok": False, "error": "invalid_item"}
        key = (peer_id, "legacy", root_index, rel.replace("\\", "/"))
        return self._preview_wait(peer_id, {
            "msg_type": "aiconfig_req", "root_index": root_index,
            "rel_path": rel,
        }, key, timeout)

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
        return self._send_frame({
            "msg_type": "aiconfig_req", "inventory_refresh": True,
        }, peer_id)

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
            self._local_collected_at = time.time()
        roots = []
        for index, (tool, kind, raw) in enumerate(self._roots()):
            roots.append({
                "root_index": index,
                "tool": tool,
                "kind": kind,
                "path": raw,
                "count": sum(1 for e in entries if e["root_index"] == index),
            })
        listing = [
            {
                "tool": e["tool"],
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
            "tools": [t for t in ai_profiles.TOOLS
                      if t["key"] in set(getattr(self._cfg, "ai_config_tools", []))],
            "custom_paths": list(getattr(self._cfg, "ai_config_custom_paths", [])),
            "roots": roots,
            "entries": listing,
        }

    def local_read(self, tool, rel) -> dict:
        """Read one file's text content (local only, ≤64 KB).

        Returns {"ok", "content", "truncated"}; binary files (any NUL byte
        in the leading window) are refused with error "binary".
        """
        if not isinstance(tool, str) or not isinstance(rel, str):
            return {"ok": False, "error": "invalid_item"}
        target, reason = self._land_target(tool, rel)
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
        return {
            "ok": True,
            "content": data.decode("utf-8", errors="replace"),
            "truncated": size > LOCAL_READ_MAX_BYTES,
        }

    def local_save(self, tool, rel, content) -> dict:
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
        target, reason = self._land_target(tool, rel)
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
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return {"ok": False, "error": "io_error"}
        result: dict = {"ok": True}
        if backup_overwrote:
            result["backup_overwrote"] = True
        return result

    def local_trash(self, tool, rel) -> dict:
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
        target, reason = self._land_target(tool, rel)
        if target is None:
            return {"ok": False, "error": reason or "no_local_root"}
        if target.is_symlink() or not (target.is_file() or target.is_dir()):
            return {"ok": False, "error": "not_found"}
        base = self._trash_base()
        rel_dir = os.path.dirname(rel.replace("\\", "/"))
        dest_dir = base.joinpath(*[p for p in rel_dir.split("/") if p]) \
            if rel_dir else base
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

    def local_open(self, tool, rel) -> dict:
        """Open a watched file (or directory) with the OS default app."""
        if not isinstance(tool, str) or not isinstance(rel, str):
            return {"ok": False, "error": "invalid_item"}
        target, reason = self._land_target(tool, rel)
        if target is None:
            return {"ok": False, "error": reason or "no_local_root"}
        if target.is_symlink() or not (target.is_file() or target.is_dir()):
            return {"ok": False, "error": "not_found"}
        if open_with_default_app(str(target)):
            return {"ok": True}
        return {"ok": False, "error": "open_failed"}
