/* ═══════════════════════════════════════════════════════════════════
   ClipSync AI-Config Panel (refactor round 1 — unified tab)

   The 「AI 配置」tab is now a single view with the cross-device flow as the
   hero:
     · device bar at the top — THIS device + every paired peer, each with a
       diff-count badge (files that differ from this machine)
     · selecting a peer shows its inventory grouped by TOOL PROFILE
       (Claude Code / Codex / Cursor / Gemini / custom), with per-file
       version badges vs this device's local copies
     · folders are real, pullable units: checking a folder checks every
       descendant file and pull() sends them as one batched request
       (batch_id → WS aiconfig_file events → "N/M done" progress)
     · 「一键迁移」opens the migration wizard (aiconfig-migrate-panel)
     · selecting THIS device shows the local manager — browse / preview /
       edit / trash / open over this machine's own tool-profile roots.
       Profile management (which tools are synced) lives in Settings.

   Landing modes — never a silent overwrite:
     overwrite  replace the local same-name file
     copy       save under a copy name, local files untouched (default)
     append     append the text to the local same-name .md file

   Legacy peers (old backend, no `v:2` inventories): browse + preview only,
   pull and migration are blocked (their root indices map to nothing on this
   side deterministically).
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  var H = window.__CLIPSYNC_AICONFIG_HELPERS__ || {};

  // "append" mode only lands on text/markdown files (mirrors the backend's
  // APPEND_EXTS: .txt / .md / .markdown).
  var TEXT_EXT_RE = /\.(md|markdown|txt)$/i;

  // Tree-node keys join tool + path; '' cannot appear in either.
  var NODE_SEP = '';
  function nodeKey(tool, path) {
    return String(tool || 'custom') + NODE_SEP + String(path || '');
  }

  var MODES = [
    { value: 'overwrite', labelKey: 'aiconfig.mode_overwrite', hintKey: 'aiconfig.mode_overwrite_hint' },
    { value: 'copy', labelKey: 'aiconfig.mode_copy', hintKey: 'aiconfig.mode_copy_hint' },
    { value: 'append', labelKey: 'aiconfig.mode_append', hintKey: 'aiconfig.mode_append_hint' },
  ];

  window.__CLIPSYNC_COMPONENTS__['aiconfig-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        selectedDevice: 'local',   // 'local' or a peer_id
        peerSearch: '',
        localSearch: '',
        mode: 'copy',              // default = never clobber silently
        modes: MODES,
        checked: {},               // selection key -> true (peer view)
        pulling: false,
        activeBatchId: '',
        collapsed: {},             // peer tree nodes explicitly collapsed
        localCollapsed: {},        // local tree nodes explicitly collapsed
        preview: {
          visible: false, loading: false, failed: false,
          relPath: '', tool: '', rootIndex: 0, content: '', truncated: false,
        },
        localPreview: {
          visible: false, loading: false, failed: false, error: '',
          editing: false, saving: false, tool: '', relPath: '',
          content: '', truncated: false,
        },
      };
    },

    computed: {
      /* ── Devices ──────────────────────────────────────────────── */
      peersList: function () {
        var peers = (this.store.aiConfigInventory && this.store.aiConfigInventory.peers) || {};
        return Object.keys(peers).map(function (pid) {
          var p = peers[pid];
          return {
            id: pid,
            name: p.name || pid,
            legacy: !!p.legacy,
            entries: Array.isArray(p.entries) ? p.entries : [],
            fetchedAt: p.fetchedAt || p.fetched_at || '',
          };
        }).sort(function (a, b) {
          return a.name.localeCompare(b.name);
        });
      },

      localIndex: function () {
        var entries = (this.store.aiConfigLocal && this.store.aiConfigLocal.entries) || [];
        return H.buildLocalIndex(entries);
      },

      // Device bar: THIS device first, then every paired peer with its diff
      // badge (files that differ from the local snapshot).  The badge is
      // only meaningful once the local inventory has loaded.
      devices: function () {
        var self = this;
        var out = [{
          id: 'local',
          name: this.t('aiconfig.local_device'),
          isLocal: true,
          legacy: false,
          diffTotal: 0,
        }];
        this.peersList.forEach(function (p) {
          var d = self.store.aiConfigLocal.loaded
            ? H.diffCounts(self.localIndex, p.entries, p.legacy)
            : { total: 0, missing: 0, remote_newer: 0, local_newer: 0 };
          out.push({
            id: p.id,
            name: p.name,
            isLocal: false,
            legacy: p.legacy,
            diffTotal: d.total,
            diffMissing: d.missing,
            diffRemoteNewer: d.remote_newer,
            diffLocalNewer: d.local_newer,
          });
        });
        return out;
      },

      currentIsLocal: function () {
        return this.selectedDevice === 'local';
      },

      currentPeer: function () {
        var id = this.selectedDevice;
        var list = this.peersList;
        for (var i = 0; i < list.length; i++) {
          if (list[i].id === id) return list[i];
        }
        return null;
      },

      deviceDiff: function () {
        var peer = this.currentPeer;
        if (!peer || !this.store.aiConfigLocal.loaded) return null;
        return H.diffCounts(this.localIndex, peer.entries, peer.legacy);
      },

      /* ── Tool-grouped rows (shared by peer + local views) ─────── */
      groups: function () {
        if (this.currentIsLocal) {
          var localEntries = (this.store.aiConfigLocal && this.store.aiConfigLocal.entries) || [];
          return this.groupedRows(localEntries, this.localCollapsed, this.localSearch);
        }
        var peer = this.currentPeer;
        if (!peer) return [];
        return this.groupedRows(peer.entries, this.collapsed, this.peerSearch);
      },

      /* ── Peer selection / pull ────────────────────────────────── */
      byKey: function () {
        var map = {};
        var peer = this.currentPeer;
        if (!peer) return map;
        peer.entries.forEach(function (e) { map[H.keyOf(e)] = e; });
        return map;
      },

      selectedCount: function () {
        var self = this;
        return Object.keys(this.checked).filter(function (k) { return self.checked[k]; }).length;
      },

      allSelected: function () {
        var peer = this.currentPeer;
        if (!peer) return false;
        var saw = false;
        for (var i = 0; i < peer.entries.length; i++) {
          var e = peer.entries[i];
          if (e.is_dir) continue;
          saw = true;
          if (!this.checked[H.keyOf(e)]) return false;
        }
        return saw;
      },

      modeHint: function () {
        for (var i = 0; i < MODES.length; i++) {
          if (MODES[i].value === this.mode) return this.t(MODES[i].hintKey);
        }
        return '';
      },

      activeBatch: function () {
        if (!this.activeBatchId) return null;
        return this.store.aiConfigBatches[this.activeBatchId] || null;
      },

      batchPct: function () {
        var b = this.activeBatch;
        if (!b || !(b.total > 0)) return 0;
        return Math.min(100, Math.round(b.done / b.total * 100));
      },

      batchErrorCount: function () {
        var b = this.activeBatch;
        if (!b || !Array.isArray(b.results)) return 0;
        var n = 0;
        for (var i = 0; i < b.results.length; i++) {
          if (b.results[i] && b.results[i].status === 'error') n++;
        }
        return n;
      },

      /* ── Local view helpers ───────────────────────────────────── */
      localNoPaths: function () {
        var l = this.store.aiConfigLocal;
        return !!l.loaded && !l.loadFailed && !(l.roots || []).length;
      },

      localPreviewError: function () {
        return this.localErrorMessage(this.localPreview.error);
      },
    },

    watch: {
      // Follow the inventory: fall back to THIS device when the selected
      // peer disappears from the refreshed list; drop the per-peer selection.
      peersList: function (list) {
        if (this.selectedDevice === 'local') return;
        var found = false;
        for (var i = 0; i < list.length; i++) {
          if (list[i].id === this.selectedDevice) { found = true; break; }
        }
        if (!found) {
          this.selectedDevice = 'local';
          this.checked = {};
          this.activeBatchId = '';
        }
      },
    },

    created: function () {
      var self = this;
      this._onKeyDown = function (e) {
        if (e.key !== 'Escape') return;
        if (this.preview.visible) { this.closePreview(); return; }
        if (this.localPreview.visible) { this.closeLocalPreview(); return; }
        if (this.store.aiConfigMigrateOpen) { this.store.closeAiConfigMigrate(); }
      }.bind(this);
    },

    mounted: function () {
      document.addEventListener('keydown', this._onKeyDown);
      // Prime the three AI-config data sources.  The tab is the only place
      // that uses them, so fetch on first open rather than at app start.
      if (!this.store.aiConfigLocal.loaded) this.store.fetchAiConfigLocal();
      if (!this.store.aiConfigLoaded) this.store.fetchAiConfigInventory(false);
      if (!this.store.aiConfigProfilesLoaded) this.store.fetchAiConfigProfiles();
    },

    beforeUnmount: function () {
      document.removeEventListener('keydown', this._onKeyDown);
    },

    methods: {
      fmtSize: H.fmtSize,
      fmtTime: H.fmtTime,
      mtimeMs: H.mtimeMs,

      /* ── Device bar ───────────────────────────────────────────── */
      selectDevice: function (id) {
        if (id === this.selectedDevice) return;
        this.selectedDevice = id;
        this.checked = {};      // selection belongs to one device
        this.collapsed = {};
        this.activeBatchId = '';
      },

      refreshPeers: function (force) {
        return this.store.fetchAiConfigInventory(!!force);
      },

      refreshLocal: function () {
        return this.store.fetchAiConfigLocal();
      },

      /* ── Tree rows (tool-grouped) ─────────────────────────────── */
      // Build the tool-grouped row list for one entry set.  Search flattens
      // matches to depth-0 rows across the matching groups.
      groupedRows: function (entries, collapsedMap, searchQ) {
        var profiles = (this.store.aiConfigProfiles && this.store.aiConfigProfiles.tools) || [];
        var order = [];
        var seen = {};
        profiles.forEach(function (p) {
          if (p && p.key && !seen[p.key]) { seen[p.key] = true; order.push(p.key); }
        });
        var groups = {};
        var groupOrder = [];
        var list = Array.isArray(entries) ? entries : [];
        list.forEach(function (e) {
          var t = String(e.tool || 'custom');
          if (!groups[t]) { groups[t] = []; groupOrder.push(t); }
          groups[t].push(e);
        });
        groupOrder.sort(function (a, b) {
          var ai = order.indexOf(a), bi = order.indexOf(b);
          ai = ai < 0 ? order.length : ai;
          bi = bi < 0 ? order.length : bi;
          return ai - bi || a.localeCompare(b);
        });
        var self = this;
        return groupOrder.map(function (t) {
          var files = groups[t];
          return {
            key: t,
            label: H.toolLabel(profiles, t),
            entries: files,
            fileCount: files.filter(function (e) { return !e.is_dir; }).length,
            rows: self.rowsForGroup(files, t, collapsedMap, searchQ),
          };
        });
      },

      rowsForGroup: function (entries, tool, collapsedMap, searchQ) {
        var out = [];
        var q = (searchQ || '').toLowerCase().trim();
        if (q) {
          entries.forEach(function (e) {
            if (String(e.rel_path || '').toLowerCase().indexOf(q) === -1) return;
            out.push({
              key: H.keyOf(e), label: e.rel_path, depth: 0,
              isDir: !!e.is_dir, entry: e, node: null, tool: tool,
            });
          });
          out.sort(function (a, b) { return a.label.localeCompare(b.label); });
          return out;
        }
        var nodes = {};
        var roots = [];
        entries.forEach(function (e) {
          var rel = String(e.rel_path || '').replace(/\/+$/, '');
          if (!rel) return;
          var parts = rel.split('/');
          var parent = null, leaf = null;
          for (var j = 0; j < parts.length; j++) {
            var key = nodeKey(tool, parts.slice(0, j + 1).join('/'));
            if (j === 0) {
              leaf = nodes[key];
              if (!leaf) {
                leaf = nodes[key] = {
                  key: key, tool: tool, name: parts[0], path: parts[0],
                  is_dir: parts.length > 1 || !!e.is_dir, entry: null, children: [],
                };
                roots.push(leaf);
              }
              parent = leaf;
            } else {
              var child = null;
              for (var c = 0; c < parent.children.length; c++) {
                if (parent.children[c].key === key) { child = parent.children[c]; break; }
              }
              if (!child) {
                child = {
                  key: key, tool: tool, name: parts[j], path: parts.slice(0, j + 1).join('/'),
                  is_dir: j < parts.length - 1 || !!e.is_dir, entry: null, children: [],
                };
                parent.children.push(child);
              }
              parent = child; leaf = child;
            }
          }
          if (leaf) { leaf.entry = e; leaf.is_dir = !!e.is_dir; }
        });
        var walk = function (nodesList, depth) {
          for (var i = 0; i < nodesList.length; i++) {
            var node = nodesList[i];
            out.push({
              key: node.key, label: node.name, depth: depth,
              isDir: node.is_dir, entry: node.entry, node: node, tool: node.tool,
            });
            if (node.is_dir && !collapsedMap[node.key]) walk(node.children, depth + 1);
          }
        };
        walk(roots, 0);
        return out;
      },

      isDirExpanded: function (node, map) {
        return !(node && map[node.key]);
      },

      toggleDir: function (node, map) {
        if (!node) return;
        var next = Object.assign({}, map);
        if (next[node.key]) delete next[node.key];
        else next[node.key] = true;
        if (map === this.collapsed) this.collapsed = next;
        else this.localCollapsed = next;
      },

      /* ── Version badges (peer rows vs local) ──────────────────── */
      verKey: function (st) {
        var keys = {
          missing: 'aiconfig.ver_missing',
          same: 'aiconfig.ver_same',
          local_newer: 'aiconfig.ver_local_newer',
          remote_newer: 'aiconfig.ver_remote_newer'
        };
        return keys[st] || '';
      },

      compareState: function (entry) {
        if (!this.store.aiConfigLocal.loaded) return null;
        var peer = this.currentPeer;
        return H.compareState(this.localIndex, entry, peer && peer.legacy);
      },

      compareTitle: function (entry) {
        var st = this.compareState(entry);
        if (!st) return '';
        var localByPath = this.localIndex.byPath || {};
        var local = localByPath[String((entry && entry.rel_path) || '')];
        var head = this.t(this.verKey(st));
        if (!local) return head;
        var side = function (e) {
          return H.fmtSize(e.size) + ' · ' + H.fmtTime(e.mtime);
        };
        return head + '\n' + this.t('aiconfig.ver_tooltip', {
          local: side(local),
          remote: side(entry),
        });
      },

      /* ── Selection / folder whole-select ──────────────────────── */
      isChecked: function (entry) {
        return !!this.checked[H.keyOf(entry)];
      },

      toggleCheck: function (entry) {
        var k = H.keyOf(entry);
        var next = Object.assign({}, this.checked);
        if (next[k]) delete next[k];
        else next[k] = true;
        this.checked = next;
      },

      toggleSelectAll: function () {
        var peer = this.currentPeer;
        if (!peer) return;
        if (this.allSelected) { this.checked = {}; return; }
        var next = {};
        peer.entries.forEach(function (e) {
          if (e.is_dir) return;
          next[H.keyOf(e)] = true;
        });
        this.checked = next;
      },

      // Descendant FILE selection keys under (tool, rel).  Folder selects
      // operate at file granularity: checking a folder checks every file the
      // peer's (recursive) inventory reports under it.
      descendantFileKeys: function (tool, rel) {
        var peer = this.currentPeer;
        if (!peer) return [];
        var prefix = String(rel || '').replace(/\/+$/, '') + '/';
        var keys = [];
        peer.entries.forEach(function (e) {
          if (e.is_dir) return;
          if (String(e.tool || 'custom') !== String(tool || 'custom')) return;
          var r = String(e.rel_path || '');
          if (r === rel || r.indexOf(prefix) === 0) keys.push(H.keyOf(e));
        });
        return keys;
      },

      folderState: function (tool, rel) {
        var keys = this.descendantFileKeys(tool, rel);
        var checkedN = 0;
        var self = this;
        keys.forEach(function (k) { if (self.checked[k]) checkedN++; });
        return {
          keys: keys,
          count: keys.length,
          checked: keys.length > 0 && checkedN === keys.length,
          partial: checkedN > 0 && checkedN < keys.length,
        };
      },

      toggleFolder: function (tool, rel) {
        var st = this.folderState(tool, rel);
        var target = !st.checked;
        var next = Object.assign({}, this.checked);
        st.keys.forEach(function (k) {
          if (target) next[k] = true;
          else delete next[k];
        });
        this.checked = next;
      },

      /* ── Pull (batched, per-file WS results) ──────────────────── */
      hasNonTextSelection: function (items) {
        for (var i = 0; i < items.length; i++) {
          if (!TEXT_EXT_RE.test(items[i].rel_path || '')) return true;
        }
        return false;
      },

      pull: function () {
        var self = this;
        var peer = this.currentPeer;
        if (!peer || peer.legacy || this.selectedCount === 0 || this.pulling) return;
        var items = [];
        var byKey = this.byKey;
        Object.keys(this.checked).forEach(function (k) {
          if (!self.checked[k]) return;
          var e = byKey[k];
          if (e && !e.is_dir) items.push({ tool: String(e.tool || 'custom'), rel_path: e.rel_path });
        });
        if (items.length === 0) return;
        if (this.mode === 'append' && this.hasNonTextSelection(items)) {
          this.store.showToast(this.t('aiconfig.append_ext_warning'), 3500, 'warning');
          return;
        }
        var batchId = 'aiconfig_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
        this.store.startAiConfigBatch(batchId, items.length, peer.id);
        this.activeBatchId = batchId;
        this.pulling = true;
        ClipsyncAPI.pullAiConfigFiles(peer.id, items, this.mode, batchId)
          .then(function (res) {
            self.pulling = false;
            var n = (res && typeof res.requested === 'number') ? res.requested : items.length;
            if (n > 0) {
              self.store.showToast(self.t('aiconfig.pull_requested', { count: n }), 3200, 'success');
              self.checked = {};
            } else {
              self.store.clearAiConfigBatch(batchId);
              self.activeBatchId = '';
              var reasons = (res && Array.isArray(res.errors)) ? res.errors : [];
              self.store.showToast(
                reasons.length
                  ? (self.t('aiconfig.pull_failed') + ' (' + reasons.join(', ') + ')')
                  : self.t('aiconfig.pull_failed'),
                3000, 'error');
            }
          })
          .catch(function (e) {
            self.pulling = false;
            self.store.clearAiConfigBatch(batchId);
            self.activeBatchId = '';
            var reasons = (e && e.data && Array.isArray(e.data.errors)) ? e.data.errors : [];
            self.store.showToast(
              reasons.length
                ? (self.t('aiconfig.pull_failed') + ' (' + reasons.join(', ') + ')')
                : self.t('aiconfig.pull_failed'),
              3000, 'error');
          });
      },

      dismissBatch: function () {
        if (this.activeBatchId) this.store.clearAiConfigBatch(this.activeBatchId);
        this.activeBatchId = '';
      },

      // Re-pull just the failed files of the active batch, as a fresh batch.
      retryFailures: function () {
        var self = this;
        var peer = this.currentPeer;
        var b = this.activeBatch;
        if (!peer || !b || !Array.isArray(b.results)) return;
        var items = [];
        b.results.forEach(function (r) {
          if (r && r.status === 'error' && r.rel_path) {
            items.push({ tool: String(r.tool || 'custom'), rel_path: r.rel_path });
          }
        });
        if (items.length === 0) return;
        var batchId = 'aiconfig_' + Date.now() + '_r_' + Math.random().toString(36).slice(2, 8);
        this.store.startAiConfigBatch(batchId, items.length, peer.id);
        this.activeBatchId = batchId;
        ClipsyncAPI.pullAiConfigFiles(peer.id, items, this.mode, batchId)
          .catch(function (e) {
            self.store.clearAiConfigBatch(batchId);
            if (self.activeBatchId === batchId) self.activeBatchId = '';
            self.store.showToast(self.t('aiconfig.pull_failed'), 3000, 'error');
          });
      },

      /* ── Pull-result badges ───────────────────────────────────── */
      resultFor: function (entry) {
        var list = this.store.aiConfigResults || [];
        var id = this.selectedDevice;
        for (var i = 0; i < list.length; i++) {
          var r = list[i];
          if (r.peer_id === id && r.rel_path === entry.rel_path) return r;
        }
        return null;
      },

      statusGlyph: function (status) {
        switch (status) {
          case 'saved': return '✓';
          case 'copied': return '⧉';
          case 'appended': return '+';
          default: return '✕';
        }
      },

      statusTitle: function (result) {
        if (!result) return '';
        var key = result.status === 'error' ? 'aiconfig.result_error'
          : result.status === 'copied' ? 'aiconfig.result_copied'
          : result.status === 'appended' ? 'aiconfig.result_appended'
          : 'aiconfig.result_saved';
        return this.t(key, { path: result.rel_path });
      },

      /* ── Peer preview (v2 vs legacy) ──────────────────────────── */
      openPreview: function (entry) {
        var self = this;
        var peer = this.currentPeer;
        if (!peer) return;
        this.preview = {
          visible: true, loading: true, failed: false,
          relPath: entry.rel_path, tool: entry.tool, rootIndex: entry.root_index,
          content: '', truncated: false,
        };
        var p = peer.legacy
          ? ClipsyncAPI.previewAiConfigLegacy(peer.id, entry.root_index, entry.rel_path)
          : ClipsyncAPI.previewAiConfigFile(peer.id, String(entry.tool || 'custom'), entry.rel_path);
        p.then(function (res) {
          self.preview.loading = false;
          self.preview.content = (res && res.content) || '';
          self.preview.truncated = !!(res && res.truncated);
        }).catch(function () {
          self.preview.loading = false;
          self.preview.failed = true;
        });
      },

      closePreview: function () {
        this.preview.visible = false;
      },

      /* ── Migration wizard ─────────────────────────────────────── */
      openMigrate: function () {
        this.store.openAiConfigMigrate();
      },

      /* ── Local manager (THIS device) ──────────────────────────── */
      localErrorMessage: function (rawErr) {
        var err = String(rawErr || '').trim();
        if (!err) return this.t('aiconfig.preview_failed');
        if (err.toLowerCase().indexOf('binary') !== -1) {
          return this.t('aiconfig.local_preview_binary');
        }
        return this.t('aiconfig.local_preview_failed', { reason: err });
      },

      localSkillCount: function () {
        var entries = (this.store.aiConfigLocal && this.store.aiConfigLocal.entries) || [];
        var n = 0;
        for (var i = 0; i < entries.length; i++) {
          if (entries[i] && entries[i].is_dir) n++;
        }
        return n;
      },

      openSettingsAiconfig: function () {
        this.store.settingsRequestedSection = 'aiconfig';
        this.store.openSettingsPanel();
      },

      openLocalPreview: function (entry) {
        var self = this;
        if (entry && entry.is_dir) {
          this.openEntryDir(entry);
          return;
        }
        this.localPreview = {
          visible: true, loading: true, failed: false, error: '',
          editing: false, saving: false,
          tool: String(entry.tool || 'custom'), relPath: entry.rel_path,
          content: '', truncated: false,
        };
        ClipsyncAPI.getAiConfigLocalItem(entry.tool, entry.rel_path)
          .then(function (res) {
            if (!res || !res.ok) {
              self.localPreview.loading = false;
              self.localPreview.failed = true;
              self.localPreview.error = (res && res.error) || '';
              self.store.showToast(self.localErrorMessage(self.localPreview.error), 3200, 'error');
              return;
            }
            self.localPreview.loading = false;
            self.localPreview.content = (res && res.content) || '';
            self.localPreview.truncated = !!(res && res.truncated);
          })
          .catch(function (e) {
            self.localPreview.loading = false;
            self.localPreview.failed = true;
            self.localPreview.error = (e && e.message) ? e.message : '';
            self.store.showToast(self.localErrorMessage(self.localPreview.error), 3200, 'error');
          });
      },

      closeLocalPreview: function () {
        this.localPreview.visible = false;
      },

      startLocalEdit: function () {
        this.localPreview.editing = true;
      },

      cancelLocalEdit: function () {
        this.localPreview.editing = false;
      },

      saveLocalEdit: function () {
        var self = this;
        var p = this.localPreview;
        if (p.saving) return;
        p.saving = true;
        ClipsyncAPI.saveAiConfigLocal(p.tool, p.relPath, p.content)
          .then(function (res) {
            p.saving = false;
            if (res && res.ok) {
              self.store.showToast(self.t('aiconfig.local_saved_toast'), 3000, 'success');
              p.editing = false;
              self.store.fetchAiConfigLocal();
            } else {
              self.store.showToast(self.t('aiconfig.local_save_failed',
                { reason: (res && res.error) || '' }), 3200, 'error');
            }
          })
          .catch(function (e) {
            p.saving = false;
            self.store.showToast(self.t('aiconfig.local_save_failed',
              { reason: (e && e.message) || '' }), 3200, 'error');
          });
      },

      trashEntry: function (entry) {
        var self = this;
        this.store.confirm(
          this.t('aiconfig.local_trash_title'),
          this.t('aiconfig.local_trash_confirm', { path: entry.rel_path })
        ).then(function () {
          return ClipsyncAPI.trashAiConfigLocal(entry.tool, entry.rel_path);
        }).then(function (res) {
          if (res && res.ok) {
            var dest = (res && res.trashed_to) || '';
            self.store.showToast(self.t('aiconfig.local_trashed_toast', { dest: dest }), 3200, 'success');
            var prefix = String(entry.rel_path || '').replace(/\/+$/, '') + '/';
            var cur = self.store.aiConfigLocal.entries.slice();
            for (var i = cur.length - 1; i >= 0; i--) {
              var rp = String(cur[i].rel_path || '');
              if (cur[i].tool === entry.tool &&
                  (rp === entry.rel_path ||
                   (entry.is_dir && rp.indexOf(prefix) === 0))) {
                cur.splice(i, 1);
              }
            }
            self.store.aiConfigLocal.entries = cur;
            if (self.localPreview.visible &&
                self.localPreview.tool === entry.tool &&
                (self.localPreview.relPath === entry.rel_path ||
                 (entry.is_dir &&
                  String(self.localPreview.relPath || '').indexOf(prefix) === 0))) {
              self.closeLocalPreview();
            }
          } else {
            self.store.showToast(self.t('aiconfig.local_trash_failed',
              { reason: (res && res.error) || '' }), 3200, 'error');
          }
        }).catch(function (err) {
          if (err && typeof err === 'object') {
            self.store.showToast(self.t('aiconfig.local_trash_failed',
              { reason: (err && err.message) || '' }), 3200, 'error');
          }
        });
      },

      openEntryDir: function (entry) {
        var self = this;
        ClipsyncAPI.openAiConfigLocal(entry.tool, entry.rel_path)
          .then(function (res) {
            if (!res || !res.ok) {
              self.store.showToast(self.t('aiconfig.local_open_failed',
                { reason: (res && res.error) || '' }), 3000, 'error');
            }
          })
          .catch(function (e) {
            self.store.showToast(self.t('aiconfig.local_open_failed',
              { reason: (e && e.message) || '' }), 3000, 'error');
          });
      },

      onRowClick: function (row) {
        if (!row) return;
        if (row.isDir) {
          if (row.node) {
            this.toggleDir(row.node, this.currentIsLocal ? this.localCollapsed : this.collapsed);
          }
        } else if (row.entry) {
          this.openLocalPreview(row.entry);
        }
      },

      deviceTooltip: function (dev) {
        if (dev.isLocal) return this.t('aiconfig.local_device_hint');
        var parts = [];
        if (dev.legacy) parts.push(this.t('aiconfig.legacy_peer'));
        if (this.store.aiConfigLocal.loaded && dev.diffTotal > 0) {
          parts.push(this.t('aiconfig.diff_summary', {
            missing: dev.diffMissing, remote: dev.diffRemoteNewer, local: dev.diffLocalNewer,
          }));
        }
        return parts.join('\n');
      },
    },

    template:
      '<div class="aiconfig-panel" style="overflow-y:auto">' +

        '<!-- Header -->' +
        '<div class="favorites-panel__header">' +
          '<div class="favorites-panel__header-left">' +
            '<span class="favorites-panel__header-icon">🤖</span>' +
            '<span class="favorites-panel__header-title">{{ t(\'ui.aiconfig\') }}</span>' +
          '</div>' +
          '<div class="aiconfig-panel__header-actions">' +
            '<button class="settings-btn settings-btn--accent settings-btn--sm" style="white-space:nowrap" @click="openMigrate">🪄 {{ t(\'aiconfig.migrate_title\') }}</button>' +
            '<button class="settings-btn settings-btn--sm" style="white-space:nowrap" @click="refreshPeers(true)" :disabled="store.aiConfigRefreshing">' +
              '{{ store.aiConfigRefreshing ? \'...\' : (\'🔄 \' + t(\'ui.refresh\')) }}' +
            '</button>' +
          '</div>' +
        '</div>' +

        '<!-- Device bar: THIS device + every paired peer, diff badges -->' +
        '<div class="aiconfig-panel__hero glass">' +
          '<button v-for="dev in devices" :key="dev.id"' +
            ' class="aiconfig-panel__device-pill"' +
            ' :class="{ \'aiconfig-panel__device-pill--active\': dev.id === selectedDevice }"' +
            ' @click="selectDevice(dev.id)"' +
            ' :title="dev.isLocal ? t(\'aiconfig.local_device_hint\') : deviceTooltip(dev)">' +
            '<span class="aiconfig-panel__device-pill-name">{{ dev.isLocal ? \'🏠 \' : \'💻 \' }}{{ dev.name }}</span>' +
            '<span v-if="dev.legacy" class="aiconfig-panel__device-pill-tag aiconfig-panel__device-pill-tag--legacy">{{ t(\'aiconfig.legacy_peer\') }}</span>' +
            '<span v-if="!dev.isLocal && dev.diffTotal > 0" class="aiconfig-panel__device-pill-badge"' +
              ' :class="{ \'aiconfig-panel__device-pill-badge--warn\': dev.diffRemoteNewer > 0 }"' +
              '>{{ dev.diffTotal }}</span>' +
          '</button>' +
        '</div>' +

        ('' + // ── PEER VIEW ─────────────────────────────────────────
        '<template v-if="!currentIsLocal">') +

          '<div v-if="!store.aiConfigLoaded" class="panel-empty">' +
            '<div class="panel-empty-title">{{ t(\'ui.loading\') }}</div>' +
          '</div>' +

          '<div v-else-if="!currentPeer" class="panel-empty">' +
            '<div class="panel-empty-icon">🤖</div>' +
            '<div class="panel-empty-title">{{ t(\'aiconfig.empty_title\') }}</div>' +
            '<div class="panel-empty-desc">{{ store.aiConfigLoadFailed ? t(\'aiconfig.load_failed\') : t(\'aiconfig.empty_desc\') }}</div>' +
            '<button class="settings-btn settings-btn--sm" style="margin-top:10px" @click="refreshPeers(true)" :disabled="store.aiConfigRefreshing">{{ t(\'ui.refresh\') }}</button>' +
          '</div>' +

          '<template v-else>' +
            '<div v-if="currentPeer.legacy" class="aiconfig-panel__notice glass">🔒 {{ t(\'aiconfig.legacy_read_only\') }}</div>' +

            '<div v-if="deviceDiff" class="aiconfig-panel__diff-summary glass">' +
              '<span v-if="deviceDiff.total === 0" class="aiconfig-panel__diff-line">✅ {{ t(\'aiconfig.diff_synced\') }}</span>' +
              '<span v-else class="aiconfig-panel__diff-line">{{ t(\'aiconfig.diff_summary\', { missing: deviceDiff.missing, remote: deviceDiff.remote_newer, local: deviceDiff.local_newer }) }}</span>' +
            '</div>' +

            '<div class="aiconfig-panel__toolbar">' +
              '<input type="search" class="aiconfig-panel__search" v-model="peerSearch"' +
                ' :placeholder="t(\'aiconfig.search_placeholder\')" :aria-label="t(\'aiconfig.search_placeholder\')">' +
              '<label class="aiconfig-panel__selectall">' +
                '<input type="checkbox" :checked="allSelected" @change="toggleSelectAll"> {{ t(\'aiconfig.select_all\') }}' +
              '</label>' +
            '</div>' +

            '<div v-for="g in groups" :key="g.key" class="aiconfig-panel__tool-section glass">' +
              '<div class="aiconfig-panel__tool-section-head">' +
                '<span class="aiconfig-panel__tool-section-title">{{ g.label }}</span>' +
                '<span class="aiconfig-panel__tool-section-count">{{ g.fileCount }}</span>' +
              '</div>' +
              '<div class="aiconfig-panel__tablewrap">' +
                '<table class="aiconfig-panel__table">' +
                  '<thead>' +
                    '<tr>' +
                      '<th class="aiconfig-panel__th-check"></th>' +
                      '<th>{{ t(\'aiconfig.col_file\') }}</th>' +
                      '<th>{{ t(\'aiconfig.col_size\') }}</th>' +
                      '<th>{{ t(\'aiconfig.col_time\') }}</th>' +
                      '<th class="aiconfig-panel__th-status"></th>' +
                    '</tr>' +
                  '</thead>' +
                  '<tbody>' +
                    '<tr v-for="row in g.rows" :key="row.key">' +
                      '<td class="aiconfig-panel__td-check">' +
                        '<span v-if="row.isDir && row.node" class="aiconfig-panel__tree-chevron" :class="{ \'aiconfig-panel__tree-chevron--open\': isDirExpanded(row.node, collapsed) }" role="button" tabindex="0" :aria-label="row.label" :aria-expanded="isDirExpanded(row.node, collapsed)" @click.stop="toggleDir(row.node, collapsed)" @keydown.enter.space.stop.prevent="toggleDir(row.node, collapsed)">▸</span>' +
                        '<span v-else class="aiconfig-panel__tree-chevron aiconfig-panel__tree-chevron--spacer"></span>' +
                        '<span v-if="row.isDir && row.node" class="aiconfig-panel__folder-check">' +
                          '<input type="checkbox" :checked="folderState(row.tool, row.node.path).checked"' +
                            ' @change="toggleFolder(row.tool, row.node.path)"' +
                            ' :title="folderState(row.tool, row.node.path).count ? t(\'aiconfig.folder_select_count\', { count: folderState(row.tool, row.node.path).count }) : \'\'"' +
                            ' :aria-label="t(\'aiconfig.folder_select_count\', { count: folderState(row.tool, row.node.path).count })">' +
                        '</span>' +
                        '<input v-else-if="!row.isDir" type="checkbox" :checked="isChecked(row.entry)" @change="toggleCheck(row.entry)" :aria-label="row.entry.rel_path">' +
                      '</td>' +
                      '<td class="aiconfig-panel__cell-path" :style="row.depth ? { paddingLeft: (12 + row.depth * 18) + \'px\' } : {}">' +
                        '<button v-if="row.isDir && row.node" class="aiconfig-panel__path-btn aiconfig-panel__path-btn--dir selectable" @click="toggleDir(row.node, collapsed)" :title="t(\'aiconfig.local_open_dir\')">📁 {{ row.label }}</button>' +
                        '<span v-else-if="row.isDir" class="aiconfig-panel__path-btn aiconfig-panel__path-btn--dir selectable">📁 {{ row.label }}</span>' +
                        '<template v-else>' +
                          '<button class="aiconfig-panel__path-btn selectable" @click="openPreview(row.entry)" :title="t(\'aiconfig.preview_title\')">{{ row.label }}</button>' +
                          '<span v-if="compareState(row.entry)" class="aiconfig-panel__ver-badge"' +
                            ' :class="\'aiconfig-panel__ver-badge--\' + compareState(row.entry)"' +
                            ' :title="compareTitle(row.entry)">{{ t(verKey(compareState(row.entry))) }}</span>' +
                        '</template>' +
                      '</td>' +
                      '<td class="aiconfig-panel__cell-size">{{ row.isDir ? \'\' : fmtSize(row.entry.size) }}</td>' +
                      '<td class="aiconfig-panel__cell-time">{{ row.isDir ? \'\' : fmtTime(row.entry.mtime) }}</td>' +
                      '<td class="aiconfig-panel__cell-status">' +
                        '<span v-if="!row.isDir && resultFor(row.entry)" class="aiconfig-panel__badge"' +
                          ' :class="\'aiconfig-panel__badge--\' + resultFor(row.entry).status"' +
                          ' :title="statusTitle(resultFor(row.entry))">{{ statusGlyph(resultFor(row.entry).status) }}</span>' +
                      '</td>' +
                    '</tr>' +
                    '<tr v-if="g.rows.length === 0">' +
                      '<td colspan="5" class="aiconfig-panel__none">{{ t(\'aiconfig.no_match\') }}</td>' +
                    '</tr>' +
                  '</tbody>' +
                '</table>' +
              '</div>' +
            '</div>' +

            '<div v-if="groups.length === 0 && !currentPeer.legacy" class="panel-empty">' +
              '<div class="panel-empty-icon">📁</div>' +
              '<div class="panel-empty-title">{{ t(\'aiconfig.peer_empty\') }}</div>' +
            '</div>' +

            '<!-- Action bar + batch progress -->' +
            '<div v-if="!currentPeer.legacy" class="aiconfig-panel__actions glass">' +
              '<div class="aiconfig-panel__modes">' +
                '<span class="aiconfig-panel__modes-label">{{ t(\'aiconfig.mode_label\') }}</span>' +
                '<label v-for="m in modes" :key="m.value" class="aiconfig-panel__mode" :title="t(m.hintKey)">' +
                  '<input type="radio" name="aiconfig-mode" :value="m.value" v-model="mode"> {{ t(m.labelKey) }}' +
                '</label>' +
                '<span class="aiconfig-panel__mode-hint">{{ modeHint }}</span>' +
              '</div>' +
              '<button class="settings-btn settings-btn--accent aiconfig-panel__pull"' +
                ' @click="pull" :disabled="selectedCount === 0 || pulling"' +
                ' :title="modeHint">' +
                '{{ pulling ? \'...\' : t(\'aiconfig.pull\', { count: selectedCount }) }}' +
              '</button>' +
            '</div>' +

            '<div v-if="activeBatch" class="aiconfig-panel__batch glass">' +
              '<div class="aiconfig-panel__batch-head">' +
                '<span class="aiconfig-panel__batch-label">{{ t(\'aiconfig.batch_progress\', { done: activeBatch.done, total: activeBatch.total }) }}</span>' +
                '<div class="aiconfig-panel__batch-actions">' +
                  '<button v-if="activeBatch.finished && batchErrorCount > 0" class="settings-btn settings-btn--sm" @click="retryFailures">↻ {{ t(\'aiconfig.batch_retry\') }} ({{ batchErrorCount }})</button>' +
                  '<button v-if="activeBatch.finished" class="settings-btn settings-btn--sm" @click="dismissBatch">✕ {{ t(\'aiconfig.batch_done\') }}</button>' +
                  '<button v-else class="settings-btn settings-btn--sm" @click="dismissBatch" :title="t(\'ui.close\')">✕</button>' +
                '</div>' +
              '</div>' +
              '<div class="aiconfig-panel__batch-bar">' +
                '<div class="aiconfig-panel__batch-bar-fill" :style="{ width: batchPct + \'%\' }"></div>' +
              '</div>' +
              '<div v-if="activeBatch.finished && activeBatch.results.length" class="aiconfig-panel__batch-results">' +
                '<span v-for="(r, i) in activeBatch.results" :key="i" class="aiconfig-panel__batch-result"' +
                  ' :class="\'aiconfig-panel__badge--\' + r.status"' +
                  ' :title="statusTitle(r)">{{ statusGlyph(r.status) }}</span>' +
              '</div>' +
            '</div>' +
          '</template>' +
        '</template>' +

        ('' + // ── LOCAL VIEW ────────────────────────────────────────
        '<template v-else>') +

          '<section class="aiconfig-panel__local glass" style="flex:none">' +
            '<div class="aiconfig-panel__local-header">' +
              '<div class="aiconfig-panel__local-header-left">' +
                '<span class="aiconfig-panel__local-title">🏠 {{ t(\'aiconfig.local_title\') }}</span>' +
                '<span v-if="store.aiConfigLocal.loaded && store.aiConfigLocal.entries.length" class="aiconfig-panel__local-meta">' +
                  '{{ t(\'aiconfig.local_skills_count\', { count: localSkillCount() }) }}' +
                  '<template v-if="store.aiConfigLocal.collected_at"> · {{ t(\'aiconfig.local_collected_at\', { time: fmtTime(store.aiConfigLocal.collected_at) }) }}</template>' +
                '</span>' +
              '</div>' +
              '<div class="aiconfig-panel__local-toolbar">' +
                '<button class="settings-btn settings-btn--sm" style="white-space:nowrap" @click="openSettingsAiconfig">⚙️ {{ t(\'aiconfig.local_manage_profiles\') }}</button>' +
                '<input type="search" class="aiconfig-panel__search" v-model="localSearch"' +
                  ' :placeholder="t(\'aiconfig.search_placeholder\')" :aria-label="t(\'aiconfig.search_placeholder\')">' +
                '<button class="settings-btn settings-btn--sm" style="white-space:nowrap" @click="refreshLocal" :disabled="store.aiConfigLocal.refreshing">' +
                  '{{ store.aiConfigLocal.refreshing ? \'...\' : (\'🔄 \' + t(\'ui.refresh\')) }}' +
                '</button>' +
              '</div>' +
            '</div>' +

            '<div v-if="!store.aiConfigLocal.loaded" class="panel-empty">' +
              '<div class="panel-empty-title">{{ t(\'ui.loading\') }}</div>' +
            '</div>' +

            '<div v-else-if="store.aiConfigLocal.loadFailed" class="panel-empty">' +
              '<div class="panel-empty-icon">📁</div>' +
              '<div class="panel-empty-title">{{ t(\'aiconfig.local_empty_title\') }}</div>' +
              '<div class="panel-empty-desc">{{ t(\'aiconfig.load_failed\') }}</div>' +
              '<button class="settings-btn settings-btn--sm" style="margin-top:8px" @click="refreshLocal">{{ t(\'aiconfig.local_retry\') }}</button>' +
            '</div>' +

            '<div v-else-if="localNoPaths" class="panel-empty">' +
              '<div class="panel-empty-icon">🗂</div>' +
              '<div class="panel-empty-title">{{ t(\'aiconfig.local_no_paths\') }}</div>' +
              '<div class="panel-empty-desc">{{ t(\'aiconfig.local_empty_desc\') }}</div>' +
              '<button class="settings-btn settings-btn--sm" style="margin-top:8px" @click="openSettingsAiconfig">{{ t(\'aiconfig.local_manage_profiles\') }}</button>' +
            '</div>' +

            '<div v-else-if="groups.length === 0" class="panel-empty">' +
              '<div class="panel-empty-icon">📁</div>' +
              '<div class="panel-empty-title">{{ t(\'aiconfig.local_empty_title\') }}</div>' +
              '<div class="panel-empty-desc">{{ localSearch ? t(\'aiconfig.no_match\') : t(\'aiconfig.local_empty_desc\') }}</div>' +
            '</div>' +

            '<div v-else>' +
              '<div v-for="g in groups" :key="g.key" class="aiconfig-panel__tool-section glass">' +
                '<div class="aiconfig-panel__tool-section-head">' +
                  '<span class="aiconfig-panel__tool-section-title">{{ g.label }}</span>' +
                  '<span class="aiconfig-panel__tool-section-count">{{ g.fileCount }}</span>' +
                '</div>' +
                '<div class="aiconfig-panel__local-tablewrap">' +
                  '<table class="aiconfig-panel__table">' +
                    '<thead>' +
                      '<tr>' +
                        '<th>{{ t(\'aiconfig.col_file\') }}</th>' +
                        '<th>{{ t(\'aiconfig.col_size\') }}</th>' +
                        '<th>{{ t(\'aiconfig.col_time\') }}</th>' +
                        '<th class="aiconfig-panel__local-th-actions"></th>' +
                      '</tr>' +
                    '</thead>' +
                    '<tbody>' +
                      '<tr v-for="row in g.rows" :key="row.key">' +
                        '<td class="aiconfig-panel__cell-path" :style="row.depth ? { paddingLeft: (12 + row.depth * 18) + \'px\' } : {}">' +
                          '<span v-if="row.isDir && row.node" class="aiconfig-panel__tree-chevron" :class="{ \'aiconfig-panel__tree-chevron--open\': isDirExpanded(row.node, localCollapsed) }" role="button" tabindex="0" :aria-label="row.label" :aria-expanded="isDirExpanded(row.node, localCollapsed)" @click.stop="toggleDir(row.node, localCollapsed)" @keydown.enter.space.stop.prevent="toggleDir(row.node, localCollapsed)">▸</span>' +
                          '<span v-else class="aiconfig-panel__tree-chevron aiconfig-panel__tree-chevron--spacer"></span>' +
                          '<button class="aiconfig-panel__path-btn selectable" :class="{ \'aiconfig-panel__path-btn--dir\': row.isDir }" @click="onRowClick(row)" @dblclick.prevent="row.isDir && openEntryDir(row.entry || { tool: row.tool, rel_path: row.path + \'/\' })" :title="row.isDir ? t(\'aiconfig.local_open_dir\') : t(\'aiconfig.preview_title\')">{{ row.isDir ? \'📁 \' : \'\' }}{{ row.label }}</button>' +
                        '</td>' +
                        '<td class="aiconfig-panel__cell-size">{{ row.isDir ? \'\' : fmtSize(row.entry.size) }}</td>' +
                        '<td class="aiconfig-panel__cell-time">{{ fmtTime(row.entry.mtime) }}</td>' +
                        '<td class="aiconfig-panel__local-actions">' +
                          '<button class="settings-btn settings-btn--sm aiconfig-panel__icon-btn" @click="openEntryDir(row.entry || { tool: row.tool, rel_path: row.path + \'/\' })" :title="t(\'aiconfig.local_open_dir\')" :aria-label="t(\'aiconfig.local_open_dir\')">📂</button>' +
                          '<button v-if="row.entry" class="settings-btn settings-btn--sm aiconfig-panel__icon-btn aiconfig-panel__icon-btn--danger" @click="trashEntry(row.entry)" :title="t(\'aiconfig.local_trash_title\')" :aria-label="t(\'aiconfig.local_trash_title\')">🗑</button>' +
                        '</td>' +
                      '</tr>' +
                      '<tr v-if="g.rows.length === 0">' +
                        '<td colspan="4" class="aiconfig-panel__none">{{ t(\'aiconfig.no_match\') }}</td>' +
                      '</tr>' +
                    '</tbody>' +
                  '</table>' +
                '</div>' +
              '</div>' +
            '</div>' +
          '</section>' +
        '</template>' +

        '<!-- Migration wizard modal -->' +
        '<aiconfig-migrate-panel v-if="store.aiConfigMigrateOpen"></aiconfig-migrate-panel>' +

        '<!-- Peer preview overlay -->' +
        '<transition name="dialog-fade">' +
          '<div v-if="preview.visible" class="aiconfig-preview__overlay" role="dialog" aria-modal="true" @click.self="closePreview">' +
            '<div class="aiconfig-preview glass-neo">' +
              '<div class="aiconfig-preview__header">' +
                '<code class="aiconfig-preview__path selectable">{{ preview.relPath }}</code>' +
                '<button class="settings-dialog__close" @click="closePreview" :title="t(\'ui.close\')" :aria-label="t(\'ui.close\')">' +
                  '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
                    '<line x1="18" y1="6" x2="6" y2="18"></line>' +
                    '<line x1="6" y1="6" x2="18" y2="18"></line>' +
                  '</svg>' +
                '</button>' +
              '</div>' +
              '<div v-if="preview.truncated" class="aiconfig-preview__notice">{{ t(\'aiconfig.preview_truncated\') }}</div>' +
              '<pre v-if="preview.loading" class="aiconfig-preview__content selectable">{{ t(\'ui.loading\') }}</pre>' +
              '<pre v-else-if="preview.failed" class="aiconfig-preview__content aiconfig-preview__content--error selectable">{{ t(\'aiconfig.preview_failed\') }}</pre>' +
              '<pre v-else class="aiconfig-preview__content selectable">{{ preview.content || t(\'aiconfig.preview_empty\') }}</pre>' +
            '</div>' +
          '</div>' +
        '</transition>' +

        '<!-- Local preview / edit overlay -->' +
        '<transition name="dialog-fade">' +
          '<div v-if="localPreview.visible" class="aiconfig-preview__overlay" role="dialog" aria-modal="true" @click.self="closeLocalPreview">' +
            '<div class="aiconfig-local-preview glass-neo">' +
              '<div class="aiconfig-local-preview__header">' +
                '<code class="aiconfig-local-preview__path selectable">{{ localPreview.relPath }}</code>' +
                '<div class="aiconfig-local-preview__btns">' +
                  '<button v-if="!localPreview.loading && !localPreview.failed && !localPreview.editing" class="settings-btn settings-btn--sm" @click="startLocalEdit">✏️ {{ t(\'aiconfig.local_edit\') }}</button>' +
                  '<button v-if="localPreview.editing" class="settings-btn settings-btn--sm settings-btn--accent" @click="saveLocalEdit" :disabled="localPreview.saving">{{ localPreview.saving ? \'...\' : t(\'aiconfig.local_save\') }}</button>' +
                  '<button v-if="localPreview.editing" class="settings-btn settings-btn--sm" @click="cancelLocalEdit">{{ t(\'ui.cancel\') }}</button>' +
                  '<button class="settings-dialog__close" @click="closeLocalPreview" :title="t(\'ui.close\')" :aria-label="t(\'ui.close\')">' +
                    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
                      '<line x1="18" y1="6" x2="6" y2="18"></line>' +
                      '<line x1="6" y1="6" x2="18" y2="18"></line>' +
                    '</svg>' +
                  '</button>' +
                '</div>' +
              '</div>' +
              '<div v-if="localPreview.truncated" class="aiconfig-preview__notice">{{ t(\'aiconfig.preview_truncated\') }}</div>' +
              '<pre v-if="localPreview.loading" class="aiconfig-preview__content selectable">{{ t(\'ui.loading\') }}</pre>' +
              '<pre v-else-if="localPreview.failed" class="aiconfig-preview__content aiconfig-preview__content--error selectable">{{ localPreviewError }}</pre>' +
              '<textarea v-else-if="localPreview.editing" class="aiconfig-local-preview__editor selectable" v-model="localPreview.content" spellcheck="false"></textarea>' +
              '<pre v-else class="aiconfig-preview__content selectable">{{ localPreview.content || t(\'aiconfig.preview_empty\') }}</pre>' +
            '</div>' +
          '</div>' +
        '</transition>' +
      '</div>',
  };

})();
