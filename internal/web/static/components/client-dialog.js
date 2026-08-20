/* ═══════════════════════════════════════════════════════════════════
   ClipSync Client Dialog Component
   Renders confirm, prompt, and alert dialogs triggered by
   store.confirm(), store.prompt(), store.alert().
   Replaces browser-native alert/confirm/prompt.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['client-dialog'] = {
    inject: ['store'],

    data: function () {
      return {
        inputValue: '',
      };
    },

    watch: {
      'store.clientDialog': {
        immediate: true,
        handler: function (dlg) {
          if (dlg && dlg.type === 'prompt') {
            this.inputValue = dlg.defaultValue || '';
          }
        },
      },
    },

    template:
      '<transition name="dialog-fade">' +
        '<div v-if="store.clientDialog" class="dialog-overlay" role="dialog" aria-modal="true" @click.self="onOverlayClick" @contextmenu.prevent>' +
          '<div class="dialog-card dialog-card--small" :class="\'dialog-card--client-\' + store.clientDialog.type">' +

            '<div class="dialog-card__header">' +
              '<h3 class="dialog-card__title">{{ store.clientDialog.title }}</h3>' +
              '<p v-if="store.clientDialog.message" class="dialog-card__message">{{ store.clientDialog.message }}</p>' +
            '</div>' +

            '<div v-if="store.clientDialog.type === \'prompt\'" class="dialog-card__body">' +
              '<input class="dialog-input" type="text" v-model="inputValue" ref="promptInput" ' +
                     '@keyup.enter="onConfirm" @keyup.escape="onCancel">' +
            '</div>' +

            '<div class="dialog-card__footer">' +
              '<template v-if="store.clientDialog.type === \'alert\'">' +
                '<button class="dialog-btn dialog-btn--primary" @click="onConfirm" ref="okBtn">{{ t(\'ui.ok\') }}</button>' +
              '</template>' +
              '<template v-else>' +
                '<button class="dialog-btn dialog-btn--secondary" @click="onCancel">{{ t(\'ui.cancel\') }}</button>' +
                '<button class="dialog-btn dialog-btn--primary" @click="onConfirm" ref="okBtn">' +
                  '{{ store.clientDialog.type === \'prompt\' ? t(\'ui.ok\') : t(\'ui.confirm\') }}' +
                '</button>' +
              '</template>' +
            '</div>' +

          '</div>' +
        '</div>' +
      '</transition>',

    methods: {
      onConfirm: function () {
        var dlg = this.store.clientDialog;
        if (!dlg) return;
        this.store.clientDialog = null;
        if (dlg.type === 'prompt') {
          dlg.resolve(this.inputValue);
        } else {
          dlg.resolve();
        }
      },

      onCancel: function () {
        var dlg = this.store.clientDialog;
        if (!dlg) return;
        this.store.closeClientDialog();
      },

      onOverlayClick: function () {
        var dlg = this.store.clientDialog;
        if (!dlg) return;
        if (dlg.type === 'alert') {
          this.onConfirm();
        } else {
          this.onCancel();
        }
      },
    },

    mounted: function () {
      var self = this;
      // Focus the OK button or input when dialog opens
      this.$watch('store.clientDialog', function (dlg) {
        if (!dlg) return;
        self.$nextTick(function () {
          if (dlg.type === 'prompt' && self.$refs.promptInput) {
            self.$refs.promptInput.focus();
            self.$refs.promptInput.select();
          } else if (self.$refs.okBtn) {
            self.$refs.okBtn.focus();
          }
        });
      });
    },
  };

})();
