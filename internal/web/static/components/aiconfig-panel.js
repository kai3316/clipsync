/* ═══════════════════════════════════════════════════════════════════
   ClipSync AI-Config Panel Component (round 12)
   Browse the AI-tool config file inventories (CLAUDE.md, memory md,
   skills, ...) advertised by PAIRED devices, preview individual files,
   and pull the selected ones to this machine.

   Landing modes — never a silent overwrite:
     overwrite  replace the local same-name file
     copy       save under a copy name, local files untouched (default)
     append     append the text to the local same-name .md file
   Per-file results arrive asynchronously over WS (`aiconfig_file`) and
   are folded into store.aiConfigResults by ws.js; this panel badges
   each row with its latest outcome.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  // Selection key joins root_index and rel_path. Keys are only ever matched
  // against entries via keyOf() (never parsed back), so any separator works.
  var KEY_SEP = '|';

  // "append" mode only lands on text/markdown files (mirrors the backend's
  // APPEND_EXTS: .txt / .md / .markdown).
  var TEXT_EXT_RE = /\.(md|markdown|txt)$/i;

  function keyOf(entry) {
    return String(entry.root_index) + KEY_SEP + String(entry.rel_path);
  }

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

  var MODES = [
    { value: 'overwrite', labelKey: 'aiconfig.mode_overwrite', hintKey: 'aiconfig.mode_overwrite_hint' },
    { value: 'copy', labelKey: 'aiconfig.mode_copy', hintKey: 'aiconfig.mode_copy_hint' },
    { value: 'append', labelKey: 'aiconfig.mode_append', hintKey: 'aiconfig.mode_append_hint' },
  ];

  window.__CLIPSYNC_COMPONENTS__['aiconfig-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        search: '',
        selectedPeerId: '',
        checked: {},            // selection key -> true
        mode: 'copy',           // default = never clobber silently
        pulling: false,
        modes: MODES,
        preview: {
          visible: false,
          loading: false,
          failed: false,
          relPath: '',
          rootIndex: 0,
          content: '',
          truncated: false,
        },
      };
    },

    computed: {
      peersList: function () {
        var peers = (this.store.aiConfigInventory && this.store.aiConfigInventory.peers) || {};
        var self = this;
        return Object.keys(peers).map(function (pid) {
          var p = peers[pid];
          return {
            id: pid,
            name: p.name || pid,
            entries: Array.isArray(p.entries) ? p.entries : [],
            fetchedAt: p.fetchedAt || p.fetched_at || '',
          };
        }).sort(function (a, b) {
          return a.name.localeCompare(b.name);
        });
      },

      currentPeer: function () {
        var id = this.selectedPeerId;
        for (var i = 0; i < this.peersList.length; i++) {
          if (this.peersList[i].id === id) return this.peersList[i];
        }
        return null;
      },

      filteredEntries: function () {
        var peer = this.currentPeer;
        if (!peer) return [];
        var q = (this.search || '').toLowerCase().trim();
        var items = peer.entries.slice();
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

      multiRoot: function () {
        var peer = this.currentPeer;
        if (!peer) return false;
        var seen = {};
        for (var i = 0; i < peer.entries.length; i++) {
          seen[String(peer.entries[i].root_index)] = true;
        }
        return Object.keys(seen).length > 1;
      },

      selectedCount: function () {
        return Object.keys(this.checked).filter(function (k) { return this.checked[k]; }, this).length;
      },

      allSelected: function () {
        var items = this.filteredEntries;
        if (items.length === 0) return false;
        for (var i = 0; i < items.length; i++) {
          if (!this.checked[keyOf(items[i])]) return false;
        }
        return true;
      },

      modeHint: function () {
        for (var i = 0; i < MODES.length; i++) {
          if (MODES[i].value === this.mode) return this.t(MODES[i].hintKey);
        }
        return '';
      },
    },

    watch: {
      // Follow the inventory: auto-select the first peer on arrival, and
      // fall back off a peer that disappeared from the refreshed list.
      peersList: function (list) {
        if (!list.length) {
          this.selectedPeerId = '';
          this.checked = {};
          return;
        }
        var found = false;
        for (var i = 0; i < list.length; i++) {
          if (list[i].id === this.selectedPeerId) { found = true; break; }
        }
        if (!found) {
          this.selectedPeerId = list[0].id;
          this.checked = {};
        }
      },
    },

    created: function () {
      this._onKeyDown = function (e) {
        if (e.key === 'Escape' && this.preview.visible) {
          this.closePreview();
        }
      }.bind(this);
    },

    mounted: function () {
      document.addEventListener('keydown', this._onKeyDown);
      // First open of the tab fetches the cached inventory; the refresh
      // button asks the backend to re-request fresh inventories (?refresh=1).
      if (!this.store.aiConfigLoaded) {
        this.store.fetchAiConfigInventory(false);
      }
    },

    beforeUnmount: function () {
      document.removeEventListener('keydown', this._onKeyDown);
    },

    methods: {
      keyOf: keyOf,
      fmtSize: fmtSize,
      fmtTime: fmtTime,

      refresh: function (force) {
        return this.store.fetchAiConfigInventory(!!force);
      },

      selectPeer: function (id) {
        if (id === this.selectedPeerId) return;
        this.selectedPeerId = id;
        this.checked = {};   // selection belongs to one device
      },

      isChecked: function (entry) {
        return !!this.checked[keyOf(entry)];
      },

      toggleCheck: function (entry) {
        var k = keyOf(entry);
        var next = Object.assign({}, this.checked);
        if (next[k]) {
          delete next[k];
        } else {
          next[k] = true;
        }
        this.checked = next;
      },

      toggleSelectAll: function () {
        var items = this.filteredEntries;
        if (this.allSelected) {
          this.checked = {};
          return;
        }
        var next = {};
        for (var i = 0; i < items.length; i++) {
          next[keyOf(items[i])] = true;
        }
        this.checked = next;
      },

      // Latest WS pull result badge for a row (this device + path).
      resultFor: function (entry) {
        var list = this.store.aiConfigResults || [];
        for (var i = 0; i < list.length; i++) {
          var r = list[i];
          if (r.peer_id === this.selectedPeerId && r.rel_path === entry.rel_path) {
            return r;
          }
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

      openPreview: function (entry) {
        var self = this;
        if (!this.selectedPeerId) return;
        this.preview = {
          visible: true,
          loading: true,
          failed: false,
          relPath: entry.rel_path,
          rootIndex: entry.root_index,
          content: '',
          truncated: false,
        };
        ClipsyncAPI.previewAiConfigFile(this.selectedPeerId, entry.root_index, entry.rel_path)
          .then(function (res) {
            self.preview.loading = false;
            self.preview.content = (res && res.content) || '';
            self.preview.truncated = !!(res && res.truncated);
          })
          .catch(function () {
            self.preview.loading = false;
            self.preview.failed = true;
          });
      },

      closePreview: function () {
        this.preview.visible = false;
      },

      // "append" only lands on text/markdown files — the backend would
      // reject anything else per-file with an error event; catching the
      // obvious case up front keeps the failure visible and immediate.
      hasNonTextSelection: function (items) {
        for (var i = 0; i < items.length; i++) {
          if (!TEXT_EXT_RE.test(items[i].rel_path || '')) return true;
        }
        return false;
      },

      pull: function () {
        var self = this;
        var peer = this.currentPeer;
        if (!peer || this.selectedCount === 0 || this.pulling) return;

        // Rebuild the item list from the checked keys (never trust that the
        // entries still line up with an earlier snapshot).
        var byKey = {};
        peer.entries.forEach(function (e) { byKey[keyOf(e)] = e; });
        var items = [];
        Object.keys(this.checked).forEach(function (k) {
          if (!self.checked[k]) return;
          var e = byKey[k];
          if (e) items.push({ root_index: e.root_index, rel_path: e.rel_path });
        });
        if (items.length === 0) return;
        if (this.mode === 'append' && this.hasNonTextSelection(items)) {
          this.store.showToast(self.t('aiconfig.append_ext_warning'), 3500, 'warning');
          return;
        }

        this.pulling = true;
        ClipsyncAPI.pullAiConfigFiles(this.selectedPeerId, items, this.mode)
          .then(function (res) {
            self.pulling = false;
            var n = (res && typeof res.requested === 'number') ? res.requested : items.length;
            self.store.showToast(self.t('aiconfig.pull_requested', { count: n }), 3200, 'success');
            self.checked = {};
          })
          .catch(function (e) {
            self.pulling = false;
            // The backend answers requested=0 with an `errors` reason list
            // (peer_offline / peer_not_paired / send_failed / ...).
            var reasons = (e && e.data && Array.isArray(e.data.errors)) ? e.data.errors : [];
            self.store.showToast(
              reasons.length ? (self.t('aiconfig.pull_failed') + ' (' + reasons.join(', ') + ')')
                             : self.t('aiconfig.pull_failed'),
              3000, 'error');
          });
      },
    },

    template:
      '<div class="aiconfig-panel">' +

        '<!-- Header -->' +
        '<div class="favorites-panel__header">' +
          '<div class="favorites-panel__header-left">' +
            '<span class="favorites-panel__header-icon">🤖</span>' +
            '<span class="favorites-panel__header-title">{{ t(\'ui.aiconfig\') }}</span>' +
            '<span v-if="peersList.length" class="favorites-panel__header-count neon-badge">{{ peersList.length }}</span>' +
          '</div>' +
          '<button class="settings-btn settings-btn--sm" @click="refresh(true)" :disabled="store.aiConfigRefreshing">' +
            '{{ store.aiConfigRefreshing ? \'...\' : t(\'ui.refresh\') }}' +
          '</button>' +
        '</div>' +

        '<!-- First load -->' +
        '<div v-if="!store.aiConfigLoaded" class="panel-empty">' +
          '<div class="panel-empty-title">{{ t(\'ui.loading\') }}</div>' +
        '</div>' +

        '<!-- Empty state / load failure -->' +
        '<div v-else-if="peersList.length === 0" class="panel-empty">' +
          '<div class="panel-empty-icon">🤖</div>' +
          '<div class="panel-empty-title">{{ t(\'aiconfig.empty_title\') }}</div>' +
          '<div class="panel-empty-desc">{{ store.aiConfigLoadFailed ? t(\'aiconfig.load_failed\') : t(\'aiconfig.empty_desc\') }}</div>' +
          '<button class="settings-btn settings-btn--sm" style="margin-top:10px" @click="refresh(true)" :disabled="store.aiConfigRefreshing">' +
            '{{ store.aiConfigRefreshing ? \'...\' : t(\'ui.refresh\') }}' +
          '</button>' +
        '</div>' +

        '<!-- Body: peer list + file table -->' +
        '<div v-else class="aiconfig-panel__body">' +

            '<!-- Device list -->' +
            '<div class="aiconfig-panel__peers glass">' +
              '<div class="favorites-panel__sidebar-title">{{ t(\'aiconfig.devices\') }}</div>' +
              '<button v-for="p in peersList" :key="p.id"' +
                ' class="aiconfig-panel__peer-btn"' +
                ' :class="{ \'aiconfig-panel__peer-btn--active\' : p.id === selectedPeerId }"' +
                ' @click="selectPeer(p.id)">' +
                '<span class="aiconfig-panel__peer-name">{{ p.name }}</span>' +
                '<span class="aiconfig-panel__peer-meta">{{ t(\'aiconfig.files_count\', { count: p.entries.length }) }}</span>' +
                '<span v-if="p.fetchedAt" class="aiconfig-panel__peer-meta">{{ t(\'aiconfig.fetched_at\', { time: fmtTime(p.fetchedAt) }) }}</span>' +
              '</button>' +
            '</div>' +

            '<!-- Files -->' +
            '<div class="aiconfig-panel__files">' +
              '<div class="aiconfig-panel__toolbar">' +
                '<input type="search" class="aiconfig-panel__search" v-model="search"' +
                  ' :placeholder="t(\'aiconfig.search_placeholder\')" :aria-label="t(\'aiconfig.search_placeholder\')">' +
                '<label class="aiconfig-panel__selectall">' +
                  '<input type="checkbox" :checked="allSelected" @change="toggleSelectAll"> {{ t(\'aiconfig.select_all\') }}' +
                '</label>' +
              '</div>' +

              '<div class="aiconfig-panel__tablewrap glass">' +
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
                    '<tr v-for="e in filteredEntries" :key="keyOf(e)">' +
                      '<td class="aiconfig-panel__td-check">' +
                        '<input type="checkbox" :checked="isChecked(e)" @change="toggleCheck(e)"' +
                          ' :aria-label="e.rel_path">' +
                      '</td>' +
                      '<td class="aiconfig-panel__cell-path">' +
                        '<span v-if="multiRoot" class="aiconfig-panel__root-chip" :title="t(\'aiconfig.root_label\', { n: e.root_index })">R{{ e.root_index }}</span>' +
                        '<button class="aiconfig-panel__path-btn selectable" @click="openPreview(e)" :title="t(\'aiconfig.preview_title\')">{{ e.rel_path }}</button>' +
                      '</td>' +
                      '<td class="aiconfig-panel__cell-size">{{ fmtSize(e.size) }}</td>' +
                      '<td class="aiconfig-panel__cell-time">{{ fmtTime(e.mtime) }}</td>' +
                      '<td class="aiconfig-panel__cell-status">' +
                        '<span v-if="resultFor(e)" class="aiconfig-panel__badge"' +
                          ' :class="\'aiconfig-panel__badge--\' + resultFor(e).status"' +
                          ' :title="statusTitle(resultFor(e))">{{ statusGlyph(resultFor(e).status) }}</span>' +
                      '</td>' +
                    '</tr>' +
                    '<tr v-if="filteredEntries.length === 0">' +
                      '<td colspan="5" class="aiconfig-panel__none">{{ t(\'aiconfig.no_match\') }}</td>' +
                    '</tr>' +
                  '</tbody>' +
                '</table>' +
              '</div>' +

              '<!-- Action bar: landing mode + pull button -->' +
              '<div class="aiconfig-panel__actions glass">' +
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
            '</div>' +
          '</div>' +

        '<!-- Preview overlay -->' +
        '<transition name="dialog-fade">' +
          '<div v-if="preview.visible" class="aiconfig-preview__overlay" role="dialog" aria-modal="true" @click.self="closePreview">' +
            '<div class="aiconfig-preview glass-neo">' +
              '<div class="aiconfig-preview__header">' +
                '<code class="aiconfig-preview__path selectable">{{ preview.relPath }}</code>' +
                '<button class="settings-dialog__close" @click="closePreview" :title="t(\'ui.close\')">' +
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
      '</div>',
  };

})();
