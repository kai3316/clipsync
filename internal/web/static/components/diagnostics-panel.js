/* ═══════════════════════════════════════════════════════════════════
   ClipSync Diagnostics Panel Component
   Standalone page for the scan-style diagnostics (previously buried in
   Settings → Advanced). Runs the /api/diagnostics scan and reveals each
   check one by one, with a "request permission / open settings" action on
   the firewall and permissions checks.

   Round 19: the backend now returns a comprehensive grouped payload
   (``v2: true`` + ``groups``) covering system / network / internet sync /
   AI config / chat / transfers / storage.  This panel renders the groups
   as collapsible cards (each item reveals in the scan animation), and
   falls back to the legacy flat ``checks`` list when talking to an older
   backend that predates the grouped contract.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  // Fixed order + locale label for each diagnostic group.  A group missing
  // from the server payload renders as an "unavailable" card instead of
  // vanishing, so an older/partial backend never silently hides a category.
  var DIAG_GROUP_DEFS = [
    { id: 'system',     labelKey: 'diag.v2.group.system' },
    { id: 'network',    labelKey: 'diag.v2.group.network' },
    { id: 'internet',   labelKey: 'diag.v2.group.internet' },
    { id: 'ai_config',  labelKey: 'diag.v2.group.ai_config' },
    { id: 'chat',       labelKey: 'diag.v2.group.chat' },
    { id: 'transfer',   labelKey: 'diag.v2.group.transfer' },
    { id: 'filesystem', labelKey: 'diag.v2.group.filesystem' },
  ];

  var DIAG_V2_ITEM_LABELS = {
    app_version: 'diag.v2.item.app_version',
    uptime: 'diag.v2.item.uptime',
    data_dir: 'diag.v2.item.data_dir',
    log_path: 'diag.v2.item.log_path',
    lan_ip: 'diag.v2.item.lan_ip',
    tcp_port: 'diag.v2.item.tcp_port',
    mdns_service: 'diag.v2.item.mdns_service',
    web_service: 'diag.v2.item.web_service',
    firewall: 'diag.v2.item.firewall',
    internet_enabled: 'diag.v2.item.internet_enabled',
    relay_state: 'diag.v2.item.relay_state',
    brokers: 'diag.v2.item.brokers',
    netpair_count: 'diag.v2.item.netpair_count',
    pending_count: 'diag.v2.item.pending_count',
    watch_roots: 'diag.v2.item.watch_roots',
    local_entries: 'diag.v2.item.local_entries',
    last_collected: 'diag.v2.item.last_collected',
    trash_size: 'diag.v2.item.trash_size',
    chat_sessions: 'diag.v2.item.chat_sessions',
    active_transfers: 'diag.v2.item.active_transfers',
    transfer_failures: 'diag.v2.item.transfer_failures',
    history_db_size: 'diag.v2.item.history_db_size',
    disk_free: 'diag.v2.item.disk_free',
  };

  window.__CLIPSYNC_COMPONENTS__['diagnostics-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        diagScanning: false,
        diagChecks: [],          // legacy flat checks (pre-v2 backends)
        diagGroups: null,        // {group_id: {items, label_key}} (v2)
        diagGroupStart: {},      // group_id -> flat reveal index of first item
        diagRevealed: 0,
        diagSummary: '',
        diagCollapsed: {},
        diagGroupDefs: DIAG_GROUP_DEFS,  // order + label keys, exposed for the template
      };
    },

    computed: {},

    beforeUnmount: function () {
      if (this._diagTimers && this._diagTimers.length) {
        for (var i = 0; i < this._diagTimers.length; i++) {
          clearTimeout(this._diagTimers[i]);
        }
        this._diagTimers = [];
      }
    },

    methods: {

      runDiagnostics: function () {
        var self = this;
        self.diagScanning = true;
        self.diagChecks = [];
        self.diagGroups = null;
        self.diagGroupStart = {};
        self.diagRevealed = 0;
        self.diagSummary = '';
        ClipsyncAPI._fetch('GET', '/api/diagnostics').then(function (res) {
          if (res && res.groups) {
            self._applyDiagGroups(res);
          } else {
            self._applyLegacyChecks(res);
          }
        }).catch(function () {
          self.diagScanning = false;
          self.diagChecks = [{
            id: 'error', ok: false, detail: '',
            guidance: self.t('settings_window.diag_failed'),
          }];
          self.diagGroups = null;
          self.diagRevealed = 1;
          self.diagSummary = 'fail';
        });
      },

      // ── v2 grouped payload ─────────────────────────────────────
      _applyDiagGroups: function (res) {
        var self = this;
        var groups = res.groups || {};
        var starts = {};
        var start = 0;
        DIAG_GROUP_DEFS.forEach(function (def) {
          var g = groups[def.id];
          starts[def.id] = start;
          start += (g && g.items && g.items.length) || 0;
        });
        self.diagGroups = groups;
        self.diagGroupStart = starts;
        self.diagSummary = self._groupsSummary(groups);
        self.diagRevealed = 0;
        if (start === 0) {
          self.diagScanning = false;
          return;
        }
        self._diagTimers = self._diagTimers || [];
        for (var i = 0; i < start; i++) {
          (function (idx) {
            self._diagTimers.push(setTimeout(function () { self.diagRevealed = idx + 1; }, 350 * (idx + 1)));
          })(i);
        }
        self._diagTimers.push(setTimeout(function () { self.diagScanning = false; }, 350 * start + 400));
      },

      _groupsSummary: function (groups) {
        var hasFail = false, hasWarn = false;
        DIAG_GROUP_DEFS.forEach(function (def) {
          var g = groups && groups[def.id];
          if (!g || !g.items || !g.items.length) {
            // A missing / empty group is not a pass — surface a warning.
            hasWarn = true;
            return;
          }
          g.items.forEach(function (it) {
            if (it.status === 'fail') hasFail = true;
            else if (it.status === 'warn') hasWarn = true;
          });
        });
        if (hasFail) return 'fail';
        if (hasWarn) return 'warn';
        return 'ok';
      },

      // ── legacy flat checks (pre-v2 backends) ───────────────────
      _applyLegacyChecks: function (res) {
        var self = this;
        var checks = (res && res.checks) || [];
        self.diagChecks = checks;
        self.diagSummary = (res && res.summary) || 'ok';
        if (checks.length === 0) {
          self.diagScanning = false;
          return;
        }
        self._diagTimers = self._diagTimers || [];
        checks.forEach(function (_, i) {
          self._diagTimers.push(setTimeout(function () {
            self.diagRevealed = i + 1;
            if (i === checks.length - 1) {
              self._diagTimers.push(setTimeout(function () { self.diagScanning = false; }, 400));
            }
          }, 350 * (i + 1)));
        });
      },

      // ── group helpers ──────────────────────────────────────────
      groupItems: function (groupId) {
        var g = this.diagGroups && this.diagGroups[groupId];
        return (g && g.items) || [];
      },

      groupStart: function (groupId) {
        return this.diagGroupStart[groupId] || 0;
      },

      groupUnavailable: function (groupId) {
        return this.groupItems(groupId).length === 0;
      },

      itemRevealed: function (groupId, j) {
        return this.groupStart(groupId) + j < this.diagRevealed;
      },

      diagTotalItems: function () {
        var self = this;
        var total = 0;
        DIAG_GROUP_DEFS.forEach(function (def) {
          total += self.groupItems(def.id).length;
        });
        return total;
      },

      groupStatus: function (groupId) {
        var items = this.groupItems(groupId);
        // An empty/unavailable group is not a pass — render a warn badge.
        if (items.length === 0) return 'warn';
        var hasFail = false, hasWarn = false;
        for (var i = 0; i < items.length; i++) {
          if (items[i].status === 'fail') hasFail = true;
          else if (items[i].status === 'warn') hasWarn = true;
        }
        if (hasFail) return 'fail';
        if (hasWarn) return 'warn';
        return 'ok';
      },

      groupStatusText: function (status) {
        if (status === 'ok') return this.t('diag.v2.status.ok');
        if (status === 'warn') return this.t('diag.v2.status.warn');
        return this.t('diag.v2.status.fail');
      },

      toggleGroup: function (groupId) {
        var c = Object.assign({}, this.diagCollapsed);
        c[groupId] = !c[groupId];
        this.diagCollapsed = c;
      },

      isCollapsed: function (groupId) {
        return !!this.diagCollapsed[groupId];
      },

      statusIcon: function (status) {
        if (status === 'ok') return '✓';
        if (status === 'warn') return '!';
        return '✕';
      },

      statusClass: function (status) {
        if (status === 'ok') return 'ok';
        if (status === 'warn') return 'warn';
        return 'fail';
      },

      // ── labels / detail / hint resolution ──────────────────────
      diagLabel: function (id) {
        var labels = {
          server_port: this.t('settings_window.diag_server_port'),
          discovery: this.t('settings_window.diag_discovery'),
          advertising: this.t('settings_window.diag_advertising'),
          web_companion: this.t('settings_window.diag_web'),
          network: this.t('settings_window.diag_network'),
          firewall: this.t('settings_window.diag_firewall'),
          permissions: this.t('settings_window.diag_permissions'),
          mdns: this.t('settings_window.diag_mdns'),
          clipboard_tool: this.t('settings_window.diag_clipboard_tool'),
          error: this.t('settings_window.diag_title'),
        };
        return labels[id] || id;
      },

      diagV2ItemLabel: function (item) {
        var key = DIAG_V2_ITEM_LABELS[item.id] || item.id;
        return this.t(key);
      },

      // Resolve the per-check detail line. Prefers the server-supplied i18n
      // key; falls back to the raw server string (e.g. for new checks).
      diagDetail: function (chk) {
        if (chk && chk.detail_key) {
          var tr = this.t(chk.detail_key, chk.detail_params || {});
          if (tr !== chk.detail_key) return tr;
        }
        return (chk && chk.detail) || '';
      },

      // Resolve the per-check guidance text. Prefers the server-supplied i18n
      // key; falls back to the raw server string.
      diagGuidance: function (chk) {
        if (chk && chk.guidance_key) {
          var tr = this.t(chk.guidance_key, chk.guidance_params || {});
          if (tr !== chk.guidance_key) return tr;
        }
        if (chk && chk.hint_key) {
          var htr = this.t(chk.hint_key, chk.hint_params || {});
          if (htr !== chk.hint_key) return htr;
        }
        return (chk && (chk.guidance || chk.hint)) || '';
      },

      requestDiagnosticsAction: function (chk) {
        var self = this;
        var action = chk.id === 'permissions' ? 'local_network' : (chk.id === 'firewall' ? 'firewall' : null);
        if (!action) return;
        ClipsyncAPI._fetch('POST', '/api/diagnostics/request', { action: action })
          .then(function (res) {
            if (!res || res.ok !== true) {
              self.store.showToast(self.t('settings_window.diag_request_failed'), 2500);
              return;
            }
            if (action === 'firewall') {
              // The rule may be applied by an elevated netsh process — wait a
              // moment, then re-scan so the fixed rule shows up as OK.
              self.store.showToast(self.t('settings_window.diag_request_done'), 2500);
              setTimeout(function () { self.runDiagnostics(); }, 2500);
            }
          })
          .catch(function () {
            self.store.showToast(self.t('settings_window.diag_request_failed'), 2500);
          });
      },

      diagSummaryText: function () {
        if (this.diagSummary === 'ok') return this.t('settings_window.diag_all_ok');
        if (this.diagSummary === 'warn') return this.t('settings_window.diag_warn');
        return this.t('settings_window.diag_fail');
      },
    },

    template:
      '<div class="diagnostics-panel">' +

        '<!-- Header -->' +
        '<div class="favorites-panel__header">' +
          '<div class="favorites-panel__header-left">' +
            '<span class="favorites-panel__header-icon">🩺</span>' +
            '<span class="favorites-panel__header-title">{{ t(\'ui.diagnostics\') }}</span>' +
          '</div>' +
        '</div>' +

        '<!-- Intro + run button -->' +
        '<div class="overview-card glass">' +
          '<p class="diagnostics-panel__intro">{{ t(\'diag.intro\') }}</p>' +
          '<button class="settings-btn settings-btn--accent diagnostics-panel__run" @click="runDiagnostics" :disabled="diagScanning">' +
            '{{ diagScanning ? t(\'settings_window.diag_scanning\') : t(\'settings_window.diag_run\') }}' +
          '</button>' +
        '</div>' +

        '<!-- Grouped results (v2) -->' +
        '<div v-if="diagGroups" class="overview-card glass diagnostics-panel__results">' +
          '<div class="diag-groups">' +
            '<div v-for="def in diagGroupDefs" :key="def.id" class="diag-group"' +
                 ':class="{ \'diag-group--revealed\': groupStart(def.id) < diagRevealed }">' +
              '<button class="diag-group__header" @click="toggleGroup(def.id)" :aria-expanded="!isCollapsed(def.id)" :aria-controls="\'diag-group-items-\' + def.id">' +
                '<span class="diag-group__chevron">{{ isCollapsed(def.id) ? \'▸\' : \'▾\' }}</span>' +
                '<span class="diag-group__title">{{ t(def.labelKey) }}</span>' +
                '<span v-if="!groupUnavailable(def.id)" class="diag-group__count">{{ groupItems(def.id).length }}</span>' +
                '<span class="diag-group__status" :class="\'diag-group__status--\' + groupStatus(def.id)">{{ groupStatusText(groupStatus(def.id)) }}</span>' +
              '</button>' +
              '<div v-if="!isCollapsed(def.id)" :id="\'diag-group-items-\' + def.id" class="diag-group__items">' +
                '<div v-if="groupUnavailable(def.id)" class="diag-group__unavailable">' +
                  '{{ t(\'diag.v2.group.unavailable\') }}' +
                '</div>' +
                '<div v-for="(item, j) in groupItems(def.id)" :key="item.id" class="diag-check"' +
                     ':class="{ \'diag-check--revealed\': itemRevealed(def.id, j), \'diag-check--ok\': itemRevealed(def.id, j) && item.status === \'ok\', \'diag-check--warn\': itemRevealed(def.id, j) && item.status === \'warn\', \'diag-check--fail\': itemRevealed(def.id, j) && item.status === \'fail\' }">' +
                  '<span class="diag-check__status">{{ itemRevealed(def.id, j) ? statusIcon(item.status) : \'·\' }}</span>' +
                  '<div class="diag-check__body">' +
                    '<span class="diag-check__label selectable">{{ diagV2ItemLabel(item) }}</span>' +
                    '<span v-if="itemRevealed(def.id, j) && diagDetail(item)" class="diag-check__detail selectable">{{ diagDetail(item) }}</span>' +
                    '<span v-if="itemRevealed(def.id, j) && diagGuidance(item)" class="diag-check__guidance selectable">💡 {{ diagGuidance(item) }}</span>' +
                    '<button v-if="itemRevealed(def.id, j) && item.id === \'firewall\' && item.status === \'fail\'" class="settings-btn settings-btn--sm diagnostics-panel__action" style="align-self:flex-start;margin-top:4px" @click="requestDiagnosticsAction(item)">{{ t(\'settings_window.diag_request\') }}</button>' +
                  '</div>' +
                '</div>' +
              '</div>' +
            '</div>' +
          '</div>' +
          '<div v-if="!diagScanning && diagRevealed >= diagTotalItems()" class="diag-summary" :class="\'diag-summary--\' + diagSummary">' +
            '{{ diagSummaryText() }}' +
          '</div>' +
        '</div>' +

        '<!-- Legacy flat results (pre-v2 backends) -->' +
        '<div v-else-if="diagChecks.length > 0" class="overview-card glass diagnostics-panel__results">' +
          '<div class="diag-scan">' +
            '<div v-for="(chk, i) in diagChecks" :key="chk.id" class="diag-check"' +
                 ':class="{ \'diag-check--revealed\': i < diagRevealed, \'diag-check--ok\': chk.ok === true && i < diagRevealed, \'diag-check--fail\': chk.ok === false && i < diagRevealed }">' +
              '<span class="diag-check__status">{{ i < diagRevealed ? (chk.ok ? \'✓\' : \'✕\') : \'·\' }}</span>' +
              '<div class="diag-check__body">' +
                '<span class="diag-check__label selectable">{{ diagLabel(chk.id) }}</span>' +
                '<span v-if="i < diagRevealed && diagDetail(chk)" class="diag-check__detail selectable">{{ diagDetail(chk) }}</span>' +
                '<span v-if="i < diagRevealed && diagGuidance(chk)" class="diag-check__guidance selectable">💡 {{ diagGuidance(chk) }}</span>' +
                '<button v-if="i < diagRevealed && (chk.id === \'firewall\' || chk.id === \'permissions\')" class="settings-btn settings-btn--sm diagnostics-panel__action" style="align-self:flex-start;margin-top:4px" @click="requestDiagnosticsAction(chk)">{{ t(\'settings_window.diag_request\') }}</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
          '<div v-if="!diagScanning && diagChecks.length > 0 && diagRevealed >= diagChecks.length" class="diag-summary" :class="\'diag-summary--\' + diagSummary">' +
            '{{ diagSummaryText() }}' +
          '</div>' +
        '</div>' +

        '<!-- Empty state (before first scan) -->' +
        '<div v-else class="panel-empty">' +
          '<div class="panel-empty-icon">🩺</div>' +
          '<div class="panel-empty-title">{{ t(\'diag.empty_title\') }}</div>' +
          '<div class="panel-empty-desc">{{ t(\'diag.empty_desc\') }}</div>' +
        '</div>' +
      '</div>',
  };

})();
