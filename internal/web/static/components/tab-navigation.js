/* ═══════════════════════════════════════════════════════════════════
   ClipSync Tab Navigation Component
   Supports horizontal (default) and vertical sidebar modes.
   Each tab shows an icon, label, and optional count badge.
   The active tab has an accent gradient background with glow.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['tab-navigation'] = {
    inject: ['store'],

    props: {
      mode: {
        type: String,
        default: 'horizontal', // 'horizontal' | 'vertical'
      },
    },

    template:
      '<nav :class="mode === \'vertical\' ? \'tab-nav-vertical\' : \'tab-nav\'">' +
        '<button' +
          ' v-for="tab in tabs"' +
          ' :key="tab.id"' +
          ' :class="[mode === \'vertical\' ? \'tab-nav-vertical__btn\' : \'tab-nav__btn\',' +
          '  store.activeTab === tab.id ? (mode === \'vertical\' ? \'tab-nav-vertical__btn--active\' : \'tab-nav__btn--active\') : \'\']"' +
          ' @click="store.activeTab = tab.id"' +
          ' :title="tab.label"' +
        '>' +
          '<span :class="mode === \'vertical\' ? \'tab-nav-vertical__icon\' : \'tab-nav__icon\'">{{ tab.icon }}</span>' +
          '<span :class="mode === \'vertical\' ? \'tab-nav-vertical__label\' : \'tab-nav__label\'">{{ tab.label }}</span>' +
          '<span v-if="tab.count > 0" class="tab-nav__badge">{{ tab.count }}</span>' +
        '</button>' +
      '</nav>',

    methods: {},

    computed: {
      tabs: function () {
        var s = this.store;
        var t = this.t;
        return [
          {
            id: 'overview',
            label: t('ui.overview'),
            icon: '📊',
            count: 0,
          },
          {
            id: 'history',
            label: t('ui.history'),
            icon: '📋',
            count: s.history.length,
          },
          {
            id: 'devices',
            label: t('ui.devices'),
            icon: '💻',
            count: s.connectedCount,
          },
          {
            id: 'transfers',
            label: t('ui.transfers'),
            icon: '📤',
            count: s.activeTransfers.length,
          },
          {
            id: 'favorites',
            label: t('ui.favorites'),
            icon: '⭐',
            count: s.favorites.length,
          },
          {
            id: 'diagnostics',
            label: t('ui.diagnostics'),
            icon: '🩺',
            count: 0,
          },
        ];
      },
    },
  };

})();
