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

      isOnline: function () {
        return !!this.device.connected;
      },

      isPaired: function () {
        return !!this.device.paired;
      },

      isDiscovered: function () {
        return !this.isOnline && !this.isPaired && !this.isLocal;
      },

      statusDot: function () {
        if (this.isLocal) return 'device-card__dot--accent';
        if (this.isOnline) return 'device-card__dot--online';
        if (this.isPaired) return 'device-card__dot--warning';
        return '';
      },

      statusText: function () {
        if (this.isLocal) return this.t('device.this_device');
        if (this.isOnline) return this.t('device.connected');
        if (this.isPaired) return this.t('device.paired_offline');
        return this.t('device.discovered');
      },

      statusClass: function () {
        if (this.isLocal) return 'badge';
        if (this.isOnline) return 'badge badge--success';
        if (this.isPaired) return 'badge badge--warning';
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
        if (this.isOnline) {
          acts.push({ key: 'disconnect', label: this.t('device.disconnect'), cls: '' });
        } else if (this.isPaired) {
          acts.push({ key: 'connect', label: this.t('device.connect'), cls: 'device-card__action--accent' });
          acts.push({ key: 'unpair', label: this.t('device.unpair'), cls: 'device-card__action--danger' });
        } else {
          acts.push({ key: 'connect', label: this.t('device.connect'), cls: 'device-card__action--accent' });
        }
        if (!this.isOnline) {
          acts.push({ key: 'forget', label: this.t('device.remove'), cls: 'device-card__action--danger' });
        }
        return acts;
      },
    },

    template:
      '<div' +
        ' class="device-card card"' +
        ' :class="{ \'device-card--local\': isLocal, \'device-card--online\': isOnline, \'device-card--offline\': !isOnline && !isLocal }"' +
        ' @contextmenu.prevent="onContextMenu"' +
      '>' +
        '<div class="device-card__status">' +
          '<span class="device-card__dot" :class="statusDot"></span>' +
        '</div>' +
        '<div class="device-card__icon">{{ osIcon }}</div>' +
        '<div class="device-card__info">' +
          '<span class="device-card__name text-ellipsis">{{ device.device_name || device.name || device.device_id }}</span>' +
          '<span class="device-card__id text-ellipsis">{{ isLocal ? \'🖥 \' + t(\'device.this_computer\') : device.device_id }}</span>' +
          '<!-- Note -->' +
          '<div v-if="!editingNote" class="device-card__note" role="button" tabindex="0" @click.stop="startEditNote" @keyup.enter="startEditNote" @keyup.space.prevent="startEditNote">' +
            '<span v-if="device.note" class="device-card__note-text">{{ device.note }}</span>' +
            '<span v-else class="device-card__note-placeholder">{{ t(\'device.add_note\') }}</span>' +
          '</div>' +
          '<div v-if="editingNote" class="device-card__note-edit" @click.stop>' +
            '<input type="text" v-model="noteDraft" class="device-card__note-input" :placeholder="t(\'device.note_placeholder\')" @keyup.enter="saveNote" @keyup.escape="cancelEditNote" ref="noteInput">' +
            '<button class="device-card__note-save" @click="saveNote" :disabled="noteSaving">{{ noteSaving ? \'...\' : t(\'device.save_note\') }}</button>' +
            '<button class="device-card__note-cancel" :aria-label="t(\'ui.cancel\')" @click="cancelEditNote">✕</button>' +
          '</div>' +
        '</div>' +
        '<div class="device-card__meta">' +
          '<span :class="statusClass">{{ statusText }}</span>' +
          '<span v-if="isOnline && device.encrypted" style="font-size:10px;color:var(--clipsync-fg-muted);margin-left:4px">🔒</span>' +
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

        var runAction = function (method, afterSuccess) {
          method.then(function (res) {
            if (res && res.ok) {
              self.store.showToast(self.t('device.action_success', { action: actionName }), 2000);
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
        if (key === 'connect') {
          runAction(ClipsyncAPI.connectDevice(peerId), function () {
            self.device.connected = true;
            self.device.paired = true;
          });
          return;
        }
        if (key === 'disconnect') {
          runAction(ClipsyncAPI.disconnectDevice(peerId), function () {
            self.device.connected = false;
          });
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
        self.actionLoading = false;
      },

      onContextMenu: function (e) {
        this.store.contextMenu = {
          visible: true,
          x: e.clientX,
          y: e.clientY,
          mode: 'device',
          target: this.device,
        };
      },
    },
  };

})();
