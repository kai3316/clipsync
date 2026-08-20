/* ═══════════════════════════════════════════════════════════════════
   ClipSync I18n — Lightweight client-side translation module.

   Translations are injected via __I18N_JSON__ at serve time, so no
   additional network requests are needed.  Works as a plain JS object.

   Usage:
     ClipsyncI18n.t('web.push_button')          // => "Push to Desktop"
     ClipsyncI18n.t('web.push_sent')            // => "Sent!"
     ClipsyncI18n.t('missing.key')              // => "missing.key" (fallback)
     ClipsyncI18n.t('history.show_more', { count: 5 })
                                                 // => "Show 5 more..."
     ClipsyncI18n.locale                         // => "en" or "zh-CN"

   The module is initialised automatically from window.__I18N_JSON__
   (set by the Python server's _interpolate_html).  Callers should
   invoke ClipsyncI18n.init() in their app's mounted() hook if needed.
   ═══════════════════════════════════════════════════════════════════ */

var ClipsyncI18n = (function () {
  'use strict';

  var _translations = {};
  var _locale = 'en';
  var _ready = false;

  /* ═══════════════════════════════════════════════════════════════
     Public API
     ═══════════════════════════════════════════════════════════════ */

  return {

    /**
     * (Re-)initialise with a translations dictionary.
     * @param {object} translations - Flat key-value pairs
     * @param {string} [locale='en'] - Active locale code
     */
    init: function (translations, locale) {
      _translations = translations || {};
      _locale = locale || 'en';
      _ready = true;
    },

    /**
     * Translate a key, with optional {name: value} format placeholders.
     * Falls back to the raw key if the translation is missing.
     *
     * @param {string} key        - i18n key (e.g. "web.push_button")
     * @param {object} [fmt={}]   - Format placeholders (e.g. { count: 5 })
     * @returns {string}
     */
    t: function (key, fmt) {
      var text = _translations[key];
      if (typeof text !== 'string') {
        text = key;
      }
      if (fmt && typeof fmt === 'object') {
        Object.keys(fmt).forEach(function (k) {
          // split/join replaces every occurrence, whereas String.replace with
          // a string pattern only replaces the first — a key used twice (or a
          // {count} that appears more than once) would otherwise be left
          // partially substituted.
          text = text.split('{' + k + '}').join(fmt[k]);
        });
      }
      return text;
    },

    /** @type {string} Active locale code */
    get locale() {
      return _locale;
    },

    /** @type {boolean} Whether initialised */
    get ready() {
      return _ready;
    },

  };

})();
