/* ═══════════════════════════════════════════════════════════════════
   ClipSync Toast Notification Component
   Displays brief messages in a bottom-centre stack. Several toasts can
   be visible at once; each is timed and dismissed by the store
   (store.showToast pushes, store._dismissToast/_dropToast remove), so
   this component is a pure renderer.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['toast'] = {
    inject: ['store'],

    computed: {
      // Icon per toast type; unknown types fall back to the info glyph.
      icons: function () {
        return {
          success: '✅',
          error: '❌',
          warning: '⚠️',
          info: 'ℹ️',
        };
      },
    },

    template:
      '<div class="toast-stack" aria-live="polite">' +
        '<div v-for="tItem in store.toasts" :key="tItem.id" role="status"' +
          ' :class="[\'toast\', \'toast--\' + tItem.type, { \'toast--leave\': tItem.leaving }]">' +
          '<span class="toast__icon">{{ icons[tItem.type] || icons.info }}</span>' +
          '<span class="toast__text">{{ tItem.message }}</span>' +
        '</div>' +
      '</div>',
  };

})();
