/* ═══════════════════════════════════════════════════════════════════
   ClipSync Status Bar Component
   Bottom bar showing connected device count, sync status, and the
   local device's IP address. Reactively updates from the store.
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

      wsConnected: function () {
        return !!(ClipsyncWS && ClipsyncWS.connected);
      },

      syncStatus: function () {
        return this.wsConnected ? this.t('status.sync_active') : this.t('status.sync_paused');
      },

      localIp: function () {
        // The device entries from /api/devices only carry id/name/connected/
        // paired — they have no `ip` field. The reachable LAN address is
        // tracked on `overview` (populated by fetchOverview), so read it from
        // there instead (with a device `ip` fallback for future use).
        var local = this.store.localDevice();
        return (local && local.ip) || this.store.overview.localIp || '';
      },
    },

    template:
      '<footer class="status-bar">' +
        '<div class="status-bar__left">' +
          '<span class="status-bar__device-count">{{ deviceCountText }}</span>' +
          '<span v-if="store.overview.uptimeSeconds" style="margin-left:12px;font-size:11px;color:var(--clipsync-fg-muted)">' +
            '{{ t(\'status.uptime\', { time: store.formatUptime(store.overview.uptimeSeconds) }) }}' +
          '</span>' +
        '</div>' +
        '<div class="status-bar__center">' +
          '<span class="status-bar__sync">' +
            '<span class="status-bar__sync-dot" :class="{ \'status-bar__sync-dot--active\': wsConnected }"></span>' +
            '{{ syncStatus }}' +
          '</span>' +
        '</div>' +
        '<div class="status-bar__right">' +
          '<span v-if="localIp" class="status-bar__ip text-ellipsis">{{ localIp }}</span>' +
        '</div>' +
      '</footer>',
  };

})();
