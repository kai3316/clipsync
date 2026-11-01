"""Two landings of one file must not write into each other.

The failure this holds shut: overwrite mode wrote ``<name>.clipsync.tmp`` — one
fixed name shared by every writer of that file — and then ``os.replace``d it, so
two responses landing at once could replace the target with each other's
half-written temp.  Copy mode was a check followed by a write
(``if not target.exists(): ... write``), which is true only until a second
landing slips between the two, and its candidate names ("-2", "-3", …) were the
same kind of guess.  Neither needs a crash to go wrong: two peers, or one peer
answering a batch twice, is enough.
"""

from __future__ import annotations

import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from internal.sync.ai_config import AIConfigManager


class Rig:
    """A manager whose landing target is a fixed directory."""

    def __init__(self, root: Path):
        self.root = root
        cfg = types.SimpleNamespace(
            device_id="selfid",
            device_name="SelfDev",
            ai_config_tools=[],
            ai_config_custom_paths=[str(root)],
            data_dir="",
            peers={},
        )
        self.mgr = AIConfigManager(
            cfg,
            send_fn=lambda peer_id, frame: True,
            connected_fn=lambda: [],
            event_fn=None,
            save_fn=None,
        )
        # Resolution is the part that is not under test here; the write is.
        self.mgr._land_target = lambda tool, rel, root_id=None: (root / rel, None)
        self.mgr._peer_display_name = lambda peer_id: "Laptop"

    def land(self, rel: str, data: bytes, mode: str) -> tuple[str, str | None]:
        return self.mgr._land_file("custom", rel, data, mode, "p1")


@pytest.fixture
def rig(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    return Rig(root)


def _land_all(rig, payloads, mode):
    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(lambda data: rig.land("CLAUDE.md", data, mode), payloads))


def test_concurrent_overwrites_leave_one_complete_file(rig):
    payloads = [f"# revision {index}\n".encode() * 200 for index in range(8)]
    results = _land_all(rig, payloads, "overwrite")

    assert all(status == "saved" for status, _reason in results)
    target = rig.root / "CLAUDE.md"
    landed = target.read_bytes()
    # Whatever won, it is one writer's whole payload — not an interleaving of
    # two, and not a truncated file.
    assert landed in payloads
    # No temp file survived any of them.
    assert [path.name for path in rig.root.iterdir() if "clipsync.tmp" in path.name] == []


def test_concurrent_copies_never_collide_on_a_name(rig):
    (rig.root / "CLAUDE.md").write_bytes(b"# the local file, which stays")
    payloads = [f"# copy {index}\n".encode() * 50 for index in range(6)]
    results = _land_all(rig, payloads, "copy")

    assert all(status == "copied" for status, _reason in results)
    on_disk = {
        path.read_bytes() for path in rig.root.iterdir() if path.name != "CLAUDE.md"
    }
    # Every copy landed under its own name: nothing was overwritten and no
    # payload was lost to a race.
    assert on_disk == set(payloads)
    assert (rig.root / "CLAUDE.md").read_bytes() == b"# the local file, which stays"
    assert len(list(rig.root.iterdir())) == len(payloads) + 1


def test_a_second_copy_of_the_same_content_gets_its_own_name(rig):
    """Same bytes twice is still two files: the second must not be dropped."""
    data = b"# same content\n"
    first = rig.land("NOTES.md", data, "copy")
    second = rig.land("NOTES.md", data, "copy")

    assert first[0] == "copied" and second[0] == "copied"
    # The first landing created the real name; the second found it taken.
    names = sorted(path.name for path in rig.root.iterdir())
    assert names == ["NOTES.from.Laptop.md", "NOTES.md"]


def test_a_preview_and_a_pull_for_the_same_file_do_not_replace_each_other(tmp_path):
    """One pending record per (peer, tool, root, rel) — the wire cannot tell
    a preview response from a pull response, so only one action may be waiting."""
    root = tmp_path / "root"
    root.mkdir()
    rig = Rig(root)
    rig.mgr._connected_peers = lambda: ["p1"]
    rig.mgr._is_paired = lambda peer_id: True
    rig.mgr._peer_legacy = lambda peer_id: False
    key = ("p1", "v3", "custom", "", "CLAUDE.md")
    # A live record: the expiry sweep would drop a stale one before the check.
    rig.mgr._pending[key] = {
        "mode": "copy", "ts": time.time(), "batch_id": "b1", "root": "",
    }

    result = rig.mgr.preview("p1", "custom", "CLAUDE.md")

    assert result["ok"] is False and result["error"] == "busy"
    # The pull's record is untouched — its mode and batch are how its data
    # gets landed and reported.
    assert rig.mgr._pending[key]["mode"] == "copy"
    assert rig.mgr._pending[key]["batch_id"] == "b1"


def test_a_duplicate_pull_does_not_reset_the_pending_record(rig):
    """The same file requested twice is one request and one landing."""
    rig.mgr._connected_peers = lambda: ["p1"]
    rig.mgr._is_paired = lambda peer_id: True
    rig.mgr._peer_legacy = lambda peer_id: False
    sent = []
    rig.mgr._send_frame = lambda frame, peer_id: sent.append(frame) or True

    first = rig.mgr.pull("p1", [{"tool": "custom", "rel_path": "CLAUDE.md"}], mode="copy")
    second = rig.mgr.pull("p1", [{"tool": "custom", "rel_path": "CLAUDE.md"}], mode="overwrite")

    assert first["requested"] == 1
    assert second["requested"] == 0, "the second request would land the same file twice"
    assert len(sent) == 1
    key = ("p1", "v3", "custom", "", "CLAUDE.md")
    assert rig.mgr._pending[key]["mode"] == "copy", (
        "the in-flight pull must keep the mode it was sent with"
    )
