"""Round 12 — web UI layer for AI-config sync (inventory browse + pull).

Static wiring assertions for the web dashboard's 「配置」 tab:
  1. index.html hosts the aiconfig-panel in BOTH layouts (wide sidebar +
     narrow horizontal tabs) and loads its component script.
  2. tab-navigation registers the tab; api.js wraps the three REST calls
     (inventory / preview / pull) against the agreed contract paths and
     payloads.
  3. store.js holds the inventory + per-file-result state with defensive
     normalization; ws.js folds the WS ``aiconfig_file`` event (known
     status vocabulary only) into it.
  4. The settings panel exposes the ai_config_paths editor wired to the
     standard settings API, staged behind a save button.
  5. Locales: en / zh-CN key sets identical and every round-12 key present
     and non-empty in both.
  6. A node --check pass over every JS file this round touched (when node
     is available).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")


def _read(*parts) -> str:
    with open(os.path.join(_STATIC, *parts), encoding="utf-8") as f:
        return f.read()


# ── 1. index.html: panel mounted in both layouts + script tag ──────────


def test_panel_mounted_in_both_layouts():
    html = _read("index.html")
    wide = '<aiconfig-panel v-else-if="store.activeTab === \'aiconfig\'" key="aiconfig"></aiconfig-panel>'
    assert html.count(wide) == 2, (
        "aiconfig-panel must be wired into the wide AND narrow layouts"
    )
    # It must sit inside the same v-else-if chain as its sibling panels.
    for chunk in html.split(wide):
        pass  # existence + count is the assertion; chain order checked below


def test_panel_chain_order_with_siblings():
    html = _read("index.html")
    fav = "<favorites-panel"
    ai = "<aiconfig-panel"
    diag = "<diagnostics-panel"
    assert html.index(fav) < html.index(ai) < html.index(diag)


def test_panel_script_tag_loaded():
    html = _read("index.html")
    assert 'src="components/aiconfig-panel.js?token=__TOKEN__"' in html
    # Must load before app.js, which registers every component at mount.
    assert html.index("components/aiconfig-panel.js") < html.index("js/app.js")


def test_panel_css_present():
    css = _read("index.html")
    for cls in (
        ".aiconfig-panel {",
        ".aiconfig-panel__peers",
        ".aiconfig-panel__table",
        ".aiconfig-panel__actions",
        ".aiconfig-preview__overlay",
        ".aiconfig-preview__content",
    ):
        assert cls in css, f"missing CSS rule: {cls}"


# ── 2. Tab registration + API wrappers ─────────────────────────────────


def test_tab_navigation_registers_aiconfig():
    src = _read("components", "tab-navigation.js")
    assert "id: 'aiconfig'" in src
    assert "t('ui.aiconfig')" in src
    # Badge = number of LOCAL config files (round 18: the AI Config tab is
    # the local manager only; device inventories live in the Devices tab).
    assert "aiConfigLocal" in src


def test_api_inventory_wrapper():
    src = _read("js", "api.js")
    assert "getAiConfigInventory" in src
    assert "'/api/aiconfig/inventory'" in src
    assert "'?refresh=1'" in src


def test_api_preview_wrapper_contract():
    src = _read("js", "api.js")
    assert "previewAiConfigFile" in src
    assert "'/api/aiconfig/preview'" in src
    # POST body carries exactly the contract fields.
    body = src.split("previewAiConfigFile")[1]
    for field in ("peer_id", "root_index", "rel_path"):
        assert field in body, field
    # Tolerant parsing: raw-text bodies must not break the modal.
    assert "JSON.parse(bodyText)" in src
    assert "65536" in src  # 64KB truncation heuristic


def test_api_pull_wrapper_contract():
    src = _read("js", "api.js")
    assert "pullAiConfigFiles" in src
    assert "'/api/aiconfig/pull'" in src
    body = src.split("pullAiConfigFiles")[1]
    for field in ("peer_id", "items", "mode"):
        assert field in body, field


def test_api_paths_wrappers_use_dedicated_endpoint():
    # The watch list intentionally bypasses POST /api/settings (its whitelist
    # does not carry ai_config_paths) — edits go through /api/aiconfig/paths.
    src = _read("js", "api.js")
    assert "getAiConfigPaths" in src
    assert "setAiConfigPaths" in src
    assert src.count("'/api/aiconfig/paths'") == 2  # GET + POST
    body = src.split("setAiConfigPaths")[1]
    assert "paths: paths" in body


def test_api_preview_surfaces_backend_error_reasons():
    src = _read("js", "api.js")
    body = src.split("previewAiConfigFile")[1]
    # Error bodies are JSON {ok:false,error:...} — the reason must reach the
    # caller instead of a bare "HTTP 400".
    assert "parsed.error" in body


# ── 3. Store state + WS event wiring ───────────────────────────────────


def test_store_declares_aiconfig_state():
    src = _read("js", "store.js")
    assert "aiConfigInventory:" in src
    assert "aiConfigLoaded:" in src
    assert "aiConfigLoadFailed:" in src
    assert "aiConfigResults:" in src
    assert "fetchAiConfigInventory:" in src
    assert "applyAiConfigFileResult:" in src


def test_store_normalizes_inventory_defensively():
    src = _read("js", "store.js")
    chunk = src.split("fetchAiConfigInventory: function")[1].split("applyAiConfigFileResult")[0]
    # Missing/malformed peers map degrades to an empty object, entries to [].
    assert "typeof res.peers === 'object'" in chunk
    assert "Array.isArray(p.entries)" in chunk
    # The wire keys each entry's path as `path`; the store normalizes it to
    # the spec'd `rel_path` so every consumer relies on one name.
    assert "e.rel_path || e.path" in chunk
    # fetched_at (epoch float) becomes the camelCase store field.
    assert "fetchedAt" in chunk and "fetched_at" in chunk
    # Failure must still settle the load flag so the panel shows its empty
    # state instead of spinning forever.
    assert "aiConfigLoaded = true" in chunk


def test_ws_handles_aiconfig_file_event():
    src = _read("js", "ws.js")
    assert "case 'aiconfig_file'" in src
    chunk = src.split("case 'aiconfig_file'")[1].split("break;")[0]
    # Only the known status vocabulary reaches the store.
    for status in ("saved", "copied", "appended", "error"):
        assert f"'{status}'" in chunk, status
    assert "store.applyAiConfigFileResult" in chunk
    # Malformed payloads without a path are dropped before the store call.
    assert "data.rel_path" in chunk


# ── 4. Panel component + settings section wiring ────────────────────────


def test_panel_component_structure():
    # Round 18: the device browse/pull UI relocated from the AI Config tab
    # to the Devices tab, so the device assertions live in the relocated
    # aiconfig-device-panel component.
    src = _read("components", "aiconfig-device-panel.js")
    assert "__CLIPSYNC_COMPONENTS__['aiconfig-device-panel']" in src
    # Registered under its own name and injected with the shared store.
    assert "inject: ['store']" in src
    # Default landing mode must be copy — never a silent overwrite.
    assert "mode: 'copy'" in src
    # All three modes offered.
    for mode in ("overwrite", "copy", "append"):
        assert f"value: '{mode}'" in src
    # Pull button disabled at zero selection; preview opens per file.
    assert "selectedCount === 0 || pulling" in src
    assert "openPreview" in src
    # Append mode guards non-text selections client-side (the backend only
    # lands .txt/.md/.markdown in append mode).
    assert "append_ext_warning" in src
    assert "TEXT_EXT_RE" in src
    # Pull failures surface the backend's reason list (peer_offline, ...).
    assert "data.errors" in src
    # Empty state uses the agreed copy keys.
    assert "aiconfig.empty_title" in src
    assert "aiconfig.empty_desc" in src


def test_settings_panel_ai_config_section_wiring():
    src = _read("components", "settings-panel.js")
    # New settings nav entry + search index entry.
    assert "mk('aiconfig'" in src
    assert "aiconfig: [" in src
    # The watch list loads from and saves to its dedicated endpoints —
    # POST /api/settings does NOT carry ai_config_paths (whitelist).
    assert "getAiConfigPaths" in src
    assert "setAiConfigPaths(paths)" in src
    assert "loadAiConfigPaths" in src
    # Loaded on open, mirroring logs/certs section loading.
    assert src.count("=== 'aiconfig') this.loadAiConfigPaths()") == 2
    # Dirty tracking so closing with unsaved edits prompts.
    assert "this.markDirty('aiconfig')" in src
    assert "dirtySections['aiconfig'] = false" in src
    # Row add/remove + save path.
    assert "addAiConfigPath" in src
    assert "removeAiConfigPath" in src
    assert "saveAiConfigPaths" in src
    # Broadcast hint shown next to the save button.
    assert "settings_window.aiconfig_save_hint" in src


def test_settings_section_template_registered():
    src = _read("components", "settings-panel.js")
    assert "activeSection === 'aiconfig'" in src.replace("\\'", "'")


# ── 5. Locale parity ────────────────────────────────────────────────────

_NEW_KEYS = [
    "ui.aiconfig",
    "settings_nav.aiconfig",
    "aiconfig.empty_title",
    "aiconfig.empty_desc",
    "aiconfig.load_failed",
    "aiconfig.devices",
    "aiconfig.files_count",
    "aiconfig.fetched_at",
    "aiconfig.search_placeholder",
    "aiconfig.select_all",
    "aiconfig.col_file",
    "aiconfig.col_size",
    "aiconfig.col_time",
    "aiconfig.no_match",
    "aiconfig.root_label",
    "aiconfig.mode_label",
    "aiconfig.mode_overwrite",
    "aiconfig.mode_overwrite_hint",
    "aiconfig.mode_copy",
    "aiconfig.mode_copy_hint",
    "aiconfig.mode_append",
    "aiconfig.mode_append_hint",
    "aiconfig.pull",
    "aiconfig.pull_requested",
    "aiconfig.pull_failed",
    "aiconfig.append_ext_warning",
    "aiconfig.preview_title",
    "aiconfig.preview_truncated",
    "aiconfig.preview_empty",
    "aiconfig.preview_failed",
    "aiconfig.result_saved",
    "aiconfig.result_copied",
    "aiconfig.result_appended",
    "aiconfig.result_error",
    "settings_window.aiconfig_desc",
    "settings_window.aiconfig_paths_label",
    "settings_window.aiconfig_paths_hint",
    "settings_window.aiconfig_path_placeholder",
    "settings_window.aiconfig_add_path",
    "settings_window.save_aiconfig",
    "settings_window.aiconfig_save_hint",
    "settings.aiconfig_saved",
    "settings.save_aiconfig_failed",
]


def _locales():
    with open(os.path.join(_STATIC, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def test_locale_key_sets_identical():
    en, zh = _locales()
    en_only = set(en) - set(zh)
    zh_only = set(zh) - set(en)
    assert not en_only, f"keys missing from zh-CN.json: {sorted(en_only)}"
    assert not zh_only, f"keys missing from en.json: {sorted(zh_only)}"


def test_new_round12_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
    for key in _NEW_KEYS:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key
    # Placeholders used by the panel code must exist in both translations.
    for key in ("aiconfig.files_count", "aiconfig.fetched_at", "aiconfig.pull",
                "aiconfig.pull_requested", "aiconfig.result_saved",
                "aiconfig.result_copied", "aiconfig.result_appended",
                "aiconfig.result_error"):
        assert "{count}" in en[key] or "{time}" in en[key] or "{path}" in en[key], key
        assert "{count}" in zh[key] or "{time}" in zh[key] or "{path}" in zh[key], key


def test_locale_json_files_still_parse():
    en, zh = _locales()
    assert len(en) > 1000 and len(zh) > 1000


# ── 6. JS syntax (node --check over every file this round touched) ──────


_TOUCHED_JS = [
    ("components", "aiconfig-panel.js"),
    ("components", "aiconfig-device-panel.js"),
    ("components", "device-panel.js"),
    ("components", "tab-navigation.js"),
    ("components", "settings-panel.js"),
    ("js", "api.js"),
    ("js", "store.js"),
    ("js", "ws.js"),
]


def test_touched_js_passes_node_check(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        proc = subprocess.run(
            [node, "--check", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.returncode == 0, (
            f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"
        )


def test_no_nul_bytes_in_touched_js():
    # A stray NUL byte inside a template string survives some editors but is
    # a landmine for others — keep the shipped sources clean.
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        with open(path, "rb") as f:
            data = f.read()
        assert b"\x00" not in data, f"NUL byte found in {os.path.join(*parts)}"
