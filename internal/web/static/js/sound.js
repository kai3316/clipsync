/* ═══════════════════════════════════════════════════════════════════
   ClipSync Sound Effects
   Synthesises simple tones using the Web Audio API — no audio files
   needed.  Respects the `sound_enabled` setting from localStorage.

   Usage:
     ClipsyncSound.playCopy();      // clipboard copy chime
     ClipsyncSound.playPaste();     // paste blip
     ClipsyncSound.playConnect();   // ascending connection tone
     ClipsyncSound.playDisconnect();// descending disconnection tone
     ClipsyncSound.playError();     // low error buzz

   All methods are no-ops when sound is disabled or Web Audio is
   unsupported.
   ═══════════════════════════════════════════════════════════════════ */

var ClipsyncSound = (function () {
  'use strict';

  var ctx = null;
  var _soundEnabled = true;

  /* ── Initialise AudioContext on first user gesture ─────────────── */
  function _ensureContext() {
    if (ctx) return true;
    try {
      var AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return false;
      ctx = new AudioContext();
    } catch (e) {
      return false;
    }
    return true;
  }

  /* ── Check sound preference ────────────────────────────────────── */
  function _loadEnabled() {
    // Prefer the server-loaded value from the store (the source of truth);
    // fall back to the legacy localStorage key for older installs.
    var store = window.__CLIPSYNC_STORE__;
    if (store && typeof store.soundEnabled === 'boolean') {
      _soundEnabled = store.soundEnabled;
      return;
    }
    try {
      var val = localStorage.getItem('clipsync_sound');
      if (val === '0' || val === 'false') {
        _soundEnabled = false;
      }
    } catch (e) { /* ignore */ }
  }
  _loadEnabled();

  /* ── Core tone player ──────────────────────────────────────────── */
  function _beep(freq, duration, type) {
    if (!_soundEnabled) return;
    if (!_ensureContext()) return;

    type = type || 'sine';

    // Resume AudioContext if suspended (browser autoplay policy)
    if (ctx.state === 'suspended') {
      ctx.resume();
    }

    try {
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();

      osc.type = type;
      osc.frequency.setValueAtTime(freq, ctx.currentTime);

      gain.gain.setValueAtTime(0.3, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);

      osc.connect(gain);
      gain.connect(ctx.destination);

      osc.start(ctx.currentTime);
      osc.stop(ctx.currentTime + duration);
    } catch (e) {
      // Silently ignore — audio is non-critical
    }
  }

  /* ── Two-tone sequence player ──────────────────────────────────── */
  function _twoTone(freq1, freq2, totalDuration, type) {
    if (!_soundEnabled) return;
    if (!_ensureContext()) return;
    type = type || 'sine';

    if (ctx.state === 'suspended') {
      ctx.resume();
    }

    try {
      var half = totalDuration / 2;
      var now = ctx.currentTime;

      // Tone 1
      var osc1 = ctx.createOscillator();
      var gain1 = ctx.createGain();
      osc1.type = type;
      osc1.frequency.setValueAtTime(freq1, now);
      gain1.gain.setValueAtTime(0.3, now);
      gain1.gain.exponentialRampToValueAtTime(0.001, now + half);
      osc1.connect(gain1);
      gain1.connect(ctx.destination);
      osc1.start(now);
      osc1.stop(now + half);

      // Tone 2
      var osc2 = ctx.createOscillator();
      var gain2 = ctx.createGain();
      osc2.type = type;
      osc2.frequency.setValueAtTime(freq2, now + half);
      gain2.gain.setValueAtTime(0.3, now + half);
      gain2.gain.exponentialRampToValueAtTime(0.001, now + totalDuration);
      osc2.connect(gain2);
      gain2.connect(ctx.destination);
      osc2.start(now + half);
      osc2.stop(now + totalDuration);
    } catch (e) {
      // Silently ignore
    }
  }

  /* ═══════════════════════════════════════════════════════════════════
     Public API
     ═══════════════════════════════════════════════════════════════════ */

  return {
    /** Short high-pitched beep — clipboard copy event. */
    playCopy: function () {
      _beep(800, 0.08, 'sine');
    },

    /** Short blip — paste event. */
    playPaste: function () {
      _beep(600, 0.06, 'sine');
    },

    /** Ascending two-tone — peer connected. */
    playConnect: function () {
      _twoTone(400, 600, 0.15, 'sine');
    },

    /** Descending two-tone — peer disconnected. */
    playDisconnect: function () {
      _twoTone(600, 400, 0.15, 'sine');
    },

    /** Low buzz — error occurred. */
    playError: function () {
      _beep(200, 0.2, 'square');
    },

    /** Enable or disable sound effects. Persisted to localStorage. */
    setEnabled: function (enabled) {
      _soundEnabled = !!enabled;
      // Keep the reactive store in sync so WS tones and the settings toggle
      // always agree with the server-side preference.
      var store = window.__CLIPSYNC_STORE__;
      if (store) store.soundEnabled = _soundEnabled;
      try {
        localStorage.setItem('clipsync_sound', enabled ? '1' : '0');
      } catch (e) { /* ignore */ }
    },

    /** Check whether sound is enabled. */
    get isEnabled() {
      return _soundEnabled;
    },
  };

})();
