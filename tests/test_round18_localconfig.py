"""Round 18 — local AI-config file manager (no pairing required).

The Round-12 AIConfigManager already collects a local inventory, serves
verified file content to paired peers, and lands pulled files.  This round
adds *local* file-management on top of the SAME collector, so a user can
browse and edit their own AI-config files without any peer:

  GET  /api/aiconfig/local            listing (collector-shaped)
  GET  /api/aiconfig/local/item       read one text file (<=64KB)
  POST /api/aiconfig/local/save       write back + auto .bak (atomic)
  POST /api/aiconfig/local/trash      move to recoverable trash (no delete)
  POST /api/aiconfig/open             open with the OS default app

Everything is exercised through the same lightweight stubs as
test_round12_aiconfig.py — no app/tkinter/transport stack boots.
"""

import hashlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.sync.ai_config import (
    LOCAL_SAVE_MAX_BYTES,
    AIConfigManager,
    open_with_default_app,
)


def _mk(root, rel, content=b"data"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


class StubMgr:
    """Manager wired to recording stubs (mirrors round-12 test stubs)."""

    def __init__(self, tmp_path, roots, data_dir=None, peers=("p1",)):
        self.sent = []
        self.events = []
        self.saved = []
        cfg = types.SimpleNamespace(
            device_id="selfid",
            device_name="SelfDev",
            ai_config_paths=list(roots),
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


# -------------------------------------------------------------- listing

def test_local_listing_shape_and_reuses_collector(tmp_path):
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules\n")
    _mk(tmp_path / "r", "sub/notes.md", b"hi")
    listing = s.mgr.local_listing()
    assert set(listing) == {"collected_at", "roots", "entries"}
    assert listing["collected_at"] > 0
    assert listing["roots"] == [
        {"root_index": 0, "path": str(tmp_path / "r"), "count": 2}]
    assert len(listing["entries"]) == 2
    by_rel = {e["rel_path"]: e for e in listing["entries"]}
    assert set(by_rel) == {"CLAUDE.md", "sub/notes.md"}
    for e in listing["entries"]:
        assert set(e) == {"root_index", "rel_path", "size", "mtime", "sha256"}
        assert e["root_index"] == 0
        assert e["size"] > 0 and e["mtime"] > 0
    # entries carry the collector's sha256 prefix, not a copy
    assert by_rel["CLAUDE.md"]["sha256"] == \
        hashlib.sha256(b"# rules\n").hexdigest()[:16]


def test_local_listing_empty_when_no_paths(tmp_path):
    s = StubMgr(tmp_path, roots=[])
    listing = s.mgr.local_listing()
    assert listing["roots"] == [] and listing["entries"] == []
    assert listing["collected_at"] > 0


# ---------------------------------------------------------------- item

def test_local_item_reads_text_and_truncates(tmp_path):
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "small.md", b"# hello")
    _mk(tmp_path / "r", "big.md", b"x" * 70000)
    res = s.mgr.local_read(0, "small.md")
    assert res == {"ok": True, "content": "# hello", "truncated": False}
    res2 = s.mgr.local_read(0, "big.md")
    assert res2["ok"] is True and res2["truncated"] is True
    assert len(res2["content"].encode("utf-8")) <= 64 * 1024


def test_local_item_rejects_binary_missing_and_traversal(tmp_path):
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "blob.png", b"\x89PNG\r\n\x1a\n\x00data")
    assert s.mgr.local_read(0, "blob.png")["error"] == "binary"
    assert s.mgr.local_read(0, "missing.md")["error"] == "not_found"
    assert s.mgr.local_read(0, "../escape.md")["error"] == "unsafe_path"
    assert s.mgr.local_read(0, "C:/win.ini")["error"] == "unsafe_path"
    assert s.mgr.local_read(9, "small.md")["error"] == "no_root"


# ---------------------------------------------------------------- save

def test_local_save_writes_back_and_auto_backup(tmp_path):
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "CLAUDE.md", b"# old\n")
    res = s.mgr.local_save(0, "CLAUDE.md", "# new content\n")
    assert res["ok"] is True
    assert (tmp_path / "r" / "CLAUDE.md").read_text(encoding="utf-8") \
        == "# new content\n"
    # pre-save original preserved as <rel_path>.bak in the same directory
    assert (tmp_path / "r" / "CLAUDE.md.bak").read_text(encoding="utf-8") \
        == "# old\n"
    # a second save overwrites the .bak with the newest pre-save content
    res2 = s.mgr.local_save(0, "CLAUDE.md", "# v3\n")
    assert res2["ok"] is True
    assert (tmp_path / "r" / "CLAUDE.md.bak").read_text(encoding="utf-8") \
        == "# new content\n"
    # .bak shows up in the local listing = "edited here" marker
    rels = {e["rel_path"] for e in s.mgr.local_listing()["entries"]}
    assert "CLAUDE.md.bak" in rels


def test_local_save_creates_missing_file_in_subdir(tmp_path):
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    res = s.mgr.local_save(0, "skills/deep/SKILL.md", "# new skill\n")
    assert res["ok"] is True
    p = tmp_path / "r" / "skills" / "deep" / "SKILL.md"
    assert p.read_text(encoding="utf-8") == "# new skill\n"
    assert not (tmp_path / "r" / "skills" / "deep" / "SKILL.md.bak").exists()


def test_local_save_rejects_binary_traversal_oversize_and_missing(tmp_path):
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "f.md", b"x")
    assert s.mgr.local_save(0, "f.md", "a\x00b")["error"] == "binary"
    assert s.mgr.local_save(0, "../evil.md", "text")["error"] == "unsafe_path"
    assert s.mgr.local_save(0, "C:/evil.md", "text")["error"] == "unsafe_path"
    assert s.mgr.local_save(0, "f.md", "y" * (LOCAL_SAVE_MAX_BYTES + 1))[
        "error"] == "too_large"
    assert s.mgr.local_save(0, "f.md", None)["error"] == "content_required"
    assert s.mgr.local_save(0, "f.md", b"not-str")["error"] == "content_required"
    # nothing was touched by any of the failed saves
    assert (tmp_path / "r" / "f.md").read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "r" / "f.md.bak").exists()


# ---------------------------------------------------------------- trash

def test_local_trash_moves_into_recoverable_trash(tmp_path):
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "sub/notes.md", b"secret")
    res = s.mgr.local_trash(0, "sub/notes.md")
    assert res["ok"] is True
    trashed = Path(res["trashed_to"])
    assert trashed.exists() and trashed.is_file()
    assert trashed.read_text(encoding="utf-8") == "secret"
    # original is gone, but a recoverable copy remains under data_dir/aiconfig_trash
    assert not (tmp_path / "r" / "sub" / "notes.md").exists()
    assert tmp_path / "aiconfig_trash" in trashed.parents
    # original path naming preserved: sub/notes.md -> aiconfig_trash/sub/...
    assert trashed.parent == tmp_path / "aiconfig_trash" / "sub"


def test_local_trash_renames_on_collision_and_rejects_bad(tmp_path):
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "a.md", b"one")
    r1 = s.mgr.local_trash(0, "a.md")
    assert r1["ok"] is True
    _mk(tmp_path / "r", "a.md", b"two")
    r2 = s.mgr.local_trash(0, "a.md")
    assert r2["ok"] is True
    assert Path(r2["trashed_to"]) != Path(r1["trashed_to"])
    assert Path(r2["trashed_to"]).read_text(encoding="utf-8") == "two"
    assert Path(r1["trashed_to"]).read_text(encoding="utf-8") == "one"
    # bad inputs
    assert s.mgr.local_trash(0, "../evil.md")["error"] == "unsafe_path"
    assert s.mgr.local_trash(0, "missing.md")["error"] == "not_found"


def test_local_trash_suffix_increments_when_destination_exists(tmp_path,
                                                               monkeypatch):
    from internal.sync import ai_config as aic
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "f.md", b"live")
    monkeypatch.setattr(aic.time, "strftime", lambda fmt: "20260101_000000")
    dest = tmp_path / "aiconfig_trash" / "20260101_000000_f.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("occupied")
    res = s.mgr.local_trash(0, "f.md")
    assert res["ok"] is True
    assert Path(res["trashed_to"]).name == "20260101_000000_f-2.md"
    assert Path(res["trashed_to"]).read_text(encoding="utf-8") == "live"


# ----------------------------------------------------------------- open

def test_open_windows_uses_startfile(tmp_path, monkeypatch):
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules")
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p),
                        raising=False)
    res = s.mgr.local_open(0, "CLAUDE.md")
    assert res["ok"] is True
    assert calls == [str(tmp_path / "r" / "CLAUDE.md")]


def test_open_posix_branches_use_open_and_xdg_open(tmp_path, monkeypatch):
    import subprocess
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")])
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules")
    for plat, argv0 in (("darwin", "open"), ("linux", "xdg-open")):
        seen = []

        def fake_run(cmd, check=True):
            seen.append(cmd)

        monkeypatch.setattr(sys, "platform", plat)
        monkeypatch.setattr(subprocess, "run", fake_run)
        res = s.mgr.local_open(0, "CLAUDE.md")
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


# ------------------------------------------------------- REST layer

def test_local_routes_via_rest(tmp_path):
    from internal.web.api import aiconfig as api
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules\n")
    api.bind(s.mgr)
    try:
        # GET /api/aiconfig/local
        data, status = api.handle("GET", "/api/aiconfig/local", {}, b"")
        assert status == 200
        assert set(data) == {"collected_at", "roots", "entries"}
        assert len(data["entries"]) == 1

        # GET /api/aiconfig/local/item
        data, status = api.handle(
            "GET", "/api/aiconfig/local/item",
            {"root_index": ["0"], "rel_path": ["CLAUDE.md"]}, b"")
        assert status == 200 and data["content"] == "# rules\n"
        data, status = api.handle(
            "GET", "/api/aiconfig/local/item",
            {"root_index": ["0"], "rel_path": ["nope.md"]}, b"")
        assert status == 404 and data["error"] == "not_found"
        data, status = api.handle(
            "GET", "/api/aiconfig/local/item",
            {"root_index": ["0"], "rel_path": ["../escape.md"]}, b"")
        assert status == 400 and data["error"] == "unsafe_path"

        # POST /api/aiconfig/local/save
        data, status = api.handle(
            "POST", "/api/aiconfig/local/save", {},
            json.dumps({"root_index": 0, "rel_path": "CLAUDE.md",
                        "content": "# saved\n"}).encode())
        assert status == 200 and data["ok"] is True
        assert (tmp_path / "r" / "CLAUDE.md").read_text(encoding="utf-8") \
            == "# saved\n"

        # POST /api/aiconfig/local/trash
        data, status = api.handle(
            "POST", "/api/aiconfig/local/trash", {},
            json.dumps({"root_index": 0, "rel_path": "CLAUDE.md"}).encode())
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
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules")
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p),
                        raising=False)
    api.bind(s.mgr)
    try:
        data, status = api.handle(
            "POST", "/api/aiconfig/open", {},
            json.dumps({"root_index": 0, "rel_path": "CLAUDE.md"}).encode())
        assert status == 200 and data["ok"] is True
        assert calls == [str(tmp_path / "r" / "CLAUDE.md")]
        data, status = api.handle(
            "POST", "/api/aiconfig/open", {},
            json.dumps({"root_index": 0, "rel_path": "missing.md"}).encode())
        assert status == 400 and data["error"] == "not_found"
        data, status = api.handle(
            "POST", "/api/aiconfig/open", {},
            json.dumps({"root_index": 0, "rel_path": "../x.md"}).encode())
        assert status == 400 and data["error"] == "unsafe_path"
    finally:
        api.bind(None)


def test_paths_add_remove_reuse_round12_endpoints(tmp_path):
    from internal.web.api import aiconfig as api
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "CLAUDE.md", b"# rules")
    api.bind(s.mgr)
    try:
        data, status = api.handle("GET", "/api/aiconfig/paths", {}, b"")
        assert status == 200 and data["ok"] is True
        data, status = api.handle(
            "POST", "/api/aiconfig/paths", {},
            json.dumps({"paths": [str(tmp_path / "r"), "~/new"]}).encode())
        assert status == 200
        assert data["paths"] == [str(tmp_path / "r"), "~/new"]
        # remove one path via the same endpoint
        data, status = api.handle(
            "POST", "/api/aiconfig/paths", {},
            json.dumps({"paths": ["~/new"]}).encode())
        assert status == 200 and data["paths"] == ["~/new"]
        assert s.cfg.ai_config_paths == ["~/new"]
        # the changed watch list recollected + broadcast (round-12 behavior)
        assert s.sent  # at least one inventory broadcast frame went out
    finally:
        api.bind(None)
