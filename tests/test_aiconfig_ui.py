"""Refactor round 1 — AI-config web UI layer (unified tool-profile model).

The 「配置」 tab is now ONE unified panel (aiconfig-panel.js) driven by TOOL
PROFILES instead of raw watch paths:
  - device bar (this device + each paired device, diff badges per device)
  - per-tool grouped inventory with compare badges + folder whole-select
  - batch progress + per-file retry
  - a migration wizard (aiconfig-migrate-panel.js) + local manage sub-view
  - all diff/format logic shared via js/aiconfig-helpers.js (pure, testable)
The settings window's AI-config section is a profile checkbox list + custom
paths — the old raw watch-path list and its dedicated /paths endpoint are gone.

Static wiring assertions:
  1. index.html hosts the unified panel in both layouts, the helpers +
     wizard scripts, and the new CSS — and no longer loads the deleted
     aiconfig-device-panel.js.
  2. js/aiconfig-helpers.js is the single source of diff/format logic; a
     Node test drives it directly (pure functions).
  3. api.js wraps profiles / pull (folders + batch) / preview / local against
     the tool-based contract — no /api/aiconfig/paths wrappers remain.
  4. store.js holds the unified state (inventory, local, batches, migrate,
     profiles) and defensively normalizes every fetch.
  5. aiconfig-panel.js wires the unified UI (device bar, tool groups, folder
     whole-select, batch progress, migrate mount, local view) to the API.
  6. aiconfig-migrate-panel.js implements the wizard (source → diff → apply)
     with skip/overwrite/copy strategies and batch progress.
  7. settings-panel.js exposes the profile checkbox list (no raw path rows,
     no hardcoded preset table — profiles come from the API).
  8. tab-navigation.js badge = summed file-diff across paired devices.
  9. Locales: en / zh-CN key sets identical; every new key present and
     non-empty in both; the refactored-away keys are gone.
  10. A node --check pass over every JS file this round touched.
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")


def _read(*parts) -> str:
    with open(os.path.join(_STATIC, *parts), encoding="utf-8") as f:
        return f.read()


def _locales():
    with open(os.path.join(_STATIC, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC, "locales", "zh-CN.json"), encoding="utf-8") as f:
        zh = json.load(f)
    return en, zh


def _has_node():
    return shutil.which("node") is not None


def _T(src: str) -> str:
    """Un-escape template-string quotes so t('key') tokens are searchable.

    Components embed their templates as single-quoted JS strings, which means
    every t('...') call inside a template is written t(\'...\') in the source.
    Normalizing lets tests assert on the rendered form without caring about
    which quoting layer a token lives in.
    """
    return src.replace("\\'", "'")


# ── 1. index.html: unified panel + scripts + CSS ────────────────────────


def test_panel_mounted_in_both_layouts():
    # The tab → panel mapping is a SINGLE source of truth in app.js
    # (PANEL_COMPONENTS); index.html mounts one <component :is="panelComponent">
    # per layout instead of a hand-maintained v-else-if chain (the old chain
    # was duplicated in the wide + narrow layouts and drifted).
    html = _read("index.html")
    mount = '<component :is="panelComponent" :key="store.activeTab"></component>'
    assert html.count(mount) == 2, (
        "the panel component mount must appear in the wide AND narrow layouts"
    )
    # No literal v-else-if panel chain remains anywhere.
    assert "<overview-panel v-if=" not in html
    assert "<aiconfig-panel v-else-if=" not in html
    # aiconfig is wired through the mapping, alongside every other tab.
    app_src = _read("js", "app.js")
    assert "aiconfig: 'aiconfig-panel'" in app_src
    assert "panelComponent: function ()" in app_src
    for tab, name in (("overview", "overview-panel"), ("history", "history-panel"),
                      ("devices", "device-panel"), ("transfers", "transfer-panel"),
                      ("chat", "chat-panel"), ("favorites", "favorites-panel"),
                      ("aiconfig", "aiconfig-panel"),
                      ("diagnostics", "diagnostics-panel")):
        assert f"{tab}: '{name}'" in app_src, f"missing mapping {tab} → {name}"


def test_index_html_loads_unified_scripts():
    html = _read("index.html")
    # Shared pure helpers load first; the panel and wizard after it, all
    # before app.js (which registers every component at mount).
    assert 'src="js/aiconfig-helpers.js?token=__TOKEN__"' in html
    assert 'src="components/aiconfig-panel.js?token=__TOKEN__"' in html
    assert 'src="components/aiconfig-migrate-panel.js?token=__TOKEN__"' in html
    i_helpers = html.index("aiconfig-helpers.js")
    i_panel = html.index("components/aiconfig-panel.js")
    i_wizard = html.index("aiconfig-migrate-panel.js")
    i_app = html.index("js/app.js")
    assert i_helpers < i_panel < i_wizard < i_app
    # The deleted device panel is not loaded anywhere.
    assert "aiconfig-device-panel.js" not in html


def test_index_html_has_unified_panel_css():
    css = _read("index.html")
    for cls in (
        ".aiconfig-panel__hero {",
        ".aiconfig-panel__device-pill {",
        ".aiconfig-panel__device-pill-badge {",
        ".aiconfig-panel__device-pill-tag--legacy {",
        ".aiconfig-panel__folder-check {",
        ".aiconfig-panel__batch {",
        ".aiconfig-panel__batch-bar {",
        ".aiconfig-panel__local {",
        ".aiconfig-panel__local-tablewrap {",
        ".aiconfig-panel__ver-badge {",
        ".aiconfig-panel__ver-badge--missing {",
        ".aiconfig-migrate__overlay {",
        ".aiconfig-migrate__groups {",
        ".aiconfig-migrate__strategy {",
        ".settings-checkbox {",
    ):
        assert cls in css, f"missing CSS rule: {cls}"


def test_device_panel_component_is_deleted():
    # The old browse/pull UI was merged into the unified panel; the separate
    # component file must be gone (not merely unused).
    assert not os.path.exists(os.path.join(_STATIC, "components",
                                           "aiconfig-device-panel.js"))
    assert not os.path.exists(os.path.join(_STATIC, "components",
                                           "aiconfig-helpers.js"))


# ── 2. aiconfig-helpers.js: shared pure logic ───────────────────────────


def test_helpers_export_table():
    src = _read("js", "aiconfig-helpers.js")
    for fn in ("fmtSize", "fmtTime", "mtimeMs", "keyOf", "legacyKeyOf",
               "buildLocalIndex", "compareState", "diffCounts", "toolLabel"):
        assert f"{fn}:" in src, fn
    assert "KEY_SEP" in src
    assert "__CLIPSYNC_AICONFIG_HELPERS__" in src


_NODE_HELPERS = textwrap.dedent(r"""
    const fs = require('fs');
    global.window = {};
    eval(fs.readFileSync(process.argv[2], 'utf8'));
    const H = global.window.__CLIPSYNC_AICONFIG_HELPERS__;

    const assert = (cond, msg) => { if (!cond) { console.error('FAIL ' + msg); process.exit(1); } };
    const E = (tool, rel_path, sha256, mtime) => ({ tool, rel_path, sha256, mtime });

    const localEntries = [
      E('claude_code', 'CLAUDE.md', 'aaaa', 1000),
      E('claude_code', 'settings.json', 'bbbb', 2000),
      E('custom', 'notes.md', 'cccc', 3000),
      { tool: 'claude_code', rel_path: 'skills/x/', is_dir: true }, // excluded
    ];
    const idx = H.buildLocalIndex(localEntries);

    // keyOf: tool + rel_path, joined by a separator that can't collide.
    assert(H.keyOf(E('claude_code', 'CLAUDE.md')) === 'claude_codeCLAUDE.md', 'keyOf shape');
    assert(H.legacyKeyOf({ root_index: 2, rel_path: 'x' }) === 'legacy2x', 'legacyKeyOf shape');
    // buildLocalIndex keys by (tool, rel_path); directories are dropped.
    assert(idx.byKey['claude_codeCLAUDE.md'].sha256 === 'aaaa', 'byKey lookup');
    assert(idx.byKey['claude_codeskills/x/'] === undefined, 'dirs excluded');
    assert(idx.byPath['notes.md'].tool === 'custom', 'byPath fallback');

    // compareState: same / missing / local_newer / remote_newer.
    assert(H.compareState(idx, E('claude_code', 'CLAUDE.md', 'aaaa', 1000)) === 'same', 'same');
    assert(H.compareState(idx, E('claude_code', 'NEW.md', 'dddd', 1)) === 'missing', 'missing');
    assert(H.compareState(idx, E('claude_code', 'CLAUDE.md', 'zzzz', 500)) === 'local_newer', 'local_newer');
    assert(H.compareState(idx, E('claude_code', 'CLAUDE.md', 'zzzz', 5000)) === 'remote_newer', 'remote_newer');
    // Equal mtime with differing hash falls through to "remote is newer".
    assert(H.compareState(idx, E('claude_code', 'CLAUDE.md', 'zzzz', 1000)) === 'remote_newer', 'equal mtime');
    // Directory rows and an unloaded local index produce no badge.
    assert(H.compareState(idx, { tool: 'claude_code', rel_path: 'skills/x/', is_dir: true }) === null, 'dir null');
    assert(H.compareState(null, E('claude_code', 'CLAUDE.md', 'aaaa', 1000)) === null, 'no local null');

    // mtime unit invariance: seconds below ~1e12, anything bigger is ms.
    const msIdx = H.buildLocalIndex([E('custom', 'a.md', 's1', 2000000000000)]);
    assert(H.compareState(msIdx, E('custom', 'a.md', 's2', 1500000000000)) === 'local_newer', 'ms local_newer');
    assert(H.compareState(msIdx, E('custom', 'a.md', 's2', 3000000000000)) === 'remote_newer', 'ms remote_newer');

    // A legacy remote row (root_index) compares against local by rel_path only.
    assert(H.compareState(idx, { root_index: 0, rel_path: 'notes.md', sha256: 'cccc', mtime: 1 }, true) === 'same', 'legacy same');
    // A v2 remote whose tool is not local still falls back to byPath.
    assert(H.compareState(idx, E('codex', 'notes.md', 'cccc', 1)) === 'same', 'byPath fallback');

    // diffCounts aggregates the four states.
    const cnt = H.diffCounts(idx, [
      E('claude_code', 'CLAUDE.md', 'aaaa', 1),        // same
      E('claude_code', 'NEW.md', 'x', 1),             // missing
      E('claude_code', 'settings.json', 'zzzz', 1),   // local_newer
      E('claude_code', 'settings.json', 'zzzz', 9e6), // remote_newer
    ]);
    assert(cnt.same === undefined && cnt.total === 3, 'diffCounts total');
    assert(cnt.missing === 1 && cnt.local_newer === 1 && cnt.remote_newer === 1, 'diffCounts buckets');
    assert(H.diffCounts(idx, 'not-a-list').total === 0, 'diffCounts defensive');

    // Formatters + tool label.
    assert(H.mtimeMs(1000) === 1000000 && H.mtimeMs(2000000000000) === 2000000000000, 'mtimeMs');
    assert(H.mtimeMs(undefined) === -1, 'mtimeMs missing');
    assert(H.fmtSize(512) === '512 B' && H.fmtSize(2048) === '2.0 KB', 'fmtSize');
    assert(H.toolLabel([{ key: 'claude_code', label: 'Claude Code' }], 'claude_code') === 'Claude Code', 'toolLabel');
    assert(H.toolLabel([], 'custom') === 'custom', 'toolLabel fallback');

    console.log('ALL_OK');
""")


def test_helpers_behavior_via_node(tmp_path):
    if not _has_node():
        pytest.skip("node not available")
    script = tmp_path / "helpers_check.js"
    script.write_text(_NODE_HELPERS, encoding="utf-8")
    path = os.path.join(_STATIC, "js", "aiconfig-helpers.js")
    proc = subprocess.run([shutil.which("node"), str(script), path],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True)
    assert proc.returncode == 0, (
        f"helpers script failed:\n{proc.stdout}\n{proc.stderr}")
    assert "ALL_OK" in proc.stdout


# ── 3. api.js: tool-based contract, no /paths wrappers ──────────────────


def test_api_profiles_wrappers():
    src = _read("js", "api.js")
    assert "getAiConfigProfiles" in src
    assert "'/api/aiconfig/profiles'" in src
    body = src.split("setAiConfigProfiles")[1].split("previewAiConfigFile")[0]
    assert "'/api/aiconfig/profiles'" in body          # POST
    for field in ("tools", "custom_paths"):
        assert field in body, field


def test_api_pull_wrapper_supports_folders_and_batch():
    src = _read("js", "api.js")
    assert "pullAiConfigFiles" in src
    assert "'/api/aiconfig/pull'" in src
    body = src.split("pullAiConfigFiles: function")[1]
    for field in ("peer_id", "items", "mode"):
        assert field in body, field
    # Folder items are just entries with is_dir; batch_id rides the request.
    assert "batch_id" in body
    assert "batchId" in body


def test_api_preview_wrappers_split_tool_and_legacy():
    src = _read("js", "api.js")
    assert "previewAiConfigFile" in src
    assert "previewAiConfigLegacy" in src
    v2 = src.split("previewAiConfigFile: function")[1]
    for field in ("peer_id", "tool", "rel_path"):
        assert field in v2, field
    leg = src.split("previewAiConfigLegacy: function")[1]
    for field in ("peer_id", "root_index", "rel_path"):
        assert field in leg, field
    # Tolerant parsing: raw-text bodies must not break the preview modal.
    assert "JSON.parse(bodyText)" in src
    assert "65536" in src  # 64KB truncation heuristic


def test_api_local_wrappers_use_tool_query():
    src = _read("js", "api.js")
    body = src.split("getAiConfigLocalItem: function")[1]
    assert "'/api/aiconfig/local/item?tool='" in body
    assert "'&rel_path='" in body
    assert "encodeURIComponent(tool)" in body
    assert "encodeURIComponent(relPath)" in body
    # local save / trash / open carry the tool field, never root_index.
    for method, url, fields in (
        ("saveAiConfigLocal", "/api/aiconfig/local/save",
         ("tool", "rel_path", "content")),
        ("trashAiConfigLocal", "/api/aiconfig/local/trash",
         ("tool", "rel_path")),
        ("openAiConfigLocal", "/api/aiconfig/open",
         ("tool", "rel_path")),
    ):
        body = src.split(f"{method}: function")[1]
        assert f"'{url}'" in body, method
        for field in fields:
            assert field in body, f"{method}:{field}"


def test_api_has_no_paths_endpoint_wrappers():
    # The raw watch-path list and its dedicated endpoint are gone.
    src = _read("js", "api.js")
    assert "getAiConfigPaths" not in src
    assert "setAiConfigPaths" not in src
    assert "'/api/aiconfig/paths'" not in src


# ── 4. store.js: unified state + defensive normalization ────────────────


def test_store_declares_unified_aiconfig_state():
    src = _read("js", "store.js")
    for token in (
        "aiConfigInventory:",
        "aiConfigLocal:",
        "aiConfigProfiles:",
        "aiConfigBatches:",
        "aiConfigMigrateOpen:",
        "fetchAiConfigInventory: function",
        "fetchAiConfigLocal: function",
        "fetchAiConfigProfiles: function",
        "applyAiConfigFileResult: function",
        "startAiConfigBatch: function",
        "clearAiConfigBatch: function",
        "openAiConfigMigrate: function",
        "closeAiConfigMigrate: function",
    ):
        assert token in src, token


def test_store_inventory_fetch_normalizes_defensively():
    src = _read("js", "store.js")
    chunk = src.split("fetchAiConfigInventory: function")[1].split(
        "applyAiConfigFileResult")[0]
    assert "typeof res.peers === 'object'" in chunk
    assert "Array.isArray(p.entries)" in chunk
    # The wire keys each entry's path as `path`; the store normalizes to the
    # spec'd `rel_path` so every consumer relies on one name.
    assert "e.rel_path || e.path" in chunk
    # Failure must still settle the load flag so the panel shows empty/retry.
    assert "aiConfigLoaded = true" in chunk


def test_store_local_fetch_normalizes_defensively():
    src = _read("js", "store.js")
    chunk = src.split("fetchAiConfigLocal: function")[1].split(
        "openAiConfigMigrate")[0]
    # The local manager must work with zero paired devices — no peer gate.
    assert "getAiConfigLocal" in chunk
    # Malformed roots/entries degrade to empty lists, never a crash.
    assert "Array.isArray(res.roots)" in chunk
    assert "Array.isArray(res.entries)" in chunk
    assert "e.rel_path || e.path" in chunk
    assert "aiConfigLocal.loaded = true" in chunk


def test_store_apply_batch_tracks_progress():
    src = _read("js", "store.js")
    chunk = src.split("applyAiConfigFileResult: function")[1].split(
        "startAiConfigBatch")[0]
    # Rolling results list, capped.
    assert "aiConfigResults.unshift" in chunk
    # Batch tracking: a batch_id on the WS event folds into aiConfigBatches
    # and flips `finished` when the count reaches the total.
    assert "batch_id" in chunk
    assert "batch.done += 1" in chunk
    assert "batch.finished = true" in chunk
    # The toast key maps each status vocabulary.
    for key in ("aiconfig.result_error", "aiconfig.result_copied",
                "aiconfig.result_appended", "aiconfig.result_saved"):
        assert key in chunk, key


def test_store_batch_lifecycle():
    src = _read("js", "store.js")
    chunk = src.split("startAiConfigBatch: function")[1].split(
        "openAiConfigMigrate")[0]
    assert "total: total" in chunk and "done: 0" in chunk
    assert "results: []" in chunk and "finished: false" in chunk
    assert "peerId: peerId || ''" in chunk
    # clearAiConfigBatch drops the entry so the map never grows unbounded
    # (it sits after the migrate helpers in the store, so search the tail).
    tail = src.split("clearAiConfigBatch: function")[1]
    assert "delete this.aiConfigBatches[batchId]" in tail
    # Only a known batch may be cleared — unknown ids are ignored.
    assert "if (batchId && this.aiConfigBatches[batchId])" in tail


# ── 5. aiconfig-panel.js: unified tab wiring ────────────────────────────


def test_panel_component_structure():
    src = _read("components", "aiconfig-panel.js")
    assert "__CLIPSYNC_COMPONENTS__['aiconfig-panel']" in src
    assert "inject: ['store']" in src
    # Default landing mode is copy — never a silent overwrite.
    assert "mode: 'copy'" in src
    # The tab opens on the LOCAL device; peer pills are built from the
    # inventory, not hardcoded.
    assert "selectedDevice: 'local'" in src
    assert "buildDevices" in src or "devices: function" in src


def test_panel_device_bar_and_diff_badges():
    src = _T(_read("components", "aiconfig-panel.js"))
    # Every paired device gets a pill with a summed diff badge.
    assert "aiconfig-panel__device-pill" in src
    assert "H.diffCounts(self.localIndex, p.entries, p.legacy)" in src
    assert "diffTotal: d.total" in src
    assert "diffRemoteNewer: d.remote_newer" in src
    # Local pill is the first entry, labelled via the locale.
    assert "name: this.t('aiconfig.local_device')" in src
    assert "isLocal: true" in src
    # The per-device summary line + legacy banner.
    assert "t('aiconfig.diff_summary'" in src
    assert "t('aiconfig.legacy_read_only')" in src
    assert "t('aiconfig.peer_empty')" in src
    # The diff line is only drawn for a non-legacy peer with a real diff.
    assert "deviceDiff.total === 0" in src


def test_panel_uses_shared_helpers():
    src = _read("components", "aiconfig-panel.js")
    # The panel binds the helpers into the Vue instance; every diff/format
    # decision goes through them.
    for token in ("H.fmtSize", "H.fmtTime", "H.mtimeMs",
                  "H.keyOf", "H.buildLocalIndex", "H.compareState",
                  "H.diffCounts", "H.toolLabel"):
        assert token in src, token


def test_panel_folder_whole_select_and_batch():
    src = _T(_read("components", "aiconfig-panel.js"))
    # Folder rows can be whole-selected (recursive); the checkbox state and
    # descendant enumeration live here.
    assert "descendantFileKeys: function" in src
    assert "folderState: function" in src
    assert "toggleFolder: function" in src
    # Batch pull progress: N/M bar + retry of the failures.
    assert "activeBatch: function" in src
    assert "batchPct: function" in src
    assert "retryFailures: function" in src
    assert "t('aiconfig.batch_progress'" in src
    assert "t('aiconfig.batch_retry')" in src
    assert "t('aiconfig.batch_done')" in src
    # Folder whole-select hides when there is nothing to expand.
    assert "t('aiconfig.folder_select_count'" in src


def test_panel_migrate_mount_and_local_view():
    src = _T(_read("components", "aiconfig-panel.js"))
    # The wizard is mounted from the unified panel, gated on the store flag.
    assert '<aiconfig-migrate-panel v-if="store.aiConfigMigrateOpen"></aiconfig-migrate-panel>' in src
    assert "openMigrate: function" in src
    assert "t('aiconfig.migrate_title')" in src
    # Local manage sub-view survives inside the unified tab.
    for token in (
        "openLocalPreview: function",
        "startLocalEdit: function",
        "saveLocalEdit: function",
        "trashEntry: function",
        "openEntryDir: function",
        "openSettingsAiconfig: function",
        "t('aiconfig.local_manage_profiles')",
        "t('aiconfig.local_trash_confirm'",
        "t('aiconfig.local_trashed_toast'",
    ):
        assert token in src, token
    # Trash is a confirmed move — never a silent delete.
    assert "store.confirm" in src


def test_panel_mount_primes_data_sources():
    src = _read("components", "aiconfig-panel.js")
    assert "this.store.fetchAiConfigLocal()" in src
    assert "this.store.fetchAiConfigInventory(false)" in src
    assert "this.store.fetchAiConfigProfiles()" in src


def test_panel_has_no_legacy_watch_path_or_device_panel_tokens():
    src = _read("components", "aiconfig-panel.js")
    # The unified panel lives in ONE file — the deleted device-panel component
    # is not referenced, and the raw watch-path API is not touched.  (root_index
    # may legitimately appear: legacy peers are still browsable/previewable.)
    for token in ("getAiConfigPaths", "setAiConfigPaths",
                  "aiconfig-device-panel", "peersList === null"):
        assert token not in src, token


# ── 6. aiconfig-migrate-panel.js: the wizard ────────────────────────────


def test_migrate_panel_component_structure():
    src = _read("components", "aiconfig-migrate-panel.js")
    assert "__CLIPSYNC_COMPONENTS__['aiconfig-migrate-panel']" in src
    assert "inject: ['store']" in src
    # Three landing strategies, all surfaced through the locale.
    assert "STRATEGIES" in src
    for value, label in (("skip", "aiconfig.migrate_strategy_skip"),
                         ("overwrite", "aiconfig.migrate_strategy_overwrite"),
                         ("copy", "aiconfig.migrate_strategy_copy")):
        assert f"'{label}'" in src, label
    # Only v2 peers are migratable (legacy peers are read-only).
    assert "v2Peers: function" in src


def test_migrate_panel_template_and_flow():
    src = _T(_read("components", "aiconfig-migrate-panel.js"))
    for token in (
        "t('aiconfig.migrate_source')",
        "t('aiconfig.migrate_diff_count'",
        "t('aiconfig.migrate_nothing_to_do')",
        "t('aiconfig.migrate_nothing_checked')",
        "t('aiconfig.migrate_apply'",
        "t('aiconfig.migrate_strategy_label')",
        "t('aiconfig.migrate_no_source')",
    ):
        assert token in src, token
    # Applying with zero checked rows is refused client-side.
    assert "aiconfig.migrate_nothing_checked" in src
    # Batch progress reuses the panel's batch vocabulary.
    assert "batch" in src


def test_migrate_panel_has_no_legacy_tokens():
    src = _read("components", "aiconfig-migrate-panel.js")
    assert "getAiConfigPaths" not in src
    assert "aiconfig-device-panel" not in src


# ── 7. settings-panel.js: tool-profile checkbox list ────────────────────


def test_settings_panel_ai_config_section_wiring():
    src = _read("components", "settings-panel.js")
    # Settings nav + search index entry.
    assert "mk('aiconfig'" in src
    assert "aiconfig: [" in src
    # Profiles load from the API on open (both the open watcher and the
    # active-section watcher prime them).
    assert "loadAiConfigProfiles: function" in src
    assert src.count("'aiconfig') this.loadAiConfigProfiles()") == 2
    # Tool toggles + custom path rows + save.
    assert "toolEnabled: function" in src
    assert "toggleAiConfigTool: function" in src
    assert "addAiConfigCustomPath" in src
    assert "removeAiConfigCustomPath" in src
    assert "saveAiConfigProfiles: function" in src
    # The template renders the fetched profile cards as checkboxes.
    assert "class=\"settings-checkbox\"" in src
    assert "toolEnabled(prof.key)" in src
    assert "profilePaths(prof)" in src


def test_settings_panel_ai_config_fetches_profiles_not_presets():
    src = _read("components", "settings-panel.js")
    # No hardcoded preset table — the cards come from GET /api/aiconfig/profiles.
    assert "getAiConfigProfiles" in src
    assert "AICONFIG_PRESETS" not in src
    assert "getAiConfigPaths" not in src and "setAiConfigPaths" not in src
    # Custom paths are capped at the backend limit (50) on the way out.
    assert "custom.length < 50" in src
    # Saving normalizes via the API and settles the dirty flag.
    assert "setAiConfigProfiles" in src
    assert "dirtySections['aiconfig'] = false" in src


def test_settings_section_template_registered():
    src = _read("components", "settings-panel.js")
    assert "activeSection === 'aiconfig'" in src.replace("\\'", "'")


# ── 8. tab-navigation.js: badge = summed diff across peers ──────────────


def test_tab_navigation_aiconfig_badge_is_diff_total():
    src = _read("components", "tab-navigation.js")
    assert "id: 'aiconfig'" in src
    assert "t('ui.aiconfig')" in src
    # Badge = total file-diff count across every paired device (vs this
    # machine's inventory) — zero = nothing worth pulling.
    assert "H.diffCounts" in src
    assert "buildLocalIndex" in src
    assert "total += H.diffCounts" in src
    assert "aiConfigInventory" in src


# ── 9. Locale parity ───────────────────────────────────────────────────

_NEW_KEYS = [
    # unified panel / device bar
    "aiconfig.local_device",
    "aiconfig.local_device_hint",
    "aiconfig.peer_empty",
    "aiconfig.legacy_peer",
    "aiconfig.legacy_read_only",
    "aiconfig.diff_summary",
    "aiconfig.diff_synced",
    # folder whole-select + batch
    "aiconfig.folder_select_count",
    "aiconfig.batch_progress",
    "aiconfig.batch_retry",
    "aiconfig.batch_done",
    # migration wizard
    "aiconfig.migrate_title",
    "aiconfig.migrate_source",
    "aiconfig.migrate_no_source",
    "aiconfig.migrate_no_source_desc",
    "aiconfig.migrate_diff_count",
    "aiconfig.migrate_nothing_to_do",
    "aiconfig.migrate_nothing_checked",
    "aiconfig.migrate_apply",
    "aiconfig.migrate_strategy_label",
    "aiconfig.migrate_strategy_skip",
    "aiconfig.migrate_strategy_skip_hint",
    "aiconfig.migrate_strategy_overwrite",
    "aiconfig.migrate_strategy_overwrite_hint",
    "aiconfig.migrate_strategy_copy",
    "aiconfig.migrate_strategy_copy_hint",
    # local manage sub-view
    "aiconfig.local_title",
    "aiconfig.local_empty_title",
    "aiconfig.local_empty_desc",
    "aiconfig.local_no_paths",
    "aiconfig.local_manage_profiles",
    "aiconfig.local_collected_at",
    "aiconfig.local_skills_count",
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
    # settings section (profiles + custom paths)
    "settings_nav.aiconfig",
    "settings_window.aiconfig_desc",
    "settings_window.aiconfig_tools_label",
    "settings_window.aiconfig_tools_hint",
    "settings_window.aiconfig_custom_paths_label",
    "settings_window.aiconfig_custom_paths_hint",
    "settings_window.aiconfig_add_path",
    "settings_window.aiconfig_path_placeholder",
    "settings_window.aiconfig_save_hint",
    "settings.aiconfig_saved",
    "settings.save_aiconfig_failed",
    # compare badges (kept from the prior rounds)
    "aiconfig.ver_missing",
    "aiconfig.ver_same",
    "aiconfig.ver_local_newer",
    "aiconfig.ver_remote_newer",
    "aiconfig.ver_tooltip",
]

# Keys the refactor deliberately removed (raw watch-path model / old device
# panel).  Their absence is the assertion that the old UI is really gone.
_GONE_KEYS = [
    "aiconfig.devices",
    "aiconfig.files_count",
    "aiconfig.local_add_paths",
    "aiconfig.local_manage_paths",
    "aiconfig.local_paths_hint",
    "aiconfig.local_add_path",
    "aiconfig.local_paths_saved",
    "aiconfig.local_paths_save_failed",
    "aiconfig.local_device_title",
    "settings_window.aiconfig_paths_label",
    "settings_window.aiconfig_paths_hint",
]


def test_locale_key_sets_identical():
    en, zh = _locales()
    en_only = set(en) - set(zh)
    zh_only = set(zh) - set(en)
    assert not en_only, f"keys missing from zh-CN.json: {sorted(en_only)}"
    assert not zh_only, f"keys missing from en.json: {sorted(zh_only)}"


def test_refactor_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
    for key in _NEW_KEYS:
        assert key in en, f"missing from en.json: {key}"
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key


def test_removed_watch_path_keys_are_gone():
    en, zh = _locales()
    for key in _GONE_KEYS:
        assert key not in en, f"stale key still in en.json: {key}"
        assert key not in zh, f"stale key still in zh-CN.json: {key}"


def test_locale_placeholders_present():
    en, zh = _locales()
    # {count} interpolations.
    for key in ("aiconfig.folder_select_count", "aiconfig.migrate_diff_count",
                "aiconfig.migrate_apply", "aiconfig.local_skills_count"):
        assert "{count}" in en[key], key
        assert "{count}" in zh[key], key
    # Batch progress interpolates done/total.
    assert "{done}" in en["aiconfig.batch_progress"]
    assert "{done}" in zh["aiconfig.batch_progress"]
    assert "{total}" in en["aiconfig.batch_progress"]
    assert "{total}" in zh["aiconfig.batch_progress"]
    # The device diff summary carries all three buckets.
    assert "{missing}" in en["aiconfig.diff_summary"]
    assert "{remote}" in en["aiconfig.diff_summary"]
    assert "{local}" in en["aiconfig.diff_summary"]
    for ph in ("{missing}", "{remote}", "{local}"):
        assert ph in zh["aiconfig.diff_summary"], ph
    # Local actions interpolate their slot values.
    assert "{time}" in en["aiconfig.local_collected_at"]
    assert "{time}" in zh["aiconfig.local_collected_at"]
    assert "{path}" in en["aiconfig.local_trash_confirm"]
    assert "{path}" in zh["aiconfig.local_trash_confirm"]
    assert "{dest}" in en["aiconfig.local_trashed_toast"]
    assert "{dest}" in zh["aiconfig.local_trashed_toast"]
    # The saved toast has no slot (it names the .bak backup verbatim).
    assert "{path}" not in en["aiconfig.local_saved_toast"]
    for key in ("aiconfig.local_save_failed", "aiconfig.local_trash_failed",
                "aiconfig.local_open_failed", "aiconfig.local_preview_failed"):
        assert "{reason}" in en[key], key
        assert "{reason}" in zh[key], key
    # Version tooltip interpolates both sides.
    assert "{local}" in en["aiconfig.ver_tooltip"]
    assert "{remote}" in en["aiconfig.ver_tooltip"]
    assert "{local}" in zh["aiconfig.ver_tooltip"]
    assert "{remote}" in zh["aiconfig.ver_tooltip"]


def test_locale_json_files_still_parse():
    en, zh = _locales()
    assert len(en) > 1000 and len(zh) > 1000


# ── 10. JS syntax (node --check over every file this round touched) ─────

_TOUCHED_JS = [
    ("js", "aiconfig-helpers.js"),
    ("components", "aiconfig-panel.js"),
    ("components", "aiconfig-migrate-panel.js"),
    ("components", "tab-navigation.js"),
    ("components", "settings-panel.js"),
    ("js", "api.js"),
    ("js", "store.js"),
    ("js", "ws.js"),
]


def test_touched_js_passes_node_check(tmp_path):
    if not _has_node():
        pytest.skip("node not available")
    node = shutil.which("node")
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        assert os.path.exists(path), f"missing touched JS: {os.path.join(*parts)}"
        proc = subprocess.run([node, "--check", path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
        assert proc.returncode == 0, (
            f"{os.path.join(*parts)} fails node --check:\n{proc.stderr}"
        )


def test_no_nul_bytes_in_touched_js():
    for parts in _TOUCHED_JS:
        path = os.path.join(_STATIC, *parts)
        with open(path, "rb") as f:
            data = f.read()
        assert b"\x00" not in data, f"NUL byte found in {os.path.join(*parts)}"


def test_no_hardcoded_preset_table_anywhere():
    # The profile table's single source of truth is the backend
    # (ai_profiles.py, served via /api/aiconfig/profiles).  No JS file may
    # embed its own preset list that could drift.
    for root, _dirs, files in os.walk(os.path.join(_STATIC, "components")):
        for name in files:
            if not name.endswith(".js"):
                continue
            src = _read(os.path.relpath(os.path.join(root, name), _STATIC))
            assert "AICONFIG_PRESETS" not in src, f"presets in {name}"
    for name in ("store.js", "api.js"):
        src = _read("js", name)
        assert "AICONFIG_PRESETS" not in src, f"presets in {name}"
