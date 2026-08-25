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

      // Force the intended opening size once per fresh session.  Chromium has
      // a quirk where --window-size's width applies but the height is
      // overridden by the browser's saved window bounds; resizeTo pins both.
      // The sessionStorage guard means only the first load of a new window
      // resizes — a reload afterwards respects the user's own resize.
      try {
        if (!sessionStorage.getItem('clipsync_size_applied')) {
          window.resizeTo(1152, 648);
          sessionStorage.setItem('clipsync_size_applied', '1');
        }
      } catch (e) { /* ignore — normal tabs / blocked resize */ }

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
        // Allow text selection in form fields and anything explicitly marked
        // .selectable (clip preview text is selectable on purpose — it's a
        // clipboard manager, so dragging out part of a clip must work).
        if (isEditable(e.target)) return;
        if (e.target && e.target.closest && e.target.closest('.selectable')) return;
        e.preventDefault();
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
      // Gate on the local webview host (localhost / 127.0.0.1 — the desktop
      // webview's only address) OR a fine-pointer device, so a touch-primary
      // desktop convertible still gets the wizard while a phone on the LAN IP
      // does not.
      var isLocalHost = /^(localhost|127\.0\.0\.1)$/.test(window.location.hostname || '');
      var isDesktop = isLocalHost || (window.matchMedia && window.matchMedia('(pointer: fine)').matches);
      // A factory reset can't reach browser localStorage — the host's one-shot
      // marker turns into this flag, so wipe stale per-browser UI state (group
      // registry, chat mutes, theme, onboarding) for a true clean slate.
      if (window.__CLIPSYNC_RESET__) {
        try {
          ['clipsync_groups', 'clipsync_chat_mutes', 'clipsync_theme',
           'clipsync_onboarded', 'clipsync_ui_backend', 'clipsync_sound']
            .forEach(function (k) { localStorage.removeItem(k); });
        } catch (e) { /* ignore */ }
      }
      // A factory/config reset clears the desktop language flag but not this
      // browser's clipsync_onboarded localStorage — the two onboarding systems
      // would disagree and the web wizard never re-appears.  When the server
      // says the config is fresh, re-surface the wizard despite the stale flag.
      if (window.__CLIPSYNC_FRESH__) {
        store.onboardingDone = false;
      }
      if (!store.onboardingDone && store.deviceId && isDesktop) {
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

      // Initial data loads are driven by the WebSocket 'connected' event
      // (registered below), which fires once on the first connect and again
      // on every reconnect — so there is no separate loadData() call here,
      // or startup would fetch everything twice.  If the WS never connects
      // (e.g. a LAN proxy blocks the ws:// upgrade), fall back to a one-time
      // HTTP load so the app still renders instead of sitting on the loading
      // spinner forever.
      this._dataLoadTriggered = false;
      this._loadFallbackTimer = setTimeout(function () {
        if (!self._dataLoadTriggered) {
          self._dataLoadTriggered = true;
          self.loadData();
        }
      }, 3000);

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
        transferComplete: function (data) {
          self.loadTransfers().catch(function () {});
          // ws.js already stamps a truthful status from the payload — toast
          // must agree, not celebrate a failed/cancelled transfer.
          var toastKey = 'transfer.complete_toast';
          if (data && data.cancelled) {
            toastKey = 'transfer.cancelled';
          } else if (data && data.success === false) {
            toastKey = 'transfer.failed_toast';
          }
          store.showToast(self.t(toastKey), 2000);
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
          // Refresh data on reconnect.  The initial connect fires this event
          // too, so it is the single data-load trigger — mounted() no longer
          // calls loadData() directly (which would load everything twice at
          // startup).  _dataLoadTriggered only gates the WS-down fallback.
          self._dataLoadTriggered = true;
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
      if (this._loadFallbackTimer) {
        clearTimeout(this._loadFallbackTimer);
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

        // Failsafe: never leave the UI spinning forever, but don't flip to the
        // empty-state panels while the first fetch is still in flight either.
        // Wait up to 8s for the fetches to resolve/reject; if they haven't by
        // then, surface an explicit "still loading / retry" state via
        // store.loadError instead of the misleading empty states.
        var failsafeTimer = setTimeout(function () {
          if (store.loading || store.initialLoad) {
            store.loading = false;
            store.initialLoad = false;
            store.loadError = true;
          }
        }, 8000);

        try {
          var promises = [
            this.loadDevices(),
            this.loadFavorites(),
            this.loadTransfers(),
            this.loadChat(),
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

          Promise.all(promises)
            .then(function () {
              store.loadError = false;
            })
            .catch(function () {
              // A fetch genuinely rejected — surface it instead of leaving an
              // empty UI silently. Only flip to the error state when there is
              // nothing to show yet: a background reconnect refresh failing
              // shouldn't hide already-loaded history/favorites.
              if (store.history.length === 0 && store.favorites.length === 0) {
                store.loadError = true;
              }
              store.showToast(self.t('ui.load_failed'), 3000);
            })
            .finally(function () {
              clearTimeout(failsafeTimer);
              store.loading = false;
              store.initialLoad = false;
            });
        } catch (e) {
          clearTimeout(failsafeTimer);
          store.loading = false;
          store.initialLoad = false;
          store.loadError = true;
        }
      },

      loadHistory: function () {
        // Honour the user's web history limit (default 30) instead of a
        // hardcoded 30 so the setting actually affects the main panel.
        var limit = (store.settingsCache && store.settingsCache.web_history_limit) || 30;
        return ClipsyncAPI.getHistory({ limit: limit, offset: 0 }).then(function (res) {
          var items = (res && res.items) ? res.items : [];
          // This fetches a fresh page-1 snapshot (initial load / WS reconnect).
          // If the user has already loaded beyond the first page (via "Load
          // more"), merge the snapshot into the loaded list instead of
          // clobbering it — otherwise a reconnect collapses their pages.  This
          // mirrors the history_updated upsert/prepend merge in ws.js.
          if (store.history.length > limit) {
            // Upsert/prepend the page-1 snapshot via the shared helper (which
            // also bumps the mutation tick when the merge changes the list, so
            // an in-flight calibration abandons its write-back instead of
            // overwriting the merged state).  Recompute the "load more" cursor
            // from the loaded list length instead of a "+fresh.length" delta:
            // dedupe may have discarded incoming duplicates, so the delta
            // would overshoot the real count and the next fetch would skip
            // entries (mirrors ws.js and mobile.html).
            store.mergeHistoryFresh(items);
            store.setHistoryCursor(res && res.total != null ? res.total : null);
            if (res && res.total != null && store.history.length > res.total) {
              // Missed history_item_deleted broadcasts leave ghost rows in the
              // loaded list, which would inflate the cursor and make Load More
              // skip live items.  History is ordered pinned-DESC, timestamp-DESC,
              // so a deleted row can sit at the TOP (pinned) or in the MIDDLE —
              // trimming the tail would evict LIVE oldest entries and keep the
              // ghost.  Do a full calibration instead: fetch the authoritative
              // list (limit=total returns every remaining item) and replace
              // wholesale, then recompute the cursor from the real length.
              // Shared with ws.js via store.calibrateHistory() (throttled +
              // race guarded — see store.js).
              return store.calibrateHistory(res.total);
            }
            if (res && res.total != null) {
              store.setHistoryCursor(res.total);
            }
          } else {
            // Route through the shared helper: it filters malformed null rows,
            // rebuilds the list, and bumps the reconcile guard only when the
            // snapshot actually changed (explicit field compare — so an
            // identical reconnect doesn't spuriously invalidate an in-flight
            // calibration, while a real change still does).  Cursor aligns via
            // the shared helper too (visible length — the invariant every
            // cursor-shrink path depends on).
            store.replaceHistory(items);
            store.setHistoryCursor(res && res.total != null ? res.total : null);
          }
          return items;
        });
      },

      loadDevices: function () {
        return ClipsyncAPI.getDevices().then(function (res) {
          store.devicesLoadFailed = false;
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
        }).catch(function (e) {
          store.devicesLoadFailed = true;
          throw e;
        });
      },

      loadFavorites: function () {
        return ClipsyncAPI.getFavorites().then(function (res) {
          var favs = (res && res.favorites) ? res.favorites : (res && res.items) ? res.items : [];
          store.favorites.splice(0, store.favorites.length);
          for (var i = 0; i < favs.length; i++) {
            store.favorites.push(favs[i]);
          }
          // A reset/deleted data folder leaves zero favorites but a stale
          // clipsync_groups localStorage registry (groups are per-browser).
          // Clear the registry so ghost groups can't resurrect in the sidebar.
          if (favs.length === 0 && store.groupNames.length > 0) {
            store.groupNames = [];
            store.persistGroups();
          }
          return favs;
        });
      },

      loadSettings: function () {
        return ClipsyncAPI.getSettings().then(function (res) {
          var s = (res && res.settings) || {};
          store.settingsCache = s;
          // Seed the internet-sync relay state (kept live afterwards by the
          // WS `relay_state` event handled in ws.js).
          if (s.internet_sync_state) store.relayState = s.internet_sync_state;
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
       * Load nearby-chat sessions (and the open conversation's messages) so
       * the chat tab badge and session list are fresh on startup/reconnect.
       * Catches internally — chat is never a blocker for the rest of the app.
       * @returns {Promise<void>}
       */
      loadChat: function () {
        var self = this;
        return ClipsyncAPI.chatSessions().then(function (res) {
          if (res && res.sessions) {
            store.replaceChatSessions(res.sessions);
          }
          // If a conversation is already open, refresh its messages too so a
          // reconnect doesn't leave the chat pane on stale entries.
          if (store.activeChatSession) {
            return ClipsyncAPI.chatMessages(store.activeChatSession).then(function (mres) {
              if (mres && mres.messages) {
                store.replaceChatMessages(mres.messages);
              }
            }).catch(function (e) {
              // Keep the existing messages on a transient failure.
              console.error('[ClipSync] Failed to refresh chat messages:', e);
            });
          }
        }).catch(function (e) {
          console.error('[ClipSync] Failed to load chat sessions:', e);
        });
      },

      /**
       * Global keyboard shortcut handler.
       */
      onKeyDown: function (e) {
        var store = window.__CLIPSYNC_STORE__;

        // Escape – clear selection, preview, close context menu
        if (e.key === 'Escape') {
          // A client-side confirm/prompt dialog owns Escape while it is up —
          // don't clear selection / close panels underneath it too.
          if (store.clientDialog) {
            return;
          }
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
          // NOTE: the settings panel handles its own Escape (via a listener it
          // installs while open) so closing with unsaved staged edits can
          // prompt to confirm discarding them.
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

        // ↑/↓/Enter/Del — keyboard navigation over the visible history list.
        // Only on the history tab, and only when no modal/menu owns the keys
        // and no editable field is focused.
        // Enter/Del are further skipped when focus sits on an interactive
        // control (a history card handles its own Enter; a focused button
        // owns both) so actions never fire twice.
        var navTag = (e.target && e.target.tagName) || '';
        var navEditable = navTag === 'INPUT' || navTag === 'TEXTAREA' ||
          !!(e.target && e.target.isContentEditable);
        var navBlocked = navEditable ||
          !!store.clientDialog || !!store.activeDialog ||
          !!(store.contextMenu && store.contextMenu.visible) ||
          !!store.settingsPanelVisible ||
          !!(store.translateModal && store.translateModal.visible) ||
          !!store.showOnboarding;
        var navKey = e.key === 'ArrowDown' || e.key === 'ArrowUp';
        var actKey = e.key === 'Enter' || e.key === 'Delete' || e.key === 'Backspace';
        var actOnControl = !!(e.target && e.target.closest &&
          e.target.closest('button, a, select, [role="button"]'));
        if ((navKey || (actKey && !actOnControl)) && !navBlocked &&
            !e.ctrlKey && !e.metaKey && !e.altKey &&
            store.activeTab === 'history') {
          var kItems = store.filteredHistory();
          var kLen = kItems.length;
          if (kLen === 0) return;
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            store.kbdIndex = store.kbdIndex < 0 ? 0 : Math.min(store.kbdIndex + 1, kLen - 1);
            this._scrollKbdItem();
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            store.kbdIndex = store.kbdIndex < 0 ? 0 : Math.max(store.kbdIndex - 1, 0);
            this._scrollKbdItem();
          } else if (e.key === 'Enter') {
            if (store.kbdIndex >= 0 && store.kbdIndex < kLen) {
              e.preventDefault();
              this._copyKbdItem(kItems[store.kbdIndex]);
            }
          } else if (e.key === 'Delete' || e.key === 'Backspace') {
            if (store.kbdIndex >= 0 && store.kbdIndex < kLen) {
              e.preventDefault();
              this._deleteKbdItem(kItems[store.kbdIndex]);
            }
          }
        }
      },

      /**
       * Keep the keyboard-focused history card in view after ↑/↓ moves it.
       */
      _scrollKbdItem: function () {
        this.$nextTick(function () {
          var el = document.querySelector('.history-item--kbd');
          if (el && el.scrollIntoView) {
            el.scrollIntoView({ block: 'nearest' });
          }
        });
      },

      /**
       * Copy the keyboard-focused item to the desktop clipboard via
       * paste-rich (so IMAGE entries work too). Mirrors history-item's
       * copy action, including the paste-count bump and toasts.
       * @param {Object} item
       */
      _copyKbdItem: function (item) {
        var self = this;
        if (!item || item.entry_id === undefined || item.entry_id === null) return;
        var eid = item.entry_id;
        ClipsyncAPI.pasteRich(eid).then(function (res) {
          if (res && res.ok !== false) {
            var idx = store.history.findIndex(function (h) {
              return h.entry_id === eid;
            });
            if (idx !== -1) {
              store.history[idx].paste_count = (store.history[idx].paste_count || 0) + 1;
            }
            store.showToast(self.t('history.copied'), 1500);
          } else {
            store.showToast(self.t('history.copy_failed'), 2000);
          }
        }).catch(function () {
          store.showToast(self.t('history.copy_failed'), 2000);
        });
      },

      /**
       * Delete the keyboard-focused item. Mirrors history-item's delete:
       * shared removal helper + pagination-cursor shrink, then keep the
       * cursor clamped to the shorter list so continued Del presses walk
       * down without skipping an entry.
       * @param {Object} item
       */
      _deleteKbdItem: function (item) {
        var self = this;
        if (!item || item.entry_id === undefined || item.entry_id === null) return;
        var eid = item.entry_id;
        ClipsyncAPI.deleteItem(eid).then(function (res) {
          if (res && res.ok !== false) {
            var removed = store.removeHistoryItems([eid]);
            if (removed > 0) {
              store.historyOffset = Math.max(0, store.historyOffset - 1);
            }
            var len = store.filteredHistory().length;
            if (len > 0 && store.kbdIndex > len - 1) {
              store.kbdIndex = len - 1;
            }
            store.showToast(self.t('history.deleted_toast'), 1200);
          }
        }).catch(function () {
          store.showToast(self.t('history.delete_failed'), 2000);
        });
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
