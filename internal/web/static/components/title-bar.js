/* ═══════════════════════════════════════════════════════════════════
   ClipSync Title Bar Component
   Top bar with ClipSync branding, a search input with debounced
   filtering, a theme toggle button, and a settings placeholder.
   Uses glass-morphism background styling.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['title-bar'] = {
    inject: ['store'],

    data: function () {
      return {
        searchInput: this.store.historySearch || '',
        _searchTimer: null,
      };
    },

    watch: {
      // Keep the search box in sync when the search is restored after a
      // refresh (children mount before the root restores it, so the initial
      // data() copy can be stale).
      'store.historySearch': function (val) {
        this.searchInput = val || '';
      },
    },

    computed: {
      themeLabel: function () {
        var t = this.store.theme;
        if (t === 'dark') return '🌙';
        if (t === 'light') return '☀️';
        return '🖥️'; // system — monitor reads as "follow the OS", not refresh
      },

      themeTitle: function () {
        var t = this.store.theme;
        if (t === 'dark') return this.t('web.dark_mode');
        if (t === 'light') return this.t('web.light_mode');
        return this.t('web.system_mode');
      },
    },

    template:
      '<header class="title-bar glass" style="-webkit-app-region:drag">' +
        '<div class="title-bar__brand" style="-webkit-app-region:drag">' +
          '<span class="title-bar__logo">' +
            '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
              '<rect x="5" y="4" width="14" height="17" rx="2" ry="2"/>' +
              '<line x1="9" y1="2" x2="9" y2="6"/>' +
              '<line x1="15" y1="2" x2="15" y2="6"/>' +
              '<line x1="9" y1="10" x2="15" y2="10"/>' +
              '<line x1="9" y1="14" x2="15" y2="14"/>' +
            '</svg>' +
          '</span>' +
          '<h1 class="title-bar__title">{{ t(\'web.app_name\') }}</h1>' +
          '<span style="font-size:11px;color:var(--clipsync-fg-muted);margin-left:8px">· {{ store.deviceName }}</span>' +
        '</div>' +
        '<div class="title-bar__search" style="-webkit-app-region:no-drag">' +
          '<span class="title-bar__search-icon">&#128269;</span>' +
          '<input' +
            ' type="text"' +
            ' class="title-bar__search-input"' +
            ' :placeholder="t(\'ui.search\')"' +
            ' :value="searchInput"' +
            ' @input="onSearchInput"' +
            ' autocomplete="off"' +
            ' spellcheck="false"' +
          '>' +
          '<button' +
            ' v-if="searchInput"' +
            ' class="title-bar__search-clear"' +
            ' @click="clearSearch"' +
            ' :title="t(\'ui.cancel\')"' +
          '>&#10006;&#65039;</button>' +
        '</div>' +
        '<div class="title-bar__actions" style="-webkit-app-region:no-drag">' +
          '<button' +
            ' class="title-bar__action-btn"' +
            ' @click="cycleTheme"' +
            ' :title="themeTitle"' +
            ' :aria-label="themeTitle"' +
          '>{{ themeLabel }}</button>' +
          '<button' +
            ' class="title-bar__action-btn"' +
            ' :title="t(\'ui.refresh\')"' +
            ' :aria-label="t(\'ui.refresh\')"' +
            ' @click="refreshData"' +
          '>&#128260;</button>' +
          '<button' +
            ' class="title-bar__action-btn"' +
            ' :title="t(\'ui.settings\')"' +
            ' :aria-label="t(\'ui.settings\')"' +
            ' @click="openSettings"' +
          '>&#9881;&#65039;</button>' +
          <!-- Native browser window chrome provides minimize/close buttons in app mode -->
        '</div>' +
      '</header>',

    methods: {
      onSearchInput: function (e) {
        var val = e.target.value;
        this.searchInput = val;

        var self = this;
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(function () {
          self.store.historySearch = val;
          // Auto-switch to history tab when searching
          if (val && self.store.activeTab !== 'history') {
            self.store.activeTab = 'history';
          }
        }, 200);
      },

      clearSearch: function () {
        this.searchInput = '';
        this.store.historySearch = '';
        clearTimeout(this._searchTimer);
      },

      refreshData: function () {
        var self = this;
        // Keep the user's place across the reload.
        try {
          sessionStorage.setItem('clipsync_ui_state', JSON.stringify({
            activeTab: this.store.activeTab,
            historySearch: this.store.historySearch,
          }));
        } catch (e) { /* ignore */ }

        var doReload = function () { window.location.reload(true); };

        // Genuine hard refresh like Ctrl+Shift+R: the service worker serves
        // JS/CSS cache-first, so a normal reload can keep showing stale UI.
        // Drop the shell cache so updated assets are re-fetched, then reload
        // the whole page. While offline, keep the cached shell and fall back
        // to a plain data refresh instead of wiping the offline cache.
        if (window.caches && window.caches.keys && navigator.onLine !== false) {
          window.caches.keys()
            .then(function (names) {
              return Promise.all(names.map(function (n) { return window.caches.delete(n); }));
            })
            .then(doReload)
            .catch(doReload);
        } else {
          if (this.$root && typeof this.$root.loadData === 'function') {
            this.$root.loadData().catch(function () {});
          }
          this.store.fetchOverview();
          this.store.showToast(this.t('ui.refreshed'), 1500);
        }
      },

      cycleTheme: function () {
        var current = this.store.theme;
        var next;
        if (current === 'system') {
          next = 'light';
        } else if (current === 'light') {
          next = 'dark';
        } else {
          next = 'system';
        }
        this.store.setTheme(next);
      },

      openSettings: function () {
        this.store.openSettingsPanel();
      },
    },
  };

})();
