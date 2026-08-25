"""AI-config sync (Round 12) — metadata inventory + selective file pull.

Each device keeps a user-maintained watch list (``cfg.ai_config_paths``) of
root directories that hold AI tool configuration files: CLAUDE.md, memory
markdown files, skills directories, .mcp.json, etc.  Paired devices exchange
*metadata only* — relative path, sha256 prefix, size, mtime — via
``aiconfig_inv`` frames; either side may then pull an individual file with
``aiconfig_req``, answered by ``aiconfig_data`` carrying base64 content.

Design constraints:
- Paired peers only.  The transport's unpaired gate already drops these
  frame types from unpaired connections; every handler re-checks pairing as
  defense in depth.
- Never silently overwrite.  The receiver lands pulled content according to
  an explicit ``mode`` chosen in the web UI ("overwrite" | "copy" | "append");
  the manager never picks a destructive default itself.
- Bounded.  Collection skips files > 1 MB and temp junk, caps at
  MAX_ENTRIES per device; served/pulled content is capped at MAX_FILE_SIZE;
  b64 decode failures are discarded.

Wire payloads (JSON):
  aiconfig_inv  {"msg_type", "device_name": str, "entries": [entry...]}
      entry = {"path": str(rel posix), "root_index": int,
               "sha256": 16 hex chars, "size": int, "mtime": float}
  aiconfig_req  {"msg_type", "root_index": int, "rel_path": str}
      Special form: {"msg_type", "inventory_refresh": true} asks the peer to
      re-send its inventory now (used by GET /api/aiconfig/inventory?refresh=1).
  aiconfig_data {"msg_type", "root_index": int, "rel_path": str,
                 "sha256": 16 hex, "b64_content": str, "truncated": bool}
"""

import base64
import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path

from internal.protocol.codec import encode_frame

logger = logging.getLogger(__name__)

# Files larger than this are invisible to the feature (never inventoried,
# never served, never landed).  Mirrors the spec's 1 MB cap and keeps every
# aiconfig_data frame far below the transport's 10 MB frame limit even after
# base64 expansion (~1.37x).
MAX_FILE_SIZE = 1024 * 1024
# Hard cap on inventory entries advertised per device — protects both the
# collector (a runaway directory tree) and the wire (inv frame size).
MAX_ENTRIES = 2000
# Watch-list roots accepted per device (set_watch_list sanity bound).
MAX_ROOTS = 50
# rel_path / device-name string bounds on the wire.
MAX_PATH_LEN = 512
# Preview responses are truncated to this many bytes of text.
PREVIEW_MAX_BYTES = 64 * 1024
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
    """Expand one watch-list entry to an absolute Path, or None if unusable.

    Leading "~" expands against *home* (the caller's real home when None so
    tests can inject a fake one).  No environment-variable expansion: watch
    list entries are plain user-typed paths.
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


def _hash_file(path: Path, max_bytes: int = MAX_FILE_SIZE) -> tuple[str, int] | None:
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
    paths,
    home: str | Path | None = None,
    max_bytes: int = MAX_FILE_SIZE,
    max_entries: int = MAX_ENTRIES,
) -> list[dict]:
    """Collect inventory entries across all watch-list roots (pure function).

    For every root: recursive walk, skipping files over *max_bytes*, symlinked
    entries (never followed), and temp junk names.  Paths are emitted relative
    to their root using forward slashes so they are stable across platforms.
    Output is deterministic (sorted within each root).  The global result is
    capped at *max_entries* entries.
    """
    entries: list[dict] = []
    seen_roots: set[str] = set()
    for index, raw in enumerate(paths):
        if not isinstance(raw, str) or len(entries) >= max_entries:
            continue
        root = expand_root(raw, home=home)
        if root is None:
            continue
        root_key = str(root).lower() if os.name == "nt" else str(root)
        if root_key in seen_roots:
            continue  # duplicate root — same files would be advertised twice
        seen_roots.add(root_key)
        if not root.is_dir():
            continue
        found: list[tuple[str, Path, os.stat_result]] = []
        try:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not is_temp_name(d) and not (Path(dirpath) / d).is_symlink()
                )
                for fname in sorted(filenames):
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
        except OSError:
            continue
        for rel, full, st in found:
            if len(entries) >= max_entries:
                break
            hashed = _hash_file(full, max_bytes=max_bytes)
            if hashed is None:
                continue
            digest, size = hashed
            entries.append({
                "path": rel,
                "root_index": index,
                "sha256": digest,
                "size": size,
                "mtime": float(st.st_mtime),
            })
    return entries


def resolve_safe(root: Path, rel: str) -> Path | None:
    """Resolve *rel* inside *root*, refusing any escape attempt.

    Rejects absolute paths, drive prefixes, ".." segments (after normalizing
    both separator styles) and anything whose resolved location falls outside
    the resolved root.  Returns an absolute Path on success.
    """
    if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH_LEN:
        return None
    if "\x00" in rel:
        return None
    norm = rel.replace("\\", "/")
    # Absolute forms are refused outright: windows drive prefixes and both
    # leading-separator styles ("/etc/passwd", "\\server\\share").
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


class AIConfigManager:
    """Owns the local AI-config inventory, peer inventory cache, request
    routing and landing logic.  All public methods are thread-safe."""

    def __init__(self, cfg, send_fn, connected_fn=None, event_fn=None,
                 save_fn=None):
        """*cfg* must expose ``device_id``, ``device_name``,
        ``ai_config_paths`` and ``peers`` (dict of PeerInfo-like objects with
        ``paired``/``device_name``); it is read live so setting changes take
        effect without re-wiring.

        *send_fn(peer_id, frame_bytes)* transmits one frame;
        *connected_fn()* returns currently connected peer ids;
        *event_fn(dict)* receives WS-bound events (e.g. aiconfig_file);
        *save_fn()* persists the config after set_watch_list().
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
        # peer_id -> {"name": str, "entries": [...], "fetched_at": float}
        self._peer_inventories: dict[str, dict] = {}
        # (peer_id, root_index, rel_path) -> pending record.  A pending record
        # carries the landing mode; preview records additionally hold a
        # threading.Event + result slot for the synchronous REST response.
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

    @staticmethod
    def _sanitize_entry(entry) -> dict | None:
        """Validate one wire inventory entry; None = drop it."""
        if not isinstance(entry, dict):
            return None
        path = entry.get("path")
        ri = entry.get("root_index")
        sha = entry.get("sha256")
        size = entry.get("size")
        mtime = entry.get("mtime")
        if not isinstance(path, str) or not path or len(path) > MAX_PATH_LEN:
            return None
        if ".." in path.replace("\\", "/").split("/"):
            return None
        if isinstance(ri, bool) or not isinstance(ri, int) \
                or not (0 <= ri <= MAX_ROOTS):
            return None
        if not isinstance(sha, str) or not _SHA16_RE.match(sha):
            return None
        if isinstance(size, bool) or not isinstance(size, int) \
                or not (0 <= size <= MAX_FILE_SIZE):
            return None
        if isinstance(mtime, bool) or not isinstance(mtime, (int, float)) \
                or mtime != mtime or mtime in (float("inf"), float("-inf")):
            return None
        return {
            "path": path,
            "root_index": ri,
            "sha256": sha,
            "size": size,
            "mtime": float(mtime),
        }

    # ---------------------------------------------------------- inventory

    def collect(self) -> list[dict]:
        """Re-scan the watch list and remember the result."""
        entries = collect_roots(list(getattr(self._cfg, "ai_config_paths", [])))
        with self._lock:
            self._local_entries = entries
            self._local_collected_at = time.time()
        return entries

    def build_inv_payload(self) -> dict:
        with self._lock:
            entries = [dict(e) for e in self._local_entries]
        return {
            "msg_type": "aiconfig_inv",
            "device_name": str(getattr(self._cfg, "device_name", "")),
            "entries": entries,
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
        """Watch list was edited (web settings / API) — recollect + rebroadcast."""
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

    def _handle_inv(self, payload: dict, peer_id: str) -> None:
        name = payload.get("device_name")
        if not isinstance(name, str):
            name = ""
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            logger.debug("aiconfig_inv from %s has invalid entries", peer_id[:12])
            return
        cleaned: list[dict] = []
        for entry in raw_entries[:MAX_ENTRIES]:
            clean = self._sanitize_entry(entry)
            if clean is not None:
                cleaned.append(clean)
        with self._lock:
            self._peer_inventories[peer_id] = {
                "name": name[:128],
                "entries": cleaned,
                "fetched_at": time.time(),
            }
        logger.info("Stored aiconfig inventory from %s (%d entries)",
                    peer_id[:12], len(cleaned))

    def _handle_req(self, payload: dict, peer_id: str) -> None:
        # Refresh form: the peer wants our current inventory again.
        if payload.get("inventory_refresh"):
            self.send_inventory_to(peer_id)
            return
        ri = payload.get("root_index")
        rel = payload.get("rel_path")
        if isinstance(ri, bool) or not isinstance(ri, int):
            return
        if not isinstance(rel, str) or not rel:
            return
        roots = list(getattr(self._cfg, "ai_config_paths", []))
        if not (0 <= ri < len(roots)):
            logger.debug("aiconfig_req bad root_index %s from %s", ri,
                         peer_id[:12])
            return
        root = expand_root(roots[ri])
        if root is None or not root.is_dir():
            return
        target = resolve_safe(root, rel)
        if target is None:
            logger.info("Rejected aiconfig_req with unsafe path from %s",
                        peer_id[:12])
            return
        # The file must be part of our CURRENT inventory (i.e. we advertised
        # it) and its content hash must still match what we advertised.
        with self._lock:
            known = next(
                (e for e in self._local_entries
                 if e["root_index"] == ri and e["path"]
                 == rel.replace("\\", "/")),
                None,
            )
        if known is None:
            logger.debug("aiconfig_req for unadvertised path from %s",
                         peer_id[:12])
            return
        if not target.is_file() or target.is_symlink():
            return
        try:
            size = target.stat().st_size
        except OSError:
            return
        truncated = size > MAX_FILE_SIZE
        try:
            with open(target, "rb") as f:
                data = f.read(MAX_FILE_SIZE)
        except OSError:
            return
        digest = hashlib.sha256(data).hexdigest()[:16]
        if not truncated and digest != known["sha256"]:
            # File changed since collection — refuse until the next
            # inventory refresh rather than serving unverified content.
            logger.info("aiconfig_req hash drift for %s — refusing", rel)
            return
        self._send_frame({
            "msg_type": "aiconfig_data",
            "root_index": ri,
            "rel_path": known["path"],
            "sha256": digest,
            "b64_content": base64.b64encode(data).decode("ascii"),
            "truncated": truncated,
        }, peer_id)

    def _handle_data(self, payload: dict, peer_id: str) -> None:
        ri = payload.get("root_index")
        rel = payload.get("rel_path")
        sha = payload.get("sha256")
        b64 = payload.get("b64_content")
        if isinstance(ri, bool) or not isinstance(ri, int):
            return
        if not isinstance(rel, str) or not rel:
            return
        if not isinstance(sha, str) or not isinstance(b64, str):
            return
        key = (peer_id, ri, rel.replace("\\", "/"))
        with self._lock:
            pending = self._pending.pop(key, None)
        if pending is None:
            logger.debug("Unsolicited aiconfig_data from %s dropped",
                         peer_id[:12])
            return
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            logger.info("aiconfig_data b64 decode failed from %s",
                        peer_id[:12])
            self._finish_pending(pending, key, "error", reason="b64_decode")
            return
        if len(data) > MAX_FILE_SIZE:
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
        status, reason = self._land_file(
            ri, rel, data, pending.get("mode", "copy"), peer_id)
        self._finish_pending(pending, key, status, reason=reason)

    # ------------------------------------------------------------ landing

    def _local_root(self, ri: int) -> Path | None:
        roots = list(getattr(self._cfg, "ai_config_paths", []))
        if not (0 <= ri < len(roots)):
            return None
        root = expand_root(roots[ri])
        if root is None:
            return None
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return root

    def _land_file(self, ri: int, rel: str, data: bytes, mode: str,
                   peer_id: str) -> tuple[str, str | None]:
        """Write pulled bytes per *mode*.  Returns (status, reason)."""
        root = self._local_root(ri)
        if root is None:
            return "error", "no_local_root"
        target = resolve_safe(root, rel)
        if target is None:
            return "error", "unsafe_path"
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
                # Newline-separated: open the existing file with a boundary
                # newline when needed; never stack a blank line when the
                # incoming chunk already ends with one.
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
                        reason: str | None = None) -> None:
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
            "rel_path": key[2],
            "status": status,
        }
        if reason:
            ws_event["reason"] = reason
        self._emit(ws_event)
        logger.debug("aiconfig_file status=%s reason=%s path=%s",
                     status, reason, key[2])

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

    def pull(self, peer_id: str, items, mode: str = "copy") -> dict:
        """Request several files from a paired peer (REST pull backend).

        Returns {"requested": N, "errors": [..]} without waiting for the
        async aiconfig_data replies — those land via _handle_data and are
        reported through the aiconfig_file WS event.
        """
        if mode not in ("overwrite", "copy", "append"):
            mode = "copy"  # never let a malformed request turn destructive
        if not self._is_paired(peer_id):
            return {"requested": 0, "errors": ["peer_not_paired"]}
        if peer_id not in self._connected_peers():
            return {"requested": 0, "errors": ["peer_offline"]}
        if not isinstance(items, list):
            return {"requested": 0, "errors": ["items_required"]}
        requested = 0
        errors: list[str] = []
        with self._lock:
            self._prune_pending()
        for item in items[:200]:
            if not isinstance(item, dict):
                errors.append("invalid_item")
                continue
            ri = item.get("root_index")
            rel = item.get("rel_path")
            if isinstance(ri, bool) or not isinstance(ri, int) \
                    or not isinstance(rel, str) or not rel \
                    or len(rel) > MAX_PATH_LEN:
                errors.append("invalid_item")
                continue
            key = (peer_id, ri, rel.replace("\\", "/"))
            with self._lock:
                self._pending[key] = {"mode": mode, "ts": time.time()}
            if self._send_frame({
                "msg_type": "aiconfig_req",
                "root_index": ri,
                "rel_path": rel,
            }, peer_id):
                requested += 1
            else:
                with self._lock:
                    self._pending.pop(key, None)
                errors.append("send_failed")
        return {"requested": requested, "errors": errors}

    def preview(self, peer_id: str, ri: int, rel: str,
                timeout: float = PREVIEW_TIMEOUT) -> dict:
        """Fetch one file's content for display only — nothing touches disk.

        Blocks up to *timeout* seconds waiting for the peer's aiconfig_data
        (the HTTP worker thread can afford this; ThreadingHTTPServer keeps
        serving other requests meanwhile)."""
        if not self._is_paired(peer_id):
            return {"ok": False, "error": "peer_not_paired"}
        if peer_id not in self._connected_peers():
            return {"ok": False, "error": "peer_offline"}
        if isinstance(ri, bool) or not isinstance(ri, int) \
                or not isinstance(rel, str) or not rel:
            return {"ok": False, "error": "invalid_item"}
        key = (peer_id, ri, rel.replace("\\", "/"))
        event = threading.Event()
        record: dict = {
            "mode": "preview", "ts": time.time(),
            "event": event, "result": None,
        }
        with self._lock:
            self._prune_pending()
            self._pending[key] = record
        if not self._send_frame({
            "msg_type": "aiconfig_req",
            "root_index": ri,
            "rel_path": rel,
        }, peer_id):
            with self._lock:
                self._pending.pop(key, None)
            return {"ok": False, "error": "send_failed"}
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(key, None)
            return {"ok": False, "error": "timeout"}
        # The data handler popped the registry entry and wrote "result"
        # into this very record BEFORE setting the event, so reading the
        # record here (happens-after the Event) is race-free.
        result = record.get("result")
        return result or {"ok": False, "error": "no_data"}

    # ---------------------------------------------------------------- REST

    def get_peer_inventories(self) -> dict:
        """Snapshot of cached peer inventories for GET /api/aiconfig/inventory."""
        with self._lock:
            out = {}
            for pid, inv in self._peer_inventories.items():
                out[pid] = {
                    "name": inv.get("name", ""),
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
                "paths": list(getattr(self._cfg, "ai_config_paths", [])),
            }

    def set_watch_list(self, paths) -> dict:
        """Normalize + persist the watch list; returns the stored value."""
        if not isinstance(paths, list):
            return {"ok": False, "error": "list_required"}
        cleaned: list[str] = []
        for raw in paths[:MAX_ROOTS]:
            if not isinstance(raw, str):
                continue
            s = raw.strip()
            if s and s not in cleaned:
                cleaned.append(s)
        self._cfg.ai_config_paths = cleaned
        if self._save_fn is not None:
            try:
                self._save_fn()
            except Exception:
                logger.debug("aiconfig watch-list persist failed",
                             exc_info=True)
        return {"ok": True, "paths": cleaned}
