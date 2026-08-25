/* ═══════════════════════════════════════════════════════════════════
   ClipSync Translate Modal Component
   Glass-neo modal for AI-powered text translation via LibreTranslate.
   Select source/target language, click translate, copy the result.
   Neo-futuristic design matching the ClipSync design system.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['translate-modal'] = {
    inject: ['store'],

    data: function () {
      return {
        sourceLang: 'auto',
        targetLang: 'zh',
        translated: '',
        translating: false,
        error: '',
        languages: [
          { code: 'auto', nameKey: 'translate.lang_auto' },
          { code: 'en', nameKey: 'translate.lang_en' },
          { code: 'zh', nameKey: 'translate.lang_zh' },
          { code: 'ja', nameKey: 'translate.lang_ja' },
          { code: 'ko', nameKey: 'translate.lang_ko' },
          { code: 'fr', nameKey: 'translate.lang_fr' },
          { code: 'de', nameKey: 'translate.lang_de' },
          { code: 'es', nameKey: 'translate.lang_es' },
          { code: 'pt', nameKey: 'translate.lang_pt' },
          { code: 'ru', nameKey: 'translate.lang_ru' },
          { code: 'ar', nameKey: 'translate.lang_ar' },
          { code: 'hi', nameKey: 'translate.lang_hi' },
        ],
      };
    },

    computed: {
      // "auto" is only meaningful for the source language. The target
      // selector must exclude it. Filtering here (rather than with a
      // v-if on the same element as v-for) avoids Vue's v-if > v-for
      // precedence, which would evaluate the condition without the loop
      // variable and throw.
      targetLanguages: function () {
        return this.languages.filter(function (lang) {
          return lang.code !== 'auto';
        });
      },
    },

    methods: {
      doTranslate: function () {
        var tm = this.store.translateModal;
        var text = tm.text;
        if (!text || this.translating) return;

        var self = this;
        this.translating = true;
        this.error = '';
        this.translated = '';

        // Sync local state into the store so reactive watchers see it
        tm.sourceLang = this.sourceLang;
        tm.targetLang = this.targetLang;
        tm.translating = true;

        ClipsyncAPI.translate(text, this.targetLang, this.sourceLang)
          .then(function (res) {
            if (res && res.ok) {
              self.translated = res.translated || '';
              tm.translated = self.translated;
              if (res.truncated) {
                // MyMemory caps requests at ~500 bytes; the backend flagged
                // that the clip was cut before translating.
                self.store.showToast(self.t('translate.truncated'), 3000);
              }
            } else {
              self.error = (res && res.error) || self.t('translate.failed');
            }
            self.translating = false;
            tm.translating = false;
          })
          .catch(function (e) {
            console.error('[ClipSync] Translate error:', e);
            self.error = self.t('translate.service_unavailable');
            self.translating = false;
            tm.translating = false;
          });
      },

      copyTranslated: function () {
        var text = this.translated || this.store.translateModal.translated;
        if (!text) return;

        if (navigator.clipboard && navigator.clipboard.writeText) {
          var self = this;
          navigator.clipboard.writeText(text).then(function () {
            self.store.showToast(self.t('translate.copied'), 1500);
          }).catch(function () {
            self.store.showToast(self.t('history.copy_failed'), 2000);
          });
        } else {
          // Fallback for older browsers / non-HTTPS
          var textarea = document.createElement('textarea');
          textarea.value = text;
          textarea.style.position = 'fixed';
          textarea.style.opacity = '0';
          document.body.appendChild(textarea);
          textarea.select();
          try {
            document.execCommand('copy');
            this.store.showToast(this.t('translate.copied'), 1500);
          } catch (e) {
            this.store.showToast(this.t('history.copy_failed'), 2000);
          }
          document.body.removeChild(textarea);
        }
      },

      copySource: function () {
        var text = this.store.translateModal.text;
        if (!text) return;

        if (navigator.clipboard && navigator.clipboard.writeText) {
          var self = this;
          navigator.clipboard.writeText(text).then(function () {
            self.store.showToast(self.t('translate.copied'), 1500);
          }).catch(function () {
            self.store.showToast(self.t('history.copy_failed'), 2000);
          });
        } else {
          // Fallback for older browsers / non-HTTPS
          var textarea = document.createElement('textarea');
          textarea.value = text;
          textarea.style.position = 'fixed';
          textarea.style.opacity = '0';
          document.body.appendChild(textarea);
          textarea.select();
          try {
            document.execCommand('copy');
            this.store.showToast(this.t('translate.copied'), 1500);
          } catch (e) {
            this.store.showToast(this.t('history.copy_failed'), 2000);
          }
          document.body.removeChild(textarea);
        }
      },

      close: function () {
        this.store.closeTranslateModal();
        this.translated = '';
        this.error = '';
        this.translating = false;
      },

      onOverlayClick: function (e) {
        if (e.target === this.$refs.overlay) {
          this.close();
        }
      },

      onKeyDown: function (e) {
        if (e.key === 'Escape') {
          this.close();
        }
      },
    },

    watch: {
      'store.translateModal.visible': function (visible) {
        if (visible) {
          // Sync local state from the store when opened
          var tm = this.store.translateModal;
          this.sourceLang = tm.sourceLang || 'auto';
          this.targetLang = tm.targetLang || 'zh';
          this.translated = tm.translated || '';
          this.translating = tm.translating || false;
          this.error = '';

          // Remember what was focused so it can be restored on close.
          if (!this._prevFocus) {
            this._prevFocus = document.activeElement;
          }

          // Bind global Escape key listener
          var self = this;
          this._onKeyDown = this.onKeyDown.bind(this);
          document.addEventListener('keydown', this._onKeyDown);

          // Move focus into the modal (close button) for keyboard users.
          this.$nextTick(function () {
            if (self.$refs.closeBtn) {
              self.$refs.closeBtn.focus();
            }
          });
        } else {
          // Clean up Escape key listener
          if (this._onKeyDown) {
            document.removeEventListener('keydown', this._onKeyDown);
            this._onKeyDown = null;
          }
          // Restore focus to the element that opened the modal.
          if (this._prevFocus) {
            var prev = this._prevFocus;
            this._prevFocus = null;
            if (prev.focus && document.contains(prev)) {
              prev.focus();
            }
          }
        }
      },
    },

    beforeUnmount: function () {
      if (this._onKeyDown) {
        document.removeEventListener('keydown', this._onKeyDown);
        this._onKeyDown = null;
      }
      // Restore focus if the modal is torn down while still open.
      if (this._prevFocus) {
        var prev = this._prevFocus;
        this._prevFocus = null;
        if (prev.focus && document.contains(prev)) {
          prev.focus();
        }
      }
    },

    template:
      '<div' +
        ' v-if="store.translateModal.visible"' +
        ' class="translate-overlay"' +
        ' role="dialog"' +
        ' aria-modal="true"' +
        ' aria-labelledby="translate-modal-title"' +
        ' ref="overlay"' +
        ' @click="onOverlayClick"' +
      '>' +
        '<div class="translate-modal glass-neo animate-holo-reveal">' +
          '<!-- Header -->' +
          '<div class="translate-modal__header">' +
            '<span class="translate-modal__title" id="translate-modal-title">' +
              '<span class="translate-modal__title-icon">&#x1F310;</span>' +
              ' {{ t(\'translate.title\') }}' +
            '</span>' +
            '<button ref="closeBtn" class="translate-modal__close" @click="close"' +
              ' :aria-label="t(\'ui.close\')">&times;</button>' +
          '</div>' +

          '<!-- Body -->' +
          '<div class="translate-modal__body">' +
            '<!-- Source text -->' +
            '<div class="translate-modal__section">' +
              '<div class="translate-modal__result-header">' +
                '<span class="translate-modal__label">{{ t(\'translate.original_text\') }}</span>' +
                '<button' +
                  ' class="btn-ghost translate-modal__copy-btn"' +
                  ' @click="copySource"' +
                '>' +
                  '<span>&#x1F4CB;</span> {{ t(\'ui.copy\') }}' +
                '</button>' +
              '</div>' +
              '<div class="translate-modal__source-text selectable">{{ store.translateModal.text }}</div>' +
            '</div>' +

            '<!-- Language selectors -->' +
            '<div class="translate-modal__lang-row">' +
              '<div class="translate-modal__lang-group">' +
                '<label class="translate-modal__label">{{ t(\'translate.source\') }}</label>' +
                '<select v-model="sourceLang" class="translate-modal__select">' +
                  '<option' +
                    ' v-for="lang in languages"' +
                    ' :key="lang.code"' +
                    ' :value="lang.code"' +
                  '>{{ t(lang.nameKey) }}</option>' +
                '</select>' +
              '</div>' +
              '<div class="translate-modal__lang-arrow">&#x27A1;</div>' +
              '<div class="translate-modal__lang-group">' +
                '<label class="translate-modal__label">{{ t(\'translate.target\') }}</label>' +
                '<select v-model="targetLang" class="translate-modal__select">' +
                  '<option' +
                    ' v-for="lang in targetLanguages"' +
                    ' :key="lang.code"' +
                    ' :value="lang.code"' +
                  '>{{ t(lang.nameKey) }}</option>' +
                '</select>' +
              '</div>' +
            '</div>' +

            '<!-- Translate button -->' +
            '<button' +
              ' class="btn-primary translate-modal__translate-btn"' +
              ' :disabled="translating || !store.translateModal.text"' +
              ' @click="doTranslate"' +
            '>' +
              '<span v-if="translating" class="translate-modal__spinner"></span>' +
              '<span v-if="!translating">{{ t(\'translate.translate\') }}</span>' +
              '<span v-if="translating">{{ t(\'translate.translating\') }}</span>' +
            '</button>' +

            '<!-- Error message -->' +
            '<div v-if="error" class="translate-modal__error">' +
              '<span class="translate-modal__error-icon">&#x26A0;</span>' +
              ' {{ error }}' +
            '</div>' +

            '<!-- Result area -->' +
            '<div v-if="translated" class="translate-modal__result animate-scale-in">' +
              '<div class="translate-modal__result-header">' +
                '<span class="translate-modal__label">{{ t(\'translate.result\') }}</span>' +
                '<button' +
                  ' class="btn-ghost translate-modal__copy-btn"' +
                  ' @click="copyTranslated"' +
                '>' +
                  '<span>&#x1F4CB;</span> {{ t(\'ui.copy\') }}' +
                '</button>' +
              '</div>' +
              '<div class="translate-modal__result-text selectable">{{ translated }}</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>',
  };

})();
