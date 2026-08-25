"""Round 19 — diagnostics web UX (frontend static assertions).

Covers the grouped diagnostics panel rewrite:

  1. The panel component renders v2 groups: group defs, collapse/expand,
     per-item status mapping (ok/warn/fail), unavailable-group placeholder,
     and the legacy flat-checks fallback for pre-v2 backends.
  2. index.html ships the new group CSS (card, header, status badge, warn
     row) and still loads the component script.
  3. Locales: en / zh-CN key sets identical and every ``diag.v2.*`` key is
     present and non-empty in both.
  4. node --check passes over diagnostics-panel.js (when node is available).
"""

import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")


def _read(*parts) -> str:
    with open(os.path.join(_STATIC, *parts), encoding="utf-8") as f:
        return f.read()


# ── 1. panel component: group rendering + status mapping ─────────────────


def test_panel_declares_all_group_defs():
    src = _read("components", "diagnostics-panel.js")
    for gid in ("system", "network", "internet", "ai_config", "chat",
                "transfer", "filesystem"):
        assert f"id: '{gid}'" in src, f"missing group def: {gid}"
    # Group defs are exposed to the template through data().
    assert "diagGroupDefs: DIAG_GROUP_DEFS" in src


def test_panel_has_group_collapse_expand():
    src = _read("components", "diagnostics-panel.js")
    assert "toggleGroup" in src
    assert "isCollapsed" in src
    assert "diagCollapsed" in src
    assert "diag-group__header" in src
    assert "@click=\"toggleGroup(def.id)\"" in src


def test_panel_maps_three_way_status():
    src = _read("components", "diagnostics-panel.js")
    # statusIcon: ok→✓, warn→!, fail→✕
    assert "statusIcon: function" in src
    assert "status === 'ok'" in src
    assert "status === 'warn'" in src
    assert "statusClass: function" in src
    # statusClass returns ok/warn/fail strings for CSS binding
    assert "return 'ok'" in src and "return 'warn'" in src and "return 'fail'" in src


def test_panel_renders_unavailable_group_placeholder():
    src = _read("components", "diagnostics-panel.js")
    assert "diag-group__unavailable" in src
    assert "diag.v2.group.unavailable" in src
    assert "groupUnavailable" in src


def test_panel_keeps_legacy_fallback():
    src = _read("components", "diagnostics-panel.js")
    assert "_applyLegacyChecks" in src
    assert "res.groups" in src          # v2 branch
    assert "res.checks" in src          # legacy branch
    # The panel prefers groups when present, otherwise falls back.
    assert "if (res && res.groups)" in src


def test_panel_reveal_animation_preserved():
    src = _read("components", "diagnostics-panel.js")
    assert "diagRevealed" in src
    assert "350" in src  # scan timing
    assert "diag-group--revealed" in src


def test_panel_summary_counts_unavailable_groups_as_warn():
    src = _read("components", "diagnostics-panel.js")
    chunk = src.split("_groupsSummary: function")[1]
    # A missing/empty group must surface as a warning, not a silent pass.
    assert "!g.items.length" in chunk
    assert "hasWarn = true" in chunk


def test_panel_item_label_map_covers_new_items():
    src = _read("components", "diagnostics-panel.js")
    for item in ("app_version", "uptime", "data_dir", "log_path", "lan_ip",
                 "tcp_port", "mdns_service", "web_service", "firewall",
                 "internet_enabled", "relay_state", "brokers", "netpair_count",
                 "pending_count", "watch_roots", "local_entries",
                 "last_collected", "trash_size", "chat_sessions",
                 "active_transfers", "transfer_failures", "history_db_size",
                 "disk_free"):
        assert f"{item}: 'diag.v2.item.{item}'" in src, f"missing label: {item}"


# ── 2. index.html: CSS + script loading ───────────────────────────────────


def test_group_css_present():
    css = _read("index.html")
    for cls in (".diag-groups {", ".diag-group {", ".diag-group__header {",
                ".diag-group__title {", ".diag-group__status--ok {",
                ".diag-group__status--warn {", ".diag-group__status--fail {",
                ".diag-group__unavailable {", ".diag-check--warn {"):
        assert cls in css, f"missing CSS rule: {cls}"


def test_panel_script_tag_loaded():
    html = _read("index.html")
    assert 'src="components/diagnostics-panel.js?token=__TOKEN__"' in html
    assert html.index("components/diagnostics-panel.js") < html.index("js/app.js")


def test_panel_mounted_in_both_layouts():
    html = _read("index.html")
    needle = "<diagnostics-panel v-else-if=\"store.activeTab === 'diagnostics'\" key=\"diagnostics\"></diagnostics-panel>"
    assert html.count(needle) == 2


# ── 3. locale parity + v2 keys ────────────────────────────────────────────


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


def test_v2_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
    v2_keys = sorted(k for k in en if k.startswith("diag.v2."))
    assert len(v2_keys) >= 100, f"expected ~100 v2 keys, got {len(v2_keys)}"
    for key in v2_keys:
        assert key in zh, f"missing from zh-CN.json: {key}"
        assert isinstance(en[key], str) and en[key].strip(), key
        assert isinstance(zh[key], str) and zh[key].strip(), key


def test_group_and_status_keys_present():
    en, _zh = _locales()
    for key in ("diag.v2.group.system", "diag.v2.group.network",
                "diag.v2.group.internet", "diag.v2.group.ai_config",
                "diag.v2.group.chat", "diag.v2.group.transfer",
                "diag.v2.group.filesystem", "diag.v2.group.unavailable",
                "diag.v2.status.ok", "diag.v2.status.warn",
                "diag.v2.status.fail"):
        assert key in en, key


def test_detail_placeholders_match_backend_params():
    en, zh = _locales()
    # Keys whose detail carries a backend parameter must actually interpolate
    # that parameter in BOTH locales (a missing {param} would render literally).
    checks = {
        "diag.v2.item.app_version.detail": "{version}",
        "diag.v2.item.uptime.detail": "{uptime}",
        "diag.v2.item.tcp_port.ok.detail": "{port}",
        "diag.v2.item.brokers.warn.detail": "{count}",
        "diag.v2.item.last_collected.ok.detail": "{ago}",
        "diag.v2.item.disk_free.ok.detail": "{free}",
    }
    for key, param in checks.items():
        assert param in en[key], (key, en[key])
        assert param in zh[key], (key, zh[key])


def test_locale_json_files_still_parse():
    en, zh = _locales()
    assert len(en) > 1000 and len(zh) > 1000


# ── 4. JS syntax (node --check) ───────────────────────────────────────────


def test_diagnostics_panel_passes_node_check(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    path = os.path.join(_STATIC, "components", "diagnostics-panel.js")
    proc = subprocess.run(
        [node, "--check", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.returncode == 0, (
        f"diagnostics-panel.js fails node --check:\n{proc.stderr}"
    )


def test_no_nul_bytes_in_diagnostics_panel():
    with open(os.path.join(_STATIC, "components", "diagnostics-panel.js"),
              "rb") as f:
        data = f.read()
    assert b"\x00" not in data
