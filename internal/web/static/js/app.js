/* ═══════════════════════════════════════════════════════════════════
   ClipSync Vue 3 Application Entry
   Creates the Vue app, provides the reactive store, registers all
   components, and mounts to #app.

   Components are defined in separate files under components/ and
   stored on window.__CLIPSYNC_COMPONENTS__ before this script runs.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  var createApp = Vue.createApp;
  var store = window.__CLIPSYNC_STORE__;

  if (!store) {
    console.error('[ClipSync] Store not found. Ensure store.js is loaded before app.js.');
    return;
  }

  var app = createApp({
    data: function () {
      return {
        store: store,
        isWideLayout: window.innerWidth >= 768,
      };
    },

    provide: function () {
      return {
        store: this.store,
      };
    },

    watch: {
      // Selection is per-panel: history selects by entry_id, favorites by
      // `.id`. Clearing on tab switch prevents favorite uuid strings leaking
      // into the history multi-select action bar (and vice versa).
      'store.activeTab': function () {
        this.store.clearSelection();
      },
    },

    mounted: function () {
      var self = this;

      // Track layout width for sidebar vs horizontal tabs
      var mq = window.matchMedia('(min-width: 768px)');
      this.isWideLayout = mq.matches;
      mq.addEventListener('change', function (e) {
        self.isWideLayout = e.matches;
      });

      // Prevent browser native context menu — we use our own
      document.addEventListener('contextmenu', function (e) {
        e.preventDefault();
      });

      // Prevent middle-click auto-scroll and stray text selection so the
      // app never behaves like a browsable document.  Form fields stay
      // fully editable and selectable.
      function isEditable(el) {
        return !!(el && el.closest && el.closest('input, textarea, [contenteditable]'));
      }
      document.addEventListener('mousedown', function (e) {
        if (e.button === 1 && !isEditable(e.target)) {
          e.preventDefault();
        }
      });
      document.addEventListener('selectstart', function (e) {
        if (!isEditable(e.target)) {
          e.preventDefault();
        }
      });

      // Parse server URL and token from the current page
      var url = window.location.origin;
      var params = new URLSearchParams(window.location.search);
      var token = params.get('token') || '';

      // Initialise the reactive store with server metadata
      store.init(url, token, window.__CLIPSYNC_DEVICE_ID__ || '', window.__CLIPSYNC_DEVICE_NAME__ || '');

      // Restore the tab/search the user was on before a manual refresh, so a
      // hard reload keeps their place instead of bouncing back to Overview.
      try {
        var _prevUi = JSON.parse(sessionStorage.getItem('clipsync_ui_state') || 'null');
        if (_prevUi && _prevUi.activeTab) store.activeTab = _prevUi.activeTab;
        if (_prevUi && _prevUi.historySearch) store.historySearch = _prevUi.historySearch;
        sessionStorage.removeItem('clipsync_ui_state');
      } catch (e) { /* ignore */ }

      // One-time first-run onboarding wizard (desktop-like clients only).
      // The wizard renames THIS computer and explains P2P pairing — neither
      // makes sense for a phone/tablet that landed on the dashboard, where
      // "Name this device" would rename the PC and pairing is impossible.
      if (!store.onboardingDone && store.deviceId &&
          window.matchMedia && window.matchMedia('(pointer: fine)').matches) {
        store.showOnboarding = true;
      }

      // Register the service worker for offline app-shell caching. The server
      // token-protects static files, so the SW URL must carry the token too.
      if ('serviceWorker' in navigator && token) {
        navigator.serviceWorker.register('/sw.js?token=' + encodeURIComponent(token)).catch(function () {
          // SW is optional — never break the app if registration fails.
        });
      }

      // Initialise the API client
      ClipsyncAPI.init(url, token);

      // Connect the WebSocket (convert http(s):// to ws(s):// + /ws path)
      var wsUrl = url.replace(/^http/, 'ws') + '/ws';
      ClipsyncWS.connect(wsUrl, token);

      // Fetch initial data
      this.loadData();

      // 5-second overview refresh — runs whenever the window is focused and
      // page visible, regardless of the active tab, so the always-visible
      // status bar counts never go stale.
      this._overviewTimer = setInterval(function () {
        if (!document.hasFocus()) return;
        if (document.hidden) return;
        store.fetchOverview();
      }, 5000);

      // WebSocket events. Each loader is fire-and-forget, so swallow
      // rejections to avoid unhandled promise rejections on transient
      // network failures.
      //
      // `history_updated` and `devices_updated` are handled inside ws.js,
      // which splices the reactive store from the broadcast payload (the
      // server sends the full list), so no HTTP refetch is needed here.
      // `transfer_progress` is likewise applied to activeTransfers by ws.js,
      // so we only refetch the transfer list on completion (and reconnect,
      // via loadData) to pick up the completed entry's history path.
      //
      // Handlers are stored so they can be removed in beforeUnmount —
      // otherwise a teardown/remount would stack duplicate listeners and each
      // event would fire N times.
      this._wsHandlers = {
        transferComplete: function () {
          self.loadTransfers().catch(function () {});
          store.showToast(self.t('transfer.complete_toast'), 2000);
        },
        pairingRequest: function (data) {
          if (data && data.peer_id) {
            store.pairingRequests.push(data);
            store.showToast(self.t('notify.pairing_request', {
              name: data.peer_name || data.peer_id,
              code: data.code || '',
            }), 3000);
          }
        },
        pairingResolved: function (data) {
          if (!data || !data.peer_id) return;
          store.pairingRequests = store.pairingRequests.filter(function (r) {
            return r.peer_id !== data.peer_id;
          });
        },
        connected: function () {
          // Refresh data on reconnect
          self.loadData();
        },
      };

      ClipsyncWS.on('transfer_complete', this._wsHandlers.transferComplete);
      ClipsyncWS.on('pairing_request', this._wsHandlers.pairingRequest);
      ClipsyncWS.on('pairing_resolved', this._wsHandlers.pairingResolved);
      ClipsyncWS.on('connected', this._wsHandlers.connected);

      // Keyboard shortcuts
      document.addEventListener('keydown', this.onKeyDown);

      // System theme change listener
      if (window.matchMedia) {
        this._themeQuery = window.matchMedia('(prefers-color-scheme: dark)');
        this._onThemeChange = function () {
          if (store.theme === 'system') {
            store.loadTheme();
          }
        };
        this._themeQuery.addEventListener('change', this._onThemeChange);
      }
    },

    beforeUnmount: function () {
      // Unregister WS handlers before closing the socket so a remount never
      // stacks duplicate listeners (each event would otherwise fire N times).
      if (this._wsHandlers) {
        ClipsyncWS.off('transfer_complete', this._wsHandlers.transferComplete);
        ClipsyncWS.off('pairing_request', this._wsHandlers.pairingRequest);
        ClipsyncWS.off('pairing_resolved', this._wsHandlers.pairingResolved);
        ClipsyncWS.off('connected', this._wsHandlers.connected);
        this._wsHandlers = null;
      }
      ClipsyncWS.disconnect();
      document.removeEventListener('keydown', this.onKeyDown);
      if (this._overviewTimer) {
        clearInterval(this._overviewTimer);
      }
      if (this._themeQuery && this._onThemeChange) {
        this._themeQuery.removeEventListener('change', this._onThemeChange);
      }
    },

    methods: {

      /**
       * Fetch all initial data from the server.
       * Each call is wrapped in try/catch so one failure doesn't block others.
       */
      loadData: function () {
        var self = this;
        store.loading = store.initialLoad;

        // Failsafe: force loading off after 2s no matter what
        var failsafeTimer = setTimeout(function () {
          if (store.loading || store.initialLoad) {
            store.loading = false;
            store.initialLoad = false;
          }
        }, 2000);

        try {
          var promises = [
            this.loadDevices(),
            this.loadFavorites(),
            this.loadTransfers(),
          ];

          // History reads `settingsCache.web_history_limit`, so it must wait
          // for settings to resolve before the first fetch — otherwise it
          // always uses the default 30 and the "History items shown" setting
          // looks dead until a manual reload. If settings fail, history still
          // loads (with the default limit).
          promises.push(
            this.loadSettings()
              .catch(function () {
                return null;
              })
              .then(function () {
                return self.loadHistory();
              })
          );

          // Also load overview
          store.fetchOverview();

          Promise.all(promises).catch(function () {
            // Surface the failure instead of leaving an empty UI silently.
            store.showToast(self.t('ui.load_failed'), 3000);
          }).finally(function () {
            clearTimeout(failsafeTimer);
            store.loading = false;
            store.initialLoad = false;
          });
        } catch (e) {
          clearTimeout(failsafeTimer);
          store.loading = false;
          store.initialLoad = false;
        }
      },

      loadHistory: function () {
        // Honour the user's web history limit (default 30) instead of a
        // hardcoded 30 so the setting actually affects the main panel.
        var limit = (store.settingsCache && store.settingsCache.web_history_limit) || 30;
        return ClipsyncAPI.getHistory({ limit: limit, offset: 0 }).then(function (res) {
          store.history.splice(0, store.history.length);
          var items = (res && res.items) ? res.items : [];
          for (var i = 0; i < items.length; i++) {
            store.history.push(items[i]);
          }
          store.historyHasMore = (res && res.total != null) ? (res.offset + items.length < res.total) : false;
          store.historyOffset = items.length;
          return items;
        });
      },

      loadDevices: function () {
        return ClipsyncAPI.getDevices().then(function (res) {
          store.devices.splice(0, store.devices.length);
          var devs = (res && res.devices) ? res.devices : [];
          for (var i = 0; i < devs.length; i++) {
            store.devices.push(devs[i]);
          }
          // Polling fallback for pending pairings (the WS push is dropped when
          // no web client is attached, so the device list fetch re-syncs them).
          if (res && res.pending_pairings) {
            store.syncPairingRequests(res.pending_pairings);
          }
          return devs;
        });
      },

      loadFavorites: function () {
        return ClipsyncAPI.getFavorites().then(function (res) {
          var favs = (res && res.favorites) ? res.favorites : (res && res.items) ? res.items : [];
          store.favorites.splice(0, store.favorites.length);
          for (var i = 0; i < favs.length; i++) {
            store.favorites.push(favs[i]);
          }
          return favs;
        });
      },

      loadSettings: function () {
        return ClipsyncAPI.getSettings().then(function (res) {
          var s = (res && res.settings) || {};
          store.settingsCache = s;
          if (s.ui_backend) store.uiBackend = s.ui_backend;
          if (typeof s.sound_enabled === 'boolean') store.soundEnabled = s.sound_enabled;
          if (typeof s.ui_animation_enabled === 'boolean') store.animationsEnabled = s.ui_animation_enabled;
          // The server is the source of truth for the sound preference — keep
          // the sound module in sync so WS tones obey the saved setting.
          if (typeof ClipsyncSound !== 'undefined' && ClipsyncSound.setEnabled) {
            ClipsyncSound.setEnabled(store.soundEnabled);
          }
          return s;
        });
      },

      loadTransfers: function () {
        return ClipsyncAPI.getTransfers().then(function (res) {
          if (res && res.active) {
            store.activeTransfers.splice(0, store.activeTransfers.length);
            for (var i = 0; i < res.active.length; i++) {
              store.activeTransfers.push(res.active[i]);
            }
          }
          if (res && res.history) {
            store.transferHistory.splice(0, store.transferHistory.length);
            for (var j = 0; j < res.history.length; j++) {
              store.transferHistory.push(res.history[j]);
            }
          }
          return res;
        });
      },

      /**
       * Global keyboard shortcut handler.
       */
      onKeyDown: function (e) {
        var store = window.__CLIPSYNC_STORE__;

        // Escape – clear selection, preview, close context menu
        if (e.key === 'Escape') {
          // Don't steal Escape from a focused form field (history/favorites
          // search boxes, inline editors, dialog inputs) — the focused control
          // owns Escape there (clears its own input / closes its own popup).
          var escTarget = e.target;
          var escEditable = !!escTarget && (
            escTarget.tagName === 'INPUT' || escTarget.tagName === 'TEXTAREA' ||
            !!(escTarget.isContentEditable) ||
            !!(escTarget.closest && escTarget.closest('[contenteditable]'))
          );
          if (escEditable) {
            return;
          }
          // Close a server-pushed dialog if it is dismissible. dialog-modal.js
          // handles the primary case (it stops propagation on Escape); this is
          // the global fallback so a cancellable dialog never stays stuck open.
          if (store.activeDialog) {
            var adType = store.activeDialog.dialog_type;
            if (adType !== 'progress' && adType !== 'confirm') {
              store.closeDialog();
            }
          }
          store.clearSelection();
          store.previewItem = null;
          store.contextMenu.visible = false;
          if (store.settingsPanelVisible) {
            store.closeSettingsPanel();
          }
          return;
        }

        // Ctrl+A – select all visible items (ignore when typing in a field)
        if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
          var tag = (e.target && e.target.tagName) || '';
          var editable = tag === 'INPUT' || tag === 'TEXTAREA' || !!(e.target && e.target.isContentEditable);
          if (!editable && (store.activeTab === 'history' || store.activeTab === 'favorites')) {
            e.preventDefault();
            var isFav = store.activeTab === 'favorites';
            var items = isFav ? store.filteredFavorites() : store.filteredHistory();
            var ids = [];
            for (var i = 0; i < items.length; i++) {
              // Favorites are keyed by `.id`; history items by `.entry_id`.
              ids.push(isFav ? items[i].id : items[i].entry_id);
            }
            store.selectedIds = new Set(ids);
          }
          return;
        }
      },
    },
  });

  // ── Register all components ───────────────────────────────────────

  var components = window.__CLIPSYNC_COMPONENTS__ || {};
  Object.keys(components).forEach(function (name) {
    app.component(name, components[name]);
  });

  // ── Init i18n BEFORE mount so all components see ready translations ──

  if (typeof ClipsyncI18n !== 'undefined' && window.__I18N_JSON__) {
    ClipsyncI18n.init(window.__I18N_JSON__, window.__I18N_LOCALE__);
  }

  // ── Global t() helper — all components share this single function ──

  app.config.globalProperties.t = function (key, fmt) {
    if (typeof ClipsyncI18n !== 'undefined') {
      return ClipsyncI18n.t(key, fmt);
    }
    return key;
  };

  // ── Mount the app ─────────────────────────────────────────────────

  app.mount('#app');

  // ── Expose for debugging ──────────────────────────────────────────

  window.__CLIPSYNC_APP__ = app;

})();
