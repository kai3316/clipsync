/* ═══════════════════════════════════════════════════════════════════
   ClipSync Device Card Component
   Displays a single device with status dot, name, device ID, OS icon,
   notes field, and context-sensitive action buttons.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['device-card'] = {
    inject: ['store'],

    props: {
      device: { type: Object, required: true },
    },

    data: function () {
      return {
        editingNote: false,
        noteDraft: this.device.note || '',
        noteSaving: false,
        actionLoading: false,
        actionLabel: '',
      };
    },

    computed: {
      isLocal: function () {
        return this.device.device_id === this.store.deviceId;
      },

      // A real sync session = a live connection on a paired device.  This is
      // the page's definition of "已连接": chat-only / unpaired sessions are
      // temporary and render in their own section.
      isConnected: function () {
        return !!this.device.connected && !!this.device.paired;
      },

      // Live but not paired — a chat (or similar) temporary session that must
      // not count as a sync device.
      isTemporary: function () {
        return !!this.device.connected && !this.device.paired;
      },

      isPairedOffline: function () {
        return !!this.device.paired && !this.device.connected;
      },

      // Any live transport session (sync or temporary) — the raw "online"
      // signal used by card visuals and actions.
      hasLiveSession: function () {
        return !!this.device.connected;
      },

      isPaired: function () {
        return !!this.device.paired;
      },

      isDiscovered: function () {
        return !this.hasLiveSession && !this.isPaired && !this.isLocal;
      },

      statusDot: function () {
        if (this.isLocal) return 'device-card__dot--accent';
        if (this.isConnected) return 'device-card__dot--online';
        if (this.isTemporary) return 'device-card__dot--temp';
        if (this.isPairedOffline) return 'device-card__dot--warning';
        return '';
      },

      statusText: function () {
        if (this.isLocal) return this.t('device.this_device');
        if (this.isConnected) return this.t('device.connected');
        // Mid-reconnect shows live attempt progress instead of a bare
        // "Paired · offline" badge, so a dropped device looks active.
        if (this.device.reconnecting) {
          var n = Number(this.device.reconnect_attempt) || 0;
          var max = Number(this.device.reconnect_max) || 0;
          return this.t('device.reconnecting', { attempt: n, max: max });
        }
        if (this.isTemporary) return this.t('device.temporary_connected');
        if (this.isPairedOffline) return this.t('device.paired_offline');
        return this.t('device.discovered');
      },

      statusClass: function () {
        if (this.isLocal) return 'badge';
        if (this.isConnected) return 'badge badge--success';
        if (this.isTemporary) return 'badge badge--info';
        if (this.isPairedOffline) return 'badge badge--warning';
        return 'badge badge--info';
      },

      osIcon: function () {
        var os = (this.device.os || '').toLowerCase();
        if (os.indexOf('win') !== -1) return '🪟';
        if (os.indexOf('mac') !== -1 || os.indexOf('darwin') !== -1) return '🍎';
        if (os.indexOf('linux') !== -1) return '🐧';
        if (os.indexOf('android') !== -1) return '📱';
        if (os.indexOf('ios') !== -1 || os.indexOf('iphone') !== -1) return '📱';
        return '💻';
      },

      hasActions: function () {
        return !this.isLocal;
      },

      actions: function () {
        if (this.isLocal) return [];
        var acts = [];
        // Chat invite reaches live peers (incl. temporary chat sessions) and
        // paired devices (incl. internet-paired ones, via the relay) — offer
        // it for all three live/paired states.
        if (this.hasLiveSession || this.isPaired) {
          acts.push({ key: 'chat', label: this.t('devices.chat_action'), cls: 'device-card__action--accent' });
          // Connectivity probe: reachable online OR paired (relay can reach a
          // paired-but-LAN-offline peer) — exactly the chat condition.
          acts.push({ key: 'test', label: this.t('device.test_connection'), cls: '' });
        }
        if (this.isConnected || this.isTemporary) {
          acts.push({ key: 'disconnect', label: this.t('device.disconnect'), cls: '' });
        } else if (this.isPaired) {
          acts.push({ key: 'connect', label: this.t('device.connect'), cls: 'device-card__action--accent' });
          acts.push({ key: 'unpair', label: this.t('device.unpair'), cls: 'device-card__action--danger' });
        } else {
          acts.push({ key: 'connect', label: this.t('device.connect'), cls: 'device-card__action--accent' });
        }
        // Forget only from an offline state — a live session (sync or chat)
        // must be disconnected before it can be removed.
        if (!this.isConnected && !this.isTemporary) {
          acts.push({ key: 'forget', label: this.t('device.remove'), cls: 'device-card__action--danger' });
        }
        return acts;
      },
    },

    template:
      '<div' +
        ' class="device-card card"' +
        ' :class="{ \'device-card--local\': isLocal, \'device-card--online\': hasLiveSession, \'device-card--offline\': !hasLiveSession && !isLocal }"' +
        ' @contextmenu.prevent="onContextMenu"' +
      '>' +
        '<div class="device-card__status">' +
          '<span class="device-card__dot" :class="statusDot"></span>' +
        '</div>' +
        '<div class="device-card__icon">{{ osIcon }}</div>' +
        '<div class="device-card__info">' +
          '<span class="device-card__name text-ellipsis">{{ device.device_name || device.name || device.device_id }}</span>' +
          '<span class="device-card__id text-ellipsis selectable">{{ isLocal ? \'💻 \' + t(\'device.this_computer\') : device.device_id }}</span>' +
          '<span v-if="device.os" class="device-card__os">{{ device.os }}</span>' +
          '<!-- Note. Not offered on the local device: notes are cross-device' +
          'memos keyed to a peer, and the backend drops a note whose peer_id is' +
          'the local id (it is never in cfg.peers) — so editing here would be' +
          'a "saved" that silently never persists. -->' +
          '<div v-if="!isLocal && !editingNote" class="device-card__note" role="button" tabindex="0" @click.stop="startEditNote" @keyup.enter="startEditNote" @keyup.space.prevent="startEditNote">' +
            '<span v-if="device.note" class="device-card__note-text selectable">{{ device.note }}</span>' +
            '<span v-else class="device-card__note-placeholder">{{ t(\'device.add_note\') }}</span>' +
          '</div>' +
          '<div v-if="!isLocal && editingNote" class="device-card__note-edit" @click.stop @contextmenu.stop>' +
            '<input type="text" v-model="noteDraft" class="device-card__note-input" :placeholder="t(\'device.note_placeholder\')" @keyup.enter="saveNote" @keyup.escape="cancelEditNote" ref="noteInput">' +
            '<button class="device-card__note-save" @click="saveNote" :disabled="noteSaving">{{ noteSaving ? \'...\' : t(\'device.save_note\') }}</button>' +
            '<button class="device-card__note-cancel" :aria-label="t(\'ui.cancel\')" @click="cancelEditNote">✕</button>' +
          '</div>' +
        '</div>' +
        '<div class="device-card__meta">' +
          '<span :class="statusClass">{{ statusText }}</span>' +
          '<span v-if="hasLiveSession && device.encrypted" style="font-size:10px;color:var(--clipsync-fg-muted);margin-left:4px">🔒</span>' +
        '</div>' +
        '<!-- Action buttons -->' +
        '<div v-if="hasActions" class="device-card__actions">' +
          '<button v-for="act in actions" :key="act.key"' +
            ' class="device-card__action"' +
            ' :class="act.cls"' +
            ' @click.stop="doAction(act.key)"' +
            ' :disabled="actionLoading"' +
          '>{{ actionLoading && actionLabel === act.key ? \'...\' : act.label }}</button>' +
        '</div>' +
      '</div>',

    methods: {
      startEditNote: function () {
        this.editingNote = true;
        this.noteDraft = this.device.note || '';
        var self = this;
        this.$nextTick(function () {
          var inp = self.$refs.noteInput;
          if (inp) inp.focus();
        });
      },

      cancelEditNote: function () {
        this.editingNote = false;
        this.noteDraft = this.device.note || '';
      },

      // 💬 Open a chat session with this device and switch to the Chat tab.
      startChat: function () {
        var self = this;
        var peerId = this.device.device_id;
        var name = this.device.device_name || this.device.name || this.device.note || peerId;
        ClipsyncAPI.chatInvite(peerId, name).then(function (res) {
          if (res && res.session_id) {
            self.store.activeChatSession = res.session_id;
          }
          self.store.activeTab = 'chat';
          if (!(res && res.session_id) && !(res && res.connecting)) {
            self.store.showToast(self.t('chat.err_connect_timeout'), 2500);
          }
        }).catch(function () {
          self.store.showToast(self.t('chat.err_connect_timeout'), 2500);
        }).finally(function () {
          self.actionLoading = false;
          self.actionLabel = '';
        });
      },

      saveNote: function () {
        var self = this;
        var note = (this.noteDraft || '').trim();
        self.noteSaving = true;
        ClipsyncAPI.updateDeviceNote(this.device.device_id, note)
          .then(function (res) {
            if (res && res.ok) {
              var idx = self.store.devices.findIndex(function (d) {
                return d.device_id === self.device.device_id;
              });
              if (idx !== -1) {
                self.store.devices[idx].note = note;
              }
              self.editingNote = false;
              self.store.showToast(self.t('device.note_saved'), 1500);
            } else {
              self.store.showToast(self.t('device.note_save_failed'), 2000);
            }
          })
          .catch(function () {
            self.store.showToast(self.t('device.note_save_failed'), 2000);
          })
          .finally(function () {
            self.noteSaving = false;
          });
      },

      doAction: function (key) {
        var self = this;
        var peerId = this.device.device_id;
        self.actionLoading = true;
        self.actionLabel = key;

        var labels = {
          connect: self.t('device.connect'),
          disconnect: self.t('device.disconnect'),
          unpair: self.t('device.unpair'),
          forget: self.t('device.remove'),
        };
        var actionName = labels[key] || key;

        var runAction = function (method, afterSuccess, successMsg) {
          method.then(function (res) {
            if (res && res.ok) {
              // successMsg lets an action override the generic "… successful"
              // toast — connect uses "connecting…" because {ok:true} only
              // means the attempt was *initiated*, not that a session exists.
              self.store.showToast(successMsg || self.t('device.action_success', { action: actionName }), 2000);
              if (afterSuccess) afterSuccess();
            } else {
              self.store.showToast(self.t('device.action_failed', { action: actionName }) +
                ((res && res.error) ? ': ' + res.error : ''), 2500);
            }
          }).catch(function () {
            self.store.showToast(self.t('device.action_failed', { action: actionName }), 2000);
          }).finally(function () {
            self.actionLoading = false;
            self.actionLabel = '';
          });
        };

        // Unpair/forget remove the device from the server's peer list, so it
        // must also be dropped from the local store — otherwise it lingers in
        // the UI after a "success" toast.
        var removeFromStore = function () {
          var idx = self.store.devices.findIndex(function (d) {
            return d.device_id === peerId;
          });
          if (idx !== -1) {
            self.store.devices.splice(idx, 1);
          }
        };

        var method;
        if (key === 'chat') {
          // Chat invite succeeds with a session_id (or a connecting signal);
          // switch to the Chat tab so the user lands on the conversation.
          self.startChat();
          return;
        }
        if (key === 'connect') {
          // No optimistic set: {ok:true} only means the handshake was
          // *initiated* (main._on_connect).  The real result arrives as a
          // devices_updated broadcast ≤3s later (device moves to Connected),
          // or as a connect_rejected toast when the peer refuses us (its
          // user removed/forgot this device).  So the immediate toast is
          // "connecting…", never "connected" — claiming success here is the
          // lie that left a rejected connect looking like a silent no-op.
          runAction(ClipsyncAPI.connectDevice(peerId), null, self.t('device.connect_started'));
          return;
        }
        if (key === 'disconnect') {
          // Same: no optimistic flip, real state converges via broadcast.
          runAction(ClipsyncAPI.disconnectDevice(peerId));
          return;
        }
        if (key === 'unpair') {
          var deviceName = self.device.device_name || self.device.name || peerId;
          this.store.confirm(
            self.t('device.unpair_confirm_title'),
            self.t('device.unpair_confirm_msg', {name: deviceName})
          )
            .then(function () { runAction(ClipsyncAPI.unpairDevice(peerId), removeFromStore); })
            .catch(function () { self.actionLoading = false; });
          return;
        }
        if (key === 'forget') {
          var deviceName = self.device.device_name || self.device.name || peerId;
          this.store.confirm(
            self.t('device.remove_confirm_title'),
            self.t('device.remove_confirm_msg', {name: deviceName})
          )
            .then(function () { runAction(ClipsyncAPI.forgetDevice(peerId), removeFromStore); })
            .catch(function () { self.actionLoading = false; });
          return;
        }
        if (key === 'test') {
          // Full probe result (per-channel RTT + reasons), not a bool.  The
          // shared store helper owns the endpoint + toast (the internet-pair
          // peer row delegates to it too); this method just keeps the card's
          // busy flag for the button spinner.
          self.store.testPeerConnection(peerId).finally(function () {
            self.actionLoading = false;
            self.actionLabel = '';
          });
          return;
        }
        self.actionLoading = false;
      },

      onContextMenu: function (e) {
        this.store.contextMenu = {
          visible: true,
          x: e.clientX,
          y: e.clientY,
          mode: 'device',
          target: this.device,
          opener: e.currentTarget || e.target,
        };
      },
    },
  };

})();
