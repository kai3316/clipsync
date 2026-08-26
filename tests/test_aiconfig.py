"""Refactor round 1 — AI-config sync (codec, config migration, tool profiles,
collector, manager, REST).

The feature now runs on TOOL PROFILES (internal/sync/ai_profiles.py) instead
of a raw watch-path list: a device enables built-in profiles (Claude Code,
Codex, Cursor, Gemini) plus user custom paths, inventory entries carry a
``tool`` key (never a root index across the wire), and landing targets are
resolved through THIS device's profile table — the old ambiguous_root /
no_local_root mapping errors are gone.

Handlers are exercised through lightweight stubs: no app/tkinter/transport
stack boots here, and every frame crosses the wire through the real codec.

Coverage per the refactor plan:
  - profile expansion / validation / legacy watch-path migration helpers
  - collector emits (tool, rel_path) v2 entries; folders via is_dir
  - inv roundtrip + sanitization; legacy (root_index) inv cached read-only
  - req serves verified data by tool; hash drift / traversal refused
  - pull lands per mode with batch_id echoed through aiconfig_file events
  - folder whole-select expansion pulls every descendant file recursively
  - preview (v2 + legacy) never touches disk
  - /api/aiconfig/profiles GET/POST, pull/inventory/local REST routes
  - settings whitelist carries the new profile fields
"""

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.protocol import codec
from internal.protocol.codec import decode_message, encode_frame
from internal.sync import ai_profiles
from internal.sync.ai_config import (
    MAX_ENTRIES,
    MAX_PATH_LEN,
    LOCAL_SAVE_MAX_BYTES,
    AIConfigManager,
    collect_roots,
    expand_root,
    is_temp_name,
    resolve_safe,
    open_with_default_app,
)

# ------------------------------------------------------------------ codec

def test_aiconfig_frames_roundtrip():
    payloads = {
        "aiconfig_inv": {"v": 2, "device_name": "DevA",
                         "entries": [{"tool": "claude_code",
                                      "rel_path": "CLAUDE.md",
                                      "sha256": "ab" * 8, "size": 12,
                                      "mtime": 123.5}]},
        "aiconfig_req": {"tool": "claude_code", "rel_path": "skills/x/SKILL.md"},
        "aiconfig_data": {"tool": "claude_code", "rel_path": "skills/x/SKILL.md",
                          "sha256": "cd" * 8, "b64_content": "aGk=",
                          "truncated": False},
    }
    for msg_type, extra in payloads.items():
        raw = encode_frame({"msg_type": msg_type, **extra},
                           source_device="device-A")
        msg = decode_message(raw)
        assert getattr(msg, "msg_type", "") == msg_type
        assert msg.source_device == "device-A"
        for k, v in extra.items():
            assert msg._raw_payload[k] == v


def test_aiconfig_types_are_paired_only():
    # must never be admitted from unpaired peers at the transport gate
    assert set(codec.AICONFIG_MSG_TYPES) == {
        "aiconfig_inv", "aiconfig_req", "aiconfig_data"}
    assert not (codec.AICONFIG_MSG_TYPES & codec.UNPAIRED_GATE_MSG_TYPES)


# ------------------------------------------------------------------ config

@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    from internal.config import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg_mod, "_config_path",
                        lambda: tmp_path / "config.json")
    yield cfg_mod


def test_config_defaults_enable_all_tool_profiles(isolated_config):
    cfg_mod = isolated_config
    cfg = cfg_mod.Config()
    assert cfg.ai_config_tools == ai_profiles.DEFAULT_TOOL_KEYS
    assert cfg.ai_config_custom_paths == []
    # credentials must never be advertised by a built-in profile
    joined = json.dumps(ai_profiles.TOOLS)
    assert "auth.json" not in joined
    assert ".credentials" not in joined


def test_config_profiles_roundtrip(isolated_config):
    cfg_mod = isolated_config
    cfg = cfg_mod.Config()
    cfg.ai_config_tools = ["claude_code", "cursor"]
    cfg.ai_config_custom_paths = ["~/ai-configs", "D:\\notes"]
    cfg_mod.save(cfg)
    loaded = cfg_mod.load()
    assert loaded.ai_config_tools == ["claude_code", "cursor"]
    assert loaded.ai_config_custom_paths == ["~/ai-configs", "D:\\notes"]
    # the old raw watch-list field is gone from the model
    assert not hasattr(loaded, "ai_config_paths")


def test_config_legacy_watch_paths_migrate_to_profiles(isolated_config):
    cfg_mod = isolated_config
    path = cfg_mod._config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "ai_config_paths": ["~/.claude/CLAUDE.md",
                            "~/.codex/config.toml",
                            "~/my-notes"],
    }), encoding="utf-8")
    loaded = cfg_mod.load()
    # a tool is enabled when ANY of its profile entries appeared in the old list
    assert set(loaded.ai_config_tools) == {"claude_code", "codex"}
    # the unmatched path becomes a user custom path
    assert "~/my-notes" in loaded.ai_config_custom_paths


def test_config_legacy_bad_type_falls_back_to_all_tools(isolated_config):
    cfg_mod = isolated_config
    path = cfg_mod._config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ai_config_paths": "not-a-list"}),
                    encoding="utf-8")
    loaded = cfg_mod.load()
    # malformed legacy list → the (non-empty) all-tools default
    assert loaded.ai_config_tools == ai_profiles.DEFAULT_TOOL_KEYS


# --------------------------------------------------------------- profiles

def test_effective_roots_expands_enabled_profiles_and_custom():
    # no tools, no custom paths -> feature off
    assert ai_profiles.effective_roots([], []) == []
    # all four built-ins -> every profile entry, in display order
    roots = ai_profiles.effective_roots(ai_profiles.DEFAULT_TOOL_KEYS, [])
    assert len(roots) == 8
    assert [r[0] for r in roots] == (
        ["claude_code"] * 3 + ["codex"] * 1 + ["cursor"] * 2 + ["gemini"] * 2)
    assert ("claude_code", "file", "~/.claude/CLAUDE.md") in roots
    assert ("claude_code", "dir", "~/.claude/skills") in roots
    assert ("cursor", "dir", "~/.cursor/rules") in roots
    # custom paths ride along as pseudo-tool "custom"
    roots = ai_profiles.effective_roots(["claude_code"], ["~/extra", "D:\\n"])
    assert ("claude_code", "file", "~/.claude/CLAUDE.md") in roots
    assert ("custom", "dir", "~/extra") in roots
    assert ("custom", "dir", "D:\\n") in roots


def test_validate_tool_keys_dedupes_bounds_and_rejects_junk():
    keys = ai_profiles.validate_tool_keys(
        [" claude_code ", "codex", "claude_code", "bogus", 42, None])
    assert keys == ["claude_code", "codex"]
    assert ai_profiles.validate_tool_keys("nope") == []
    assert len(ai_profiles.validate_tool_keys(
        ["claude_code"] * 999)) <= ai_profiles.MAX_TOOL_KEYS


def test_validate_custom_paths_cleans_and_bounds():
    paths = ai_profiles.validate_custom_paths(
        ["  ~/a ", "", "~/a", "D:\\n", None])
    assert paths == ["~/a", "D:\\n"]
    # dedupe happens BEFORE the cap, so 999 identical paths collapse to one
    assert ai_profiles.validate_custom_paths(["x"] * 999) == ["x"]
    assert len(ai_profiles.validate_custom_paths(
        [f"x{i}" for i in range(999)])) == ai_profiles.MAX_CUSTOM_PATHS


def test_migrate_watch_paths_classification():
    # no tools mentioned -> all tools enabled by default (feature stays on)
    keys, custom = ai_profiles.migrate_watch_paths(["~/custom/dir"])
    assert keys == ai_profiles.DEFAULT_TOOL_KEYS
    assert custom == ["~/custom/dir"]
    # profile paths enable their tool; unknown paths become custom
    keys, custom = ai_profiles.migrate_watch_paths(
        ["~/.claude/CLAUDE.md", "~/.cursor/commands", "~/notes"])
    assert keys == ["claude_code", "cursor"]
    assert custom == ["~/notes"]
    # non-list input -> all tools, no custom
    assert ai_profiles.migrate_watch_paths(None) == (
        ai_profiles.DEFAULT_TOOL_KEYS, [])


# --------------------------------------------------------------- collector

def _mk(root, rel, content=b"data"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_collect_expands_home_and_relative_posix_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _mk(home / "airoot", "CLAUDE.md", b"# rules\n")
    _mk(home / "airoot", "skills/deep/SKILL.md", b"skill")
    entries = collect_roots([("custom", "dir", "~/airoot")], home=home)
    assert len(entries) == 2
    by_path = {e["path"]: e for e in entries}
    assert "skills/deep/SKILL.md" in by_path  # forward slashes, relative
    ent = by_path["CLAUDE.md"]
    assert ent["tool"] == "custom"
    assert ent["root_index"] == 0
    assert ent["sha256"] == hashlib.sha256(b"# rules\n").hexdigest()[:16]
    assert ent["size"] == len(b"# rules\n")
    st = (home / "airoot" / "CLAUDE.md").stat()
    assert abs(ent["mtime"] - st.st_mtime) < 1.0


def test_collect_tool_profile_root_tags_every_entry(tmp_path):
    home = tmp_path / "home"
    _mk(home, "CLAUDE.md", b"# rules\n")
    _mk(home / "skills", "a/SKILL.md", b"skill")
    entries = collect_roots(
        [("claude_code", "file", "~/CLAUDE.md"),
         ("claude_code", "dir", "~/skills")],
        home=home)
    by_path = {e["path"]: e for e in entries}
    assert by_path["CLAUDE.md"]["tool"] == "claude_code"
    assert by_path["a/SKILL.md"]["tool"] == "claude_code"
    # duplicate roots of the SAME tool are deduped by (tool, rel)
    entries_dup = collect_roots(
        [("claude_code", "dir", "~/skills"),
         ("claude_code", "dir", "~/skills")],
        home=home)
    assert len(entries_dup) == 1
    # a different tool pointing at the same directory is a distinct entry
    entries2 = collect_roots(
        [("claude_code", "dir", "~/skills"),
         ("gemini", "dir", "~/skills")],
        home=home)
    assert len(entries2) == 2
    assert {e["tool"] for e in entries2} == {"claude_code", "gemini"}


def test_collect_filters_oversize_temp_and_symlinks(tmp_path):
    home = tmp_path / "home"
    root = home / "r"
    _mk(root, "keep.md", b"ok")
    _mk(root, "big.bin", b"x" * 11)
    _mk(root, "junk.tmp", b"tmp")
    _mk(root, "~$doc.md", b"office")
    _mk(root, ".DS_Store", b"junk")
    _mk(root, "Thumbs.db", b"junk")
    _mk(root, ".claude/settings.json", b"{}")  # dotfiles are KEPT
    link = root / "link.md"
    try:
        os.symlink(root / "keep.md", link)
    except OSError:
        link = None  # Windows without symlink privilege
    entries = collect_roots([("custom", "dir", "~/r")], home=home, max_bytes=10)
    paths = {e["path"] for e in entries}
    assert paths == {"keep.md", ".claude/settings.json"}
    if link is not None:
        assert "link.md" not in paths


def test_collect_caps_entries_and_skips_missing_or_duplicate_roots(tmp_path):
    root = tmp_path / "cap"
    for i in range(8):
        _mk(root, f"f{i}.md", b"x")
    entries = collect_roots(
        [("custom", "dir", str(root)),
         ("custom", "dir", str(tmp_path / "missing")),
         ("custom", "dir", str(root))],
        max_entries=3)
    assert len(entries) == 3  # capped; missing root skipped; dup root once


def test_collect_folder_entries_carry_is_dir(tmp_path):
    root = tmp_path / "r"
    _mk(root, "skills/my-skill/SKILL.md", b"# sk")
    entries = collect_roots([("custom", "dir", str(root))], include_dirs=True)
    by_path = {e["path"]: e for e in entries}
    assert by_path["skills/my-skill/"]["is_dir"] is True
    assert by_path["skills/my-skill/"]["size"] is None
    assert by_path["skills/my-skill/"]["sha256"] == ""
    assert by_path["skills/my-skill/SKILL.md"]["is_dir"] is False


def test_expand_root_forms():
    assert expand_root("~", home="/h") is not None
    assert str(expand_root("~/sub", home="/h")).replace("\\", "/") == "/h/sub"
    assert expand_root("C:\\x") is not None
    assert expand_root("") is None
    assert expand_root(None) is None


def test_resolve_safe_rejects_traversal_and_absolute():
    base = Path(tempfile.mkdtemp())
    ok = resolve_safe(base, "a/b.md")
    assert ok is not None and ok.is_absolute()
    for bad in ("../escape.md", "a/../../escape.md", "..\\win.md",
                "/etc/passwd", "C:/Windows/system32/x", "C:\\x",
                "", None, "a\x00b", "x" * 600):
        assert resolve_safe(base, bad) is None, bad
    assert resolve_safe(base, "./a/./b.md") is not None  # dot segments fine


def test_is_temp_name():
    for junk in ("x.tmp", "~$report.md", ".DS_Store", "Thumbs.db",
                 "notes.swp"):
        assert is_temp_name(junk), junk
    for keep in ("CLAUDE.md", ".mcp.json", "settings.json", "SKILL.md"):
        assert not is_temp_name(keep), keep


# ----------------------------------------------------------------- manager

class _StubMgr:
    """AIConfigManager wired to recording stubs; cfg uses tool profiles.

    *roots* are user custom watch paths (tool == "custom"); *tool_keys*
    enable built-in profiles — pair those with the ``tool_home`` fixture so
    their ~/.claude etc. paths resolve into the sandbox.
    """

    def __init__(self, tmp_path, roots=None, tool_keys=(), peers=("p1",),
                 data_dir=None):
        roots = list(roots or [])
        self.sent = []          # (peer_id, frame_bytes)
        self.events = []        # aiconfig_file WS events
        self.saved = []         # save_fn calls
        cfg = types.SimpleNamespace(
            device_id="selfid",
            device_name="SelfDev",
            ai_config_tools=list(tool_keys),
            ai_config_custom_paths=list(roots),
            data_dir=str(data_dir) if data_dir else "",
            peers={
                pid: types.SimpleNamespace(device_id=pid, paired=True,
                                           device_name=f"Peer-{pid}")
                for pid in peers
            },
        )
        self.cfg = cfg
        self.mgr = AIConfigManager(
            cfg,
            send_fn=lambda pid, frame: self.sent.append((pid, frame)) or True,
            connected_fn=lambda: list(peers),
            event_fn=self.events.append,
            save_fn=lambda: self.saved.append(1),
        )


@pytest.fixture
def tool_home(tmp_path, monkeypatch):
    """Redirect the built-in profile paths into tmp_path for sandboxed tests.

    expand_root is the manager's one funnel for every profile/custom path, so
    monkeypatching it makes ~/.claude etc. resolve to tmp_path — no real home
    is ever touched when a test enables a built-in tool profile.
    """
    from internal.sync import ai_config as aic
    mapping = {
        "~/.claude/CLAUDE.md": tmp_path / "claude" / "CLAUDE.md",
        "~/.claude/settings.json": tmp_path / "claude" / "settings.json",
        "~/.claude/skills": tmp_path / "claude" / "skills",
        "~/.codex/config.toml": tmp_path / "codex" / "config.toml",
        "~/.cursor/rules": tmp_path / "cursor" / "rules",
        "~/.cursor/commands": tmp_path / "cursor" / "commands",
        "~/.gemini/settings.json": tmp_path / "gemini" / "settings.json",
        "~/.gemini/GEMINI.md": tmp_path / "gemini" / "GEMINI.md",
    }

    def _expand(path_str, home=None):
        key = path_str.strip() if isinstance(path_str, str) else ""
        if key in mapping:
            return mapping[key]
        return Path(key).expanduser()

    monkeypatch.setattr(aic, "expand_root", _expand)
    return tmp_path


def _feed(receiver, payload, peer_id="p1"):
    """Encode a full payload dict and hand it to the receiver's router."""
    raw = encode_frame(payload)
    msg = decode_message(raw)
    receiver.mgr.handle_message(msg.msg_type, msg._raw_payload, peer_id)


def _serving_setup(tmp_path):
    """Server side: one custom root with two known files, collected."""
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "srv-root")])
    _mk(tmp_path / "srv-root", "CLAUDE.md", b"# hello")
    _mk(tmp_path / "srv-root", "notes.md", b"line1\n")
    s.mgr.collect()
    return s


# ------------------------------------------------------ inventory (v2)

def test_inv_roundtrip_stores_by_peer_with_tool(tmp_path):
    a = _StubMgr(tmp_path, roots=[str(tmp_path / "a")])
    _mk(tmp_path / "a", "CLAUDE.md", b"# mine")
    b = _StubMgr(tmp_path, roots=[])
    a.mgr.collect()
    assert a.mgr.send_inventory_to("p1")
    assert len(a.sent) == 1
    _feed(b, decode_message(a.sent[0][1])._raw_payload)
    inv = b.mgr.get_peer_inventories()
    assert set(inv.keys()) == {"p1"}
    assert inv["p1"]["name"] == "SelfDev"
    assert inv["p1"]["legacy"] is False
    entry = inv["p1"]["entries"][0]
    assert entry["tool"] == "custom"
    assert entry["rel_path"] == "CLAUDE.md"
    assert inv["p1"]["fetched_at"] > 0


def test_inv_garbage_entries_sanitized_and_capped(tmp_path):
    b = _StubMgr(tmp_path, roots=[])
    bad_entries = [
        "not-a-dict",
        {"tool": "custom", "rel_path": "../evil", "sha256": "ab" * 8,
         "size": 1, "mtime": 1.0},                     # traversal dropped
        {"tool": True, "rel_path": "bool.md", "sha256": "ab" * 8,
         "size": 1, "mtime": 1.0},                     # bool tool dropped
        {"tool": "custom", "rel_path": "bad-hash.md", "sha256": "zz",
         "size": 1, "mtime": 1.0},
        {"tool": "custom", "rel_path": "neg.md", "sha256": "ab" * 8,
         "size": -5, "mtime": 1.0},
        {"tool": "custom", "rel_path": "good.md", "sha256": "ab" * 8,
         "size": 9, "mtime": 7},                       # the one valid entry
    ]
    _feed(b, {"msg_type": "aiconfig_inv", "v": 2, "device_name": "X",
              "entries": bad_entries})
    entries = b.mgr.get_peer_inventories()["p1"]["entries"]
    assert [e["rel_path"] for e in entries] == ["good.md"]
    assert b.mgr.get_peer_inventories()["p1"]["legacy"] is False
    _feed(b, {"msg_type": "aiconfig_inv", "v": 2, "device_name": "X",
              "entries": [
                  {"tool": "custom", "rel_path": f"f{i}.md",
                   "sha256": "ab" * 8, "size": i, "mtime": i}
                  for i in range(MAX_ENTRIES + 50)] + ["tail-junk"]})
    stored = b.mgr.get_peer_inventories()["p1"]["entries"]
    assert len(stored) == MAX_ENTRIES
    _feed(b, {"msg_type": "aiconfig_inv", "v": 2, "device_name": 5,
              "entries": "nope"})


def test_inv_folder_entries_carried_with_is_dir(tmp_path):
    a = _StubMgr(tmp_path, roots=[str(tmp_path / "a")])
    _mk(tmp_path / "a", "CLAUDE.md", b"# mine")
    _mk(tmp_path / "a", "skills/my-skill/SKILL.md", b"# sk")
    b = _StubMgr(tmp_path, roots=[])
    a.mgr.collect()  # include_dirs=True -> the skill folder is advertised too
    assert a.mgr.send_inventory_to("p1")
    _feed(b, decode_message(a.sent[0][1])._raw_payload)
    inv = b.mgr.get_peer_inventories()["p1"]["entries"]
    by_path = {e["rel_path"]: e for e in inv}
    assert by_path["skills/my-skill/"]["is_dir"] is True
    assert by_path["skills/my-skill/"]["size"] is None
    assert by_path["skills/my-skill/"]["sha256"] == ""
    assert by_path["CLAUDE.md"]["is_dir"] is False
    # an is_dir entry without the trailing-slash folder shape is dropped,
    # while any trailing-slash path is a valid folder entry whatever it looks
    # like (the req handler's is_file() gate keeps folders unpullable).
    _feed(b, {"msg_type": "aiconfig_inv", "v": 2, "device_name": "X",
              "entries": [
                  {"tool": "custom", "rel_path": "dangle", "is_dir": True,
                   "mtime": 1.0},
                  {"tool": "custom", "rel_path": "config.toml/", "is_dir": True,
                   "mtime": 1.0},
              ]})
    inv2 = {e["rel_path"]: e
            for e in b.mgr.get_peer_inventories()["p1"]["entries"]}
    assert "dangle" not in inv2
    assert inv2["config.toml/"]["is_dir"] is True


def test_unpaired_sender_rejected_for_all_three_types(tmp_path):
    b = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    b.cfg.peers["ghost"] = types.SimpleNamespace(
        device_id="ghost", paired=False, device_name="Ghost")
    b.mgr.collect()
    _feed(b, {"msg_type": "aiconfig_inv", "v": 2, "device_name": "G",
              "entries": []}, peer_id="ghost")
    assert b.mgr.get_peer_inventories() == {}
    _feed(b, {"msg_type": "aiconfig_req", "tool": "custom", "rel_path": "x.md"},
          peer_id="ghost")
    _feed(b, {"msg_type": "aiconfig_data", "tool": "custom", "rel_path": "x.md",
              "sha256": "ab" * 8, "b64_content": "aGk="}, peer_id="ghost")
    assert b.sent == [] and b.events == []


# ------------------------------------------------------ req -> serve

def test_req_serves_verified_data(tmp_path):
    s = _serving_setup(tmp_path)
    _feed(s, {"msg_type": "aiconfig_req", "tool": "custom",
              "rel_path": "CLAUDE.md"})
    assert len(s.sent) == 1
    pid, frame = s.sent[0]
    assert pid == "p1"
    payload = decode_message(frame)._raw_payload
    content = base64.b64decode(payload["b64_content"])
    assert content == b"# hello"
    assert payload["tool"] == "custom"
    assert payload["rel_path"] == "CLAUDE.md"
    assert payload["sha256"] == hashlib.sha256(content).hexdigest()[:16]
    assert payload["truncated"] is False


def test_req_hash_drift_and_unadvertised_path_refused(tmp_path):
    s = _serving_setup(tmp_path)
    (tmp_path / "srv-root" / "CLAUDE.md").write_bytes(b"# CHANGED")
    _feed(s, {"msg_type": "aiconfig_req", "tool": "custom",
              "rel_path": "CLAUDE.md"})
    assert s.sent == []  # stale inventory -> refuse until recollect
    # File exists on disk but was never advertised:
    _mk(tmp_path / "srv-root", "secret.env", b"token=1")
    _feed(s, {"msg_type": "aiconfig_req", "tool": "custom",
              "rel_path": "secret.env"})
    assert s.sent == []


def test_req_traversal_attacks_rejected(tmp_path):
    s = _serving_setup(tmp_path)
    _mk(tmp_path, "outside.md", b"top secret")  # outside srv-root
    for evil in ("../outside.md", "..\\outside.md", "..\\..\\outside.md",
                 "sub/../../outside.md",
                 "C:/Windows/win.ini", "/etc/passwd"):
        _feed(s, {"msg_type": "aiconfig_req", "tool": "custom",
                  "rel_path": evil})
        assert s.sent == [], evil
    for bad_tool in (True, 42, "", None):
        _feed(s, {"msg_type": "aiconfig_req", "tool": bad_tool,
                  "rel_path": "CLAUDE.md"})
        assert s.sent == []


def test_req_legacy_form_served_from_root_index(tmp_path):
    # A pre-refactor peer (root_index req) is still served by a v2 host.
    s = _serving_setup(tmp_path)
    _feed(s, {"msg_type": "aiconfig_req", "root_index": 0,
              "rel_path": "CLAUDE.md"})
    assert len(s.sent) == 1
    payload = decode_message(s.sent[0][1])._raw_payload
    assert base64.b64decode(payload["b64_content"]) == b"# hello"
    for bad_idx in (-1, 5, True, None):
        before = len(s.sent)
        _feed(s, {"msg_type": "aiconfig_req", "root_index": bad_idx,
                  "rel_path": "CLAUDE.md"})
        assert len(s.sent) == before  # the bad request produced no reply


def test_req_inventory_refresh_resends_inv(tmp_path):
    s = _serving_setup(tmp_path)
    assert s.mgr.request_inventory("p1")
    # the refresh request itself goes out as a aiconfig_req ...
    req = decode_message(s.sent[0][1])._raw_payload
    assert req == {"msg_type": "aiconfig_req", "inventory_refresh": True}
    # ... and feeding it through the router makes us re-send our inventory
    s.mgr.handle_message("aiconfig_req", req, "p1")
    reply = decode_message(s.sent[1][1])._raw_payload
    assert reply["msg_type"] == "aiconfig_inv"
    assert reply["v"] == 2
    assert len(reply["entries"]) >= 2
    assert not s.mgr.request_inventory("stranger")  # unpaired -> refused


# ------------------------------------------------------------ pull landing

def _pull_and_deliver(receiver, server, rel, mode, tool="custom"):
    """Full async loop: pull -> req -> serve -> land."""
    res = receiver.mgr.pull("p1", [{"tool": tool, "rel_path": rel}], mode=mode)
    assert res["requested"] == 1, res
    req = decode_message(receiver.sent[-1][1])._raw_payload
    assert req["msg_type"] == "aiconfig_req"
    assert req.get("tool") == tool and req["rel_path"] == rel
    server.mgr.handle_message("aiconfig_req", req, "p1")
    reply = decode_message(server.sent[-1][1])._raw_payload
    assert reply["msg_type"] == "aiconfig_data"
    before = len(receiver.sent)
    receiver.mgr.handle_message("aiconfig_data", reply, "p1")
    return len(receiver.sent) > before  # True => a reply frame went back


def test_pull_three_modes_landing(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    s = _serving_setup(tmp_path)

    # overwrite: explicit user choice replaces the local copy exactly
    _mk(recv_roots, "CLAUDE.md", b"# old")
    assert _pull_and_deliver(r, s, "CLAUDE.md", "overwrite") is False
    assert (recv_roots / "CLAUDE.md").read_bytes() == b"# hello"
    assert [(e["status"], e["rel_path"]) for e in r.events] == \
        [("saved", "CLAUDE.md")]

    # copy: original untouched, sibling named .from.<device>.
    _mk(recv_roots, "notes.md", b"local")
    assert _pull_and_deliver(r, s, "notes.md", "copy") is False
    assert (recv_roots / "notes.md").read_bytes() == b"local"
    copied = recv_roots / "notes.from.Peer-p1.md"
    assert copied.read_bytes() == b"line1\n"
    # collision-safe second copy
    _pull_and_deliver(r, s, "notes.md", "copy")
    assert (recv_roots / "notes.from.Peer-p1-2.md").exists()

    # append: lands at the SAME relative path locally, newline-separated,
    # text extensions only (the pulled block follows the local content).
    _mk(recv_roots, "notes.md", b"local notes")  # overwrite the earlier copy
    assert _pull_and_deliver(r, s, "notes.md", "append") is False
    assert (recv_roots / "notes.md").read_bytes() == b"local notes\nline1\n"
    assert r.events[-1]["status"] == "appended"


def test_append_rejects_non_text_extensions(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    srv_root = tmp_path / "bin-root"
    _mk(srv_root, "blob.png", b"\x89PNG")
    s = _StubMgr(tmp_path, roots=[str(srv_root)])
    s.mgr.collect()
    assert _pull_and_deliver(r, s, "blob.png", "append") is False
    assert r.events[-1]["status"] == "error"
    assert r.events[-1]["reason"] == "append_not_text"
    assert list(recv_roots.iterdir()) == []  # nothing landed


def test_pull_unknown_mode_falls_back_to_copy(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    s = _serving_setup(tmp_path)
    _mk(recv_roots, "notes.md", b"local")
    res = r.mgr.pull("p1", [{"tool": "custom", "rel_path": "notes.md"}],
                     mode="rm -rf")  # hostile/unknown mode
    assert res["requested"] == 1
    s.mgr.handle_message("aiconfig_req",
                         decode_message(r.sent[-1][1])._raw_payload, "p1")
    r.mgr.handle_message("aiconfig_data",
                         decode_message(s.sent[-1][1])._raw_payload, "p1")
    # landed as COPY — the original was NOT overwritten
    assert (recv_roots / "notes.md").read_bytes() == b"local"
    assert (recv_roots / "notes.from.Peer-p1.md").read_bytes() == b"line1\n"


def test_pull_requires_paired_connected_peer(tmp_path):
    offline = _StubMgr(tmp_path, roots=[], peers=())
    res = offline.mgr.pull("p1", [{"tool": "custom", "rel_path": "x"}],
                           mode="copy")
    assert res == {"requested": 0, "errors": ["peer_not_paired"]}
    ghost = _StubMgr(tmp_path, roots=[], peers=("g",))
    res2 = ghost.mgr.pull(
        "unpaired-peer", [{"tool": "custom", "rel_path": "x"}], mode="copy")
    assert res2["errors"] == ["peer_not_paired"]


def test_data_hash_mismatch_b64_failure_unsolicited_dropped(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    good = base64.b64encode(b"payload").decode()

    # register one pending pull, then corrupt the hash
    r.mgr.pull("p1", [{"tool": "custom", "rel_path": "f.md"}], mode="overwrite")
    r.mgr.handle_message("aiconfig_data", {
        "tool": "custom", "rel_path": "f.md", "sha256": "00" * 8,
        "b64_content": good}, "p1")
    assert r.events[-1]["status"] == "error"
    assert r.events[-1]["reason"] == "hash_mismatch"
    assert list(recv_roots.iterdir()) == []

    # b64 garbage
    r.mgr.pull("p1", [{"tool": "custom", "rel_path": "f.md"}], mode="overwrite")
    r.mgr.handle_message("aiconfig_data", {
        "tool": "custom", "rel_path": "f.md",
        "sha256": hashlib.sha256(b"payload").hexdigest()[:16],
        "b64_content": "!!!not-b64!!!"}, "p1")
    assert r.events[-1]["reason"] == "b64_decode"

    # unsolicited (no matching pending) silently dropped
    n_events = len(r.events)
    r.mgr.handle_message("aiconfig_data", {
        "tool": "custom", "rel_path": "other.md",
        "sha256": hashlib.sha256(b"payload").hexdigest()[:16],
        "b64_content": good}, "p1")
    assert len(r.events) == n_events
    assert list(recv_roots.iterdir()) == []


def test_pending_expiry_prunes_stale_requests(tmp_path):
    (tmp_path / "rr").mkdir(exist_ok=True)
    r = _StubMgr(tmp_path, roots=[str(tmp_path / "rr")])
    r.mgr.pull("p1", [{"tool": "custom", "rel_path": "slow.md"}], mode="copy")
    key = ("p1", "v2", "custom", "slow.md")
    assert key in r.mgr._pending
    # simulate the clock having moved past the TTL
    old = next(iter(r.mgr._pending.values()))
    old["ts"] = time.time() - 3600
    r.mgr.pull("p1", [{"tool": "custom", "rel_path": "fresh.md"}], mode="copy")
    assert key not in r.mgr._pending


# ------------------------------------------- folder whole-select + batch

def _feed_folder_pull(receiver, server, folder_item, mode="copy",
                      batch_id="b1"):
    """Pull one folder item against a server whose inv the receiver has, and
    deliver every file back.  Returns the pull result."""
    res = receiver.mgr.pull("p1", [folder_item], mode=mode, batch_id=batch_id)
    for pid, frame in list(receiver.sent):
        payload = decode_message(frame)._raw_payload
        if payload.get("msg_type") != "aiconfig_req":
            continue
        server.mgr.handle_message("aiconfig_req", payload, "p1")
        reply = decode_message(server.sent[-1][1])._raw_payload
        receiver.mgr.handle_message("aiconfig_data", reply, "p1")
    return res


def test_folder_pull_expands_and_lands_every_descendant(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "srv-root")])
    _mk(tmp_path / "srv-root", "skills/my-skill/SKILL.md", b"# sk")
    _mk(tmp_path / "srv-root", "skills/my-skill/sub/a.md", b"a")
    _mk(tmp_path / "srv-root", "skills/other/b.md", b"b")
    s.mgr.collect()
    # the receiving side must know the peer's inventory to expand the folder
    assert s.mgr.send_inventory_to("p1")
    _feed(r, decode_message(s.sent[-1][1])._raw_payload)

    res = _feed_folder_pull(
        r, s, {"tool": "custom", "rel_path": "skills/my-skill/",
               "is_dir": True}, batch_id="mig1")
    assert res["requested"] == 2, res
    assert res["expanded"] == 2  # folder itself dropped, two files requested
    reqs = [decode_message(f)._raw_payload for _, f in r.sent
            if decode_message(f)._raw_payload.get("msg_type") == "aiconfig_req"]
    assert sorted(q["rel_path"] for q in reqs) == [
        "skills/my-skill/SKILL.md", "skills/my-skill/sub/a.md"]

    # every file landed under the folder's relative structure (copy mode →
    # .from.<device> siblings alongside the originals)
    assert (recv_roots / "skills" / "my-skill" /
            "SKILL.from.Peer-p1.md").read_bytes() == b"# sk"
    assert (recv_roots / "skills" / "my-skill" / "sub" /
            "a.from.Peer-p1.md").read_bytes() == b"a"
    # the sibling folder was NOT pulled
    assert not (recv_roots / "skills" / "other" / "b.from.Peer-p1.md").exists()
    # the folder entry itself is not a file on disk
    assert (recv_roots / "skills" / "my-skill").is_dir()

    # per-file WS events echo the batch_id
    landed = [e for e in r.events if e.get("batch_id") == "mig1"]
    assert sorted(e["rel_path"] for e in landed) == [
        "skills/my-skill/SKILL.md", "skills/my-skill/sub/a.md"]
    assert all(e["status"] == "copied" for e in landed)
    assert all(e["tool"] == "custom" for e in landed)


def test_folder_pull_unknown_folder_passes_through_for_clean_refusal(tmp_path):
    r = _StubMgr(tmp_path, roots=[str(tmp_path / "recv-root")])
    (tmp_path / "recv-root").mkdir(exist_ok=True)
    s = _serving_setup(tmp_path)
    assert s.mgr.send_inventory_to("p1")
    _feed(r, decode_message(s.sent[-1][1])._raw_payload)
    res = r.mgr.pull("p1", [
        {"tool": "custom", "rel_path": "no-such-folder/", "is_dir": True},
    ], mode="copy", batch_id="x")
    # nothing matched the folder's prefix -> the folder item itself passes
    # through as a single request (requested == 1); the peer refuses it as
    # unadvertised, so no data comes back and nothing lands.
    assert res["requested"] == 1
    assert res["errors"] == []
    assert res["expanded"] == 1
    req = decode_message(r.sent[-1][1])._raw_payload
    assert req["rel_path"] == "no-such-folder/"
    before = len(s.sent)
    s.mgr.handle_message("aiconfig_req", req, "p1")
    assert len(s.sent) == before  # server refused — no aiconfig_data reply
    assert r.events == []         # and the receiver never saw a landing result


def test_pull_tool_profile_lands_at_profile_root(tool_home):
    """The flagship tool-profile flow: a claude_code entry crosses the wire as
    (tool, rel_path), and the receiver resolves the landing target through ITS
    OWN claude_code profile — no root index involved."""
    srv = _StubMgr(tool_home, tool_keys=["claude_code"], roots=[])
    _mk(tool_home / "claude", "CLAUDE.md", b"# hello")
    srv.mgr.collect()
    entry = srv.mgr._local_entries[0]
    assert entry["tool"] == "claude_code"
    assert entry["path"] == "CLAUDE.md"

    recv = _StubMgr(tool_home, tool_keys=["claude_code"], roots=[])
    res = recv.mgr.pull("p1", [{"tool": "claude_code",
                                "rel_path": "CLAUDE.md"}], mode="copy")
    assert res["requested"] == 1
    req = decode_message(recv.sent[-1][1])._raw_payload
    assert req["tool"] == "claude_code" and "root_index" not in req
    srv.mgr.handle_message("aiconfig_req", req, "p1")
    reply = decode_message(srv.sent[-1][1])._raw_payload
    assert reply["tool"] == "claude_code"
    recv.mgr.handle_message("aiconfig_data", reply, "p1")
    # landed as a copy sibling INSIDE the receiver's own ~/.claude profile root
    landed = tool_home / "claude" / "CLAUDE.from.Peer-p1.md"
    assert landed.read_bytes() == b"# hello"
    assert (tool_home / "claude" / "CLAUDE.md").read_bytes() == b"# hello"


def test_top_level_file_wins_over_sibling_dir_root(tool_home):
    """The flagship pull must not be 'ambiguous_tool_root' just because the
    claude_code profile ALSO enables the skills dir: rel_path 'CLAUDE.md' can
    only have come from the peer's CLAUDE.md file root (dir roots advertise
    paths relative to themselves), so the receiver's own file root wins."""
    srv = _StubMgr(tool_home, tool_keys=["claude_code"], roots=[])
    _mk(tool_home / "claude", "CLAUDE.md", b"# hello")
    srv.mgr.collect()
    recv = _StubMgr(tool_home, tool_keys=["claude_code"], roots=[])
    res = recv.mgr.pull("p1", [{"tool": "claude_code",
                                "rel_path": "CLAUDE.md"}], mode="copy")
    assert res["requested"] == 1
    req = decode_message(recv.sent[-1][1])._raw_payload
    srv.mgr.handle_message("aiconfig_req", req, "p1")
    reply = decode_message(srv.sent[-1][1])._raw_payload
    recv.mgr.handle_message("aiconfig_data", reply, "p1")
    # landed beside ~/.claude/CLAUDE.md — NOT inside the skills dir
    assert (tool_home / "claude" / "CLAUDE.from.Peer-p1.md").read_bytes() \
        == b"# hello"
    assert not (tool_home / "claude" / "skills" / "CLAUDE.md").exists()


def test_folder_pull_via_tool_profile(tool_home):
    """Folder whole-select against a built-in profile root: recursive, per-file
    hashes, progress events — a real 'sync the skills folder' flow.  The peer's
    rel_paths are relative to ITS skills dir root (my-skill/…), and the landing
    side resolves them back through ITS OWN claude_code skills root."""
    srv = _StubMgr(tool_home, tool_keys=["claude_code"], roots=[])
    _mk(tool_home / "claude" / "skills", "my-skill/SKILL.md", b"# sk")
    _mk(tool_home / "claude" / "skills", "my-skill/sub/a.md", b"a")
    _mk(tool_home / "claude" / "skills", "other/b.md", b"b")
    srv.mgr.collect()

    recv = _StubMgr(tool_home, tool_keys=["claude_code"], roots=[])
    _feed(recv, srv.mgr.build_inv_payload())
    res = _feed_folder_pull(
        recv, srv,
        {"tool": "claude_code", "rel_path": "my-skill/", "is_dir": True},
        batch_id="sk1")
    assert res["requested"] == 2, res
    reqs = [decode_message(f)._raw_payload for _, f in recv.sent
            if decode_message(f)._raw_payload.get("msg_type") == "aiconfig_req"]
    assert sorted(q["rel_path"] for q in reqs) == [
        "my-skill/SKILL.md", "my-skill/sub/a.md"]
    # both files landed recursively under the profile skills dir (copy mode,
    # siblings of the server's own copies since both sides share the sandbox)
    assert (tool_home / "claude" / "skills" / "my-skill" /
            "SKILL.from.Peer-p1.md").read_bytes() == b"# sk"
    assert (tool_home / "claude" / "skills" / "my-skill" / "sub" /
            "a.from.Peer-p1.md").read_bytes() == b"a"
    assert not (tool_home / "claude" / "skills" / "other" /
                "b.from.Peer-p1.md").exists()


# ---------------------------------------------------------------- preview

def test_preview_returns_content_without_touching_disk(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    s = _serving_setup(tmp_path)
    result_box = {}

    def run():
        result_box["res"] = r.mgr.preview("p1", "custom", "CLAUDE.md")

    t = threading.Thread(target=run)
    t.start()
    deadline = time.time() + 2.0
    while not r.sent and time.time() < deadline:
        time.sleep(0.01)  # wait for the preview's aiconfig_req to be sent
    assert r.sent, "preview never sent its request"
    req = decode_message(r.sent[-1][1])._raw_payload
    assert req["msg_type"] == "aiconfig_req"
    assert req["tool"] == "custom"
    s.mgr.handle_message("aiconfig_req", req, "p1")
    reply = decode_message(s.sent[-1][1])._raw_payload
    r.mgr.handle_message("aiconfig_data", reply, "p1")
    t.join(2.0)
    assert not t.is_alive()
    res = result_box["res"]
    assert res["ok"] is True
    assert res["content"] == "# hello"
    assert res["truncated"] is False
    assert list(recv_roots.iterdir()) == []  # NOTHING landed on disk


def test_preview_times_out_cleanly(tmp_path):
    r = _StubMgr(tmp_path, roots=[], peers=("p1",))
    start = time.monotonic()
    res = r.mgr.preview("p1", "custom", "x.md", timeout=0.05)
    assert res == {"ok": False, "error": "timeout"}
    assert time.monotonic() - start < 1.0
    assert r.mgr._pending == {}


def test_preview_v2_rejected_on_legacy_peer(tmp_path):
    r = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _feed(r, {"msg_type": "aiconfig_inv", "device_name": "Old",
              "entries": [{"root_index": 0, "path": "CLAUDE.md",
                           "sha256": "ab" * 8, "size": 1, "mtime": 1.0}]})
    res = r.mgr.preview("p1", "custom", "CLAUDE.md")
    assert res == {"ok": False, "error": "legacy_peer"}


# ---------------------------------------------- legacy (read-only) compat

def test_legacy_inv_cached_read_only(tmp_path):
    b = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _feed(b, {"msg_type": "aiconfig_inv", "device_name": "Old",
              "entries": [{"root_index": 0, "path": "CLAUDE.md",
                           "sha256": "ab" * 8, "size": 1, "mtime": 1.0}]})
    inv = b.mgr.get_peer_inventories()["p1"]
    assert inv["legacy"] is True
    assert inv["name"] == "Old"
    assert inv["entries"][0]["root_index"] == 0
    assert inv["entries"][0]["path"] == "CLAUDE.md"
    # pull is blocked: root indices cannot resolve to THIS device's profiles
    res = b.mgr.pull("p1", [{"tool": "custom", "rel_path": "CLAUDE.md"}],
                     mode="copy")
    assert res == {"requested": 0, "errors": ["legacy_peer_read_only"]}


def test_legacy_preview_still_works(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    s = _serving_setup(tmp_path)
    legacy_inv = {"msg_type": "aiconfig_inv", "device_name": "Old",
                  "entries": [{"root_index": 0, "path": "CLAUDE.md",
                               "sha256": "ab" * 8, "size": 1, "mtime": 1.0}]}
    _feed(r, legacy_inv)
    # the v2 HOST also saw the legacy peer's inv, so it replies legacy-shaped
    _feed(s, legacy_inv, peer_id="p1")
    result_box = {}

    def run():
        result_box["res"] = r.mgr.preview_legacy("p1", 0, "CLAUDE.md")

    t = threading.Thread(target=run)
    t.start()
    deadline = time.time() + 2.0
    while not r.sent and time.time() < deadline:
        time.sleep(0.01)
    assert r.sent, "legacy preview never sent its request"
    req = decode_message(r.sent[-1][1])._raw_payload
    assert req["msg_type"] == "aiconfig_req" and "root_index" in req
    s.mgr.handle_message("aiconfig_req", req, "p1")
    # the v2 host answers the legacy request with a legacy-shaped reply
    reply = decode_message(s.sent[-1][1])._raw_payload
    assert "root_index" in reply and reply["root_index"] == 0
    r.mgr.handle_message("aiconfig_data", reply, "p1")
    t.join(2.0)
    assert not t.is_alive()
    assert result_box["res"]["content"] == "# hello"
    assert list(recv_roots.iterdir()) == []


# ------------------------------------------------------------- watch list

def test_set_profiles_normalizes_and_persists(tmp_path):
    s = _StubMgr(tmp_path, roots=["~/old"])
    res = s.mgr.set_profiles(["claude_code", "bogus", "claude_code", 42],
                             ["  ~/configs ", "", "~/configs", "D:\\notes"])
    assert res["ok"] is True
    assert res["tools"] == ["claude_code"]
    assert res["custom_paths"] == ["~/configs", "D:\\notes"]
    assert s.cfg.ai_config_tools == ["claude_code"]
    assert s.cfg.ai_config_custom_paths == ["~/configs", "D:\\notes"]
    assert s.saved == [1]


# ------------------------------------------------------------- REST layer

def test_api_routes_with_bound_manager(tmp_path):
    from internal.web.api import aiconfig as api
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md")
    api.bind(s.mgr)
    try:
        data, status = api.handle("GET", "/api/aiconfig/inventory", {}, b"")
        assert status == 200 and data["peers"] == {}
        data, status = api.handle("GET", "/api/aiconfig/inventory",
                                  {"refresh": ["1"], "peer_id": ["p1"]}, b"")
        assert status == 200 and data["refreshed"] == ["p1"]
        data, status = api.handle("GET", "/api/aiconfig/inventory",
                                  {"refresh": ["1"]}, b"")
        assert status == 200 and data["refreshed"] == []  # no cached peers yet

        data, status = api.handle("POST", "/api/aiconfig/pull", {},
                                  json.dumps({"peer_id": "p1", "mode": "copy",
                                              "batch_id": "b1",
                                              "items": [
                                                  {"tool": "custom",
                                                   "rel_path": "CLAUDE.md"},
                                                  "junk"]}).encode())
        assert status == 200 and data["requested"] == 1
        assert data["expanded"] == 2

        data, status = api.handle("POST", "/api/aiconfig/pull", {},
                                  json.dumps({"items": []}).encode())
        assert status == 400  # missing peer_id
        data, status = api.handle("GET", "/api/aiconfig/nonsense", {}, b"")
        assert status == 404
    finally:
        api.bind(None)


def test_api_profiles_get_and_post(tmp_path, tool_home):
    from internal.web.api import aiconfig as api
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    api.bind(s.mgr)
    try:
        data, status = api.handle("GET", "/api/aiconfig/profiles", {}, b"")
        assert status == 200
        assert data["ok"] is True
        assert data["tools"] == ai_profiles.TOOLS   # single source of truth
        assert data["enabled"] == []                # no built-ins enabled yet
        assert data["custom_paths"] == [str(tmp_path / "r")]

        # POST normalizes + persists + recollects + rebroadcasts
        data, status = api.handle(
            "POST", "/api/aiconfig/profiles", {},
            json.dumps({"tools": ["claude_code", "bogus", "claude_code"],
                        "custom_paths": ["~/x", "", "~/x", "~/y"]}).encode())
        assert status == 200
        assert data["tools"] == ["claude_code"]
        assert data["custom_paths"] == ["~/x", "~/y"]
        assert "broadcast_to" in data
        assert s.cfg.ai_config_tools == ["claude_code"]
        assert s.cfg.ai_config_custom_paths == ["~/x", "~/y"]
        assert s.saved == [1]
    finally:
        api.bind(None)


def test_api_routes_unbound_returns_503(tmp_path):
    from internal.web.api import aiconfig as api
    api.bind(None)
    try:
        data, status = api.handle("GET", "/api/aiconfig/inventory", {}, b"")
        assert status == 503
        data, status = api.handle("POST", "/api/aiconfig/pull", {}, b"{}")
        assert status == 503
    finally:
        api.bind(None)


# ------------------------------------------------------------- local manager

def test_local_listing_shape_and_reuses_collector(tmp_path):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules\n")
    _mk(tmp_path / "r", "sub/notes.md", b"hi")
    listing = s.mgr.local_listing()
    assert set(listing) == {"collected_at", "tools", "custom_paths", "roots",
                            "entries"}
    assert listing["collected_at"] > 0
    assert listing["custom_paths"] == [str(tmp_path / "r")]
    assert listing["tools"] == []          # no built-in profiles enabled
    # The local file manager shows the folder tree, so "sub/" appears as a
    # folder entry alongside its file.
    assert listing["roots"] == [
        {"root_index": 0, "tool": "custom", "kind": "dir",
         "path": str(tmp_path / "r"), "count": 3}]
    assert len(listing["entries"]) == 3
    by_rel = {e["rel_path"]: e for e in listing["entries"]}
    assert set(by_rel) == {"CLAUDE.md", "sub/notes.md", "sub/"}
    for e in listing["entries"]:
        assert e["tool"] == "custom"
        assert set(e) == {"tool", "rel_path", "size", "mtime", "sha256",
                          "is_dir"}
    # folder entry: no size / empty content hash, flagged is_dir
    assert by_rel["sub/"]["is_dir"] is True
    assert by_rel["sub/"]["size"] is None and by_rel["sub/"]["sha256"] == ""
    assert by_rel["CLAUDE.md"]["is_dir"] is False
    assert by_rel["CLAUDE.md"]["size"] > 0 and by_rel["CLAUDE.md"]["mtime"] > 0
    # entries carry the collector's sha256 prefix, not a copy
    assert by_rel["CLAUDE.md"]["sha256"] == \
        hashlib.sha256(b"# rules\n").hexdigest()[:16]


def test_local_listing_empty_when_no_paths(tmp_path):
    s = _StubMgr(tmp_path, roots=[])
    listing = s.mgr.local_listing()
    assert listing["roots"] == [] and listing["entries"] == []
    assert listing["collected_at"] > 0


def test_local_listing_enabled_profiles_group_their_tools(tmp_path, tool_home):
    s = _StubMgr(tmp_path, tool_keys=["claude_code"], roots=[])
    _mk(tool_home / "claude", "CLAUDE.md", b"# rules\n")
    listing = s.mgr.local_listing()
    # tools = the enabled profile cards (single source of truth from ai_profiles)
    assert [t["key"] for t in listing["tools"]] == ["claude_code"]
    assert listing["tools"][0]["label"]
    # roots = every entry of the enabled profile, each with its own file count
    assert [r["tool"] for r in listing["roots"]] == ["claude_code"] * 3
    assert [r["kind"] for r in listing["roots"]] == ["file", "file", "dir"]
    assert [r["path"] for r in listing["roots"]] == [
        "~/.claude/CLAUDE.md", "~/.claude/settings.json", "~/.claude/skills"]
    assert [r["count"] for r in listing["roots"]] == [1, 0, 0]
    # only the existing file shows up as an entry, tagged with its tool
    assert [e["rel_path"] for e in listing["entries"]] == ["CLAUDE.md"]
    assert listing["entries"][0]["tool"] == "claude_code"


def test_local_item_reads_text_and_truncates(tmp_path):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "small.md", b"# hello")
    _mk(tmp_path / "r", "big.md", b"x" * 70000)
    res = s.mgr.local_read("custom", "small.md")
    assert res == {"ok": True, "content": "# hello", "truncated": False}
    res2 = s.mgr.local_read("custom", "big.md")
    assert res2["ok"] is True and res2["truncated"] is True
    assert len(res2["content"].encode("utf-8")) <= 64 * 1024


def test_local_item_rejects_binary_missing_and_traversal(tmp_path):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "blob.png", b"\x89PNG\r\n\x1a\n\x00data")
    assert s.mgr.local_read("custom", "blob.png")["error"] == "binary"
    assert s.mgr.local_read("custom", "missing.md")["error"] == "not_found"
    # traversal/absolute paths cannot resolve to a profile root at all
    assert s.mgr.local_read("custom", "../escape.md")["error"] == "no_local_root"
    assert s.mgr.local_read("custom", "C:/win.ini")["error"] == "no_local_root"
    assert s.mgr.local_read("nope_tool", "small.md")["error"] == "no_local_root"


def test_local_save_writes_back_and_auto_backup(tmp_path):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "CLAUDE.md", b"# old\n")
    res = s.mgr.local_save("custom", "CLAUDE.md", "# new content\n")
    assert res["ok"] is True
    assert (tmp_path / "r" / "CLAUDE.md").read_text(encoding="utf-8") \
        == "# new content\n"
    # pre-save original preserved as <rel_path>.bak in the same directory
    assert (tmp_path / "r" / "CLAUDE.md.bak").read_text(encoding="utf-8") \
        == "# old\n"
    # a second save overwrites the .bak with the newest pre-save content
    res2 = s.mgr.local_save("custom", "CLAUDE.md", "# v3\n")
    assert res2["ok"] is True
    assert (tmp_path / "r" / "CLAUDE.md.bak").read_text(encoding="utf-8") \
        == "# new content\n"
    # .bak shows up in the local listing = "edited here" marker
    rels = {e["rel_path"] for e in s.mgr.local_listing()["entries"]}
    assert "CLAUDE.md.bak" in rels


def test_local_save_creates_missing_file_in_subdir(tmp_path):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    res = s.mgr.local_save("custom", "skills/deep/SKILL.md", "# new skill\n")
    assert res["ok"] is True
    p = tmp_path / "r" / "skills" / "deep" / "SKILL.md"
    assert p.read_text(encoding="utf-8") == "# new skill\n"
    assert not (tmp_path / "r" / "skills" / "deep" / "SKILL.md.bak").exists()


def test_local_save_rejects_binary_traversal_oversize_and_missing(tmp_path):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "f.md", b"x")
    assert s.mgr.local_save("custom", "f.md", "a\x00b")["error"] == "binary"
    assert s.mgr.local_save("custom", "../evil.md", "text")[
        "error"] == "no_local_root"
    assert s.mgr.local_save("custom", "C:/evil.md", "text")[
        "error"] == "no_local_root"
    assert s.mgr.local_save("custom", "f.md",
                            "y" * (LOCAL_SAVE_MAX_BYTES + 1))[
        "error"] == "too_large"
    assert s.mgr.local_save("custom", "f.md", None)[
        "error"] == "content_required"
    assert s.mgr.local_save("custom", "f.md", b"not-str")[
        "error"] == "content_required"
    # nothing was touched by any of the failed saves
    assert (tmp_path / "r" / "f.md").read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "r" / "f.md.bak").exists()


def test_local_trash_moves_into_recoverable_trash(tmp_path):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "sub/notes.md", b"secret")
    res = s.mgr.local_trash("custom", "sub/notes.md")
    assert res["ok"] is True
    trashed = Path(res["trashed_to"])
    assert trashed.exists() and trashed.is_file()
    assert trashed.read_text(encoding="utf-8") == "secret"
    # original is gone, but a recoverable copy remains under data_dir/aiconfig_trash
    assert not (tmp_path / "r" / "sub" / "notes.md").exists()
    assert tmp_path / "aiconfig_trash" in trashed.parents
    # original path naming preserved: sub/notes.md -> aiconfig_trash/sub/...
    assert trashed.parent == tmp_path / "aiconfig_trash" / "sub"


def test_local_trash_folder_moves_whole_directory(tmp_path):
    """Trashing a folder (not just a file) moves the whole directory tree into
    the recoverable trash, preserving its contents and structure."""
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "skills/my-skill/SKILL.md", b"skill")
    _mk(tmp_path / "r", "skills/my-skill/sub/extra.md", b"extra")
    res = s.mgr.local_trash("custom", "skills/my-skill/")
    assert res["ok"] is True
    trashed = Path(res["trashed_to"])
    assert trashed.exists() and trashed.is_dir()
    assert (trashed / "SKILL.md").read_text(encoding="utf-8") == "skill"
    assert (trashed / "sub" / "extra.md").read_text(encoding="utf-8") == "extra"
    # The whole folder is gone from the original root; the recoverable copy
    # keeps the relative path structure under data_dir/aiconfig_trash.
    assert not (tmp_path / "r" / "skills" / "my-skill").exists()
    assert tmp_path / "aiconfig_trash" in trashed.parents


def test_local_trash_renames_on_collision_and_rejects_bad(tmp_path):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "a.md", b"one")
    r1 = s.mgr.local_trash("custom", "a.md")
    assert r1["ok"] is True
    _mk(tmp_path / "r", "a.md", b"two")
    r2 = s.mgr.local_trash("custom", "a.md")
    assert r2["ok"] is True
    assert Path(r2["trashed_to"]) != Path(r1["trashed_to"])
    assert Path(r2["trashed_to"]).read_text(encoding="utf-8") == "two"
    assert Path(r1["trashed_to"]).read_text(encoding="utf-8") == "one"
    # bad inputs
    assert s.mgr.local_trash("custom", "../evil.md")["error"] == "no_local_root"
    assert s.mgr.local_trash("custom", "missing.md")["error"] == "not_found"


def test_local_trash_suffix_increments_when_destination_exists(tmp_path,
                                                               monkeypatch):
    from internal.sync import ai_config as aic
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "f.md", b"live")
    monkeypatch.setattr(aic.time, "strftime", lambda fmt: "20260101_000000")
    dest = tmp_path / "aiconfig_trash" / "20260101_000000_f.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("occupied")
    res = s.mgr.local_trash("custom", "f.md")
    assert res["ok"] is True
    assert Path(res["trashed_to"]).name == "20260101_000000_f-2.md"
    assert Path(res["trashed_to"]).read_text(encoding="utf-8") == "live"


def test_open_windows_uses_startfile(tmp_path, monkeypatch):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules")
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p),
                        raising=False)
    res = s.mgr.local_open("custom", "CLAUDE.md")
    assert res["ok"] is True
    assert calls == [str(tmp_path / "r" / "CLAUDE.md")]


def test_open_posix_branches_use_open_and_xdg_open(tmp_path, monkeypatch):
    import subprocess
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules")
    for plat, argv0 in (("darwin", "open"), ("linux", "xdg-open")):
        seen = []

        def fake_run(cmd, check=True):
            seen.append(cmd)

        monkeypatch.setattr(sys, "platform", plat)
        monkeypatch.setattr(subprocess, "run", fake_run)
        res = s.mgr.local_open("custom", "CLAUDE.md")
        assert res["ok"] is True, plat
        assert seen == [[argv0, str(tmp_path / "r" / "CLAUDE.md")]], plat


def test_open_direct_helper_and_failure(tmp_path, monkeypatch):
    target = str(tmp_path / "CLAUDE.md")
    _mk(tmp_path, "CLAUDE.md", b"# rules")
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p),
                        raising=False)
    assert open_with_default_app(target) is True
    assert calls == [target]
    # a raising startfile -> helper reports failure
    def boom(_p):
        raise OSError("no default app")

    monkeypatch.setattr(os, "startfile", boom, raising=False)
    assert open_with_default_app(target) is False


# ------------------------------------------------------- local REST layer

def test_local_routes_via_rest(tmp_path):
    from internal.web.api import aiconfig as api
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules\n")
    api.bind(s.mgr)
    try:
        # GET /api/aiconfig/local
        data, status = api.handle("GET", "/api/aiconfig/local", {}, b"")
        assert status == 200
        assert set(data) == {"collected_at", "tools", "custom_paths", "roots",
                             "entries"}
        assert len(data["entries"]) == 1

        # GET /api/aiconfig/local/item
        data, status = api.handle(
            "GET", "/api/aiconfig/local/item",
            {"tool": ["custom"], "rel_path": ["CLAUDE.md"]}, b"")
        assert status == 200 and data["content"] == "# rules\n"
        data, status = api.handle(
            "GET", "/api/aiconfig/local/item",
            {"tool": ["custom"], "rel_path": ["nope.md"]}, b"")
        assert status == 404 and data["error"] == "not_found"
        data, status = api.handle(
            "GET", "/api/aiconfig/local/item",
            {"tool": ["custom"], "rel_path": ["../escape.md"]}, b"")
        assert status == 400 and data["error"] == "no_local_root"

        # POST /api/aiconfig/local/save
        data, status = api.handle(
            "POST", "/api/aiconfig/local/save", {},
            json.dumps({"tool": "custom", "rel_path": "CLAUDE.md",
                        "content": "# saved\n"}).encode())
        assert status == 200 and data["ok"] is True
        assert (tmp_path / "r" / "CLAUDE.md").read_text(encoding="utf-8") \
            == "# saved\n"

        # POST /api/aiconfig/local/trash
        data, status = api.handle(
            "POST", "/api/aiconfig/local/trash", {},
            json.dumps({"tool": "custom", "rel_path": "CLAUDE.md"}).encode())
        assert status == 200 and data["ok"] is True
        assert Path(data["trashed_to"]).exists()
        assert not (tmp_path / "r" / "CLAUDE.md").exists()

        # unbound -> 503
        api.bind(None)
        data, status = api.handle("POST", "/api/aiconfig/local/save", {},
                                  b"{}")
        assert status == 503
    finally:
        api.bind(None)


def test_open_route_via_rest(tmp_path, monkeypatch):
    from internal.web.api import aiconfig as api
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules")
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p),
                        raising=False)
    api.bind(s.mgr)
    try:
        data, status = api.handle(
            "POST", "/api/aiconfig/open", {},
            json.dumps({"tool": "custom", "rel_path": "CLAUDE.md"}).encode())
        assert status == 200 and data["ok"] is True
        assert calls == [str(tmp_path / "r" / "CLAUDE.md")]
        data, status = api.handle(
            "POST", "/api/aiconfig/open", {},
            json.dumps({"tool": "custom", "rel_path": "missing.md"}).encode())
        assert status == 400 and data["error"] == "not_found"
        data, status = api.handle(
            "POST", "/api/aiconfig/open", {},
            json.dumps({"tool": "custom", "rel_path": "../x.md"}).encode())
        assert status == 400 and data["error"] == "no_local_root"
    finally:
        api.bind(None)


# ══════════════════════════════════════════════════
# merged from test_round19_presets.py — single-file custom roots
# ══════════════════════════════════════════════════

def test_file_root_advertises_itself_as_one_entry(tmp_path):
    f = _mk(tmp_path, "CLAUDE.md", b"# rules\n")
    entries = collect_roots([("custom", "file", str(f))])
    assert len(entries) == 1
    assert entries[0]["path"] == "CLAUDE.md"
    assert entries[0]["tool"] == "custom"
    assert entries[0]["root_index"] == 0
    assert entries[0]["sha256"]


def test_file_root_read_returns_the_file_itself(tmp_path):
    f = _mk(tmp_path, "settings.json", b'{"k": 1}')
    stub = _StubMgr(tmp_path, roots=[str(f)])
    res = stub.mgr.local_read("custom", "settings.json")
    assert res["ok"] and res["content"] == '{"k": 1}'
    # a rel that isn't the file's own basename is refused
    bad = stub.mgr.local_read("custom", "other.json")
    assert bad["ok"] is False and bad["error"] == "no_local_root"


def test_file_root_save_writes_and_leaves_bak(tmp_path):
    f = _mk(tmp_path, "CLAUDE.md", b"old\n")
    stub = _StubMgr(tmp_path, roots=[str(f)], data_dir=tmp_path)
    res = stub.mgr.local_save("custom", "CLAUDE.md", "new\n")
    assert res["ok"] is True
    assert f.read_text() == "new\n"
    assert (tmp_path / "CLAUDE.md.bak").read_text() == "old\n"


def test_file_root_trash_moves_the_file(tmp_path):
    f = _mk(tmp_path, "GEMINI.md", b"ctx\n")
    stub = _StubMgr(tmp_path, roots=[str(f)], data_dir=str(tmp_path / "data"))
    res = stub.mgr.local_trash("custom", "GEMINI.md")
    assert res["ok"] is True
    assert not f.exists()
    assert "aiconfig_trash" in res["trashed_to"]


def test_directory_root_still_recurses(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    _mk(d, "a.mdc", b"a")
    _mk(d, "b.mdc", b"bb")
    entries = collect_roots([("custom", "dir", str(d))])
    assert {e["path"] for e in entries} == {"a.mdc", "b.mdc"}


def test_collector_skips_missing_file_root_silently(tmp_path):
    entries = collect_roots([("custom", "file", str(tmp_path / "nope.json"))])
    assert entries == []


def test_file_root_serves_pull_to_peer(tmp_path):
    """A single-file custom root must be servable on the peer path too: the
    aiconfig_req handler targets the file itself (no directory walk), so a pull
    lands its content on the receiver."""
    srv = _StubMgr(tmp_path, roots=[str(tmp_path / "CLAUDE.md")])
    _mk(tmp_path, "CLAUDE.md", b"# hello\n")
    srv.mgr.collect()
    assert [e["path"] for e in srv.mgr._local_entries] == ["CLAUDE.md"]

    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    _mk(recv_roots, "CLAUDE.md", b"# old")  # landing needs an existing target
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])

    # pull: full async req -> serve -> land loop against the file-root server
    assert _pull_and_deliver(r, srv, "CLAUDE.md", "overwrite") is False
    assert (recv_roots / "CLAUDE.md").read_bytes() == b"# hello\n"


def test_file_root_preview_returns_content_without_landing(tmp_path):
    """Preview of a single-file root returns the file's content and never
    lands anything on the receiver's disk."""
    srv = _StubMgr(tmp_path, roots=[str(tmp_path / "CLAUDE.md")])
    _mk(tmp_path, "CLAUDE.md", b"# hello\n")
    srv.mgr.collect()

    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    result_box = {}

    def run():
        result_box["res"] = r.mgr.preview("p1", "custom", "CLAUDE.md")

    t = threading.Thread(target=run)
    t.start()
    deadline = time.time() + 2.0
    while not r.sent and time.time() < deadline:
        time.sleep(0.01)  # wait for the preview's aiconfig_req to be sent
    assert r.sent, "preview never sent its request"
    req = decode_message(r.sent[-1][1])._raw_payload
    assert req["msg_type"] == "aiconfig_req"
    srv.mgr.handle_message("aiconfig_req", req, "p1")
    reply = decode_message(srv.sent[-1][1])._raw_payload
    r.mgr.handle_message("aiconfig_data", reply, "p1")
    t.join(2.0)
    assert not t.is_alive()
    res = result_box["res"]
    assert res["ok"] is True
    assert res["content"] == "# hello\n"
    assert res["truncated"] is False
    assert list(recv_roots.iterdir()) == []


# ══════════════════════════════════════════════════
# split from test_round13_wrapup.py — settings whitelist ai_config_*
# ══════════════════════════════════════════════════

def test_settings_whitelist_uses_tool_profile_fields():
    from internal.web.api import settings as settings_api
    assert "ai_config_tools" in settings_api._SAFE_FIELDS
    assert "ai_config_tools" in settings_api._MUTABLE_FIELDS
    assert "ai_config_custom_paths" in settings_api._SAFE_FIELDS
    assert "ai_config_custom_paths" in settings_api._MUTABLE_FIELDS
    # the raw watch-list field is gone from the whitelist
    assert "ai_config_paths" not in settings_api._SAFE_FIELDS


class _SettingsCfg:
    """Minimal config stand-in carrying the fields the handlers touch."""

    def __init__(self):
        self.private_key_pem = "KEY"
        self.internet_sync_enabled = False
        self.relay_brokers = []
        self.relay_secret = ""
        self.peer_relay_secrets = {}
        self.ai_config_tools = []
        self.ai_config_custom_paths = []


@pytest.fixture()
def sandboxed_persist(monkeypatch, tmp_path):
    """Redirect config persistence into the test sandbox (no real disk writes)."""
    from internal.web.api import settings as settings_api
    monkeypatch.setattr(
        settings_api, "_config_path", lambda: tmp_path / "config.json",
    )
    monkeypatch.setattr(
        settings_api, "save_config", lambda cfg, enc_mgr=None: None,
    )


def _body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


@pytest.mark.usefixtures("sandboxed_persist")
def test_ai_config_profiles_writable_via_settings_post():
    from internal.web.api.settings import update_settings
    cfg = _SettingsCfg()
    tools = ["claude_code", "codex"]
    custom = ["~/ai-configs"]
    data, status = update_settings(
        _body({"ai_config_tools": tools, "ai_config_custom_paths": custom}),
        cfg)
    assert status == 200
    assert data["updated"]["ai_config_tools"] == tools
    assert data["updated"]["ai_config_custom_paths"] == custom
    assert cfg.ai_config_tools == tools
    assert cfg.ai_config_custom_paths == custom

    # A non-list value is rejected without touching the field.
    data, status = update_settings(_body({"ai_config_tools": "~/x"}), cfg)
    assert status == 400
    assert cfg.ai_config_tools == tools


def test_get_settings_exposes_ai_config_profiles():
    from internal.web.api.settings import get_settings
    cfg = _SettingsCfg()
    cfg.ai_config_tools = ["claude_code"]
    cfg.ai_config_custom_paths = ["~/ai-configs"]
    result, status = get_settings(cfg)
    assert status == 200
    assert result["settings"]["ai_config_tools"] == ["claude_code"]
    assert result["settings"]["ai_config_custom_paths"] == ["~/ai-configs"]
