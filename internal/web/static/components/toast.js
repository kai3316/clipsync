/* ═══════════════════════════════════════════════════════════════════
   ClipSync Toast Notification Component
   Displays brief messages at the bottom-centre of the screen.
   Auto-dismisses based on the duration set in the store.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['toast'] = {
    inject: ['store'],

    data: function () {
      return {
        show: false,
        message: '',
        leave: false,
      };
    },

    computed: {
      typeIcon: function () {
        var t = this.store.toastType;
        if (t === 'success') return '✅';
        if (t === 'error') return '❌';
        if (t === 'warning') return '⚠️';
        return 'ℹ️';
      },
    },

    template:
      '<div v-if="show" role="status" aria-live="polite" aria-atomic="true" :class="[\'toast\', \'toast--\' + store.toastType, { \'toast--leave\': leave }]" @animationend="onAnimationEnd">' +
        '<span class="toast__icon">{{ typeIcon }}</span>' +
        '<span class="toast__text">{{ store.toastMessage }}</span>' +
      '</div>',

    watch: {
      'store.toastVisible': function (val) {
        if (val) {
          this.leave = false;
          this.show = true;
        } else {
          this.dismiss();
        }
      },
    },

    methods: {
      dismiss: function () {
        this.leave = true;
      },

      onAnimationEnd: function () {
        // The leave animation (toastOut) drives dismissal; a plain CSS
        // animation never fires `transitionend`, which is why the old handler
        // was dead code and the node stayed mounted.
        if (this.leave) {
          this.show = false;
          this.leave = false;
        }
      },
    },

    mounted: function () {
      // If there's already a toast visible when this component mounts, show
      // it. The store's own timer drives the hide, so we don't schedule a
      // second one here (that would cut long toasts short at a hardcoded
      // 2200ms).
      if (this.store.toastVisible) {
        this.show = true;
      }
    },
  };

})();
