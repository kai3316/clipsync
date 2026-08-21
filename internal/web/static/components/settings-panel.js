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

        // Security
        encryptionEnabled: true,
        passwordValue: '',
        passwordSet: false,
        showPassword: false,

        // Advanced
        historyMax: 200,
        syncDebounce: 0.5,
        pollInterval: 0.5,
        receiveDir: '',
        transferTimeout: 300,
        maxReconnect: 10,
        logLevel: 'INFO',
        notificationsEnabled: true,

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

      sectionTabs: function () {
        // Plain-text labels, no emoji — the settings_nav.* translations no
        // longer carry emoji prefixes, so there is no icon column here.
        return [
          { id: 'appearance',    label: this.t('settings_nav.appearance') },
          { id: 'network',       label: this.t('settings_nav.network') },
          { id: 'web',           label: this.t('settings_nav.web_companion') },
          { id: 'filter',        label: this.t('settings_nav.filter') },
          { id: 'security',      label: this.t('settings_nav.security') },
          { id: 'advanced',      label: this.t('settings_nav.advanced') },
          { id: 'data',          label: this.t('settings.data') },
          { id: 'about',         label: this.t('settings_nav.about') },
          { id: 'danger',        label: this.t('settings_window.danger_zone') },
        ];
      },
    },

    methods: {
      selectSection: function (id) {
        this.activeSection = id;
      },

      populateFromCache: function () {
        var s = this.store.settingsCache || {};
        if (s.port !== undefined) this.port = String(s.port);
        if (s.relay_url !== undefined) this.relayUrl = s.relay_url || '';
        if (s.web_enabled !== undefined) this.webEnabled = !!s.web_enabled;
        if (s.web_port !== undefined) this.webPort = String(s.web_port);
        if (s.web_history_limit !== undefined) this.webHistoryLimit = s.web_history_limit;
        this.webToken = this.store.token || '';
        this.webLanIp = this.store.overview.localIp || '';
        this.webLocalUrl = (this.webLanIp && this.webPort) ? 'http://' + this.webLanIp + ':' + this.webPort : '';
        if (s.filter_enabled_categories !== undefined) {
          var cats = s.filter_enabled_categories || [];
          this.filterEnabled = cats.length > 0;
          this.filterCreditCard = cats.indexOf('credit_card') >= 0;
          this.filterSSN = cats.indexOf('ssn') >= 0;
          this.filterApiKey = cats.indexOf('api_key') >= 0;
          this.filterPrivateKey = cats.indexOf('private_key') >= 0;
          this.filterPassword = cats.indexOf('password') >= 0;
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
      },

      // ── Save methods ─────────────────────────────────────────────

      saveNetwork: function () {
        var self = this;
        self.networkSaving = true;
        ClipsyncAPI.updateSettings({
          port: parseInt(self.port, 10) || 53317,
          relay_url: self.relayUrl,
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
        var payload = { encryption_enabled: self.encryptionEnabled };
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
        }).then(function (res) {
          if (res && res.updated) self.store.mergeSettings(res.updated);
          self.store.showToast(self.t('settings_window.advanced_saved'), 3000);
        }).catch(function () {
          self.store.showToast(self.t('settings.save_advanced_failed'), 2000);
        }).finally(function () {
          self.advancedSaving = false;
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
            self.store.showToast(self.t('settings.backup_created'), 3000);
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
          var self = this;
          this.$nextTick(function () {
            document.addEventListener('keydown', self._onKeyDown);
          });
        } else {
          document.removeEventListener('keydown', this._onKeyDown);
        }
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
                    '<button class="settings-toggle" :class="{ \'settings-toggle--on\': webEnabled }" @click="webEnabled = !webEnabled">' +
                      '<span class="settings-toggle__knob"></span>' +
                    '</button>' +
                  '</div>' +
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
                  '<button class="settings-btn settings-btn--accent" @click="saveAdvanced" :disabled="advancedSaving" style="width:100%;margin-top:12px">' +
                    '{{ advancedSaving ? \'...\' : t(\'settings_window.save_advanced\') }}' +
                  '</button>' +
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
