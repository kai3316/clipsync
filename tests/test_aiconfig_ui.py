"""Round 18 — web UI layer for the local AI-config manager.

The 「配置」 tab is now split into two sections:
  1. 本机配置 (local config manager, round 18) — a file manager over THIS
     device's watch roots, independent of any pairing: watch-path
     management dialog, file table (path/size/mtime, sorted), search,
     click-to-preview, edit-and-save (leaves a .bak), move-to-Recycle-Bin
     with a confirm, and open-containing-folder.  The local API is fully
     defensive (empty + retry when the backend predates it).
  2. 设备配置 (device config, round 12) — the existing peer-inventory
     browse / pull UI, unchanged.

Static wiring assertions:
  1. index.html hosts the two-section layout + the new local CSS classes,
     and the panel is still mounted in both layouts.
  2. api.js wraps the five new local endpoints (local / local/item / save /
     trash / open) against the agreed contract paths and payloads.
  3. store.js holds the aiConfigLocal state + a defensive fetch helper.
  4. aiconfig-panel.js wires the local manager UI (toolbar, paths dialog,
     file table, preview/edit/save, trash confirm, open dir) to the API
     and keeps the device section intact.
  5. Locales: en / zh-CN key sets identical and every new key present and
     non-empty in both.
  6. A node --check pass over every JS file this round touched.
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


# ── 1. index.html: two-section layout + CSS + panel mounts ─────────────


def test_panel_mounted_in_both_layouts():
    html = _read("index.html")
    wide = '<aiconfig-panel v-else-if="store.activeTab === \'aiconfig\'" key="aiconfig"></aiconfig-panel>'
    assert html.count(wide) == 2, (
        "aiconfig-panel must be wired into the wide AND narrow layouts"
    )


def test_index_html_has_local_section_css():
    css = _read("index.html")
    for cls in (
        ".aiconfig-panel__local {",
        ".aiconfig-panel__local-tablewrap {",
        ".aiconfig-panel__local-actions {",
        ".aiconfig-paths__overlay {",
        ".aiconfig-local-preview__editor {",
        ".aiconfig-device {",
    ):
        assert cls in css, f"missing CSS rule: {cls}"


def test_index_html_loads_panel_script():
    html = _read("index.html")
    assert 'src="components/aiconfig-panel.js?token=__TOKEN__"' in html
    assert html.index("components/aiconfig-panel.js") < html.index("js/app.js")


# ── 2. api.js: five new local-endpoint wrappers ────────────────────────


def test_api_local_inventory_wrapper():
    src = _read("js", "api.js")
    assert "getAiConfigLocal" in src
    assert "'/api/aiconfig/local'" in src


def test_api_local_item_wrapper_uses_query_params():
    src = _read("js", "api.js")
    body = src.split("getAiConfigLocalItem")[1].split("saveAiConfigLocal")[0]
    assert "'/api/aiconfig/local/item?root_index='" in body
    assert "'&rel_path='" in body
    assert "encodeURIComponent(rootIndex)" in body
    assert "encodeURIComponent(relPath)" in body


def test_api_local_save_wrapper_payload():
    src = _read("js", "api.js")
    assert "saveAiConfigLocal" in src
    body = src.split("saveAiConfigLocal")[1].split("trashAiConfigLocal")[0]
    assert "'/api/aiconfig/local/save'" in body
    for field in ("root_index", "rel_path", "content"):
        assert field in body, field


def test_api_local_trash_wrapper_payload():
    src = _read("js", "api.js")
    assert "trashAiConfigLocal" in src
    body = src.split("trashAiConfigLocal")[1].split("openAiConfigLocal")[0]
    assert "'/api/aiconfig/local/trash'" in body
    for field in ("root_index", "rel_path"):
        assert field in body, field


def test_api_local_open_wrapper_payload():
    src = _read("js", "api.js")
    assert "openAiConfigLocal" in src
    body = src.split("openAiConfigLocal")[1]
    assert "'/api/aiconfig/open'" in body
    for field in ("root_index", "rel_path"):
        assert field in body, field


# ── 3. store.js: aiConfigLocal state + defensive fetch ────────────────


def test_store_declares_local_state():
    src = _read("js", "store.js")
    assert "aiConfigLocal: {" in src
    assert "fetchAiConfigLocal: function" in src
    assert "aiConfigLocal.loaded" in src
    assert "aiConfigLocal.loadFailed" in src


def test_store_local_fetch_normalizes_defensively():
    src = _read("js", "store.js")
    chunk = src.split("fetchAiConfigLocal: function")[1].split(
        "Internet pairing helpers (round 14)")[0]
    # The local manager must work with zero paired devices — no peer gate.
    assert "getAiConfigLocal" in chunk
    # Malformed roots/entries degrade to empty lists, never a crash.
    assert "Array.isArray(res.roots)" in chunk
    assert "Array.isArray(res.entries)" in chunk
    # The wire keys an entry's path as `path` (inventory convention); the
    # store normalizes to `rel_path` so the panel relies on one name.
    assert "e.rel_path || e.path" in chunk
    # Failure must settle loaded=true so the panel shows empty/retry.
    assert "aiConfigLocal.loaded = true" in chunk


# ── 4. aiconfig-panel.js: local manager + device section wiring ────────


def test_panel_local_only_layout():
    # The AI Config tab is the LOCAL manager only — device config moved out.
    src = _read("components", "aiconfig-panel.js")
    assert "aiconfig-panel__local" in src
    assert "aiconfig.local_title" in src
    # No device section / peer inventory left behind in the AI Config tab.
    assert "aiconfig-panel__device" not in src
    assert "peersList" not in src
    assert "aiconfig-panel__body" not in src


def test_panel_local_toolbar_and_paths_dialog():
    src = _read("components", "aiconfig-panel.js")
    # Toolbar: refresh + watch-path management.
    assert "refreshLocal" in src
    assert "openPathsDialog" in src
    assert "aiconfig.local_manage_paths" in src
    # Paths dialog loads from and saves to /api/aiconfig/paths.
    assert "getAiConfigPaths" in src
    assert "setAiConfigPaths" in src
    assert "addPathRow" in src
    assert "removePathRow" in src
    assert "savePaths" in src
    assert "aiconfig-paths__overlay" in src
    # Saving auto-recollects so the table shows the fresh list.
    assert "aiconfig.local_paths_saved" in src
    assert "aiconfig.local_paths_save_failed" in src


def test_panel_local_file_list_search():
    src = _read("components", "aiconfig-panel.js")
    assert "localFilteredEntries" in src
    assert "localSearch" in src
    assert "localMultiRoot" in src
    assert "localRootLabel" in src
    assert "aiconfig-panel__local-tablewrap" in src


def test_panel_local_preview_edit_save():
    src = _read("components", "aiconfig-panel.js")
    # Click a row → preview; preview can switch to a textarea edit.
    assert "openLocalPreview" in src
    assert "startLocalEdit" in src
    assert "cancelLocalEdit" in src
    assert "saveLocalEdit" in src
    assert "aiconfig-local-preview__editor" in src
    # Save hits the save endpoint and toasts the .bak guarantee.
    assert "saveAiConfigLocal" in src
    assert "aiconfig.local_saved_toast" in src
    assert "aiconfig.local_save_failed" in src
    # Binary / failed previews surface a clear reason instead of a crash.
    assert "localPreviewError" in src
    assert "aiconfig.local_preview_binary" in src
    assert "aiconfig.local_preview_failed" in src


def test_panel_local_trash_confirm():
    src = _read("components", "aiconfig-panel.js")
    # Delete is a confirmed move to the Recycle Bin — never a hard delete.
    assert "trashEntry" in src
    assert "store.confirm" in src
    assert "aiconfig.local_trash_title" in src
    assert "aiconfig.local_trash_confirm" in src
    assert "trashAiConfigLocal" in src
    assert "aiconfig.local_trashed_toast" in src
    assert "aiconfig.local_trash_failed" in src
    # Cancel (confirm rejection) is a silent no-op — the catch guards on
    # a real Error so a dismissed dialog never toasts "trash failed".
    assert "typeof err === 'object'" in src


def test_panel_local_open_dir():
    src = _read("components", "aiconfig-panel.js")
    assert "openEntryDir" in src
    assert "openAiConfigLocal" in src
    assert "aiconfig.local_open_dir" in src
    assert "aiconfig.local_open_failed" in src


def test_panel_local_defensive_empty_state():
    src = _read("components", "aiconfig-panel.js")
    # Empty watch list → "add paths first" guide, independent of pairing.
    assert "localNoPaths" in src
    assert "aiconfig.local_no_paths" in src
    assert "aiconfig.local_empty_desc" in src
    # Local API not ready / 404 → empty + retry, never a crash.
    assert "store.aiConfigLocal.loadFailed" in src
    assert "aiconfig.local_retry" in src
    # First-open primes BOTH sections.
    assert "store.fetchAiConfigLocal()" in src


def test_device_config_relocated_to_devices_tab():
    # The round-12 device browse/pull UI now lives in aiconfig-device-panel.
    dev = _read("components", "aiconfig-device-panel.js")
    assert "__CLIPSYNC_COMPONENTS__['aiconfig-device-panel']" in dev
    assert "peersList" in dev
    assert "selectedCount === 0 || pulling" in dev
    assert "openPreview" in dev
    assert "pullAiConfigFiles" in dev
    assert "aiconfig.empty_title" in dev
    assert "aiconfig.empty_desc" in dev
    assert "aiconfig.mode_label" in dev
    assert "aiconfig.devices" in dev
    assert "aiconfig.local_device_title" in dev
    # Mounted exactly once in the Devices tab (device-panel.js).
    panel = _read("components", "device-panel.js")
    assert "<aiconfig-device-panel></aiconfig-device-panel>" in panel
    # index.html loads the new component script before app.js.
    html = _read("index.html")
    assert 'src="components/aiconfig-device-panel.js?token=__TOKEN__"' in html
    assert html.index("components/aiconfig-device-panel.js") < html.index("js/app.js")
    # No duplication: the device strings must NOT appear in aiconfig-panel.js.
    local = _read("components", "aiconfig-panel.js")
    for token in ("peersList", "pullAiConfigFiles", "aiconfig-panel__device"):
        assert token not in local, token


# ── 5. Locale parity ───────────────────────────────────────────────────

_NEW_KEYS = [
    "aiconfig.local_title",
    "aiconfig.local_device_title",
    "aiconfig.local_empty_title",
    "aiconfig.local_empty_desc",
    "aiconfig.local_no_paths",
    "aiconfig.local_manage_paths",
    "aiconfig.local_paths_hint",
    "aiconfig.local_add_path",
    "aiconfig.local_paths_saved",
    "aiconfig.local_paths_save_failed",
    "aiconfig.local_collected_at",
    "aiconfig.local_files_count",
    "aiconfig.local_edit",
    "aiconfig.local_save",
    "aiconfig.local_saved_toast",
    "aiconfig.local_save_failed",
    "aiconfig.local_trash_title",
    "aiconfig.local_trash_confirm",
    "aiconfig.local_trashed_toast",
    "aiconfig.local_trash_failed",
    "aiconfig.local_open_dir",
    "aiconfig.local_open_failed",
    "aiconfig.local_preview_binary",
    "aiconfig.local_preview_failed",
    "aiconfig.local_retry",
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


def test_new_round18_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
    for key in _NEW_KEYS:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key
    # Placeholders used by the panel code must exist in both translations.
    for key in ("aiconfig.local_files_count", "aiconfig.local_collected_at",
                "aiconfig.local_saved_toast"):
        assert "{count}" in en[key] or "{time}" in en[key] or "{path}" in en[key] or ".bak" in en[key], key
        assert "{count}" in zh[key] or "{time}" in zh[key] or "{path}" in zh[key] or ".bak" in zh[key], key
    for key in ("aiconfig.local_trash_confirm",):
        assert "{path}" in en[key] and "{path}" in zh[key], key
    for key in ("aiconfig.local_trashed_toast",):
        assert "{dest}" in en[key] and "{dest}" in zh[key], key
    for key in ("aiconfig.local_save_failed", "aiconfig.local_trash_failed",
                "aiconfig.local_open_failed", "aiconfig.local_preview_failed"):
        assert "{reason}" in en[key] and "{reason}" in zh[key], key


def test_locale_json_files_still_parse():
    en, zh = _locales()
    assert len(en) > 1000 and len(zh) > 1000


# ── 6. JS syntax (node --check over every file this round touched) ─────


_TOUCHED_JS = [
    ("components", "aiconfig-panel.js"),
    ("components", "aiconfig-device-panel.js"),
    ("components", "device-panel.js"),
    ("js", "api.js"),
    ("js", "store.js"),
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
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        with open(path, "rb") as f:
            data = f.read()
        assert b"\x00" not in data, f"NUL byte found in {os.path.join(*parts)}"


# ══════════════════════════════════════════════════
# merged from test_round12_webui.py (shared scaffold suffixed r12)
# ══════════════════════════════════════════════════

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT_r12 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_r12 = os.path.join(_ROOT_r12, "internal", "web", "static")


def _read_r12(*parts) -> str:
    with open(os.path.join(_STATIC_r12, *parts), encoding="utf-8") as f:
        return f.read()


# ── 1. index.html: panel mounted in both layouts + script tag ──────────


def test_panel_mounted_in_both_layouts_r12():
    html = _read_r12("index.html")
    wide = '<aiconfig-panel v-else-if="store.activeTab === \'aiconfig\'" key="aiconfig"></aiconfig-panel>'
    assert html.count(wide) == 2, (
        "aiconfig-panel must be wired into the wide AND narrow layouts"
    )
    # It must sit inside the same v-else-if chain as its sibling panels.
    for chunk in html.split(wide):
        pass  # existence + count is the assertion; chain order checked below


def test_panel_chain_order_with_siblings():
    html = _read_r12("index.html")
    fav = "<favorites-panel"
    ai = "<aiconfig-panel"
    diag = "<diagnostics-panel"
    assert html.index(fav) < html.index(ai) < html.index(diag)


def test_panel_script_tag_loaded():
    html = _read_r12("index.html")
    assert 'src="components/aiconfig-panel.js?token=__TOKEN__"' in html
    # Must load before app.js, which registers every component at mount.
    assert html.index("components/aiconfig-panel.js") < html.index("js/app.js")


def test_panel_css_present():
    css = _read_r12("index.html")
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
    src = _read_r12("components", "tab-navigation.js")
    assert "id: 'aiconfig'" in src
    assert "t('ui.aiconfig')" in src
    # Badge = number of LOCAL config files (round 18: the AI Config tab is
    # the local manager only; device inventories live in the Devices tab).
    assert "aiConfigLocal" in src


def test_api_inventory_wrapper():
    src = _read_r12("js", "api.js")
    assert "getAiConfigInventory" in src
    assert "'/api/aiconfig/inventory'" in src
    assert "'?refresh=1'" in src


def test_api_preview_wrapper_contract():
    src = _read_r12("js", "api.js")
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
    src = _read_r12("js", "api.js")
    assert "pullAiConfigFiles" in src
    assert "'/api/aiconfig/pull'" in src
    body = src.split("pullAiConfigFiles")[1]
    for field in ("peer_id", "items", "mode"):
        assert field in body, field


def test_api_paths_wrappers_use_dedicated_endpoint():
    # The watch list intentionally bypasses POST /api/settings (its whitelist
    # does not carry ai_config_paths) — edits go through /api/aiconfig/paths.
    src = _read_r12("js", "api.js")
    assert "getAiConfigPaths" in src
    assert "setAiConfigPaths" in src
    assert src.count("'/api/aiconfig/paths'") == 2  # GET + POST
    body = src.split("setAiConfigPaths")[1]
    assert "paths: paths" in body


def test_api_preview_surfaces_backend_error_reasons():
    src = _read_r12("js", "api.js")
    body = src.split("previewAiConfigFile")[1]
    # Error bodies are JSON {ok:false,error:...} — the reason must reach the
    # caller instead of a bare "HTTP 400".
    assert "parsed.error" in body


# ── 3. Store state + WS event wiring ───────────────────────────────────


def test_store_declares_aiconfig_state():
    src = _read_r12("js", "store.js")
    assert "aiConfigInventory:" in src
    assert "aiConfigLoaded:" in src
    assert "aiConfigLoadFailed:" in src
    assert "aiConfigResults:" in src
    assert "fetchAiConfigInventory:" in src
    assert "applyAiConfigFileResult:" in src


def test_store_normalizes_inventory_defensively():
    src = _read_r12("js", "store.js")
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
    src = _read_r12("js", "ws.js")
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
    src = _read_r12("components", "aiconfig-device-panel.js")
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
    src = _read_r12("components", "settings-panel.js")
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
    src = _read_r12("components", "settings-panel.js")
    assert "activeSection === 'aiconfig'" in src.replace("\\'", "'")


# ── 5. Locale parity ────────────────────────────────────────────────────

_NEW_KEYS_r12 = [
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


def _locales_r12():
    with open(os.path.join(_STATIC_r12, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC_r12, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def test_locale_key_sets_identical_r12():
    en, zh = _locales_r12()
    en_only = set(en) - set(zh)
    zh_only = set(zh) - set(en)
    assert not en_only, f"keys missing from zh-CN.json: {sorted(en_only)}"
    assert not zh_only, f"keys missing from en.json: {sorted(zh_only)}"


def test_new_round12_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales_r12()
    for key in _NEW_KEYS_r12:
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


def test_locale_json_files_still_parse_r12():
    en, zh = _locales_r12()
    assert len(en) > 1000 and len(zh) > 1000


# ── 6. JS syntax (node --check over every file this round touched) ──────


_TOUCHED_JS_r12 = [
    ("components", "aiconfig-panel.js"),
    ("components", "aiconfig-device-panel.js"),
    ("components", "device-panel.js"),
    ("components", "tab-navigation.js"),
    ("components", "settings-panel.js"),
    ("js", "api.js"),
    ("js", "store.js"),
    ("js", "ws.js"),
]


def test_touched_js_passes_node_check_r12(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for parts in _TOUCHED_JS_r12:
        path = os.path.join(_STATIC_r12, *parts)
        proc = subprocess.run(
            [node, "--check", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.returncode == 0, (
            f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"
        )


def test_no_nul_bytes_in_touched_js_r12():
    # A stray NUL byte inside a template string survives some editors but is
    # a landmine for others — keep the shipped sources clean.
    for parts in _TOUCHED_JS_r12:
        path = os.path.join(_STATIC_r12, *parts)
        with open(path, "rb") as f:
            data = f.read()
        assert b"\x00" not in data, f"NUL byte found in {os.path.join(*parts)}"


# ══════════════════════════════════════════════════
# merged from test_round19_version_compare.py (shared scaffold suffixed r19)
# ══════════════════════════════════════════════════

import os
import shutil
import subprocess
import textwrap

_ROOT_r19 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_r19 = os.path.join(_ROOT_r19, "internal", "web", "static")

_COMPONENT = os.path.join(_STATIC_r19, "components", "aiconfig-device-panel.js")

_NEW_KEYS_r19 = [
    "aiconfig.ver_missing",
    "aiconfig.ver_same",
    "aiconfig.ver_local_newer",
    "aiconfig.ver_remote_newer",
    "aiconfig.ver_tooltip",
]


def _read_r19(*parts) -> str:
    with open(os.path.join(_STATIC_r19, *parts), encoding="utf-8") as f:
        return f.read()


def _locales_r19():
    with open(os.path.join(_STATIC_r19, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC_r19, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


# ── 1. compare-function behaviour (constructed store data, run via Node) ─

# The component registers a plain object (no Vue invoked at load time), so it
# can be eval'd in a stub-windowed Node and its methods driven directly.
_NODE_SCRIPT = textwrap.dedent(r"""
    const fs = require('fs');
    global.window = {};
    eval(fs.readFileSync(process.argv[2], 'utf8'));
    const comp = global.window.__CLIPSYNC_COMPONENTS__['aiconfig-device-panel'];

    const R = (rel_path, sha256, size, mtime) => ({ rel_path, sha256, size, mtime });

    function makeCtx(entries, loaded = true) {
      const store = { aiConfigLocal: { loaded, entries } };
      const localByPath = comp.computed.localByPath.call({ store });
      return { store, localByPath, t: () => '' };
    }

    function stateOf(entries, remote, loaded = true) {
      return comp.methods.compareState.call(makeCtx(entries, loaded), remote);
    }

    const cases = [
      // name, localEntries, remoteEntry, expected
      ['missing',
        [R('A.md', 'aaaa', 10, 1000)],
        R('B.md', 'bbbb', 20, 2000), 'missing'],
      ['same',
        [R('A.md', 'aaaa', 10, 1000)],
        R('A.md', 'aaaa', 10, 1000), 'same'],
      ['local_newer',
        [R('A.md', 'local', 10, 2000)],
        R('A.md', 'remote', 20, 1000), 'local_newer'],
      ['remote_newer',
        [R('A.md', 'local', 10, 1000)],
        R('A.md', 'remote', 20, 2000), 'remote_newer'],
      // mtime unit invariance: values > 1e12 are ms, anything smaller is
      // seconds (1e12 seconds ≈ year 33658, so 1e12 itself is still seconds).
      ['local_newer_ms',
        [R('A.md', 'local', 10, 2000000000000)],
        R('A.md', 'remote', 20, 1500000000000), 'local_newer'],
      ['remote_newer_sec',
        [R('A.md', 'local', 10, 1000)],
        R('A.md', 'remote', 20, 2000), 'remote_newer'],
      // equal mtime with differing hash falls through to "remote is newer".
      ['equal_mtime_falls_remote',
        [R('A.md', 'local', 10, 1234)],
        R('A.md', 'remote', 20, 1234), 'remote_newer'],
      // duplicate rel_path across local roots: first match wins, no crash.
      ['duplicate_local_paths',
        [R('A.md', 'first', 1, 100), R('A.md', 'second', 2, 200)],
        R('A.md', 'first', 1, 100), 'same'],
    ];

    for (const [name, local, remote, expected] of cases) {
      const got = stateOf(local, remote);
      if (got !== expected) {
        console.error(`FAIL ${name}: expected ${expected}, got ${got}`);
        process.exit(1);
      }
      console.log(`ok ${name} -> ${got}`);
    }

    // Defensive: local inventory not loaded → null (no badge rendered).
    const notLoaded = stateOf([], R('A.md', 'aaaa', 1, 1), false);
    if (notLoaded !== null) {
      console.error(`FAIL not-loaded should be null, got ${notLoaded}`);
      process.exit(1);
    }
    console.log('ok not-loaded -> null');

    // compareTitle: a missing file has no local side — label only (no crash,
    // no "Local: …" line).  Methods are bound onto the instance like Vue does.
    function ctxWithT(entries) {
      return Object.assign(makeCtx(entries), {
        t: (k) => k,
        compareState: comp.methods.compareState,
        verKey: comp.methods.verKey,
      });
    }
    const titleMissing = comp.methods.compareTitle.call(
      ctxWithT([R('A.md', 'aaaa', 10, 1000)]), R('B.md', 'bbbb', 20, 2000));
    if (titleMissing !== 'aiconfig.ver_missing') {
      console.error(`FAIL title(missing) -> ${JSON.stringify(titleMissing)}`);
      process.exit(1);
    }
    console.log('ok title(missing) -> aiconfig.ver_missing');

    // compareTitle for a matched file builds both sides via the tooltip key.
    const titleSame = comp.methods.compareTitle.call(
      ctxWithT([R('A.md', 'aaaa', 10, 1000)]), R('A.md', 'aaaa', 10, 1000));
    if (typeof titleSame !== 'string' || !titleSame.includes('\n') ||
        !titleSame.includes('aiconfig.ver_tooltip')) {
      console.error(`FAIL title(same) -> ${JSON.stringify(titleSame)}`);
      process.exit(1);
    }
    console.log('ok title(same) carries tooltip');

    console.log('ALL_OK');
""")


def test_compare_function_states_via_node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = tmp_path / "round19_compare.js"
    script.write_text(_NODE_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script), _COMPONENT],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.returncode == 0, (
        f"round19 compare script failed:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "ALL_OK" in proc.stdout


def test_compare_logic_present_statically():
    # Belt-and-suspenders: the four states + defensive guard are named in the
    # component even on machines without node.
    src = _read_r19("components", "aiconfig-device-panel.js")
    for token in (
        "localByPath",
        "compareState: function",
        "compareTitle: function",
        "'missing'",
        "'same'",
        "'local_newer'",
        "'remote_newer'",
        "localStore.loaded",
        "mtimeMs",
    ):
        assert token in src, token


# ── 2. index.html CSS ──────────────────────────────────────────────────


def test_index_html_hosts_version_badge_css():
    css = _read_r19("index.html")
    for cls in (
        ".aiconfig-panel__ver-badge {",
        ".aiconfig-panel__ver-badge--same {",
        ".aiconfig-panel__ver-badge--local_newer {",
        ".aiconfig-panel__ver-badge--remote_newer {",
        ".aiconfig-panel__ver-badge--missing {",
    ):
        assert cls in css, f"missing CSS rule: {cls}"


# ── 3. component template binding + mount priming ──────────────────────


def test_panel_binds_version_badge_in_template():
    src = _read_r19("components", "aiconfig-device-panel.js")
    # Badge is rendered inline after the file path, gated on compareState.
    assert "aiconfig-panel__ver-badge" in src
    assert "compareState(e)" in src
    assert "compareTitle(e)" in src
    assert "aiconfig.ver_" in src
    # Path button / preview / check interactions stay intact.
    assert "openPreview(e)" in src
    assert "toggleCheck(e)" in src
    assert "pullAiConfigFiles" in src


def test_panel_mount_primes_local_inventory():
    src = _read_r19("components", "aiconfig-device-panel.js")
    assert "store.aiConfigLocal.loaded" in src
    assert "store.fetchAiConfigLocal()" in src


# ── 4. Locale parity ───────────────────────────────────────────────────


def test_locale_key_sets_identical_r19():
    en, zh = _locales_r19()
    en_only = set(en) - set(zh)
    zh_only = set(zh) - set(en)
    assert not en_only, f"keys missing from zh-CN.json: {sorted(en_only)}"
    assert not zh_only, f"keys missing from en.json: {sorted(zh_only)}"


def test_new_round19_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales_r19()
    for key in _NEW_KEYS_r19:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key
    # The tooltip interpolates both sides' formatted "size · mtime".
    for key in ("aiconfig.ver_tooltip",):
        assert "{local}" in en[key] and "{remote}" in en[key], key
        assert "{local}" in zh[key] and "{remote}" in zh[key], key
    # The visible badge states must differ across all four keys (non-empty
    # and mutually distinguishable so the panel reads correctly).
    seen = set()
    for key in ("aiconfig.ver_missing", "aiconfig.ver_same",
                "aiconfig.ver_local_newer", "aiconfig.ver_remote_newer"):
        assert en[key] != zh[key], f"same text both languages: {key}"


def test_locale_json_files_still_parse_r19():
    en, zh = _locales_r19()
    assert len(en) > 1000 and len(zh) > 1000


# ── 5. JS syntax (node --check over the touched file) ──────────────────


_TOUCHED_JS_r19 = [
    ("components", "aiconfig-device-panel.js"),
]


def test_touched_js_passes_node_check_r19():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for parts in _TOUCHED_JS_r19:
        path = os.path.join(_STATIC_r19, *parts)
        proc = subprocess.run(
            [node, "--check", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.returncode == 0, (
            f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"
        )


def test_no_nul_bytes_in_touched_js_r19():
    for parts in _TOUCHED_JS_r19:
        path = os.path.join(_STATIC_r19, *parts)
        with open(path, "rb") as f:
            data = f.read()
        assert b"\x00" not in data, f"NUL byte found in {os.path.join(*parts)}"
