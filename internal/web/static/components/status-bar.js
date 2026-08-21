/* ═══════════════════════════════════════════════════════════════════
   ClipSync Status Bar Component
   Bottom bar with live system telemetry: connected/paired/history
   counts, sync status, network, and local IP. Reactively updates from
   the store.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['status-bar'] = {
    inject: ['store'],

    computed: {
      deviceCountText: function () {
        var count = this.store.connectedCount;
        if (count === 0) {
          return '🔴 ' + this.t('status.no_devices');
        }
        return '🟢 ' + this.t('status.devices_connected', { count: count });
      },

      pairedText: function () {
        return '🔗 ' + this.store.overview.pairedCount + ' ' + this.t('overview.paired');
      },

      historyText: function () {
        return '📋 ' + this.store.overview.historyCount + ' ' + this.t('overview.history');
      },

      networkText: function () {
        var nt = this.store.overview.networkType;
        var nd = this.store.overview.networkDetail;
        var base;
        if (nt === 'wifi') base = 'Wi-Fi';
        else if (nt === 'ethernet') base = this.t('network.ethernet');
        else base = this.t('network.lan') || 'LAN';
        return nd ? base + ' · ' + nd : base;
      },

      wsConnected: function () {
        return !!(ClipsyncWS && ClipsyncWS.connected);
      },

      syncStatus: function () {
        // Same source + wording as the overview panel's status bar so the
        // top and bottom never disagree about sync state.
        return this.store.overview.syncEnabled ? this.t('sync.active') : this.t('sync.paused');
      },

      localIp: function () {
        var local = this.store.localDevice();
        return (local && local.ip) || this.store.overview.localIp || '';
      },
    },

    template:
      '<footer class="status-bar">' +
        '<div class="status-bar__left">' +
          '<span class="status-bar__stat status-bar__stat--accent">{{ deviceCountText }}</span>' +
          '<span class="status-bar__sep status-bar__hide-narrow">·</span>' +
          '<span class="status-bar__stat status-bar__hide-narrow">{{ pairedText }}</span>' +
          '<span class="status-bar__sep status-bar__hide-narrow">·</span>' +
          '<span class="status-bar__stat status-bar__hide-narrow">{{ historyText }}</span>' +
          '<span v-if="store.overview.uptimeSeconds" class="status-bar__sep">·</span>' +
          '<span v-if="store.overview.uptimeSeconds" class="status-bar__stat status-bar__uptime">' +
            '⏱ {{ store.formatUptime(store.overview.uptimeSeconds) }}' +
          '</span>' +
        '</div>' +
        '<div class="status-bar__center">' +
          '<span class="status-bar__sync">' +
            '<span class="status-bar__sync-dot" :class="{ \'status-bar__sync-dot--active\': store.overview.syncEnabled }"></span>' +
            '{{ syncStatus }}' +
          '</span>' +
        '</div>' +
        '<div class="status-bar__right">' +
          '<span class="status-bar__stat">{{ networkText }}</span>' +
          '<span v-if="localIp" class="status-bar__sep">·</span>' +
          '<span v-if="localIp" class="status-bar__ip">{{ localIp }}</span>' +
        '</div>' +
      '</footer>',
  };

})();
