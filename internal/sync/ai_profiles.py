"""Canonical AI-tool config profiles — single source of truth for the AI-config
sync feature.

Each profile describes one AI coding tool's config files and skills /
commands directories as watch *entries*.  The profile structure is identical
on every device (it ships in the binary), so a peer's inventory entries name
their ``tool`` AND the stable ``id`` of the entry they came from, and the
receiving side resolves a landing target through the SAME profile.

The entry ``id`` is what makes that resolution unambiguous.  A ``rel_path``
travels relative to *its own root*, so ``tool`` alone is not a root identity
once a tool has more than one directory entry (Claude Code's ``skills`` +
``commands`` + ``agents``): both roots would claim the same rel_path.  Ids are
stable and positional indices are not — a table edit shifts every later index
and would land files in the wrong folder on a peer running another build — so
ids cross the wire and indices stay internal.

**Never rename an id**: an id IS the cross-device identity of a root, so
renaming one is indistinguishable from replacing it with a different root.

An entry is either a single config FILE (e.g. ``~/.claude/CLAUDE.md`` — that
exact file, keeping credentials like ``auth.json`` out) or a DIRECTORY that is
recursively walked (``~/.claude/skills``).  Two optional keys exist because
real tools need them:

- ``paths``: per-platform overrides keyed by ``sys.platform``, for tools whose
  config genuinely lives elsewhere on another OS (goose keeps it under
  ``%APPDATA%/Block/goose/config`` on Windows but ``~/.config/goose``
  everywhere else).
- ``exclude``: glob patterns, relative to a dir entry, that are never
  collected.  Credentials sit *next to* config in several tools (goose's
  plaintext ``secrets.yaml`` beside ``config.yaml``), so a directory root
  needs to be able to carve them out.

Besides the built-in tools there is always a pseudo-tool ``"custom"`` holding
user-added watch paths; its roots are whatever the user typed, so their file/
dir kind is inferred at collection time and their root id is the normalized
path itself (two devices that watch the same path therefore agree on it).
"""

from __future__ import annotations

import sys
from typing import Literal

# Entry kinds: "file" = exactly that file; "dir" = recursive directory walk.
EntryKind = Literal["file", "dir"]

# Pseudo-tool key for user-added watch paths (never a built-in profile).
CUSTOM_KEY = "custom"

# Built-in tool profiles, in display order.  ``entries`` paths are plain
# user-style paths ("~" expands against the real home at collection time),
# stored relative so the same table is meaningful on every OS.  Each entry's
# ``id`` is its permanent cross-device identity — unique within the tool, and
# never renamed once shipped (see the module docstring).
TOOLS: list[dict] = [
    {
        "key": "claude_code",
        "label": "Claude Code",
        "entries": [
            {"id": "memory", "path": "~/.claude/CLAUDE.md", "kind": "file"},
            {"id": "settings", "path": "~/.claude/settings.json", "kind": "file"},
            {"id": "skills", "path": "~/.claude/skills", "kind": "dir"},
            {"id": "commands", "path": "~/.claude/commands", "kind": "dir"},
            {"id": "agents", "path": "~/.claude/agents", "kind": "dir"},
        ],
    },
    {
        "key": "codex",
        "label": "Codex",
        "entries": [
            {"id": "config", "path": "~/.codex/config.toml", "kind": "file"},
        ],
    },
    {
        "key": "cursor",
        "label": "Cursor",
        "entries": [
            {"id": "rules", "path": "~/.cursor/rules", "kind": "dir"},
            {"id": "commands", "path": "~/.cursor/commands", "kind": "dir"},
        ],
    },
    {
        "key": "gemini",
        "label": "Gemini CLI",
        "entries": [
            {"id": "settings", "path": "~/.gemini/settings.json", "kind": "file"},
            {"id": "memory", "path": "~/.gemini/GEMINI.md", "kind": "file"},
            {"id": "skills", "path": "~/.gemini/skills", "kind": "dir"},
            {"id": "commands", "path": "~/.gemini/commands", "kind": "dir"},
        ],
    },
]

# Enabled-by-default tool keys (the four presets, in display order).
DEFAULT_TOOL_KEYS: list[str] = [t["key"] for t in TOOLS]

# Max built-in tools / user paths accepted — sanity bounds mirroring the old
# MAX_ROOTS cap (custom paths plus every built-in entry stay far below).
MAX_TOOL_KEYS = len(TOOLS) + 8
MAX_CUSTOM_PATHS = 50

# Bounds for the two long-term schema knobs.  Root ids cross the wire, so they
# are length-capped like any other untrusted string.
MAX_ROOT_ID_LEN = 256
MAX_EXCLUDES = 20


def tool_by_key(key: str) -> dict | None:
    """Return the built-in profile dict for *key*, or None."""
    if not isinstance(key, str):
        return None
    for tool in TOOLS:
        if tool["key"] == key:
            return tool
    return None


def validate_tool_keys(keys) -> list[str]:
    """Dedupe + bound a list of tool keys against the known profiles."""
    if not isinstance(keys, list):
        return []
    seen: list[str] = []
    for raw in keys:
        if not isinstance(raw, str):
            continue
        k = raw.strip()
        if k and tool_by_key(k) is not None and k not in seen:
            seen.append(k)
    return seen[:MAX_TOOL_KEYS]


def validate_custom_paths(paths) -> list[str]:
    """Dedupe + bound a list of user custom watch paths."""
    if not isinstance(paths, list):
        return []
    cleaned: list[str] = []
    for raw in paths:
        if not isinstance(raw, str):
            continue
        s = raw.strip()
        if s and s not in cleaned:
            cleaned.append(s)
    return cleaned[:MAX_CUSTOM_PATHS]


def entry_path(entry: dict, platform: str | None = None) -> str:
    """The user-style path for *entry* on *platform* (default: this machine).

    ``paths`` overrides ``path`` for platforms that genuinely differ; every
    other platform falls through to ``path`` so the common case stays a single
    string.
    """
    if not isinstance(entry, dict):
        return ""
    plat = platform if isinstance(platform, str) else sys.platform
    per_os = entry.get("paths")
    if isinstance(per_os, dict):
        override = per_os.get(plat)
        if isinstance(override, str) and override.strip():
            return override.strip()
    path = entry.get("path")
    return path.strip() if isinstance(path, str) else ""


def entry_excludes(entry: dict) -> list[str]:
    """Glob patterns under a dir *entry* that are never collected."""
    if not isinstance(entry, dict):
        return []
    raw = entry.get("exclude")
    if not isinstance(raw, list):
        return []
    return [p.strip() for p in raw if isinstance(p, str) and p.strip()][:MAX_EXCLUDES]


def custom_root_id(path: str) -> str:
    """Stable root id for a custom watch *path* — the normalized path itself.

    Custom paths are per-device text, but two devices that watch the same path
    should agree on its identity, so the path IS the id rather than an opaque
    hash (it also keeps inventories readable when debugging).
    """
    return _norm_path(path)[:MAX_ROOT_ID_LEN]


def valid_root_id(value) -> bool:
    """Whether *value* is acceptable as a root id arriving off the wire."""
    if not isinstance(value, str) or not value or len(value) > MAX_ROOT_ID_LEN:
        return False
    return not any(ch == "\x00" or ch in "\r\n" for ch in value)


def effective_roots(tool_keys, custom_paths):
    """Flatten enabled tool profiles + custom paths into watch roots.

    Returns a list of ``(tool, root_id, kind, path)`` tuples in deterministic
    order — one per built-in profile entry, then the custom paths (``"dir"``
    here means "treat as directory root", which the collector falls back to for
    a custom path that does not exist yet).  ``root_id`` is the entry's stable
    id, which travels on the wire so a peer can tell two dir roots of the same
    tool apart.

    A custom path that duplicates a built-in entry path is dropped: it would
    otherwise produce two roots over one physical directory, advertising every
    file twice under two different tools.  This also lets a custom path that
    was only ever a workaround for a missing profile entry retire itself once
    the entry ships.
    """
    roots: list[tuple[str, str, EntryKind, str]] = []
    for key in validate_tool_keys(tool_keys):
        tool = tool_by_key(key)
        if tool is None:
            continue
        for entry in tool["entries"]:
            path = entry_path(entry)
            if path:
                roots.append((key, entry["id"], entry["kind"], path))
    builtin = all_tool_entry_paths()
    for path in validate_custom_paths(custom_paths):
        if _norm_path(path) in builtin:
            continue
        roots.append((CUSTOM_KEY, custom_root_id(path), "dir", path))
    return roots


def root_entry(tool_key: str, root_id: str) -> dict | None:
    """The profile entry a ``(tool, root_id)`` pair names, or None."""
    tool = tool_by_key(tool_key)
    if tool is None:
        return None
    for entry in tool["entries"]:
        if entry["id"] == root_id:
            return entry
    return None


def all_tool_entry_paths() -> set[str]:
    """Every built-in profile entry path, normalized — used to classify old
    ``ai_config_paths`` during migration, and to keep a custom path from
    duplicating a root the profile table already covers.

    Every platform variant is included, not just this machine's, so a config
    synced from another OS still classifies the same way."""
    out: set[str] = set()
    for tool in TOOLS:
        for entry in tool["entries"]:
            out.add(_norm_path(entry["path"]))
            per_os = entry.get("paths")
            if isinstance(per_os, dict):
                for variant in per_os.values():
                    if isinstance(variant, str) and variant.strip():
                        out.add(_norm_path(variant))
    return out


def _norm_path(path: str) -> str:
    """Normalize a user-style path for equality comparison (migration only)."""
    return path.strip().replace("\\", "/").rstrip("/")


def migrate_watch_paths(old_paths) -> tuple[list[str], list[str]]:
    """Classify a legacy ``ai_config_paths`` list into (tool_keys, custom_paths).

    A tool is enabled when ANY of its profile entry paths appears in the old
    list (the user clearly wants that tool synced); the profile expansion then
    adds any entries the old list was missing.  Paths that match no built-in
    profile entry become custom paths.  Unknown/blank entries are dropped.
    """
    if not isinstance(old_paths, list):
        return list(DEFAULT_TOOL_KEYS), []
    normalized = [p.strip() for p in old_paths if isinstance(p, str) and p.strip()]
    known = all_tool_entry_paths()
    enabled: list[str] = []
    leftover: list[str] = []
    for tool in TOOLS:
        entry_paths = {_norm_path(e["path"]) for e in tool["entries"]}
        if any(_norm_path(p) in entry_paths for p in normalized):
            enabled.append(tool["key"])
    matched: set[str] = set()
    for key in enabled:
        tool = tool_by_key(key)
        for entry in tool["entries"]:
            matched.add(_norm_path(entry["path"]))
    for p in normalized:
        if _norm_path(p) in matched or _norm_path(p) in known:
            continue
        leftover.append(p)
    return enabled or list(DEFAULT_TOOL_KEYS), validate_custom_paths(leftover)
