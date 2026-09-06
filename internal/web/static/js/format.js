/* ═══════════════════════════════════════════════════════════════════
   ClipSync Format — Shared human-readable value formatting.

   These formatters were duplicated five times (chat-panel, dialog-modal,
   transfer-panel, aiconfig-helpers, mobile.html) with identical behaviour
   and drifting literal styles.  They live here once instead.

   Usage:
     ClipsyncFormat.size(1536)        // => "1.5 KB"
     ClipsyncFormat.speed(2048)       // => "2.0 KB/s"

   Transfer ETAs are deliberately NOT here: the server formats those, because
   they need localized time units (see transfer.eta_* in internal/i18n).

   This module is a plain JS object — no ES module syntax, no framework
   dependency — so the Vue dashboard (index.html) and the vanilla-JS phone
   page (mobile.html) can both load it with a plain <script src>.
   ═══════════════════════════════════════════════════════════════════ */

var ClipsyncFormat = (function () {
  'use strict';

  var KB = 1024;
  var MB = 1024 * 1024;
  var GB = 1024 * 1024 * 1024;

  /**
   * Format a byte count as a short human-readable size.
   * Returns '' for null/undefined/negative/non-finite input, so callers can
   * bind it straight into a template and get nothing rather than "NaN B".
   * @param {number} bytes
   * @returns {string} e.g. "0 B", "1.5 KB", "2.00 GB"
   */
  function size(bytes) {
    var v = Number(bytes);
    if (!isFinite(v) || v < 0) return '';
    if (v < KB) return v + ' B';
    if (v < MB) return (v / KB).toFixed(1) + ' KB';
    if (v < GB) return (v / MB).toFixed(1) + ' MB';
    return (v / GB).toFixed(2) + ' GB';
  }

  /**
   * Format a transfer rate. Unlike size(), zero is not interesting — a
   * stalled transfer should show nothing rather than "0 B/s".
   * @param {number} bytesPerSec
   * @returns {string} e.g. "512 B/s", "1.5 KB/s", "12.0 MB/s"
   */
  function speed(bytesPerSec) {
    var v = Number(bytesPerSec);
    if (!isFinite(v) || v <= 0) return '';
    if (v < KB) return Math.round(v) + ' B/s';
    if (v < MB) return (v / KB).toFixed(1) + ' KB/s';
    return (v / MB).toFixed(1) + ' MB/s';
  }

  return {
    size: size,
    speed: speed,
  };

})();
