"""AI-config sync: the aiconfig wire, the config it persists, and the pull
path's refusals.

The feature runs on TOOL PROFILES (internal/sync/ai_profiles.py) instead of a
raw watch-path list: a device enables built-in profiles (Claude Code, Codex,
Cursor, Gemini) plus user custom paths, inventory entries carry a ``tool`` key
(never a root index across the wire), and landing targets are resolved through
THIS device's profile table.

What is pinned here:
  - the frame round-trip, the v2 inventory shape and its sanitization, and the
    read-only legacy (root_index) path for older peers
  - config defaults / persistence, and the legacy watch-path migration
  - every refusal a peer or the user can hit: unpaired sender, hash drift,
    traversal, truncated source, failed backup, unadvertised path — each one
    reported to the requester instead of leaving a batch stuck
  - what a pull is allowed to do to a local file: overwrite backs the file it
    replaces up, copy protects it, and neither one lands a truncated source
  - the REST surface (/api/aiconfig/*) and the settings whitelist fields

Handlers are exercised through lightweight stubs: no app/tkinter/transport
stack boots here, and every frame crosses the wire through the real codec.
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
from internal.sync import ai_config as aic
from internal.sync import ai_profiles
from internal.sync.ai_config import (
    LOCAL_SAVE_MAX_BYTES,
    MAX_ENTRIES,
    AIConfigManager,
    collect_roots,
    resolve_safe,
)

# ------------------------------------------------------------------ codec


def test_aiconfig_frames_roundtrip():
    payloads = {
        "aiconfig_inv": {
            "v": 2,
            "device_name": "DevA",
            "entries": [
                {
                    "tool": "claude_code",
                    "rel_path": "CLAUDE.md",
                    "sha256": "ab" * 8,
                    "size": 12,
                    "mtime": 123.5,
                }
            ],
        },
        "aiconfig_req": {"tool": "claude_code", "rel_path": "skills/x/SKILL.md"},
        "aiconfig_data": {
            "tool": "claude_code",
            "rel_path": "skills/x/SKILL.md",
            "sha256": "cd" * 8,
            "b64_content": "aGk=",
            "truncated": False,
        },
    }
    for msg_type, extra in payloads.items():
        raw = encode_frame({"msg_type": msg_type, **extra}, source_device="device-A")
        msg = decode_message(raw)
        assert getattr(msg, "msg_type", "") == msg_type
        assert msg.source_device == "device-A"
        for k, v in extra.items():
            assert msg._raw_payload[k] == v


def test_aiconfig_types_are_paired_only():
    # must never be admitted from unpaired peers at the transport gate
    assert set(codec.AICONFIG_MSG_TYPES) == {
        "aiconfig_inv",
        "aiconfig_req",
        "aiconfig_data",
        "aiconfig_err",
    }
    assert not (codec.AICONFIG_MSG_TYPES & codec.UNPAIRED_GATE_MSG_TYPES)


# ------------------------------------------------------------------ config


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    from internal.config import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg_mod, "_config_path", lambda: tmp_path / "config.json")
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
    path.write_text(
        json.dumps(
            {
                "ai_config_paths": ["~/.claude/CLAUDE.md", "~/.codex/config.toml", "~/my-notes"],
            }
        ),
        encoding="utf-8",
    )
    loaded = cfg_mod.load()
    # a tool is enabled when ANY of its profile entries appeared in the old list
    assert set(loaded.ai_config_tools) == {"claude_code", "codex"}
    # the unmatched path becomes a user custom path
    assert "~/my-notes" in loaded.ai_config_custom_paths


# --------------------------------------------------------------- profiles


def test_effective_roots_expands_enabled_profiles_and_custom():
    # no tools, no custom paths -> feature off
    assert ai_profiles.effective_roots([], []) == []
    # all four built-ins -> every profile entry, in display order
    roots = ai_profiles.effective_roots(ai_profiles.DEFAULT_TOOL_KEYS, [])
    assert len(roots) == 12
    assert [r[0] for r in roots] == (
        ["claude_code"] * 5 + ["codex"] * 1 + ["cursor"] * 2 + ["gemini"] * 4
    )
    # every root carries a stable, unique id — the wire identity a peer needs
    # to tell two dir roots of one tool apart
    assert all(ai_profiles.valid_root_id(r[1]) for r in roots)
    assert len({(r[0], r[1]) for r in roots}) == len(roots)
    assert ("claude_code", "memory", "file", "~/.claude/CLAUDE.md") in roots
    assert ("claude_code", "skills", "dir", "~/.claude/skills") in roots
    assert ("claude_code", "commands", "dir", "~/.claude/commands") in roots
    assert ("cursor", "rules", "dir", "~/.cursor/rules") in roots
    # custom paths ride along as pseudo-tool "custom"
    roots = ai_profiles.effective_roots(["claude_code"], ["~/extra", "D:\\n"])
    assert ("claude_code", "memory", "file", "~/.claude/CLAUDE.md") in roots
    assert ("custom", ai_profiles.custom_root_id("~/extra"), "dir", "~/extra") in roots
    assert ("custom", ai_profiles.custom_root_id("D:\\n"), "dir", "D:\\n") in roots


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
    entries = collect_roots([("custom", "r0", "dir", "~/airoot")], home=home)
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
    entries = collect_roots([("custom", "r0", "dir", "~/r")], home=home, max_bytes=10)
    paths = {e["path"] for e in entries}
    assert paths == {"keep.md", ".claude/settings.json"}
    if link is not None:
        assert "link.md" not in paths


def test_collect_caps_entries_and_skips_missing_or_duplicate_roots(tmp_path):
    root = tmp_path / "cap"
    for i in range(8):
        _mk(root, f"f{i}.md", b"x")
    entries = collect_roots(
        [
            ("custom", "r0", "dir", str(root)),
            ("custom", "r1", "dir", str(tmp_path / "missing")),
            ("custom", "r0", "dir", str(root)),
        ],
        max_entries=3,
    )
    assert len(entries) == 3  # capped; missing root skipped; dup root once


def test_resolve_safe_rejects_traversal_and_absolute():
    base = Path(tempfile.mkdtemp())
    ok = resolve_safe(base, "a/b.md")
    assert ok is not None and ok.is_absolute()
    for bad in (
        "../escape.md",
        "a/../../escape.md",
        "..\\win.md",
        "/etc/passwd",
        "C:/Windows/system32/x",
        "C:\\x",
        "",
        None,
        "a\x00b",
        "x" * 600,
    ):
        assert resolve_safe(base, bad) is None, bad
    assert resolve_safe(base, "./a/./b.md") is not None  # dot segments fine


# ----------------------------------------------------------------- manager


class _StubMgr:
    """AIConfigManager wired to recording stubs; cfg uses tool profiles.

    *roots* are user custom watch paths (tool == "custom"); *tool_keys*
    enable built-in profiles — pair those with the ``tool_home`` fixture so
    their ~/.claude etc. paths resolve into the sandbox.
    """

    def __init__(self, tmp_path, roots=None, tool_keys=(), peers=("p1",), data_dir=None):
        roots = list(roots or [])
        self.sent = []  # (peer_id, frame_bytes)
        self.events = []  # aiconfig_file WS events only
        self.all_events = []  # every WS event the manager emitted
        self.saved = []  # save_fn calls
        cfg = types.SimpleNamespace(
            device_id="selfid",
            device_name="SelfDev",
            ai_config_tools=list(tool_keys),
            ai_config_custom_paths=list(roots),
            data_dir=str(data_dir) if data_dir else "",
            peers={
                pid: types.SimpleNamespace(device_id=pid, paired=True, device_name=f"Peer-{pid}")
                for pid in peers
            },
        )
        self.cfg = cfg
        self.mgr = AIConfigManager(
            cfg,
            send_fn=lambda pid, frame: self.sent.append((pid, frame)) or True,
            connected_fn=lambda: list(peers),
            event_fn=self._on_event,
            save_fn=lambda: self.saved.append(1),
        )

    def _on_event(self, event):
        """Split the event stream: `events` stays the per-file result list the
        assertions below read, `all_events` keeps everything (inventory
        notifications included)."""
        self.all_events.append(event)
        if event.get("type") == "aiconfig_file":
            self.events.append(event)


@pytest.fixture
def tool_home(tmp_path, monkeypatch):
    """Redirect the built-in profile paths into tmp_path for sandboxed tests.

    expand_root is the manager's one funnel for every profile/custom path, so
    monkeypatching it makes ~/.claude etc. resolve to tmp_path — no real home
    is ever touched when a test enables a built-in tool profile.
    """
    from internal.sync import ai_config as aic
    from internal.sync import ai_profiles

    # Derived from the profile table rather than hand-listed, so adding a
    # profile entry can never leave this fixture silently stale (a missing key
    # would fall through to the REAL home directory).
    mapping = {}
    for tool in ai_profiles.TOOLS:
        for entry in tool["entries"]:
            sub = tool["key"].replace("_", "-")
            rel = ai_profiles.entry_path(entry).split("/", 1)[-1]
            mapping[ai_profiles.entry_path(entry)] = tmp_path / sub / rel
    # Legacy aliases kept for tests that name the old tmp layout directly.
    mapping.update(
        {
            "~/.claude/CLAUDE.md": tmp_path / "claude" / "CLAUDE.md",
            "~/.claude/settings.json": tmp_path / "claude" / "settings.json",
            "~/.claude/skills": tmp_path / "claude" / "skills",
            "~/.claude/commands": tmp_path / "claude" / "commands",
            "~/.claude/agents": tmp_path / "claude" / "agents",
            "~/.codex/config.toml": tmp_path / "codex" / "config.toml",
            "~/.cursor/rules": tmp_path / "cursor" / "rules",
            "~/.cursor/commands": tmp_path / "cursor" / "commands",
            "~/.gemini/settings.json": tmp_path / "gemini" / "settings.json",
            "~/.gemini/GEMINI.md": tmp_path / "gemini" / "GEMINI.md",
            "~/.gemini/skills": tmp_path / "gemini" / "skills",
            "~/.gemini/commands": tmp_path / "gemini" / "commands",
        }
    )

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


def _sent_types(stub):
    """msg_types of everything the stub has sent so far."""
    return [decode_message(f)._raw_payload.get("msg_type") for _, f in stub.sent]


def _assert_no_content_served(stub, note=""):
    """No file content left the box.

    A refusal is now explicit (aiconfig_err) instead of silence, so "nothing
    was served" means "no aiconfig_data", not "no frames at all" — and an
    aiconfig_err never carries content by construction.
    """
    types = _sent_types(stub)
    assert "aiconfig_data" not in types, (note, types)
    assert set(types) <= {"aiconfig_err"}, (note, types)
    for _, frame in stub.sent:
        assert "b64_content" not in decode_message(frame)._raw_payload


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
        {
            "tool": "custom",
            "rel_path": "../evil",
            "sha256": "ab" * 8,
            "size": 1,
            "mtime": 1.0,
        },  # traversal dropped
        {
            "tool": True,
            "rel_path": "bool.md",
            "sha256": "ab" * 8,
            "size": 1,
            "mtime": 1.0,
        },  # bool tool dropped
        {"tool": "custom", "rel_path": "bad-hash.md", "sha256": "zz", "size": 1, "mtime": 1.0},
        {"tool": "custom", "rel_path": "neg.md", "sha256": "ab" * 8, "size": -5, "mtime": 1.0},
        {
            "tool": "custom",
            "rel_path": "good.md",
            "sha256": "ab" * 8,
            "size": 9,
            "mtime": 7,
        },  # the one valid entry
    ]
    _feed(b, {"msg_type": "aiconfig_inv", "v": 2, "device_name": "X", "entries": bad_entries})
    entries = b.mgr.get_peer_inventories()["p1"]["entries"]
    assert [e["rel_path"] for e in entries] == ["good.md"]
    assert b.mgr.get_peer_inventories()["p1"]["legacy"] is False
    _feed(
        b,
        {
            "msg_type": "aiconfig_inv",
            "v": 2,
            "device_name": "X",
            "entries": [
                {
                    "tool": "custom",
                    "rel_path": f"f{i}.md",
                    "sha256": "ab" * 8,
                    "size": i,
                    "mtime": i,
                }
                for i in range(MAX_ENTRIES + 50)
            ]
            + ["tail-junk"],
        },
    )
    stored = b.mgr.get_peer_inventories()["p1"]["entries"]
    assert len(stored) == MAX_ENTRIES
    _feed(b, {"msg_type": "aiconfig_inv", "v": 2, "device_name": 5, "entries": "nope"})


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
    _feed(
        b,
        {
            "msg_type": "aiconfig_inv",
            "v": 2,
            "device_name": "X",
            "entries": [
                {"tool": "custom", "rel_path": "dangle", "is_dir": True, "mtime": 1.0},
                {"tool": "custom", "rel_path": "config.toml/", "is_dir": True, "mtime": 1.0},
            ],
        },
    )
    inv2 = {e["rel_path"]: e for e in b.mgr.get_peer_inventories()["p1"]["entries"]}
    assert "dangle" not in inv2
    assert inv2["config.toml/"]["is_dir"] is True


def test_unpaired_sender_rejected_for_all_three_types(tmp_path):
    b = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    b.cfg.peers["ghost"] = types.SimpleNamespace(
        device_id="ghost", paired=False, device_name="Ghost"
    )
    b.mgr.collect()
    _feed(
        b, {"msg_type": "aiconfig_inv", "v": 2, "device_name": "G", "entries": []}, peer_id="ghost"
    )
    assert b.mgr.get_peer_inventories() == {}
    _feed(b, {"msg_type": "aiconfig_req", "tool": "custom", "rel_path": "x.md"}, peer_id="ghost")
    _feed(
        b,
        {
            "msg_type": "aiconfig_data",
            "tool": "custom",
            "rel_path": "x.md",
            "sha256": "ab" * 8,
            "b64_content": "aGk=",
        },
        peer_id="ghost",
    )
    assert b.sent == [] and b.events == []


# ------------------------------------------------------ req -> serve


def test_req_serves_verified_data(tmp_path):
    s = _serving_setup(tmp_path)
    _feed(s, {"msg_type": "aiconfig_req", "tool": "custom", "rel_path": "CLAUDE.md"})
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
    _feed(s, {"msg_type": "aiconfig_req", "tool": "custom", "rel_path": "CLAUDE.md"})
    # stale inventory -> refuse until recollect.  The refusal is explicit so the
    # requester stops waiting, but it still carries no content.
    _assert_no_content_served(s, "hash drift")
    assert decode_message(s.sent[-1][1])._raw_payload["reason"] == "hash_drift"
    # File exists on disk but was never advertised:
    _mk(tmp_path / "srv-root", "secret.env", b"token=1")
    _feed(s, {"msg_type": "aiconfig_req", "tool": "custom", "rel_path": "secret.env"})
    _assert_no_content_served(s, "unadvertised")
    assert decode_message(s.sent[-1][1])._raw_payload["reason"] == "not_advertised"


def test_req_traversal_attacks_rejected(tmp_path):
    s = _serving_setup(tmp_path)
    _mk(tmp_path, "outside.md", b"top secret")  # outside srv-root
    for evil in (
        "../outside.md",
        "..\\outside.md",
        "..\\..\\outside.md",
        "sub/../../outside.md",
        "C:/Windows/win.ini",
        "/etc/passwd",
    ):
        _feed(s, {"msg_type": "aiconfig_req", "tool": "custom", "rel_path": evil})
        _assert_no_content_served(s, evil)
    before = len(s.sent)
    for bad_tool in (True, 42, "", None):
        _feed(s, {"msg_type": "aiconfig_req", "tool": bad_tool, "rel_path": "CLAUDE.md"})
        # a malformed request is dropped outright — not even a refusal, since
        # there is no well-formed (tool, rel) to name one against
        assert len(s.sent) == before, bad_tool


def test_req_legacy_form_served_from_root_index(tmp_path):
    # A pre-refactor peer (root_index req) is still served by a v2 host.
    s = _serving_setup(tmp_path)
    _feed(s, {"msg_type": "aiconfig_req", "root_index": 0, "rel_path": "CLAUDE.md"})
    assert len(s.sent) == 1
    payload = decode_message(s.sent[0][1])._raw_payload
    assert base64.b64decode(payload["b64_content"]) == b"# hello"
    for bad_idx in (-1, 5):
        s.sent.clear()
        _feed(s, {"msg_type": "aiconfig_req", "root_index": bad_idx, "rel_path": "CLAUDE.md"})
        # refused, explicitly, with no content
        _assert_no_content_served(s, bad_idx)
        assert decode_message(s.sent[-1][1])._raw_payload["reason"] == "bad_root_index"
    for bad_idx in (True, None):
        s.sent.clear()
        _feed(s, {"msg_type": "aiconfig_req", "root_index": bad_idx, "rel_path": "CLAUDE.md"})
        assert s.sent == [], bad_idx  # not a usable index at all -> dropped


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
    assert reply["v"] == 3
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
    assert [(e["status"], e["rel_path"]) for e in r.events] == [("saved", "CLAUDE.md")]

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
    # text only — decided by the bytes, not by the extension (the pulled
    # block follows the local content).
    _mk(recv_roots, "notes.md", b"local notes")  # overwrite the earlier copy
    assert _pull_and_deliver(r, s, "notes.md", "append") is False
    assert (recv_roots / "notes.md").read_bytes() == b"local notes\nline1\n"
    assert r.events[-1]["status"] == "appended"


def test_append_refuses_binary_content_and_takes_any_text(tmp_path):
    """The append rule is about content, not a filename whitelist.

    Two halves, because the rule flipped sides: what used to be refused for its
    extension (``.json`` — text, and exactly the AI-tool config people merge)
    now lands, and what used to slip through on its extension (a ``.md`` full
    of PNG bytes) is what gets turned away.
    """
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    srv_root = tmp_path / "bin-root"
    _mk(srv_root, "blob.png", b"\x89PNG")  # invalid UTF-8: not text
    _mk(srv_root, "settings.json", b'{"a": 1}')  # text, off the old whitelist
    s = _StubMgr(tmp_path, roots=[str(srv_root)])
    s.mgr.collect()

    # Off the old whitelist, and it lands: .json is text.
    _pull_and_deliver(r, s, "settings.json", "append")
    assert r.events[-1]["status"] == "appended"
    assert (recv_roots / "settings.json").read_bytes() == b'{"a": 1}\n'

    # The incoming bytes decide first: PNG magic is not UTF-8, so it is
    # refused whatever it is called — including when it is called .md.
    _mk(srv_root, "notes.md", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    s.mgr.collect()
    _pull_and_deliver(r, s, "notes.md", "append")
    assert r.events[-1]["status"] == "error"
    assert r.events[-1]["reason"] == "append_not_text"
    assert not (recv_roots / "notes.md").exists()  # nothing landed

    # ...and the local file has to be text too, or the append corrupts it.
    _mk(recv_roots, "local.md", b"\x00\x01\x02")
    _mk(srv_root, "local.md", b"plain text")
    s.mgr.collect()
    _pull_and_deliver(r, s, "local.md", "append")
    assert r.events[-1]["status"] == "error"
    assert r.events[-1]["reason"] == "append_not_text"
    assert (recv_roots / "local.md").read_bytes() == b"\x00\x01\x02"  # untouched


def test_pull_unknown_mode_falls_back_to_copy(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    s = _serving_setup(tmp_path)
    _mk(recv_roots, "notes.md", b"local")
    res = r.mgr.pull(
        "p1", [{"tool": "custom", "rel_path": "notes.md"}], mode="rm -rf"
    )  # hostile/unknown mode
    assert res["requested"] == 1
    s.mgr.handle_message("aiconfig_req", decode_message(r.sent[-1][1])._raw_payload, "p1")
    r.mgr.handle_message("aiconfig_data", decode_message(s.sent[-1][1])._raw_payload, "p1")
    # landed as COPY — the original was NOT overwritten
    assert (recv_roots / "notes.md").read_bytes() == b"local"
    assert (recv_roots / "notes.from.Peer-p1.md").read_bytes() == b"line1\n"


def test_pull_requires_paired_connected_peer(tmp_path):
    offline = _StubMgr(tmp_path, roots=[], peers=())
    res = offline.mgr.pull("p1", [{"tool": "custom", "rel_path": "x"}], mode="copy")
    assert res == {"requested": 0, "errors": ["peer_not_paired"]}
    ghost = _StubMgr(tmp_path, roots=[], peers=("g",))
    res2 = ghost.mgr.pull("unpaired-peer", [{"tool": "custom", "rel_path": "x"}], mode="copy")
    assert res2["errors"] == ["peer_not_paired"]


def test_data_hash_mismatch_b64_failure_unsolicited_dropped(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    good = base64.b64encode(b"payload").decode()

    # register one pending pull, then corrupt the hash
    r.mgr.pull("p1", [{"tool": "custom", "rel_path": "f.md"}], mode="overwrite")
    r.mgr.handle_message(
        "aiconfig_data",
        {"tool": "custom", "rel_path": "f.md", "sha256": "00" * 8, "b64_content": good},
        "p1",
    )
    assert r.events[-1]["status"] == "error"
    assert r.events[-1]["reason"] == "hash_mismatch"
    assert list(recv_roots.iterdir()) == []

    # b64 garbage
    r.mgr.pull("p1", [{"tool": "custom", "rel_path": "f.md"}], mode="overwrite")
    r.mgr.handle_message(
        "aiconfig_data",
        {
            "tool": "custom",
            "rel_path": "f.md",
            "sha256": hashlib.sha256(b"payload").hexdigest()[:16],
            "b64_content": "!!!not-b64!!!",
        },
        "p1",
    )
    assert r.events[-1]["reason"] == "b64_decode"

    # unsolicited (no matching pending) silently dropped
    n_events = len(r.events)
    r.mgr.handle_message(
        "aiconfig_data",
        {
            "tool": "custom",
            "rel_path": "other.md",
            "sha256": hashlib.sha256(b"payload").hexdigest()[:16],
            "b64_content": good,
        },
        "p1",
    )
    assert len(r.events) == n_events
    assert list(recv_roots.iterdir()) == []


# ------------------------------------------- folder whole-select + batch


def _feed_folder_pull(receiver, server, folder_item, mode="copy", batch_id="b1"):
    """Pull one folder item against a server whose inv the receiver has, and
    deliver every file back.  Returns the pull result."""
    res = receiver.mgr.pull("p1", [folder_item], mode=mode, batch_id=batch_id)
    for _pid, frame in list(receiver.sent):
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
        r, s, {"tool": "custom", "rel_path": "skills/my-skill/", "is_dir": True}, batch_id="mig1"
    )
    assert res["requested"] == 2, res
    assert res["expanded"] == 2  # folder itself dropped, two files requested
    reqs = [
        decode_message(f)._raw_payload
        for _, f in r.sent
        if decode_message(f)._raw_payload.get("msg_type") == "aiconfig_req"
    ]
    assert sorted(q["rel_path"] for q in reqs) == [
        "skills/my-skill/SKILL.md",
        "skills/my-skill/sub/a.md",
    ]

    # every file landed under the folder's relative structure.  Copy mode with
    # nothing already there writes the REAL name — a ".from.<device>" sibling
    # with no original beside it is a file the AI tool would never read.
    assert (recv_roots / "skills" / "my-skill" / "SKILL.md").read_bytes() == b"# sk"
    assert (recv_roots / "skills" / "my-skill" / "sub" / "a.md").read_bytes() == b"a"
    assert not (recv_roots / "skills" / "my-skill" / "SKILL.from.Peer-p1.md").exists()
    # the sibling folder was NOT pulled
    assert not (recv_roots / "skills" / "other").exists()
    # the folder entry itself is not a file on disk
    assert (recv_roots / "skills" / "my-skill").is_dir()

    # per-file WS events echo the batch_id
    landed = [e for e in r.events if e.get("batch_id") == "mig1"]
    assert sorted(e["rel_path"] for e in landed) == [
        "skills/my-skill/SKILL.md",
        "skills/my-skill/sub/a.md",
    ]
    assert all(e["status"] == "copied" for e in landed)
    assert all(e["tool"] == "custom" for e in landed)


def test_folder_pull_unknown_folder_passes_through_for_clean_refusal(tmp_path):
    r = _StubMgr(tmp_path, roots=[str(tmp_path / "recv-root")])
    (tmp_path / "recv-root").mkdir(exist_ok=True)
    s = _serving_setup(tmp_path)
    assert s.mgr.send_inventory_to("p1")
    _feed(r, decode_message(s.sent[-1][1])._raw_payload)
    res = r.mgr.pull(
        "p1",
        [
            {"tool": "custom", "rel_path": "no-such-folder/", "is_dir": True},
        ],
        mode="copy",
        batch_id="x",
    )
    # nothing matched the folder's prefix -> the folder item itself passes
    # through as a single request (requested == 1); the peer refuses it as
    # unadvertised, so no file content comes back and nothing lands.
    assert res["requested"] == 1
    assert res["errors"] == []
    assert res["expanded"] == 1
    req = decode_message(r.sent[-1][1])._raw_payload
    assert req["rel_path"] == "no-such-folder/"
    s.mgr.handle_message("aiconfig_req", req, "p1")
    # The refusal is explicit, not silence: the server answers aiconfig_err so
    # the requester can finish the batch now instead of waiting out PENDING_TTL.
    err = decode_message(s.sent[-1][1])._raw_payload
    assert err["msg_type"] == "aiconfig_err"
    assert err["reason"] == "not_advertised"
    assert err["rel_path"] == "no-such-folder/"
    r.mgr.handle_message("aiconfig_err", err, "p1")
    assert [e["status"] for e in r.events] == ["error"]
    assert r.events[0]["reason"] == "not_advertised"
    assert r.events[0]["batch_id"] == "x"
    # and nothing was written on the receiving side
    assert list((tmp_path / "recv-root").iterdir()) == []


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
    res = recv.mgr.pull("p1", [{"tool": "claude_code", "rel_path": "CLAUDE.md"}], mode="copy")
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
    res = recv.mgr.pull("p1", [{"tool": "claude_code", "rel_path": "CLAUDE.md"}], mode="copy")
    assert res["requested"] == 1
    req = decode_message(recv.sent[-1][1])._raw_payload
    srv.mgr.handle_message("aiconfig_req", req, "p1")
    reply = decode_message(srv.sent[-1][1])._raw_payload
    recv.mgr.handle_message("aiconfig_data", reply, "p1")
    # landed beside ~/.claude/CLAUDE.md — NOT inside the skills dir
    assert (tool_home / "claude" / "CLAUDE.from.Peer-p1.md").read_bytes() == b"# hello"
    assert not (tool_home / "claude" / "skills" / "CLAUDE.md").exists()


def test_same_rel_under_two_dir_roots_both_survive_inventory(tool_home):
    """Regression: rel_path is relative to its OWN root, so cursor's rules/ and
    commands/ dirs can each hold a 'x.md' — two different files that both say
    rel_path='x.md'.  The inventory used to key entries on (tool, rel_path)
    alone and silently dropped the second one.  With the root id in the key both
    survive, with distinct hashes."""
    srv = _StubMgr(tool_home, tool_keys=["cursor"], roots=[])
    _mk(tool_home / "cursor" / "rules", "x.md", b"from-rules")
    _mk(tool_home / "cursor" / "commands", "x.md", b"from-commands")
    srv.mgr.collect()

    files = [e for e in srv.mgr._local_entries if not e.get("is_dir")]
    assert [e["path"] for e in files] == ["x.md", "x.md"], files
    assert {e["root"] for e in files} == {"rules", "commands"}
    assert len({e["sha256"] for e in files}) == 2

    inv = srv.mgr.build_inv_payload()
    assert inv["v"] == 3
    pairs = sorted((e["root"], e["rel_path"]) for e in inv["entries"] if not e.get("is_dir"))
    assert pairs == [("commands", "x.md"), ("rules", "x.md")]


def test_same_rel_under_two_dir_roots_each_lands_in_its_own_root(tool_home):
    """Regression: with only (tool, rel_path) on the wire the receiver could not
    tell which of cursor's two dir roots a file came from and refused the pull
    as 'ambiguous_tool_root'.  The v3 root id resolves each one to ITS root."""
    srv = _StubMgr(tool_home, tool_keys=["cursor"], roots=[])
    _mk(tool_home / "cursor" / "rules", "x.md", b"from-rules")
    _mk(tool_home / "cursor" / "commands", "x.md", b"from-commands")
    srv.mgr.collect()

    recv = _StubMgr(tool_home, tool_keys=["cursor"], roots=[])
    _feed(recv, srv.mgr.build_inv_payload())
    res = recv.mgr.pull(
        "p1",
        [
            {"tool": "cursor", "root": "rules", "rel_path": "x.md"},
            {"tool": "cursor", "root": "commands", "rel_path": "x.md"},
        ],
        mode="copy",
        batch_id="two-roots",
    )
    assert res["requested"] == 2 and res["errors"] == []

    reqs = [
        decode_message(f)._raw_payload
        for _, f in recv.sent
        if decode_message(f)._raw_payload.get("msg_type") == "aiconfig_req"
    ]
    assert sorted(q["root"] for q in reqs[-2:]) == ["commands", "rules"]
    for req in reqs[-2:]:
        srv.mgr.handle_message("aiconfig_req", req, "p1")
        recv.mgr.handle_message("aiconfig_data", decode_message(srv.sent[-1][1])._raw_payload, "p1")

    # each copy landed beside the original inside its OWN root
    assert (tool_home / "cursor" / "rules" / "x.from.Peer-p1.md").read_bytes() == b"from-rules"
    assert (
        tool_home / "cursor" / "commands" / "x.from.Peer-p1.md"
    ).read_bytes() == b"from-commands"
    landed = [e for e in recv.events if e["type"] == "aiconfig_file"]
    assert [e["status"] for e in landed] == ["copied", "copied"]
    assert sorted(e["root"] for e in landed) == ["commands", "rules"]


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
    _feed(
        r,
        {
            "msg_type": "aiconfig_inv",
            "device_name": "Old",
            "entries": [
                {"root_index": 0, "path": "CLAUDE.md", "sha256": "ab" * 8, "size": 1, "mtime": 1.0}
            ],
        },
    )
    res = r.mgr.preview("p1", "custom", "CLAUDE.md")
    assert res == {"ok": False, "error": "legacy_peer"}


# ---------------------------------------------- legacy (read-only) compat


def test_legacy_inv_cached_read_only(tmp_path):
    b = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _feed(
        b,
        {
            "msg_type": "aiconfig_inv",
            "device_name": "Old",
            "entries": [
                {"root_index": 0, "path": "CLAUDE.md", "sha256": "ab" * 8, "size": 1, "mtime": 1.0}
            ],
        },
    )
    inv = b.mgr.get_peer_inventories()["p1"]
    assert inv["legacy"] is True
    assert inv["name"] == "Old"
    assert inv["entries"][0]["root_index"] == 0
    assert inv["entries"][0]["path"] == "CLAUDE.md"
    # pull is blocked: root indices cannot resolve to THIS device's profiles
    res = b.mgr.pull("p1", [{"tool": "custom", "rel_path": "CLAUDE.md"}], mode="copy")
    assert res == {"requested": 0, "errors": ["legacy_peer_read_only"]}


def test_legacy_preview_still_works(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgr(tmp_path, roots=[str(recv_roots)])
    s = _serving_setup(tmp_path)
    legacy_inv = {
        "msg_type": "aiconfig_inv",
        "device_name": "Old",
        "entries": [
            {"root_index": 0, "path": "CLAUDE.md", "sha256": "ab" * 8, "size": 1, "mtime": 1.0}
        ],
    }
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


# ------------------------------------------------------------- REST layer


def test_api_routes_with_bound_manager(tmp_path):
    from internal.web.api import aiconfig as api

    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md")
    api.bind(s.mgr)
    try:
        data, status = api.handle("GET", "/api/aiconfig/inventory", {}, b"")
        assert status == 200 and data["peers"] == {}
        data, status = api.handle(
            "GET", "/api/aiconfig/inventory", {"refresh": ["1"], "peer_id": ["p1"]}, b""
        )
        assert status == 200 and data["refreshed"] == ["p1"]
        data, status = api.handle("GET", "/api/aiconfig/inventory", {"refresh": ["1"]}, b"")
        assert status == 200 and data["refreshed"] == []  # no cached peers yet

        data, status = api.handle(
            "POST",
            "/api/aiconfig/pull",
            {},
            json.dumps(
                {
                    "peer_id": "p1",
                    "mode": "copy",
                    "batch_id": "b1",
                    "items": [{"tool": "custom", "rel_path": "CLAUDE.md"}, "junk"],
                }
            ).encode(),
        )
        assert status == 200 and data["requested"] == 1
        assert data["expanded"] == 2

        data, status = api.handle(
            "POST", "/api/aiconfig/pull", {}, json.dumps({"items": []}).encode()
        )
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
        assert data["tools"] == ai_profiles.TOOLS  # single source of truth
        assert data["enabled"] == []  # no built-ins enabled yet
        assert data["custom_paths"] == [str(tmp_path / "r")]

        # POST normalizes + persists + recollects + rebroadcasts
        data, status = api.handle(
            "POST",
            "/api/aiconfig/profiles",
            {},
            json.dumps(
                {
                    "tools": ["claude_code", "bogus", "claude_code"],
                    "custom_paths": ["~/x", "", "~/x", "~/y"],
                }
            ).encode(),
        )
        assert status == 200
        assert data["tools"] == ["claude_code"]
        assert data["custom_paths"] == ["~/x", "~/y"]
        assert "broadcast_to" in data
        assert s.cfg.ai_config_tools == ["claude_code"]
        assert s.cfg.ai_config_custom_paths == ["~/x", "~/y"]
        assert s.saved == [1]
    finally:
        api.bind(None)


# ------------------------------------------------------------- local manager


def test_local_listing_shape_and_reuses_collector(tmp_path):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules\n")
    _mk(tmp_path / "r", "sub/notes.md", b"hi")
    listing = s.mgr.local_listing()
    assert set(listing) == {"collected_at", "tools", "custom_paths", "roots", "entries"}
    assert listing["collected_at"] > 0
    assert listing["custom_paths"] == [str(tmp_path / "r")]
    assert listing["tools"] == []  # no built-in profiles enabled
    # The local file manager shows the folder tree, so "sub/" appears as a
    # folder entry alongside its file.
    assert listing["roots"] == [
        {
            "root_index": 0,
            "tool": "custom",
            "root": ai_profiles.custom_root_id(str(tmp_path / "r")),
            "kind": "dir",
            "path": str(tmp_path / "r"),
            "count": 3,
        }
    ]
    assert len(listing["entries"]) == 3
    by_rel = {e["rel_path"]: e for e in listing["entries"]}
    assert set(by_rel) == {"CLAUDE.md", "sub/notes.md", "sub/"}
    for e in listing["entries"]:
        assert e["tool"] == "custom"
        # every entry names the root its rel_path is relative to
        assert e["root"] == ai_profiles.custom_root_id(str(tmp_path / "r"))
        assert set(e) == {"tool", "root", "rel_path", "size", "mtime", "sha256", "is_dir"}
    # folder entry: no size / empty content hash, flagged is_dir
    assert by_rel["sub/"]["is_dir"] is True
    assert by_rel["sub/"]["size"] is None and by_rel["sub/"]["sha256"] == ""
    assert by_rel["CLAUDE.md"]["is_dir"] is False
    assert by_rel["CLAUDE.md"]["size"] > 0 and by_rel["CLAUDE.md"]["mtime"] > 0
    # entries carry the collector's sha256 prefix, not a copy
    assert by_rel["CLAUDE.md"]["sha256"] == hashlib.sha256(b"# rules\n").hexdigest()[:16]


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
    assert (tmp_path / "r" / "CLAUDE.md").read_text(encoding="utf-8") == "# new content\n"
    # pre-save original preserved as <rel_path>.bak in the same directory
    assert (tmp_path / "r" / "CLAUDE.md.bak").read_text(encoding="utf-8") == "# old\n"
    # a second save overwrites the .bak with the newest pre-save content
    res2 = s.mgr.local_save("custom", "CLAUDE.md", "# v3\n")
    assert res2["ok"] is True
    assert (tmp_path / "r" / "CLAUDE.md.bak").read_text(encoding="utf-8") == "# new content\n"
    # .bak shows up in the local listing = "edited here" marker
    rels = {e["rel_path"] for e in s.mgr.local_listing()["entries"]}
    assert "CLAUDE.md.bak" in rels


def test_local_save_rejects_binary_traversal_oversize_and_missing(tmp_path):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "f.md", b"x")
    assert s.mgr.local_save("custom", "f.md", "a\x00b")["error"] == "binary"
    assert s.mgr.local_save("custom", "../evil.md", "text")["error"] == "no_local_root"
    assert s.mgr.local_save("custom", "C:/evil.md", "text")["error"] == "no_local_root"
    assert (
        s.mgr.local_save("custom", "f.md", "y" * (LOCAL_SAVE_MAX_BYTES + 1))["error"] == "too_large"
    )
    assert s.mgr.local_save("custom", "f.md", None)["error"] == "content_required"
    assert s.mgr.local_save("custom", "f.md", b"not-str")["error"] == "content_required"
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


def test_open_windows_uses_startfile(tmp_path, monkeypatch):
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules")
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p), raising=False)
    res = s.mgr.local_open("custom", "CLAUDE.md")
    assert res["ok"] is True
    assert calls == [str(tmp_path / "r" / "CLAUDE.md")]


def test_open_posix_branches_use_open_and_xdg_open(tmp_path, monkeypatch):
    import subprocess

    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules")
    for plat, argv0 in (("darwin", "open"), ("linux", "xdg-open")):
        seen = []

        def fake_run(cmd, check=True, seen=seen):
            seen.append(cmd)

        monkeypatch.setattr(sys, "platform", plat)
        monkeypatch.setattr(subprocess, "run", fake_run)
        res = s.mgr.local_open("custom", "CLAUDE.md")
        assert res["ok"] is True, plat
        assert seen == [[argv0, str(tmp_path / "r" / "CLAUDE.md")]], plat


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
        assert set(data) == {"collected_at", "tools", "custom_paths", "roots", "entries"}
        assert len(data["entries"]) == 1

        # GET /api/aiconfig/local/item
        data, status = api.handle(
            "GET", "/api/aiconfig/local/item", {"tool": ["custom"], "rel_path": ["CLAUDE.md"]}, b""
        )
        assert status == 200 and data["content"] == "# rules\n"
        data, status = api.handle(
            "GET", "/api/aiconfig/local/item", {"tool": ["custom"], "rel_path": ["nope.md"]}, b""
        )
        assert status == 404 and data["error"] == "not_found"
        data, status = api.handle(
            "GET",
            "/api/aiconfig/local/item",
            {"tool": ["custom"], "rel_path": ["../escape.md"]},
            b"",
        )
        assert status == 400 and data["error"] == "no_local_root"

        # POST /api/aiconfig/local/save
        data, status = api.handle(
            "POST",
            "/api/aiconfig/local/save",
            {},
            json.dumps(
                {"tool": "custom", "rel_path": "CLAUDE.md", "content": "# saved\n"}
            ).encode(),
        )
        assert status == 200 and data["ok"] is True
        assert (tmp_path / "r" / "CLAUDE.md").read_text(encoding="utf-8") == "# saved\n"

        # POST /api/aiconfig/local/trash
        data, status = api.handle(
            "POST",
            "/api/aiconfig/local/trash",
            {},
            json.dumps({"tool": "custom", "rel_path": "CLAUDE.md"}).encode(),
        )
        assert status == 200 and data["ok"] is True
        assert Path(data["trashed_to"]).exists()
        assert not (tmp_path / "r" / "CLAUDE.md").exists()

        # unbound -> 503
        api.bind(None)
        data, status = api.handle("POST", "/api/aiconfig/local/save", {}, b"{}")
        assert status == 503
    finally:
        api.bind(None)


# ══════════════════════════════════════════════════
# merged from test_round19_presets.py — single-file custom roots
# ══════════════════════════════════════════════════


def test_file_root_advertises_itself_as_one_entry(tmp_path):
    f = _mk(tmp_path, "CLAUDE.md", b"# rules\n")
    entries = collect_roots([("custom", "r0", "file", str(f))])
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


# ══════════════════════════════════════════════════
# landing honesty: the UI's promises must match what _land_file does
# ══════════════════════════════════════════════════


class TestLandingKeepsItsPromises:
    """The strategy hints in the locale files make two concrete promises:
    overwrite backs the replaced file up as ``.bak``, and the "only files I do
    not have" strategy leaves existing files alone.  Both were text-only."""

    def test_overwrite_backs_up_what_it_replaced(self, tmp_path):
        recv_roots = tmp_path / "recv-root"
        recv_roots.mkdir()
        r = _StubMgr(tmp_path, roots=[str(recv_roots)])
        s = _serving_setup(tmp_path)
        _mk(recv_roots, "CLAUDE.md", b"# hand-tuned, took an hour")
        assert _pull_and_deliver(r, s, "CLAUDE.md", "overwrite") is False
        assert (recv_roots / "CLAUDE.md").read_bytes() == b"# hello"
        # the displaced content is recoverable, under the name the hint promises
        assert (recv_roots / "CLAUDE.md.bak").read_bytes() == b"# hand-tuned, took an hour"
        assert r.events[-1]["status"] == "saved"

    def test_overwrite_aborts_when_the_backup_fails(self, tmp_path, monkeypatch):
        """A backup that cannot be written must stop the overwrite, not proceed
        without it — the whole point is that the local file stays recoverable."""
        recv_roots = tmp_path / "recv-root"
        recv_roots.mkdir()
        r = _StubMgr(tmp_path, roots=[str(recv_roots)])
        s = _serving_setup(tmp_path)
        _mk(recv_roots, "CLAUDE.md", b"# precious")

        def boom(src, dst, *a, **kw):
            raise OSError(13, "no")

        monkeypatch.setattr(aic.shutil, "copy2", boom)
        assert _pull_and_deliver(r, s, "CLAUDE.md", "overwrite") is False
        assert (recv_roots / "CLAUDE.md").read_bytes() == b"# precious"
        assert r.events[-1]["status"] == "error"
        assert r.events[-1]["reason"] == "backup_failed"

    def test_copy_writes_the_real_name_when_nothing_is_there(self, tmp_path):
        """ "Only pull what I don't have" has to produce a file the AI tool will
        actually read — CLAUDE.from.Laptop.md is invisible to every tool."""
        recv_roots = tmp_path / "recv-root"
        recv_roots.mkdir()
        r = _StubMgr(tmp_path, roots=[str(recv_roots)])
        s = _serving_setup(tmp_path)
        assert _pull_and_deliver(r, s, "CLAUDE.md", "copy") is False
        assert (recv_roots / "CLAUDE.md").read_bytes() == b"# hello"
        assert sorted(p.name for p in recv_roots.iterdir()) == ["CLAUDE.md"]
        assert r.events[-1]["status"] == "copied"


class TestTruncatedSourceIsNeverLanded:
    """A file that grows past MAX_CONFIG_FILE_SIZE between the inventory and
    the request is served with ``truncated: true`` and only its first 1 MB.
    ``truncated`` also skips the hash check, so nothing else stops it from
    replacing a complete local file with a cut-off one."""

    def _grown_setup(self, tmp_path):
        """Advertise a small file, then let it grow past the cap.

        Oversize files are never advertised in the first place, so this
        collect-then-grow race is the only way a truncated payload is served.
        """
        srv_root = tmp_path / "srv-root"
        _mk(srv_root, "big.md", b"# small for now\n")
        s = _StubMgr(tmp_path, roots=[str(srv_root)])
        s.mgr.collect()
        _mk(srv_root, "big.md", b"y" * (aic.MAX_CONFIG_FILE_SIZE + 500))
        return s

    def test_truncated_payload_is_refused(self, tmp_path):
        recv_roots = tmp_path / "recv-root"
        recv_roots.mkdir()
        r = _StubMgr(tmp_path, roots=[str(recv_roots)])
        s = self._grown_setup(tmp_path)
        _mk(recv_roots, "big.md", b"# my complete file")
        res = r.mgr.pull("p1", [{"tool": "custom", "rel_path": "big.md"}], mode="overwrite")
        assert res["requested"] == 1
        req = decode_message(r.sent[-1][1])._raw_payload
        s.mgr.handle_message("aiconfig_req", req, "p1")
        reply = decode_message(s.sent[-1][1])._raw_payload
        assert reply["msg_type"] == "aiconfig_data"
        assert reply["truncated"] is True
        r.mgr.handle_message("aiconfig_data", reply, "p1")
        # the local file is untouched and the user is told why
        assert (recv_roots / "big.md").read_bytes() == b"# my complete file"
        assert not (recv_roots / "big.md.bak").exists()
        assert r.events[-1]["status"] == "error"
        assert r.events[-1]["reason"] == "source_truncated"

    def test_truncated_preview_is_still_allowed(self, tmp_path):
        """Refusing to LAND a truncated file must not break previewing it —
        a preview is display-only and says so via its `truncated` flag."""
        r = _StubMgr(tmp_path, roots=[str(tmp_path / "recv-root")])
        (tmp_path / "recv-root").mkdir(exist_ok=True)
        s = self._grown_setup(tmp_path)
        box = {}

        def run():
            box["res"] = r.mgr.preview("p1", "custom", "big.md", timeout=5.0)

        th = threading.Thread(target=run)
        th.start()
        deadline = time.time() + 2.0
        while not r.sent and time.time() < deadline:
            time.sleep(0.01)
        assert r.sent, "preview never sent its request"
        s.mgr.handle_message("aiconfig_req", decode_message(r.sent[-1][1])._raw_payload, "p1")
        r.mgr.handle_message("aiconfig_data", decode_message(s.sent[-1][1])._raw_payload, "p1")
        th.join(5.0)
        assert not th.is_alive()
        assert box["res"]["ok"] is True
        assert box["res"]["truncated"] is True
        assert len(box["res"]["content"]) == aic.PREVIEW_MAX_BYTES
        assert list((tmp_path / "recv-root").iterdir()) == []


class TestRefusalsAreReported:
    """Every "we will not serve this" path used to just return, leaving the
    requester to wait out PENDING_TTL with a batch stuck at N-1/N."""

    def test_hash_drift_refusal_reaches_the_requester(self, tmp_path):
        recv_roots = tmp_path / "recv-root"
        recv_roots.mkdir()
        r = _StubMgr(tmp_path, roots=[str(recv_roots)])
        s = _serving_setup(tmp_path)
        res = r.mgr.pull(
            "p1", [{"tool": "custom", "rel_path": "CLAUDE.md"}], mode="copy", batch_id="b7"
        )
        assert res["requested"] == 1
        req = decode_message(r.sent[-1][1])._raw_payload
        # the file changes between the inventory and the request
        _mk(tmp_path / "srv-root", "CLAUDE.md", b"# changed underneath")
        s.mgr.handle_message("aiconfig_req", req, "p1")
        err = decode_message(s.sent[-1][1])._raw_payload
        assert err["msg_type"] == "aiconfig_err"
        assert err["reason"] == "hash_drift"
        r.mgr.handle_message("aiconfig_err", err, "p1")
        assert r.events[-1]["status"] == "error"
        assert r.events[-1]["reason"] == "hash_drift"
        assert r.events[-1]["batch_id"] == "b7"
        assert list(recv_roots.iterdir()) == []  # nothing was written

    def test_a_refusal_for_an_unknown_pending_is_ignored(self, tmp_path):
        """An aiconfig_err we never asked for must not fabricate a UI event."""
        r = _StubMgr(tmp_path, roots=[str(tmp_path / "recv-root")])
        r.mgr.handle_message(
            "aiconfig_err",
            {"tool": "custom", "rel_path": "never-asked.md", "reason": "hash_drift"},
            "p1",
        )
        assert r.events == []

    def test_an_abandoned_pull_is_reported_when_its_pending_expires(self, tmp_path):
        """The old-peer path: a peer that refuses by staying silent.  The
        pending record expires and the batch must still reach a terminal
        state instead of showing progress forever.

        Through ``expire_pulls``, which is what the runtime's tick calls.
        The sweep itself was reachable all along — only from ``pull()`` and
        ``_preview_wait()``, so nothing ran it while a batch was waiting, and
        this test passing is what let that go unnoticed."""
        r = _StubMgr(tmp_path, roots=[str(tmp_path / "recv-root")])
        s = _serving_setup(tmp_path)
        _feed(r, s.mgr.build_inv_payload())
        res = r.mgr.pull(
            "p1", [{"tool": "custom", "rel_path": "CLAUDE.md"}], mode="copy", batch_id="ghost"
        )
        assert res["requested"] == 1
        assert r.events == []  # nothing reported while the request is in flight
        # age the pending record past its TTL, then trip the sweep
        with r.mgr._lock:
            for rec in r.mgr._pending.values():
                rec["ts"] -= aic.PENDING_TTL + 1
        r.mgr.expire_pulls()
        assert r.mgr._pending == {}
        assert r.events[-1]["status"] == "error"
        assert r.events[-1]["reason"] == "no_reply"
        assert r.events[-1]["batch_id"] == "ghost"


class TestInventoryRefreshIsHonest:
    """?refresh=1 promises the peer's *current* inventory."""

    def test_a_refresh_request_rescans_the_roots(self, tmp_path):
        s = _serving_setup(tmp_path)
        n_before = len(s.mgr._local_entries)
        # a new file appears after the last collect()
        _mk(tmp_path / "srv-root", "AGENTS.md", b"# new")
        s.mgr.handle_message("aiconfig_req", {"inventory_refresh": True}, "p1")
        reply = decode_message(s.sent[-1][1])._raw_payload
        assert reply["msg_type"] == "aiconfig_inv"
        rels = [e["rel_path"] for e in reply["entries"]]
        assert "AGENTS.md" in rels, "refresh answered from the stale cache"
        assert len(reply["entries"]) == n_before + 1

    def test_back_to_back_refreshes_do_not_rescan(self, tmp_path):
        """A peer opening the panel repeatedly must not drive a disk walk per
        click; the second answer comes from the cache the first one built."""
        s = _serving_setup(tmp_path)
        calls = []
        real = s.mgr.collect
        s.mgr.collect = lambda: (calls.append(1), real())[1]
        s.mgr.handle_message("aiconfig_req", {"inventory_refresh": True}, "p1")
        assert len(calls) == 1
        _mk(tmp_path / "srv-root", "AGENTS.md", b"# new")
        s.mgr.handle_message("aiconfig_req", {"inventory_refresh": True}, "p1")
        assert len(calls) == 1, "throttle did not hold"
        reply = decode_message(s.sent[-1][1])._raw_payload
        assert "AGENTS.md" not in [e["rel_path"] for e in reply["entries"]]

    def test_a_failing_rescan_still_answers(self, tmp_path):
        """A collect() that raises (a root vanished mid-walk) must not swallow
        the reply — the peer would sit waiting for an inventory forever."""
        s = _serving_setup(tmp_path)

        def boom():
            raise OSError("root went away")

        s.mgr.collect = boom
        s.mgr.handle_message("aiconfig_req", {"inventory_refresh": True}, "p1")
        assert decode_message(s.sent[-1][1])._raw_payload["msg_type"] == "aiconfig_inv"

    def test_an_arriving_inventory_announces_itself(self, tmp_path):
        """GET ?refresh=1 returns the STALE cache and asks the peers in the
        background, so the WS event is the UI's only signal that the fresh
        entries can now be read."""
        r = _StubMgr(tmp_path, roots=[])
        s = _serving_setup(tmp_path)
        _feed(r, s.mgr.build_inv_payload())
        evs = [e for e in r.all_events if e["type"] == "aiconfig_inventory"]
        assert len(evs) == 1
        assert evs[0]["peer_id"] == "p1"
        assert evs[0]["device_name"] == "SelfDev"
        assert evs[0]["count"] == len(s.mgr._local_entries)
        assert evs[0]["legacy"] is False
        # ... and it is not mistaken for a per-file pull result
        assert r.events == []


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
        settings_api,
        "_config_path",
        lambda: tmp_path / "config.json",
    )
    monkeypatch.setattr(
        settings_api,
        "save_config",
        lambda cfg, enc_mgr=None: None,
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
        _body({"ai_config_tools": tools, "ai_config_custom_paths": custom}), cfg
    )
    assert status == 200
    assert data["updated"]["ai_config_tools"] == tools
    assert data["updated"]["ai_config_custom_paths"] == custom
    assert cfg.ai_config_tools == tools
    assert cfg.ai_config_custom_paths == custom

    # A non-list value is rejected without touching the field.
    data, status = update_settings(_body({"ai_config_tools": "~/x"}), cfg)
    assert status == 400
    assert cfg.ai_config_tools == tools


# ---------------------------------------------- local_listing keeps its scan


def test_local_listing_stores_the_scan_it_just_did(tmp_path):
    """A dashboard refresh must also refresh what peers are told.

    ``local_listing()`` does a full ``collect_roots()`` walk.  It used to
    stamp ``_local_collected_at`` and throw the entries away, so
    ``build_inv_payload()`` kept answering peers from the snapshot taken at
    process start: the dashboard showed a new file, the peer never did.
    """
    s = _StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules\n")
    s.mgr.collect()
    assert [e["path"] for e in s.mgr._local_entries] == ["CLAUDE.md"]

    # A file appears after the initial collect.
    _mk(tmp_path / "r", "AGENTS.md", b"# agents\n")
    listing = s.mgr.local_listing()

    rels = {e["rel_path"] for e in listing["entries"]}
    assert "AGENTS.md" in rels  # the dashboard sees it...
    stored = {e["path"] for e in s.mgr._local_entries}
    assert "AGENTS.md" in stored  # ...and so does the peer
    wire = {e["rel_path"] for e in s.mgr.build_inv_payload()["entries"]}
    assert "AGENTS.md" in wire

