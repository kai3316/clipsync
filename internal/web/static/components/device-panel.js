/* ═══════════════════════════════════════════════════════════════════
   ClipSync Device Panel Component
   Sections (top→bottom): This Device, Internet pairing, Connected, Offline,
   Discovered, Pairing Requests.
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
        // True when the last device-list fetch rejected — distinguishes
        // "loaded and empty" from "could not load at all".
        loadFailed: false,

        // Internet pairing (round 15): transient UI bits. The peer list and
        // generated code live in the store (kept live by WS + status fetch).
        netpairLoading: false,
        netpairGenerating: false,
        netpairCodeInput: '',
        netpairConfirming: false,
        netpairError: '',
        netpairBusy: false,       // a rename/unpair request is in flight
        _netpairLoadInFlight: false,
        netpairClockTimer: null,  // refreshes relative "last sync" times
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

      // ── Internet pairing (round 15) mirrors of store state ───────

      netpairPeers: function () {
        return this.store.internetPairPeers || [];
      },

      // The overview "已配 {N} 台互联网设备" count. A peer whose `paired`
      // flag is explicitly false is a stale/in-progress row, not counted.
      netpairPairedCount: function () {
        var count = 0;
        (this.store.internetPairPeers || []).forEach(function (p) {
          if (p.paired !== false) count++;
        });
        return count;
      },

      netpairGeneratedCode: function () {
        return this.store.internetPairCode || '';
      },

      // The generated code grouped for display: ABCD-EFGH-IJKL.
      netpairDisplayCode: function () {
        var raw = this.netpairGeneratedCode || '';
        var clean = raw.toUpperCase().replace(/[^A-Z0-9]/g, '');
        if (!clean) return '';
        return clean.match(/.{1,4}/g).join('-');
      },

      // Internet-sync toggle + relay state, shared semantics with the
      // settings panel so the overview line means the same thing.
      internetSyncEnabled: function () {
        var cache = this.store.settingsCache || {};
        return !!cache.internet_sync_enabled;
      },

      effectiveRelayState: function () {
        if (this.store.relayState) return this.store.relayState;
        var cache = this.store.settingsCache || {};
        return cache.internet_sync_state || 'off';
      },

      relayDisplayState: function () {
        var state = this.effectiveRelayState || 'off';
        if (this.internetSyncEnabled && state === 'off') return 'initial';
        return state;
      },

      relayStateKey: function () {
        return 'relay.state.' + (this.relayDisplayState || 'off');
      },

      relayStateColor: function () {
        switch (this.relayDisplayState) {
          case 'online': return 'var(--clipsync-success)';
          case 'connecting': return 'var(--clipsync-warning)';
          case 'initial': return 'var(--clipsync-warning)';
          case 'error': return 'var(--clipsync-danger)';
          default: return 'var(--clipsync-fg-muted)';
        }
      },

      // The local device's own internet badge: only meaningful when the relay
      // is actually online (green). relay offline → no badge at all.
      localInternetOnline: function () {
        return this.internetSyncEnabled && this.effectiveRelayState === 'online';
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

        '<!-- This Device (always at the very top) -->' +
        '<div v-if="localDev" class="device-panel__section">' +
          '<div class="section-header">💻 {{ t(\'devices.this_device\') }}' +
            '<span v-if="localInternetOnline" class="netpair-local-badge" :title="t(\'devices.netpair_also_internet\')">🌐 {{ t(\'devices.netpair_online\') }}</span>' +
          '</div>' +
          '<device-card :device="localDev"></device-card>' +
        '</div>' +

        '<!-- Internet pairing (round 15) -->' +
        '<div class="device-panel__section netpair-section">' +
          '<div class="section-header">' +
            '🌐 {{ t(\'devices.netpair_title\') }}' +
            '<span class="section-header__badge">{{ netpairPairedCount }}</span>' +
          '</div>' +

          '<div class="netpair-overview">' +
            '<span class="netpair-overview__dot" :style="{ background: relayStateColor }"></span>' +
            '<span class="netpair-overview__state">{{ t(relayStateKey) }}</span>' +
            '<span class="netpair-overview__count">{{ t(\'devices.netpair_overview_paired\', { count: netpairPairedCount }) }}</span>' +
          '</div>' +
          '<div class="netpair-privacy">🔒 {{ t(\'settings_window.internet_sync_hint\') }}</div>' +

          '<div v-if="netpairLoading" class="netpair-loading">{{ t(\'ui.loading\') }}</div>' +

          '<template v-else>' +
            '<!-- Empty state + three-step guide -->' +
            '<div v-if="netpairPairedCount === 0" class="netpair-empty card">' +
              '<div class="netpair-empty__title">{{ t(\'devices.netpair_empty_title\') }}</div>' +
              '<ol class="netpair-steps">' +
                '<li class="netpair-step">' +
                  '<span class="netpair-step__icon">{{ internetSyncEnabled ? \'✓\' : \'①\' }}</span>' +
                  '<span class="netpair-step__text">{{ internetSyncEnabled ? t(\'devices.netpair_step1_done\') : t(\'devices.netpair_step1\') }}</span>' +
                  '<button v-if="!internetSyncEnabled" class="btn-ghost netpair-step__link" @click="openInternetSyncSettings">{{ t(\'devices.netpair_go_settings\') }}</button>' +
                '</li>' +
                '<li class="netpair-step"><span class="netpair-step__icon">②</span><span class="netpair-step__text">{{ t(\'devices.netpair_step2\') }}</span></li>' +
                '<li class="netpair-step"><span class="netpair-step__icon">③</span><span class="netpair-step__text">{{ t(\'devices.netpair_step3\') }}</span></li>' +
              '</ol>' +
            '</div>' +

            '<!-- Generate + enter a pairing code -->' +
            '<div class="netpair-generate">' +
              '<button class="btn-ghost" @click="generateNetpairCode" :disabled="netpairGenerating">' +
                '{{ netpairGenerating ? \'...\' : (netpairGeneratedCode ? t(\'devices.netpair_regenerate\') : t(\'devices.netpair_generate\')) }}' +
              '</button>' +
              '<template v-if="netpairGeneratedCode">' +
                '<div class="netpair-code">' +
                  '<code class="netpair-code__value selectable">{{ netpairDisplayCode }}</code>' +
                  '<button class="btn-ghost" @click="copyNetpairCode">{{ t(\'ui.copy\') }}</button>' +
                '</div>' +
                '<span class="netpair-hint">{{ t(\'devices.netpair_code_valid_hint\') }}</span>' +
              '</template>' +
            '</div>' +

            '<div class="netpair-enter">' +
              '<div class="netpair-enter__row">' +
                '<input type="text" class="netpair-enter__input" v-model="netpairCodeInput" spellcheck="false" autocomplete="off"' +
                  ' :placeholder="t(\'devices.netpair_enter_title\')"' +
                  ' :aria-label="t(\'devices.netpair_enter_title\')"' +
                  ' @input="onNetpairCodeInput" @keydown.enter.prevent="confirmNetpairCode">' +
                '<button class="btn-ghost" @click="confirmNetpairCode" :disabled="netpairConfirming">' +
                  '{{ netpairConfirming ? \'...\' : t(\'devices.netpair_confirm\') }}' +
                '</button>' +
              '</div>' +
              '<span v-if="netpairError" class="netpair-error">{{ netpairError }}</span>' +
            '</div>' +

            '<!-- Paired-over-internet device list -->' +
            '<div v-if="netpairPeers.length > 0" class="netpair-peers">' +
              '<div class="netpair-peers__title">{{ t(\'devices.netpair_paired_list\') }}</div>' +
              '<div v-for="peer in netpairPeers" :key="peer.peer_id" class="netpair-peer card">' +
                '<div class="netpair-peer__info">' +
                  '<span class="netpair-peer__name text-ellipsis">{{ peerDisplayName(peer) }}</span>' +
                  '<span class="netpair-peer__id text-mono selectable">{{ shortId(peer.peer_id) }}</span>' +
                '</div>' +
                '<div class="netpair-peer__status">' +
                  '<span class="netpair-peer__dot" :class="peer.online ? \'netpair-peer__dot--online\' : \'netpair-peer__dot--offline\'"></span>' +
                  '<span class="netpair-peer__state">{{ peer.online ? t(\'devices.netpair_online\') : t(\'devices.netpair_offline\') }}</span>' +
                  '<span class="netpair-peer__last-seen">· {{ lastSeenText(peer) }}</span>' +
                '</div>' +
                '<div class="netpair-peer__actions">' +
                  '<button class="btn-ghost" @click="renamePeer(peer)" :disabled="netpairBusy">{{ t(\'devices.netpair_rename\') }}</button>' +
                  '<button class="btn-ghost btn-danger" @click="unpairPeer(peer)" :disabled="netpairBusy">{{ t(\'devices.netpair_unpair\') }}</button>' +
                '</div>' +

                '<!-- One-line delivery status (round 17): queued badge + last result -->' +
                '<div v-if="deliveryShown(peer)" class="netpair-peer__delivery">' +
                  '<span v-if="deliveryPending(peer) > 0" class="netpair-delivery-badge netpair-delivery-badge--pending" :title="t(\'delivery.offline_retry_hint\')">' +
                    '{{ t(\'delivery.pending_badge\', { count: deliveryPending(peer) }) }}' +
                  '</span>' +
                  '<span v-if="deliveryLastStatus(peer)" class="netpair-delivery-result" :class="deliveryLastClass(peer)">' +
                    '{{ deliveryLastIcon(peer) }} {{ t(deliveryLastKey(peer)) }}' +
                  '</span>' +
                '</div>' +
              '</div>' +
            '</div>' +
          '</template>' +
        '</div>' +

        '<div v-if="store.loading" class="device-panel__loading">' +
          '<div class="skeleton-card animate-shimmer" v-for="n in 2" :key="n"></div>' +
        '</div>' +

        '<template v-else>' +

          '<!-- Connected -->' +
          '<div v-if="onlineRemoteDevices.length > 0" class="device-panel__section">' +
            '<div class="section-header">' +
              '🟢 {{ t(\'device.connected\') }}' +
              '<span class="section-header__badge">{{ onlineRemoteDevices.length }}</span>' +
            '</div>' +
            '<div v-for="dev in onlineRemoteDevices" :key="dev.device_id" class="device-internet-wrap">' +
              '<span v-if="netpairPeerFor(dev.device_id)" class="netpair-card-badge" :class="netpairPeerFor(dev.device_id).online ? \'netpair-card-badge--online\' : \'netpair-card-badge--offline\'" :title="t(\'devices.netpair_also_internet\')">🌐 {{ netpairPeerFor(dev.device_id).online ? t(\'devices.netpair_online\') : t(\'devices.netpair_offline\') }}</span>' +
              '<device-card :device="devWithAlias(dev)"></device-card>' +
            '</div>' +
          '</div>' +

          '<!-- Paired Offline -->' +
          '<div v-if="pairedOfflineDevices.length > 0" class="device-panel__section">' +
            '<div class="section-header">' +
              '🟠 {{ t(\'device.paired_offline\') }}' +
              '<span class="section-header__badge section-header__badge--muted">{{ pairedOfflineDevices.length }}</span>' +
            '</div>' +
            '<div v-for="dev in pairedOfflineDevices" :key="dev.device_id" class="device-internet-wrap">' +
              '<span v-if="netpairPeerFor(dev.device_id)" class="netpair-card-badge" :class="netpairPeerFor(dev.device_id).online ? \'netpair-card-badge--online\' : \'netpair-card-badge--offline\'" :title="t(\'devices.netpair_also_internet\')">🌐 {{ netpairPeerFor(dev.device_id).online ? t(\'devices.netpair_online\') : t(\'devices.netpair_offline\') }}</span>' +
              '<device-card :device="devWithAlias(dev)"></device-card>' +
            '</div>' +
          '</div>' +

          '<!-- Discovered -->' +
          '<div v-if="discoveredDevices.length > 0" class="device-panel__section">' +
            '<div class="section-header">' +
              '🔍 {{ t(\'device.discovered\') }}' +
              '<span class="section-header__badge section-header__badge--muted">{{ discoveredDevices.length }}</span>' +
            '</div>' +
            '<div v-for="dev in discoveredDevices" :key="dev.device_id" class="device-internet-wrap">' +
              '<span v-if="netpairPeerFor(dev.device_id)" class="netpair-card-badge" :class="netpairPeerFor(dev.device_id).online ? \'netpair-card-badge--online\' : \'netpair-card-badge--offline\'" :title="t(\'devices.netpair_also_internet\')">🌐 {{ netpairPeerFor(dev.device_id).online ? t(\'devices.netpair_online\') : t(\'devices.netpair_offline\') }}</span>' +
              '<device-card :device="devWithAlias(dev)"></device-card>' +
            '</div>' +
          '</div>' +

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
                '<span class="pairing-request-card__code">{{ t(\'ui.pairing_code_label\') }} <strong>{{ formattedCode(pr) }}</strong></span>' +
                '<span v-if="pr.sas" class="pairing-request-card__code">&#128737; {{ t(\'devices.sas_label\') }} <strong style="letter-spacing:1px">{{ pr.sas }}</strong></span>' +
                '<span v-if="pr.sas" class="pairing-request-card__hint">{{ t(\'devices.sas_verify_hint\') }}</span>' +
                '<span class="pairing-request-card__hint">{{ pairingHint(pr) }}</span>' +
                '<span class="pairing-request-card__hint">{{ t(\'devices.pairing_expiry_hint\') }}</span>' +
              '</div>' +
              '<div class="pairing-request-card__actions">' +
                '<button v-if="pr.status !== \'confirmed_waiting\'" class="device-card__action device-card__action--accent" @click="acceptPairing(pr)" :disabled="pairingResponding === pr.peer_id">' +
                  '{{ pairingResponding === pr.peer_id ? \'...\' : t(\'ui.confirm\') }}' +
                '</button>' +
                '<span v-else class="pairing-request-card__waiting">⏳ {{ t(\'pairing.state.confirmed_waiting\') }}</span>' +
                '<button class="device-card__action device-card__action--danger" @click="rejectPairing(pr)" :disabled="pairingResponding === pr.peer_id">' +
                  '{{ pairingResponding === pr.peer_id ? \'...\' : t(\'ui.reject\') }}' +
                '</button>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<!-- Load failed -->' +
          '<div v-if="loadFailed || store.devicesLoadFailed" class="panel-empty">' +
            '<span class="panel-empty-icon">⚠️</span>' +
            '<p class="panel-empty-title">{{ t(\'devices.load_failed\') }}</p>' +
            '<p class="panel-empty-desc">{{ t(\'web.error\') }}</p>' +
            '<button class="btn-ghost" @click="refresh" :disabled="refreshing">' +
              '{{ refreshing ? \'...\' : t(\'common.retry\') }}' +
            '</button>' +
          '</div>' +

          '<!-- Empty: loaded and genuinely no devices -->' +
          '<div v-else-if="allRemoteDevices.length === 0 && pairingRequests.length === 0" class="panel-empty">' +
            '<span class="panel-empty-icon">📡</span>' +
            '<p class="panel-empty-title">{{ t(\'devices.no_devices_found\') }}</p>' +
            '<p class="panel-empty-desc">{{ t(\'devices.auto_discover_hint\') }}</p>' +
          '</div>' +
        '</template>' +

        '<!-- AI-config device inventories (round 12, relocated here round 18) -->' +
        '<aiconfig-device-panel></aiconfig-device-panel>' +
      '</div>',

    methods: {
      // ── Internet pairing (round 15) ──────────────────────────────

      // Refresh the paired-over-internet list + generated code. The store
      // method is fully defensive (older backend → empty state, no throw).
      loadNetpairState: function () {
        var self = this;
        if (this._netpairLoadInFlight) return;
        this._netpairLoadInFlight = true;
        this.netpairLoading = true;
        this.store.fetchInternetPairStatus().finally(function () {
          self.netpairLoading = false;
          self._netpairLoadInFlight = false;
          // Seed each peer card's one-line delivery status (queued badge +
          // last send result) once the paired list is known.
          self.loadAllDeliveries();
        });
      },

      // ── Internet delivery status (round 17) ────────────────────────

      // Fetch delivery data for every paired peer (offline-retry pending
      // counts + most recent send result). Defensive: a backend without the
      // endpoint settles silently and the card simply shows no delivery row.
      loadAllDeliveries: function () {
        var self = this;
        (this.store.internetPairPeers || []).forEach(function (peer) {
          self.store.fetchInternetDelivery(peer.peer_id);
        });
      },

      // The normalized delivery state for a peer (null when the backend
      // hasn't reported any — hides the whole row).
      deliveryFor: function (peer) {
        if (!peer || !peer.peer_id) return null;
        return this.store.internetDelivery[String(peer.peer_id)] || null;
      },

      // Whether the one-line delivery row should render at all: only when
      // data was actually loaded AND there is something to say (a queued
      // count or a last send result). "Nothing to show" and "backend not
      // ready" both hide the row.
      deliveryShown: function (peer) {
        var d = this.deliveryFor(peer);
        if (!d || !d.loaded || d.loadFailed) return false;
        return (d.pending > 0) || !!d.lastStatus;
      },

      deliveryPending: function (peer) {
        var d = this.deliveryFor(peer);
        return (d && typeof d.pending === 'number') ? d.pending : 0;
      },

      deliveryLastStatus: function (peer) {
        var d = this.deliveryFor(peer);
        var st = d && d.lastStatus;
        return (['sent', 'delivered', 'failed', 'queued'].indexOf(st) !== -1) ? st : null;
      },

      deliveryLastClass: function (peer) {
        switch (this.deliveryLastStatus(peer)) {
          case 'delivered': return 'netpair-delivery-result--ok';
          case 'failed': return 'netpair-delivery-result--err';
          case 'queued': return 'netpair-delivery-result--queued';
          default: return 'netpair-delivery-result--sending';
        }
      },

      deliveryLastIcon: function (peer) {
        switch (this.deliveryLastStatus(peer)) {
          case 'delivered': return '✅';
          case 'failed': return '❌';
          default: return '⏳';
        }
      },

      deliveryLastKey: function (peer) {
        var st = this.deliveryLastStatus(peer) || 'sent';
        return 'delivery.' + (st === 'failed' ? 'not_delivered' : st === 'sent' ? 'sending' : st);
      },

      generateNetpairCode: function () {
        var self = this;
        if (this.netpairGenerating) return;
        this.netpairGenerating = true;
        this.netpairError = '';
        ClipsyncAPI.generateInternetPair()
          .then(function (res) {
            self.netpairGenerating = false;
            if (res && res.ok && res.code) {
              self.store.internetPairCode = String(res.code);
            } else {
              self.store.showToast(self.t('dialog.failed'), 2000);
            }
          })
          .catch(function () {
            self.netpairGenerating = false;
            self.store.showToast(self.t('dialog.failed'), 2000);
          });
      },

      copyNetpairCode: function () {
        var code = this.netpairDisplayCode;
        if (!code) return;
        var self = this;
        var done = function () {
          self.store.showToast(self.t('devices.netpair_code_copied'), 2000);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(code).then(done).catch(done);
        } else {
          var textarea = document.createElement('textarea');
          textarea.value = code;
          textarea.style.position = 'fixed';
          textarea.style.opacity = '0';
          document.body.appendChild(textarea);
          textarea.select();
          try { document.execCommand('copy'); } catch (e) { /* ignore */ }
          document.body.removeChild(textarea);
          done();
        }
      },

      // Keep the code input to exactly 12 A-Z0-9 chars, ignoring spaces and
      // lower-casing as the user types (12-char alphanumeric pairing code).
      onNetpairCodeInput: function () {
        this.netpairCodeInput = (this.netpairCodeInput || '')
          .toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 12);
      },

      confirmNetpairCode: function () {
        var self = this;
        var code = (this.netpairCodeInput || '')
          .toUpperCase().replace(/[^A-Z0-9]/g, '');
        if (code.length !== 12) {
          this.netpairError = this.t('devices.netpair_error_format');
          return;
        }
        // Pairing over the internet is meaningless while the relay is off.
        if (!this.internetSyncEnabled) {
          this.netpairError = this.t('devices.netpair_error_sync_off');
          return;
        }
        this.netpairError = '';
        this.netpairConfirming = true;
        ClipsyncAPI.enterInternetPair(code)
          .then(function (res) {
            self.netpairConfirming = false;
            if (res && res.ok) {
              self.netpairCodeInput = '';
              var peerId = res.peer_id;
              // Refresh the list, then toast with the peer's name once known
              // (fall back to its id on a degraded backend).
              self.store.fetchInternetPairStatus().finally(function () {
                var name = peerId;
                var list = self.store.internetPairPeers || [];
                for (var i = 0; i < list.length; i++) {
                  if (String(list[i].peer_id) === String(peerId)) {
                    name = list[i].name || peerId;
                    break;
                  }
                }
                self.store.showToast(
                  self.t('settings_window.netpair_paired_toast', { name: name }),
                  3000, 'success');
              });
            } else {
              self.netpairError = self.t('devices.netpair_error_invalid');
            }
          })
          .catch(function (e) {
            self.netpairConfirming = false;
            if (e && e.status === 400) {
              // Distinguish "paired with yourself" from a generic bad code.
              var reason = (e.data && e.data.error) || '';
              if (/self|own|same/i.test(reason)) {
                self.netpairError = self.t('devices.netpair_error_self');
              } else {
                self.netpairError = self.t('devices.netpair_error_invalid');
              }
            } else {
              self.netpairError = self.t('dialog.failed');
            }
          });
      },

      // ✏️ Rename an internet-paired peer. Prompt is pre-filled with the
      // current alias/name; on success the list updates immediately.
      renamePeer: function (peer) {
        var self = this;
        var current = peer.alias || peer.name || '';
        this.store.prompt(
          this.t('devices.netpair_rename_title'),
          this.t('devices.netpair_rename_prompt'),
          current
        ).then(function (name) {
          name = (name || '').trim();
          if (!name || name === current) return;
          self.netpairBusy = true;
          ClipsyncAPI.renameInternetPair(peer.peer_id, name)
            .then(function (res) {
              self.netpairBusy = false;
              if (res && res.ok) {
                self.store.setInternetPeerName(peer.peer_id, name);
                self.store.showToast(
                  self.t('devices.netpair_renamed_toast', { name: name }), 2000);
                // The backend persisted the alias — refetch for the
                // authoritative copy.
                self.store.fetchInternetPairStatus();
              } else {
                self.store.showToast(self.t('dialog.failed'), 2000);
              }
            })
            .catch(function () {
              self.netpairBusy = false;
              self.store.showToast(self.t('dialog.failed'), 2000);
            });
        }).catch(function () { /* cancelled */ });
      },

      // 🗑 Unpair an internet-paired peer on THIS side only.
      unpairPeer: function (peer) {
        var self = this;
        var displayName = peer.alias || peer.name || this.shortId(peer.peer_id);
        this.store.confirm(
          this.t('devices.netpair_unpair'),
          this.t('devices.netpair_unpair_confirm', { name: displayName })
        ).then(function () {
          self.netpairBusy = true;
          ClipsyncAPI.unpairInternetPair(peer.peer_id)
            .then(function (res) {
              self.netpairBusy = false;
              if (res && res.ok) {
                self.store.removeInternetPeer(peer.peer_id);
                self.store.showToast(
                  self.t('devices.netpair_unpaired_toast', { name: displayName }), 2000);
                self.store.fetchInternetPairStatus();
              } else {
                self.store.showToast(self.t('dialog.failed'), 2000);
              }
            })
            .catch(function () {
              self.netpairBusy = false;
              self.store.showToast(self.t('dialog.failed'), 2000);
            });
        }).catch(function () { /* cancelled */ });
      },

      // Step-1 "turn on internet sync" opens Settings → Network.
      openInternetSyncSettings: function () {
        this.store.settingsRequestedSection = 'network';
        this.store.openSettingsPanel();
      },

      // Display name: user alias wins, then the device's own name, then a
      // short id.
      peerDisplayName: function (peer) {
        if (peer && peer.alias) return peer.alias;
        return (peer && peer.name) ? peer.name : this.shortId(peer && peer.peer_id);
      },

      // "最近同步 {相对时间}" — last_seen epoch seconds → localized relative
      // time; null → "尚未同步". Re-rendered on the 30s clock.
      lastSeenText: function (peer) {
        return this.relTime(peer && peer.last_seen);
      },

      relTime: function (ts) {
        if (!ts) return this.t('devices.netpair_never_synced');
        var diff = Math.max(0, Date.now() - ts * 1000);
        var min = Math.floor(diff / 60000);
        if (min < 1) return this.t('time.just_now');
        if (min < 60) return this.t('time.minutes_ago', { count: min });
        var h = Math.floor(min / 60);
        if (h < 24) return this.t('time.hours_ago', { count: h });
        var d = Math.floor(h / 24);
        return this.t('time.days_ago', { count: d });
      },

      // The internet-pair peer matching a LAN device id (null when the device
      // is not also paired over the relay). netpair peer ids are real
      // device ids once a handshake confirms identity.
      netpairPeerFor: function (deviceId) {
        var peers = this.store.internetPairPeers || [];
        for (var i = 0; i < peers.length; i++) {
          if (String(peers[i].peer_id) === String(deviceId)) return peers[i];
        }
        return null;
      },
      // A LAN device that is ALSO internet-paired shows the user's alias as
      // its card name (same name as the internet-pair list) so the two lists
      // never disagree about what this device is called.
      devWithAlias: function (dev) {
        var peer = this.netpairPeerFor(dev.device_id);
        if (!peer || !peer.alias) return dev;
        var copy = Object.assign({}, dev);
        copy.device_name = peer.alias;
        return copy;
      },

      shortId: function (id) {
        return (id && id.length > 8) ? id.slice(0, 8) : (id || '');
      },

      refresh: function () {
        var self = this;
        this.refreshing = true;
        ClipsyncAPI.getDevices()
          .then(function (res) {
            self.loadFailed = false;
            self.store.devicesLoadFailed = false;
            if (res && res.devices) {
              self.store.devices = res.devices;
            }
            // Polling fallback for pending pairings (the WS push is dropped
            // when no web client is attached, so refresh must re-sync them).
            if (res && res.pending_pairings) {
              self.store.syncPairingRequests(res.pending_pairings);
            }
          })
          .catch(function () {
            // Don't claim "No devices found" when the list simply couldn't be
            // loaded — surface a distinct failed state with a Retry action.
            self.loadFailed = true;
            self.store.devicesLoadFailed = true;
          })
          .finally(function () {
            self.refreshing = false;
          });
      },

      formattedCode: function (pr) {
        var code = (pr && pr.code) || '';
        // Split the 8-digit code in half so it's easy to compare across
        // devices without misreading adjacent digits.
        return code.length > 4 ? code.slice(0, 4) + ' ' + code.slice(4) : code;
      },

      pairingHint: function (pr) {
        var st = pr.status || 'pending';
        if (st === 'confirmed_waiting') return this.t('pairing.state.confirmed_waiting');
        if (st === 'peer_confirmed') return '✅ ' + this.t('pairing.state.peer_confirmed');
        return this.t('devices.pairing_confirm_hint');
      },

      acceptPairing: function (pr) {
        var self = this;
        this.store.confirm(
          self.t('device.pairing_confirm_title'),
          self.t('device.pairing_confirm_msg')
        )
          .then(function () {
            self.pairingResponding = pr.peer_id;
            ClipsyncAPI.sendPairingResponse(pr.peer_id, 'confirm', pr.code || '')
              .then(function () {
                // Do NOT splice the card here: confirmation is two-sided, so
                // the server keeps it in "waiting for the other device" until
                // the peer confirms.  Refresh re-renders it with the waiting
                // state (or removes it once both sides have confirmed).
                self.refresh();
              })
              .catch(function () {
                self.store.showToast(self.t('device.pairing_failed'), 2000);
              })
              .finally(function () {
                self.pairingResponding = null;
              });
          })
          .catch(function () {
            // User cancelled the confirm dialog — do nothing.
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
          .catch(function () {
            // A network failure must not look like a successful no-op — the
            // request card would otherwise sit there forever.
            self.store.showToast(self.t('device.pairing_failed'), 2000);
          })
          .finally(function () {
            self.pairingResponding = null;
          });
      },
    },

    mounted: function () {
      // Load the internet-pairing state whenever the Devices tab mounts (the
      // v-else-if on activeTab unmounts this component on tab switch, so this
      // also re-freshes every time the user returns to Devices).
      this.loadNetpairState();
      // Refresh relative "last sync" times every 30s while the tab is open.
      var self = this;
      this.netpairClockTimer = setInterval(function () {
        try { self.$forceUpdate(); } catch (e) { /* component unmounted */ }
      }, 30000);
    },

    beforeUnmount: function () {
      if (this.netpairClockTimer) {
        clearInterval(this.netpairClockTimer);
        this.netpairClockTimer = null;
      }
    },
  };

})();
