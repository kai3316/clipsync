/* ═══════════════════════════════════════════════════════════════════
   ClipSync AI-Config MIGRATION Wizard (refactor round 1)

   One-click migration of AI-tool config + skills across devices:
     ① pick a source device (v2 peers only — legacy peers are read-only)
     ② the wizard computes the per-file diff against THIS device and shows
        it grouped by tool profile, pre-checking every differing file
     ③ a conflict strategy picks the landing mode:
          skip       (default) pull only files this device doesn't have yet
          overwrite  replace existing copies (backend keeps a .bak)
          copy       write to a copy name, existing files untouched
     ④ Apply = one batched pull (batch_id → WS aiconfig_file events →
        "N/M done" + per-file result table, failures retryable).

   The wizard is mounted by aiconfig-panel when store.aiConfigMigrateOpen is
   true; it resets its own state on mount.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  var H = window.__CLIPSYNC_AICONFIG_HELPERS__ || {};

  var STRATEGIES = [
    { value: 'skip', labelKey: 'aiconfig.migrate_strategy_skip', hintKey: 'aiconfig.migrate_strategy_skip_hint' },
    { value: 'overwrite', labelKey: 'aiconfig.migrate_strategy_overwrite', hintKey: 'aiconfig.migrate_strategy_overwrite_hint' },
    { value: 'copy', labelKey: 'aiconfig.migrate_strategy_copy', hintKey: 'aiconfig.migrate_strategy_copy_hint' },
  ];

  // Turn a key list into a {key: true} map for merging into `touched`.
  function mark(keys) {
    var out = {};
    for (var i = 0; i < keys.length; i++) out[keys[i]] = true;
    return out;
  }

  window.__CLIPSYNC_COMPONENTS__['aiconfig-migrate-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        sourcePeerId: '',
        strategy: 'skip',
        strategies: STRATEGIES,
        checked: {},           // selection key -> true
        touched: {},           // selection key -> true once the user toggled it
        batchId: '',
        running: false,
      };
    },

    computed: {
      v2Peers: function () {
        var peers = (this.store.aiConfigInventory && this.store.aiConfigInventory.peers) || {};
        return Object.keys(peers).map(function (pid) {
          var p = peers[pid];
          return { id: pid, name: p.name || pid, legacy: !!p.legacy,
                   entries: Array.isArray(p.entries) ? p.entries : [] };
        }).filter(function (p) { return !p.legacy; })
          .sort(function (a, b) { return a.name.localeCompare(b.name); });
      },

      sourcePeer: function () {
        var id = this.sourcePeerId;
        var list = this.v2Peers;
        for (var i = 0; i < list.length; i++) {
          if (list[i].id === id) return list[i];
        }
        return null;
      },

      localIndex: function () {
        var entries = (this.store.aiConfigLocal && this.store.aiConfigLocal.entries) || [];
        return H.buildLocalIndex(entries);
      },

      // Per-tool diff rows for the source peer — every entry that differs
      // from this device (missing / local_newer / remote_newer; "same" is
      // not a migration candidate).  Folder entries are excluded — a folder
      // diff is carried by its files.
      diffGroups: function () {
        var peer = this.sourcePeer;
        if (!peer || !this.store.aiConfigLocal.loaded) return [];
        var profiles = (this.store.aiConfigProfiles && this.store.aiConfigProfiles.tools) || [];
        var order = [];
        var seen = {};
        profiles.forEach(function (p) {
          if (p && p.key && !seen[p.key]) { seen[p.key] = true; order.push(p.key); }
        });
        var groups = {};
        var groupOrder = [];
        var self = this;
        peer.entries.forEach(function (e) {
          if (!e || e.is_dir) return;
          var state = H.compareState(self.localIndex, e, false);
          if (!state || state === 'same') return;
          var t = String(e.tool || 'custom');
          if (!groups[t]) { groups[t] = []; groupOrder.push(t); }
          groups[t].push({
            tool: t,
            // Carried so the row's identity survives into rowKey()/applyItems():
            // rel_path is relative to its own root, so two roots of one tool can
            // report the same rel and the tool alone is not an identity.
            root: String(e.root || ''),
            rel_path: String(e.rel_path || ''),
            state: state,
            size: e.size,
            mtime: e.mtime,
          });
        });
        groupOrder.sort(function (a, b) {
          var ai = order.indexOf(a), bi = order.indexOf(b);
          ai = ai < 0 ? order.length : ai;
          bi = bi < 0 ? order.length : bi;
          return ai - bi || a.localeCompare(b);
        });
        return groupOrder.map(function (t) {
          var rows = groups[t];
          rows.sort(function (a, b) { return a.rel_path.localeCompare(b.rel_path); });
          return {
            key: t,
            label: H.toolLabel(profiles, t),
            rows: rows,
          };
        });
      },

      diffCount: function () {
        var n = 0;
        this.diffGroups.forEach(function (g) { n += g.rows.length; });
        return n;
      },

      checkedCount: function () {
        var self = this;
        return Object.keys(this.checked).filter(function (k) { return self.checked[k]; }).length;
      },

      batch: function () {
        if (!this.batchId) return null;
        return this.store.aiConfigBatches[this.batchId] || null;
      },

      batchPct: function () {
        var b = this.batch;
        if (!b || !(b.total > 0)) return 0;
        return Math.min(100, Math.round(b.done / b.total * 100));
      },

      batchErrorCount: function () {
        var b = this.batch;
        if (!b || !Array.isArray(b.results)) return 0;
        var n = 0;
        for (var i = 0; i < b.results.length; i++) {
          if (b.results[i] && b.results[i].status === 'error') n++;
        }
        return n;
      },

      canApply: function () {
        return !!this.sourcePeer && this.checkedCount > 0 && !this.running;
      },

      strategyHint: function () {
        for (var i = 0; i < STRATEGIES.length; i++) {
          if (STRATEGIES[i].value === this.strategy) return this.t(STRATEGIES[i].hintKey);
        }
        return '';
      },

      // The landing mode the chosen strategy maps to.  'skip' also lands as
      // 'copy', but its item list is filtered to files this device is missing —
      // and the backend writes a missing file under its real name, so "skip
      // existing" genuinely means "fill the gaps".
      landingMode: function () {
        return this.strategy === 'overwrite' ? 'overwrite' : 'copy';
      },
    },

    watch: {
      sourcePeerId: function () {
        // Fresh source → re-check every diff (migration default).
        this.touched = {};
        this.syncChecks();
      },

      // The diff arrives asynchronously (peer inventory + local index land in
      // either order, and an inventory refresh can change it later), so the
      // selection has to follow it instead of being computed once on open.
      diffGroups: function () {
        this.syncChecks();
      },
    },

    mounted: function () {
      // Reset each open; default to the first v2 peer if any.
      var self = this;
      this.$nextTick(function () {
        self.checked = {};
        self.touched = {};
        self.batchId = '';
        self.running = false;
        if (self.v2Peers.length && !self.sourcePeerId) {
          self.sourcePeerId = self.v2Peers[0].id;
        }
        self.syncChecks();
      });
    },

    methods: {
      fmtSize: H.fmtSize,
      fmtTime: H.fmtTime,

      rowKey: function (row) {
        return H.keyOf(row);
      },

      defaultChecks: function () {
        var next = {};
        var self = this;
        this.diffGroups.forEach(function (g) {
          g.rows.forEach(function (row) { next[self.rowKey(row)] = true; });
        });
        return next;
      },

      // Select-all / deselect-all.  Both directions count as an explicit user
      // choice, so every visible row is marked touched — otherwise the next
      // inventory refresh would re-check everything the user just cleared.
      toggleAll: function () {
        if (this.diffCount === 0) return;
        var keys = [];
        var self = this;
        this.diffGroups.forEach(function (g) {
          g.rows.forEach(function (row) { keys.push(self.rowKey(row)); });
        });
        this.checked = (this.checkedCount === this.diffCount) ? {} : mark(keys);
        this.touched = Object.assign({}, this.touched, mark(keys));
      },

      // Re-derive the selection against the current diff, honouring anything
      // the user explicitly toggled.  Needed because diffGroups starts empty:
      // it depends on store.aiConfigLocal.loaded, so on a cold open
      // defaultChecks() ran against nothing and the panel sat there with every
      // row visible, nothing selected and Apply refusing to fire until the
      // user re-picked the source peer.
      syncChecks: function () {
        var next = {};
        var self = this;
        this.diffGroups.forEach(function (g) {
          g.rows.forEach(function (row) {
            var k = self.rowKey(row);
            // Untouched rows default to checked; a row the user unchecked
            // stays unchecked across an inventory refresh.  Rows that vanished
            // from the diff are dropped by virtue of not being visited.
            next[k] = self.touched[k] ? !!self.checked[k] : true;
            if (!next[k]) delete next[k];
          });
        });
        this.checked = next;
      },

      isChecked: function (row) {
        return !!this.checked[this.rowKey(row)];
      },

      toggleRow: function (row) {
        var k = this.rowKey(row);
        var next = Object.assign({}, this.checked);
        if (next[k]) delete next[k];
        else next[k] = true;
        this.checked = next;
        this.touched = Object.assign({}, this.touched, mark([k]));
      },

      groupChecked: function (g) {
        var self = this;
        return g.rows.length > 0 && g.rows.every(function (r) { return self.checked[self.rowKey(r)]; });
      },

      toggleGroup: function (g) {
        var target = !this.groupChecked(g);
        var next = Object.assign({}, this.checked);
        var self = this;
        var keys = [];
        g.rows.forEach(function (row) {
          var k = self.rowKey(row);
          keys.push(k);
          if (target) next[k] = true;
          else delete next[k];
        });
        this.checked = next;
        this.touched = Object.assign({}, this.touched, mark(keys));
      },

      selectSource: function (id) {
        this.sourcePeerId = id;
      },

      stateKey: function (state) {
        var keys = {
          missing: 'aiconfig.ver_missing',
          local_newer: 'aiconfig.ver_local_newer',
          remote_newer: 'aiconfig.ver_remote_newer'
        };
        return keys[state] || '';
      },

      // Strategy decides both the item list and the landing mode.
      applyItems: function () {
        var items = [];
        var self = this;
        this.diffGroups.forEach(function (g) {
          g.rows.forEach(function (row) {
            if (!self.checked[self.rowKey(row)]) return;
            if (self.strategy === 'skip' && row.state !== 'missing') return;
            items.push({ tool: row.tool, root: row.root, rel_path: row.rel_path });
          });
        });
        var mode = this.landingMode;
        return { items: items, mode: mode };
      },

      apply: function () {
        var self = this;
        var peer = this.sourcePeer;
        if (!peer || this.running) return;
        var plan = this.applyItems();
        if (plan.items.length === 0) {
          this.store.showToast(this.t('aiconfig.migrate_nothing_checked'), 3000, 'warning');
          return;
        }
        this.batchId = 'aiconfig_migrate_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
        this.store.startAiConfigBatch(this.batchId, plan.items.length, peer.id);
        this.running = true;
        ClipsyncAPI.pullAiConfigFiles(peer.id, plan.items, plan.mode, this.batchId)
          .then(function (res) {
            self.running = false;
            var n = (res && typeof res.requested === 'number') ? res.requested : plan.items.length;
            if (n === 0) {
              self.store.clearAiConfigBatch(self.batchId);
              self.batchId = '';
              var reasons = (res && Array.isArray(res.errors)) ? res.errors : [];
              self.store.showToast(
                reasons.length
                  ? (self.t('aiconfig.pull_failed') + ' (' + reasons.join(', ') + ')')
                  : self.t('aiconfig.pull_failed'),
                3200, 'error');
            }
          })
          .catch(function (e) {
            self.running = false;
            self.store.clearAiConfigBatch(self.batchId);
            self.batchId = '';
            var reasons = (e && e.data && Array.isArray(e.data.errors)) ? e.data.errors : [];
            self.store.showToast(
              reasons.length
                ? (self.t('aiconfig.pull_failed') + ' (' + reasons.join(', ') + ')')
                : self.t('aiconfig.pull_failed'),
              3200, 'error');
          });
      },

      retryFailures: function () {
        var self = this;
        var peer = this.sourcePeer;
        var b = this.batch;
        if (!peer || !b || !Array.isArray(b.results)) return;
        var items = [];
        b.results.forEach(function (r) {
          if (r && r.status === 'error' && r.rel_path) {
            items.push({
              tool: String(r.tool || 'custom'),
              root: String(r.root || ''),
              rel_path: r.rel_path,
            });
          }
        });
        if (items.length === 0) return;
        this.batchId = 'aiconfig_migrate_' + Date.now() + '_r_' + Math.random().toString(36).slice(2, 8);
        this.store.startAiConfigBatch(this.batchId, items.length, peer.id);
        // Retry under the SAME landing mode the user chose, not a hardcoded
        // 'copy': retrying an "Overwrite" migration used to silently downgrade
        // to side-by-side copies, so the files the user asked to replace were
        // left in place with ".from.<device>" siblings next to them.
        ClipsyncAPI.pullAiConfigFiles(peer.id, items, this.landingMode, this.batchId)
          .catch(function () {
            self.store.clearAiConfigBatch(self.batchId);
            self.batchId = '';
          });
      },

      statusGlyph: function (status) {
        switch (status) {
          case 'saved': return '✓';
          case 'copied': return '⧉';
          case 'appended': return '+';
          default: return '✕';
        }
      },

      close: function () {
        this.store.closeAiConfigMigrate();
      },
    },

    template:
      '<div class="aiconfig-migrate__overlay" role="dialog" aria-modal="true" @click.self="close">' +
        '<div class="aiconfig-migrate glass-neo">' +
          '<div class="aiconfig-migrate__header">' +
            '<span class="aiconfig-migrate__title">🪄 {{ t(\'aiconfig.migrate_title\') }}</span>' +
            '<button class="settings-dialog__close" @click="close" :title="t(\'ui.close\')" :aria-label="t(\'ui.close\')">' +
              '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
                '<line x1="18" y1="6" x2="6" y2="18"></line>' +
                '<line x1="6" y1="6" x2="18" y2="18"></line>' +
              '</svg>' +
            '</button>' +
          '</div>' +

          '<div v-if="v2Peers.length === 0" class="aiconfig-migrate__empty">' +
            '<div class="panel-empty-icon">🤖</div>' +
            '<div class="panel-empty-title">{{ t(\'aiconfig.migrate_no_source\') }}</div>' +
            '<div class="panel-empty-desc">{{ t(\'aiconfig.migrate_no_source_desc\') }}</div>' +
          '</div>' +

          '<template v-else>' +
            '<!-- Step 1: source device -->' +
            '<div class="aiconfig-migrate__step">' +
              '<div class="aiconfig-migrate__step-label">{{ t(\'aiconfig.migrate_source\') }}</div>' +
              '<div class="aiconfig-migrate__sources">' +
                '<button v-for="p in v2Peers" :key="p.id"' +
                  ' class="aiconfig-migrate__source"' +
                  ' :class="{ \'aiconfig-migrate__source--active\': p.id === sourcePeerId }"' +
                  ' @click="selectSource(p.id)">' +
                  '{{ p.name }}' +
                '</button>' +
              '</div>' +
            '</div>' +

            '<template v-if="sourcePeer">' +
              '<!-- Step 2: diff checklist -->' +
              '<div class="aiconfig-migrate__diffbar">' +
                '<span class="aiconfig-migrate__diffcount">{{ t(\'aiconfig.migrate_diff_count\', { count: diffCount }) }}</span>' +
                '<label class="aiconfig-panel__selectall">' +
                  '<input type="checkbox" :checked="checkedCount > 0 && checkedCount === diffCount" @change="toggleAll()"> {{ t(\'aiconfig.select_all\') }}' +
                '</label>' +
              '</div>' +

              '<div v-if="diffGroups.length === 0" class="aiconfig-migrate__empty">' +
                '<div class="panel-empty-icon">✅</div>' +
                '<div class="panel-empty-title">{{ t(\'aiconfig.migrate_nothing_to_do\') }}</div>' +
              '</div>' +

              '<div v-else class="aiconfig-migrate__groups">' +
                '<div v-for="g in diffGroups" :key="g.key" class="aiconfig-migrate__group">' +
                  '<div class="aiconfig-migrate__group-head">' +
                    '<label class="aiconfig-panel__selectall">' +
                      '<input type="checkbox" :checked="groupChecked(g)" @change="toggleGroup(g)"> {{ g.label }}' +
                    '</label>' +
                    '<span class="aiconfig-migrate__group-count">{{ g.rows.length }}</span>' +
                  '</div>' +
                  '<div class="aiconfig-migrate__rows">' +
                    '<label v-for="row in g.rows" :key="rowKey(row)" class="aiconfig-migrate__row">' +
                      '<input type="checkbox" :checked="isChecked(row)" @change="toggleRow(row)">' +
                      '<code class="aiconfig-migrate__path selectable">{{ row.rel_path }}</code>' +
                      '<span class="aiconfig-panel__ver-badge" :class="\'aiconfig-panel__ver-badge--\' + row.state" :title="t(stateKey(row.state))">{{ t(stateKey(row.state)) }}</span>' +
                    '</label>' +
                  '</div>' +
                '</div>' +
              '</div>' +

              '<!-- Step 3: strategy + apply -->' +
              '<div class="aiconfig-migrate__strategy">' +
                '<span class="aiconfig-panel__modes-label">{{ t(\'aiconfig.migrate_strategy_label\') }}</span>' +
                '<div class="aiconfig-panel__modes">' +
                  '<label v-for="s in strategies" :key="s.value" class="aiconfig-panel__mode" :title="t(s.hintKey)">' +
                    '<input type="radio" name="aiconfig-migrate-strategy" :value="s.value" v-model="strategy"> {{ t(s.labelKey) }}' +
                  '</label>' +
                '</div>' +
                '<span class="aiconfig-panel__mode-hint">{{ strategyHint }}</span>' +
              '</div>' +

              '<div class="aiconfig-migrate__footer">' +
                '<button class="settings-btn" @click="close">{{ t(\'ui.cancel\') }}</button>' +
                '<button class="settings-btn settings-btn--accent" @click="apply" :disabled="!canApply">' +
                  '{{ running ? \'...\' : t(\'aiconfig.migrate_apply\', { count: checkedCount }) }}' +
                '</button>' +
              '</div>' +

              '<div v-if="batch" class="aiconfig-panel__batch aiconfig-migrate__batch">' +
                '<div class="aiconfig-panel__batch-head">' +
                  '<span class="aiconfig-panel__batch-label">{{ t(\'aiconfig.batch_progress\', { done: batch.done, total: batch.total }) }}</span>' +
                  '<div class="aiconfig-panel__batch-actions">' +
                    '<button v-if="batch.finished && batchErrorCount > 0" class="settings-btn settings-btn--sm" @click="retryFailures">↻ {{ t(\'aiconfig.batch_retry\') }} ({{ batchErrorCount }})</button>' +
                    '<button v-if="batch.finished" class="settings-btn settings-btn--sm" @click="close">✕ {{ t(\'aiconfig.batch_done\') }}</button>' +
                  '</div>' +
                '</div>' +
                '<div class="aiconfig-panel__batch-bar">' +
                  '<div class="aiconfig-panel__batch-bar-fill" :style="{ width: batchPct + \'%\' }"></div>' +
                '</div>' +
                '<div v-if="batch.finished && batch.results.length" class="aiconfig-panel__batch-results">' +
                  '<span v-for="(r, i) in batch.results" :key="i" class="aiconfig-panel__batch-result"' +
                    ' :class="\'aiconfig-panel__badge--\' + r.status"' +
                    ' :title="(r.rel_path || \'\') + (\' · \' + t(r.status === \'error\' ? \'aiconfig.result_error\' : r.status === \'copied\' ? \'aiconfig.result_copied\' : r.status === \'appended\' ? \'aiconfig.result_appended\' : \'aiconfig.result_saved\'))">{{ statusGlyph(r.status) }}</span>' +
                '</div>' +
              '</div>' +
            '</template>' +
          '</template>' +
        '</div>' +
      '</div>',
  };

})();
