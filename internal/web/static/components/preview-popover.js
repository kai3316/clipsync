/* ═══════════════════════════════════════════════════════════════════
   ClipSync Preview Popover Component
   Floating preview card that appears when hovering over a history
   item. Anchored to the pointer (not the item's bounding rect) and
   flipped / clamped so it always stays fully on screen. Non-interactive
   (pointer-events: none) so it never blocks hovering adjacent items.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['preview-popover'] = {
    inject: ['store'],

    data: function () {
      return {
        _height: 0,
      };
    },

    computed: {
      popoverStyle: function () {
        // store.previewPosition = { x, y } viewport coords of the hover
        // cursor. Place the card below-right of the pointer, flipping left
        // when it would overflow the right edge and flipping above when it
        // would overflow the bottom. Anchor to the pointer (not the item
        // rect) so the popover never has to overlap the full-width list.
        var pos = this.store.previewPosition || {};
        var cx = pos.x != null ? pos.x : 0;
        var cy = pos.y != null ? pos.y : 0;

        var width = 360;
        var gap = 16;
        var margin = 8;
        var vw = window.innerWidth;
        var vh = window.innerHeight;
        var height = this._height || 360;

        var x = cx + gap;
        var y = cy + gap;

        if (x + width > vw - margin) {
          x = cx - width - gap;
        }
        if (x < margin) x = margin;
        if (x + width > vw - margin) x = vw - width - margin;

        if (y + height > vh - margin) {
          y = cy - height - gap;
        }
        if (y < margin) y = margin;

        return {
          position: 'fixed',
          left: Math.round(x) + 'px',
          top: Math.round(y) + 'px',
          width: width + 'px',
          maxHeight: (vh - margin * 2) + 'px',
        };
      },

      typeIcon: function () {
        var ct = ((this.store.previewItem || {}).content_type || '').toUpperCase();
        if (ct === 'IMAGE' || ct === 'IMAGE_EMF') return { icon: '🖼', label: this.t('preview.type_image') };
        if (ct === 'FILE') return { icon: '📄', label: this.t('preview.type_file') };
        if (ct === 'HTML') return { icon: '🌐', label: this.t('preview.type_html') };
        if (ct === 'RTF') return { icon: '📋', label: this.t('preview.type_rtf') };
        return { icon: '📝', label: this.t('preview.type_text') };
      },

      isImage: function () {
        var ct = ((this.store.previewItem || {}).content_type || '').toUpperCase();
        return ct === 'IMAGE' || ct === 'IMAGE_EMF';
      },

      timestamp: function () {
        var ts = this.store.previewItem && this.store.previewItem.timestamp;
        if (!ts) return '';
        var tsMs = ts > 1e12 ? ts : ts * 1000;
        var d = new Date(tsMs);
        var month = String(d.getMonth() + 1).padStart(2, '0');
        var day = String(d.getDate()).padStart(2, '0');
        var hours = String(d.getHours()).padStart(2, '0');
        var mins = String(d.getMinutes()).padStart(2, '0');
        return month + '/' + day + ' ' + hours + ':' + mins;
      },
    },

    watch: {
      // Measure the rendered height once visible so vertical flip/clamp
      // uses the real card size instead of a hardcoded estimate.
      'store.previewItem': function (val) {
        if (!val) return;
        var self = this;
        this.$nextTick(function () {
          if (self.$el) self._height = self.$el.offsetHeight;
        });
      },
    },

    template:
      '<transition name="preview-popover-trans">' +
        '<div' +
          ' v-if="store.previewItem"' +
          ' class="preview-popover glass-neo scan-lines"' +
          ' :style="popoverStyle"' +
        '>' +
          '<!-- Header: type icon + timestamp -->' +
          '<div class="preview-popover__header">' +
            '<span class="preview-popover__type">' +
              '<span class="preview-popover__type-icon">{{ typeIcon.icon }}</span>' +
              '<span class="preview-popover__type-label neon-badge">{{ typeIcon.label }}</span>' +
            '</span>' +
            '<span class="preview-popover__time text-subtle">{{ timestamp }}</span>' +
          '</div>' +

          '<!-- Content body -->' +
          '<div class="preview-popover__content">' +
            '<div v-if="isImage" class="preview-popover__placeholder">' +
              '<span class="preview-popover__placeholder-icon">🖼</span>' +
              '<span class="text-muted">{{ t(\'preview.image_content\') }}</span>' +
            '</div>' +
            '<div v-else class="preview-popover__text">' +
              '{{ store.previewItem.text_preview || t(\'preview.empty\') }}' +
            '</div>' +
          '</div>' +

          '<!-- Footer: source badge + pinned indicator -->' +
          '<div class="preview-popover__footer" v-if="store.previewItem.source_name || store.previewItem.pinned">' +
            '<span v-if="store.previewItem.source_name" class="preview-popover__source badge">{{ store.previewItem.source_name }}</span>' +
            '<span v-if="store.previewItem.pinned" class="preview-popover__pinned text-accent">📌 {{ t(\'preview.pinned\') }}</span>' +
          '</div>' +

          '<!-- Animated scan sweep overlay -->' +
          '<div class="preview-popover__sweep"></div>' +
        '</div>' +
      '</transition>',
  };

})();
