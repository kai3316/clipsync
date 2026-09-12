/* ═══════════════════════════════════════════════════════════════════
   ClipSync Device Panel Component
   Sections (top→bottom): This Device, Pairing Requests, Connected,
   Temporary, Paired Offline, Discovered, Removed — then the Internet
   pairing block at the bottom (collapsed behind a toggle).
   Uses device-card component with action buttons and notes editing.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  // Shared "also paired over the internet" badge for a LAN device card.  The
  // same markup used to be inlined three times (Connected / Paired Offline /
  // Discovered); one definition keeps the online/offline label + class logic
  // in a single place.  Renders nothing for a device without a netpair peer.
  window.__CLIPSYNC_COMPONENTS__['netpair-badge'] = {
    props: {
      peer: { type: Object, default: null },
    },
    computed: {
      online: function () {
        return !!(this.peer && this.peer.online);
      },
    },
    template:
      '<span v-if="peer" class="netpair-card-badge" @contextmenu.stop' +
        ' :class="online ? \'netpair-card-badge--online\' : \'netpair-card-badge--offline\'"' +
        ' :title="t(\'devices.netpair_also_internet\')">' +
        '🌐 {{ online ? t(\'devices.netpair_online\') : t(\'devices.netpair_offline\') }}' +
      '</span>',
  };

  window.__CLIPSYNC_COMPONENTS__['device-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        refreshing: false,
        // {peer_id: true} for every pairing response still in flight.  Two
        // requests can be pending at once, so a single scalar would let the
        // second one re-enable the first one's buttons (and the first reply
        // to finish would re-enable the other, still-running one).
        pairingResponding: {},
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
        netpairTestingId: '',     // peer_id whose "test connection" probe is in flight
        netpairExpanded: true,    // default expanded; fold it if the page is busy
        _netpairLoadInFlight: false,
        netpairClockTimer: null,  // refreshes relative "last sync" times

        // Section collapsibility — every section defaults to expanded (same
        // default as the internet-pairing header), foldable by its title.
        sections: {
          local: true,
          pairing: true,
          connected: true,
          temporary: true,
          paired: true,
          discovered: true,
          removed: true,
        },
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

      // A real sync session = a live connection on a paired device.  This is
      // the page's definition of "已连接": chat-only / unpaired sessions are
      // temporary and render in their own section below.
      connectedSyncDevices: function () {
        return this.allRemoteDevices.filter(function (d) { return d.connected && d.paired; });
      },

      // Live but not paired — a chat (or similar) temporary session that
      // must not count as a sync device.
      temporaryConnectedDevices: function () {
        return this.allRemoteDevices.filter(function (d) { return d.connected && !d.paired; });
      },

      pairedOfflineDevices: function () {
        return this.allRemoteDevices.filter(function (d) { return d.paired && !d.connected; });
      },

      discoveredDevices: function () {
        return this.allRemoteDevices.filter(function (d) { return !d.paired && !d.connected; });
      },

      // Archived (forgotten) devices, manageable via Restore / Delete.
      removedDevices: function () {
        return this.store.removedDevices || [];
      },

      pairingRequests: function () {
        return this.store.pairingRequests || [];
      },

      // ── Internet pairing (round 15) mirrors of store state ───────

      netpairPeers: function () {
        return this.store.internetPairPeers || [];
      },

      // The overview "已配 {N} 台互联网设备" count. Every entry in
      // internetPairPeers is a confirmed pair — the backend status endpoint
      // only lists confirmed pairs (provisional base32 tags are excluded),
      // and the store normalizes `paired` to true for all of them — so the
      // count is simply the list length.
      netpairPairedCount: function () {
        return (this.store.internetPairPeers || []).length;
      },

      netpairGeneratedCode: function () {
        return this.store.internetPairCode || '';
      },

      // Codes entered here that the partner has not answered yet (round 20).
      // Between submitting a code and the reply this is the only thing on the
      // page that says anything happened, and it is deliberately not part of
      // the paired list: a 4-char tag is not a device.
      netpairWaiting: function () {
        return this.store.internetPairWaiting || [];
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
          '<button type="button" class="section-header section-header--toggle" :aria-expanded="sections.local" @click="toggleSection(\'local\')">' +
            '💻 {{ t(\'devices.this_device\') }}' +
            '<span v-if="localInternetOnline" class="netpair-local-badge" :title="t(\'devices.netpair_also_internet\')">🌐 {{ t(\'devices.netpair_online\') }}</span>' +
            '<span class="netpair-section__chevron" :class="{ \'netpair-section__chevron--open\': sections.local }">▾</span>' +
          '</button>' +
          '<div v-if="sections.local"><device-card :device="localDev"></device-card></div>' +
        '</div>' +

        '<!-- Internet pairing moved to the bottom (collapsed behind a toggle) -->' +

        '<div v-if="store.loading" class="device-panel__loading">' +
          '<div class="skeleton-card animate-shimmer" v-for="n in 2" :key="n"></div>' +
        '</div>' +

        '<template v-else>' +

          '<!-- Pairing Requests — pinned to the top of the page: the codes are' +
          'time-sensitive and both must be compared on the two devices, so they' +
          'must not sit below the device lists. -->' +
          '<div v-if="pairingRequests.length > 0" class="device-panel__section">' +
            '<button type="button" class="section-header section-header--toggle" style="color:var(--clipsync-warning)" :aria-expanded="sections.pairing" @click="toggleSection(\'pairing\')">' +
              '🔐 {{ t(\'devices.pairing_requests\') }}' +
              '<span class="section-header__badge">{{ pairingRequests.length }}</span>' +
              '<span class="netpair-section__chevron" :class="{ \'netpair-section__chevron--open\': sections.pairing }">▾</span>' +
            '</button>' +
            '<div v-if="sections.pairing">' +
              '<div v-for="pr in pairingRequests" :key="pr.peer_id" class="pairing-request-card">' +
                '<div class="pairing-request-card__info">' +
                  '<span class="pairing-request-card__name">{{ pr.peer_name || pr.device_name || pr.peer_id }}</span>' +
                  '<span class="pairing-request-card__id">{{ pr.peer_id }}</span>' +
                  // The 8-digit shared pairing code is the only value actually
                  // validated on both devices. The SAS fingerprint is derived
                  // from different inputs and is never equal to it, so showing
                  // both is confusing — display only the code to compare.
                  '<div class="pairing-request-card__codes">' +
                    '<span class="pairing-request-code-badge selectable">{{ t(\'ui.pairing_code_label\') }} <strong>{{ formattedCode(pr) }}</strong></span>' +
                    '<button class="btn-ghost" @click="copyPairingCode(pr)">{{ t(\'ui.copy\') }}</button>' +
                  '</div>' +
                  '<span class="pairing-request-card__hint">{{ pairingHint(pr) }}</span>' +
                  '<span class="pairing-request-card__hint">{{ t(\'devices.pairing_expiry_hint\') }}</span>' +
                '</div>' +
                '<div class="pairing-request-card__actions">' +
                  '<button v-if="pr.status !== \'confirmed_waiting\' && pr.status !== \'expired\'" class="device-card__action device-card__action--accent" @click="acceptPairing(pr)" :disabled="!!pairingResponding[pr.peer_id]">' +
                    '{{ pairingResponding[pr.peer_id] ? \'...\' : t(\'ui.confirm\') }}' +
                  '</button>' +
                  // Expired is not "confirmed, waiting for the other device" —
                  // label it honestly as expired (same state the hint shows).
                  '<span v-else class="pairing-request-card__waiting">⏳ {{ t(pr.status === \'expired\' ? \'pairing.state.expired\' : \'pairing.state.confirmed_waiting\') }}</span>' +
                  '<button class="device-card__action device-card__action--danger" @click="rejectPairing(pr)" :disabled="!!pairingResponding[pr.peer_id]">' +
                    '{{ pairingResponding[pr.peer_id] ? \'...\' : t(\'ui.reject\') }}' +
                  '</button>' +
                '</div>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<!-- Connected: paired + live session → real sync -->' +
          '<div v-if="connectedSyncDevices.length > 0" class="device-panel__section">' +
            '<button type="button" class="section-header section-header--toggle" :aria-expanded="sections.connected" @click="toggleSection(\'connected\')">' +
              '🟢 {{ t(\'device.connected\') }}' +
              '<span class="section-header__badge">{{ connectedSyncDevices.length }}</span>' +
              '<span class="netpair-section__chevron" :class="{ \'netpair-section__chevron--open\': sections.connected }">▾</span>' +
            '</button>' +
            '<div v-if="sections.connected">' +
              '<div v-for="dev in connectedSyncDevices" :key="dev.device_id" class="device-internet-wrap">' +
                '<netpair-badge :peer="netpairPeerFor(dev.device_id)"></netpair-badge>' +
                '<device-card :device="devWithAlias(dev)"></device-card>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<!-- Temporary: live but not paired (chat-only sessions) -->' +
          '<div v-if="temporaryConnectedDevices.length > 0" class="device-panel__section">' +
            '<button type="button" class="section-header section-header--toggle" :aria-expanded="sections.temporary" @click="toggleSection(\'temporary\')">' +
              '🟣 {{ t(\'device.temporary_connected\') }}' +
              '<span class="section-header__badge">{{ temporaryConnectedDevices.length }}</span>' +
              '<span class="netpair-section__chevron" :class="{ \'netpair-section__chevron--open\': sections.temporary }">▾</span>' +
            '</button>' +
            '<div v-if="sections.temporary">' +
              '<div v-for="dev in temporaryConnectedDevices" :key="dev.device_id" class="device-internet-wrap">' +
                '<device-card :device="devWithAlias(dev)"></device-card>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<!-- Paired Offline -->' +
          '<div v-if="pairedOfflineDevices.length > 0" class="device-panel__section">' +
            '<button type="button" class="section-header section-header--toggle" :aria-expanded="sections.paired" @click="toggleSection(\'paired\')">' +
              '🟠 {{ t(\'device.paired_offline\') }}' +
              '<span class="section-header__badge section-header__badge--muted">{{ pairedOfflineDevices.length }}</span>' +
              '<span class="netpair-section__chevron" :class="{ \'netpair-section__chevron--open\': sections.paired }">▾</span>' +
            '</button>' +
            '<div v-if="sections.paired">' +
              '<div v-for="dev in pairedOfflineDevices" :key="dev.device_id" class="device-internet-wrap">' +
                '<netpair-badge :peer="netpairPeerFor(dev.device_id)"></netpair-badge>' +
                '<device-card :device="devWithAlias(dev)"></device-card>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<!-- Discovered -->' +
          '<div v-if="discoveredDevices.length > 0" class="device-panel__section">' +
            '<button type="button" class="section-header section-header--toggle" :aria-expanded="sections.discovered" @click="toggleSection(\'discovered\')">' +
              '🔍 {{ t(\'device.discovered\') }}' +
              '<span class="section-header__badge section-header__badge--muted">{{ discoveredDevices.length }}</span>' +
              '<span class="netpair-section__chevron" :class="{ \'netpair-section__chevron--open\': sections.discovered }">▾</span>' +
            '</button>' +
            '<div v-if="sections.discovered">' +
              '<div v-for="dev in discoveredDevices" :key="dev.device_id" class="device-internet-wrap">' +
                '<netpair-badge :peer="netpairPeerFor(dev.device_id)"></netpair-badge>' +
                '<device-card :device="devWithAlias(dev)"></device-card>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<!-- Removed devices (archive) — forgotten devices, manageable via' +
          'Restore / Delete permanently. -->' +
          '<div v-if="removedDevices.length > 0" class="device-panel__section">' +
            '<button type="button" class="section-header section-header--toggle" :aria-expanded="sections.removed" @click="toggleSection(\'removed\')">' +
              '🗑 {{ t(\'devices.removed_title\') }}' +
              '<span class="section-header__badge section-header__badge--muted">{{ removedDevices.length }}</span>' +
              '<span class="netpair-section__chevron" :class="{ \'netpair-section__chevron--open\': sections.removed }">▾</span>' +
            '</button>' +
            '<div v-if="sections.removed">' +
              '<div v-for="dev in removedDevices" :key="dev.device_id" class="removed-device-row">' +
                '<div class="removed-device-row__info">' +
                  '<span class="removed-device-row__name text-ellipsis">{{ dev.device_name || dev.device_id }}</span>' +
                  '<span class="removed-device-row__id text-mono selectable">{{ shortId(dev.device_id) }}</span>' +
                  '<span v-if="dev.removed_at" class="removed-device-row__time">' +
                    '{{ t(\'devices.removed_at\', { time: relTime(dev.removed_at) }) }}' +
                  '</span>' +
                '</div>' +
                '<div class="removed-device-row__actions">' +
                  '<button class="device-card__action device-card__action--accent" @click="restoreRemovedDevice(dev)">' +
                    '{{ t(\'device.restore\') }}' +
                  '</button>' +
                  '<button class="device-card__action device-card__action--danger" @click="purgeRemovedDevice(dev)">' +
                    '{{ t(\'device.purge\') }}' +
                  '</button>' +
                '</div>' +
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

          '<!-- Empty: loaded and genuinely no devices. The Removed archive is' +
          '"devices you had", so a page holding only archived rows must not' +
          'also claim "No devices found" — that empty state waits until the' +
          'archive is gone too. -->' +
          '<div v-else-if="allRemoteDevices.length === 0 && pairingRequests.length === 0 && removedDevices.length === 0" class="panel-empty">' +
            '<span class="panel-empty-icon">📡</span>' +
            '<p class="panel-empty-title">{{ t(\'devices.no_devices_found\') }}</p>' +
            '<p class="panel-empty-desc">{{ t(\'devices.auto_discover_hint\') }}</p>' +
          '</div>' +
        '</template>' +

        '<!-- Internet pairing (round 15) — at the BOTTOM, collapsed behind a' +
        'toggle so the page isn\'t a wall of pairing prompts.  Only when the' +
        'switch is on does the detail (relay state, generate/enter code,' +
        'paired list) show. -->' +
        '<div v-if="!store.loading && !loadFailed && !store.devicesLoadFailed" class="device-panel__section netpair-section netpair-section--bottom">' +
          '<button type="button" class="netpair-section__header" :aria-expanded="netpairExpanded" :aria-label="t(\'devices.netpair_toggle_hint\')" @click="toggleNetpair">' +
            '<span class="netpair-section__title">' +
              '🌐 {{ t(\'devices.netpair_title\') }}' +
              '<span class="section-header__badge">{{ netpairPairedCount }}</span>' +
            '</span>' +
            '<span class="netpair-section__chevron" :class="{ \'netpair-section__chevron--open\': netpairExpanded }">▾</span>' +
          '</button>' +

          '<div v-if="netpairExpanded" class="netpair-body">' +
            '<div class="netpair-overview">' +
              '<span class="netpair-overview__dot" :style="{ background: relayStateColor }"></span>' +
              '<span class="netpair-overview__state">{{ t(relayStateKey) }}</span>' +
              '<span class="netpair-overview__count">{{ t(\'devices.netpair_overview_paired\', { count: netpairPairedCount }) }}</span>' +
            '</div>' +
            '<div class="netpair-privacy">🔒 {{ t(\'settings_window.internet_sync_hint\') }}</div>' +
            '<div v-if="netpairLoading" class="netpair-loading">{{ t(\'ui.loading\') }}</div>' +
            '<template v-else>' +
              '<div v-if="!internetSyncEnabled" class="netpair-syncoff">' +
                '<span class="netpair-syncoff__text">{{ t(\'devices.netpair_sync_off\') }}</span>' +
                '<button type="button" class="btn-ghost" @click="openInternetSyncSettings">{{ t(\'devices.netpair_go_settings\') }}</button>' +
              '</div>' +
              // Generate + enter a pairing code only make sense while the
              // relay is actually on — with sync off the controls would be
              // dead UI (entering a code is already rejected by the handler).
              // The paired list below stays visible for management.
              '<div v-if="internetSyncEnabled" class="netpair-actions">' +
                '<button class="btn-ghost" @click="generateNetpairCode" :disabled="netpairGenerating">' +
                  '{{ netpairGenerating ? \'...\' : (netpairGeneratedCode ? t(\'devices.netpair_regenerate\') : t(\'devices.netpair_generate\')) }}' +
                '</button>' +
                '<template v-if="netpairGeneratedCode">' +
                  '<div class="netpair-code">' +
                    '<code class="netpair-code__value selectable">{{ netpairDisplayCode }}</code>' +
                    '<button class="btn-ghost" @click="copyNetpairCode">{{ t(\'ui.copy\') }}</button>' +
                  '</div>' +
                '</template>' +
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

              // Codes entered here whose partner has not answered yet.  Shown
              // above the paired list, because that is where the reader looks
              // right after submitting one, and with a way back out: a code
              // typed for the wrong device would otherwise sit here until it
              // expires.
              '<div v-if="netpairWaiting.length > 0" class="netpair-waiting">' +
                '<div class="netpair-waiting__title">{{ t(\'devices.netpair_waiting_list\') }}</div>' +
                '<div v-for="row in netpairWaiting" :key="row.peer_id" class="netpair-waiting__row card">' +
                  '<span class="netpair-waiting__spin">⏳</span>' +
                  '<span class="netpair-waiting__text text-ellipsis">{{ waitingText(row) }}</span>' +
                  '<span v-if="waitingSinceText(row)" class="netpair-waiting__since">{{ waitingSinceText(row) }}</span>' +
                  '<button class="btn-ghost" @click="unpairPeer(row)" :disabled="netpairBusy">{{ t(\'devices.netpair_waiting_cancel\') }}</button>' +
                '</div>' +
                '<div class="netpair-waiting__hint">{{ t(\'devices.netpair_waiting_hint\') }}</div>' +
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
                    '<button class="btn-ghost" @click="testNetpairPeer(peer)" :disabled="netpairBusy || netpairTestingId === peer.peer_id">{{ netpairTestingId === peer.peer_id ? \'...\' : t(\'device.test_connection\') }}</button>' +
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
        '</div>' +
      '</div>',

    methods: {
      // ── Internet pairing (round 15) ──────────────────────────────

      // Toggle the internet-pairing section (the switch in the section
      // header).  Expanded by default so the pairing controls and paired list
      // are visible without hunting; the user can fold it when the page is
      // busy.
      toggleNetpair: function () {
        this.netpairExpanded = !this.netpairExpanded;
      },

      // Fold / unfold one page section (this-device, pairing, connected,
      // temporary, paired, discovered, removed).  All default to expanded.
      toggleSection: function (name) {
        if (name in this.sections) {
          this.sections[name] = !this.sections[name];
        }
      },

      // 🔄 Bring an archived (removed) device back into the known list.  Keeps
      // its paired flag and best-effort reconnects to its last address, so an
      // online device returns to sync directly; an offline one lands in
      // "Paired · offline" and reconnects when it comes back.
      restoreRemovedDevice: function (dev) {
        var self = this;
        var name = dev.device_name || dev.device_id;
        this.store.confirm(
          this.t('devices.restore_confirm_title'),
          this.t('devices.restore_confirm_msg', { name: name })
        )
          .then(function () {
            ClipsyncAPI.restoreDevice(dev.device_id)
              .then(function (res) {
                if (res && res.ok) {
                  self.store.showToast(
                    self.t('devices.restored_toast', { name: name }), 2000, 'success');
                } else {
                  self.store.showToast((res && res.error) || self.t('dialog.failed'), 2000);
                }
              })
              .catch(function () {
                self.store.showToast(self.t('dialog.failed'), 2000);
              });
          })
          .catch(function () { /* cancelled */ });
      },

      // 🗑 Permanently delete an archived (removed) device.  Irreversible —
      // confirm before proceeding.
      purgeRemovedDevice: function (dev) {
        var self = this;
        var name = dev.device_name || dev.device_id;
        this.store.confirm(
          this.t('devices.purge_confirm_title'),
          this.t('devices.purge_confirm_msg', { name: name })
        )
          .then(function () {
            ClipsyncAPI.purgeDevice(dev.device_id)
              .then(function (res) {
                if (res && res.ok) {
                  self.store.showToast(
                    self.t('devices.purged_toast', { name: name }), 2000, 'success');
                } else {
                  self.store.showToast((res && res.error) || self.t('dialog.failed'), 2000);
                }
              })
              .catch(function () {
                self.store.showToast(self.t('dialog.failed'), 2000);
              });
          })
          .catch(function () { /* cancelled */ });
      },

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

      // Surface a netpair action failure: keep the inline error anchored to
      // the form (it stays until the next attempt) and ALSO fire a toast so
      // the failure isn't missed (same pattern as the speed test).
      _setNetpairError: function (msg) {
        this.netpairError = msg;
        this.store.showToast(msg, 3000, 'error');
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

      // 📋 Copy text to the clipboard (shared by the netpair-code and the LAN
      // pairing-request copy actions).  Falls back to a hidden textarea when
      // the async Clipboard API is unavailable; both callers toast the same
      // "code copied" message so the two pairing codes behave identically.
      _copyToClipboard: function (text) {
        var self = this;
        var done = function () {
          self.store.showToast(self.t('devices.netpair_code_copied'), 2000);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done).catch(done);
        } else {
          var textarea = document.createElement('textarea');
          textarea.value = text;
          textarea.style.position = 'fixed';
          textarea.style.opacity = '0';
          document.body.appendChild(textarea);
          textarea.select();
          try { document.execCommand('copy'); } catch (e) { /* ignore */ }
          document.body.removeChild(textarea);
          done();
        }
      },

      copyNetpairCode: function () {
        var code = this.netpairDisplayCode;
        if (!code) return;
        this._copyToClipboard(code);
      },

      // 📋 Copy an 8-digit LAN pairing-request code to the clipboard.  Shares
      // the same clipboard helper as the netpair code.
      copyPairingCode: function (pr) {
        var code = this.formattedCode(pr);
        if (!code) return;
        this._copyToClipboard(code);
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
          this._setNetpairError(this.t('devices.netpair_error_format'));
          return;
        }
        // Pairing over the internet is meaningless while the relay is off.
        if (!this.internetSyncEnabled) {
          this._setNetpairError(this.t('devices.netpair_error_sync_off'));
          return;
        }
        this.netpairError = '';
        this.netpairConfirming = true;
        ClipsyncAPI.enterInternetPair(code)
          .then(function (res) {
            self.netpairConfirming = false;
            if (res && res.ok) {
              self.netpairCodeInput = '';
              // The paired peer + its name arrive via the WS netpair_peer
              // broadcast (→ store.applyNetpairPeer), which fires the single
              // "paired" toast for BOTH sides.  Refresh the list here so the
              // new peer row appears without waiting on the broadcast; no
              // local toast — it would duplicate the WS one.
              self.store.fetchInternetPairStatus();
            } else {
              self._setNetpairError(self.t('devices.netpair_error_invalid'));
            }
          })
          .catch(function (e) {
            self.netpairConfirming = false;
            if (e && e.status === 400) {
              // Distinguish "paired with yourself" from a generic bad code.
              var reason = (e.data && e.data.error) || '';
              if (/self|own|same|cannot pair with this device/i.test(reason)) {
                self._setNetpairError(self.t('devices.netpair_error_self'));
              } else {
                self._setNetpairError(self.t('devices.netpair_error_invalid'));
              }
            } else {
              self._setNetpairError(self.t('dialog.failed'));
            }
          });
      },

      // 🔌 Test connectivity to an internet-paired peer: probe the relay
      // channel (and the LAN channel when also reachable), toast the result.
      // The endpoint + result formatting live in the shared store helper,
      // which the LAN device card's test action also delegates to — this
      // method only owns the busy flag.
      //
      // The flag holds the peer_id being probed, not a bare boolean: a shared
      // boolean made ONE click grey out and spin the test button on EVERY row
      // in the list, so several devices looked stuck when only one was busy.
      testNetpairPeer: function (peer) {
        var self = this;
        if (!peer || !peer.peer_id) return;
        if (self.netpairTestingId) return;
        self.netpairTestingId = peer.peer_id;
        self.store.testPeerConnection(peer.peer_id).finally(function () {
          self.netpairTestingId = '';
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

      // Step-1 "turn on internet sync" opens Settings → Remote Sync.
      openInternetSyncSettings: function () {
        this.store.settingsRequestedSection = 'remote';
        this.store.openSettingsPanel();
      },

      // "等待 {name} 确认" for a code we entered: the partner's name once its
      // hello has arrived, and the tag the code carried until then — the tag
      // is what the user has to compare against what they typed.
      waitingText: function (row) {
        var name = (row && row.name) ? row.name : this.shortId(row && row.peer_id);
        return this.t('devices.netpair_waiting_for', { name: name });
      },

      // How long this wait has been going.  `since` is null after a restart
      // (the clock is lost, the wait is not), so that renders as nothing
      // rather than through relTime — which would say "尚未同步", a sync
      // message about a pairing that has not happened yet.
      waitingSinceText: function (row) {
        return (row && row.since) ? this.relTime(row.since) : '';
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
        // devices_updated is edge-triggered on the server (it only fires when
        // the device fingerprint changes), so a stale GET response that
        // overwrites a fresher WS push would never be corrected.  Record the
        // tick now and drop our snapshot if a push landed while we waited.
        var startTick = this.store.devicesMutationTick;
        ClipsyncAPI.getDevices()
          .then(function (res) {
            self.loadFailed = false;
            self.store.devicesLoadFailed = false;
            if (self.store.devicesMutationTick !== startTick) return;
            if (res && res.devices) {
              self.store.devices = res.devices;
            }
            // Polling fallback for pending pairings (the WS push is dropped
            // when no web client is attached, so refresh must re-sync them).
            if (res && res.pending_pairings) {
              self.store.syncPairingRequests(res.pending_pairings);
            }
            // Same fallback for the removed-devices archive: restore/purge
            // from another tab would otherwise be invisible on a cold refresh.
            if (res && res.removed) {
              self.store.syncRemovedDevices(res.removed);
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
        if (st === 'expired') return '⏳ ' + this.t('pairing.state.expired');
        return this.t('devices.pairing_confirm_hint');
      },

      acceptPairing: function (pr) {
        var self = this;
        this.store.confirm(
          self.t('device.pairing_confirm_title'),
          self.t('device.pairing_confirm_msg')
        )
          .then(function () {
            self.pairingResponding[pr.peer_id] = true;
            ClipsyncAPI.sendPairingResponse(pr.peer_id, 'confirm', pr.code || '')
              .then(function (res) {
                if (!res || !res.ok) {
                  // {ok:false} (expired / code mismatch / peer cancelled) must
                  // not look like a successful confirm.  Re-read the server
                  // state too, or the card keeps showing the stale request
                  // that just failed.
                  self.store.showToast(self.t('device.pairing_failed'), 2000);
                  self.refresh();
                  return;
                }
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
                delete self.pairingResponding[pr.peer_id];
              });
          })
          .catch(function (reason) {
            // A deliberate cancel is a no-op.  Anything else means the dialog
            // was torn down under the user (a server-pushed dialog superseded
            // it), so the Confirm click would otherwise vanish with no sign.
            if (reason && reason !== 'cancel') {
              self.store.showToast(self.t('device.pairing_failed'), 2000);
            }
          });
      },

      rejectPairing: function (pr) {
        this.pairingResponding[pr.peer_id] = true;
        var self = this;
        ClipsyncAPI.sendPairingResponse(pr.peer_id, 'reject', '')
          .then(function (res) {
            if (!res || !res.ok) {
              self.store.showToast(self.t('device.pairing_failed'), 2000);
              self.refresh();
              return;
            }
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
            delete self.pairingResponding[pr.peer_id];
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
