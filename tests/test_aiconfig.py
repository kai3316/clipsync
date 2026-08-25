"""Round 12 — AI-config sync (codec, config key, collector, manager, REST).

Handlers are exercised through lightweight stubs (make_mgr / bind pattern,
mirroring tests/test_round11_integration.py): no app/tkinter/transport stack
boots here, and every frame crosses the wire through the real codec.
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
from internal.sync.ai_config import (
    MAX_ENTRIES,
    AIConfigManager,
    collect_roots,
    expand_root,
    is_temp_name,
    resolve_safe,
)

# ------------------------------------------------------------------ codec

def test_aiconfig_frames_roundtrip():
    payloads = {
        "aiconfig_inv": {"device_name": "DevA",
                         "entries": [{"path": "CLAUDE.md", "root_index": 0,
                                      "sha256": "ab" * 8, "size": 12,
                                      "mtime": 123.5}]},
        "aiconfig_req": {"root_index": 1, "rel_path": "skills/x/SKILL.md"},
        "aiconfig_data": {"root_index": 1, "rel_path": "skills/x/SKILL.md",
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


def test_config_ai_config_paths_roundtrip(isolated_config):
    cfg_mod = isolated_config
    cfg = cfg_mod.Config()
    cfg.ai_config_paths = ["~/ai-configs", "D:\\notes"]
    cfg_mod.save(cfg)
    loaded = cfg_mod.load()
    assert loaded.ai_config_paths == ["~/ai-configs", "D:\\notes"]
    # round 19: fresh default is the common AI-tool config presets
    fresh = cfg_mod.Config()
    assert len(fresh.ai_config_paths) >= 4
    assert ".claude" in " ".join(fresh.ai_config_paths)


def test_config_ai_config_paths_bad_type_falls_back(isolated_config):
    cfg_mod = isolated_config
    path = cfg_mod._config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ai_config_paths": "not-a-list"}),
                    encoding="utf-8")
    loaded = cfg_mod.load()
    # bad type → the (non-empty) preset default, not an empty list
    assert len(loaded.ai_config_paths) >= 4


# --------------------------------------------------------------- collector

def _mk_round12(root, rel, content=b"data"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_collect_expands_home_and_relative_posix_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _mk_round12(home / "airoot", "CLAUDE.md", b"# rules\n")
    _mk_round12(home / "airoot", "skills/deep/SKILL.md", b"skill")
    entries = collect_roots(["~/airoot"], home=home)
    assert len(entries) == 2
    by_path = {e["path"]: e for e in entries}
    assert "skills/deep/SKILL.md" in by_path  # forward slashes, relative
    ent = by_path["CLAUDE.md"]
    assert ent["root_index"] == 0
    assert ent["sha256"] == hashlib.sha256(b"# rules\n").hexdigest()[:16]
    assert ent["size"] == len(b"# rules\n")
    st = (home / "airoot" / "CLAUDE.md").stat()
    assert abs(ent["mtime"] - st.st_mtime) < 1.0


def test_collect_filters_oversize_temp_and_symlinks(tmp_path):
    home = tmp_path / "home"
    root = home / "r"
    _mk_round12(root, "keep.md", b"ok")
    _mk_round12(root, "big.bin", b"x" * 11)
    _mk_round12(root, "junk.tmp", b"tmp")
    _mk_round12(root, "~$doc.md", b"office")
    _mk_round12(root, ".DS_Store", b"junk")
    _mk_round12(root, "Thumbs.db", b"junk")
    _mk_round12(root, ".claude/settings.json", b"{}")  # dotfiles are KEPT
    link = root / "link.md"
    try:
        os.symlink(root / "keep.md", link)
    except OSError:
        link = None  # Windows without symlink privilege
    entries = collect_roots(["~/r"], home=home, max_bytes=10)
    paths = {e["path"] for e in entries}
    assert paths == {"keep.md", ".claude/settings.json"}
    if link is not None:
        assert "link.md" not in paths


def test_collect_caps_entries_and_skips_missing_or_duplicate_roots(tmp_path):
    root = tmp_path / "cap"
    for i in range(8):
        _mk_round12(root, f"f{i}.md", b"x")
    entries = collect_roots([str(root), str(tmp_path / "missing"), str(root)],
                            max_entries=3)
    assert len(entries) == 3  # capped; missing root skipped; dup root once


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

class _StubMgrRound12:
    """Manager wired to recording stubs."""

    def __init__(self, tmp_path, roots, peers=("p1",)):
        self.sent = []          # (peer_id, frame_bytes)
        self.events = []
        self.saved = []
        cfg = types.SimpleNamespace(
            device_id="selfid",
            device_name="SelfDev",
            ai_config_paths=list(roots),
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


def _feed(receiver: _StubMgrRound12, sender_stub: _StubMgrRound12, payload_extra: dict,
          msg_type: str, peer_id="p1"):
    """Encode a frame on 'the wire' and hand it to the receiver."""
    raw = encode_frame({"msg_type": msg_type, **payload_extra})
    msg = decode_message(raw)
    receiver.mgr.handle_message(msg.msg_type, msg._raw_payload, peer_id)


def test_inv_roundtrip_stores_by_peer(tmp_path):
    a = _StubMgrRound12(tmp_path, roots=[str(tmp_path / "a")])
    _mk_round12(tmp_path / "a", "CLAUDE.md", b"# mine")
    b = _StubMgrRound12(tmp_path, roots=[])
    a.mgr.collect()
    assert a.mgr.send_inventory_to("p1")
    assert len(a.sent) == 1
    _feed(b, a, {"device_name": "SelfDev", "entries":
                 decode_message(a.sent[0][1])._raw_payload["entries"]},
          "aiconfig_inv")
    inv = b.mgr.get_peer_inventories()
    assert set(inv.keys()) == {"p1"}
    assert inv["p1"]["name"] == "SelfDev"
    assert inv["p1"]["entries"][0]["path"] == "CLAUDE.md"
    assert inv["p1"]["fetched_at"] > 0


def test_inv_garbage_entries_sanitized_and_capped(tmp_path):
    b = _StubMgrRound12(tmp_path, roots=[])
    bad_entries = [
        "not-a-dict",
        {"path": "../evil", "root_index": 0, "sha256": "ab" * 8, "size": 1,
         "mtime": 1.0},                       # traversal path dropped
        {"path": "ok.md", "root_index": True, "sha256": "ab" * 8, "size": 1,
         "mtime": 1.0},                       # bool index dropped
        {"path": "bad-hash.md", "root_index": 0, "sha256": "zz", "size": 1,
         "mtime": 1.0},
        {"path": "neg.md", "root_index": 0, "sha256": "ab" * 8, "size": -5,
         "mtime": 1.0},
        {"path": "good.md", "root_index": 3, "sha256": "ab" * 8, "size": 9,
         "mtime": 7},                         # the one valid entry
    ]
    _feed(b, None, {"device_name": "X", "entries": bad_entries}, "aiconfig_inv")
    entries = b.mgr.get_peer_inventories()["p1"]["entries"]
    assert [e["path"] for e in entries] == ["good.md"]
    _feed(b, None, {"device_name": "X", "entries": [
        {"path": f"f{i}.md", "root_index": i % 50, "sha256": "ab" * 8,
         "size": i, "mtime": i} for i in range(MAX_ENTRIES + 50)] +
        ["tail-junk"]},
        "aiconfig_inv")
    stored = b.mgr.get_peer_inventories()["p1"]["entries"]
    assert len(stored) == MAX_ENTRIES
    _feed(b, None, {"device_name": 5, "entries": "nope"}, "aiconfig_inv")


def test_unpaired_sender_rejected_for_all_three_types(tmp_path):
    b = _StubMgrRound12(tmp_path, roots=[str(tmp_path / "r")])
    b.cfg.peers["ghost"] = types.SimpleNamespace(
        device_id="ghost", paired=False, device_name="Ghost")
    b.mgr.collect()
    _feed(b, None, {"device_name": "G", "entries": []}, "aiconfig_inv",
          peer_id="ghost")
    assert b.mgr.get_peer_inventories() == {}
    _feed(b, None, {"root_index": 0, "rel_path": "x.md"}, "aiconfig_req",
          peer_id="ghost")
    _feed(b, None, {"root_index": 0, "rel_path": "x.md",
                    "sha256": "ab" * 8, "b64_content": "aGk="},
          "aiconfig_data", peer_id="ghost")
    assert b.sent == [] and b.events == []


def _serving_setup(tmp_path):
    """Server side: collected inventory with one known file."""
    s = _StubMgrRound12(tmp_path, roots=[str(tmp_path / "srv-root")])
    _mk_round12(tmp_path / "srv-root", "CLAUDE.md", b"# hello")
    _mk_round12(tmp_path / "srv-root", "notes.md", b"line1\n")
    s.mgr.collect()
    return s


def test_req_serves_verified_data(tmp_path):
    s = _serving_setup(tmp_path)
    _feed(s, None, {"root_index": 0, "rel_path": "CLAUDE.md"}, "aiconfig_req")
    assert len(s.sent) == 1
    pid, frame = s.sent[0]
    assert pid == "p1"
    payload = decode_message(frame)._raw_payload
    content = base64.b64decode(payload["b64_content"])
    assert content == b"# hello"
    assert payload["sha256"] == hashlib.sha256(content).hexdigest()[:16]
    assert payload["truncated"] is False


def test_req_hash_drift_and_unadvertised_path_refused(tmp_path):
    s = _serving_setup(tmp_path)
    (tmp_path / "srv-root" / "CLAUDE.md").write_bytes(b"# CHANGED")
    _feed(s, None, {"root_index": 0, "rel_path": "CLAUDE.md"}, "aiconfig_req")
    assert s.sent == []  # stale inventory -> refuse until recollect
    # File exists on disk but was never advertised:
    _mk_round12(tmp_path / "srv-root", "secret.env", b"token=1")
    _feed(s, None, {"root_index": 0, "rel_path": "secret.env"}, "aiconfig_req")
    assert s.sent == []


def test_req_traversal_attacks_rejected(tmp_path):
    s = _serving_setup(tmp_path)
    _mk_round12(tmp_path, "outside.md", b"top secret")  # outside srv-root
    for evil in ("../outside.md", "..\\outside.md", "..\\..\\outside.md",
                 "sub/../../outside.md",
                 "C:/Windows/win.ini", "/etc/passwd"):
        _feed(s, None, {"root_index": 0, "rel_path": evil}, "aiconfig_req")
        assert s.sent == [], evil
    for bad_idx in (-1, 5, True, None):
        _feed(s, None, {"root_index": bad_idx, "rel_path": "CLAUDE.md"},
              "aiconfig_req")
        assert s.sent == []


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
    assert len(reply["entries"]) >= 2
    assert not s.mgr.request_inventory("stranger")  # unpaired -> refused


# ------------------------------------------------------------ pull landing

def _pull_and_deliver(receiver: _StubMgrRound12, server: _StubMgrRound12, rel, mode,
                      root_index=0):
    """Full async loop: pull -> req -> serve -> land."""
    res = receiver.mgr.pull("p1", [{"root_index": root_index,
                                    "rel_path": rel}], mode=mode)
    assert res["requested"] == 1, res
    req = decode_message(receiver.sent[-1][1])._raw_payload
    assert req["msg_type"] == "aiconfig_req"
    server.mgr.handle_message(
        "aiconfig_req", req, "p1")
    reply = decode_message(server.sent[-1][1])._raw_payload
    assert reply["msg_type"] == "aiconfig_data"
    before = len(receiver.sent)
    receiver.mgr.handle_message("aiconfig_data", reply, "p1")
    return len(receiver.sent) > before  # True => a reply frame went back


def test_pull_three_modes_landing(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgrRound12(tmp_path, roots=[str(recv_roots)])
    s = _serving_setup(tmp_path)

    # overwrite: explicit user choice replaces the local copy exactly
    _mk_round12(recv_roots, "CLAUDE.md", b"# old")
    assert _pull_and_deliver(r, s, "CLAUDE.md", "overwrite") is False
    assert (recv_roots / "CLAUDE.md").read_bytes() == b"# hello"
    assert [(e["status"], e["rel_path"]) for e in r.events] == \
        [("saved", "CLAUDE.md")]

    # copy: original untouched, sibling named .from.<device>.
    _mk_round12(recv_roots, "notes.md", b"local")
    assert _pull_and_deliver(r, s, "notes.md", "copy") is False
    assert (recv_roots / "notes.md").read_bytes() == b"local"
    copied = recv_roots / "notes.from.Peer-p1.md"
    assert copied.read_bytes() == b"line1\n"
    # collision-safe second copy
    _pull_and_deliver(r, s, "notes.md", "copy")
    assert (recv_roots / "notes.from.Peer-p1-2.md").exists()

    # append: lands at the SAME relative path locally, newline-separated,
    # text extensions only (the pulled block follows the local content).
    _mk_round12(recv_roots, "notes.md", b"local notes")  # overwrite the earlier copy
    assert _pull_and_deliver(r, s, "notes.md", "append") is False
    assert (recv_roots / "notes.md").read_bytes() == b"local notes\nline1\n"
    assert [(e["status"]) for e in r.events][-1] == "appended"


def test_append_rejects_non_text_extensions(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgrRound12(tmp_path, roots=[str(recv_roots)])
    srv_root = tmp_path / "bin-root"
    _mk_round12(srv_root, "blob.png", b"\x89PNG")
    s = _StubMgrRound12(tmp_path, roots=[str(srv_root)])
    s.mgr.collect()
    assert _pull_and_deliver(r, s, "blob.png", "append") is False
    assert r.events[-1]["status"] == "error"
    assert list(recv_roots.iterdir()) == []  # nothing landed


def test_pull_unknown_mode_falls_back_to_copy(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgrRound12(tmp_path, roots=[str(recv_roots)])
    s = _serving_setup(tmp_path)
    _mk_round12(recv_roots, "notes.md", b"local")
    res = r.mgr.pull("p1", [{"root_index": 0, "rel_path": "notes.md"}],
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
    offline = _StubMgrRound12(tmp_path, roots=[], peers=())
    res = offline.mgr.pull("p1", [{"root_index": 0, "rel_path": "x"}],
                           mode="copy")
    assert res == {"requested": 0, "errors": ["peer_not_paired"]}
    ghost = _StubMgrRound12(tmp_path, roots=[], peers=("g",))
    res2 = ghost.mgr.pull(
        "unpaired-peer", [{"root_index": 0, "rel_path": "x"}], mode="copy")
    assert res2["errors"] == ["peer_not_paired"]


def test_data_hash_mismatch_b64_failure_unsolicited_dropped(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgrRound12(tmp_path, roots=[str(recv_roots)])
    good = base64.b64encode(b"payload").decode()

    # register one pending pull, then corrupt the hash
    r.mgr.pull("p1", [{"root_index": 0, "rel_path": "f.md"}], mode="overwrite")
    r.mgr.handle_message("aiconfig_data", {
        "root_index": 0, "rel_path": "f.md", "sha256": "00" * 8,
        "b64_content": good}, "p1")
    assert r.events[-1]["status"] == "error"
    assert list(recv_roots.iterdir()) == []

    # b64 garbage
    r.mgr.pull("p1", [{"root_index": 0, "rel_path": "f.md"}], mode="overwrite")
    r.mgr.handle_message("aiconfig_data", {
        "root_index": 0, "rel_path": "f.md",
        "sha256": hashlib.sha256(b"payload").hexdigest()[:16],
        "b64_content": "!!!not-b64!!!"}, "p1")
    assert r.events[-1]["reason"] == "b64_decode"

    # unsolicited (no matching pending) silently dropped
    n_events = len(r.events)
    r.mgr.handle_message("aiconfig_data", {
        "root_index": 0, "rel_path": "other.md",
        "sha256": hashlib.sha256(b"payload").hexdigest()[:16],
        "b64_content": good}, "p1")
    assert len(r.events) == n_events
    assert list(recv_roots.iterdir()) == []


def test_pending_expiry_prunes_stale_requests(tmp_path):
    r = _StubMgrRound12(tmp_path, roots=[str(tmp_path / "rr")], )
    (tmp_path / "rr").mkdir(exist_ok=True)
    r.mgr.pull("p1", [{"root_index": 0, "rel_path": "slow.md"}],
               mode="copy")
    key = ("p1", 0, "slow.md")
    assert key in r.mgr._pending
    # simulate the clock having moved past the TTL
    old = next(iter(r.mgr._pending.values()))
    old["ts"] = time.time() - 3600
    r.mgr.pull("p1", [{"root_index": 0, "rel_path": "fresh.md"}],
               mode="copy")
    assert key not in r.mgr._pending


# ---------------------------------------------------------------- preview

def test_preview_returns_content_without_touching_disk(tmp_path):
    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgrRound12(tmp_path, roots=[str(recv_roots)])
    s = _serving_setup(tmp_path)
    result_box = {}

    def run():
        result_box["res"] = r.mgr.preview("p1", 0, "CLAUDE.md")

    t = threading.Thread(target=run)
    t.start()
    deadline = time.time() + 2.0
    while not r.sent and time.time() < deadline:
        time.sleep(0.01)  # wait for the preview's aiconfig_req to be sent
    assert r.sent, "preview never sent its request"
    req = decode_message(r.sent[-1][1])._raw_payload
    assert req["msg_type"] == "aiconfig_req"
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
    r = _StubMgrRound12(tmp_path, roots=[], peers=("p1",))
    start = time.monotonic()
    res = r.mgr.preview("p1", 0, "x.md", timeout=0.05)
    assert res == {"ok": False, "error": "timeout"}
    assert time.monotonic() - start < 1.0
    assert r.mgr._pending == {}


# ------------------------------------------------------------- watch list

def test_set_watch_list_normalizes_and_persists(tmp_path):
    s = _StubMgrRound12(tmp_path, roots=["~/old"], )
    res = s.mgr.set_watch_list(
        ["  ~/configs ", "", "~/configs", 42, None, "D:\\notes"])
    assert res["ok"] is True
    assert res["paths"] == ["~/configs", "D:\\notes"]
    assert s.cfg.ai_config_paths == ["~/configs", "D:\\notes"]
    assert s.saved == [1]
    assert s.mgr.set_watch_list("nope")["ok"] is False


# ------------------------------------------------------------- REST layer

def test_api_routes_with_bound_manager(tmp_path):
    from internal.web.api import aiconfig as api
    s = _StubMgrRound12(tmp_path, roots=[str(tmp_path / "r")])
    _mk_round12(tmp_path / "r", "CLAUDE.md")
    api.bind(s.mgr)
    try:
        data, status = api.handle("GET", "/api/aiconfig/inventory",
                                  {}, b"")
        assert status == 200 and data["peers"] == {}
        data, status = api.handle("GET", "/api/aiconfig/inventory",
                                  {"refresh": ["1"], "peer_id": ["p1"]}, b"")
        assert status == 200 and data["refreshed"] == ["p1"]
        data, status = api.handle("GET", "/api/aiconfig/inventory",
                                  {"refresh": ["1"]}, b"")
        assert status == 200 and data["refreshed"] == []  # no cached peers yet

        data, status = api.handle("POST", "/api/aiconfig/pull",
                                  {},
                                  json.dumps({"peer_id": "p1", "mode": "copy",
                                              "items": [
                                                  {"root_index": 0,
                                                   "rel_path": "CLAUDE.md"},
                                                  "junk"]}).encode())
        assert status == 200 and data["requested"] == 1

        body = json.dumps({"paths": ["~/new"]}).encode()
        data, status = api.handle("POST", "/api/aiconfig/paths", {}, body)
        assert status == 200 and data["paths"] == ["~/new"]
        assert s.cfg.ai_config_paths == ["~/new"]
        data, status = api.handle("GET", "/api/aiconfig/paths", {}, b"")
        assert status == 200 and data["paths"] == ["~/new"]

        data, status = api.handle("POST", "/api/aiconfig/pull", {},
                                  json.dumps({"items": []}).encode())
        assert status == 400  # missing peer_id
        data, status = api.handle("GET", "/api/aiconfig/nonsense", {}, b"")
        assert status == 404
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


# ══════════════════════════════════════════════════
# merged from test_round18_localconfig.py
# ══════════════════════════════════════════════════

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.sync.ai_config import (
    LOCAL_SAVE_MAX_BYTES,
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
    # The local file manager shows the folder tree, so "sub/" appears as a
    # folder entry alongside its file.
    assert listing["roots"] == [
        {"root_index": 0, "path": str(tmp_path / "r"), "count": 3}]
    assert len(listing["entries"]) == 3
    by_rel = {e["rel_path"]: e for e in listing["entries"]}
    assert set(by_rel) == {"CLAUDE.md", "sub/notes.md", "sub/"}
    for e in listing["entries"]:
        assert set(e) == {"root_index", "rel_path", "size", "mtime", "sha256", "is_dir"}
        assert e["root_index"] == 0
    # folder entry: no size / no content hash, flagged is_dir
    assert by_rel["sub/"]["is_dir"] is True
    assert by_rel["sub/"]["size"] is None and by_rel["sub/"]["sha256"] is None
    assert by_rel["CLAUDE.md"]["is_dir"] is False
    assert by_rel["CLAUDE.md"]["size"] > 0 and by_rel["CLAUDE.md"]["mtime"] > 0
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


def test_local_trash_folder_moves_whole_directory(tmp_path):
    """Trashing a folder (not just a file) moves the whole directory tree into
    the recoverable trash, preserving its contents and structure."""
    s = StubMgr(tmp_path, roots=[str(tmp_path / "r")], data_dir=tmp_path)
    _mk(tmp_path / "r", "skills/my-skill/SKILL.md", b"skill")
    _mk(tmp_path / "r", "skills/my-skill/sub/extra.md", b"extra")
    res = s.mgr.local_trash(0, "skills/my-skill/")
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


# ══════════════════════════════════════════════════
# merged from test_round19_presets.py
# ══════════════════════════════════════════════════


from internal.config.config import Config

# ------------------------------------------------------------- defaults

def test_default_watch_list_has_the_common_ai_config_paths():
    cfg = Config()
    assert isinstance(cfg.ai_config_paths, list)
    joined = " ".join(cfg.ai_config_paths)
    for needle in (".claude", ".codex", ".cursor", ".gemini"):
        assert needle in joined
    # credentials must NOT be in the default list
    assert "auth.json" not in joined
    assert ".credentials" not in joined


# ------------------------------------------------------- single-file roots

def test_file_root_advertises_itself_as_one_entry(tmp_path):
    f = _mk(tmp_path, "CLAUDE.md", b"# rules\n")
    entries = collect_roots([str(f)])
    assert len(entries) == 1
    assert entries[0]["path"] == "CLAUDE.md"
    assert entries[0]["root_index"] == 0
    assert entries[0]["sha256"]


def test_file_root_read_returns_the_file_itself(tmp_path):
    f = _mk(tmp_path, "settings.json", b'{"k": 1}')
    stub = StubMgr(tmp_path, roots=[str(f)])
    res = stub.mgr.local_read(0, "settings.json")
    assert res["ok"] and res["content"] == '{"k": 1}'
    # a rel that isn't the file's own basename is refused
    bad = stub.mgr.local_read(0, "other.json")
    assert bad["ok"] is False and bad["error"] == "unsafe_path"


def test_file_root_save_writes_and_leaves_bak(tmp_path):
    f = _mk(tmp_path, "CLAUDE.md", b"old\n")
    stub = StubMgr(tmp_path, roots=[str(f)])
    res = stub.mgr.local_save(0, "CLAUDE.md", "new\n")
    assert res["ok"] is True
    assert f.read_text() == "new\n"
    assert (tmp_path / "CLAUDE.md.bak").read_text() == "old\n"


def test_file_root_trash_moves_the_file(tmp_path):
    f = _mk(tmp_path, "GEMINI.md", b"ctx\n")
    stub = StubMgr(tmp_path, roots=[str(f)], data_dir=str(tmp_path / "data"))
    res = stub.mgr.local_trash(0, "GEMINI.md")
    assert res["ok"] is True
    assert not f.exists()
    assert "aiconfig_trash" in res["trashed_to"]


def test_directory_root_still_recurses(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    _mk(d, "a.mdc", b"a")
    _mk(d, "b.mdc", b"bb")
    entries = collect_roots([str(d)])
    assert {e["path"] for e in entries} == {"a.mdc", "b.mdc"}


def test_collector_skips_missing_file_root_silently(tmp_path):
    entries = collect_roots([str(tmp_path / "nope.json")])
    assert entries == []


def test_file_root_serves_pull_to_peer(tmp_path):
    """A single-file watch root must be servable on the peer path too: the
    aiconfig_req handler targets the file itself (no directory walk), so a pull
    lands its content on the receiver."""
    srv = _StubMgrRound12(tmp_path, roots=[str(tmp_path / "CLAUDE.md")])
    _mk_round12(tmp_path, "CLAUDE.md", b"# hello\n")
    srv.mgr.collect()
    assert [e["path"] for e in srv.mgr._local_entries] == ["CLAUDE.md"]

    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    _mk_round12(recv_roots, "CLAUDE.md", b"# old")  # landing needs an existing target
    r = _StubMgrRound12(tmp_path, roots=[str(recv_roots)])

    # pull: full async req -> serve -> land loop against the file-root server
    assert _pull_and_deliver(r, srv, "CLAUDE.md", "overwrite") is False
    assert (recv_roots / "CLAUDE.md").read_bytes() == b"# hello\n"


def test_file_root_preview_returns_content_without_landing(tmp_path):
    """Preview of a single-file root returns the file's content and never
    lands anything on the receiver's disk."""
    srv = _StubMgrRound12(tmp_path, roots=[str(tmp_path / "CLAUDE.md")])
    _mk_round12(tmp_path, "CLAUDE.md", b"# hello\n")
    srv.mgr.collect()

    recv_roots = tmp_path / "recv-root"
    recv_roots.mkdir()
    r = _StubMgrRound12(tmp_path, roots=[str(recv_roots)])
    result_box = {}

    def run():
        result_box["res"] = r.mgr.preview("p1", 0, "CLAUDE.md")

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


# ------------------------------------------------------------- frontend static

def test_preset_button_wired_in_panel_and_locales():
    import json
    from pathlib import Path
    js = Path("internal/web/static/components/aiconfig-panel.js").read_text(encoding="utf-8")
    assert "AICONFIG_PRESETS" in js
    assert ".claude/CLAUDE.md" in js
    assert "addPresetPaths" in js
    assert "aiconfig.local_add_presets" in js
    for lang in ("en", "zh-CN"):
        loc = json.loads(Path(f"internal/web/static/locales/{lang}.json").read_text(encoding="utf-8"))
        assert loc.get("aiconfig.local_add_presets")
        assert loc.get("aiconfig.local_presets_added")

# ══════════════════════════════════════════════════
# split from test_round13_wrapup.py — settings whitelist ai_config_paths
# ══════════════════════════════════════════════════



def test_settings_whitelist_includes_ai_config_paths():
    from internal.web.api import settings as settings_api
    assert "ai_config_paths" in settings_api._SAFE_FIELDS
    assert "ai_config_paths" in settings_api._MUTABLE_FIELDS


class _SettingsCfg:
    """Minimal config stand-in carrying the fields the handlers touch."""

    def __init__(self):
        self.private_key_pem = "KEY"
        self.internet_sync_enabled = False
        self.relay_brokers = []
        self.relay_secret = ""
        self.peer_relay_secrets = {}
        self.ai_config_paths = []


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
def test_ai_config_paths_writable_via_settings_post():
    from internal.web.api.settings import update_settings
    cfg = _SettingsCfg()
    paths = ["~/ai-configs", "~/.claude"]
    data, status = update_settings(_body({"ai_config_paths": paths}), cfg)
    assert status == 200
    assert data["updated"]["ai_config_paths"] == paths
    assert cfg.ai_config_paths == paths

    # A non-list value is rejected without touching the field.
    data, status = update_settings(_body({"ai_config_paths": "~/x"}), cfg)
    assert status == 400
    assert cfg.ai_config_paths == paths


def test_get_settings_exposes_ai_config_paths():
    from internal.web.api.settings import get_settings
    cfg = _SettingsCfg()
    cfg.ai_config_paths = ["~/ai-configs"]
    result, status = get_settings(cfg)
    assert status == 200
    assert result["settings"]["ai_config_paths"] == ["~/ai-configs"]


@pytest.mark.usefixtures("sandboxed_persist")
def test_dedicated_aiconfig_paths_endpoint_unchanged():
    """The dedicated /api/aiconfig/paths editor still functions (dual entry)."""
    from internal.web.api import aiconfig as aiconfig_api

    class _Mgr:
        def __init__(self):
            self._paths = ["~/orig"]

        def local_summary(self):
            return {"collected_at": 0, "entry_count": 0, "paths": list(self._paths)}

        def set_watch_list(self, paths):
            self._paths = list(paths)
            return {"ok": True, "paths": list(paths)}

        def on_watch_list_changed(self):
            return 0

    aiconfig_api.bind(_Mgr())
    try:
        data, status = aiconfig_api.handle(
            "GET", "/api/aiconfig/paths", {}, b"")
        assert status == 200
        assert data["paths"] == ["~/orig"]

        data, status = aiconfig_api.handle(
            "POST", "/api/aiconfig/paths", {},
            _body({"paths": ["~/new"]}))
        assert status == 200
        assert data["paths"] == ["~/new"]
    finally:
        aiconfig_api.bind(None)


