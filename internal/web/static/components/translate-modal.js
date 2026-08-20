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
          { code: 'auto', name: 'Auto-detect' },
          { code: 'en', name: 'English' },
          { code: 'zh', name: 'Chinese' },
          { code: 'ja', name: 'Japanese' },
          { code: 'ko', name: 'Korean' },
          { code: 'fr', name: 'French' },
          { code: 'de', name: 'German' },
          { code: 'es', name: 'Spanish' },
          { code: 'pt', name: 'Portuguese' },
          { code: 'ru', name: 'Russian' },
          { code: 'ar', name: 'Arabic' },
          { code: 'hi', name: 'Hindi' },
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
            } else {
              self.error = (res && res.error) || 'Translation failed.';
            }
            self.translating = false;
            tm.translating = false;
          })
          .catch(function (e) {
            console.error('[ClipSync] Translate error:', e);
            self.error =
              'Translation service unavailable. Check your network connection.';
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

          // Bind global Escape key listener
          var self = this;
          this._onKeyDown = this.onKeyDown.bind(this);
          document.addEventListener('keydown', this._onKeyDown);
        } else {
          // Clean up Escape key listener
          if (this._onKeyDown) {
            document.removeEventListener('keydown', this._onKeyDown);
            this._onKeyDown = null;
          }
        }
      },
    },

    beforeUnmount: function () {
      if (this._onKeyDown) {
        document.removeEventListener('keydown', this._onKeyDown);
        this._onKeyDown = null;
      }
    },

    template:
      '<div' +
        ' v-if="store.translateModal.visible"' +
        ' class="translate-overlay"' +
        ' ref="overlay"' +
        ' @click="onOverlayClick"' +
      '>' +
        '<div class="translate-modal glass-neo animate-holo-reveal">' +
          '<!-- Header -->' +
          '<div class="translate-modal__header">' +
            '<span class="translate-modal__title">' +
              '<span class="translate-modal__title-icon">&#x1F310;</span>' +
              ' {{ t(\'translate.title\') }}' +
            '</span>' +
            '<button class="translate-modal__close" @click="close"' +
              ' :aria-label="t(\'ui.close\')">&times;</button>' +
          '</div>' +

          '<!-- Body -->' +
          '<div class="translate-modal__body">' +
            '<!-- Source text -->' +
            '<div class="translate-modal__section">' +
              '<label class="translate-modal__label">{{ t(\'translate.original_text\') }}</label>' +
              '<div class="translate-modal__source-text">{{ store.translateModal.text }}</div>' +
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
                  '>{{ lang.name }}</option>' +
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
                  '>{{ lang.name }}</option>' +
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
              '<div class="translate-modal__result-text">{{ translated }}</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>',
  };

})();
