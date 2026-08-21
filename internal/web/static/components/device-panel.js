/* ═══════════════════════════════════════════════════════════════════
   ClipSync Device Panel Component
   Sections: This Device, Connected, Offline, Discovered, Pairing Requests.
   Uses device-card component with action buttons and notes editing.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['device-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        refreshing: false,
        pairingResponding: null,
      };
    },

    computed: {
      localDev: function () {
        return this.store.localDevice();
      },

      allRemoteDevices: function () {
        var selfId = this.store.deviceId;
        return this.store.devices.filter(function (d) {
          return d.device_id !== selfId;
        });
      },

      onlineRemoteDevices: function () {
        return this.allRemoteDevices.filter(function (d) { return d.connected; });
      },

      pairedOfflineDevices: function () {
        return this.allRemoteDevices.filter(function (d) { return d.paired && !d.connected; });
      },

      discoveredDevices: function () {
        return this.allRemoteDevices.filter(function (d) { return !d.paired && !d.connected; });
      },

      pairingRequests: function () {
        return this.store.pairingRequests || [];
      },
    },

    template:
      '<div class="device-panel">' +
        '<div class="device-panel__header">' +
          '<h2 class="device-panel__title">{{ t(\'devices.title\') }}</h2>' +
          '<button class="btn-ghost" @click="refresh" :disabled="refreshing">' +
            '<span :class="{ \'animate-spin\': refreshing }">&#128260;</span> {{ t(\'ui.refresh\') }}' +
          '</button>' +
        '</div>' +

        '<div v-if="store.loading" class="device-panel__loading">' +
          '<div class="skeleton-card animate-shimmer" v-for="n in 2" :key="n"></div>' +
        '</div>' +

        '<template v-else>' +

          '<!-- Pairing Requests -->' +
          '<div v-if="pairingRequests.length > 0" class="device-panel__section">' +
            '<div class="section-header" style="color:var(--clipsync-warning)">' +
              '🔐 {{ t(\'devices.pairing_requests\') }}' +
              '<span class="section-header__badge">{{ pairingRequests.length }}</span>' +
            '</div>' +
            '<div v-for="pr in pairingRequests" :key="pr.peer_id" class="pairing-request-card">' +
              '<div class="pairing-request-card__info">' +
                '<span class="pairing-request-card__name">{{ pr.peer_name || pr.device_name || pr.peer_id }}</span>' +
                '<span class="pairing-request-card__id">{{ pr.peer_id }}</span>' +
                '<span class="pairing-request-card__code">{{ t(\'ui.pairing_code\', {code: pr.code}) }}</span>' +
              '</div>' +
              '<div class="pairing-request-card__actions">' +
                '<button class="device-card__action device-card__action--accent" @click="acceptPairing(pr)" :disabled="pairingResponding === pr.peer_id">' +
                  '{{ pairingResponding === pr.peer_id ? \'...\' : t(\'ui.confirm\') }}' +
                '</button>' +
                '<button class="device-card__action device-card__action--danger" @click="rejectPairing(pr)" :disabled="pairingResponding === pr.peer_id">' +
                  '{{ t(\'ui.reject\') }}' +
                '</button>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<!-- This Device -->' +
          '<div v-if="localDev" class="device-panel__section">' +
            '<div class="section-header">🖥 {{ t(\'devices.this_device\') }}</div>' +
            '<device-card :device="localDev"></device-card>' +
          '</div>' +

          '<!-- Connected -->' +
          '<div v-if="onlineRemoteDevices.length > 0" class="device-panel__section">' +
            '<div class="section-header">' +
              '🟢 {{ t(\'device.connected\') }}' +
              '<span class="section-header__badge">{{ onlineRemoteDevices.length }}</span>' +
            '</div>' +
            '<device-card v-for="dev in onlineRemoteDevices" :key="dev.device_id" :device="dev"></device-card>' +
          '</div>' +

          '<!-- Paired Offline -->' +
          '<div v-if="pairedOfflineDevices.length > 0" class="device-panel__section">' +
            '<div class="section-header">' +
              '🟠 {{ t(\'device.paired_offline\') }}' +
              '<span class="section-header__badge section-header__badge--muted">{{ pairedOfflineDevices.length }}</span>' +
            '</div>' +
            '<device-card v-for="dev in pairedOfflineDevices" :key="dev.device_id" :device="dev"></device-card>' +
          '</div>' +

          '<!-- Discovered -->' +
          '<div v-if="discoveredDevices.length > 0" class="device-panel__section">' +
            '<div class="section-header">' +
              '🔍 {{ t(\'device.discovered\') }}' +
              '<span class="section-header__badge section-header__badge--muted">{{ discoveredDevices.length }}</span>' +
            '</div>' +
            '<device-card v-for="dev in discoveredDevices" :key="dev.device_id" :device="dev"></device-card>' +
          '</div>' +

          '<!-- Empty -->' +
          '<div v-if="allRemoteDevices.length === 0 && pairingRequests.length === 0" class="panel-empty">' +
            '<span class="panel-empty-icon">📡</span>' +
            '<p class="panel-empty-title">{{ t(\'devices.no_devices_found\') }}</p>' +
            '<p class="panel-empty-desc">{{ t(\'devices.auto_discover_hint\') }}</p>' +
          '</div>' +
        '</template>' +
      '</div>',

    methods: {
      refresh: function () {
        var self = this;
        this.refreshing = true;
        ClipsyncAPI.getDevices()
          .then(function (res) {
            if (res && res.devices) {
              self.store.devices = res.devices;
            }
            // Polling fallback for pending pairings (the WS push is dropped
            // when no web client is attached, so refresh must re-sync them).
            if (res && res.pending_pairings) {
              self.store.syncPairingRequests(res.pending_pairings);
            }
          })
          .catch(function () {})
          .finally(function () {
            self.refreshing = false;
          });
      },

      acceptPairing: function (pr) {
        this.pairingResponding = pr.peer_id;
        var self = this;
        ClipsyncAPI.sendPairingResponse(pr.peer_id, 'confirm', pr.code || '')
          .then(function () {
            var idx = self.store.pairingRequests.findIndex(function (r) {
              return r.peer_id === pr.peer_id;
            });
            if (idx !== -1) self.store.pairingRequests.splice(idx, 1);
            self.store.showToast(self.t('device.pairing_confirmed'), 2000);
            // Refresh the device list so the newly paired device shows its
            // connected/paired state right away instead of staying stale
            // until the next broadcast.
            self.refresh();
          })
          .catch(function () {
            self.store.showToast(self.t('device.pairing_failed'), 2000);
          })
          .finally(function () {
            self.pairingResponding = null;
          });
      },

      rejectPairing: function (pr) {
        this.pairingResponding = pr.peer_id;
        var self = this;
        ClipsyncAPI.sendPairingResponse(pr.peer_id, 'reject', '')
          .then(function () {
            var idx = self.store.pairingRequests.findIndex(function (r) {
              return r.peer_id === pr.peer_id;
            });
            if (idx !== -1) self.store.pairingRequests.splice(idx, 1);
          })
          .catch(function () {})
          .finally(function () {
            self.pairingResponding = null;
          });
      },
    },
  };

})();
