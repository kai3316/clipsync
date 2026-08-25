/* ═══════════════════════════════════════════════════════════════════
   ClipSync AI-Config Panel Component (round 18)
   The 「配置」(AI Config) tab is now the LOCAL config manager ONLY — a file
   manager over THIS device's watch roots. It needs NO paired device:
   browsing / previewing / editing / trashing / opening folders all hit
   the local /api/aiconfig/local* endpoints directly.

   Round 18 relocation: the device-related config (peer inventories,
   preview, pull) used to live here too.  Per the product decision it
   moved to the 「设备」(Devices) tab as aiconfig-device-panel.js — nothing
   here duplicates it.

   Default watch presets (round 19): the common AI-tool config locations
   (Claude Code, Codex, Cursor, Gemini CLI).  Files AND directories are both
   valid watch entries — file entries (e.g. ~/.claude/CLAUDE.md) sync exactly
   that file while keeping credentials like auth.json out. */

   var AICONFIG_PRESETS = [
     '~/.claude/CLAUDE.md',
     '~/.claude/settings.json',
     '~/.claude/skills',
     '~/.codex/config.toml',
     '~/.cursor/rules',
     '~/.cursor/commands',
     '~/.gemini/settings.json',
     '~/.gemini/GEMINI.md'
   ];

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  // Selection key joins root_index and rel_path. Keys are only ever matched
  // against entries via keyOf() (never parsed back), so any separator works.
  var KEY_SEP = '|';

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

  window.__CLIPSYNC_COMPONENTS__['aiconfig-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        localSearch: '',
        // Expanded folder keys in the tree view: "root_index|rel_path".
        // Folders render collapsed by default so a big skill doesn't dump
        // every nested file onto the list (the "一个 skill 所有文件都显示了"
        // complaint).  Folders stay open across refreshes (keyed, not index).
        localExpanded: {},
        pathsDialog: { visible: false, paths: [], saving: false },
        localPreview: {
          visible: false,
          loading: false,
          failed: false,
          error: '',
          editing: false,
          saving: false,
          relPath: '',
          rootIndex: 0,
          content: '',
          truncated: false,
        },
      };
    },

    computed: {
      localRootMap: function () {
        var m = {};
        var roots = this.store.aiConfigLocal.roots || [];
        for (var i = 0; i < roots.length; i++) {
          var r = roots[i];
          if (r && typeof r.root_index === 'number') {
            m[r.root_index] = r.path;
          }
        }
        return m;
      },

      localMultiRoot: function () {
        return Object.keys(this.localRootMap).length > 1;
      },

      localFilteredEntries: function () {
        var q = (this.localSearch || '').toLowerCase().trim();
        var items = (this.store.aiConfigLocal.entries || []).slice();
        if (q) {
          items = items.filter(function (e) {
            return String(e.rel_path || '').toLowerCase().indexOf(q) !== -1;
          });
        }
        items.sort(function (a, b) {
          return String(a.rel_path || '').localeCompare(String(b.rel_path || ''));
        });
        return items;
      },

      // Nested folder/file tree built from the flat entry list.  rel_path is
      // split on "/" so subdirectories (a skill's references/, a command's
      // templates/) become parent nodes; the backend's is_dir entries attach
      // the real folder metadata to those nodes.  A FILE watch root (e.g.
      // ~/.claude/CLAUDE.md) is itself a top-level node, not a folder.
      localTree: function () {
        var items = this.store.aiConfigLocal.entries || [];
        var roots = [];
        var nodes = {};
        for (var i = 0; i < items.length; i++) {
          var e = items[i];
          var rel = String(e.rel_path || '').replace(/\/+$/, '');
          if (!rel) continue;
          var parts = rel.split('/');
          var node = null;
          var parent = null;
          for (var j = 0; j < parts.length; j++) {
            var key = String(e.root_index) + KEY_SEP + parts.slice(0, j + 1).join('/');
            if (j === 0) {
              node = nodes[key];
              if (!node) {
                node = nodes[key] = {
                  key: key,
                  root_index: e.root_index,
                  name: parts[0],
                  path: parts[0],
                  is_dir: parts.length > 1 || !!e.is_dir,
                  entry: null,
                  children: [],
                };
                roots.push(node);
              }
              parent = node;
            } else {
              var child = null;
              for (var c = 0; c < parent.children.length; c++) {
                if (parent.children[c].key === key) { child = parent.children[c]; break; }
              }
              if (!child) {
                child = {
                  key: key,
                  root_index: e.root_index,
                  name: parts[j],
                  path: parts.slice(0, j + 1).join('/'),
                  is_dir: j < parts.length - 1 || !!e.is_dir,
                  entry: null,
                  children: [],
                };
                parent.children.push(child);
              }
              parent = child;
              node = child;
            }
          }
          // Attach the backend entry to the node the entry names.  File
          // entries land on their leaf node; a directory entry (rel_path with
          // trailing slash) lands on its folder node, carrying the metadata
          // openEntryDir/trash need.
          if (node) {
            node.entry = e;
            node.is_dir = !!e.is_dir;
          }
        }
        return roots;
      },

      // Rows to render: the flat search results while searching, otherwise a
      // depth-annotated walk of the tree (folders open only when expanded).
      localRows: function () {
        var q = (this.localSearch || '').toLowerCase().trim();
        var out = [];
        if (q) {
          var items = this.localFilteredEntries;
          for (var i = 0; i < items.length; i++) {
            var m = items[i];
            out.push({
              key: this.localKeyOf(m),
              label: m.rel_path,
              depth: 0,
              isDir: !!m.is_dir,
              entry: m,
              node: null,
            });
          }
          return out;
        }
        var self = this;
        var walk = function (nodes, depth) {
          for (var i = 0; i < nodes.length; i++) {
            var node = nodes[i];
            out.push({
              key: node.key,
              label: node.name,
              depth: depth,
              isDir: node.is_dir,
              entry: node.entry,
              node: node,
            });
            if (node.is_dir && self.isDirExpanded(node)) {
              walk(node.children, depth + 1);
            }
          }
        };
        walk(this.localTree, 0);
        return out;
      },

      // True when the watch list is empty / nothing was collected — the
      // manager's "add watch paths first" guide, independent of pairing.
      localNoPaths: function () {
        var l = this.store.aiConfigLocal;
        return !!l.loaded && !l.loadFailed && !(l.roots || []).length;
      },

      localPreviewError: function () {
        return this.localErrorMessage(this.localPreview.error);
      },
    },

    created: function () {
      this._onKeyDown = function (e) {
        if (e.key !== 'Escape') return;
        if (this.localPreview.visible) { this.closeLocalPreview(); return; }
        if (this.pathsDialog.visible) { this.closePathsDialog(); }
      }.bind(this);
    },

    mounted: function () {
      document.addEventListener('keydown', this._onKeyDown);
      // First open of the tab primes the LOCAL manager (independent of any
      // pairing).  Device inventories now live in the Devices tab.
      if (!this.store.aiConfigLocal.loaded) {
        this.store.fetchAiConfigLocal();
      }
    },

    beforeUnmount: function () {
      document.removeEventListener('keydown', this._onKeyDown);
    },

    methods: {
      fmtSize: fmtSize,
      fmtTime: fmtTime,

      localKeyOf: function (entry) {
        return String(entry.root_index) + KEY_SEP + String(entry.rel_path);
      },

      localRootLabel: function (ri) {
        var p = this.localRootMap[ri];
        return p ? ('R' + ri + ' · ' + p) : ('R' + ri);
      },

      // Build a human-readable local-preview failure message from the raw
      // backend reason. A binary file gets its own clearer copy; everything
      // else (too large / path traversal / not found / ...) carries the
      // backend's reason so the user knows exactly what went wrong.
      localErrorMessage: function (rawErr) {
        var err = String(rawErr || '').trim();
        if (!err) return this.t('aiconfig.preview_failed');
        if (err.toLowerCase().indexOf('binary') !== -1) {
          return this.t('aiconfig.local_preview_binary');
        }
        return this.t('aiconfig.local_preview_failed', { reason: err });
      },

      refreshLocal: function () {
        return this.store.fetchAiConfigLocal();
      },

      // ── Tree view helpers (round 19) ─────────────────────────────

      isDirExpanded: function (node) {
        return !!(node && this.localExpanded[node.key]);
      },

      toggleDir: function (node) {
        if (!node) return;
        if (this.localExpanded[node.key]) {
          delete this.localExpanded[node.key];
        } else {
          this.localExpanded[node.key] = true;
        }
        // Reassign a copy so Vue 2's object-change detection picks it up.
        this.localExpanded = Object.assign({}, this.localExpanded);
      },

      // Clicking a folder toggles its expansion; a file opens the preview.
      // In search mode a folder has no tree node — open it in the OS file
      // manager instead (that's what clicking a folder meant before the tree).
      // Double-clicking a folder also opens it in the OS file manager.
      onRowClick: function (row) {
        if (!row) return;
        if (row.isDir) {
          if (row.node) { this.toggleDir(row.node); }
          else { this.openRowDir(row); }
        } else if (row.entry) {
          this.openLocalPreview(row.entry);
        }
      },

      openRowDir: function (row) {
        if (!row || !row.isDir) return;
        // node.entry carries the folder's own is_dir entry (trailing slash);
        // a missing one means a stale intermediate node — build a synthetic
        // entry so "open in OS" still resolves the folder path.
        if (row.entry && row.entry.is_dir) {
          this.openEntryDir(row.entry);
        } else if (row.node) {
          this.openEntryDir({
            root_index: row.node.root_index,
            rel_path: row.node.path + '/',
          });
        }
      },

      openPathsDialog: function () {
        var self = this;
        if (!window.ClipsyncAPI || !window.ClipsyncAPI.getAiConfigPaths) {
          this.store.showToast(this.t('aiconfig.local_paths_save_failed'), 3000, 'error');
          return;
        }
        this.pathsDialog.paths = [];
        this.pathsDialog.saving = false;
        window.ClipsyncAPI.getAiConfigPaths().then(function (res) {
          self.pathsDialog.paths = ((res && Array.isArray(res.paths)) ? res.paths : [])
            .map(function (p) { return String(p == null ? '' : p); });
          self.pathsDialog.visible = true;
        }).catch(function () {
          self.store.showToast(self.t('aiconfig.local_paths_save_failed'), 3000, 'error');
        });
      },

      closePathsDialog: function () {
        this.pathsDialog.visible = false;
      },

      addPathRow: function () {
        this.pathsDialog.paths.push('');
      },

      removePathRow: function (idx) {
        this.pathsDialog.paths.splice(idx, 1);
      },

      addPresetPaths: function () {
        var cur = (this.pathsDialog.paths || []).map(function (p) {
          return String(p == null ? '' : p).trim();
        }).filter(function (p) { return p; });
        var seen = {};
        cur.forEach(function (p) { seen[p] = true; });
        var merged = cur.slice();
        AICONFIG_PRESETS.forEach(function (p) {
          if (!seen[p]) { seen[p] = true; merged.push(p); }
        });
        this.pathsDialog.paths = merged;
        this.store.showToast(this.t('aiconfig.local_presets_added'), 2200, 'success');
      },

      savePaths: function () {
        var self = this;
        var paths = this.pathsDialog.paths
          .map(function (p) { return String(p == null ? '' : p).trim(); })
          .filter(function (p) { return p; });
        this.pathsDialog.saving = true;
        window.ClipsyncAPI.setAiConfigPaths(paths).then(function (res) {
          self.pathsDialog.saving = false;
          if (res && res.ok) {
            self.closePathsDialog();
            self.store.showToast(self.t('aiconfig.local_paths_saved'), 2800, 'success');
            // The backend recollects on save; refetch to show the fresh list.
            self.store.fetchAiConfigLocal();
          } else {
            self.store.showToast(self.t('aiconfig.local_paths_save_failed'), 3000, 'error');
          }
        }).catch(function () {
          self.pathsDialog.saving = false;
          self.store.showToast(self.t('aiconfig.local_paths_save_failed'), 3000, 'error');
        });
      },

      openLocalPreview: function (entry) {
        var self = this;
        // A folder entry can't be previewed as text — open it in the OS file
        // manager instead (that's the "open the skill" action).
        if (entry && entry.is_dir) {
          this.openEntryDir(entry);
          return;
        }
        this.localPreview = {
          visible: true,
          loading: true,
          failed: false,
          error: '',
          editing: false,
          saving: false,
          rootIndex: entry.root_index,
          relPath: entry.rel_path,
          content: '',
          truncated: false,
        };
        ClipsyncAPI.getAiConfigLocalItem(entry.root_index, entry.rel_path)
          .then(function (res) {
            if (!res || !res.ok) {
              self.localPreview.loading = false;
              self.localPreview.failed = true;
              self.localPreview.error = (res && res.error) || '';
              // Binary / too-large / traversal errors get an explicit toast
              // so the failure is visible even if the preview stays closed.
              self.store.showToast(self.localErrorMessage(self.localPreview.error),
                3200, 'error');
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
            self.store.showToast(self.localErrorMessage(self.localPreview.error),
              3200, 'error');
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
        ClipsyncAPI.saveAiConfigLocal(p.rootIndex, p.relPath, p.content)
          .then(function (res) {
            p.saving = false;
            if (res && res.ok) {
              self.store.showToast(self.t('aiconfig.local_saved_toast'), 3000, 'success');
              p.editing = false;
              // The file changed — refresh so size/mtime are fresh.
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
          // User confirmed — move to the OS Recycle Bin (recoverable).
          return ClipsyncAPI.trashAiConfigLocal(entry.root_index, entry.rel_path);
        }).then(function (res) {
          if (res && res.ok) {
            var dest = (res && res.trashed_to) || '';
            self.store.showToast(self.t('aiconfig.local_trashed_toast', { dest: dest }),
              3200, 'success');
            // Drop the trashed row(s) from the local list immediately.  A
            // folder trash removes its own row AND every entry under it.
            var prefix = String(entry.rel_path || '').replace(/\/+$/, '') + '/';
            var cur = self.store.aiConfigLocal.entries.slice();
            for (var i = cur.length - 1; i >= 0; i--) {
              var rp = String(cur[i].rel_path || '');
              if (cur[i].root_index === entry.root_index &&
                  (rp === entry.rel_path ||
                   (entry.is_dir && rp.indexOf(prefix) === 0))) {
                cur.splice(i, 1);
              }
            }
            self.store.aiConfigLocal.entries = cur;
            if (self.localPreview.visible &&
                self.localPreview.rootIndex === entry.root_index &&
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
          // store.confirm rejects (no value) when the user cancels — that is
          // a silent no-op. A genuine API failure arrives as an Error.
          if (err && typeof err === 'object') {
            self.store.showToast(self.t('aiconfig.local_trash_failed',
              { reason: (err && err.message) || '' }), 3200, 'error');
          }
        });
      },

      openEntryDir: function (entry) {
        var self = this;
        ClipsyncAPI.openAiConfigLocal(entry.root_index, entry.rel_path)
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
    },

    template:
      '<div class="aiconfig-panel" style="overflow-y:auto">' +

        '<!-- Header -->' +
        '<div class="favorites-panel__header">' +
          '<div class="favorites-panel__header-left">' +
            '<span class="favorites-panel__header-icon">📁</span>' +
            '<span class="favorites-panel__header-title">{{ t(\'ui.aiconfig\') }}</span>' +
            '<span v-if="store.aiConfigLocal.loaded && store.aiConfigLocal.entries.length" class="favorites-panel__header-count neon-badge">{{ store.aiConfigLocal.entries.length }}</span>' +
          '</div>' +
          '<button class="settings-btn settings-btn--sm" @click="openPathsDialog">' +
            '🗂 {{ t(\'aiconfig.local_manage_paths\') }}' +
          '</button>' +
        '</div>' +

        '<!-- Local config manager (round 18) -->' +
        '<section class="aiconfig-panel__local glass" style="flex:none">' +
          '<div class="aiconfig-panel__local-header">' +
            '<div class="aiconfig-panel__local-header-left">' +
              '<span class="aiconfig-panel__local-title">📁 {{ t(\'aiconfig.local_title\') }}</span>' +
              '<span v-if="store.aiConfigLocal.loaded && store.aiConfigLocal.entries.length" class="aiconfig-panel__local-meta">' +
                '{{ t(\'aiconfig.local_files_count\', { count: store.aiConfigLocal.entries.length }) }}' +
                '<template v-if="store.aiConfigLocal.collected_at"> · {{ t(\'aiconfig.local_collected_at\', { time: fmtTime(store.aiConfigLocal.collected_at) }) }}</template>' +
              '</span>' +
            '</div>' +
            '<div class="aiconfig-panel__local-toolbar">' +
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
            '<button class="settings-btn settings-btn--sm" style="margin-top:8px" @click="openPathsDialog">{{ t(\'aiconfig.local_manage_paths\') }}</button>' +
          '</div>' +

          '<div v-else-if="localFilteredEntries.length === 0" class="panel-empty">' +
            '<div class="panel-empty-icon">📁</div>' +
            '<div class="panel-empty-title">{{ t(\'aiconfig.local_empty_title\') }}</div>' +
            '<div class="panel-empty-desc">{{ localSearch ? t(\'aiconfig.no_match\') : t(\'aiconfig.local_empty_desc\') }}</div>' +
            '<button v-if="!localSearch" class="settings-btn settings-btn--sm" style="margin-top:8px" @click="refreshLocal">{{ t(\'aiconfig.local_retry\') }}</button>' +
          '</div>' +

          '<div v-else class="aiconfig-panel__local-tablewrap">' +
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
                '<tr v-for="row in localRows" :key="row.key">' +
                  '<td class="aiconfig-panel__cell-path" :style="row.depth ? { paddingLeft: (12 + row.depth * 18) + \'px\' } : {}">' +
                    '<span v-if="row.isDir && row.node" class="aiconfig-panel__tree-chevron" :class="{ \'aiconfig-panel__tree-chevron--open\': localExpanded[row.node.key] }" role="button" tabindex="0" :aria-label="row.label" :aria-expanded="!!localExpanded[row.node.key]" @click.stop="toggleDir(row.node)" @keydown.enter.space.stop.prevent="toggleDir(row.node)">▸</span>' +
                    '<span v-else class="aiconfig-panel__tree-chevron aiconfig-panel__tree-chevron--spacer"></span>' +
                    '<span v-if="localMultiRoot && !row.isDir" class="aiconfig-panel__root-chip" :title="localRootLabel(row.entry.root_index)">R{{ row.entry.root_index }}</span>' +
                    // Folders: click to expand/collapse the tree, double-click
                    // to open in the OS file manager (the "打开那个技能" action);
                    // files open a text preview.  The 📁 marks folder rows.
                    '<button class="aiconfig-panel__path-btn selectable" :class="{ \'aiconfig-panel__path-btn--dir\': row.isDir }" @click="onRowClick(row)" @dblclick.prevent="openRowDir(row)" :title="row.isDir ? t(\'aiconfig.local_open_dir\') : t(\'aiconfig.preview_title\')">{{ row.isDir ? \'📁 \' : \'\' }}{{ row.label }}</button>' +
                  '</td>' +
                  '<td class="aiconfig-panel__cell-size">{{ row.isDir ? \'\' : fmtSize(row.entry.size) }}</td>' +
                  '<td class="aiconfig-panel__cell-time">{{ fmtTime(row.entry.mtime) }}</td>' +
                  '<td class="aiconfig-panel__local-actions">' +
                    '<button class="settings-btn settings-btn--sm aiconfig-panel__icon-btn" @click="openRowDir(row)" :title="t(\'aiconfig.local_open_dir\')" :aria-label="t(\'aiconfig.local_open_dir\')">📂</button>' +
                    // Files AND folders can be trashed (a folder goes to the
                    // recycle bin whole, recoverable).
                    '<button v-if="row.entry" class="settings-btn settings-btn--sm aiconfig-panel__icon-btn aiconfig-panel__icon-btn--danger" @click="trashEntry(row.entry)" :title="t(\'aiconfig.local_trash_title\')" :aria-label="t(\'aiconfig.local_trash_title\')">🗑</button>' +
                  '</td>' +
                '</tr>' +
              '</tbody>' +
            '</table>' +
          '</div>' +
        '</section>' +

        '<!-- Paired-device AI-config inventories (round 12; relocated here from' +
        'the Devices tab — device configs belong with the config manager, not in' +
        'the device list) -->' +
        '<aiconfig-device-panel></aiconfig-device-panel>' +

        '<!-- Watch-paths management dialog (round 18) -->' +
        '<transition name="dialog-fade">' +
          '<div v-if="pathsDialog.visible" class="aiconfig-paths__overlay" role="dialog" aria-modal="true" @click.self="closePathsDialog">' +
            '<div class="aiconfig-paths glass-neo">' +
              '<div class="aiconfig-paths__header">' +
                '<span class="aiconfig-paths__title">🗂 {{ t(\'aiconfig.local_manage_paths\') }}</span>' +
                '<button class="settings-dialog__close" @click="closePathsDialog" :title="t(\'ui.close\')" :aria-label="t(\'ui.close\')">' +
                  '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
                    '<line x1="18" y1="6" x2="6" y2="18"></line>' +
                    '<line x1="6" y1="6" x2="18" y2="18"></line>' +
                  '</svg>' +
                '</button>' +
              '</div>' +
              '<div class="aiconfig-paths__hint">{{ t(\'aiconfig.local_paths_hint\') }}</div>' +
              '<div class="aiconfig-paths__list">' +
                '<div v-for="(p, idx) in pathsDialog.paths" :key="\'localpath-\' + idx" class="aiconfig-paths__row">' +
                  '<input type="text" class="settings-input" v-model="pathsDialog.paths[idx]" spellcheck="false"' +
                    ' :placeholder="t(\'settings_window.aiconfig_path_placeholder\')">' +
                  '<button class="settings-btn settings-btn--sm" @click="removePathRow(idx)" :aria-label="t(\'ui.delete\')">✕</button>' +
                '</div>' +
                '<button class="settings-btn settings-btn--sm" style="margin-top:6px" @click="addPathRow">+ {{ t(\'aiconfig.local_add_path\') }}</button>' +
                  '<button class="settings-btn settings-btn--sm" style="margin-top:6px;margin-left:6px" @click="addPresetPaths">✨ {{ t(\'aiconfig.local_add_presets\') }}</button>' +
              '</div>' +
              '<div class="aiconfig-paths__footer">' +
                '<button class="settings-btn" @click="closePathsDialog">{{ t(\'ui.cancel\') }}</button>' +
                '<button class="settings-btn settings-btn--accent" @click="savePaths" :disabled="pathsDialog.saving">' +
                  '{{ pathsDialog.saving ? \'...\' : t(\'settings_window.save_aiconfig\') }}' +
                '</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</transition>' +

        '<!-- Local preview / edit overlay (round 18) -->' +
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
