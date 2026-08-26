/* ═══════════════════════════════════════════════════════════════════
   ClipSync AI-Config shared helpers (refactor round 1)

   The AI-config panels used to each carry their own fmtSize / fmtTime /
   mtime-threshold / selection-key logic (three near-identical copies in
   aiconfig-panel, aiconfig-device-panel, and the migration code).  That
   all lives here now, plus the diff-state computation every view shares.

   Pure functions — no store / Vue dependency, safe to unit-test directly
   (window.__CLIPSYNC_AICONFIG_HELPERS__).
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // Selection/compare keys join tool + rel_path.  Keys are only ever matched
  // against entries via keyOf() (never parsed back), so any separator works;
  //  cannot appear in a tool key or (realistically) a path.
  var KEY_SEP = '';

  function fmtSize(n) {
    var v = Number(n);
    if (!isFinite(v) || v < 0) return '';
    if (v < 1024) return v + ' B';
    if (v < 1048576) return (v / 1024).toFixed(1) + ' KB';
    if (v < 1073741824) return (v / 1048576).toFixed(1) + ' MB';
    return (v / 1073741824).toFixed(2) + ' GB';
  }

  // mtime units are whatever the backend serializes — treat values below
  // ~year 33658 in seconds, anything bigger as milliseconds already.
  function fmtTime(v) {
    if (v === undefined || v === null || v === '') return '';
    var num = Number(v);
    if (!isFinite(num)) return '';
    var ms = num > 1e12 ? num : num * 1000;
    var d = new Date(ms);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleString();
  }

  // Normalise an mtime to epoch-milliseconds for NEWER/OLDER comparison.
  // Same unit convention as fmtTime(): seconds below ~year 33658, anything
  // bigger is already milliseconds.  Returns -1 when absent/unparseable so a
  // missing side never crashes the comparison.
  function mtimeMs(v) {
    if (v === undefined || v === null || v === '') return -1;
    var num = Number(v);
    if (!isFinite(num)) return -1;
    return num > 1e12 ? num : num * 1000;
  }

  function keyOf(entry) {
    return String((entry && entry.tool) || 'custom') +
      KEY_SEP + String((entry && entry.rel_path) || '');
  }

  function legacyKeyOf(entry) {
    return 'legacy' + KEY_SEP + String((entry && entry.root_index) || 0) +
      KEY_SEP + String((entry && entry.rel_path) || '');
  }

  /**
   * Index local entries for diff comparison.
   *   byKey   v2 entries by (tool, rel_path)
   *   byPath  every file entry by rel_path (fallback match, and what legacy
   *           remote entries compare against — root indices are per-device
   *           and mean nothing on the local side)
   * Directory entries are excluded: a folder compares through its files, and
   * an empty-sha256 folder row would otherwise always read "same".
   * @returns {{byKey: Object, byPath: Object}}
   */
  function buildLocalIndex(entries) {
    var byKey = {};
    var byPath = {};
    var list = Array.isArray(entries) ? entries : [];
    for (var i = 0; i < list.length; i++) {
      var e = list[i];
      if (!e || typeof e !== 'object') continue;
      if (e.is_dir) continue;
      var rel = String(e.rel_path || '');
      if (!rel) continue;
      if (!(rel in byPath)) byPath[rel] = e;
      if (e.tool) {
        var k = keyOf(e);
        if (!(k in byKey)) byKey[k] = e;
      }
    }
    return { byKey: byKey, byPath: byPath };
  }

  /**
   * Version-badge state for one remote row vs the local index.
   *   missing       local has no file with the same path
   *   same          same content (sha256)
   *   local_newer   differs AND local mtime is newer
   *   remote_newer  differs AND remote mtime is newer/equal
   * Returns null when the row is a directory or no local index is ready —
   * no badge is shown rather than guessing.
   */
  function compareState(local, entry, legacy) {
    if (!entry || entry.is_dir) return null;
    // Local inventory not loaded yet — return null rather than treating every
    // remote entry as "missing" (which would flash a bogus badge before the
    // local list settles).
    if (!local) return null;
    var rel = String(entry.rel_path || entry.path || '');
    var localEntry = legacy
      ? (local && local.byPath || {})[rel]
      : ((local && local.byKey || {})[keyOf(entry)] || (local && local.byPath || {})[rel]);
    if (!localEntry) return 'missing';
    if (String(localEntry.sha256 || '') === String(entry.sha256 || '')) return 'same';
    return (mtimeMs(localEntry.mtime) > mtimeMs(entry.mtime))
      ? 'local_newer' : 'remote_newer';
  }

  /** Aggregate diff counts across one peer's entry list. */
  function diffCounts(local, entries, legacy) {
    var out = { missing: 0, local_newer: 0, remote_newer: 0, total: 0 };
    var list = Array.isArray(entries) ? entries : [];
    for (var i = 0; i < list.length; i++) {
      var st = compareState(local, list[i], legacy);
      if (st === 'missing') out.missing++;
      else if (st === 'local_newer') out.local_newer++;
      else if (st === 'remote_newer') out.remote_newer++;
    }
    out.total = out.missing + out.local_newer + out.remote_newer;
    return out;
  }

  /** Human label for a tool key from the profile table (falls back to key). */
  function toolLabel(profileTools, key) {
    var tools = Array.isArray(profileTools) ? profileTools : [];
    for (var i = 0; i < tools.length; i++) {
      if (tools[i] && tools[i].key === key) return tools[i].label || key;
    }
    return key;
  }

  window.__CLIPSYNC_AICONFIG_HELPERS__ = {
    KEY_SEP: KEY_SEP,
    fmtSize: fmtSize,
    fmtTime: fmtTime,
    mtimeMs: mtimeMs,
    keyOf: keyOf,
    legacyKeyOf: legacyKeyOf,
    buildLocalIndex: buildLocalIndex,
    compareState: compareState,
    diffCounts: diffCounts,
    toolLabel: toolLabel,
  };

})();
