/* ═══════════════════════════════════════════════════════════════════
   ClipSync Settings Dialog Component
   Centered modal dialog with tab navigation on the left and content
   on the right. Replaces the old slide-in panel for a clean, spacious
   settings experience.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  // Settings-search index (classic-desktop parity): every section's i18n
  // keys, so a query typed into the header box is matched against the
  // localized labels the user actually sees — the same visible-text matching
  // the classic settings window uses over its widget tree. Both English and
  // Chinese queries work because only the ACTIVE locale's strings are tested.
  var SETTINGS_SEARCH_KEYS = {
    appearance: [
      'settings.appearance', 'settings_window.theme_system',
      'settings_window.theme_light', 'settings_window.theme_dark',
      'settings.language', 'settings.language_hint', 'settings.preferences',
      'network.auto_start', 'settings.animation', 'settings.ui_mode',
      'settings_window.ui_backend_hint',
    ],
    network: [
      'network.connection', 'network.tcp_port', 'settings_window.port_hint',
      'network.service_type',
      'settings_window.service_type_hint', 'network.local_address',
      'settings_window.save_network',
      'settings_window.internet_sync_title', 'network.internet_sync',
      'settings_window.internet_sync_hint', 'settings_window.relay_brokers_label',
      // Round 15: pairing management moved to the Devices page; the network
      // section keeps the toggle + status row and a pointer to the Devices tab.
      'relay.state.initial',
      'settings_window.netpair_error_detail',
      'settings_window.netpair_manage_hint', 'settings_window.netpair_manage_cta',
    ],
    web: [
      'settings_nav.web_companion', 'settings_window.web_enable',
      'settings_window.web_enable_webview_hint', 'settings_window.web_port',
      'settings_window.web_port_hint', 'settings_window.web_history_limit',
      'settings_window.web_history_limit_desc', 'settings_window.web_token',
      'settings_window.web_token_regenerate', 'settings_window.web_token_clear',
      'settings_window.web_local_url', 'settings_window.save_web',
    ],
    translation: [
      'settings_window.translation_title', 'settings_window.translation_hint',
      'settings_window.translate_url', 'settings_window.translate_api_key',
      'settings_window.translate_key_set', 'settings_window.translate_key_not_set',
      'settings_window.clear_translate_key', 'settings_window.save_translation',
    ],
    filter: [
      'settings_window.filter_title', 'settings_window.filter_desc',
      'settings_window.filter_categories', 'filter.credit_card', 'filter.ssn',
      'filter.api_key', 'filter.private_key', 'filter.password',
      'settings_window.app_filter_title', 'settings_window.app_filter_desc',
      'settings_window.app_filter_enable', 'settings_window.app_filter_mode',
      'settings_window.app_filter_blacklist', 'settings_window.app_filter_whitelist',
      'settings_window.app_filter_list', 'settings_window.save_filter',
    ],
    security: [
      'security.title', 'settings_window.security_desc',
      'settings_window.encryption_title', 'settings_window.enable_encryption',
      'security.pre_shared_password', 'security.password_set',
      'security.no_password', 'settings_window.password_hint',
      'settings_window.encryption_hint', 'settings_window.notify_title',
      'settings.sound', 'settings_window.notify_device_connect',
      'settings_window.notify_transfer', 'settings_window.notify_pairing',
      'settings_window.notify_sync', 'settings_window.certs_title',
      'settings_window.certs_empty', 'settings_window.save_security',
    ],
    advanced: [
      'settings_window.advanced_title', 'settings_window.history_max',
      'settings_window.history_max_age', 'settings_window.sync_debounce',
      'settings_window.poll_interval', 'settings_window.receive_dir',
      'settings_window.transfer_timeout', 'settings_window.max_reconnect',
      'settings_window.log_level', 'settings_window.enable_notifications',
      'settings_window.clipboard_behavior', 'settings_window.paste_to_top',
      'settings_window.low_memory_mode', 'settings_window.retry_capture',
      'settings_window.source_tracking', 'settings_window.plain_text_only',
      'settings_window.dedup_method', 'hotkeys.enabled', 'hotkeys.title',
      'settings_window.save_advanced',
    ],
    logs: [
      'settings_window.logs_title', 'settings_window.logs_refresh',
      'settings_window.logs_export', 'settings_window.no_logs',
    ],
    data: [
      'settings.data', 'settings.export_json', 'settings.export_csv',
      'settings.export_markdown', 'settings.import', 'settings.create_backup',
      'settings.backup_list', 'settings.restore_backup',
      'settings.open_data_folder', 'settings.open_backups_folder',
      'settings.data_dir', 'settings.favorites_path', 'settings.save_data_paths',
    ],
    aiconfig: [
      'settings_nav.aiconfig', 'settings_window.aiconfig_desc',
      'settings_window.aiconfig_paths_label', 'settings_window.aiconfig_paths_hint',
      'settings_window.aiconfig_path_placeholder', 'settings_window.aiconfig_add_path',
      'settings_window.save_aiconfig', 'settings_window.aiconfig_save_hint',
    ],
    about: [
      'settings.about', 'settings.version', 'settings.device_name',
      'settings.device_id', 'overview.platform', 'settings_window.about_desc',
      'settings_window.auto_update_check', 'settings_window.auto_update_check_hint',
      'settings_window.update_check_now', 'settings_window.update_install_now',
      'settings_window.update_download',
    ],
    danger: [
      'settings_window.danger_zone', 'settings_window.danger_zone_desc',
      'settings_window.restart_app', 'settings_window.factory_reset',
    ],
  };

  window.__CLIPSYNC_COMPONENTS__['settings-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        activeSection: 'appearance',

        // Header search box: filters/jumps between sections by matching the
        // query against each section's localized setting labels.
        searchQuery: '',

        // Network
        port: '',
        autoStart: false,
        serviceType: '',

        // Internet (cross-network) sync.  The toggle saves immediately (the
        // host live-applies it and broadcasts relay_state transitions); the
        // broker list is staged behind the advanced fold with its own save.
        // Pairing management itself lives on the Devices page (round 15).
        internetSyncEnabled: false,
        relayBrokersText: '',
        brokersSaving: false,
        brokersOpen: false,
        relayTesting: false,
        relayTestResult: null,   // {summary, results:[{endpoint,ok,latency_ms,detail}]}

        // Web Companion
        webEnabled: true,
        webPort: '',
        webHistoryLimit: 10,
        webToken: '',
        webLanIp: '',

        // Content Filter
        filterEnabled: false,
        filterCreditCard: true,
        filterSSN: true,
        filterApiKey: true,
        filterPrivateKey: true,
        filterPassword: true,

        // App Filter (which apps are monitored)
        appFilterEnabled: false,
        appFilterMode: 'blacklist',
        appFilterList: '',

        // Security
        encryptionEnabled: true,
        passwordValue: '',
        passwordSet: false,
        showPassword: false,

        // Translation
        translateUrl: '',
        translateKeyValue: '',
        translateKeySet: false,
        showTranslateKey: false,
        translateSaving: false,

        // Advanced
        historyMax: 200,
        historyMaxAgeDays: 0,
        syncDebounce: 0.5,
        pollInterval: 0.5,
        receiveDir: '',
        transferTimeout: 300,
        maxReconnect: 10,
        logLevel: 'INFO',
        notificationsEnabled: true,

        // Clipboard behavior
        pasteToTop: true,
        lowMemory: false,
        retryCapture: true,
        dedupMethod: 'sha256',
        sourceTracking: true,
        plainTextOnly: false,

        // Data locations
        dataDir: '',
        favoritesPath: '',
        dataSaving: false,

        // AI-config sync: root folders whose config files (CLAUDE.md, memory
        // md, skills, ...) paired devices may browse/pull. Staged behind a
        // save button like the other path settings.
        aiConfigPaths: [],
        aiConfigSaving: false,

        // Hotkeys
        hotkeys: {},
        hotkeysEnabled: false,

        // Notifications (per-event toggles)
        notifyDeviceConnect: true,
        notifyTransfer: true,
        notifyPairing: true,
        notifySync: true,

        // Logs
        logs: '',
        logsLoading: false,

        // Trusted devices / certificates
        certDevices: [],
        certsLoading: false,

        // Update download / check
        updateDownloading: false,
        updateChecking: false,
        // null = not checked yet; true/false = result of the last check
        updateAvailable: null,
        updateLatest: '',
        autoUpdateCheck: true,

        // States
        saving: false,
        exporting: false,
        importing: false,
        backingUp: false,
        restoring: false,
        backupList: [],
        backupLoading: false,
        networkSaving: false,
        webSaving: false,
        filterSaving: false,
        securitySaving: false,
        advancedSaving: false,
        resetting: false,
        restarting: false,

        // Unsaved-changes tracking for staged sections (section id -> true).
        dirtySections: {},
        // True while populateFromCache() is filling local fields so those
        // programmatic writes don't mark sections as dirty.
        _skipDirty: false,
        // Element focused before the dialog opened (restored on close).
        _prevFocus: null,
        // True while the "discard unsaved changes?" confirm is up, so an Escape
        // (which the client-dialog handles by cancelling) doesn't reopen it.
        _closingPromptOpen: false,
      };
    },

    computed: {
      visible: function () {
        return this.store.settingsPanelVisible;
      },

      locales: function () {
        // Only locales that actually ship a JSON translation file under
        // static/locales/. Offering more would present choices that fall back
        // to English silently.
        return [
          { code: 'en', label: 'English' },
          { code: 'zh-CN', label: '中文 (简体)' },
        ];
      },

      currentLocale: function () {
        if (typeof ClipsyncI18n !== 'undefined' && ClipsyncI18n.ready) {
          return ClipsyncI18n.locale || 'en';
        }
        return 'en';
      },

      uiBackend: function () {
        return this.store.uiBackend || 'webview';
      },

      // True when any staged section has unsaved edits.
      hasDirtySections: function () {
        var d = this.dirtySections;
        return Object.keys(d).some(function (k) { return !!d[k]; });
      },

      // Derived from the live web-port / LAN-IP fields so editing the port (or
      // the token) is immediately reflected in the displayed + copied URL
      // without a separate cached data field going stale.
      webLocalUrl: function () {
        if (!this.webLanIp || !this.webPort) return '';
        // Deliver the lightweight phone page (mobile.html), matching the QR
        // code, so a phone opening the copied URL gets the mobile UI instead
        // of the heavy desktop dashboard.
        return 'http://' + this.webLanIp + ':' + this.webPort +
          '/mobile.html?token=' + encodeURIComponent(this.store.token);
      },

      // Live internet-sync state.  The WS `relay_state` event keeps
      // store.relayState current; before the first event/fetch arrives the
      // settings snapshot's copy is used ('' only before the very first load).
      effectiveRelayState: function () {
        if (this.store.relayState) return this.store.relayState;
        var cache = this.store.settingsCache || {};
        return cache.internet_sync_state || 'off';
      },

      // relay_state=off while internet sync is ENABLED is the moment right
      // before the host starts connecting — show an "initial" state instead of
      // a confusing "Off".
      relayDisplayState: function () {
        var state = this.effectiveRelayState || 'off';
        if (this.internetSyncEnabled && state === 'off') return 'initial';
        return state;
      },

      relayStateKey: function () {
        return 'relay.state.' + (this.relayDisplayState || 'off');
      },

      relayStateColor: function () {
        switch (this.relayDisplayState) {
          case 'online': return 'var(--clipsync-success)';
          case 'connecting': return 'var(--clipsync-warning)';
          case 'initial': return 'var(--clipsync-warning)';
          case 'error': return 'var(--clipsync-danger)';
          default: return 'var(--clipsync-fg-muted)';
        }
      },

      // Detailed, actionable text shown only in the relay error state — the
      // status line never leaves a bare red "Error" with no explanation.
      relayStateErrorText: function () {
        return this.effectiveRelayState === 'error'
          ? this.t('settings_window.netpair_error_detail') : '';
      },

      // The network section no longer manages pairing — it points at the
      // Devices page (round 15). Relay status stays here.

      themeOptions: function () {
        return [
          { value: 'system', label: this.t('settings_window.theme_system') },
          { value: 'light', label: this.t('settings_window.theme_light') },
          { value: 'dark', label: this.t('settings_window.theme_dark') },
        ];
      },

      logLevelOptions: function () {
        return ['DEBUG', 'INFO', 'WARNING', 'ERROR'];
      },

      hotkeyFields: function () {
        var names = [
          'paste_1', 'paste_2', 'paste_3', 'paste_4',
          'paste_5', 'paste_6', 'paste_7', 'paste_8', 'paste_9',
          'paste_plain', 'toggle_monitor', 'show_window',
        ];
        var self = this;
        return names.map(function (n) {
          return { key: n, value: self.hotkeys[n] || '' };
        });
      },

      sectionTabs: function () {
        // Plain-text labels, no emoji — the settings_nav.* translations no
        // longer carry emoji prefixes, so there is no icon column here.
        var dirty = this.dirtySections;
        var counts = this.searchMatchCounts;
        var hasQuery = !!(this.searchQuery || '').trim();
        var mk = function (id, label) {
          return {
            id: id,
            label: label,
            dirty: !!dirty[id],
            count: counts[id] || 0,
            dim: hasQuery && !counts[id],
          };
        };
        return [
          mk('appearance',  this.t('settings_nav.appearance')),
          mk('network',     this.t('settings_nav.network')),
          mk('web',         this.t('settings_nav.web_companion')),
          mk('translation', this.t('settings_nav.translation')),
          mk('filter',      this.t('settings_nav.filter')),
          mk('security',    this.t('settings_nav.security')),
          mk('advanced',    this.t('settings_nav.advanced')),
          mk('logs',        this.t('settings_nav.logs')),
          mk('data',        this.t('settings.data')),
          mk('aiconfig',    this.t('settings_nav.aiconfig')),
          mk('about',       this.t('settings_nav.about')),
          mk('danger',      this.t('settings_window.danger_zone')),
        ];
      },

      // Query → section-id map of how many of the section's visible labels
      // contain the query (case-insensitive). Empty query → empty map.
      searchMatchCounts: function () {
        var q = (this.searchQuery || '').trim().toLowerCase();
        var out = {};
        if (!q) return out;
        var self = this;
        Object.keys(SETTINGS_SEARCH_KEYS).forEach(function (id) {
          var n = 0;
          SETTINGS_SEARCH_KEYS[id].forEach(function (key) {
            var v = self.t(key);
            if (typeof v === 'string' && v.toLowerCase().indexOf(q) >= 0) n++;
          });
          if (n > 0) out[id] = n;
        });
        return out;
      },

      searchNoMatches: function () {
        var q = (this.searchQuery || '').trim();
        return !!q && Object.keys(this.searchMatchCounts).length === 0;
      },
    },

    methods: {
      selectSection: function (id) {
        this.activeSection = id;
      },

      // ── Settings search (classic-desktop parity) ─────────────────

      // Enter jumps to the first section whose labels match the query.
      onSearchEnter: function () {
        var tabs = this.sectionTabs;
        for (var i = 0; i < tabs.length; i++) {
          if (tabs[i].count > 0) {
            this.selectSection(tabs[i].id);
            return;
          }
        }
      },

      clearSearch: function () {
        this.searchQuery = '';
      },

      // Mark a staged section as having unsaved edits (no-op while the local
      // fields are being (re)filled from the settings cache).
      markDirty: function (section) {
        if (this._skipDirty) return;
        this.dirtySections[section] = true;
      },

      hotkeyLabel: function (key) {
        return this.t('hotkeys.' + key);
      },

      populateFromCache: function () {
        var self = this;
        this._skipDirty = true;
        var s = this.store.settingsCache || {};
        if (s.port !== undefined) this.port = String(s.port);
        if (s.auto_start !== undefined) this.autoStart = !!s.auto_start;
        if (s.service_type !== undefined) this.serviceType = s.service_type || '';
        if (s.internet_sync_enabled !== undefined) this.internetSyncEnabled = !!s.internet_sync_enabled;
        if (s.relay_brokers !== undefined) this.relayBrokersText = (s.relay_brokers || []).join('\n');
        if (s.web_enabled !== undefined) this.webEnabled = !!s.web_enabled;
        if (s.web_port !== undefined) this.webPort = String(s.web_port);
        if (s.web_history_limit !== undefined) this.webHistoryLimit = s.web_history_limit;
        this.webToken = this.store.token || '';
        this.webLanIp = this.store.overview.localIp || '';
        if (s.filter_enabled_categories !== undefined) {
          // null/undefined = not configured → all categories enabled (default).
          // [] = explicitly disabled. Non-empty = that subset.
          var cats = s.filter_enabled_categories;
          var allOn = cats === null || cats === undefined;
          this.filterEnabled = allOn || cats.length > 0;
          this.filterCreditCard = allOn || cats.indexOf('credit_card') >= 0;
          this.filterSSN = allOn || cats.indexOf('ssn') >= 0;
          this.filterApiKey = allOn || cats.indexOf('api_key') >= 0;
          this.filterPrivateKey = allOn || cats.indexOf('private_key') >= 0;
          this.filterPassword = allOn || cats.indexOf('password') >= 0;
        }
        if (s.encryption_enabled !== undefined) this.encryptionEnabled = !!s.encryption_enabled;
        if (s.password_set !== undefined) this.passwordSet = !!s.password_set;
        if (s.history_max_entries !== undefined) this.historyMax = s.history_max_entries;
        if (s.history_max_age_days !== undefined) this.historyMaxAgeDays = s.history_max_age_days;
        if (s.sync_debounce !== undefined) this.syncDebounce = s.sync_debounce;
        if (s.clipboard_poll_interval !== undefined) this.pollInterval = s.clipboard_poll_interval;
        if (s.file_receive_dir !== undefined) this.receiveDir = s.file_receive_dir || '';
        if (s.transfer_timeout !== undefined) this.transferTimeout = s.transfer_timeout;
        if (s.max_reconnect_attempts !== undefined) this.maxReconnect = s.max_reconnect_attempts;
        if (s.log_level !== undefined) this.logLevel = s.log_level;
        if (s.notifications_enabled !== undefined) this.notificationsEnabled = !!s.notifications_enabled;
        if (s.notify_device_connect !== undefined) this.notifyDeviceConnect = !!s.notify_device_connect;
        if (s.notify_transfer !== undefined) this.notifyTransfer = !!s.notify_transfer;
        if (s.notify_pairing !== undefined) this.notifyPairing = !!s.notify_pairing;
        if (s.notify_sync !== undefined) this.notifySync = !!s.notify_sync;
        if (s.translate_url !== undefined) this.translateUrl = s.translate_url || '';
        if (s.translate_key_set !== undefined) this.translateKeySet = !!s.translate_key_set;
        if (s.app_filter_enabled !== undefined) this.appFilterEnabled = !!s.app_filter_enabled;
        if (s.app_filter_mode !== undefined) this.appFilterMode = s.app_filter_mode;
        if (s.app_filter_list !== undefined) this.appFilterList = (s.app_filter_list || []).join('\n');
        if (s.paste_to_top !== undefined) this.pasteToTop = !!s.paste_to_top;
        if (s.low_memory_mode !== undefined) this.lowMemory = !!s.low_memory_mode;
        if (s.retry_capture_enabled !== undefined) this.retryCapture = !!s.retry_capture_enabled;
        if (s.dedup_method !== undefined) this.dedupMethod = s.dedup_method || 'sha256';
        if (s.source_tracking_enabled !== undefined) this.sourceTracking = !!s.source_tracking_enabled;
        if (s.plain_text_only !== undefined) this.plainTextOnly = !!s.plain_text_only;
        if (s.auto_update_check !== undefined) this.autoUpdateCheck = !!s.auto_update_check;
        if (s.data_dir !== undefined) this.dataDir = s.data_dir || '';
        if (s.favorites_path !== undefined) this.favoritesPath = s.favorites_path || '';
        if (s.hotkeys) this.hotkeys = Object.assign({}, s.hotkeys);
        if (s.hotkeys_enabled !== undefined) this.hotkeysEnabled = !!s.hotkeys_enabled;
        // Re-enable dirty tracking on the next tick so the watchers fired by
        // the assignments above don't mark freshly-loaded values as unsaved.
        this.$nextTick(function () { self._skipDirty = false; });
      },

      // ── Save methods ─────────────────────────────────────────────

      saveNetwork: function () {
        var self = this;
        self.networkSaving = true;
        var cache = self.store.settingsCache || {};
        // The TCP sync port only applies at startup — when it was changed,
        // surface a restart-required note (like the web-port save does).
        var portChanged = String(cache.port) !== String(self.port);
        ClipsyncAPI.updateSettings({
          port: parseInt(self.port, 10) || 53317,
          service_type: (self.serviceType || '_clipsync._tcp.local.').trim(),
        }).then(function (res) {
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.dirtySections['network'] = false;
          var msg = self.t('settings_window.network_saved');
          if (portChanged) {
            msg += ' ' + self.t('settings_window.web_restart_note_short');
          }
          self.store.showToast(msg, portChanged ? 4000 : 3000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_network_failed'), 2000);
        }).finally(function () {
          self.networkSaving = false;
        });
      },

      // ── Internet (cross-network) sync ────────────────────────────

      toggleInternetSync: function () {
        var self = this;
        this.internetSyncEnabled = !this.internetSyncEnabled;
        // Saves through the normal settings path; the host live-applies the
        // change and broadcasts relay_state transitions, which ws.js folds
        // into store.relayState so the status row below updates on its own.
        ClipsyncAPI.updateSettings({ internet_sync_enabled: this.internetSyncEnabled })
          .then(function (res) {
            if (res && res.updated) self.store.mergeSettings(res.updated);
          })
          .catch(function () {
            // Revert so the UI stays truthful to the server setting.
            self.internetSyncEnabled = !self.internetSyncEnabled;
            self.store.showToast(self.t('dialog.failed'), 2000);
          });
      },

      saveRelayBrokers: function () {
        var self = this;
        var lines = (self.relayBrokersText || '').split('\n')
          .map(function (s) { return s.trim(); })
          .filter(Boolean);
        // An empty list would silently disable internet sync with no error
        // anywhere else in the UI — refuse it here instead.
        if (lines.length === 0) {
          self.store.showToast(self.t('settings.relay_brokers_empty'), 3000);
          return;
        }
        var hasBad = lines.some(function (l) {
          return l.toLowerCase().indexOf('wss://') !== 0;
        });
        if (hasBad) {
          self.store.showToast(self.t('settings.relay_brokers_invalid'), 3000);
          return;
        }
        // De-duplicate while preserving order so a pasted-overlapping list
        // doesn't open redundant broker connections.
        var seen = {};
        var brokers = [];
        lines.forEach(function (l) {
          if (!seen[l]) { seen[l] = true; brokers.push(l); }
        });
        self.brokersSaving = true;
        ClipsyncAPI.updateSettings({ relay_brokers: brokers })
          .then(function (res) {
            self.brokersSaving = false;
            if (res && res.updated) self.store.mergeSettings(res.updated);
            self.dirtySections['network'] = false;
            self.relayBrokersText = brokers.join('\n');
            self.store.showToast(self.t('settings.relay_brokers_saved'), 2500);
          })
          .catch(function () {
            self.brokersSaving = false;
            self.store.showToast(self.t('dialog.failed'), 2000);
          });
      },

      // Test the currently staged broker list (before saving) with a light
      // per-broker TCP/TLS handshake; the live relay session is untouched.
      testRelay: function () {
        var self = this;
        if (self.relayTesting) return;
        var lines = (self.relayBrokersText || '').split('\n')
          .map(function (s) { return s.trim(); })
          .filter(Boolean);
        var seen = {}, brokers = [];
        lines.forEach(function (l) {
          if (!seen[l]) { seen[l] = true; brokers.push(l); }
        });
        self.relayTesting = true;
        self.relayTestResult = null;
        ClipsyncAPI.testRelay(brokers)
          .then(function (res) {
            self.relayTesting = false;
            if (res && res.ok) {
              self.relayTestResult = res;
            } else {
              self.relayTestResult = {
                summary: (res && res.error) || 'test failed',
                results: [],
              };
            }
          })
          .catch(function (e) {
            self.relayTesting = false;
            self.relayTestResult = {
              summary: (e && e.message) || 'test failed',
              results: [],
            };
          });
      },

      // ── Internet pairing management (round 15) ───────────────────

      // Pairing lives on the Devices page now. This jumps there (and closes
      // settings) so the user can generate/enter codes and manage peers.
      goToDevicesTab: function () {
        this.store.activeTab = 'devices';
        this.store.closeSettingsPanel();
      },

      saveWeb: function () {
        var self = this;
        self.webSaving = true;
        var cache = self.store.settingsCache || {};
        // web_history_limit comes from a <input type="number"> v-model, which
        // yields a string — the backend int type-guard silently rejects
        // strings, so coerce to a number here.
        var historyLimit = parseInt(self.webHistoryLimit, 10);
        if (isNaN(historyLimit) || historyLimit < 1) historyLimit = 10;
        var portChanged = String(cache.web_port) !== String(self.webPort);
        ClipsyncAPI.updateSettings({
          web_enabled: self.webEnabled,
          web_port: parseInt(self.webPort, 10) || 9580,
          web_history_limit: historyLimit,
        }).then(function (res) {
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.dirtySections['web'] = false;
          var msg = self.t('settings_window.web_saved');
          if (portChanged) {
            msg += ' ' + self.t('settings_window.web_restart_note_short');
          }
          self.store.showToast(msg, 4000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_web_failed'), 2000);
        }).finally(function () {
          self.webSaving = false;
        });
      },

      saveFilter: function () {
        var self = this;
        self.filterSaving = true;
        var categories = [];
        if (self.filterEnabled) {
          if (self.filterCreditCard) categories.push('credit_card');
          if (self.filterSSN) categories.push('ssn');
          if (self.filterApiKey) categories.push('api_key');
          if (self.filterPrivateKey) categories.push('private_key');
          if (self.filterPassword) categories.push('password');
        }
        ClipsyncAPI.updateSettings({
          filter_enabled_categories: categories,
          app_filter_enabled: self.appFilterEnabled,
          app_filter_mode: self.appFilterMode,
          app_filter_list: self.appFilterList
            ? self.appFilterList.split('\n').map(function (s) { return s.trim(); }).filter(Boolean)
            : [],
        }).then(function (res) {
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.dirtySections['filter'] = false;
          self.store.showToast(self.t('settings_window.filter_saved'), 2000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_filter_failed'), 2000);
        }).finally(function () {
          self.filterSaving = false;
        });
      },

      saveSecurity: function () {
        var self = this;
        self.securitySaving = true;
        var payload = {
          encryption_enabled: self.encryptionEnabled,
          notify_device_connect: self.notifyDeviceConnect,
          notify_transfer: self.notifyTransfer,
          notify_pairing: self.notifyPairing,
          notify_sync: self.notifySync,
        };
        if (self.passwordValue) {
          payload.password = self.passwordValue;
        }
        ClipsyncAPI.updateSettings(payload).then(function (res) {
          // Clear the password field without re-marking the section dirty (the
          // passwordValue watcher would otherwise flag it again).
          self._skipDirty = true;
          self.passwordValue = '';
          if (res && typeof res.password_set === 'boolean') {
            self.passwordSet = res.password_set;
          }
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.dirtySections['security'] = false;
          self.$nextTick(function () { self._skipDirty = false; });
          self.store.showToast(self.t('settings_window.security_saved'), 3000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_security_failed'), 2000);
        }).finally(function () {
          self.securitySaving = false;
        });
      },

      clearPassword: function () {
        var self = this;
        ClipsyncAPI.updateSettings({ password: '', clear_password: true }).then(function () {
          self.passwordSet = false;
          self.passwordValue = '';
          self.dirtySections['security'] = false;
          self.store.showToast(self.t('settings_window.password_cleared'), 2000);
        }).catch(function () {
          self.store.showToast(self.t('settings.clear_password_failed'), 2000);
        });
      },

      // ── Logs ────────────────────────────────────────────────────

      loadLogs: function () {
        var self = this;
        self.logsLoading = true;
        ClipsyncAPI._fetch('GET', '/api/logs?lines=200').then(function (res) {
          self.logsLoading = false;
          self.logs = (res && Array.isArray(res.logs)) ? res.logs.join('\n') : '';
        }).catch(function () {
          self.logsLoading = false;
          self.store.showToast(self.t('settings_window.logs_load_failed'), 2000);
        });
      },

      exportLogs: function () {
        var self = this;
        if (!self.logs) {
          self.store.showToast(self.t('settings_window.no_logs'), 2000);
          return;
        }
        try {
          var blob = new Blob([self.logs], { type: 'text/plain;charset=utf-8' });
          var url = URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = url;
          a.download = 'clipsync_logs.txt';
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
          self.store.showToast(self.t('settings_window.logs_exported'), 2000);
        } catch (e) {
          self.store.showToast(self.t('settings_window.logs_export_failed'), 2000);
        }
      },

      // ── Trusted devices / certificates ──────────────────────────

      loadCerts: function () {
        var self = this;
        self.certsLoading = true;
        ClipsyncAPI._fetch('GET', '/api/devices/certs').then(function (res) {
          self.certsLoading = false;
          self.certDevices = (res && Array.isArray(res.devices)) ? res.devices : [];
        }).catch(function () {
          self.certsLoading = false;
          self.store.showToast(self.t('settings_window.certs_load_failed'), 2000);
        });
      },

      shortId: function (id) {
        return (id && id.length > 8) ? id.slice(0, 8) : (id || '');
      },

      // ── Update check / download / install ───────────────────────

      toggleAutoUpdateCheck: function () {
        var self = this;
        this.autoUpdateCheck = !this.autoUpdateCheck;
        ClipsyncAPI.updateSettings({ auto_update_check: this.autoUpdateCheck })
          .then(function (res) {
            if (res && res.updated) self.store.mergeSettings(res.updated);
          })
          .catch(function () {
            // Revert so the UI stays truthful to the server setting.
            self.autoUpdateCheck = !self.autoUpdateCheck;
            self.store.showToast(self.t('dialog.failed'), 2000);
          });
      },

      checkForUpdate: function () {
        var self = this;
        self.updateChecking = true;
        // The server-side check can retry against GitHub for a while; give it
        // a generous window instead of the short default timeout.
        ClipsyncAPI._fetch('GET', '/api/update/check', null, 30000).then(function (res) {
          self.updateChecking = false;
          if (res && (res.available || (res.latest && res.latest.length))) {
            self.updateAvailable = !!res.available;
            self.updateLatest = res.latest || '';
            if (!res.available) {
              self.store.showToast(self.t('settings_window.up_to_date'), 2500);
            }
          } else {
            self.updateAvailable = null;
            self.updateLatest = '';
            self.store.showToast(self.t('settings_window.update_check_failed'), 2500);
          }
        }).catch(function () {
          self.updateChecking = false;
          self.updateAvailable = null;
          self.store.showToast(self.t('settings_window.update_check_failed'), 2500);
        });
      },

      installUpdate: function () {
        var self = this;
        self.updateDownloading = true;
        // POST /api/update/install runs the same chain as the tray:
        // download → verify → stage → apply → restart. The app exits and the
        // update helper replaces the binary, so this page dies mid-request —
        // that is expected, not an error to report.
        ClipsyncAPI._fetch('POST', '/api/update/install', {}, 30000).then(function (res) {
          if (res && res.ok) {
            self.store.showToast(self.t('settings_window.update_installing'), 5000);
          } else {
            self.updateDownloading = false;
            self.store.showToast(self.t('settings_window.update_failed') +
              ((res && res.error) ? ': ' + res.error : ''), 2500);
          }
        }).catch(function () {
          // A successful install restarts the host, which aborts this request
          // — only surface a failure when the dashboard is still alive.
          setTimeout(function () {
            self.updateDownloading = false;
          }, 4000);
        });
      },

      downloadUpdate: function () {
        var self = this;
        self.updateDownloading = true;
        // The download can take well over the 15s default request timeout, so
        // give it a long explicit window. Expected failures now come back as
        // HTTP 200 {ok:false, error:<reason>} — surfaced via res.error below.
        ClipsyncAPI._fetch('POST', '/api/update/download', {}, 120000).then(function (res) {
          self.updateDownloading = false;
          if (res && res.ok) {
            self.store.showToast(self.t('settings_window.update_downloaded', { path: res.path || '' }), 4000);
          } else {
            self.store.showToast(self.t('settings_window.update_failed') + ((res && res.error) ? ': ' + res.error : ''), 2500);
          }
        }).catch(function () {
          self.updateDownloading = false;
          self.store.showToast(self.t('settings_window.update_failed'), 2000);
        });
      },

      saveTranslation: function () {
        var self = this;
        self.translateSaving = true;
        var payload = { translate_url: (self.translateUrl || '').trim() };
        // Only set the key when the user typed one — never echo it back.
        if (self.translateKeyValue) {
          payload.set_translate_key = self.translateKeyValue;
        }
        ClipsyncAPI.updateSettings(payload).then(function (res) {
          if (res && typeof res.translate_key_set === 'boolean') {
            self.translateKeySet = res.translate_key_set;
          }
          // Clear the key field without re-marking the section dirty (the
          // translateKeyValue watcher would otherwise flag it again).
          self._skipDirty = true;
          self.translateKeyValue = '';
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.dirtySections['translation'] = false;
          self.$nextTick(function () { self._skipDirty = false; });
          self.store.showToast(self.t('settings_window.translation_saved'), 3000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_translation_failed'), 2000);
        }).finally(function () {
          self.translateSaving = false;
        });
      },

      clearTranslateKey: function () {
        var self = this;
        ClipsyncAPI.updateSettings({ clear_translate_key: true }).then(function (res) {
          self.translateKeySet = false;
          self.translateKeyValue = '';
          self.dirtySections['translation'] = false;
          self.store.showToast(self.t('settings_window.translate_key_cleared'), 2000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_translation_failed'), 2000);
        });
      },

      saveAdvanced: function () {
        var self = this;
        self.advancedSaving = true;
        // Age-based retention in days; 0 (or garbage) disables the pruning —
        // matching the server-side range guard and the config default.
        var maxAgeDays = parseFloat(self.historyMaxAgeDays);
        if (isNaN(maxAgeDays) || maxAgeDays < 0) maxAgeDays = 0;
        ClipsyncAPI.updateSettings({
          history_max_entries: parseInt(self.historyMax, 10) || 200,
          history_max_age_days: maxAgeDays,
          sync_debounce: parseFloat(self.syncDebounce) || 0.5,
          clipboard_poll_interval: parseFloat(self.pollInterval) || 0.5,
          file_receive_dir: self.receiveDir,
          transfer_timeout: parseInt(self.transferTimeout, 10) || 300,
          max_reconnect_attempts: parseInt(self.maxReconnect, 10) || 10,
          log_level: self.logLevel,
          notifications_enabled: self.notificationsEnabled,
          paste_to_top: self.pasteToTop,
          low_memory_mode: self.lowMemory,
          retry_capture_enabled: self.retryCapture,
          dedup_method: self.dedupMethod,
          source_tracking_enabled: self.sourceTracking,
          plain_text_only: !!self.plainTextOnly,
          hotkeys: self.hotkeys,
          hotkeys_enabled: self.hotkeysEnabled,
        }).then(function (res) {
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.dirtySections['advanced'] = false;
          self.store.showToast(self.t('settings_window.advanced_saved'), 3000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_advanced_failed'), 2000);
        }).finally(function () {
          self.advancedSaving = false;
        });
      },

      saveDataPaths: function () {
        var self = this;
        self.dataSaving = true;
        var cache = self.store.settingsCache || {};
        var dirChanged = (cache.data_dir || '') !== (self.dataDir || '').trim();
        var favChanged = (cache.favorites_path || '') !== (self.favoritesPath || '').trim();
        ClipsyncAPI.updateSettings({
          data_dir: (self.dataDir || '').trim(),
          favorites_path: (self.favoritesPath || '').trim(),
        }).then(function (res) {
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.dirtySections['data'] = false;
          var msg = self.t('settings.data_saved');
          if (dirChanged || favChanged) {
            msg += ' ' + self.t('settings_window.data_restart_note');
          }
          self.store.showToast(msg, 4000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_data_failed'), 2000);
        }).finally(function () {
          self.dataSaving = false;
        });
      },

      // ── AI-config sync (round 12) ────────────────────────────────

      // The watch list lives behind its own endpoints
      // (GET/POST /api/aiconfig/paths), NOT the generic settings API — load
      // it when the panel or section opens, mirroring logs/certs.
      loadAiConfigPaths: function () {
        var self = this;
        if (!window.ClipsyncAPI || !window.ClipsyncAPI.getAiConfigPaths) return;
        self._aiCfgLoading = true;
        ClipsyncAPI.getAiConfigPaths().then(function (res) {
          self._aiCfgLoading = false;
          var list = (res && Array.isArray(res.paths)) ? res.paths : [];
          self._skipDirty = true;
          self.aiConfigPaths = list.map(function (p) { return String(p == null ? '' : p); });
          self.$nextTick(function () { self._skipDirty = false; });
        }).catch(function () {
          // 404 on an older host — leave whatever rows exist for editing.
          self._aiCfgLoading = false;
        });
      },

      addAiConfigPath: function () {
        this.aiConfigPaths.push('');
      },

      removeAiConfigPath: function (idx) {
        this.aiConfigPaths.splice(idx, 1);
      },

      // Save the monitored root-folder list. Mirrors the server-side
      // normalization exactly: trim whitespace, drop blanks, exact-string
      // dedupe, cap at 50 roots. An empty list is allowed — it simply means
      // this device shares nothing. On success the backend recollects and
      // re-broadcasts the inventory to paired peers on its own.
      saveAiConfigPaths: function () {
        var self = this;
        var seen = {};
        var paths = [];
        (this.aiConfigPaths || []).forEach(function (p) {
          var v = String(p == null ? '' : p).trim();
          if (!v || seen[v]) return;
          seen[v] = true;
          if (paths.length < 50) paths.push(v);
        });
        self.aiConfigSaving = true;
        ClipsyncAPI.setAiConfigPaths(paths).then(function (res) {
          self.aiConfigSaving = false;
          if (res && res.ok === false) {
            self.store.showToast(self.t('settings.save_aiconfig_failed'), 2000);
            return;
          }
          self._skipDirty = true;
          self.aiConfigPaths = (res && Array.isArray(res.paths))
            ? res.paths.slice() : paths;
          self.$nextTick(function () { self._skipDirty = false; });
          self.dirtySections['aiconfig'] = false;
          self.store.showToast(self.t('settings.aiconfig_saved'), 3000);
        }).catch(function () {
          self.aiConfigSaving = false;
          self.store.showToast(self.t('settings.save_aiconfig_failed'), 2000);
        });
      },

      // ── Danger zone ──────────────────────────────────────────────

      factoryReset: function () {
        var msg = this.t('settings_window.factory_reset_confirm');
        var self = this;
        this.store.confirm(self.t('settings_window.factory_reset'), msg)
          .then(function () {
            self.resetting = true;
            ClipsyncAPI.updateSettings({ factory_reset: true }).then(function (res) {
              self.resetting = false;
              if (res && res.ok) {
                self.store.showToast(self.t('settings.factory_reset_complete'), 3000);
                setTimeout(function () { ClipsyncAPI.windowAction('close').catch(function () {}); }, 1500);
              } else {
                self.store.showToast(self.t('settings.factory_reset_failed'), 2000);
              }
            }).catch(function () {
              self.resetting = false;
              self.store.showToast(self.t('settings.factory_reset_failed'), 2000);
            });
          })
          .catch(function () {});
      },

      restartApp: function () {
        var msg = this.t('settings_window.restart_confirm');
        var self = this;
        this.store.confirm(self.t('settings_window.restart_app'), msg)
          .then(function () {
            self.restarting = true;
            // The backend restarts the whole app process — windowAction('close')
            // would only stop the browser window and leave the app running.
            ClipsyncAPI.restartApp()
              .then(function (res) {
                if (!res || res.ok !== true) {
                  self.restarting = false;
                  self.store.showToast(self.t('dialog.failed'), 2000);
                }
              })
              .catch(function () {
                self.restarting = false;
                self.store.showToast(self.t('dialog.failed'), 2000);
              });
          })
          .catch(function () {});
      },

      // ── Token management ─────────────────────────────────────────

      // Rewrite the current URL's token query param (to a new token, or an
      // empty string after clearing) so a reload is re-served by the server
      // instead of 403'ing on the now-stale token in the address bar.
      _rewriteUrlToken: function (token) {
        try {
          var u = new URL(window.location.href);
          u.searchParams.set('token', token || '');
          window.history.replaceState({}, '', u.toString());
        } catch (e) { /* non-http(s) URL — leave it */ }
      },

      regenerateToken: function () {
        var self = this;
        this.store.confirm(this.t('settings.token_regenerate_title'), this.t('settings.token_regenerate_confirm'))
          .then(function () {
            ClipsyncAPI.updateSettings({ regenerate_web_token: true }).then(function (res) {
              if (res && res.ok) {
                if (res.web_token) {
                  self._rewriteUrlToken(res.web_token);
                }
                window.location.reload();
              } else {
                self.store.showToast(self.t('settings.token_regenerate_failed'), 2000);
              }
            }).catch(function () {
              self.store.showToast(self.t('settings.token_regenerate_failed'), 2000);
            });
          })
          .catch(function () { /* cancelled */ });
      },

      clearToken: function () {
        var self = this;
        // Clearing the token disables web access; the old URL token is now
        // invalid, so rewrite the URL with an empty token before reloading
        // (an empty web_token accepts an empty ?token=; the stale one would 403).
        this.store.confirm(this.t('settings.token_clear_title'), this.t('settings.token_clear_confirm'))
          .then(function () {
            ClipsyncAPI.updateSettings({ clear_web_token: true }).then(function (res) {
              if (res && res.ok) {
                self._rewriteUrlToken('');
              }
              window.location.reload();
            }).catch(function () {
              self.store.showToast(self.t('settings.token_clear_failed'), 2000);
            });
          })
          .catch(function () { /* cancelled */ });
      },

      copyUrl: function () {
        // webLocalUrl already includes ?token= so a phone opening the copied
        // URL is authenticated instead of hitting "invalid token".
        var url = this.webLocalUrl;
        if (!url) return;
        var self = this;
        var done = function () {
          self.store.showToast(self.t('settings.url_copied', { url: url }), 2000);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(url).then(done).catch(done);
        } else {
          var textarea = document.createElement('textarea');
          textarea.value = url;
          textarea.style.position = 'fixed';
          textarea.style.opacity = '0';
          document.body.appendChild(textarea);
          textarea.select();
          try { document.execCommand('copy'); } catch (e) { /* ignore */ }
          document.body.removeChild(textarea);
          done();
        }
      },

      close: function () {
        var self = this;
        // Closing with unsaved staged edits asks for confirmation first. The
        // client-dialog owns focus/keys while it is up, and the visible watcher
        // restores staged fields when the panel actually closes.
        if (this.hasDirtySections) {
          this._closingPromptOpen = true;
          this.store.confirm(
            this.t('common.unsaved_changes'),
            this.t('settings_window.unsaved_changes_confirm')
          ).then(function () {
            self._closingPromptOpen = false;
            self.dirtySections = {};
            self.store.closeSettingsPanel();
          }).catch(function () {
            self._closingPromptOpen = false;
            /* stay open */
          });
          return;
        }
        this.store.closeSettingsPanel();
      },

      selectTheme: function (theme) {
        var self = this;
        this.store.setTheme(theme);
        // Persist to the backend so the choice survives across sessions and
        // applies to the classic (native) UI too — not just localStorage.
        ClipsyncAPI.updateSettings({ appearance_mode: theme })
          .then(function (res) {
            if (res && res.updated) self.store.mergeSettings(res.updated);
          })
          .catch(function () {
            // The local theme still applies; just warn that it won't persist.
            self.store.showToast(self.t('dialog.failed'), 2000);
          });
      },

      selectLocale: function (locale) {
        var self = this;
        try { localStorage.setItem('clipsync_locale', locale); } catch (e) { /* ignore */ }
        // Persist to the server so the chosen language survives a reload (the
        // served page reads cfg.language), then reload automatically — an
        // app-mode window has no address bar to reload manually.
        if (window.ClipsyncAPI && window.ClipsyncAPI.updateSettings) {
          ClipsyncAPI.updateSettings({ language: locale })
            .then(function () {
              setTimeout(function () { window.location.reload(); }, 600);
            })
            .catch(function () {
              self.store.showToast(self.t('dialog.failed'), 2000);
            });
        } else {
          this.store.showToast(this.t('settings.language_changed'), 2500);
        }
      },

      toggleSound: function () {
        var self = this;
        this.store.soundEnabled = !this.store.soundEnabled;
        if (typeof ClipsyncSound !== 'undefined' && ClipsyncSound.setEnabled) {
          ClipsyncSound.setEnabled(this.store.soundEnabled);
        }
        ClipsyncAPI.updateSettings({ sound_enabled: this.store.soundEnabled })
          .then(function (res) {
            if (res && res.updated) self.store.mergeSettings(res.updated);
          })
          .catch(function () {
            // Revert so the UI stays truthful to the server setting.
            self.store.soundEnabled = !self.store.soundEnabled;
            if (typeof ClipsyncSound !== 'undefined' && ClipsyncSound.setEnabled) {
              ClipsyncSound.setEnabled(self.store.soundEnabled);
            }
            self.store.showToast(self.t('dialog.failed'), 2000);
          });
      },

      toggleAutoStart: function () {
        var self = this;
        this.autoStart = !this.autoStart;
        ClipsyncAPI.updateSettings({ auto_start: this.autoStart })
          .then(function (res) {
            if (res && res.updated) self.store.mergeSettings(res.updated);
          })
          .catch(function () {
            self.autoStart = !self.autoStart;
            self.store.showToast(self.t('dialog.failed'), 2000);
          });
      },

      toggleAnimation: function () {
        var self = this;
        this.store.animationsEnabled = !this.store.animationsEnabled;
        ClipsyncAPI.updateSettings({ ui_animation_enabled: this.store.animationsEnabled })
          .then(function (res) {
            if (res && res.updated) self.store.mergeSettings(res.updated);
          })
          .catch(function () {
            // Revert on failure so the UI stays truthful to the server setting.
            self.store.animationsEnabled = !self.store.animationsEnabled;
            self.store.showToast(self.t('dialog.failed'), 2000);
          });
      },

      toggleUIMode: function () {
        var newMode = this.store.uiBackend === 'webview' ? 'ctk' : 'webview';
        this.store.setUIBackend(newMode);
        this.store.showToast(this.t('settings_window.ui_backend_restart'), 4000);
      },

      // ── Data management ──────────────────────────────────────────

      exportData: function (format) {
        var self = this;
        self.exporting = true;
        ClipsyncAPI.exportData(format).then(function (res) {
          self.exporting = false;
          if (res && res.ok) {
            self.store.showToast(self.t('settings.exported', {
              count: res.count,
              format: format.toUpperCase(),
              path: res.filepath || '',
            }), 3000);
          } else {
            self.store.showToast(self.t('settings.export_failed') + ((res && res.error) ? ': ' + res.error : ''), 2500);
          }
        }).catch(function () {
          self.exporting = false;
          self.store.showToast(self.t('settings.export_failed'), 2000);
        });
      },

      importData: function () {
        var self = this;
        this.store.prompt(this.t('settings.import_title'), this.t('settings.import_prompt'))
          .then(function (filepath) {
            if (!filepath || !filepath.trim()) return;
            self.importing = true;
            ClipsyncAPI.importData(filepath.trim()).then(function (res) {
          self.importing = false;
          if (res && res.ok) {
            self.store.showToast(self.t('settings.imported', { count: res.imported }), 2000);
          } else {
            self.store.showToast(self.t('settings.import_failed') + ((res && res.error) ? ': ' + res.error : ''), 2500);
          }
        }).catch(function (e) {
          self.importing = false;
          // Errors now come back as 4xx, so they land in .catch; surface the
          // backend's detail (e.message is set from the error body by api.js).
          var detail = (e && e.message) ? ': ' + e.message : '';
          self.store.showToast(self.t('settings.import_failed') + detail, 2500);
        });
          }).catch(function () { /* cancelled */ });
      },

      createBackup: function () {
        var self = this;
        self.backingUp = true;
        ClipsyncAPI.createBackup().then(function (res) {
          self.backingUp = false;
          if (res && res.ok) {
            self.store.showToast(self.t('settings.backup_created', { path: res.backup_path || '' }), 3500);
            self.loadBackups();
          } else {
            self.store.showToast(self.t('settings.backup_failed'), 2000);
          }
        }).catch(function () {
          self.backingUp = false;
          self.store.showToast(self.t('settings.backup_failed'), 2000);
        });
      },

      restoreBackup: function (path) {
        var self = this;
        this.store.confirm(self.t('settings.restore_backup'), self.t('settings.restore_confirm'))
          .then(function () {
            self.restoring = true;
        ClipsyncAPI.restoreBackup(path).then(function (res) {
          self.restoring = false;
          if (res && res.ok) {
            var s = res.summary || {};
            var summaryMsg = self.t('settings.restore_summary', {
              config: s.config ? self.t('ui.yes') : self.t('ui.no'),
              history: s.history || 0,
              favorites: s.favorites || 0,
            });
            if (res.needs_restart) {
              summaryMsg += ' ' + self.t('settings.restore_restart_note');
            }
            self.store.showToast(summaryMsg, 4500);
            // Reload history/favorites/devices so the restored data actually
            // shows up in the panels without a manual page refresh.
            try {
              if (self.$root && typeof self.$root.loadData === 'function') {
                self.$root.loadData().catch(function () {});
              }
            } catch (e) { /* root may be gone */ }
          } else {
            self.store.showToast(self.t('settings.restore_failed'), 2000);
          }
        }).catch(function () {
          self.restoring = false;
          self.store.showToast(self.t('settings.restore_failed'), 2000);
        });
          }).catch(function () { /* cancelled */ });
      },

      loadBackups: function () {
        var self = this;
        self.backupLoading = true;
        ClipsyncAPI.listBackups().then(function (res) {
          self.backupLoading = false;
          if (res && res.ok) {
            self.backupList = res.backups || [];
          }
        }).catch(function () {
          self.backupLoading = false;
        });
      },

      openDataFolder: function (which) {
        var self = this;
        ClipsyncAPI.openDataFolder(which || 'data').then(function (res) {
          if (res && res.ok) {
            self.store.showToast(self.t('settings.data_folder_opened'), 2000);
          } else {
            self.store.showToast(self.t('settings.open_folder_failed'), 2000);
          }
        }).catch(function () {
          self.store.showToast(self.t('settings.open_folder_failed'), 2000);
        });
      },

      onOverlayClick: function (e) {
        if (e.target === this.$refs.overlay) {
          this.close();
        }
      },

      _getFocusable: function () {
        var overlay = this.$refs.overlay;
        if (!overlay) return [];
        var nodes = overlay.querySelectorAll(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
          'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        var result = [];
        for (var i = 0; i < nodes.length; i++) {
          // Skip anything hidden via v-show/v-if remnants or display:none.
          if (nodes[i].offsetParent === null && nodes[i].getClientRects().length === 0) continue;
          result.push(nodes[i]);
        }
        return result;
      },

      _trapTab: function (e) {
        // A modal on top (client dialog / server dialog) owns the Tab key.
        if (this.store.clientDialog || this.store.activeDialog) return;
        var focusable = this._getFocusable();
        if (focusable.length === 0) return;
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        var active = document.activeElement;
        if (e.shiftKey) {
          if (active === first || !this.$refs.overlay.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else if (active === last) {
          e.preventDefault();
          first.focus();
        }
      },

      onKeyDown: function (e) {
        // A modal on top owns the keys — never close/trap over it. The
        // _closingPromptOpen guard also blocks the trailing half of an Escape
        // that the client-dialog already handled (it cancels the confirm and
        // clears store.clientDialog synchronously, so checking that alone would
        // re-open the confirm in the same keydown).
        if (this.store.clientDialog || this.store.activeDialog || this._closingPromptOpen) return;
        if (e.key === 'Escape') {
          this.close();
        } else if (e.key === 'Tab') {
          this._trapTab(e);
        }
      },
    },

    watch: {
      visible: function (val) {
        if (val) {
          this.populateFromCache();
          this.loadBackups();
          // A requester (e.g. the overview network-health chip) can ask the
          // panel to open on a specific section.
          if (this.store.settingsRequestedSection) {
            this.activeSection = this.store.settingsRequestedSection;
            this.store.settingsRequestedSection = '';
          }
          // If the panel reopens on the same tab, activeSection won't change,
          // so reload the section data here too.
          if (this.activeSection === 'logs') this.loadLogs();
          if (this.activeSection === 'security') this.loadCerts();
          if (this.activeSection === 'aiconfig') this.loadAiConfigPaths();
          var self = this;
          if (!this._prevFocus) {
            this._prevFocus = document.activeElement;
          }
          this.$nextTick(function () {
            document.addEventListener('keydown', self._onKeyDown);
            // Move focus into the dialog for keyboard users.
            var focusable = self._getFocusable();
            if (focusable.length > 0) {
              focusable[0].focus();
            }
          });
        } else {
          document.removeEventListener('keydown', this._onKeyDown);
          // Restore staged fields on close and clear the dirty indicator so a
          // discard always lands back on the saved server state.
          this.populateFromCache();
          this.dirtySections = {};
          // Return focus to the element that opened the panel.
          var prev = this._prevFocus;
          this._prevFocus = null;
          if (prev && prev.focus && document.contains(prev)) {
            prev.focus();
          }
        }
      },

      activeSection: function (val) {
        if (val === 'logs') this.loadLogs();
        if (val === 'security') this.loadCerts();
        if (val === 'aiconfig') this.loadAiConfigPaths();
      },

      // ── Staged-section dirty tracking ────────────────────────────
      port: function () { this.markDirty('network'); },
      serviceType: function () { this.markDirty('network'); },
      relayBrokersText: function () { this.markDirty('network'); },
      webEnabled: function () { this.markDirty('web'); },
      webPort: function () { this.markDirty('web'); },
      webHistoryLimit: function () { this.markDirty('web'); },
      translateUrl: function () { this.markDirty('translation'); },
      translateKeyValue: function () { this.markDirty('translation'); },
      filterEnabled: function () { this.markDirty('filter'); },
      filterCreditCard: function () { this.markDirty('filter'); },
      filterSSN: function () { this.markDirty('filter'); },
      filterApiKey: function () { this.markDirty('filter'); },
      filterPrivateKey: function () { this.markDirty('filter'); },
      filterPassword: function () { this.markDirty('filter'); },
      appFilterEnabled: function () { this.markDirty('filter'); },
      appFilterMode: function () { this.markDirty('filter'); },
      appFilterList: function () { this.markDirty('filter'); },
      encryptionEnabled: function () { this.markDirty('security'); },
      passwordValue: function () { this.markDirty('security'); },
      notifyDeviceConnect: function () { this.markDirty('security'); },
      notifyTransfer: function () { this.markDirty('security'); },
      notifyPairing: function () { this.markDirty('security'); },
      notifySync: function () { this.markDirty('security'); },
      historyMax: function () { this.markDirty('advanced'); },
      historyMaxAgeDays: function () { this.markDirty('advanced'); },
      syncDebounce: function () { this.markDirty('advanced'); },
      pollInterval: function () { this.markDirty('advanced'); },
      receiveDir: function () { this.markDirty('advanced'); },
      transferTimeout: function () { this.markDirty('advanced'); },
      maxReconnect: function () { this.markDirty('advanced'); },
      logLevel: function () { this.markDirty('advanced'); },
      notificationsEnabled: function () { this.markDirty('advanced'); },
      pasteToTop: function () { this.markDirty('advanced'); },
      lowMemory: function () { this.markDirty('advanced'); },
      retryCapture: function () { this.markDirty('advanced'); },
      sourceTracking: function () { this.markDirty('advanced'); },
      plainTextOnly: function () { this.markDirty('advanced'); },
      dedupMethod: function () { this.markDirty('advanced'); },
      hotkeysEnabled: function () { this.markDirty('advanced'); },
      hotkeys: {
        deep: true,
        handler: function () { this.markDirty('advanced'); },
      },
      dataDir: function () { this.markDirty('data'); },
      favoritesPath: function () { this.markDirty('data'); },
      aiConfigPaths: {
        deep: true,
        handler: function () { this.markDirty('aiconfig'); },
      },
    },

    created: function () {
      this._onKeyDown = this.onKeyDown.bind(this);
    },

    template:
      '<transition name="dialog-fade">' +
        '<div v-if="visible" class="settings-dialog-overlay" ref="overlay" role="dialog" aria-modal="true" :aria-label="t(\'settings.title\')" @click="onOverlayClick">' +
          '<div class="settings-dialog glass-neo">' +

            '<!-- Header -->' +
            '<div class="settings-dialog__header">' +
              '<h2 class="settings-dialog__title">{{ t(\'settings.title\') }}</h2>' +
              '<input ref="searchInput" type="search" class="settings-dialog__search"' +
                ' v-model="searchQuery"' +
                ' :placeholder="t(\'settings.search_placeholder\')"' +
                ' :aria-label="t(\'settings.search_placeholder\')"' +
                ' @keydown.enter.prevent="onSearchEnter"' +
                ' @keydown.escape.stop.prevent="clearSearch">' +
              '<button class="settings-dialog__close" @click="close" :title="t(\'ui.close\')" :aria-label="t(\'ui.close\')">' +
                '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
                  '<line x1="18" y1="6" x2="6" y2="18"></line>' +
                  '<line x1="6" y1="6" x2="18" y2="18"></line>' +
                '</svg>' +
              '</button>' +
            '</div>' +

            '<!-- Body: left tabs + right content -->' +
            '<div class="settings-dialog__body">' +

              '<!-- Left tab nav -->' +
              '<div class="settings-dialog__tabs">' +
                '<button v-for="tab in sectionTabs" :key="tab.id"' +
                  ' class="settings-dialog__tab"' +
                  ' :class="{ \'settings-dialog__tab--active\': activeSection === tab.id, \'settings-dialog__tab--dim\': tab.dim }"' +
                  ' @click="selectSection(tab.id)"' +
                '>' +
                  '<span class="settings-dialog__tab-label">{{ tab.label }}</span>' +
                  '<span v-if="tab.dirty" class="settings-dialog__tab-dirty" :title="t(\'common.unsaved_changes\')">●</span>' +
                  '<span v-if="tab.count" class="settings-dialog__tab-count">{{ tab.count }}</span>' +
                '</button>' +
              '</div>' +

              '<!-- Right content area -->' +
              '<div class="settings-dialog__content">' +
                '<p v-if="searchNoMatches" class="settings-hint" style="padding:8px 0">{{ t(\'settings.search_no_matches\') }}</p>' +

                '<!-- ═══════ Appearance ═══════ -->' +
                '<section v-if="activeSection === \'appearance\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings.appearance\') }}</h3>' +
                  '<div class="settings-theme-row">' +
                    '<button v-for="opt in themeOptions" :key="opt.value"' +
                      ' class="settings-theme-btn"' +
                      ' :class="{ \'settings-theme-btn--active\': store.theme === opt.value }"' +
                      ' @click="selectTheme(opt.value)">' +
                      '<span class="settings-theme-btn__label">{{ opt.label }}</span>' +
                      '<span v-if="store.theme === opt.value" class="settings-theme-btn__check">✓</span>' +
                    '</button>' +
                  '</div>' +

                  '<h3 class="settings-section__title" style="margin-top:24px">{{ t(\'settings.language\') }}</h3>' +
                  '<select class="settings-select"' +
                    ' :value="currentLocale"' +
                    ' @change="selectLocale($event.target.value)">' +
                    '<option v-for="loc in locales" :key="loc.code" :value="loc.code">{{ loc.label }}</option>' +
                  '</select>' +
                  '<p class="settings-hint">{{ t(\"settings.language_hint\") }}</p>' +

                  '<h3 class="settings-section__title" style="margin-top:24px">{{ t(\'settings.preferences\') }}</h3>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'network.auto_start\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="autoStart" :aria-label="t(\'network.auto_start\')" :class="{ \'settings-toggle--on\': autoStart }" @click="toggleAutoStart">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<p class="settings-hint">{{ t(\'settings_window.auto_start_hint\') }}</p>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings.animation\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="store.animationsEnabled" :aria-label="t(\'settings.animation\')" :class="{ \'settings-toggle--on\': store.animationsEnabled }" @click="toggleAnimation">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings.ui_mode\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="uiBackend === \'webview\'" :aria-label="t(\'settings.ui_mode\')" :class="{ \'settings-toggle--on\': uiBackend === \'webview\' }" @click="toggleUIMode">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<p class="settings-hint">{{ t(\'settings_window.ui_backend_hint\') }}</p>' +
                '</section>' +

                '<!-- ═══════ Network ═══════ -->' +
                '<section v-if="activeSection === \'network\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'network.connection\') }}</h3>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'network.tcp_port\') }}</label>' +
                    '<input type="number" class="settings-input" v-model="port" min="1024" max="65535" placeholder="53317">' +
                    '<span class="settings-hint">{{ t(\'settings_window.port_hint\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'network.service_type\') }}</label>' +
                    '<input type="text" class="settings-input" v-model="serviceType" placeholder="_clipsync._tcp.local.">' +
                    '<span class="settings-hint">{{ t(\'settings_window.service_type_hint\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<span class="settings-field__label">{{ t(\'network.local_address\') }}</span>' +
                    '<span class="settings-field__value settings-field__value--mono">{{ store.overview.localIp }}:{{ port || (store.settingsCache && store.settingsCache.port) || 53317 }}</span>' +
                  '</div>' +
                  '<button class="settings-btn settings-btn--accent" @click="saveNetwork" :disabled="networkSaving" style="width:100%;margin-top:8px">' +
                    '{{ networkSaving ? \'...\' : t(\'settings_window.save_network\') }}' +
                  '</button>' +

                  '<!-- Internet (cross-network) sync -->' +
                  '<h3 class="settings-section__title settings-section__title--sub" style="margin-top:28px">{{ t(\'settings_window.internet_sync_title\') }}</h3>' +

                  // Round 15: pairing management moved to the Devices page.
                  // This section keeps only the toggle, the relay status row,
                  // and a pointer to the Devices tab.
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'network.internet_sync\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="internetSyncEnabled" :aria-label="t(\'network.internet_sync\')" :class="{ \'settings-toggle--on\': internetSyncEnabled }" @click="toggleInternetSync">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<p class="settings-hint" style="margin-bottom:4px">{{ t(\'settings_window.internet_sync_hint\') }}</p>' +

                  // Status line: colored dot + state label, and an actionable
                  // reason when the relay is in the error state (never a bare
                  // red dot with no explanation).
                  '<div class="settings-field" style="margin-top:12px">' +
                    '<span class="settings-field__label">{{ t(\'settings_window.internet_sync_state_label\') }}</span>' +
                    '<div class="settings-field__row">' +
                      '<span class="settings-field__value">' +
                        '<span :style="{ display:\'inline-block\', width:\'10px\', height:\'10px\', borderRadius:\'50%\', marginRight:\'6px\', verticalAlign:\'middle\', background: relayStateColor }"></span>{{ t(relayStateKey) }}' +
                      '</span>' +
                    '</div>' +
                    '<p v-if="relayStateErrorText" class="settings-hint" style="color:var(--clipsync-danger);margin-top:4px">{{ relayStateErrorText }}</p>' +
                    '<div class="settings-field__row" style="margin-top:8px">' +
                      '<span class="settings-hint" style="margin:0">{{ t(\'settings_window.netpair_manage_hint\') }}</span>' +
                      '<button class="settings-btn settings-btn--sm settings-btn--accent" @click="goToDevicesTab" style="margin-left:auto;flex-shrink:0">{{ t(\'settings_window.netpair_manage_cta\') }}</button>' +
                    '</div>' +
                  '</div>' +

                  '<div style="margin-top:6px">' +
                    '<button class="settings-btn settings-btn--sm" @click="brokersOpen = !brokersOpen">{{ brokersOpen ? \'▾\' : \'▸\' }} {{ t(\'settings_window.relay_brokers_toggle\') }}</button>' +
                  '</div>' +
                  '<template v-if="brokersOpen">' +
                    '<div class="settings-field" style="margin-top:8px">' +
                      '<label class="settings-field__label">{{ t(\'settings_window.relay_brokers_label\') }}</label>' +
                      '<textarea class="settings-input" rows="4" v-model="relayBrokersText" placeholder="wss://broker.emqx.io:8884/mqtt"></textarea>' +
                      '<span class="settings-hint">{{ t(\'settings_window.relay_brokers_hint\') }}</span>' +
                    '</div>' +
                    '<button class="settings-btn settings-btn--accent" @click="saveRelayBrokers" :disabled="brokersSaving" style="width:100%;margin-top:4px">' +
                      '{{ brokersSaving ? \'...\' : t(\'settings_window.save_relay_brokers\') }}' +
                    '</button>' +
                    // Test connectivity of the staged list (no need to save first).
                    '<button class="settings-btn settings-btn--sm" @click="testRelay" :disabled="relayTesting || brokersSaving" style="width:100%;margin-top:6px">' +
                      '{{ relayTesting ? \'...\' : t(\'settings_window.test_relay_btn\') }}' +
                    '</button>' +
                    '<div v-if="relayTestResult" class="settings-field" style="margin-top:8px">' +
                      '<span class="settings-field__label">{{ t(\'settings_window.test_relay_result\') }}: <span :style="{ color: relayTestResult.results.length && relayTestResult.results.every(r =&gt; r.ok) ? \'var(--clipsync-success)\' : \'var(--clipsync-danger)\' }">{{ relayTestResult.summary }}</span></span>' +
                      '<div v-for="r in relayTestResult.results" :key="r.endpoint" style="margin-top:4px">' +
                        '<span :style="{ color: r.ok ? \'var(--clipsync-success)\' : \'var(--clipsync-danger)\' }">{{ r.ok ? \'✓\' : \'✗\' }}</span> ' +
                        '<span style="word-break:break-all">{{ r.endpoint }}</span>' +
                        '<span v-if="r.ok && r.latency_ms != null" style="opacity:.65"> · {{ r.latency_ms }} ms</span>' +
                        '<span v-if="!r.ok && r.detail" style="color:var(--clipsync-danger);word-break:break-all"> — {{ r.detail }}</span>' +
                      '</div>' +
                    '</div>' +
                  '</template>' +
                '</section>' +

                '<!-- ═══════ Web Companion ═══════ -->' +
                '<section v-if="activeSection === \'web\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings_nav.web_companion\') }}</h3>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.web_enable\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="webEnabled" :aria-label="t(\'settings_window.web_enable\')" :class="{ \'settings-toggle--on\': webEnabled }" @click="webEnabled = !webEnabled">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<p v-if="uiBackend === \'webview\'" class="settings-hint" style="margin:-4px 0 8px">{{ t(\'settings_window.web_enable_webview_hint\') }}</p>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.web_port\') }}</label>' +
                    '<input type="number" class="settings-input" v-model="webPort" min="1024" max="65535" placeholder="9580">' +
                    '<span class="settings-hint">{{ t(\'settings_window.web_port_hint\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.web_history_limit\') }}</label>' +
                    '<input type="number" class="settings-input" v-model="webHistoryLimit" min="1" max="20">' +
                    '<span class="settings-hint">{{ t(\'settings_window.web_history_limit_desc\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.web_token\') }}</label>' +
                    '<div class="settings-field__row">' +
                      '<code class="settings-token">{{ webToken || t(\'settings_window.web_token_none\') }}</code>' +
                      '<button class="settings-btn settings-btn--sm" @click="regenerateToken">{{ t(\'settings_window.web_token_regenerate\') }}</button>' +
                      '<button class="settings-btn settings-btn--sm" @click="clearToken">{{ t(\'settings_window.web_token_clear\') }}</button>' +
                    '</div>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.web_local_url\') }}</label>' +
                    '<div class="settings-field__row">' +
                      '<code class="settings-token selectable">{{ webLocalUrl || webLanIp + \':\' + webPort }}</code>' +
                      '<button class="settings-btn settings-btn--sm" @click="copyUrl">{{ t(\'ui.copy\') }}</button>' +
                    '</div>' +
                  '</div>' +
                  '<button class="settings-btn settings-btn--accent" @click="saveWeb" :disabled="webSaving" style="width:100%;margin-top:8px">' +
                    '{{ webSaving ? \'...\' : t(\'settings_window.save_web\') }}' +
                  '</button>' +
                '</section>' +

                '<!-- ═══════ Translation ═══════ -->' +
                '<section v-if="activeSection === \'translation\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings_window.translation_title\') }}</h3>' +
                  '<p class="settings-hint" style="margin-bottom:12px">{{ t(\'settings_window.translation_hint\') }}</p>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.translate_url\') }}</label>' +
                    '<input type="text" class="settings-input" v-model="translateUrl" :placeholder="t(\'settings_window.translate_url_placeholder\')">' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.translate_api_key\') }}</label>' +
                    '<div class="settings-field__row">' +
                      '<input :type="showTranslateKey ? \'text\' : \'password\'" class="settings-input" v-model="translateKeyValue" :placeholder="translateKeySet ? \'●●●●●●●●\' : t(\'settings_window.translate_key_placeholder\')">' +
                      '<button class="settings-btn settings-btn--sm" @click="showTranslateKey = !showTranslateKey">{{ showTranslateKey ? t(\'settings_window.hide\') : t(\'settings_window.show\') }}</button>' +
                    '</div>' +
                    '<button v-if="translateKeySet" class="settings-btn settings-btn--sm" @click="clearTranslateKey" style="margin-top:4px">{{ t(\'settings_window.clear_translate_key\') }}</button>' +
                    '<span class="settings-hint">{{ translateKeySet ? t(\'settings_window.translate_key_set\') : t(\'settings_window.translate_key_not_set\') }}</span>' +
                  '</div>' +
                  '<button class="settings-btn settings-btn--accent" @click="saveTranslation" :disabled="translateSaving" style="width:100%;margin-top:8px">' +
                    '{{ translateSaving ? \'...\' : t(\'settings_window.save_translation\') }}' +
                  '</button>' +
                '</section>' +

                '<!-- ═══════ Content Filter ═══════ -->' +
                '<section v-if="activeSection === \'filter\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings_window.filter_title\') }}</h3>' +
                  '<p class="settings-hint" style="margin-bottom:12px">{{ t(\'settings_window.filter_desc\') }}</p>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.filter_enable\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="filterEnabled" :aria-label="t(\'settings_window.filter_enable\')" :class="{ \'settings-toggle--on\': filterEnabled }" @click="filterEnabled = !filterEnabled">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<template v-if="filterEnabled">' +
                    '<h4 style="font-size:12px;color:var(--clipsync-fg-muted);margin:12px 0 8px">{{ t(\'settings_window.filter_categories\') }}</h4>' +
                    '<div class="settings-toggle-row">' +
                      '<span class="settings-toggle-label">{{ t(\'filter.credit_card\') }}</span>' +
                      '<button class="settings-toggle" role="switch" :aria-checked="filterCreditCard" :aria-label="t(\'filter.credit_card\')" :class="{ \'settings-toggle--on\': filterCreditCard }" @click="filterCreditCard = !filterCreditCard">' +
                        '<span class="settings-toggle__knob"></span>' +
                      '</button>' +
                    '</div>' +
                    '<div class="settings-toggle-row">' +
                      '<span class="settings-toggle-label">{{ t(\'filter.ssn\') }}</span>' +
                      '<button class="settings-toggle" role="switch" :aria-checked="filterSSN" :aria-label="t(\'filter.ssn\')" :class="{ \'settings-toggle--on\': filterSSN }" @click="filterSSN = !filterSSN">' +
                        '<span class="settings-toggle__knob"></span>' +
                      '</button>' +
                    '</div>' +
                    '<div class="settings-toggle-row">' +
                      '<span class="settings-toggle-label">{{ t(\'filter.api_key\') }}</span>' +
                      '<button class="settings-toggle" role="switch" :aria-checked="filterApiKey" :aria-label="t(\'filter.api_key\')" :class="{ \'settings-toggle--on\': filterApiKey }" @click="filterApiKey = !filterApiKey">' +
                        '<span class="settings-toggle__knob"></span>' +
                      '</button>' +
                    '</div>' +
                    '<div class="settings-toggle-row">' +
                      '<span class="settings-toggle-label">{{ t(\'filter.private_key\') }}</span>' +
                      '<button class="settings-toggle" role="switch" :aria-checked="filterPrivateKey" :aria-label="t(\'filter.private_key\')" :class="{ \'settings-toggle--on\': filterPrivateKey }" @click="filterPrivateKey = !filterPrivateKey">' +
                        '<span class="settings-toggle__knob"></span>' +
                      '</button>' +
                    '</div>' +
                    '<div class="settings-toggle-row">' +
                      '<span class="settings-toggle-label">{{ t(\'filter.password\') }}</span>' +
                      '<button class="settings-toggle" role="switch" :aria-checked="filterPassword" :aria-label="t(\'filter.password\')" :class="{ \'settings-toggle--on\': filterPassword }" @click="filterPassword = !filterPassword">' +
                        '<span class="settings-toggle__knob"></span>' +
                      '</button>' +
                    '</div>' +
                  '</template>' +

                  '<h4 style="font-size:12px;color:var(--clipsync-fg-muted);margin:16px 0 8px">{{ t(\'settings_window.app_filter_title\') }}</h4>' +
                  '<p class="settings-hint" style="margin-bottom:12px">{{ t(\'settings_window.app_filter_desc\') }}</p>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.app_filter_enable\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="appFilterEnabled" :aria-label="t(\'settings_window.app_filter_enable\')" :class="{ \'settings-toggle--on\': appFilterEnabled }" @click="appFilterEnabled = !appFilterEnabled">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<template v-if="appFilterEnabled">' +
                    '<div class="settings-field">' +
                      '<label class="settings-field__label">{{ t(\'settings_window.app_filter_mode\') }}</label>' +
                      '<select class="settings-select" v-model="appFilterMode">' +
                        '<option value="blacklist">{{ t(\'settings_window.app_filter_blacklist\') }}</option>' +
                        '<option value="whitelist">{{ t(\'settings_window.app_filter_whitelist\') }}</option>' +
                      '</select>' +
                    '</div>' +
                    '<div class="settings-field">' +
                      '<label class="settings-field__label">{{ t(\'settings_window.app_filter_list\') }}</label>' +
                      '<textarea class="settings-input" rows="4" v-model="appFilterList" :placeholder="t(\'settings_window.app_filter_list_placeholder\')"></textarea>' +
                    '</div>' +
                  '</template>' +
                  '<button class="settings-btn settings-btn--accent" @click="saveFilter" :disabled="filterSaving" style="width:100%;margin-top:12px">' +
                    '{{ filterSaving ? \'...\' : t(\'settings_window.save_filter\') }}' +
                  '</button>' +
                '</section>' +

                '<!-- ═══════ Security ═══════ -->' +
                '<section v-if="activeSection === \'security\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'security.title\') }}</h3>' +
                  '<p class="settings-hint" style="margin-bottom:12px">{{ t(\'settings_window.security_desc\') }}</p>' +

                  '<!-- Block 1/3: Encryption -->' +
                  '<h3 class="settings-section__title settings-section__title--sub">{{ t(\'settings_window.encryption_title\') }}</h3>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.enable_encryption\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="encryptionEnabled" :aria-label="t(\'settings_window.enable_encryption\')" :class="{ \'settings-toggle--on\': encryptionEnabled }" @click="encryptionEnabled = !encryptionEnabled">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-field" style="margin-top:12px">' +
                    '<label class="settings-field__label">{{ t(\'security.pre_shared_password\') }}</label>' +
                    '<div class="settings-field__row">' +
                      '<input :type="showPassword ? \'text\' : \'password\'" class="settings-input" v-model="passwordValue" :placeholder="passwordSet ? \'●●●●●●●●\' : t(\'settings_window.password_placeholder\')">' +
                      '<button class="settings-btn settings-btn--sm" @click="showPassword = !showPassword">{{ showPassword ? t(\'settings_window.hide\') : t(\'settings_window.show\') }}</button>' +
                    '</div>' +
                    '<button v-if="passwordSet" class="settings-btn settings-btn--sm" @click="clearPassword" style="margin-top:4px">{{ t(\'settings_window.clear_password\') }}</button>' +
                    '<span class="settings-hint">{{ passwordSet ? t(\'security.password_set\') : t(\'security.no_password\') }}</span>' +
                    '<p class="settings-hint" style="margin-top:8px">{{ t(\'settings_window.password_hint\') }}</p>' +
                    '<p class="settings-hint" style="margin-top:8px">{{ t(\'settings_window.encryption_hint\') }}</p>' +
                  '</div>' +

                  '<!-- Block 2/3: Notifications -->' +
                  '<h3 class="settings-section__title settings-section__title--sub" style="margin-top:28px">{{ t(\'settings_window.notify_title\') }}</h3>' +
                  '<p class="settings-hint" style="margin-bottom:8px">{{ t(\'settings_window.notify_desc\') }}</p>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label"><strong>{{ t(\'settings.sound\') }}</strong></span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="store.soundEnabled" :aria-label="t(\'settings.sound\')" :class="{ \'settings-toggle--on\': store.soundEnabled }" @click="toggleSound">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<p class="settings-hint" style="margin-bottom:8px">{{ t(\'settings_window.sound_hint\') }}</p>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.notify_device_connect\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="notifyDeviceConnect" :aria-label="t(\'settings_window.notify_device_connect\')" :class="{ \'settings-toggle--on\': notifyDeviceConnect }" @click="notifyDeviceConnect = !notifyDeviceConnect">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.notify_transfer\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="notifyTransfer" :aria-label="t(\'settings_window.notify_transfer\')" :class="{ \'settings-toggle--on\': notifyTransfer }" @click="notifyTransfer = !notifyTransfer">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.notify_pairing\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="notifyPairing" :aria-label="t(\'settings_window.notify_pairing\')" :class="{ \'settings-toggle--on\': notifyPairing }" @click="notifyPairing = !notifyPairing">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.notify_sync\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="notifySync" :aria-label="t(\'settings_window.notify_sync\')" :class="{ \'settings-toggle--on\': notifySync }" @click="notifySync = !notifySync">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +

                  '<!-- Block 3/3: Certificates -->' +
                  '<div style="display:flex;align-items:center;justify-content:space-between;margin-top:28px">' +
                    '<h3 class="settings-section__title settings-section__title--sub" style="margin:0">{{ t(\'settings_window.certs_title\') }}</h3>' +
                    '<button class="settings-btn settings-btn--sm" @click="loadCerts" :disabled="certsLoading">{{ t(\'ui.refresh\') }}</button>' +
                  '</div>' +
                  '<div v-if="certsLoading" class="settings-hint" style="padding:8px 0">{{ t(\'ui.loading\') }}</div>' +
                  '<div v-else-if="certDevices.length === 0" class="settings-hint" style="padding:8px 0">{{ t(\'settings_window.certs_empty\') }}</div>' +
                  '<div v-else class="settings-backup-list">' +
                    '<div v-for="dev in certDevices" :key="dev.device_id" class="settings-backup-item">' +
                      '<div class="settings-backup-item__info">' +
                        '<span class="settings-backup-item__name">{{ dev.device_name || shortId(dev.device_id) }}</span>' +
                        '<span class="settings-backup-item__meta text-mono selectable">{{ shortId(dev.device_id) }} · {{ dev.fingerprint_short || \'—\' }}</span>' +
                      '</div>' +
                      '<span v-if="dev.paired" class="settings-hint" style="font-size:11px;color:var(--clipsync-success);flex-shrink:0">{{ t(\'ui.paired\') }}</span>' +
                    '</div>' +
                  '</div>' +

                  '<button class="settings-btn settings-btn--accent" @click="saveSecurity" :disabled="securitySaving" style="width:100%;margin-top:12px">' +
                    '{{ securitySaving ? \'...\' : t(\'settings_window.save_security\') }}' +
                  '</button>' +
                '</section>' +

                '<!-- ═══════ Advanced ═══════ -->' +
                '<section v-if="activeSection === \'advanced\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings_window.advanced_title\') }}</h3>' +
                  '<p class="settings-hint" style="margin-bottom:12px">{{ t(\'settings_window.advanced_hint\') }}</p>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.history_max\') }}</label>' +
                    '<input type="number" class="settings-input" v-model="historyMax" min="10" max="1000">' +
                    '<span class="settings-hint">{{ t(\'settings_window.history_max_desc\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.history_max_age\') }}</label>' +
                    '<input type="number" class="settings-input" v-model="historyMaxAgeDays" min="0" max="36500" step="0.5">' +
                    '<span class="settings-hint">{{ t(\'settings_window.history_max_age_desc\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.sync_debounce\') }}</label>' +
                    '<input type="number" class="settings-input" v-model="syncDebounce" min="0.1" max="5.0" step="0.1">' +
                    '<span class="settings-hint">{{ t(\'settings_window.sync_debounce_desc\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.poll_interval\') }}</label>' +
                    '<input type="number" class="settings-input" v-model="pollInterval" min="0.1" max="5.0" step="0.1">' +
                    '<span class="settings-hint">{{ t(\'settings_window.poll_interval_desc\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.receive_dir\') }}</label>' +
                    '<input type="text" class="settings-input" v-model="receiveDir" :placeholder="t(\'settings_window.receive_dir_placeholder\')">' +
                    '<span class="settings-hint">{{ t(\'settings_window.receive_dir_desc\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.transfer_timeout\') }}</label>' +
                    '<input type="number" class="settings-input" v-model="transferTimeout" min="30" max="3600">' +
                    '<span class="settings-hint">{{ t(\'settings_window.transfer_timeout_desc\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.max_reconnect\') }}</label>' +
                    '<input type="number" class="settings-input" v-model="maxReconnect" min="1" max="100">' +
                    '<span class="settings-hint">{{ t(\'settings_window.max_reconnect_desc\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.log_level\') }}</label>' +
                    '<select class="settings-select" v-model="logLevel">' +
                      '<option v-for="lvl in logLevelOptions" :key="lvl" :value="lvl">{{ lvl }}</option>' +
                    '</select>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.enable_notifications\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="notificationsEnabled" :aria-label="t(\'settings_window.enable_notifications\')" :class="{ \'settings-toggle--on\': notificationsEnabled }" @click="notificationsEnabled = !notificationsEnabled">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +

                  '<h4 style="font-size:12px;color:var(--clipsync-fg-muted);margin:16px 0 8px">{{ t(\'settings_window.clipboard_behavior\') }}</h4>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.paste_to_top\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="pasteToTop" :aria-label="t(\'settings_window.paste_to_top\')" :class="{ \'settings-toggle--on\': pasteToTop }" @click="pasteToTop = !pasteToTop">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.low_memory_mode\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="lowMemory" :aria-label="t(\'settings_window.low_memory_mode\')" :class="{ \'settings-toggle--on\': lowMemory }" @click="lowMemory = !lowMemory">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.retry_capture\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="retryCapture" :aria-label="t(\'settings_window.retry_capture\')" :class="{ \'settings-toggle--on\': retryCapture }" @click="retryCapture = !retryCapture">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.source_tracking\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="sourceTracking" :aria-label="t(\'settings_window.source_tracking\')" :class="{ \'settings-toggle--on\': sourceTracking }" @click="sourceTracking = !sourceTracking">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.plain_text_only\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="plainTextOnly" :aria-label="t(\'settings_window.plain_text_only\')" :class="{ \'settings-toggle--on\': plainTextOnly }" @click="plainTextOnly = !plainTextOnly">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<p class="settings-hint">{{ t(\'settings_window.plain_text_only_desc\') }}</p>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.dedup_method\') }}</label>' +
                    '<select class="settings-select" v-model="dedupMethod">' +
                      '<option value="sha256">{{ t(\'settings_window.dedup_sha256\') }}</option>' +
                      '<option value="simple">{{ t(\'settings_window.dedup_simple\') }}</option>' +
                    '</select>' +
                  '</div>' +

                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'hotkeys.enabled\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="hotkeysEnabled" :aria-label="t(\'hotkeys.enabled\')" :class="{ \'settings-toggle--on\': hotkeysEnabled }" @click="hotkeysEnabled = !hotkeysEnabled">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<h4 style="font-size:12px;color:var(--clipsync-fg-muted);margin:16px 0 4px">{{ t(\'hotkeys.title\') }}</h4>' +
                  '<p class="settings-hint" style="margin-bottom:10px">{{ t(\'hotkeys.hint\') }}</p>' +
                  '<div class="settings-hotkeys">' +
                    '<div v-for="hk in hotkeyFields" :key="hk.key" class="settings-hotkey-row">' +
                      '<span class="settings-hotkey-label">{{ hotkeyLabel(hk.key) }}</span>' +
                      '<input type="text" class="settings-input settings-hotkey-input" v-model="hotkeys[hk.key]" :placeholder="hk.value">' +
                    '</div>' +
                  '</div>' +
                  '<button class="settings-btn settings-btn--accent" @click="saveAdvanced" :disabled="advancedSaving" style="width:100%;margin-top:12px">' +
                    '{{ advancedSaving ? \'...\' : t(\'settings_window.save_advanced\') }}' +
                  '</button>' +
                '</section>' +

                '<!-- ═══════ Logs ═══════ -->' +
                '<section v-if="activeSection === \'logs\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings_window.logs_title\') }}</h3>' +
                  '<div class="settings-btn-grid" style="margin-bottom:12px">' +
                    '<button class="settings-btn" @click="loadLogs" :disabled="logsLoading">' +
                      '{{ logsLoading ? \'...\' : t(\'settings_window.logs_refresh\') }}' +
                    '</button>' +
                    '<button class="settings-btn settings-btn--accent" @click="exportLogs" :disabled="logsLoading || !logs">{{ t(\'settings_window.logs_export\') }}</button>' +
                  '</div>' +
                  '<pre v-if="logs" class="settings-log-view selectable" style="max-height:320px;overflow:auto;padding:var(--clipsync-space-3);border-radius:var(--clipsync-radius-md);border:1px solid var(--clipsync-border-strong);background:var(--clipsync-panel-2);color:var(--clipsync-fg);font-family:var(--clipsync-font-mono);font-size:0.75rem;line-height:1.5;white-space:pre-wrap;word-break:break-all;margin:0">{{ logs }}</pre>' +
                  '<p v-else class="settings-hint" style="padding:8px 0">{{ t(\'settings_window.no_logs\') }}</p>' +
                '</section>' +

                '<!-- ═══════ Data ═══════ -->' +
                '<section v-if="activeSection === \'data\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings.data\') }}</h3>' +
                  '<div class="settings-btn-grid">' +
                    '<button class="settings-btn" @click="exportData(\'json\')" :disabled="exporting">' +
                      '{{ exporting ? \'...\' : t(\'settings.export_json\') }}' +
                    '</button>' +
                    '<button class="settings-btn" @click="exportData(\'csv\')" :disabled="exporting">' +
                      '{{ exporting ? \'...\' : t(\'settings.export_csv\') }}' +
                    '</button>' +
                    '<button class="settings-btn" @click="exportData(\'markdown\')" :disabled="exporting">' +
                      '{{ exporting ? \'...\' : t(\'settings.export_markdown\') }}' +
                    '</button>' +
                    '<button class="settings-btn" @click="importData" :disabled="importing">' +
                      '{{ importing ? \'...\' : t(\'settings.import\') }}' +
                    '</button>' +
                    '<button class="settings-btn settings-btn--accent" @click="createBackup" :disabled="backingUp">' +
                      '{{ backingUp ? \'...\' : t(\'settings.create_backup\') }}' +
                    '</button>' +
                  '</div>' +
                  '<div v-if="backupList.length > 0" class="settings-backup-list">' +
                    '<h4 class="settings-section__subtitle">{{ t(\'settings.backup_list\') }}</h4>' +
                    '<div v-for="bk in backupList" :key="bk.filename" class="settings-backup-item">' +
                      '<div class="settings-backup-item__info">' +
                        '<span class="settings-backup-item__name">{{ bk.filename }}</span>' +
                        '<span class="settings-backup-item__meta">{{ bk.date }} · {{ bk.size }}</span>' +
                      '</div>' +
                      '<button class="settings-btn settings-btn--sm" @click="restoreBackup(bk.path || bk.filename)" :disabled="restoring">' +
                        '{{ restoring ? \'...\' : t(\'settings.restore_backup\') }}' +
                      '</button>' +
                    '</div>' +
                  '</div>' +
                  '<div class="settings-btn-grid" style="margin-top:12px">' +
                    '<button class="settings-btn" @click="openDataFolder(\'data\')">{{ t(\'settings.open_data_folder\') }}</button>' +
                    '<button class="settings-btn" @click="openDataFolder(\'backups\')">{{ t(\'settings.open_backups_folder\') }}</button>' +
                  '</div>' +
                  '<div class="settings-field" style="margin-top:16px">' +
                    '<label class="settings-field__label">{{ t(\'settings.data_dir\') }}</label>' +
                    '<input type="text" class="settings-input" v-model="dataDir" :placeholder="t(\'settings.data_dir_placeholder\')">' +
                    '<span class="settings-hint">{{ t(\'settings.data_dir_hint\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings.favorites_path\') }}</label>' +
                    '<input type="text" class="settings-input" v-model="favoritesPath" :placeholder="t(\'settings.favorites_path_placeholder\')">' +
                    '<span class="settings-hint">{{ t(\'settings.favorites_path_hint\') }}</span>' +
                  '</div>' +
                  '<button class="settings-btn settings-btn--accent" @click="saveDataPaths" :disabled="dataSaving" style="width:100%;margin-top:8px">' +
                    '{{ dataSaving ? \'...\' : t(\'settings.save_data_paths\') }}' +
                  '</button>' +
                '</section>' +

                '<!-- ═══════ AI Config (round 12) ═══════ -->' +
                '<section v-if="activeSection === \'aiconfig\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings_nav.aiconfig\') }}</h3>' +
                  '<p class="settings-hint" style="margin-bottom:12px">{{ t(\'settings_window.aiconfig_desc\') }}</p>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.aiconfig_paths_label\') }}</label>' +
                    '<div v-for="(p, idx) in aiConfigPaths" :key="\'aipath-\' + idx" class="settings-field__row">' +
                      '<input type="text" class="settings-input" v-model="aiConfigPaths[idx]" spellcheck="false"' +
                        ' :placeholder="t(\'settings_window.aiconfig_path_placeholder\')"' +
                        ' :aria-label="t(\'settings_window.aiconfig_paths_label\')">' +
                      '<button class="settings-btn settings-btn--sm" @click="removeAiConfigPath(idx)" :aria-label="t(\'ui.delete\')">✕</button>' +
                    '</div>' +
                    '<button class="settings-btn settings-btn--sm" @click="addAiConfigPath" style="margin-top:6px">+ {{ t(\'settings_window.aiconfig_add_path\') }}</button>' +
                    '<span class="settings-hint">{{ t(\'settings_window.aiconfig_paths_hint\') }}</span>' +
                  '</div>' +
                  '<button class="settings-btn settings-btn--accent" @click="saveAiConfigPaths" :disabled="aiConfigSaving" style="width:100%;margin-top:8px">' +
                    '{{ aiConfigSaving ? \'...\' : t(\'settings_window.save_aiconfig\') }}' +
                  '</button>' +
                  '<p class="settings-hint" style="margin-top:6px">{{ t(\'settings_window.aiconfig_save_hint\') }}</p>' +
                '</section>' +

                '<!-- ═══════ About ═══════ -->' +
                '<section v-if="activeSection === \'about\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings.about\') }}</h3>' +
                  '<div class="settings-about-grid">' +
                    '<div class="settings-about-item">' +
                      '<span class="settings-about-item__label">{{ t(\'settings.version\') }}</span>' +
                      '<span class="settings-about-item__value">{{ store.overview.version || t(\'settings_window.about_version\') }}</span>' +
                    '</div>' +
                    '<div class="settings-about-item">' +
                      '<span class="settings-about-item__label">{{ t(\'settings.device_name\') }}</span>' +
                      '<span class="settings-about-item__value">{{ store.deviceName }}</span>' +
                    '</div>' +
                    '<div class="settings-about-item">' +
                      '<span class="settings-about-item__label">{{ t(\'settings.device_id\') }}</span>' +
                      '<span class="settings-about-item__value settings-about-item__value--mono selectable">{{ store.deviceId }}</span>' +
                    '</div>' +
                    '<div class="settings-about-item">' +
                      '<span class="settings-about-item__label">{{ t(\'overview.platform\') }}</span>' +
                      '<span class="settings-about-item__value">{{ store.overview.platform || \'—\' }}</span>' +
                    '</div>' +
                  '</div>' +
                  '<p style="font-size:12px;color:var(--clipsync-fg-muted);margin-top:12px;line-height:1.6">{{ t(\'settings_window.about_desc\') }}</p>' +

                  '<h3 class="settings-section__title" style="margin-top:20px">{{ t(\'settings_window.auto_update_check\') }}</h3>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.auto_update_check_toggle\') }}</span>' +
                    '<button class="settings-toggle" role="switch" :aria-checked="autoUpdateCheck" :aria-label="t(\'settings_window.auto_update_check_toggle\')" :class="{ \'settings-toggle--on\': autoUpdateCheck }" @click="toggleAutoUpdateCheck">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<p class="settings-hint">{{ t(\'settings_window.auto_update_check_hint\') }}</p>' +

                  '<div style="display:flex;gap:8px;margin-top:14px">' +
                    '<button class="settings-btn" @click="checkForUpdate" :disabled="updateChecking || updateDownloading" style="flex:1">' +
                      '{{ updateChecking ? \'...\' : t(\'settings_window.update_check_now\') }}' +
                    '</button>' +
                  '</div>' +
                  '<p v-if="updateAvailable" class="settings-hint" style="margin-top:10px;color:var(--clipsync-accent,#22D3EE)">{{ t(\'settings_window.update_available_found\', { version: updateLatest }) }}</p>' +

                  '<button v-if="updateAvailable" class="settings-btn settings-btn--accent" @click="installUpdate" :disabled="updateDownloading" style="width:100%;margin-top:10px">' +
                    '{{ updateDownloading ? \'...\' : t(\'settings_window.update_install_now\') }}' +
                  '</button>' +
                  '<button class="settings-btn" @click="downloadUpdate" :disabled="updateDownloading" style="width:100%;margin-top:10px">' +
                    '{{ updateDownloading ? \'...\' : t(\'settings_window.update_download\') }}' +
                  '</button>' +
                  '<p class="settings-hint" style="margin-top:8px">{{ t(\'settings_window.update_hint\') }}</p>' +
                '</section>' +

                '<!-- ═══════ Danger Zone ═══════ -->' +
                '<section v-if="activeSection === \'danger\'" class="settings-section">' +
                  '<h3 class="settings-section__title" style="color:var(--clipsync-danger)">{{ t(\'settings_window.danger_zone\') }}</h3>' +
                  '<p class="settings-hint" style="margin-bottom:12px">{{ t(\'settings_window.danger_zone_desc\') }}</p>' +
                  '<div style="display:flex;flex-direction:column;gap:8px">' +
                    '<button class="settings-btn" @click="restartApp" :disabled="restarting" style="width:100%">' +
                      '{{ restarting ? \'...\' : t(\'settings_window.restart_app\') }}' +
                    '</button>' +
                    '<button class="settings-btn" @click="factoryReset" :disabled="resetting" style="width:100%;border-color:var(--clipsync-danger);color:var(--clipsync-danger)">' +
                      '{{ resetting ? \'...\' : t(\'settings_window.factory_reset\') }}' +
                    '</button>' +
                  '</div>' +
                '</section>' +

              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</transition>',
  };

})();
