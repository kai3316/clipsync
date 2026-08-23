/* ═══════════════════════════════════════════════════════════════════
   ClipSync Reactive Store (Vue 3)
   Central reactive state shared across all Vue components.

   Dependencies:
     Vue 3 CDN (loaded before this script):
       <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>

   Usage:
     const { reactive, computed } = Vue;
     // Store is pre-created below and accessible globally via
     // window.__CLIPSYNC_STORE__
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // Guard against double-load
  if (window.__CLIPSYNC_STORE__) return;

  var reactive = Vue.reactive;
  var computed = Vue.computed;

  // Minimum gap between full-history calibrations (see store.calibrateHistory).
  // A calibration refetches the ENTIRE history at limit=total, so it is
  // throttled to at most one per client per window.  EVERY terminal state
  // (success write-back, raced abandon, failure, timeout) consumes the budget —
  // a constantly-mutating history would otherwise raced-abandon every attempt
  // without consuming it, triggering a full download on every broadcast with
  // no backoff.  Unified consumption caps that at one attempt per window, and
  // ghosts heal in the first 30s silent window.
  var CALIBRATION_THROTTLE_MS = 30000;
  // Safety valve for the _historyCalibrating lock: an old webview without
  // AbortController can leave the calibration fetch pending forever, which
  // would otherwise wedge the lock permanently.  After this window the
  // calibration unwedges the lock and consumes the budget so the next poll can
  // retry — but it does NOT advance the generation: a slow-but-valid response
  // that settles later still passes the gen guard and writes back (staleness
  // vs. newer data is the mutation tick's job, not the timer's).
  var CALIBRATION_TIMEOUT_MS = 16000;

  // Local i18n helper — the store is a plain object (not a Vue component),
  // so it reaches the global translator directly. Falls back to the key when
  // i18n hasn't been initialised yet.
  var t = function (key, fmt) {
    if (typeof ClipsyncI18n !== 'undefined') {
      return ClipsyncI18n.t(key, fmt);
    }
    return key;
  };

  var store = reactive({

    /* ═══════════════════════════════════════════════════════════════
       Devices
       ═══════════════════════════════════════════════════════════════ */
    devices: [],
    connectedCount: computed(function () {
      // Exclude the local device — "connected" counts remote peers only,
      // matching the backend's get_connected_peers() semantics.
      return store.devices.filter(function (d) {
        return d.connected && d.device_id !== store.deviceId;
      }).length;
    }),

    // True when the app's configured language is Chinese. Used for the few
    // inline bilingual strings that have no locale entry (e.g. wizard "Next").
    // Follows the server-injected locale (like the phone pages do), not the
    // browser's — otherwise an English app on a Chinese system mixes
    // languages mid-sentence.
    isZh: computed(function () {
      var locale = String(window.__I18N_LOCALE__ || '').toLowerCase();
      if (!locale) {
        locale = (navigator.language || '').toLowerCase();
      }
      return locale.indexOf('zh') === 0;
    }),

    // The phone-connect URL shown on onboarding step 3:
    // http://<lan-ip>:<web-port>/mobile.html?token=<token>
    mobileUrl: computed(function () {
      var ip = store.overview.localIp || (store.settingsCache && store.settingsCache.web_ip) || '';
      var port = store.overview.port || (store.settingsCache && store.settingsCache.web_port) || '';
      if (!ip || !port) return '';
      return 'http://' + ip + ':' + port + '/mobile.html?token=' + encodeURIComponent(store.token);
    }),

    /* ═══════════════════════════════════════════════════════════════
       History
       ═══════════════════════════════════════════════════════════════ */
    history: [],
    historyFilter: 'all',      // 'all' | 'text' | 'image' | 'file' | 'link'
    historySearch: '',
    historySort: 'newest',     // 'newest' | 'oldest'
    historyHasMore: false,     // true when server has more items to load
    historyOffset: 0,          // current pagination offset
    // Monotonic counter bumped on every history delete / clear (see ws.js).
    // A calibration in flight records its value at start and abandons its
    // write-back if it changed — otherwise a stale snapshot would resurrect
    // rows deleted while the fetch was in flight.
    historyMutationTick: 0,

    /* ═══════════════════════════════════════════════════════════════
       Favorites
       ═══════════════════════════════════════════════════════════════ */
    favorites: [],
    activeGroup: '',            // empty string = all groups
    favoriteSearch: '',        // search query for favorites
    groupNames: [],            // known group names (incl. empty ones), persisted

    /* ═══════════════════════════════════════════════════════════════
       Transfers
       ═══════════════════════════════════════════════════════════════ */
    activeTransfers: [],
    transferHistory: [],

    /* ═══════════════════════════════════════════════════════════════
       Nearby Chat
       ═══════════════════════════════════════════════════════════════ */
    chatSessions: [],          // sessions from /api/chat/sessions + WS pushes
    chatMessages: [],          // entries for the active session (capped at 200)
    activeChatSession: '',     // selected session_id (empty = none selected)
    chatUnread: 0,             // total unread across sessions (sidebar badge)
    mutedChatPeers: new Set(), // peer_ids muted in the chat UI (no unread badge)

    /* ═══════════════════════════════════════════════════════════════
       Overview stats (refreshed every 5s)
       ═══════════════════════════════════════════════════════════════ */
    overview: {
      connectedCount: 0,
      pairedCount: 0,
      historyCount: 0,
      activeTransferCount: 0,
      discovering: false,
      visible: false,
      syncEnabled: false,
      webEnabled: false,
      uptimeSeconds: 0,
      localIp: '',
      port: 0,
      platform: '',
      networkType: '',       // 'wifi' | 'ethernet' | 'lan'
      networkDetail: '',     // SSID or link speed
      recentActivity: '',    // "12 clips · 3 transfers"
      loading: false,
    },

    /* ═══════════════════════════════════════════════════════════════
       Pairing requests
       ═══════════════════════════════════════════════════════════════ */
    pairingRequests: [],

    /* ═══════════════════════════════════════════════════════════════
       Speed test
       ═══════════════════════════════════════════════════════════════ */
    speedTest: {
      running: false,
      resultMbps: null,
      quality: '',           // 'fast' | 'good' | 'slow'
      progress: 0,
      status: '',            // 'Sending chunk X/Y'
      error: '',
    },

    /* ═══════════════════════════════════════════════════════════════
       UI state
       ═══════════════════════════════════════════════════════════════ */
    activeTab: 'overview',      // 'overview' | 'history' | 'devices' | 'transfers' | 'favorites' | 'diagnostics'
    theme: 'system',            // 'system' | 'light' | 'dark'
    sidebarOpen: true,
    selectedIds: new Set(),     // multi-select set of entry IDs
    loading: false,
    initialLoad: true,          // true until first data fetch completes
    loadError: false,           // true when the initial load fails or times out
    devicesLoadFailed: false,   // true when the device-list fetch rejected
    toastMessage: '',
    toastVisible: false,
    toastType: 'info',          // 'info' | 'success' | 'warning' | 'error'
    previewItem: null,          // hover preview target (item object or null)
    previewPosition: { x: 0, y: 0 },  // mouse position for preview popover
    contextMenu: {
      visible: false,
      x: 0, y: 0,
      mode: 'history-item',  // 'history-item' | 'device'
      target: null           // the item or device object
    },

    /* ═══════════════════════════════════════════════════════════════
       Translate Modal
       ═══════════════════════════════════════════════════════════════ */
    translateModal: {
      visible: false,
      text: '',
      sourceLang: 'auto',
      targetLang: 'zh',
      translated: '',
      translating: false,
    },

    /* ═══════════════════════════════════════════════════════════════
       Settings Panel
       ═══════════════════════════════════════════════════════════════ */
    settingsPanelVisible: false,
    settingsRequestedSection: '',   // open settings on this section when set
    soundEnabled: true,
    animationsEnabled: true,
    uiBackend: 'webview',       // 'webview' | 'ctk'
    settingsCache: {},           // cached settings from server

    /* ═══════════════════════════════════════════════════════════════
       First-run onboarding wizard
       ═══════════════════════════════════════════════════════════════ */
    onboardingDone: false,      // true once the wizard was finished/skipped
    showOnboarding: false,      // true while the wizard overlay is visible
    onboardingStep: 1,          // 1 = welcome · 2 = pair a device · 3 = phone access
    onboardingError: '',        // inline error shown on the wizard (e.g. empty name)
    onboardingSaving: false,    // true while the device-name save is in flight

    /* ═══════════════════════════════════════════════════════════════
       Dialog system (server-pushed modals)
       ═══════════════════════════════════════════════════════════════ */
    activeDialog: null,         // { dialog_id, dialog_type, title, message, ... }
    dialogQueue: [],            // server-pushed dialogs waiting for the active one to close

    /* ═══════════════════════════════════════════════════════════════
       Client-side dialogs (confirm / prompt / alert)
       ═══════════════════════════════════════════════════════════════ */
    clientDialog: null,  // { type, title, message, defaultValue, resolve, reject }

    /* ═══════════════════════════════════════════════════════════════
       Server info (set by init)
       ═══════════════════════════════════════════════════════════════ */
    serverUrl: '',
    token: '',
    deviceId: '',
    deviceName: '',

    /* ═══════════════════════════════════════════════════════════════
       Methods
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Initialise the store with server connection info.
     * Call once at app startup.
     */
    init: function (url, token, deviceId, deviceName) {
      this.serverUrl = url;
      this.token = token;
      this.deviceId = deviceId;
      this.deviceName = deviceName;
      this.loadTheme();
      this.loadOnboarding();
      this.loadGroups();
      this.loadChatMutes();
    },

    /**
     * Load theme from localStorage or OS preference and apply it.
     */
    loadTheme: function () {
      var saved = null;
      try { saved = localStorage.getItem('clipsync_theme'); } catch (e) { /* ignore */ }

      // Only an explicit user choice is persisted. The default ("system")
      // must follow the OS preference without being written back to storage,
      // otherwise a fresh install would hardcode the OS's current theme and
      // stop tracking later OS light/dark changes.
      if (saved === 'dark' || saved === 'light') {
        this.setTheme(saved);
      } else {
        this.setTheme('system');
      }
    },

    /**
     * Set and persist the theme.
     * @param {'light'|'dark'|'system'} t
     */
    setTheme: function (t) {
      this.theme = t;

      var isDark;
      if (t === 'system') {
        isDark = window.matchMedia &&
          window.matchMedia('(prefers-color-scheme: dark)').matches;
      } else {
        isDark = t === 'dark';
      }

      var root = document.documentElement;
      if (isDark) {
        root.classList.add('dark');
        root.setAttribute('data-theme', 'dark');
      } else {
        root.classList.remove('dark');
        root.setAttribute('data-theme', 'light');
      }

      // Persist preference (only for explicit light/dark; system is the default)
      try {
        if (t === 'system') {
          localStorage.removeItem('clipsync_theme');
        } else {
          localStorage.setItem('clipsync_theme', t);
        }
      } catch (e) { /* ignore */ }
    },

    /* ═══════════════════════════════════════════════════════════════
       First-run onboarding helpers
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Load the first-run onboarding flag from localStorage.
     * "clipsync_onboarded" = "1" means the wizard was already completed/skipped.
     */
    loadOnboarding: function () {
      var saved = null;
      try { saved = localStorage.getItem('clipsync_onboarded'); } catch (e) { /* ignore */ }
      this.onboardingDone = saved === '1' || saved === 'true';
    },

    /**
     * Mark onboarding as done, persist it, and hide the wizard overlay.
     */
    completeOnboarding: function () {
      this.onboardingDone = true;
      this.showOnboarding = false;
      this.onboardingStep = 1;
      this.onboardingError = '';
      this.onboardingSaving = false;
      try { localStorage.setItem('clipsync_onboarded', '1'); } catch (e) { /* ignore */ }
    },

    /**
     * Advance to the next onboarding step (1 → 2 → 3).
     * Entering step 3 ensures the LAN IP / web port are loaded so the
     * phone-connect URL and QR button have real data to work with.
     */
    nextOnboardingStep: function () {
      this.onboardingError = '';
      if (this.onboardingStep < 3) {
        this.onboardingStep += 1;
      }
      if (this.onboardingStep === 3) {
        this.ensureOverview();
      }
    },

    /**
     * Step 1 "Start": persist the editable device name, then advance.
     * An empty name is rejected inline (backend has no validation), and a
     * failed save surfaces a toast instead of silently advancing.
     */
    startOnboarding: function () {
      var self = this;
      var name = (this.deviceName || '').trim();
      this.deviceName = name;
      if (!name) {
        this.onboardingError = this.isZh ? '请输入设备名称' : 'Please enter a device name';
        return;
      }
      this.onboardingError = '';
      this.onboardingSaving = true;
      this.saveDeviceName().then(function () {
        self.onboardingSaving = false;
        self.nextOnboardingStep();
      }).catch(function () {
        self.onboardingSaving = false;
        self.showToast(self.isZh ? '保存设备名称失败' : 'Failed to save device name', 2500, 'error');
      });
    },

    /**
     * Step 2 "Go to Devices": switch to the devices tab and close the wizard.
     */
    goToDevices: function () {
      this.activeTab = 'devices';
      this.completeOnboarding();
    },

    /**
     * Persist the device name typed on the welcome screen to the server.
     * Returns a Promise so callers can await the save and surface failures.
     * An empty name is a no-op — never persist a blank device name.
     */
    saveDeviceName: function () {
      var name = (this.deviceName || '').trim();
      this.deviceName = name;
      if (!name) {
        return Promise.resolve();
      }
      if (window.ClipsyncAPI && window.ClipsyncAPI.updateSettings) {
        return window.ClipsyncAPI.updateSettings({ device_name: name }).catch(function (e) {
          console.error('[ClipSync] Failed to save device name:', e);
          throw e;
        });
      }
      return Promise.resolve();
    },

    /**
     * Make sure the LAN IP / web port are loaded so the phone-connect URL and
     * QR button have data. Uses the overview endpoint — the same source the
     * settings panel reads for its "LAN address" row.
     * @returns {Promise<void>}
     */
    ensureOverview: function () {
      var self = this;
      if (this.overview.localIp && this.overview.port) {
        return Promise.resolve();
      }
      if (window.ClipsyncAPI && window.ClipsyncAPI.getOverview) {
        return window.ClipsyncAPI.getOverview().then(function (res) {
          if (res && res.overview) {
            self.overview.localIp = res.overview.local_ip || self.overview.localIp;
            self.overview.port = res.overview.port || self.overview.port;
          }
        }).catch(function () { /* ignore — URL stays empty until overview loads */ });
      }
      return Promise.resolve();
    },

    /**
     * Step 3 "Show QR Code": ask the server to push the phone-connect QR dialog.
     */
    showPhoneQr: function () {
      var self = this;
      this.ensureOverview().then(function () {
        if (window.ClipsyncAPI && window.ClipsyncAPI._fetch) {
          window.ClipsyncAPI._fetch('POST', '/api/show_qr', {}).catch(function (e) {
            console.error('[ClipSync] Failed to show phone QR:', e);
            self.showToast(self.isZh ? '显示二维码失败' : 'Failed to show QR code', 2500, 'error');
          });
        }
      });
    },

    /**
     * Show a toast notification.
     * @param {string} msg - Message to display
     * @param {number} [duration=2000] - Duration in ms
     */
    showToast: function (msg, duration, type) {
      // Default display time long enough to read a full notification; many
      // call sites pass shorter explicit values (e.g. 1500–3000ms).
      if (duration === undefined) duration = 3500;
      if (type === undefined) type = 'info';
      // Clear any existing timer
      if (this._toastTimer) {
        clearTimeout(this._toastTimer);
        this._toastTimer = null;
      }
      this.toastMessage = msg;
      this.toastType = type;
      this.toastVisible = true;
      var self = this;
      this._toastTimer = setTimeout(function () {
        self.toastVisible = false;
        self._toastTimer = null;
      }, duration);
    },

    /**
     * Replace the pairing-request list from the backend's authoritative
     * `pending_pairings` snapshot (polled via /api/devices). This gives the
     * device page a polling fallback so a request that arrived while the
     * dashboard was closed still shows up, instead of relying on the WS push
     * alone (which is dropped when no client is attached).
     */
    syncPairingRequests: function (pending) {
      var list = (pending || []).filter(function (p) {
        return !!p.peer_id;
      });
      this.pairingRequests = list;
    },

    /**
     * Open the translate modal with selected text.
     * @param {string} text - Source text to translate
     */
    openTranslateModal: function (text) {
      this.translateModal.visible = true;
      this.translateModal.text = text || '';
      this.translateModal.sourceLang = 'auto';
      this.translateModal.targetLang = 'zh';
      this.translateModal.translated = '';
      this.translateModal.translating = false;
    },

    /**
     * Close the translate modal.
     */
    closeTranslateModal: function () {
      this.translateModal.visible = false;
      this.translateModal.translating = false;
    },

    /**
     * Open the settings side panel.
     */
    openSettingsPanel: function () {
      this.settingsPanelVisible = true;
    },

    /**
     * Close the settings side panel.
     */
    closeSettingsPanel: function () {
      this.settingsPanelVisible = false;
    },

    /**
     * Show a server-pushed dialog modal.
     * Called by the WebSocket handler when a show_dialog message arrives.
     */
    showDialog: function (dlg) {
      // A server-pushed dialog takes precedence over any client confirm/
      // prompt/alert — reject the pending client dialog so its promise
      // doesn't hang forever under a hidden overlay (mirrors how confirm()/
      // prompt() close prior client dialogs).
      this.closeClientDialog();
      // Only one server dialog can be visible at a time. If one is already
      // up, queue the newcomer so it is shown after the current one closes
      // instead of silently clobbering it — a clobbered dialog would sit in
      // dialog.py's event.wait(120) with no on-screen UI and time out.
      if (this.activeDialog) {
        this.dialogQueue.push(dlg);
        return;
      }
      this.activeDialog = dlg;
    },

    /**
     * Close the active dialog modal and promote the next queued dialog.
     * @param {string} [dialogId] - Optional id. When given and it is NOT the
     *   active dialog, the server force-closed a queued dialog — that entry is
     *   removed from the queue and the visible dialog stays up.
     */
    closeDialog: function (dialogId) {
      // A specific dialog was requested and it isn't the one on screen —
      // the server force-closed a queued dialog. Drop it and keep the
      // visible one up.
      if (dialogId !== undefined && dialogId !== null &&
          this.activeDialog && this.activeDialog.dialog_id !== dialogId) {
        this._removeQueuedDialog(dialogId);
        return;
      }
      this.activeDialog = null;
      // Promote the next queued dialog (if any) so it becomes visible.
      if (this.dialogQueue.length > 0) {
        this.activeDialog = this.dialogQueue.shift();
      }
    },

    /**
     * Remove a queued (not-yet-shown) dialog by id. No-op when absent.
     * @param {string} dialogId
     */
    _removeQueuedDialog: function (dialogId) {
      if (dialogId === undefined || dialogId === null) return;
      for (var i = 0; i < this.dialogQueue.length; i++) {
        if (this.dialogQueue[i] && this.dialogQueue[i].dialog_id === dialogId) {
          this.dialogQueue.splice(i, 1);
          return;
        }
      }
    },

    /**
     * Show a client-side confirm dialog. Returns a Promise.
     * @param {string} title
     * @param {string} message
     * @returns {Promise<void>} — resolves on confirm, rejects on cancel
     */
    confirm: function (title, message) {
      var self = this;
      self.closeClientDialog();  // reject any pending dialog first
      return new Promise(function (resolve, reject) {
        self.clientDialog = { type: 'confirm', title: title, message: message, resolve: resolve, reject: reject };
      });
    },

    /**
     * Show a client-side prompt dialog. Returns a Promise.
     * @param {string} title
     * @param {string} message
     * @param {string} [defaultValue='']
     * @returns {Promise<string>} — resolves with input value, rejects on cancel
     */
    prompt: function (title, message, defaultValue) {
      var self = this;
      self.closeClientDialog();  // reject any pending dialog first
      return new Promise(function (resolve, reject) {
        self.clientDialog = { type: 'prompt', title: title, message: message, defaultValue: defaultValue || '', resolve: resolve, reject: reject };
      });
    },

    /**
     * Show a client-side alert dialog. Returns a Promise.
     * @param {string} title
     * @param {string} message
     * @returns {Promise<void>}
     */
    alert: function (title, message) {
      var self = this;
      self.closeClientDialog();  // reject any pending dialog first
      return new Promise(function (resolve) {
        self.clientDialog = { type: 'alert', title: title, message: message, resolve: resolve };
      });
    },

    /**
     * Close the client-side dialog.
     */
    closeClientDialog: function () {
      if (this.clientDialog && this.clientDialog.reject) {
        this.clientDialog.reject();
      }
      this.clientDialog = null;
    },

    /**
     * Merge a settings-update response into the local settings cache so the
     * settings panel shows freshly-saved values without a full reload.
     * @param {Object} updated - The `updated` map from POST /api/settings
     */
    mergeSettings: function (updated) {
      if (!updated) return;
      var cache = Object.assign({}, this.settingsCache, updated);
      this.settingsCache = cache;
      if (typeof updated.ui_backend === 'string') this.uiBackend = updated.ui_backend;
      if (typeof updated.sound_enabled === 'boolean') this.soundEnabled = updated.sound_enabled;
      if (typeof updated.ui_animation_enabled === 'boolean') this.animationsEnabled = updated.ui_animation_enabled;
    },

    /**
     * Switch between modern (webview) and classic (CTk) UI.
     * Saves to server and stores locally. Requires app restart.
     * @param {'webview'|'ctk'} backend
     */
    setUIBackend: function (backend) {
      this.uiBackend = backend;
      // Persist to server
      if (window.ClipsyncAPI && window.ClipsyncAPI.updateSettings) {
        window.ClipsyncAPI.updateSettings({ ui_backend: backend }).catch(function () {
          // ignore — will sync on restart
        });
      }
      // Persist locally
      try { localStorage.setItem('clipsync_ui_backend', backend); } catch (e) { /* ignore */ }
    },

    /**
     * Fetch overview stats from the server.
     * In-flight calls are coalesced: the 5s polling timer (app.js) and the
     * WS-event debounce (ws.js) can both fire a fetch in the same tick, and a
     * slow /api/overview should never stack concurrent requests.  The in-flight
     * response is fresh enough to satisfy both callers, so a request that
     * arrives while one is already pending is a no-op.
     */
    fetchOverview: function () {
      var self = this;
      if (this._overviewInFlight) return;
      this._overviewInFlight = true;
      this.overview.loading = true;
      if (!window.ClipsyncAPI) {
        this._overviewInFlight = false;
        return;
      }
      window.ClipsyncAPI.getOverview()
        .then(function (res) {
          if (res && res.overview) {
            var o = res.overview;
            self.overview.connectedCount = o.connected_count || 0;
            self.overview.pairedCount = o.paired_count || 0;
            self.overview.historyCount = o.history_count || 0;
            self.overview.activeTransferCount = o.active_transfers || 0;
            self.overview.discovering = o.discovering || false;
            self.overview.visible = o.visible || false;
            self.overview.syncEnabled = o.sync_enabled || false;
            self.overview.webEnabled = o.web_enabled || false;
            self.overview.uptimeSeconds = o.uptime_seconds || 0;
            self.overview.localIp = o.local_ip || '';
            self.overview.port = o.port || 0;
            self.overview.platform = o.platform || '';
            self.overview.networkType = o.network_type || '';
            self.overview.networkDetail = o.network_detail || '';
            self.overview.recentActivity = o.recent_activity || '';
            // Newer overview fields (rich stats + live activity feed).
            self.overview.connectedNames = o.connected_names || [];
            self.overview.discoveredCount = o.discovered_count || 0;
            self.overview.historyToday = o.history_today || 0;
            self.overview.historyPinned = o.history_pinned || 0;
            self.overview.historyImages = o.history_images || 0;
            self.overview.transferCompleted = o.transfer_completed || 0;
            self.overview.transferBytes = o.transfer_bytes || 0;
            self.overview.version = o.version || '';
            self.overview.recentItems = o.recent_items || [];
          }
        })
        .catch(function (e) {
          console.error('[ClipSync] Failed to fetch overview:', e);
        })
        .finally(function () {
          self._overviewInFlight = false;
          self.overview.loading = false;
        });
    },

    /**
     * Start a speed test.
     */
    startSpeedTest: function () {
      var self = this;
      this.speedTest.resultMbps = null;
      this.speedTest.quality = '';
      this.speedTest.progress = 0;
      this.speedTest.status = t('transfer.speed_test.starting');
      this.speedTest.error = '';
      if (!window.ClipsyncAPI || !window.ClipsyncAPI.startSpeedTest) {
        // No API client — don't leave the spinner running forever.
        this.speedTest.running = false;
        this.speedTest.error = t('transfer.speed_test.unavailable');
        return;
      }
      this.speedTest.running = true;
      window.ClipsyncAPI.startSpeedTest()
        .then(function (res) {
          if (res && res.ok) {
            self._pollSpeedTest();
          } else {
            self.speedTest.running = false;
            // The backend refuses to run without a connected peer — surface a
            // helpful message instead of a generic "failed to start".
            var noPeer = !(self.devices || []).some(function (d) { return d.connected; });
            self.speedTest.error = noPeer ? t('transfer.speed_test.no_peer')
                                          : t('transfer.speed_test.start_failed');
          }
        })
        .catch(function (e) {
          self.speedTest.running = false;
          self.speedTest.error = t('transfer.speed_test.unavailable');
        });
    },

    _pollSpeedTest: function () {
      var self = this;
      if (!this.speedTest.running) return;
      window.ClipsyncAPI.getSpeedTestResult()
        .then(function (res) {
          if (!res) {
            // Empty/transient response — treat it as a failure so the
            // spinner doesn't run forever.
            self.speedTest.running = false;
            self.speedTest.error = t('transfer.speed_test.failed');
            return;
          }
          if (res.done) {
            self.speedTest.running = false;
            self.speedTest.resultMbps = res.mbps;
            self.speedTest.quality = res.quality || '';
            self.speedTest.progress = 1;
            self.speedTest.status = '';
          } else {
            self.speedTest.progress = res.progress || 0;
            self.speedTest.status = res.status || '';
            setTimeout(function () { self._pollSpeedTest(); }, 500);
          }
        })
        .catch(function () {
          self.speedTest.running = false;
          self.speedTest.error = t('transfer.speed_test.failed');
        });
    },

    /**
     * Format uptime seconds to human-readable string.
     */
    formatUptime: function (seconds) {
      if (!seconds || seconds < 0) return '--';
      var d = Math.floor(seconds / 86400);
      var h = Math.floor((seconds % 86400) / 3600);
      var m = Math.floor((seconds % 3600) / 60);
      if (d > 0) return d + 'd ' + h + 'h';
      if (h > 0) return h + 'h ' + m + 'm';
      if (m > 0) return m + 'm';
      return (seconds % 60) + 's';
    },

    /* ═══════════════════════════════════════════════════════════════
       History helpers
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Check whether a text string was redacted by the sensitive-content
     * filter. The backend replaces sensitive content with the literal marker
     * "[FILTERED]" (FILTERED_MARKER); history/favorites items whose text
     * contains it should surface a small explanatory note to the receiver.
     * @param {string} text - The item text (text_preview, content, ...)
     * @returns {boolean}
     */
    isFilteredText: function (text) {
      return typeof text === 'string' && text.indexOf('[FILTERED]') !== -1;
    },

    /**
     * Get filtered + searched + sorted history list.
     * Returns a reactive computed-compatible plain array.
     * Usage in a component: call store.filteredHistory() in a computed.
     * @returns {Array}
     */
    filteredHistory: function () {
      var items = this.history.slice();
      var filter = this.historyFilter;
      var search = (this.historySearch || '').toLowerCase().trim();

      // Filter by type
      if (filter !== 'all') {
        items = items.filter(function (item) {
          var ct = (item.content_type || '').toUpperCase();
          switch (filter) {
            case 'text':  return ct === 'TEXT' || ct === 'HTML' || ct === 'RTF';
            case 'image': return ct === 'IMAGE' || ct === 'IMAGE_EMF';
            case 'file':  return ct === 'FILE';
            case 'link':  return /^https?:\/\//i.test(item.text_preview || '');
            default:      return true;
          }
        });
      }

      // Filter by search
      if (search) {
        items = items.filter(function (item) {
          var preview = (item.text_preview || '').toLowerCase();
          var source = (item.source_name || '').toLowerCase();
          var type = (item.content_type || '').toLowerCase();
          return preview.indexOf(search) !== -1 ||
                 source.indexOf(search) !== -1 ||
                 type.indexOf(search) !== -1;
        });
      }

      // Sort
      if (this.historySort === 'oldest') {
        items.sort(function (a, b) { return (a.timestamp || 0) - (b.timestamp || 0); });
      } else {
        // newest first (default)
        items.sort(function (a, b) { return (b.timestamp || 0) - (a.timestamp || 0); });
      }

      // Pinned items always first
      var pinned = items.filter(function (h) { return h.pinned; });
      var unpinned = items.filter(function (h) { return !h.pinned; });
      return pinned.concat(unpinned);
    },

    /**
     * Full-history calibration: fetch the authoritative list at limit=total
     * and replace the loaded history wholesale.  A missed
     * history_item_deleted broadcast leaves ghost rows in the loaded list,
     * which inflate the "load more" cursor and make it skip live entries.
     * History is ordered pinned-DESC, timestamp-DESC, so a deleted row can sit
     * at the TOP (pinned) or in the MIDDLE — a tail-trim would evict LIVE
     * oldest entries and keep the ghost; only a wholesale replace works.
     *
     * Shared by app.js (page-1 refresh) and ws.js (history_updated merge) so
     * the logic lives in exactly one place (mobile.html keeps an independent
     * copy — no shared JS infrastructure there — but mirrors this contract).
     *
     * Race guard: a history_item_deleted / history_clear can land while the
     * fetch is in flight.  Its snapshot would resurrect the deleted rows on
     * write-back, so we record historyMutationTick at start and abandon the
     * write-back (only pin the cursor) if it changed before the response
     * arrived.
     *
     * Throttle: a full download at limit=total is expensive, so at most one
     * calibration per client every CALIBRATION_THROTTLE_MS.  A call inside the
     * window still pins the cursor to total (so Load More can't skip) but
     * skips the refetch.  Every terminal state consumes the budget — success
     * write-back, raced abandon, failure and timeout all stamp
     * _lastCalibration — so a constantly mutating history cannot trigger a
     * full download on every broadcast.  Success / raced-abandon / failure
     * ALSO advance _calibrationGen so their late responses can never write
     * back; the TIMEOUT is the deliberate exception (it unwedges the lock and
     * consumes the budget but does NOT advance the gen, so a slow-but-valid
     * response that settles later still writes back — staleness vs. newer data
     * is the mutation tick's job, not the timer's).
     *
     * @param {number} total - authoritative item count from the triggering
     *   response (the list only has ghosts when history.length > total).
     * @returns {Promise<Array>} the calibrated items (or the current list when
     *   throttled / failed / raced).
     */
    calibrateHistory: function (total) {
      var self = this;
      if (!window.ClipsyncAPI || !window.ClipsyncAPI.getHistory) {
        // No API client — nothing to calibrate against.
        return Promise.resolve(this.history.slice());
      }
      var now = Date.now();
      if (this._historyCalibrating ||
          (this._lastCalibration && (now - this._lastCalibration) < CALIBRATION_THROTTLE_MS)) {
        // Already calibrating, or one ran recently — don't pile on.  Pin the
        // cursor to total so Load More can't skip live entries; the next
        // refresh / broadcast retries when the window elapses.
        this.setHistoryCursor(total);
        return Promise.resolve(this.history.slice());
      }
      this._historyCalibrating = true;
      var startTick = this.historyMutationTick;
      // Generation counter: success write-back, failure and raced abandon
      // advance the generation, so their late responses can never clear a
      // newer calibration's lock or write back.  The TIMEOUT is the deliberate
      // exception — it unwedges the lock and consumes the budget but does NOT
      // advance the gen, so a slow-but-valid response that settles after the
      // timeout still passes this guard and writes back (staleness vs. newer
      // data is the mutation tick's job, not the timer's).
      var calibGen = (this._calibrationGen || 0) + 1;
      this._calibrationGen = calibGen;
      // Timeout fallback: an old webview without AbortController can leave the
      // fetch pending forever, which would wedge _historyCalibrating.  After
      // CALIBRATION_TIMEOUT_MS (if this generation still owns the lock) the
      // calibration unwedges its lock and consumes the throttle budget so the
      // next poll can retry — but it does NOT advance the generation (see the
      // gen comment above): a slow-but-valid response that settles later still
      // passes the gen guard and writes back.
      var calibTimer = setTimeout(function () {
        if (self._calibrationGen === calibGen && self._historyCalibrating) {
          // Unwedge the lock so the next poll can retry, and consume the
          // throttle budget.  Deliberately does NOT advance the generation:
          // a slow-but-valid response that settles later still passes the gen
          // guard and writes back — staleness vs. newer data is the mutation
          // tick's job, not the timer's.  Advancing the gen here made any
          // fetch slower than the timeout permanently unable to heal ghosts.
          self._historyCalibrating = false;
          self._lastCalibration = Date.now();
        }
      }, CALIBRATION_TIMEOUT_MS);
      return window.ClipsyncAPI.getHistory({ limit: total, offset: 0 })
        .then(function (calRes) {
          clearTimeout(calibTimer);
          if (self._calibrationGen !== calibGen) {
            // A newer calibration superseded this one (a new calibration
            // started, or this one hit a terminal state that advanced the gen)
            // — a late response must never write back or influence the current
            // state.  The superseding event already consumed the budget, so
            // this is a plain abandon.
            return self.history.slice();
          }
          self._historyCalibrating = false;
          // A delete/clear/new-entry landed while the fetch was in flight — the
          // snapshot predates it and would resurrect/overwrite rows.  Drop the
          // write-back (the delete handler already fixed the list + cursor).
          // This is still a terminal state: advance the generation and consume
          // the throttle budget, so a constantly-mutating history doesn't
          // trigger a full download on every broadcast — at most one attempt
          // per window, and ghosts heal in the first 30s silent window.
          if (self.historyMutationTick !== startTick) {
            self._calibrationGen += 1;
            self.setHistoryCursor(total);
            self._lastCalibration = Date.now();
            return self.history.slice();
          }
          var calItems = (calRes && calRes.items) ? calRes.items : [];
          // Route through the shared helper so a malformed null row in the
          // calibration response is filtered (never stored) like every other
          // history write path.
          self.replaceHistory(calItems);
          self.setHistoryCursor(calRes && calRes.total != null ? calRes.total : null);
          // The write-back committed — advance the generation and consume the
          // throttle budget (all terminal states consume it).
          self._calibrationGen += 1;
          self._lastCalibration = Date.now();
          return calItems;
        })
        .catch(function () {
          clearTimeout(calibTimer);
          if (self._calibrationGen !== calibGen) {
            // A newer calibration superseded this one — a late failure must
            // not clear the lock or write back.  The superseding event already
            // consumed the budget.
            return self.history.slice();
          }
          // Calibration failed — keep what is loaded and pin the cursor to
          // total so Load More can't skip live entries.  A terminal state:
          // advance the generation and consume the throttle budget so a
          // persistently-failing server doesn't trigger a full download on
          // every broadcast.
          self._calibrationGen += 1;
          self._historyCalibrating = false;
          self.setHistoryCursor(total);
          self._lastCalibration = Date.now();
          return self.history.slice();
        });
    },

    /**
     * Shared history-mutation helpers.  Every history change path (local
     * delete / batch-delete / clear in history-item.js & history-panel.js, and
     * the WS delete / clear / paged-merge handlers in ws.js, plus the page-1
     * refresh merge in app.js) routes through these so the mutation-tick bump
     * that guards in-flight calibrations lives in exactly one place — a future
     * hand-written splice/unshift can't silently re-open the race.
     *
     * Each helper is behaviour-identical to the code it replaces EXCEPT for
     * bumping historyMutationTick (the delete helper also prunes selectedIds of
     * removed entries, where the old call sites did it themselves).
     */

    /**
     * Set a single row's pinned flag (re-finding by id — a pinned row reorders
     * to the top while the request is in flight) and bump the reconcile guard.
     * The bump is UNCONDITIONAL: the server committed a pin change, so any
     * in-flight calibration whose snapshot predates it must not write back —
     * even when the toggled row left the loaded window during the round-trip
     * (unpin of a boundary row drops it from page 1, and findIndex misses).
     * @param {number} eid
     * @param {boolean} pinned
     */
    setPinned: function (eid, pinned) {
      var liveIdx = this.history.findIndex(function (h) {
        return h.entry_id === eid;
      });
      if (liveIdx !== -1) {
        this.history[liveIdx].pinned = !!pinned;
      }
      this.historyMutationTick += 1;
    },

    /**
     * Batch equivalent of setPinned — same unconditional guard bump.  Uses a
     * hash set for the id-membership test (O(n+m), matching removeHistoryItems).
     * @param {Array<number>} ids
     * @param {boolean} pinned
     * @returns {number} count of loaded rows the flag was applied to
     */
    setPinnedBatch: function (ids, pinned) {
      if (!ids || !ids.length) return 0;
      var idSet = {};
      for (var si = 0; si < ids.length; si++) {
        idSet[ids[si]] = true;
      }
      var matched = 0;
      for (var bi = 0; bi < this.history.length; bi++) {
        if (idSet[this.history[bi].entry_id]) {
          this.history[bi].pinned = !!pinned;
          matched++;
        }
      }
      this.historyMutationTick += 1;
      return matched;
    },

    // Compare two row dicts on every key, BOTH directions (stronger than
    // mergeHistoryFresh's one-directional incoming-only compare): a change in
    // any field — current or future, added on either side — counts as a
    // mutation.  An explicit field list would silently diverge from the server
    // serializer the day a new field is added.  NB: do not store client-only
    // enumerable fields on history rows — the two-directional compare would
    // treat them as perpetual changes.
    _rowDiffer: function (a, b) {
      if (!a || !b) return true;
      for (var k in a) {
        if (a.hasOwnProperty(k) && a[k] !== b[k]) return true;
      }
      for (var k2 in b) {
        if (b.hasOwnProperty(k2) && b[k2] !== a[k2]) return true;
      }
      return false;
    },

    // Align the "load more" cursor with the VISIBLE list after any wholesale
    // replace or append: offset = list length (pinned to total so ghost
    // inflation can't overshoot), hasMore = length < total.  One shared
    // convention so the raw-vs-visible cursor formula can never drift across
    // the ~8 call sites again (it flip-flopped between v1.0.43 raw and
    // v1.0.44 visible for exactly that reason).
    setHistoryCursor: function (total) {
      var len = this.history.length;
      this.historyOffset = (total != null) ? Math.min(len, total) : len;
      this.historyHasMore = (total != null) ? (len < total) : false;
    },

    // Replace the whole history list with an authoritative snapshot (page-1
    // reload, wholesale broadcast, calibration write-back).  Filters out
    // malformed null rows — a null stored here would crash the renderer — and
    // bumps the reconcile guard only when the list actually changed.  Returns
    // whether the list changed.  The change detection is idempotent even when
    // null rows were skipped (it compares the CLEANED list, not raw lengths);
    // an identical snapshot skips the rebuild entirely (no reactive churn).
    replaceHistory: function (items) {
      var cleaned = [];
      for (var ri = 0; ri < items.length; ri++) {
        if (items[ri] == null) continue;
        cleaned.push(items[ri]);
      }
      var changed = cleaned.length !== this.history.length;
      if (!changed) {
        for (var ci = 0; ci < cleaned.length; ci++) {
          if (this._rowDiffer(this.history[ci], cleaned[ci])) {
            changed = true;
            break;
          }
        }
      }
      if (!changed) {
        return false;
      }
      this.history.splice(0, this.history.length);
      for (var pi = 0; pi < cleaned.length; pi++) {
        this.history.push(cleaned[pi]);
      }
      this.historyMutationTick += 1;
      return true;
    },

    /**
     * Remove history entries by entry_id (in place, one splice per row so the
     * reactive list updates).  Bumps historyMutationTick once when at least
     * one row was removed, and prunes removed ids from selectedIds.  Returns
     * the number of rows removed so callers can shrink the pagination cursor.
     * @param {Array<string|number>} ids
     * @returns {number} count of rows removed
     */
    removeHistoryItems: function (ids) {
      if (!ids || !ids.length) return 0;
      var delSet = {};
      for (var di = 0; di < ids.length; di++) {
        delSet[ids[di]] = true;
      }
      var removedCount = 0;
      for (var hiDel = this.history.length - 1; hiDel >= 0; hiDel--) {
        var histItem = this.history[hiDel];
        if (histItem && histItem.entry_id !== undefined &&
            delSet[histItem.entry_id]) {
          this.history.splice(hiDel, 1);
          removedCount++;
        }
      }
      if (removedCount > 0) {
        this.historyMutationTick += 1;
      }
      // Prune removed ids from the multi-select set whenever ids were given,
      // so a stale selection never counts a deleted item (the callers this
      // helper replaces all did this — a future path can't forget).
      var keptIds = [];
      this.selectedIds.forEach(function (sid) {
        if (!delSet[sid]) keptIds.push(sid);
      });
      this.selectedIds = new Set(keptIds);
      return removedCount;
    },

    /**
     * Clear the loaded history list wholesale and bump historyMutationTick
     * (the callers reset the pagination cursor / selection as before).
     */
    clearHistory: function () {
      this.history.splice(0, this.history.length);
      this.historyMutationTick += 1;
    },

    /**
     * Upsert/prepend a page-1 snapshot into the loaded list: matching rows are
     * updated in place (keeps their loaded position), genuinely-new rows are
     * prepended at the top preserving newest-first order, deduped (no
     * duplicates).  Bumps historyMutationTick only when the merge actually
     * changed the list — a pure display refresh (same entries, unchanged) does
     * not.  Returns true when anything changed.
     * @param {Array} items - the incoming page-1 snapshot
     * @returns {boolean} true when the list changed
     */
    mergeHistoryFresh: function (items) {
      if (!items || !items.length) return false;
      var idxById = {};
      for (var i = 0; i < this.history.length; i++) {
        var cur = this.history[i];
        if (cur && cur.entry_id !== undefined) idxById[cur.entry_id] = i;
      }
      var fresh = [];
      var changed = false;
      for (var j = 0; j < items.length; j++) {
        var inc = items[j];
        if (!inc) continue;
        var foundIdx = (inc.entry_id !== undefined && idxById[inc.entry_id] !== undefined) ? idxById[inc.entry_id] : -1;
        if (foundIdx !== -1) {
          // Update in place — keeps the item's loaded position.  A changed
          // broadcast payload is new data merged into the list; an identical
          // payload (display refresh) is not.
          var incChanged = false;
          for (var fk in inc) {
            if (inc.hasOwnProperty(fk) && inc[fk] !== this.history[foundIdx][fk]) {
              incChanged = true;
              break;
            }
          }
          if (incChanged) {
            Object.assign(this.history[foundIdx], inc);
            changed = true;
          }
        } else {
          fresh.push(inc);
        }
      }
      // Prepend new items, preserving snapshot (newest-first) order.
      for (var k = fresh.length - 1; k >= 0; k--) {
        this.history.unshift(fresh[k]);
      }
      if (fresh.length > 0) {
        changed = true;
      }
      if (changed) {
        this.historyMutationTick += 1;
      }
      return changed;
    },

    /* ═══════════════════════════════════════════════════════════════
       Favorites helpers
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Get filtered + searched + sorted favorites list.
     * @returns {Array}
     */
    filteredFavorites: function () {
      var items = this.favorites.slice();
      var group = this.activeGroup;
      var search = (this.favoriteSearch || '').toLowerCase().trim();

      if (group) {
        // "Ungrouped" is a display label for items whose stored group is
        // empty. Map it back so filtering by the Ungrouped group actually
        // shows those items (otherwise the sidebar filter returns nothing).
        var matchGroup = group === 'Ungrouped' ? '' : group;
        items = items.filter(function (f) { return (f.group || '') === matchGroup; });
      }
      if (search) {
        items = items.filter(function (f) {
          return (f.title || '').toLowerCase().indexOf(search) !== -1 ||
                 (f.content || '').toLowerCase().indexOf(search) !== -1;
        });
      }
      // Sort: by explicit position ascending (matching the backend's
      // ORDER BY position ASC, created DESC), falling back to newest first.
      items.sort(function (a, b) {
        var pa = (a.position === undefined || a.position === null) ? 0 : a.position;
        var pb = (b.position === undefined || b.position === null) ? 0 : b.position;
        if (pa !== pb) return pa - pb;
        return (b.created || 0) - (a.created || 0);
      });
      return items;
    },

    /**
     * Get unique group names with counts. Groups created from the sidebar are
     * kept in `groupNames` so empty ones (no items yet) still show with a 0
     * count instead of silently disappearing.
     * @returns {Object} { groupName: count, ... }
     */
    groupedFavorites: function () {
      var groups = {};
      for (var i = 0; i < this.favorites.length; i++) {
        var g = this.favorites[i].group || 'Ungrouped';
        groups[g] = (groups[g] || 0) + 1;
      }
      for (var j = 0; j < this.groupNames.length; j++) {
        var name = this.groupNames[j];
        if (name && groups[name] === undefined) {
          groups[name] = 0;
        }
      }
      return groups;
    },

    /* ═══════════════════════════════════════════════════════════════
       Group registry (persisted in localStorage)
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Load the persisted group-name list from localStorage.
     */
    loadGroups: function () {
      var saved = null;
      try { saved = localStorage.getItem('clipsync_groups'); } catch (e) { /* ignore */ }
      this.groupNames = [];
      if (saved) {
        try {
          var parsed = JSON.parse(saved);
          if (Array.isArray(parsed)) {
            for (var i = 0; i < parsed.length; i++) {
              if (parsed[i] && typeof parsed[i] === 'string') {
                this.groupNames.push(parsed[i]);
              }
            }
          }
        } catch (e) { /* ignore */ }
      }
    },

    /**
     * Persist the group-name list to localStorage.
     */
    persistGroups: function () {
      try {
        localStorage.setItem('clipsync_groups', JSON.stringify(this.groupNames));
      } catch (e) { /* ignore */ }
    },

    /**
     * Register a group name so an empty group stays visible in the sidebar.
     */
    ensureGroup: function (name) {
      if (!name || typeof name !== 'string') return;
      name = name.trim();
      if (!name) return;
      for (var i = 0; i < this.groupNames.length; i++) {
        if (this.groupNames[i] === name) return;
      }
      this.groupNames.push(name);
      this.persistGroups();
    },

    /**
     * Remove a group name from the registry (e.g. group deleted).
     */
    removeGroup: function (name) {
      var idx = this.groupNames.indexOf(name);
      if (idx === -1) return;
      this.groupNames.splice(idx, 1);
      this.persistGroups();
    },

    /**
     * Rename a group in the registry (keeps empty groups alive).
     */
    renameGroup: function (oldName, newName) {
      var idx = this.groupNames.indexOf(oldName);
      if (idx !== -1) {
        this.groupNames.splice(idx, 1);
      }
      if (newName && typeof newName === 'string') {
        newName = newName.trim();
        if (newName && this.groupNames.indexOf(newName) === -1) {
          this.groupNames.push(newName);
        }
      }
      this.persistGroups();
    },

    /* ═══════════════════════════════════════════════════════════════
       Selection helpers (multi-select)
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Toggle selection of a single item (Ctrl+Click).
     * @param {string|number} id - The entry_id or index
     */
    toggleSelect: function (id) {
      if (this.selectedIds.has(id)) {
        this.selectedIds.delete(id);
      } else {
        this.selectedIds.add(id);
      }
      // Trigger reactivity by replacing the Set
      this.selectedIds = new Set(this.selectedIds);
    },

    /**
     * Range select from one item to another (Shift+Click).
     * @param {string|number} fromId
     * @param {string|number} toId
     */
    rangeSelect: function (fromId, toId) {
      // Range selection must follow the order the user actually sees, not the
      // raw history array (which is unsorted / not filter-aware).
      var items = this.filteredHistory();
      var fromIdx = -1;
      var toIdx = -1;

      // Find indices by entry_id
      if (items.length > 0 && items[0].entry_id !== undefined) {
        fromIdx = items.findIndex(function (h) { return h.entry_id === fromId; });
        toIdx = items.findIndex(function (h) { return h.entry_id === toId; });
      }

      if (fromIdx === -1 || toIdx === -1) return;

      var start = Math.min(fromIdx, toIdx);
      var end = Math.max(fromIdx, toIdx);

      var newSet = new Set(this.selectedIds);
      for (var i = start; i <= end; i++) {
        var entry = items[i];
        if (entry) {
          newSet.add(entry.entry_id);
        }
      }

      this.selectedIds = new Set(newSet);
    },

    /**
     * Clear all selections.
     */
    clearSelection: function () {
      // Replace with empty Set to trigger reactivity
      this.selectedIds = new Set();
    },

    /**
     * Check if an item is selected.
     * @param {string|number} id
     * @returns {boolean}
     */
    isSelected: function (id) {
      return this.selectedIds.has(id);
    },

    /* ═══════════════════════════════════════════════════════════════
       Device helpers
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Get online devices excluding the local device.
     * @returns {Array}
     */
    onlineDevices: function () {
      var selfId = this.deviceId;
      return this.devices.filter(function (d) {
        return d.connected && d.device_id !== selfId;
      });
    },

    /**
     * Get the local device entry.
     * @returns {Object|null}
     */
    localDevice: function () {
      var selfId = this.deviceId;
      return this.devices.find(function (d) {
        return d.device_id === selfId;
      }) || null;
    },

    /* ═══════════════════════════════════════════════════════════════
       Nearby Chat helpers
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Recompute the total unread badge from the session list.  Muted peers'
     * unread is excluded so a silent device never inflates the sidebar badge.
     * @returns {number}
     */
    recalcChatUnread: function () {
      var sum = 0;
      for (var i = 0; i < this.chatSessions.length; i++) {
        var s = this.chatSessions[i];
        if (this.isChatMuted(s.peer_id)) continue;
        sum += (s.unread || 0);
      }
      this.chatUnread = sum;
      return sum;
    },

    /**
     * Load the persisted muted-peer ids from localStorage.
     */
    loadChatMutes: function () {
      var saved = null;
      try { saved = localStorage.getItem('clipsync_chat_mutes'); } catch (e) { /* ignore */ }
      this.mutedChatPeers = new Set();
      if (saved) {
        try {
          var parsed = JSON.parse(saved);
          if (Array.isArray(parsed)) {
            for (var mi = 0; mi < parsed.length; mi++) {
              if (parsed[mi] && typeof parsed[mi] === 'string') {
                this.mutedChatPeers.add(parsed[mi]);
              }
            }
          }
        } catch (e) { /* ignore */ }
      }
    },

    /**
     * Persist the muted-peer set to localStorage.
     */
    persistChatMutes: function () {
      try {
        var list = [];
        this.mutedChatPeers.forEach(function (p) { list.push(p); });
        localStorage.setItem('clipsync_chat_mutes', JSON.stringify(list));
      } catch (e) { /* ignore */ }
    },

    /**
     * Whether *peer_id*'s messages are muted (no unread badge / notification).
     * @param {string} peerId
     * @returns {boolean}
     */
    isChatMuted: function (peerId) {
      return !!peerId && this.mutedChatPeers.has(peerId);
    },

    /**
     * Toggle mute for a peer.  Mutting also clears any unread badge that
     * session already carries, so silencing a device takes effect immediately.
     * @param {string} peerId
     * @returns {boolean} the new muted state
     */
    toggleChatMute: function (peerId) {
      if (!peerId) return false;
      var muted = this.mutedChatPeers.has(peerId);
      if (muted) {
        this.mutedChatPeers.delete(peerId);
      } else {
        this.mutedChatPeers.add(peerId);
        for (var i = 0; i < this.chatSessions.length; i++) {
          if (this.chatSessions[i].peer_id === peerId) {
            this.chatSessions[i].unread = 0;
          }
        }
      }
      // Replace the Set so the mute button state re-renders reactively.
      this.mutedChatPeers = new Set(this.mutedChatPeers);
      this.persistChatMutes();
      this.recalcChatUnread();
      return !muted;
    },

    /**
     * Replace the session list wholesale (from /api/chat/sessions or a
     * chat_sessions broadcast) and keep the unread badge in sync.
     * @param {Array} list
     */
    replaceChatSessions: function (list) {
      this.chatSessions.splice(0, this.chatSessions.length);
      for (var i = 0; i < list.length; i++) {
        this.chatSessions.push(list[i]);
      }
      this.recalcChatUnread();
    },

    /**
     * Replace the active conversation's message list, capped at 200 entries
     * so the DOM never grows without bound.
     * @param {Array} list
     */
    replaceChatMessages: function (list) {
      this.chatMessages.splice(0, this.chatMessages.length);
      for (var i = 0; i < list.length; i++) {
        this.chatMessages.push(list[i]);
      }
      if (this.chatMessages.length > 200) {
        this.chatMessages.splice(0, this.chatMessages.length - 200);
      }
    },

  });

  // Expose globally
  window.__CLIPSYNC_STORE__ = store;

})();
