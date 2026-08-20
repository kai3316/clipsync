/* ═══════════════════════════════════════════════════════════════════
   ClipSync Feature Carousel — Neo-Futuristic Edition

   Continuous auto-scrolling showcase (marquee style) with edge
   gradient fades, glass-neo cards, and glow hover effects.
   Inspired by QuickClipboard's "为什么选择 QuickClipboard？" section.

   Dismissible — stores preference in localStorage.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['feature-carousel'] = {
    inject: ['store'],

    data: function () {
      return {
        dismissed: false,
        paused: false,
      };
    },

    computed: {
      features: function () {
        var t = this.t;
        return [
          { id: 'sync', emoji: '⚡', title: t('carousel.instant_sync'), desc: t('carousel.instant_sync_desc'), gradient: 'linear-gradient(135deg, var(--clipsync-accent), #00B4D8)' },
          { id: 'security', emoji: '🔒', title: t('carousel.e2e_encrypted'), desc: t('carousel.e2e_encrypted_desc'), gradient: 'linear-gradient(135deg, var(--clipsync-accent-2), #7C3AED)' },
          { id: 'format', emoji: '🎨', title: t('carousel.rich_format'), desc: t('carousel.rich_format_desc'), gradient: 'linear-gradient(135deg, #00D4FF, var(--clipsync-accent-2))' },
          { id: 'crossplatform', emoji: '🌐', title: t('carousel.cross_platform'), desc: t('carousel.cross_platform_desc'), gradient: 'linear-gradient(135deg, var(--clipsync-success), #00C9A7)' },
          { id: 'web', emoji: '📱', title: t('carousel.web_companion'), desc: t('carousel.web_companion_desc'), gradient: 'linear-gradient(135deg, #FFB020, #F97316)' },
          { id: 'zeroconfig', emoji: '⚙️', title: t('carousel.zero_config'), desc: t('carousel.zero_config_desc'), gradient: 'linear-gradient(135deg, #6366F1, var(--clipsync-accent-2))' },
          { id: 'dedup', emoji: '🔄', title: t('carousel.smart_dedup'), desc: t('carousel.smart_dedup_desc'), gradient: 'linear-gradient(135deg, var(--clipsync-accent), var(--clipsync-success))' },
          { id: 'quickpaste', emoji: '⌨️', title: t('carousel.quick_paste'), desc: t('carousel.quick_paste_desc'), gradient: 'linear-gradient(135deg, var(--clipsync-accent-2), #FFB020)' },
          { id: 'favorites', emoji: '⭐', title: t('carousel.smart_favorites'), desc: t('carousel.smart_favorites_desc'), gradient: 'linear-gradient(135deg, var(--clipsync-warning), var(--clipsync-accent-2))' },
        ];
      },

      // Duplicate features for seamless infinite scroll
      scrollItems: function () {
        return this.features.concat(this.features);
      },
    },

    methods: {
      dismiss: function () {
        this.dismissed = true;
        try {
          localStorage.setItem('clipsync_carousel_dismissed', '1');
        } catch (e) { /* ignore */ }
      },

      onMouseEnter: function () {
        this.paused = true;
      },

      onMouseLeave: function () {
        this.paused = false;
      },
    },

    mounted: function () {
      try {
        if (localStorage.getItem('clipsync_carousel_dismissed') === '1') {
          this.dismissed = true;
        }
      } catch (e) { /* ignore */ }
    },

    template:
      '<transition name="carousel-dismiss">' +
        '<div v-if="!dismissed" class="feature-carousel"' +
          ' @mouseenter="onMouseEnter"' +
          ' @mouseleave="onMouseLeave">' +

          '<!-- Dismiss button -->' +
          '<button class="feature-carousel__dismiss" @click="dismiss" :title="t(\'carousel.dismiss\')">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
              '<line x1="18" y1="6" x2="6" y2="18"></line>' +
              '<line x1="6" y1="6" x2="18" y2="18"></line>' +
            '</svg>' +
          '</button>' +

          '<!-- Section header -->' +
          '<div class="feature-carousel__header">' +
            '<h2 class="feature-carousel__title gradient-text">{{ t(\'carousel.why_clipsync\') }}</h2>' +
            '<p class="feature-carousel__subtitle text-muted">{{ t(\'carousel.tagline\') }}</p>' +
          '</div>' +

          '<!-- Scroll container -->' +
          '<div class="feature-carousel__scroll-wrap">' +
            '<!-- Left edge fade -->' +
            '<div class="feature-carousel__fade feature-carousel__fade--left"></div>' +

            '<!-- Scroll track -->' +
            '<div class="feature-carousel__scroll"' +
              ' :class="{ \'feature-carousel__scroll--paused\': paused }">' +
              '<div class="feature-carousel__scroll-inner">' +

                '<!-- Feature cards (duplicated for infinite loop) -->' +
                '<div' +
                  ' v-for="(item, idx) in scrollItems"' +
                  ' :key="\'fc-\' + idx"' +
                  ' class="feature-card group"' +
                '>' +
                  '<div class="feature-card__icon-wrap"' +
                    ' :style="{background: item.gradient}">' +
                    '<span class="feature-card__icon">{{ item.emoji }}</span>' +
                  '</div>' +
                  '<div class="feature-card__body">' +
                    '<h3 class="feature-card__title">{{ item.title }}</h3>' +
                    '<p class="feature-card__desc">{{ item.desc }}</p>' +
                  '</div>' +
                '</div>' +

              '</div>' +
            '</div>' +

            '<!-- Right edge fade -->' +
            '<div class="feature-carousel__fade feature-carousel__fade--right"></div>' +
          '</div>' +

        '</div>' +
      '</transition>',
  };

})();
