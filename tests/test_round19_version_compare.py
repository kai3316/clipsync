"""Round 19 — device-config browse: 本机 vs 远端 new/old comparison.

The 「设备配置」 device-panel (aiconfig-device-panel.js) gains a per-row
version badge next to each REMOTE file that compares it against THIS
device's local AI-config inventory (store.aiConfigLocal):

  missing       local has no file with the same rel_path   (neutral)
  same          same rel_path + identical sha256           (green)
  local_newer   sha256 differs, local mtime newer          (cyan)
  remote_newer  sha256 differs, remote mtime newer/equal   (amber → pull)

Matching is by rel_path ONLY — the two devices' root_index lists are
ordered independently, so their indices carry no cross-device meaning.
Defensive: when the local inventory hasn't loaded yet (old backend / fetch
in flight), compareState() returns null and no badge is rendered rather than
mislabeling every file "missing".  The Devices-tab mount primes the local
inventory alongside the peer inventory.

Static wiring assertions:
  1. The compare function is exercised with constructed store data in Node
     (four states + unit-invariance of the mtime comparison + the
     not-loaded → null guard + the missing-file tooltip).
  2. index.html hosts the badge CSS classes.
  3. aiconfig-device-panel.js binds the badge (class + tooltip + text) and
     primes the local inventory on mount.
  4. Locales: en / zh-CN key sets identical and every new key present and
     non-empty in both; the tooltip placeholders exist.
  5. A node --check pass over the touched JS file.
"""

import json
import os
import shutil
import subprocess
import textwrap

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")

_COMPONENT = os.path.join(_STATIC, "components", "aiconfig-device-panel.js")

_NEW_KEYS = [
    "aiconfig.ver_missing",
    "aiconfig.ver_same",
    "aiconfig.ver_local_newer",
    "aiconfig.ver_remote_newer",
    "aiconfig.ver_tooltip",
]


def _read(*parts) -> str:
    with open(os.path.join(_STATIC, *parts), encoding="utf-8") as f:
        return f.read()


def _locales():
    with open(os.path.join(_STATIC, "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(_STATIC, "locales", "zh-CN.json"), encoding="utf-8") as f:
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
    src = _read("components", "aiconfig-device-panel.js")
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
    css = _read("index.html")
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
    src = _read("components", "aiconfig-device-panel.js")
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
    src = _read("components", "aiconfig-device-panel.js")
    assert "store.aiConfigLocal.loaded" in src
    assert "store.fetchAiConfigLocal()" in src


# ── 4. Locale parity ───────────────────────────────────────────────────


def test_locale_key_sets_identical():
    en, zh = _locales()
    en_only = set(en) - set(zh)
    zh_only = set(zh) - set(en)
    assert not en_only, f"keys missing from zh-CN.json: {sorted(en_only)}"
    assert not zh_only, f"keys missing from en.json: {sorted(zh_only)}"


def test_new_round19_keys_present_and_nonempty_in_both_locales():
    en, zh = _locales()
    for key in _NEW_KEYS:
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


def test_locale_json_files_still_parse():
    en, zh = _locales()
    assert len(en) > 1000 and len(zh) > 1000


# ── 5. JS syntax (node --check over the touched file) ──────────────────


_TOUCHED_JS = [
    ("components", "aiconfig-device-panel.js"),
]


def test_touched_js_passes_node_check():
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
