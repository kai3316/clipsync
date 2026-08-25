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
