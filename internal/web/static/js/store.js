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
    // Update lifecycle state, kept in sync by `update_state` WebSocket events
    // and hydrated from GET /api/update/status on load. phase is
    // 'idle' | 'downloading' | 'ready' | 'failed'; on `ready` `version` and
    // `path` point at the runnable asset the user launches manually.
    updateState: {
      phase: 'idle', fraction: 0, downloaded: 0, total: 0,
      version: '', path: '', error: '',
    },
    connectedCount: computed(function () {
      // Exclude the local device — "connected" counts remote *sync* sessions
      // only (live connection on a paired device), agreeing with the web
      // overview's connected_count (which the backend filters to paired
      // peers too).  Chat-only temporary connections (connected && !paired)
      // don't count here.
      return store.devices.filter(function (d) {
        return d.connected && d.paired && d.device_id !== store.deviceId;
      }).length;
    }),

    // The phone-connect URL shown on onboarding step 3 and copied by the
    // overview: <scheme>://<lan-ip>:<web-port>/mobile.html?token=<token>.
    // The scheme follows the page the web UI is actually served over — the
    // web companion can sit behind TLS, and a phone copying a hardcoded
    // http:// link would fail to connect in that case.
    mobileUrl: computed(function () {
      var ip = store.overview.localIp || (store.settingsCache && store.settingsCache.web_ip) || '';
      var port = store.overview.port || (store.settingsCache && store.settingsCache.web_port) || '';
      if (!ip || !port) return '';
      var proto = (typeof window !== 'undefined' && window.location &&
                   window.location.protocol === 'https:') ? 'https' : 'http';
      return proto + '://' + ip + ':' + port + '/mobile.html?token=' + encodeURIComponent(store.token);
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
       AI-config sync (refactor round 1 — tool profiles)
       aiConfigInventory mirrors GET /api/aiconfig/inventory:
       { peers: { pid: {name, legacy, entries[], fetchedAt} }, fetchedAt }.
       v2 entries are {tool, rel_path, sha256, size, mtime, is_dir}; legacy
       peers' entries are {root_index, path, ...} and read-only (browse +
       preview only).  aiConfigResults is the rolling list of WS
       `aiconfig_file` per-file pull results (newest first, capped).
       aiConfigBatches tracks folder/batch pulls by batch_id so the panel can
       show "N/M done" progress and retry failures.
       ═══════════════════════════════════════════════════════════════ */
    aiConfigInventory: { peers: {}, fetchedAt: '' },
    aiConfigLoaded: false,     // true once the first inventory fetch settled
    aiConfigLoadFailed: false, // fetch rejected AND nothing cached to show
    aiConfigRefreshing: false, // true while a ?refresh=1 re-request runs
    aiConfigResults: [],
    aiConfigProfiles: { tools: [], enabled: [], custom_paths: [] },
    aiConfigProfilesLoaded: false,
    aiConfigBatches: {},       // batch_id -> {total, done, results: [], peerId}
    aiConfigMigrateOpen: false, // migration wizard modal visibility

    /* ═══════════════════════════════════════════════════════════════
       AI-config LOCAL manager (round 18, no pairing needed)
       aiConfigLocal mirrors GET /api/aiconfig/local:
       { collected_at, tools: [{key,label,entries}], custom_paths: [],
       roots: [{tool, kind, path, count}], entries: [{tool, rel_path, size,
       mtime, sha256, is_dir}] } — normalized defensively in
       fetchAiConfigLocal().  This is a plain file manager over THIS device's
       tool-profile roots; it must work with zero paired devices.
       ═══════════════════════════════════════════════════════════════ */
    aiConfigLocal: {
      collected_at: '',
      tools: [],
      custom_paths: [],
      roots: [],
      entries: [],
      loaded: false,     // true once the first local fetch settled
      loadFailed: false, // fetch rejected AND nothing cached to show
      refreshing: false, // true while a fresh fetch is in flight
    },

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
    },

    /* ═══════════════════════════════════════════════════════════════
       Pairing requests
       ═══════════════════════════════════════════════════════════════ */
    pairingRequests: [],

    /* ═══════════════════════════════════════════════════════════════
       Removed (archived) devices — forgotten peers kept in the backend
       config for Restore / Delete-permanently management on the device page.
       ═══════════════════════════════════════════════════════════════ */
    removedDevices: [],

    /* ═══════════════════════════════════════════════════════════════
       Internet (cross-network) sync relay state
       One of 'off' | 'connecting' | 'online' | 'error'.  Seeded from
       GET /api/settings (internet_sync_state), then kept live by the WS
       `relay_state` event (ws.js).  '' = not yet known.
       ═══════════════════════════════════════════════════════════════ */
    relayState: '',

    /* ═══════════════════════════════════════════════════════════════
       Current relay broker endpoint URL ('' = offline/disabled/unknown).
       Read-only diagnostic shown in the settings panel so a user can tell
       whether two paired devices are on the same broker.  Seeded from
       GET /api/settings (current_relay_broker), kept live by the WS
       `relay_state` event (payload.broker, ws.js).
       ═══════════════════════════════════════════════════════════════ */
    currentRelayBroker: '',

    /* ═══════════════════════════════════════════════════════════════
       Internet pairing (round 14/15)
       internetPairPeers mirrors GET /api/internetpair/status:
       [{ peer_id, name, alias, online, last_seen, paired, status }] — kept
       live by the WS `netpair_peer` event and refreshed after enter/generate/
       rename/unpair. `alias` is a user-chosen display name (empty = fall back
       to `name`); `last_seen` is an epoch-seconds timestamp or null; `online`
       is a boolean (this device has a live relay channel to the peer).
       internetPairCode is the code THIS device generated ('' = none).
       ═══════════════════════════════════════════════════════════════ */
    internetPairPeers: [],
    internetPairCode: '',

    /* ═══════════════════════════════════════════════════════════════
       Internet delivery status (round 17)
       Two lightweight mirrors, both fed by the WS `internet_delivery` event
       ({peer_id, msg_id, status}) and seeded by GET /api/internetdelivery
       per peer:

       internetDelivery: { [peerId]: { pending, lastStatus, msgStatus,
       loaded, loadFailed } } — drives the one-line status on each internet
       device card: `pending` is the number of clips queued for offline
       retry (the "待补发 N" badge), `lastStatus` is the most recent
       transition (⏳/✅/❌), and `msgStatus` (msg_id → status) dedupes the
       pending count across re-broadcasts.

       internetDeliveryMsgs: { [msg_id]: status } — the small msg_id → status
       map the chat panel reads to stamp ✓已送达 / ✗未送达 / …发送中 on an
       outgoing relay-chat bubble. Chat entries expose msg_id from newer
       hosts only; when the field is absent the badge is skipped (matched
       messages update live as the WS events arrive).

       `loaded`/`loadFailed` let the device card distinguish "backend not
       ready" from "nothing to show" so the row disappears cleanly on an
       older host.
       ═══════════════════════════════════════════════════════════════ */
    internetDelivery: {},
    internetDeliveryMsgs: {},

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
    activeTab: 'overview',      // 'overview' | 'history' | 'devices' | 'transfers' | 'favorites' | 'chat' | 'aiconfig' | 'diagnostics'
    theme: 'system',            // 'system' | 'light' | 'dark'
    sidebarOpen: true,
    selectedIds: new Set(),     // multi-select set of entry IDs
    kbdIndex: -1,               // keyboard-cursor index into the visible history list (-1 = none)
    loading: false,
    initialLoad: true,          // true until first data fetch completes
    loadError: false,           // true when the initial load fails or times out
    devicesLoadFailed: false,   // true when the device-list fetch rejected
    // Stacked toasts — [{id, message, type, leaving}]. Several notifications
    // can be visible at once; showToast caps the stack so a burst of events
    // can't flood the screen.
    toasts: [],
    previewItem: null,          // hover preview target (item object or null)
    previewPosition: { x: 0, y: 0 },  // mouse position for preview popover
    contextMenu: {
      visible: false,
      x: 0, y: 0,
      mode: 'history-item',  // 'history-item' | 'device' | 'chat-session' | 'chat-message'
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
    autoStart: false,
    uiBackend: 'webview',       // 'webview' | 'ctk'
    settingsCache: {},           // cached settings from server

    /* ═══════════════════════════════════════════════════════════════
       First-run onboarding wizard
       ═══════════════════════════════════════════════════════════════ */
    onboardingDone: false,      // true once the wizard was finished/skipped
    showOnboarding: false,      // true while the wizard overlay is visible
    onboardingStep: 1,          // 1 = welcome · 2 = pair · 3 = phone · 4-9 = settings toggles
    onboardingError: '',        // inline error shown on the wizard (e.g. empty name)
    onboardingSaving: false,    // true while the device-name save is in flight

    // Steps 4-9: feature toggles, all default OFF on a fresh install. The four
    // real toggles write their backend keys when the wizard completes. 外观/偏好
    // (steps 7-8) are shown directly as their real controls — no UI masters.
    onboardContentFilter: false,
    onboardAppFilter: false,
    onboardRemoteSync: false,
    onboardSound: false,

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
      } else if (this.settingsCache &&
                 (this.settingsCache.appearance_mode === 'dark' ||
                  this.settingsCache.appearance_mode === 'light' ||
                  this.settingsCache.appearance_mode === 'system')) {
        // The theme choice is persisted server-side too (selectAppearanceTheme
        // POSTs appearance_mode). On a fresh browser (empty localStorage) use the
        // saved server theme so the page doesn't silently fall back to the OS
        // "system" default. localStorage, when present, still wins above.
        this.setTheme(this.settingsCache.appearance_mode);
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
     * Mark onboarding as done, persist it, hide the wizard overlay, and commit
     * the four feature toggles from steps 4-6 and 9. All default OFF on a
     * fresh install, so a wizard the user just clicks through leaves them
     * explicitly off. 外观/偏好 (steps 7-8) are applied immediately by their
     * real controls.
     */
    completeOnboarding: function () {
      this.onboardingDone = true;
      this.showOnboarding = false;
      this.onboardingStep = 1;
      this.onboardingError = '';
      this.onboardingSaving = false;
      try { localStorage.setItem('clipsync_onboarded', '1'); } catch (e) { /* ignore */ }

      var self = this;
      var patch = {
        filter_enabled_categories: this.onboardContentFilter
          ? this.defaultFilterCategories()
          : [],
        app_filter_enabled: this.onboardAppFilter,
        internet_sync_enabled: this.onboardRemoteSync,
        sound_enabled: this.onboardSound,
      };
      if (this.onboardSound !== this.soundEnabled) {
        this.soundEnabled = this.onboardSound;
        if (typeof ClipsyncSound !== 'undefined' && ClipsyncSound.setEnabled) {
          ClipsyncSound.setEnabled(this.soundEnabled);
        }
      }
      if (window.ClipsyncAPI && window.ClipsyncAPI.updateSettings) {
        window.ClipsyncAPI.updateSettings(patch).then(function (res) {
          if (res && res.updated) self.mergeSettings(res.updated);
        }).catch(function () {
          self.showToast(t('dialog.failed'), 2000, 'error');
        });
      }
    },

    /**
     * The full set of sensitive-content categories the content filter guards
     * when enabled (mirrors the settings panel's default filterEnabled list).
     * @returns {string[]}
     */
    defaultFilterCategories: function () {
      return ['credit_card', 'ssn', 'api_key', 'private_key', 'password'];
    },

    /**
     * Advance to the next onboarding step (1 → 2 → … → 9).
     * Entering step 3 ensures the LAN IP / web port are loaded so the
     * phone-connect URL and QR button have real data to work with.
     */
    nextOnboardingStep: function () {
      this.onboardingError = '';
      if (this.onboardingStep < 9) {
        this.onboardingStep += 1;
      }
      if (this.onboardingStep === 3) {
        this.ensureOverview();
      }
    },

    /**
     * Wizard (and settings panel) theme picker: apply + persist appearance_mode.
     * @param {'light'|'dark'|'system'} t
     */
    selectAppearanceTheme: function (t) {
      var self = this;
      this.setTheme(t);
      if (window.ClipsyncAPI && window.ClipsyncAPI.updateSettings) {
        window.ClipsyncAPI.updateSettings({ appearance_mode: t }).then(function (res) {
          if (res && res.updated) self.settingsCache.appearance_mode = t;
        }).catch(function () {
          self.showToast(t('dialog.failed'), 2000, 'error');
        });
      }
    },

    /**
     * Toggle the UI-animation switch: flip local state and persist the key.
     */
    toggleAnimation: function () {
      var self = this;
      var next = !this.animationsEnabled;
      this.animationsEnabled = next;
      if (window.ClipsyncAPI && window.ClipsyncAPI.updateSettings) {
        window.ClipsyncAPI.updateSettings({ ui_animation_enabled: next }).then(function (res) {
          if (res && res.updated) self.settingsCache.ui_animation_enabled = next;
        }).catch(function () {
          self.animationsEnabled = !next;
          self.showToast(t('dialog.failed'), 2000, 'error');
        });
      }
    },

    /**
     * Toggle the launch-at-login switch: flip local state and persist the key.
     */
    toggleAutoStart: function () {
      var self = this;
      var next = !this.autoStart;
      this.autoStart = next;
      if (window.ClipsyncAPI && window.ClipsyncAPI.updateSettings) {
        window.ClipsyncAPI.updateSettings({ auto_start: next }).then(function (res) {
          if (res && res.updated) self.settingsCache.auto_start = next;
        }).catch(function () {
          self.autoStart = !next;
          self.showToast(t('dialog.failed'), 2000, 'error');
        });
      }
    },

    /**
     * Current UI locale, read live so the wizard's language picker reflects
     * the language the page was actually served in.
     * @returns {string}
     */
    currentLocale: function () {
      if (typeof ClipsyncI18n !== 'undefined' && ClipsyncI18n.locale) {
        return ClipsyncI18n.locale;
      }
      try { return localStorage.getItem('clipsync_locale') || 'en'; } catch (e) { return 'en'; }
    },

    /**
     * Theme choices for the wizard's inline picker (same keys as the settings
     * panel's themeOptions). @returns {Array<{value:string,label:string}>}
     */
    themeOptions: function () {
      return [
        { value: 'system', label: t('settings_window.theme_system') },
        { value: 'light', label: t('settings_window.theme_light') },
        { value: 'dark', label: t('settings_window.theme_dark') },
      ];
    },

    /**
     * Wizard language picker: persist the choice (same key the settings panel
     * uses) and reload so every string re-resolves in the new language.
     * @param {string} locale
     */
    selectLocale: function (locale) {
      var self = this;
      try { localStorage.setItem('clipsync_locale', locale); } catch (e) { /* ignore */ }
      if (window.ClipsyncAPI && window.ClipsyncAPI.updateSettings) {
        window.ClipsyncAPI.updateSettings({ language: locale }).then(function () {
          window.setTimeout(function () { window.location.reload(); }, 600);
        }).catch(function () {
          self.showToast(t('dialog.failed'), 2000, 'error');
        });
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
        this.onboardingError = t('onboarding.err_device_name');
        return;
      }
      this.onboardingError = '';
      this.onboardingSaving = true;
      this.saveDeviceName().then(function () {
        self.onboardingSaving = false;
        self.nextOnboardingStep();
      }).catch(function () {
        self.onboardingSaving = false;
        self.showToast(t('onboarding.err_save_name'), 2500, 'error');
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
     * "Show QR Code": ask the server to push the phone-connect QR dialog.
     * The single implementation both the onboarding step and the transfer
     * panel's "Send to Phone" button use (the panel delegates here and just
     * manages its own busy state around the returned promise).
     * @returns {Promise} resolves once the request settles (failure is toasted)
     */
    showPhoneQr: function () {
      var self = this;
      return this.ensureOverview().then(function () {
        if (window.ClipsyncAPI && window.ClipsyncAPI._fetch) {
          return window.ClipsyncAPI._fetch('POST', '/api/show_qr', {}).catch(function (e) {
            console.error('[ClipSync] Failed to show phone QR:', e);
            self.showToast(t('transfer.phone_qr_failed'), 2500, 'error');
          });
        }
        return null;
      });
    },

    /**
     * Show a toast notification. Toasts stack (up to MAX_TOASTS visible);
     * each is removed after its own duration.
     * @param {string} msg - Message to display
     * @param {number} [duration=3500] - Duration in ms
     * @param {'info'|'success'|'warning'|'error'} [type='info']
     */
    showToast: function (msg, duration, type) {
      // Default display time long enough to read a full notification; many
      // call sites pass shorter explicit values (e.g. 1500–3000ms).
      if (duration === undefined) duration = 3500;
      if (type === undefined) type = 'info';
      var MAX_TOASTS = 4;
      // Cap the stack: a burst of events must not flood the screen — drop the
      // oldest toast(s) instead of queueing unboundedly.
      while (this.toasts.length >= MAX_TOASTS) {
        this._dropToast(this.toasts[0].id);
      }
      this._toastSeq = (this._toastSeq || 0) + 1;
      var id = this._toastSeq;
      var self = this;
      this._toastTimers = this._toastTimers || {};
      this.toasts.push({ id: id, message: msg, type: type, leaving: false });
      this._toastTimers[id] = setTimeout(function () {
        delete self._toastTimers[id];
        self._dismissToast(id);
      }, duration);
    },

    /**
     * Start a toast's leave animation; the node is removed shortly after so
     * the fadeOut transition in index.html stays visible.
     * @param {number} id
     */
    _dismissToast: function (id) {
      var t = null;
      for (var i = 0; i < this.toasts.length; i++) {
        if (this.toasts[i].id === id) { t = this.toasts[i]; break; }
      }
      if (!t || t.leaving) return;
      t.leaving = true;
      var self = this;
      setTimeout(function () { self._dropToast(id); }, 180);
    },

    /**
     * Remove a toast from the stack immediately (no leave animation) and
     * cancel its timer. Safe to call for an already-removed id.
     * @param {number} id
     */
    _dropToast: function (id) {
      if (this._toastTimers && this._toastTimers[id]) {
        clearTimeout(this._toastTimers[id]);
        delete this._toastTimers[id];
      }
      for (var i = 0; i < this.toasts.length; i++) {
        if (this.toasts[i].id === id) {
          this.toasts.splice(i, 1);
          return;
        }
      }
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
     * Replace the removed-devices archive from the backend's authoritative
     * `removed` snapshot (polled via /api/devices and WS broadcast).  Same
     * polling-fallback rationale as syncPairingRequests.
     */
    syncRemovedDevices: function (removed) {
      var list = (removed || []).filter(function (r) {
        return !!r.device_id;
      });
      this.removedDevices = list;
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
            // Newer overview fields (rich stats + live activity feed).
            self.overview.connectedNames = o.connected_names || [];
            self.overview.discoveredCount = o.discovered_count || 0;
            self.overview.historyToday = o.history_today || 0;
            self.overview.historyPinned = o.history_pinned || 0;
            self.overview.historyImages = o.history_images || 0;
            self.overview.transferCompleted = o.transfer_completed || 0;
            self.overview.version = o.version || '';
            self.overview.recentItems = o.recent_items || [];
          }
        })
        .catch(function (e) {
          console.error('[ClipSync] Failed to fetch overview:', e);
        })
        .finally(function () {
          self._overviewInFlight = false;
        });
    },

    /**
     * Record a speed-test failure and surface it as a toast.  The inline
     * panel error row was removed (user feedback: showing the failure both
     * inline and as a toast was a duplicate) — the toast is the single
     * surface.  speedTest.error is still kept as state so the panel-empty
     * hint stays hidden after a failed test.
     * @param {string} msg
     * @param {string} [type] toast variant ('error' | 'warning' | 'info')
     */
    _setSpeedTestError: function (msg, type) {
      this.speedTest.error = msg;
      this.showToast(msg, 3500, type || 'error');
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
        this._setSpeedTestError(t('transfer.speed_test.unavailable'));
        return;
      }
      this.speedTest.running = true;
      window.ClipsyncAPI.startSpeedTest()
        .then(function (res) {
          if (res && res.ok) {
            self._pollSpeedTest();
          } else {
            self.speedTest.running = false;
            // The backend refuses to run without a connected LAN peer — the
            // speed test measures LAN throughput only.  Internet (netpair)
            // peers don't count: the relay is slow and best-effort, so the
            // honest message is "LAN-only", not a generic failure.
            var lanOnline = (self.devices || []).some(function (d) { return d.connected; });
            var anyOnline = lanOnline || (self.internetPairPeers || []).some(
              function (p) { return !!p.online; });
            if (!lanOnline && anyOnline) {
              self._setSpeedTestError(t('transfer.speed_test.lan_only'), 'warning');
            } else {
              self._setSpeedTestError(
                !anyOnline ? t('transfer.speed_test.no_peer')
                           : t('transfer.speed_test.start_failed'));
            }
          }
        })
        .catch(function (e) {
          self.speedTest.running = false;
          self._setSpeedTestError(t('transfer.speed_test.unavailable'));
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
            self._setSpeedTestError(t('transfer.speed_test.failed'));
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
          self._setSpeedTestError(t('transfer.speed_test.failed'));
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

    /**
     * Reconcile the active + history transfer lists against a GET
     * /api/transfer response.  The single implementation every caller uses
     * (initial load, WS transfer-complete, post-action refresh, periodic
     * poll) so the splice logic cannot drift between app.js and the panels.
     * @param {Object} res - { active: Array, history: Array } from the API
     */
    reconcileTransfers: function (res) {
      if (res && res.active) {
        this.activeTransfers.splice(0, this.activeTransfers.length);
        for (var i = 0; i < res.active.length; i++) {
          this.activeTransfers.push(res.active[i]);
        }
      }
      if (res && res.history) {
        this.transferHistory.splice(0, this.transferHistory.length);
        for (var j = 0; j < res.history.length; j++) {
          this.transferHistory.push(res.history[j]);
        }
      }
    },

    /**
     * Fetch the transfer snapshot and reconcile it into the store.
     * @returns {Promise<Object|null>} the API response (rejects on failure)
     */
    refreshTransfers: function () {
      var self = this;
      if (!window.ClipsyncAPI || !window.ClipsyncAPI.getTransfers) {
        return Promise.resolve(null);
      }
      return window.ClipsyncAPI.getTransfers().then(function (res) {
        self.reconcileTransfers(res);
        return res;
      });
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
     * Copy one history entry to the DESKTOP clipboard via paste-rich, bump
     * its local paste count, and toast the result.  Single source for the
     * per-item copy action — history-item and the app-level keyboard path
     * both delegate here so the paste/count/toast contract lives in one
     * place.  (The right-click context menu keeps its own inline pasteRich
     * on the exact clicked row — see context-menu.js _copyFull — and does
     * NOT route through this helper.)
     * @param {string|number} eid - history entry id
     * @param {Object} [opts] - { coarse } switches the success toast to the
     *   touch (phone) wording ("copied to desktop").
     * @returns {Promise<boolean>} resolves true when the copy succeeded
     */
    pasteHistoryItem: function (eid, opts) {
      var self = this;
      opts = opts || {};
      if (eid === undefined || eid === null) {
        return Promise.resolve(false);
      }
      return window.ClipsyncAPI.pasteRich(eid).then(function (res) {
        if (res && res.ok !== false) {
          var idx = self.history.findIndex(function (h) {
            return h.entry_id === eid;
          });
          if (idx !== -1) {
            self.history[idx].paste_count = (self.history[idx].paste_count || 0) + 1;
          }
          self.showToast(
            opts.coarse ? self.t('history.copy_to_desktop_toast') : self.t('history.copied'),
            1500
          );
          return true;
        }
        self.showToast(self.t('history.copy_failed'), 2000);
        return false;
      }).catch(function () {
        self.showToast(self.t('history.copy_failed'), 2000);
        return false;
      });
    },

    // 🔌 Probe connectivity to a peer and toast the per-channel result.  The
    // LAN device card's test action and the internet-pair peer row's "test"
    // both delegate here so the endpoint call, result parsing and toast
    // wording live in ONE place (each caller keeps its own busy flag).
    // Resolves when the probe finishes; a network failure still resolves
    // (after toasting) so a caller's .finally() runs.
    testPeerConnection: function (peerId) {
      var self = this;
      // Acknowledge the click immediately — the probe can take up to ~4s (the
      // backend ping timeout), so without this the button can feel dead even
      // though it shows "...".  The result toast (per-channel latency, or the
      // failure reason) lands on top when the probe answers.
      self.showToast(self.t('device.test_connecting'), 1800);
      return window.ClipsyncAPI.testDeviceConnection(peerId)
        .then(function (res) {
          // A successful probe always carries per-channel rows; an empty
          // array ([] is truthy in JS!) means the backend reported
          // "no_channel" — fall through so the reason actually shows.
          if (res && res.results && res.results.length > 0) {
            var parts = res.results.map(function (r) {
              var channel = self.t(r.channel === 'relay'
                ? 'device.test_channel_relay' : 'device.test_channel_lan');
              if (r.ok && r.latency_ms != null) {
                return self.t('device.test_channel_ok',
                  { channel: channel, latency: Math.round(r.latency_ms) });
              }
              return self.t('device.test_channel_fail',
                { channel: channel, reason: self._testErrorReason(r) });
            });
            var key2 = res.ok ? 'device.test_success' : 'device.test_failed';
            self.showToast(self.t(key2, { detail: parts.join(' · ') }), 4500);
          } else {
            var reason = (res && res.error === 'no_channel')
              ? self.t('device.test_no_channel') : self.t('device.test_failed');
            self.showToast(self.t('device.test_failed', { detail: reason }), 3500);
          }
        })
        .catch(function () {
          self.showToast(self.t('device.test_failed'), 3000);
        });
    },

    // Localize one per-channel failure reason from a probe result.
    _testErrorReason: function (r) {
      if (r.error === 'timeout') return this.t('device.test_timeout');
      if (r.error === 'send_failed') return this.t('device.test_send_failed');
      if (r.error === 'relay_offline') return this.t('device.test_relay_offline');
      return r.error || this.t('device.test_failed');
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
      // Registry-only groups (created on the sidebar, possibly now empty) stay
      // visible so a user who emptied a group doesn't lose it — BUT only when
      // there ARE favorites.  After a factory reset / data-folder wipe the
      // backend has zero favorites while this browser's clipsync_groups
      // localStorage may still list stale groups; showing them would resurrect
      // "111"-style ghosts the reset was supposed to clear.
      if (this.favorites.length > 0) {
        for (var j = 0; j < this.groupNames.length; j++) {
          var name = this.groupNames[j];
          if (name && groups[name] === undefined) {
            groups[name] = 0;
          }
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
      // The keyboard cursor is transient list state too — a tab switch or
      // Escape clears it along with the selection.
      this.kbdIndex = -1;
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
     * Only real sync sessions (live connection on a paired device) — a
     * chat-only temporary connection (connected && !paired) is not a transfer
     * target.  Internet (relay) pairs are intentionally NOT included: the
     * web-upload forward path (on_forward_file) only resolves peers in the
     * LAN transport's connected set, so a relay peer listed here would produce
     * a send button that always fails with "peer offline".
     * @returns {Array}
     */
    onlineDevices: function () {
      var selfId = this.deviceId;
      return this.devices.filter(function (d) {
        return d.connected && d.paired && d.device_id !== selfId;
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
     * Replace the mute set with an authoritative list (from the backend).
     * The backend is the source of truth — it also suppresses the desktop
     * notification for muted peers, so the badge must agree with it.
     * @param {Array} list
     */
    replaceChatMuted: function (list) {
      var next = new Set();
      if (Array.isArray(list)) {
        for (var i = 0; i < list.length; i++) {
          if (list[i] && typeof list[i] === 'string') next.add(list[i]);
        }
      }
      this.mutedChatPeers = next;
      this.persistChatMutes();
      this.recalcChatUnread();
    },

    /**
     * Toggle mute for a peer.  Mutting also clears any unread badge that
     * session already carries, so silencing a device takes effect immediately.
     * The change is pushed to the backend (fire-and-forget) so the desktop
     * notification/sound stops ringing for that peer too.
     * @param {string} peerId
     * @returns {boolean} the new muted state
     */
    toggleChatMute: function (peerId) {
      var self = this;
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
      if (window.ClipsyncAPI && window.ClipsyncAPI.chatMute) {
        // The backend returns the authoritative mute set; on success adopt it
        // (a concurrent toggle elsewhere wins over this local optimistic edit).
        window.ClipsyncAPI.chatMute(peerId, !muted).then(function (res) {
          if (res && Array.isArray(res.muted)) {
            var next = new Set();
            for (var mi = 0; mi < res.muted.length; mi++) {
              if (typeof res.muted[mi] === 'string') next.add(res.muted[mi]);
            }
            self.mutedChatPeers = next;
            self.persistChatMutes();
            self.recalcChatUnread();
          }
        }).catch(function (e) {
          console.error('[ClipSync] Failed to sync chat mute:', e);
        });
      }
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

    /* ═══════════════════════════════════════════════════════════════
       AI-config sync helpers (round 12)
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Fetch every paired peer's AI-config inventory. In-flight calls are
     * coalesced. Fully defensive: a host whose backend predates this feature
     * answers 404 — the fetch settles with aiConfigLoaded=true so the panel
     * shows its empty state instead of spinning forever, and any previously
     * loaded inventory is kept rather than clobbered by the failure.
     * @param {boolean} [refresh=false] - ask the backend to re-request fresh
     *   inventories from the peers (?refresh=1)
     * @returns {Promise<boolean>} true when an inventory was applied
     */
    fetchAiConfigInventory: function (refresh) {
      var self = this;
      if (!window.ClipsyncAPI || !window.ClipsyncAPI.getAiConfigInventory) {
        return Promise.resolve(false);
      }
      if (this._aiConfigInFlight) return Promise.resolve(false);
      this._aiConfigInFlight = true;
      if (refresh) this.aiConfigRefreshing = true;
      return window.ClipsyncAPI.getAiConfigInventory(!!refresh)
        .then(function (res) {
          var raw = (res && res.peers && typeof res.peers === 'object') ? res.peers : {};
          var clean = {};
          Object.keys(raw).forEach(function (pid) {
            var p = raw[pid];
            if (!p || typeof p !== 'object') return;
            var legacy = !!p.legacy;
            clean[pid] = {
              name: p.name || pid,
              legacy: legacy,
              entries: (Array.isArray(p.entries) ? p.entries : []).filter(function (e) {
                return e && typeof e === 'object' && (e.rel_path || e.path);
              }).map(function (e) {
                if (legacy) {
                  // Legacy (pre-refactor) shape — root_index + path; read-only.
                  var ri = (typeof e.root_index === 'number') ? e.root_index
                    : parseInt(e.root_index, 10);
                  return {
                    tool: null,
                    root_index: isFinite(ri) ? ri : 0,
                    rel_path: String(e.path || e.rel_path),
                    sha256: e.sha256 || '',
                    size: (typeof e.size === 'number') ? e.size : Number(e.size) || 0,
                    mtime: e.mtime,
                    is_dir: !!e.is_dir,
                  };
                }
                // v2 shape — deterministic tool key + rel_path.
                return {
                  tool: String(e.tool || 'custom'),
                  rel_path: String(e.rel_path || e.path),
                  sha256: e.sha256 || '',
                  size: (typeof e.size === 'number') ? e.size : Number(e.size) || 0,
                  mtime: e.mtime,
                  is_dir: !!e.is_dir,
                };
              }),
              fetchedAt: p.fetched_at || '',
            };
          });
          self.aiConfigInventory = { peers: clean, fetchedAt: (res && res.fetched_at) || '' };
          self.aiConfigLoaded = true;
          self.aiConfigLoadFailed = false;
          return true;
        })
        .catch(function () {
          // 404 (older backend) / network error — surface the empty state but
          // never wipe data that was already on screen.
          self.aiConfigLoaded = true;
          self.aiConfigLoadFailed =
            Object.keys((self.aiConfigInventory && self.aiConfigInventory.peers) || {}).length === 0;
          return false;
        })
        .finally(function () {
          self._aiConfigInFlight = false;
          self.aiConfigRefreshing = false;
        });
    },

    /**
     * Fetch the AI-tool profile table + this device's current selection
     * (GET /api/aiconfig/profiles).  In-flight calls are coalesced.
     * @returns {Promise<boolean>} true when a snapshot was applied
     */
    fetchAiConfigProfiles: function () {
      var self = this;
      if (!window.ClipsyncAPI || !window.ClipsyncAPI.getAiConfigProfiles) {
        return Promise.resolve(false);
      }
      if (this._aiConfigProfilesInFlight) return Promise.resolve(false);
      this._aiConfigProfilesInFlight = true;
      return window.ClipsyncAPI.getAiConfigProfiles()
        .then(function (res) {
          if (!res || typeof res !== 'object') return false;
          self.aiConfigProfiles = {
            tools: Array.isArray(res.tools) ? res.tools : [],
            enabled: Array.isArray(res.enabled) ? res.enabled : [],
            custom_paths: Array.isArray(res.custom_paths) ? res.custom_paths : [],
          };
          self.aiConfigProfilesLoaded = true;
          return true;
        })
        .catch(function () {
          self.aiConfigProfilesLoaded = true;
          return false;
        })
        .finally(function () {
          self._aiConfigProfilesInFlight = false;
        });
    },

    /**
     * Fetch THIS device's local AI-config inventory (watch roots + files).
     * Independent of pairing — the local manager works with zero peers.
     * In-flight calls are coalesced. Fully defensive: a host whose backend
     * predates this feature answers 404 — the fetch settles with
     * loaded=true (and loadFailed when nothing was cached) so the panel
     * shows its empty/retry state instead of spinning forever, and any
     * previously loaded local listing is kept rather than clobbered.
     * @returns {Promise<boolean>} true when a snapshot was applied
     */
    fetchAiConfigLocal: function () {
      var self = this;
      if (!window.ClipsyncAPI || !window.ClipsyncAPI.getAiConfigLocal) {
        return Promise.resolve(false);
      }
      if (this._aiConfigLocalInFlight) return Promise.resolve(false);
      this._aiConfigLocalInFlight = true;
      this.aiConfigLocal.refreshing = true;
      return window.ClipsyncAPI.getAiConfigLocal()
        .then(function (res) {
          if (!res || typeof res !== 'object') {
            self.aiConfigLocal.loaded = true;
            self.aiConfigLocal.loadFailed = self.aiConfigLocal.entries.length === 0;
            return false;
          }
          var roots = Array.isArray(res.roots) ? res.roots : [];
          var cleanRoots = [];
          for (var ri = 0; ri < roots.length; ri++) {
            var r = roots[ri];
            if (r && typeof r === 'object' &&
                typeof r.tool === 'string' &&
                typeof r.path === 'string') {
              cleanRoots.push({
                tool: r.tool,
                kind: r.kind || 'dir',
                path: r.path,
                count: (typeof r.count === 'number') ? r.count : 0,
              });
            }
          }
          var raw = Array.isArray(res.entries) ? res.entries : [];
          var cleanEntries = [];
          for (var ei = 0; ei < raw.length; ei++) {
            var e = raw[ei];
            if (!e || typeof e !== 'object' || !(e.rel_path || e.path)) continue;
            cleanEntries.push({
              tool: String(e.tool || 'custom'),
              rel_path: String(e.rel_path || e.path),
              sha256: e.sha256 || '',
              size: (typeof e.size === 'number') ? e.size : Number(e.size) || 0,
              mtime: e.mtime,
              is_dir: !!e.is_dir,
            });
          }
          self.aiConfigLocal = {
            collected_at: res.collected_at || '',
            tools: Array.isArray(res.tools) ? res.tools : [],
            custom_paths: Array.isArray(res.custom_paths) ? res.custom_paths : [],
            roots: cleanRoots,
            entries: cleanEntries,
            loaded: true,
            loadFailed: false,
            refreshing: false,
          };
          return true;
        })
        .catch(function () {
          // 404 (older backend) / network error — surface the empty state but
          // never wipe a local listing that was already on screen.
          self.aiConfigLocal.loaded = true;
          self.aiConfigLocal.loadFailed = self.aiConfigLocal.entries.length === 0;
          return false;
        })
        .finally(function () {
          self._aiConfigLocalInFlight = false;
          self.aiConfigLocal.refreshing = false;
        });
    },

    /* ═══════════════════════════════════════════════════════════════
       Internet pairing helpers (round 14)
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Replace the paired-over-internet device list (and the code this device
     * generated) with the backend's authoritative snapshot, normalized.  Fully
     * defensive: a host whose backend predates this feature answers 404 — the
     * fetch settles with the existing state kept (or the empty list) instead
     * of throwing, so the settings panel always shows its empty state.
     * @returns {Promise<boolean>} true when a snapshot was applied
     */
    fetchInternetPairStatus: function () {
      var self = this;
      if (!window.ClipsyncAPI || !window.ClipsyncAPI.getInternetPairStatus) {
        return Promise.resolve(false);
      }
      if (this._netpairInFlight) return Promise.resolve(false);
      this._netpairInFlight = true;
      return window.ClipsyncAPI.getInternetPairStatus()
        .then(function (res) {
          if (res && Array.isArray(res.peers)) {
            var list = [];
            for (var i = 0; i < res.peers.length; i++) {
              var p = res.peers[i];
              if (!p || p.peer_id === undefined || p.peer_id === null) continue;
              list.push({
                peer_id: String(p.peer_id),
                name: p.name || String(p.peer_id),
                alias: p.alias || '',
                online: !!p.online,
                last_seen: (typeof p.last_seen === 'number') ? p.last_seen : null,
                paired: p.paired !== false,
                status: p.status || 'paired',
              });
            }
            self.internetPairPeers = list;
          }
          // generated_code may be null when no code is pending — clear a stale
          // one so the big code block disappears once a pairing confirms.
          self.internetPairCode = (res && res.generated_code)
            ? String(res.generated_code) : '';
          return true;
        })
        .catch(function () {
          // 404 (older backend) / network error — keep whatever is loaded and
          // signal failure so the panel can render its empty state.
          return false;
        })
        .finally(function () {
          self._netpairInFlight = false;
        });
    },

    /**
     * Fold one WS `netpair_peer` event into the paired list.
     * status:"paired" upserts the peer (with a toast); status:"unpaired"
     * removes it (the acting side already confirmed, no toast needed).
     * Only called by ws.js after validating the vocabulary.
     * @param {{peer_id: string, name: string, alias?: string, status: string}} data
     */
    applyNetpairPeer: function (data) {
      var pid = (data.peer_id !== undefined && data.peer_id !== null)
        ? String(data.peer_id) : '';
      if (!pid) return;
      if (data.status === 'unpaired') {
        this.removeInternetPeer(pid);
        return;
      }
      var name = data.name || pid;
      // Upsert: drop any prior row for the same peer, then append the fresh one.
      var list = this.internetPairPeers.filter(function (p) {
        return p.peer_id !== pid;
      });
      list.push({
        peer_id: pid,
        name: name,
        alias: data.alias || '',
        online: !!data.online,
        last_seen: (typeof data.last_seen === 'number') ? data.last_seen : null,
        paired: true,
        status: 'paired',
      });
      this.internetPairPeers = list;
      // The generated code has served its purpose once a peer confirms the
      // pair — drop it so a stale/used code no longer sits in the "your
      // code" box (regenerate if another device still needs to pair).
      this.internetPairCode = '';
      this.showToast(t('settings_window.netpair_paired_toast', { name: name }),
        3000, 'success');
    },

    /**
     * Remove one internet-paired peer from the list (after a local unpair, or
     * a WS `netpair_peer` status:"unpaired" event). No-op when absent.
     * @param {string} peerId
     */
    removeInternetPeer: function (peerId) {
      var pid = String(peerId);
      this.internetPairPeers = this.internetPairPeers.filter(function (p) {
        return String(p.peer_id) !== pid;
      });
    },

    /**
     * Update an internet-paired peer's display name/alias locally after a
     * successful rename so the list reflects the change immediately.
     * @param {string} peerId
     * @param {string} name
     */
    setInternetPeerName: function (peerId, name) {
      var pid = String(peerId);
      var next = [];
      for (var i = 0; i < this.internetPairPeers.length; i++) {
        var p = this.internetPairPeers[i];
        if (String(p.peer_id) === pid) {
          next.push(Object.assign({}, p, {
            name: name || p.name,
            alias: name || p.alias,
          }));
        } else {
          next.push(p);
        }
      }
      this.internetPairPeers = next;
    },

    /**
     * Fetch one internet peer's delivery status (pending count + most recent
     * send result) from GET /api/internetdelivery. Fully defensive: an older
     * backend answers 404 and the fetch settles with loadFailed=true so the
     * device card hides its delivery row instead of spinning. In-flight
     * calls per peer are coalesced.
     * @param {string} peerId
     * @returns {Promise<boolean>} true when a snapshot was applied
     */
    fetchInternetDelivery: function (peerId) {
      var self = this;
      var pid = String(peerId || '');
      if (!pid || !window.ClipsyncAPI || !window.ClipsyncAPI.getInternetDelivery) {
        return Promise.resolve(false);
      }
      this._internetDeliveryInFlight = this._internetDeliveryInFlight || {};
      if (this._internetDeliveryInFlight[pid]) return Promise.resolve(false);
      this._internetDeliveryInFlight[pid] = true;
      var entry = this.internetDelivery[pid] || {
        pending: 0, lastStatus: null, msgStatus: {}, loaded: false, loadFailed: false,
      };
      entry.loaded = false;
      entry.loadFailed = false;
      return window.ClipsyncAPI.getInternetDelivery(pid)
        .then(function (res) {
          var qCount = 0;
          var lastStatus = null;
          var msgStatus = {};
          if (res && Array.isArray(res.sends)) {
            for (var i = 0; i < res.sends.length; i++) {
              var s = res.sends[i];
              // Skip rows missing an id — the card must never count a send
              // it cannot key or later transition.
              if (!s || s.msg_id === undefined || s.msg_id === null) continue;
              var st = (['sent', 'delivered', 'failed', 'queued'].indexOf(s.status) !== -1)
                ? s.status : 'sent';
              if (i === 0) lastStatus = st;          // sends are newest-first
              msgStatus[String(s.msg_id)] = st;
              if (st === 'queued') qCount++;
            }
          }
          // pending is authoritative when present; otherwise derive it from
          // the queued rows so the badge never lies.
          entry.pending = (res && typeof res.pending === 'number') ? res.pending : qCount;
          entry.lastStatus = lastStatus;
          entry.msgStatus = msgStatus;
          entry.loaded = true;
          entry.loadFailed = false;
          self.internetDelivery[pid] = entry;
          return true;
        })
        .catch(function () {
          // 404 (older backend) / network error — settle so the card can
          // hide the row; a later WS delivery event still surfaces live data.
          entry.loaded = true;
          entry.loadFailed = true;
          self.internetDelivery[pid] = entry;
          return false;
        })
        .finally(function () {
          if (self._internetDeliveryInFlight) delete self._internetDeliveryInFlight[pid];
        });
    },

    /**
     * Fold one WS `internet_delivery` event into the delivery state. The
     * event carries {peer_id, msg_id, status}. This:
     *   1. stamps the chat-bubble map (msg_id → status) so an outgoing
     *      relay-chat bubble shows ✓已送达 / ✗未送达 / …发送中;
     *   2. updates the peer's one-line card state (pending "待补发 N" count
     *      deduped by msg_id, plus the most recent status); and
     *   3. for genuine contact events (sent/delivered) refreshes the peer's
     *      "last sync" time so the card never looks stale next to a fresh ✅.
     * Re-broadcasting the same queued status is a no-op (no double count).
     * @param {{peer_id: string, msg_id?: string|number, status: string}} data
     */
    applyInternetDelivery: function (data) {
      if (!data || typeof data !== 'object') return;
      var pid = (data.peer_id === undefined || data.peer_id === null)
        ? '' : String(data.peer_id);
      var status = data.status;
      if (!pid || ['sent', 'delivered', 'failed', 'queued'].indexOf(status) === -1) return;
      var mid = (data.msg_id === undefined || data.msg_id === null)
        ? '' : String(data.msg_id);

      // Chat-bubble stamp map: msg_id → latest status. Capped so a long
      // session never grows it without bound (only the ~200 visible chat
      // bubbles can ever be stamped).
      if (mid) {
        this._internetDeliveryMsgOrder = this._internetDeliveryMsgOrder || [];
        if (this.internetDeliveryMsgs[mid] === undefined) {
          this._internetDeliveryMsgOrder.push(mid);
          if (this._internetDeliveryMsgOrder.length > 250) {
            var oldest = this._internetDeliveryMsgOrder.shift();
            if (oldest !== undefined) delete this.internetDeliveryMsgs[oldest];
          }
        }
        this.internetDeliveryMsgs[mid] = status;
      }

      var entry = this.internetDelivery[pid] || {
        pending: 0, lastStatus: null, msgStatus: {}, loaded: true, loadFailed: false,
      };
      var wasQueued = !!mid && entry.msgStatus[mid] === 'queued';
      if (mid) entry.msgStatus[mid] = status;
      if (status === 'queued') {
        if (!wasQueued) entry.pending = (entry.pending || 0) + 1;
      } else if (wasQueued) {
        entry.pending = Math.max(0, (entry.pending || 0) - 1);
      }
      entry.lastStatus = status;
      this.internetDelivery[pid] = entry;

      // A sent/delivered event means the peer was reachable — refresh the
      // card's "last sync" time so it never looks stale next to a fresh ✅.
      if (status === 'delivered' || status === 'sent') {
        var peers = this.internetPairPeers || [];
        for (var p = 0; p < peers.length; p++) {
          if (String(peers[p].peer_id) === pid) {
            peers[p].last_seen = Math.floor(Date.now() / 1000);
            break;
          }
        }
      }
    },

    /**
     * Record one WS `aiconfig_file` per-file pull result: keep it in the
     * rolling badge list and toast it. Only called by ws.js after it has
     * validated the status vocabulary.
     * @param {{peer_id: string, rel_path: string, status: string}} data
     */
    applyAiConfigFileResult: function (data) {
      var entry = {
        peer_id: (data.peer_id !== undefined && data.peer_id !== null) ? String(data.peer_id) : '',
        tool: data.tool || '',
        rel_path: data.rel_path || '',
        status: data.status,
        reason: data.reason || '',
        ts: Date.now(),
      };
      this.aiConfigResults.unshift(entry);
      if (this.aiConfigResults.length > 50) {
        this.aiConfigResults.splice(50, this.aiConfigResults.length - 50);
      }
      // Fold into the batch tracker so the panel can show "N/M done" and
      // retry only the failures.  A batch entry without batch_id on the WS
      // event (legacy backend) is simply not tracked.
      var batchId = data.batch_id;
      if (batchId && this.aiConfigBatches[batchId]) {
        var batch = this.aiConfigBatches[batchId];
        batch.done += 1;
        batch.results.push(entry);
        if (batch.done >= batch.total) {
          batch.finished = true;
        }
      }
      var key = entry.status === 'error' ? 'aiconfig.result_error'
        : entry.status === 'copied' ? 'aiconfig.result_copied'
        : entry.status === 'appended' ? 'aiconfig.result_appended'
        : 'aiconfig.result_saved';
      this.showToast(t(key, { path: entry.rel_path }), 2800,
        entry.status === 'error' ? 'error' : 'success');
    },

    /**
     * Register a batch pull before POSTing /api/aiconfig/pull so that
     * per-file aiconfig_file WS events (echoing *batch_id*) accumulate into
     * `aiConfigBatches[batch_id]` as `{total, done, results, finished}`.
     * @param {string} batchId non-empty batch id
     * @param {number} total number of files the pull will request
     * @param {string} [peerId] device the pull came from (progress is shown
     *   on that device's view)
     */
    startAiConfigBatch: function (batchId, total, peerId) {
      if (!batchId || !(total > 0)) return;
      this.aiConfigBatches[batchId] = {
        total: total,
        done: 0,
        results: [],
        finished: false,
        peerId: peerId || '',
      };
    },

    /**
     * Open / close the AI-config migration wizard modal.  The wizard reads
     * aiConfigMigrateOpen and resets its internal state when it becomes true.
     */
    openAiConfigMigrate: function () {
      this.aiConfigMigrateOpen = true;
    },
    closeAiConfigMigrate: function () {
      this.aiConfigMigrateOpen = false;
    },

    /**
     * Drop a batch once its pull is complete (or the user dismisses it),
     * so the batch map never grows unbounded.
     */
    clearAiConfigBatch: function (batchId) {
      if (batchId && this.aiConfigBatches[batchId]) {
        delete this.aiConfigBatches[batchId];
      }
    },

  });

  // Expose globally
  window.__CLIPSYNC_STORE__ = store;

})();
