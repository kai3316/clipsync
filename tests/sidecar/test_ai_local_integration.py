"""Real AI file operations through the sidecar dispatcher, with isolated roots."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.adapters.sidecar.rpc import Dispatcher
from internal.application.bootstrap import SidecarApplication
from internal.config.config import Config, save
from internal.sync.ai_config import AIConfigManager


@pytest.fixture
def ai_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path / "data"))
    root = tmp_path / "watched"
    root.mkdir()
    path = root / "notes.md"
    path.write_text("original content", encoding="utf-8")
    save(Config(encryption_enabled=False, ai_config_tools=[],
                ai_config_custom_paths=[str(root)]))
    app = SidecarApplication()
    app.lifecycle.start()
    manager = AIConfigManager(app.config, lambda *_: False)
    app.runtime = SimpleNamespace(ai_config=manager, stop=lambda: True)
    try:
        yield Dispatcher(app), path
    finally:
        assert app.lifecycle.stop()


def test_local_rpc_round_trip_and_recoverable_trash(ai_files):
    rpc, path = ai_files
    listing = rpc.call("ai.local.listing", {})
    row = next(item for item in listing["entries"] if item["rel_path"] == path.name)
    params = {key: row[key] for key in ("tool", "root", "rel_path")}
    assert rpc.call("ai.local.read", params)["content"] == "original content"
    assert rpc.call("ai.local.save", {**params, "content": "updated"})["ok"]
    assert path.read_text(encoding="utf-8") == "updated"
    assert path.with_name(path.name + ".bak").read_text(encoding="utf-8") == "original content"
    result = rpc.call("ai.local.trash", params)
    assert result["ok"]
    assert not path.exists()
    assert Path(result["trashed_to"]).read_text(encoding="utf-8") == "updated"


def test_local_rpc_rejects_traversal_without_touching_outside_file(ai_files):
    rpc, path = ai_files
    outside = path.parent.parent / "outside.md"
    outside.write_text("untouched", encoding="utf-8")
    row = rpc.call("ai.local.listing", {})["entries"][0]
    params = {"tool": row["tool"], "root": row["root"], "rel_path": "../outside.md"}
    for action in ("read", "save", "trash"):
        args = {**params, "content": "damaged"} if action == "save" else params
        assert rpc.call(f"ai.local.{action}", args)["ok"] is False
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_local_read_refuses_lossy_decoding(ai_files):
    rpc, path = ai_files
    original = b"config = '\xff'"
    path.write_bytes(original)
    row = rpc.call("ai.local.listing", {})["entries"][0]
    params = {key: row[key] for key in ("tool", "root", "rel_path")}
    assert rpc.call("ai.local.read", params) == {"ok": False, "error": "invalid_encoding"}
    assert path.read_bytes() == original


def test_preview_boundary_does_not_misclassify_valid_utf8(ai_files):
    from internal.sync.ai_config import LOCAL_READ_MAX_BYTES

    rpc, path = ai_files
    original = b"a" * (LOCAL_READ_MAX_BYTES - 1) + "\u4e2d".encode("utf-8")
    path.write_bytes(original)
    row = rpc.call("ai.local.listing", {})["entries"][0]
    params = {key: row[key] for key in ("tool", "root", "rel_path")}
    result = rpc.call("ai.local.read", params)
    assert result["ok"] and result["truncated"]
    assert result["content"] == "a" * (LOCAL_READ_MAX_BYTES - 1)
    assert path.read_bytes() == original
