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

    /* ═══════════════════════════════════════════════════════════════
       History
       ═══════════════════════════════════════════════════════════════ */
    history: [],
    historyFilter: 'all',      // 'all' | 'text' | 'image' | 'file' | 'link'
    historySearch: '',
    historySort: 'newest',     // 'newest' | 'oldest'
    historyHasMore: false,     // true when server has more items to load
    historyOffset: 0,          // current pagination offset

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

    /* ═══════════════════════════════════════════════════════════════
       Dialog system (server-pushed modals)
       ═══════════════════════════════════════════════════════════════ */
    activeDialog: null,         // { dialog_id, dialog_type, title, message, ... }

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
      try { localStorage.setItem('clipsync_onboarded', '1'); } catch (e) { /* ignore */ }
    },

    /**
     * Advance to the next onboarding step (1 → 2 → 3).
     */
    nextOnboardingStep: function () {
      if (this.onboardingStep < 3) this.onboardingStep += 1;
    },

    /**
     * Step 1 "Start": persist the editable device name, then advance.
     */
    startOnboarding: function () {
      this.saveDeviceName();
      this.nextOnboardingStep();
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
     */
    saveDeviceName: function () {
      var name = (this.deviceName || '').trim();
      this.deviceName = name;
      if (window.ClipsyncAPI && window.ClipsyncAPI.updateSettings) {
        window.ClipsyncAPI.updateSettings({ device_name: name }).catch(function () {
          // Non-fatal — the server picks it up on the next settings sync.
        });
      }
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
      this.activeDialog = dlg;
    },

    /**
     * Close the active dialog modal and clear its state.
     */
    closeDialog: function () {
      this.activeDialog = null;
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
     */
    fetchOverview: function () {
      var self = this;
      this.overview.loading = true;
      if (!window.ClipsyncAPI) return;
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
          self.overview.loading = false;
        });
    },

    /**
     * Start a speed test.
     */
    startSpeedTest: function () {
      var self = this;
      this.speedTest.running = true;
      this.speedTest.resultMbps = null;
      this.speedTest.quality = '';
      this.speedTest.progress = 0;
      this.speedTest.status = t('transfer.speed_test.starting');
      this.speedTest.error = '';
      if (!window.ClipsyncAPI) return;
      window.ClipsyncAPI.startSpeedTest()
        .then(function (res) {
          if (res && res.ok) {
            self._pollSpeedTest();
          } else {
            self.speedTest.running = false;
            self.speedTest.error = t('transfer.speed_test.start_failed');
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
          if (!res) return;
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

  });

  // Expose globally
  window.__CLIPSYNC_STORE__ = store;

})();
