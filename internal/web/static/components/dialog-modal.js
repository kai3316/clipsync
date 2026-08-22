/* ═══════════════════════════════════════════════════════════════════
   ClipSync Dialog Modal Component
   Renders server-pushed dialogs: alert, confirm, transfer_request,
   pick_peer, url_input, qr_code, progress.  Responses sent back to
   the server via POST /api/dialog-response.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['dialog-modal'] = {
    inject: ['store'],

    template:
      '<transition name="dialog-fade">' +
        '<div v-if="store.activeDialog" class="dialog-overlay" role="dialog" aria-modal="true" @click.self="onOverlayClick">' +
          '<div class="dialog-card" :class="\'dialog-card--\' + store.activeDialog.dialog_type">' +

            '<!-- Header -->' +
            '<div class="dialog-card__header">' +
              '<h3 class="dialog-card__title">{{ store.activeDialog.title }}</h3>' +
              '<p v-if="store.activeDialog.message" class="dialog-card__message">{{ store.activeDialog.message }}</p>' +
            '</div>' +

            '<!-- Body: type-specific content -->' +

            '<!-- transfer_request -->' +
            '<div v-if="store.activeDialog.dialog_type === \'transfer_request\'" class="dialog-card__body">' +
              '<div class="dialog-transfer-info">' +
                '<div class="dialog-transfer-info__row">' +
                  '<span class="dialog-transfer-info__label">{{ t(\'dialog.file_label\') }}</span>' +
                  '<span class="dialog-transfer-info__value">{{ store.activeDialog.file_name }}</span>' +
                '</div>' +
                '<div class="dialog-transfer-info__row" v-if="store.activeDialog.file_size">' +
                  '<span class="dialog-transfer-info__label">{{ t(\'dialog.size_label\') }}</span>' +
                  '<span class="dialog-transfer-info__value">{{ formatSize(store.activeDialog.file_size) }}</span>' +
                '</div>' +
                '<div class="dialog-transfer-info__row" v-if="store.activeDialog.sender">' +
                  '<span class="dialog-transfer-info__label">{{ t(\'dialog.from_label\') }}</span>' +
                  '<span class="dialog-transfer-info__value">{{ store.activeDialog.sender }}</span>' +
                '</div>' +
              '</div>' +
            '</div>' +

            '<!-- pick_peer -->' +
            '<div v-if="store.activeDialog.dialog_type === \'pick_peer\'" class="dialog-card__body">' +
              '<div v-if="!store.activeDialog.peers || store.activeDialog.peers.length === 0" class="dialog-empty">' +
                '{{ t(\'dialog.no_peers\') }}' +
              '</div>' +
              '<div v-for="peer in store.activeDialog.peers" :key="peer.device_id" ' +
                   'class="dialog-peer-item" ' +
                   ':class="{ \'dialog-peer-item--selected\': selectedPeerId === peer.device_id }" ' +
                   '@click="selectedPeerId = peer.device_id">' +
                '<span class="dialog-peer-item__name">{{ peer.device_name }}</span>' +
                '<span class="dialog-peer-item__check" v-if="selectedPeerId === peer.device_id">✓</span>' +
              '</div>' +
            '</div>' +

            '<!-- url_input -->' +
            '<div v-if="store.activeDialog.dialog_type === \'url_input\'" class="dialog-card__body">' +
              '<input ref="urlInput" class="dialog-input" type="url" v-model="inputValue" ' +
                     'placeholder="https://..." @keyup.enter="respond(\'send\')">' +
            '</div>' +

            '<!-- qr_code -->' +
            '<div v-if="store.activeDialog.dialog_type === \'qr_code\'" class="dialog-card__body dialog-card__body--center">' +
              '<img v-if="store.activeDialog.qr_data_url" :src="store.activeDialog.qr_data_url" ' +
                   'class="dialog-qr-img" :alt="t(\'settings_window.web_qr\')">' +
              '<p class="dialog-hint" v-if="store.activeDialog.url">{{ store.activeDialog.url }}</p>' +
            '</div>' +

            '<!-- progress -->' +
            '<div v-if="store.activeDialog.dialog_type === \'progress\'" class="dialog-card__body">' +
              '<div class="dialog-progress-bar">' +
                '<div class="dialog-progress-bar__fill" ' +
                     ':style="{ width: (store.activeDialog.progress || 0) * 100 + \'%\' }"></div>' +
              '</div>' +
              '<p class="dialog-hint">{{ store.activeDialog.progress_text || \'\' }}</p>' +
            '</div>' +

            '<!-- Footer: action buttons -->' +
            '<div class="dialog-card__footer">' +
              '<!-- alert: single OK button -->' +
              '<template v-if="store.activeDialog.dialog_type === \'alert\'">' +
                '<button class="dialog-btn dialog-btn--primary" @click="respond(\'ok\')">{{ t(\'ui.ok\') }}</button>' +
              '</template>' +

              '<!-- confirm / transfer_request -->' +
              '<template v-if="store.activeDialog.dialog_type === \'confirm\' || store.activeDialog.dialog_type === \'transfer_request\'">' +
                '<button class="dialog-btn dialog-btn--secondary" @click="respond(\'reject\')">' +
                  '{{ store.activeDialog.reject_label || t(\'ui.reject\') }}' +
                '</button>' +
                '<button class="dialog-btn dialog-btn--primary" @click="respond(\'accept\')">' +
                  '{{ store.activeDialog.accept_label || t(\'transfer.accept\') }}' +
                '</button>' +
              '</template>' +

              '<!-- pick_peer -->' +
              '<template v-if="store.activeDialog.dialog_type === \'pick_peer\'">' +
                '<button class="dialog-btn dialog-btn--secondary" @click="respond(\'cancel\')">{{ t(\'ui.cancel\') }}</button>' +
                '<button class="dialog-btn dialog-btn--primary" @click="respond(\'select\')" ' +
                        ':disabled="!selectedPeerId">{{ t(\'transfer.send\') }}</button>' +
              '</template>' +

              '<!-- url_input -->' +
              '<template v-if="store.activeDialog.dialog_type === \'url_input\'">' +
                '<button class="dialog-btn dialog-btn--secondary" @click="respond(\'cancel\')">{{ t(\'ui.cancel\') }}</button>' +
                '<button class="dialog-btn dialog-btn--primary" @click="respond(\'send\')" ' +
                        ':disabled="!inputValue.trim()">{{ t(\'transfer.send\') }}</button>' +
              '</template>' +

              '<!-- qr_code -->' +
              '<template v-if="store.activeDialog.dialog_type === \'qr_code\'">' +
                '<button class="dialog-btn dialog-btn--secondary" @click="copyUrl">{{ t(\'overview.copy_url\') }}</button>' +
                '<button class="dialog-btn dialog-btn--primary" @click="respond(\'close\')">{{ t(\'ui.close\') }}</button>' +
              '</template>' +

              '<!-- progress -->' +
              '<template v-if="store.activeDialog.dialog_type === \'progress\'">' +
                '<button class="dialog-btn dialog-btn--secondary" @click="respond(\'cancel\')">{{ t(\'ui.cancel\') }}</button>' +
              '</template>' +
            '</div>' +

          '</div>' +
        '</div>' +
      '</transition>',

    data: function () {
      return {
        selectedPeerId: '',
        inputValue: '',
      };
    },

    watch: {
      'store.activeDialog': {
        immediate: true,
        handler: function (dlg) {
          if (!dlg) {
            this.selectedPeerId = '';
            this.inputValue = '';
            // Restore focus to whatever was focused before the dialog opened.
            var prev = this._prevFocus;
            this._prevFocus = null;
            if (prev && prev.focus && document.contains(prev)) {
              prev.focus();
            }
            return;
          }
          // Pre-populate url_input from clipboard hint
          if (dlg.dialog_type === 'url_input') {
            this.inputValue = dlg.prefill || '';
          }
          // Remember the previously-focused element so it can be restored
          // on close.
          if (this._prevFocus === null) {
            this._prevFocus = document.activeElement;
          }
          var self = this;
          this.$nextTick(function () {
            // Autofocus the URL input so the user can type immediately.
            if (dlg.dialog_type === 'url_input' && self.$refs.urlInput) {
              self.$refs.urlInput.focus();
            }
          });
        },
      },
    },

    methods: {
      respond: function (action) {
        var self = this;
        var dlg = this.store.activeDialog;
        if (!dlg) return;

        var value = undefined;
        if (action === 'select') {
          value = this.selectedPeerId;
        } else if (action === 'send') {
          value = this.inputValue.trim();
        }

        if (!window.ClipsyncAPI || !window.ClipsyncAPI.respondDialog) {
          // No API client — just dismiss locally.
          this.store.closeDialog();
          return;
        }

        // Notify the server and only dismiss the dialog once the response is
        // accepted. If the POST fails the server is still waiting on this
        // dialog_id, so keep it open (and surface the failure) instead of
        // letting an accepted transfer/pairing silently never happen.
        return ClipsyncAPI.respondDialog(dlg.dialog_id, action, value)
          .then(function () {
            self.store.closeDialog();
          })
          .catch(function () {
            self.store.showToast(self.t('dialog.failed'), 2500, 'error');
          });
      },

      onOverlayClick: function () {
        var dlg = this.store.activeDialog;
        if (!dlg) return;
        // Close on overlay click for non-blocking dialogs
        if (dlg.dialog_type === 'alert' || dlg.dialog_type === 'qr_code') {
          this.respond(dlg.dialog_type === 'alert' ? 'ok' : 'close');
        }
      },

      copyUrl: function () {
        var dlg = this.store.activeDialog;
        if (!dlg || !dlg.url) return;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(dlg.url).then(function () {
            // brief feedback
          }).catch(function () { /* ignore */ });
        } else {
          var textarea = document.createElement('textarea');
          textarea.value = dlg.url;
          textarea.style.position = 'fixed';
          textarea.style.opacity = '0';
          document.body.appendChild(textarea);
          textarea.select();
          try { document.execCommand('copy'); } catch (e) { /* ignore */ }
          document.body.removeChild(textarea);
        }
      },

      formatSize: function (bytes) {
        if (!bytes) return '';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
        return (bytes / 1073741824).toFixed(2) + ' GB';
      },
    },

    mounted: function () {
      var self = this;
      // Allow Escape to dismiss non-blocking server-pushed dialogs. Blocking
      // dialogs (progress, confirm) must not be force-closed mid-operation.
      this._onDialogKeydown = function (e) {
        if (e.key !== 'Escape') return;
        var dlg = self.store.activeDialog;
        if (!dlg) return;
        if (dlg.dialog_type === 'progress' || dlg.dialog_type === 'confirm') return;
        // Dismiss through the same path as the cancel/close button so the
        // server gets the proper response.
        if (dlg.dialog_type === 'alert') {
          self.respond('ok');
        } else if (dlg.dialog_type === 'qr_code') {
          self.respond('close');
        } else if (dlg.dialog_type === 'transfer_request') {
          self.respond('reject');
        } else {
          // pick_peer / url_input
          self.respond('cancel');
        }
        // This dialog owns the Escape key — don't let the app-level handler
        // also act on the same keypress (double response / selection clear).
        e.stopImmediatePropagation();
      };
      document.addEventListener('keydown', this._onDialogKeydown);
    },

    beforeUnmount: function () {
      if (this._onDialogKeydown) {
        document.removeEventListener('keydown', this._onDialogKeydown);
        this._onDialogKeydown = null;
      }
    },
  };

})();
