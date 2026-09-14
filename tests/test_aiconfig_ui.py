# ruff: noqa: E501  # a big JS-assert script lives as a triple-quoted string below,
#                   # so long "lines" are JS statements, not Python — reflowing them
#                   # would change the string content, not just formatting.

"""The AI-config diff helpers, driven directly under Node.

Everything else this file used to assert was static wiring over the shipped
JavaScript (markup, CSS, class names, locale keys) and is gone.

What is left is the one seam no other suite can see: `js/aiconfig-helpers.js`
decides how a peer's inventory rows compare against this machine's, and the
rows are a cross-version wire format — a v3 row is keyed by (tool, root,
rel_path), a legacy row carries only `root_index`, and a v2 row carries no
root at all and must still fall back to a by-path match.  The desktop Vue and
Playwright suites hand the components payloads they wrote themselves; this
runs the shipped helper against those row shapes.
"""

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "internal", "web", "static")


def _has_node():
    return shutil.which("node") is not None


_NODE_HELPERS = textwrap.dedent(r"""
    const fs = require('fs');
    global.window = {};
    // aiconfig-helpers delegates fmtSize/fmtSpeed to the ClipsyncFormat global
    // defined in format.js; load that first, mirroring index.html's <script>
    // order, so the bare `ClipsyncFormat` reference resolves in Node too.
    eval(fs.readFileSync(process.argv[3], 'utf8'));
    eval(fs.readFileSync(process.argv[2], 'utf8'));
    const H = global.window.__CLIPSYNC_AICONFIG_HELPERS__;

    const assert = (cond, msg) => { if (!cond) { console.error('FAIL ' + msg); process.exit(1); } };
    // root is the stable watch-root id a rel_path is relative to (v3).
    const E = (tool, root, rel_path, sha256, mtime) =>
      ({ tool, root, rel_path, sha256, mtime });

    const localEntries = [
      E('claude_code', 'memory', 'CLAUDE.md', 'aaaa', 1000),
      E('claude_code', 'settings', 'settings.json', 'bbbb', 2000),
      E('custom', 'r0', 'notes.md', 'cccc', 3000),
      // Same rel under two dir roots of ONE tool: distinct files, and the
      // whole reason root is part of the identity.
      E('claude_code', 'skills', 'x.md', 'sk11', 1000),
      E('claude_code', 'commands', 'x.md', 'cm22', 1000),
      { tool: 'claude_code', root: 'skills', rel_path: 'x/', is_dir: true },
    ];
    const idx = H.buildLocalIndex(localEntries);

    // keyOf: tool + root + rel_path, joined by a separator that can't collide.
    assert(H.keyOf(E('claude_code', 'memory', 'CLAUDE.md')) ===
      'claude_code\x01memory\x01CLAUDE.md', 'keyOf shape');
    // Two roots of one tool never collapse to one key.
    assert(H.keyOf(E('claude_code', 'skills', 'x.md')) !==
      H.keyOf(E('claude_code', 'commands', 'x.md')), 'keyOf root distinct');
    assert(H.legacyKeyOf({ root_index: 2, rel_path: 'x' }) === 'legacy\x012\x01x', 'legacyKeyOf shape');
    // buildLocalIndex keys by (tool, root, rel_path); directories are dropped.
    assert(idx.byKey['claude_code\x01memory\x01CLAUDE.md'].sha256 === 'aaaa', 'byKey lookup');
    assert(idx.byKey['claude_code\x01skills\x01x.md'].sha256 === 'sk11', 'byKey skills root');
    assert(idx.byKey['claude_code\x01commands\x01x.md'].sha256 === 'cm22', 'byKey commands root');
    assert(idx.byKey['claude_code\x01skills\x01x/'] === undefined, 'dirs excluded');
    assert(idx.byPath['notes.md'].tool === 'custom', 'byPath fallback');

    // compareState: same / missing / local_newer / remote_newer.
    assert(H.compareState(idx, E('claude_code', 'memory', 'CLAUDE.md', 'aaaa', 1000)) === 'same', 'same');
    assert(H.compareState(idx, E('claude_code', 'memory', 'NEW.md', 'dddd', 1)) === 'missing', 'missing');
    assert(H.compareState(idx, E('claude_code', 'memory', 'CLAUDE.md', 'zzzz', 500)) === 'local_newer', 'local_newer');
    assert(H.compareState(idx, E('claude_code', 'memory', 'CLAUDE.md', 'zzzz', 5000)) === 'remote_newer', 'remote_newer');
    // Equal mtime with differing hash falls through to "remote is newer".
    assert(H.compareState(idx, E('claude_code', 'memory', 'CLAUDE.md', 'zzzz', 1000)) === 'remote_newer', 'equal mtime');
    // Each root compares against ITS OWN local file, never the sibling root's.
    assert(H.compareState(idx, E('claude_code', 'skills', 'x.md', 'sk11', 1000)) === 'same', 'skills root same');
    assert(H.compareState(idx, E('claude_code', 'commands', 'x.md', 'sk11', 1000)) !== 'same', 'commands root not same');
    // A root this device does not watch is "missing", NOT a byPath match
    // against a same-named file under some other root.
    assert(H.compareState(idx, E('claude_code', 'agents', 'x.md', 'sk11', 1000)) === 'missing', 'unknown root missing');
    // Directory rows and an unloaded local index produce no badge.
    assert(H.compareState(idx, { tool: 'claude_code', root: 'skills', rel_path: 'x/', is_dir: true }) === null, 'dir null');
    assert(H.compareState(null, E('claude_code', 'memory', 'CLAUDE.md', 'aaaa', 1000)) === null, 'no local null');

    // mtime unit invariance: seconds below ~1e12, anything bigger is ms.
    const msIdx = H.buildLocalIndex([E('custom', 'r0', 'a.md', 's1', 2000000000000)]);
    assert(H.compareState(msIdx, E('custom', 'r0', 'a.md', 's2', 1500000000000)) === 'local_newer', 'ms local_newer');
    assert(H.compareState(msIdx, E('custom', 'r0', 'a.md', 's2', 3000000000000)) === 'remote_newer', 'ms remote_newer');

    // A legacy remote row (root_index) compares against local by rel_path only.
    assert(H.compareState(idx, { root_index: 0, rel_path: 'notes.md', sha256: 'cccc', mtime: 1 }, true) === 'same', 'legacy same');
    // A v2 remote sends no root at all, so it still falls back to byPath.
    assert(H.compareState(idx, { tool: 'codex', rel_path: 'notes.md', sha256: 'cccc', mtime: 1 }) === 'same', 'byPath fallback');

    // diffCounts aggregates the four states.
    const cnt = H.diffCounts(idx, [
      E('claude_code', 'memory', 'CLAUDE.md', 'aaaa', 1),        // same
      E('claude_code', 'memory', 'NEW.md', 'x', 1),             // missing
      E('claude_code', 'settings', 'settings.json', 'zzzz', 1),   // local_newer
      E('claude_code', 'settings', 'settings.json', 'zzzz', 9e6), // remote_newer
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
    helpers = os.path.join(_STATIC, "js", "aiconfig-helpers.js")
    # aiconfig-helpers delegates fmtSize/fmtSpeed to the ClipsyncFormat global
    # in format.js; hand both to the script (as argv[2] and argv[3]) so the
    # Node run sees the same global the browser does.
    fmt = os.path.join(_STATIC, "js", "format.js")
    proc = subprocess.run(
        [shutil.which("node"), str(script), helpers, fmt], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"helpers script failed:\n{proc.stdout}\n{proc.stderr}"
    assert "ALL_OK" in proc.stdout
