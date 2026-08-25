"""Round 19 — AI config presets + single-file watch roots."""

import types

from internal.config.config import Config
from internal.sync.ai_config import AIConfigManager, collect_roots
from tests.test_round18_localconfig import StubMgr, _mk  # noqa: F401


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
