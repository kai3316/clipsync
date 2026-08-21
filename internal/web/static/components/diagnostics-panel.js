/* ═══════════════════════════════════════════════════════════════════
   ClipSync Diagnostics Panel Component
   Standalone page for the scan-style network diagnostics (previously
   buried in Settings → Advanced). Runs the /api/diagnostics scan and
   reveals each check one by one, with a "request permission / open
   settings" action on the firewall and permissions checks.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['diagnostics-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        diagScanning: false,
        diagChecks: [],
        diagRevealed: 0,
        diagSummary: '',
      };
    },

    computed: {},

    methods: {

      runDiagnostics: function () {
        var self = this;
        self.diagScanning = true;
        self.diagChecks = [];
        self.diagRevealed = 0;
        self.diagSummary = '';
        ClipsyncAPI._fetch('GET', '/api/diagnostics').then(function (res) {
          var checks = (res && res.checks) || [];
          self.diagChecks = checks;
          self.diagSummary = (res && res.summary) || 'ok';
          // Reveal each check one by one (scan effect).
          if (checks.length === 0) {
            self.diagScanning = false;
            return;
          }
          checks.forEach(function (_, i) {
            setTimeout(function () {
              self.diagRevealed = i + 1;
              if (i === checks.length - 1) {
                setTimeout(function () { self.diagScanning = false; }, 400);
              }
            }, 350 * (i + 1));
          });
        }).catch(function () {
          self.diagScanning = false;
          self.diagChecks = [{
            id: 'error', ok: false, detail: '',
            guidance: self.t('settings_window.diag_failed'),
          }];
          self.diagRevealed = 1;
          self.diagSummary = 'fail';
        });
      },

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
        return (chk && chk.guidance) || '';
      },

      requestDiagnosticsAction: function (chk) {
        var self = this;
        var action = chk.id === 'permissions' ? 'local_network' : (chk.id === 'firewall' ? 'firewall' : null);
        if (!action) return;
        ClipsyncAPI._fetch('POST', '/api/diagnostics/request', { action: action })
          .then(function (res) {
            if (!res || res.ok !== true) {
              self.store.showToast(self.t('settings_window.diag_request_failed'), 2500);
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

        '<!-- Scan results -->' +
        '<div v-if="diagChecks.length > 0" class="overview-card glass diagnostics-panel__results">' +
          '<div class="diag-scan">' +
            '<div v-for="(chk, i) in diagChecks" :key="chk.id" class="diag-check"' +
                 ':class="{ \'diag-check--revealed\': i < diagRevealed, \'diag-check--ok\': chk.ok === true && i < diagRevealed, \'diag-check--fail\': chk.ok === false && i < diagRevealed }">' +
              '<span class="diag-check__status">{{ i < diagRevealed ? (chk.ok ? \'✓\' : \'✕\') : \'·\' }}</span>' +
              '<div class="diag-check__body">' +
                '<span class="diag-check__label">{{ diagLabel(chk.id) }}</span>' +
                '<span v-if="i < diagRevealed && diagDetail(chk)" class="diag-check__detail">{{ diagDetail(chk) }}</span>' +
                '<span v-if="i < diagRevealed && diagGuidance(chk)" class="diag-check__guidance">💡 {{ diagGuidance(chk) }}</span>' +
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
