/* ═══════════════════════════════════════════════════════════════════
   ClipSync Settings Dialog Component
   Centered modal dialog with tab navigation on the left and content
   on the right. Replaces the old slide-in panel for a clean, spacious
   settings experience.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['settings-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        activeSection: 'appearance',

        // Network
        port: '',
        relayUrl: '',
        autoStart: false,
        serviceType: '',

        // Web Companion
        webEnabled: true,
        webPort: '',
        webHistoryLimit: 10,
        webToken: '',
        webLocalUrl: '',
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

        // Data locations
        dataDir: '',
        favoritesPath: '',
        dataSaving: false,

        // Hotkeys
        hotkeys: {},

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

        // Diagnostics (scan-style: checks revealed one by one)
        diagScanning: false,
        diagChecks: [],
        diagRevealed: 0,
        diagSummary: '',

        // Update download
        updateDownloading: false,

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
          'quick_paste', 'paste_1', 'paste_2', 'paste_3', 'paste_4',
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
        return [
          { id: 'appearance',    label: this.t('settings_nav.appearance') },
          { id: 'network',       label: this.t('settings_nav.network') },
          { id: 'web',           label: this.t('settings_nav.web_companion') },
          { id: 'translation',   label: this.t('settings_nav.translation') },
          { id: 'filter',        label: this.t('settings_nav.filter') },
          { id: 'security',      label: this.t('settings_nav.security') },
          { id: 'advanced',      label: this.t('settings_nav.advanced') },
          { id: 'logs',          label: this.t('settings_nav.logs') },
          { id: 'data',          label: this.t('settings.data') },
          { id: 'about',         label: this.t('settings_nav.about') },
          { id: 'danger',        label: this.t('settings_window.danger_zone') },
        ];
      },

      diagRows: function () {
        var r = this.diagResult || {};
        var t = this.t;
        return [
          { label: t('settings_window.diag_discovery'), value: r.discovery_running, bool: true },
          { label: t('settings_window.diag_server'), value: r.server_running, bool: true },
          { label: t('settings_window.diag_web'), value: r.web_companion_running, bool: true },
          { label: t('settings_window.diag_connected'), value: r.connected_count, bool: false },
          { label: t('settings_window.diag_paired'), value: r.paired_count, bool: false },
          { label: t('settings_window.diag_web_port'), value: r.web_port, bool: false },
          { label: t('settings_window.diag_lan_ip'), value: r.lan_ip, bool: false },
          { label: t('settings_window.diag_os'), value: r.os, bool: false },
          { label: t('settings_window.diag_version'), value: r.version, bool: false },
        ];
      },
    },

    methods: {
      selectSection: function (id) {
        this.activeSection = id;
      },

      hotkeyLabel: function (key) {
        return this.t('hotkeys.' + key);
      },

      populateFromCache: function () {
        var s = this.store.settingsCache || {};
        if (s.port !== undefined) this.port = String(s.port);
        if (s.relay_url !== undefined) this.relayUrl = s.relay_url || '';
        if (s.auto_start !== undefined) this.autoStart = !!s.auto_start;
        if (s.service_type !== undefined) this.serviceType = s.service_type || '';
        if (s.web_enabled !== undefined) this.webEnabled = !!s.web_enabled;
        if (s.web_port !== undefined) this.webPort = String(s.web_port);
        if (s.web_history_limit !== undefined) this.webHistoryLimit = s.web_history_limit;
        this.webToken = this.store.token || '';
        this.webLanIp = this.store.overview.localIp || '';
        this.webLocalUrl = (this.webLanIp && this.webPort) ? 'http://' + this.webLanIp + ':' + this.webPort : '';
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
        if (s.data_dir !== undefined) this.dataDir = s.data_dir || '';
        if (s.favorites_path !== undefined) this.favoritesPath = s.favorites_path || '';
        if (s.hotkeys) this.hotkeys = Object.assign({}, s.hotkeys);
      },

      // ── Save methods ─────────────────────────────────────────────

      saveNetwork: function () {
        var self = this;
        self.networkSaving = true;
        ClipsyncAPI.updateSettings({
          port: parseInt(self.port, 10) || 53317,
          relay_url: self.relayUrl,
          auto_start: self.autoStart,
          service_type: (self.serviceType || '_clipsync._tcp.local.').trim(),
        }).then(function (res) {
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.store.showToast(self.t('settings_window.network_saved'), 3000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_network_failed'), 2000);
        }).finally(function () {
          self.networkSaving = false;
        });
      },

      saveWeb: function () {
        var self = this;
        self.webSaving = true;
        ClipsyncAPI.updateSettings({
          web_enabled: self.webEnabled,
          web_port: parseInt(self.webPort, 10) || 9580,
          web_history_limit: self.webHistoryLimit,
        }).then(function (res) {
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.store.showToast(self.t('settings_window.web_saved'), 3000);
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
          self.passwordValue = '';
          if (res && typeof res.password_set === 'boolean') {
            self.passwordSet = res.password_set;
          }
          if (res && res.updated) self.store.mergeSettings(res.updated);
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

      // ── Diagnostics ─────────────────────────────────────────────

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
          error: this.t('settings_window.diag_title'),
        };
        return labels[id] || id;
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

      // ── Update download ─────────────────────────────────────────

      downloadUpdate: function () {
        var self = this;
        self.updateDownloading = true;
        ClipsyncAPI._fetch('POST', '/api/update/download', {}).then(function (res) {
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
          self.translateKeyValue = '';
          if (res && res.updated) self.store.mergeSettings(res.updated);
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
          self.store.showToast(self.t('settings_window.translate_key_cleared'), 2000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_translation_failed'), 2000);
        });
      },

      saveAdvanced: function () {
        var self = this;
        self.advancedSaving = true;
        ClipsyncAPI.updateSettings({
          history_max_entries: parseInt(self.historyMax, 10) || 200,
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
          hotkeys: self.hotkeys,
        }).then(function (res) {
          if (res && res.updated) self.store.mergeSettings(res.updated);
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
        ClipsyncAPI.updateSettings({
          data_dir: (self.dataDir || '').trim(),
          favorites_path: (self.favoritesPath || '').trim(),
        }).then(function (res) {
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.store.showToast(self.t('settings.data_saved'), 2500);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_data_failed'), 2000);
        }).finally(function () {
          self.dataSaving = false;
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

      regenerateToken: function () {
        var self = this;
        ClipsyncAPI.updateSettings({ regenerate_web_token: true }).then(function (res) {
          if (res && res.ok) {
            // The new token is deliberately never returned to the client, so
            // clear the stale value and prompt the user to reconnect.
            self.webToken = '';
            self.store.showToast(self.t('settings.token_regenerated'), 2500);
          } else {
            self.store.showToast(self.t('settings.token_regenerate_failed'), 2000);
          }
        }).catch(function () {
          self.store.showToast(self.t('settings.token_regenerate_failed'), 2000);
        });
      },

      clearToken: function () {
        var self = this;
        ClipsyncAPI.updateSettings({ clear_web_token: true }).then(function () {
          self.webToken = '';
          self.store.showToast(self.t('settings.token_cleared'), 2000);
        }).catch(function () {
          self.store.showToast(self.t('settings.token_clear_failed'), 2000);
        });
      },

      copyUrl: function () {
        var url = this.webLocalUrl || (this.webLanIp && this.webPort ? 'http://' + this.webLanIp + ':' + this.webPort : '');
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
        this.store.closeSettingsPanel();
      },

      selectTheme: function (theme) {
        this.store.setTheme(theme);
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
            self.store.showToast(self.t('settings.exported', { count: res.count, format: format.toUpperCase() }), 2000);
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
        }).catch(function () {
          self.importing = false;
          self.store.showToast(self.t('settings.import_failed'), 2000);
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
            self.store.showToast(self.t('settings.restore_summary', {
              config: s.config ? self.t('ui.yes') : self.t('ui.no'),
              history: s.history || 0,
              favorites: s.favorites || 0,
            }), 3000);
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

      onKeyDown: function (e) {
        if (e.key === 'Escape') {
          this.close();
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
          var self = this;
          this.$nextTick(function () {
            document.addEventListener('keydown', self._onKeyDown);
          });
        } else {
          document.removeEventListener('keydown', this._onKeyDown);
        }
      },

      activeSection: function (val) {
        if (val === 'logs') this.loadLogs();
        if (val === 'security') this.loadCerts();
      },
    },

    created: function () {
      this._onKeyDown = this.onKeyDown.bind(this);
    },

    template:
      '<transition name="dialog-fade">' +
        '<div v-if="visible" class="settings-dialog-overlay" ref="overlay" @click="onOverlayClick" @contextmenu.prevent>' +
          '<div class="settings-dialog glass-neo">' +

            '<!-- Header -->' +
            '<div class="settings-dialog__header">' +
              '<h2 class="settings-dialog__title">{{ t(\'settings.title\') }}</h2>' +
              '<button class="settings-dialog__close" @click="close" :title="t(\'ui.close\')">' +
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
                  ' :class="{ \'settings-dialog__tab--active\': activeSection === tab.id }"' +
                  ' @click="selectSection(tab.id)"' +
                '>' +
                  '<span v-if="tab.icon" class="settings-dialog__tab-icon">{{ tab.icon }}</span>' +
                  '<span class="settings-dialog__tab-label">{{ tab.label }}</span>' +
                '</button>' +
              '</div>' +

              '<!-- Right content area -->' +
              '<div class="settings-dialog__content">' +

                '<!-- ═══════ Appearance ═══════ -->' +
                '<section v-if="activeSection === \'appearance\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings.appearance\') }}</h3>' +
                  '<div class="settings-theme-row">' +
                    '<button v-for="opt in themeOptions" :key="opt.value"' +
                      ' class="settings-theme-btn"' +
                      ' :class="{ \'settings-theme-btn--active\': store.theme === opt.value }"' +
                      ' @click="selectTheme(opt.value)">' +
                      '<span v-if="opt.icon" class="settings-theme-btn__icon">{{ opt.icon }}</span>' +
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
                    '<span class="settings-toggle-label">{{ t(\'settings.sound\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': store.soundEnabled }" @click="toggleSound">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings.animation\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': store.animationsEnabled }" @click="toggleAnimation">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings.ui_mode\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': uiBackend === \'webview\' }" @click="toggleUIMode">' +
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
                    '<label class="settings-field__label">{{ t(\'network.relay_url\') }}</label>' +
                    '<input type="text" class="settings-input" v-model="relayUrl" :placeholder="t(\'settings_window.relay_placeholder\')">' +
                    '<span class="settings-hint">{{ t(\'settings_window.relay_hint\') }}</span>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'network.auto_start\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': autoStart }" @click="autoStart = !autoStart">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'network.service_type\') }}</label>' +
                    '<input type="text" class="settings-input" v-model="serviceType" placeholder="_clipsync._tcp.local.">' +
                    '<span class="settings-hint">{{ t(\'settings_window.service_type_hint\') }}</span>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<span class="settings-field__label">{{ t(\'network.local_address\') }}</span>' +
                    '<span class="settings-field__value settings-field__value--mono">{{ store.overview.localIp }}:{{ store.overview.port || port }}</span>' +
                  '</div>' +
                  '<button class="settings-btn settings-btn--accent" @click="saveNetwork" :disabled="networkSaving" style="width:100%;margin-top:8px">' +
                    '{{ networkSaving ? \'...\' : t(\'settings_window.save_network\') }}' +
                  '</button>' +
                '</section>' +

                '<!-- ═══════ Web Companion ═══════ -->' +
                '<section v-if="activeSection === \'web\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings_nav.web_companion\') }}</h3>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.web_enable\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': webEnabled, \'settings-toggle--disabled\': uiBackend === \'webview\' }" @click="uiBackend !== \'webview\' && (webEnabled = !webEnabled)">' +
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
                      '<code class="settings-token">{{ webToken || \'(none)\' }}</code>' +
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
                    '<span class="settings-toggle-label">{{ t(\'settings_window.filter_title\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': filterEnabled }" @click="filterEnabled = !filterEnabled">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<template v-if="filterEnabled">' +
                    '<h4 style="font-size:12px;color:var(--clipsync-fg-muted);margin:12px 0 8px">{{ t(\'settings_window.filter_categories\') }}</h4>' +
                    '<div class="settings-toggle-row">' +
                      '<span class="settings-toggle-label">{{ t(\'filter.credit_card\') }}</span>' +
                      '<button class="settings-toggle" :class="{ \'settings-toggle--on\': filterCreditCard }" @click="filterCreditCard = !filterCreditCard">' +
                        '<span class="settings-toggle__knob"></span>' +
                      '</button>' +
                    '</div>' +
                    '<div class="settings-toggle-row">' +
                      '<span class="settings-toggle-label">{{ t(\'filter.ssn\') }}</span>' +
                      '<button class="settings-toggle" :class="{ \'settings-toggle--on\': filterSSN }" @click="filterSSN = !filterSSN">' +
                        '<span class="settings-toggle__knob"></span>' +
                      '</button>' +
                    '</div>' +
                    '<div class="settings-toggle-row">' +
                      '<span class="settings-toggle-label">{{ t(\'filter.api_key\') }}</span>' +
                      '<button class="settings-toggle" :class="{ \'settings-toggle--on\': filterApiKey }" @click="filterApiKey = !filterApiKey">' +
                        '<span class="settings-toggle__knob"></span>' +
                      '</button>' +
                    '</div>' +
                    '<div class="settings-toggle-row">' +
                      '<span class="settings-toggle-label">{{ t(\'filter.private_key\') }}</span>' +
                      '<button class="settings-toggle" :class="{ \'settings-toggle--on\': filterPrivateKey }" @click="filterPrivateKey = !filterPrivateKey">' +
                        '<span class="settings-toggle__knob"></span>' +
                      '</button>' +
                    '</div>' +
                    '<div class="settings-toggle-row">' +
                      '<span class="settings-toggle-label">{{ t(\'filter.password\') }}</span>' +
                      '<button class="settings-toggle" :class="{ \'settings-toggle--on\': filterPassword }" @click="filterPassword = !filterPassword">' +
                        '<span class="settings-toggle__knob"></span>' +
                      '</button>' +
                    '</div>' +
                  '</template>' +

                  '<h4 style="font-size:12px;color:var(--clipsync-fg-muted);margin:16px 0 8px">{{ t(\'settings_window.app_filter_title\') }}</h4>' +
                  '<p class="settings-hint" style="margin-bottom:12px">{{ t(\'settings_window.app_filter_desc\') }}</p>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.app_filter_enable\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': appFilterEnabled }" @click="appFilterEnabled = !appFilterEnabled">' +
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
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.enable_encryption\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': encryptionEnabled }" @click="encryptionEnabled = !encryptionEnabled">' +
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
                  '</div>' +

                  '<h4 style="font-size:12px;color:var(--clipsync-fg-muted);margin:16px 0 4px">{{ t(\'settings_window.notify_title\') }}</h4>' +
                  '<p class="settings-hint" style="margin-bottom:8px">{{ t(\'settings_window.notify_desc\') }}</p>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.notify_device_connect\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': notifyDeviceConnect }" @click="notifyDeviceConnect = !notifyDeviceConnect">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.notify_transfer\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': notifyTransfer }" @click="notifyTransfer = !notifyTransfer">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.notify_pairing\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': notifyPairing }" @click="notifyPairing = !notifyPairing">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.notify_sync\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': notifySync }" @click="notifySync = !notifySync">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +

                  '<div style="display:flex;align-items:center;justify-content:space-between;margin:16px 0 4px">' +
                    '<h4 style="font-size:12px;color:var(--clipsync-fg-muted);margin:0">{{ t(\'settings_window.certs_title\') }}</h4>' +
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
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': notificationsEnabled }" @click="notificationsEnabled = !notificationsEnabled">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +

                  '<h4 style="font-size:12px;color:var(--clipsync-fg-muted);margin:16px 0 8px">{{ t(\'settings_window.clipboard_behavior\') }}</h4>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.paste_to_top\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': pasteToTop }" @click="pasteToTop = !pasteToTop">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.low_memory_mode\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': lowMemory }" @click="lowMemory = !lowMemory">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.retry_capture\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': retryCapture }" @click="retryCapture = !retryCapture">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-toggle-row">' +
                    '<span class="settings-toggle-label">{{ t(\'settings_window.source_tracking\') }}</span>' +
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': sourceTracking }" @click="sourceTracking = !sourceTracking">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
                  '<div class="settings-field">' +
                    '<label class="settings-field__label">{{ t(\'settings_window.dedup_method\') }}</label>' +
                    '<select class="settings-select" v-model="dedupMethod">' +
                      '<option value="sha256">{{ t(\'settings_window.dedup_sha256\') }}</option>' +
                      '<option value="simple">{{ t(\'settings_window.dedup_simple\') }}</option>' +
                    '</select>' +
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

                  '<h4 style="font-size:12px;color:var(--clipsync-fg-muted);margin:20px 0 8px">{{ t(\'settings_window.diag_title\') }}</h4>' +
                  '<button class="settings-btn" @click="runDiagnostics" :disabled="diagScanning" style="width:100%">' +
                    '{{ diagScanning ? t(\'settings_window.diag_scanning\') : t(\'settings_window.diag_run\') }}' +
                  '</button>' +
                  '<div class="diag-scan" style="margin-top:12px">' +
                    '<div v-for="(chk, i) in diagChecks" :key="chk.id" class="diag-check"' +
                         ':class="{ \'diag-check--revealed\': i < diagRevealed, \'diag-check--ok\': chk.ok === true && i < diagRevealed, \'diag-check--fail\': chk.ok === false && i < diagRevealed }">' +
                      '<span class="diag-check__status">{{ i < diagRevealed ? (chk.ok ? \'✓\' : \'✕\') : \'·\' }}</span>' +
                      '<div class="diag-check__body">' +
                        '<span class="diag-check__label">{{ diagLabel(chk.id) }}</span>' +
                        '<span v-if="i < diagRevealed && chk.detail" class="diag-check__detail">{{ chk.detail }}</span>' +
                        '<span v-if="i < diagRevealed && chk.guidance" class="diag-check__guidance">💡 {{ chk.guidance }}</span>' +
                        '<button v-if="i < diagRevealed && (chk.id === \'firewall\' || chk.id === \'permissions\')" class="settings-btn settings-btn--sm" style="align-self:flex-start;margin-top:4px" @click="requestDiagnosticsAction(chk)">{{ t(\'settings_window.diag_request\') }}</button>' +
                      '</div>' +
                    '</div>' +
                  '</div>' +
                  '<div v-if="!diagScanning && diagChecks.length > 0 && diagRevealed >= diagChecks.length" class="diag-summary" :class="\'diag-summary--\' + diagSummary">' +
                    '{{ diagSummaryText }}' +
                  '</div>' +
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

                '<!-- ═══════ About ═══════ -->' +
                '<section v-if="activeSection === \'about\'" class="settings-section">' +
                  '<h3 class="settings-section__title">{{ t(\'settings.about\') }}</h3>' +
                  '<div class="settings-about-grid">' +
                    '<div class="settings-about-item">' +
                      '<span class="settings-about-item__label">{{ t(\'settings.version\') }}</span>' +
                      '<span class="settings-about-item__value">{{ t(\'settings_window.about_version\') }}</span>' +
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
                  '<button class="settings-btn settings-btn--accent" @click="downloadUpdate" :disabled="updateDownloading" style="width:100%;margin-top:16px">' +
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
