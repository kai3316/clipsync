/* ═══════════════════════════════════════════════════════════════════
   ClipSync HTTP API Client
   Typed wrapper around the ClipSync REST API. All API calls
   automatically add the auth token as a query parameter.

   Usage:
     ClipsyncAPI.init('http://192.168.1.100:9580', 'my-token');
     ClipsyncAPI.getHistory().then(function(data) { ... });

   All methods return Promises that resolve with the parsed JSON
   response body.

   This module is a plain JS object — no ES module syntax.
   ═══════════════════════════════════════════════════════════════════ */

var ClipsyncAPI = (function () {
  'use strict';

  var _baseUrl = '';
  var _token = '';

  /* ═══════════════════════════════════════════════════════════════
     Public API
     ═══════════════════════════════════════════════════════════════ */

  return {
    /**
     * Initialise the API client.
     * @param {string} baseUrl - Server root URL (e.g. http://192.168.1.100:9580)
     * @param {string} token   - Auth token
     */
    init: function (baseUrl, token) {
      // Strip trailing slash
      _baseUrl = baseUrl.replace(/\/+$/, '');
      _token = token;
    },

    /**
     * Return a type icon emoji for a clipboard content type.
     * Single source of truth used by every component that renders a
     * content-type icon (history items, favorites, overview feed).
     * @param {string} type - e.g. 'TEXT', 'FILE', 'IMAGE', 'RTF', 'HTML', 'URL'
     * @returns {string}
     */
    typeIcon: function (type) {
      var t = String(type || '').toUpperCase();
      if (t.indexOf('IMAGE') !== -1) return '🖼';
      if (t.indexOf('URL') !== -1) return '🔗';
      if (t.indexOf('HTML') !== -1) return '🌐';
      if (t.indexOf('RTF') !== -1) return '📝';
      if (t.indexOf('FILE') !== -1) return '📄';
      return '📄';
    },

    /* ═══════════════════════════════════════════════════════════════
       History endpoints
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Get clipboard history.
     * @returns {Promise<{items: Array}>}
     */
    getHistory: function (params) {
      var qs = '';
      if (params) {
        var parts = [];
        for (var k in params) {
          if (params.hasOwnProperty(k) && params[k] != null) {
            parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
          }
        }
        if (parts.length) qs = '?' + parts.join('&');
      }
      return this._fetch('GET', '/api/history' + qs);
    },

    /**
     * Get a single history entry with its full content (including the base64
     * `types` map). List responses strip `types` to keep the payload small, so
     * clients needing full text (copy / add-to-favorite) fetch it here.
     * @returns {Promise<{item: Object}>}
     */
    getHistoryItem: function (entryId) {
      return this._fetch('GET', '/api/history/item?entry_id=' + encodeURIComponent(entryId));
    },

    /**
     * Push text to the server's clipboard.
     * @param {string} text
     * @returns {Promise<{ok: boolean, len: number}>}
     */
    pushText: function (text) {
      return this._fetch('POST', '/api/push', { text: text });
    },

    /**
     * Translate text via the server's LibreTranslate proxy.
     * @param {string} text - Text to translate
     * @param {string} [targetLang='zh'] - Target language code
     * @param {string} [sourceLang='auto'] - Source language code
     * @returns {Promise<{ok: boolean, translated: string, source_lang: string, target_lang: string}>}
     */
    translate: function (text, targetLang, sourceLang) {
      return this._fetch('POST', '/api/translate', {
        text: text,
        target: targetLang || 'zh',
        source: sourceLang || 'auto',
      });
    },

    /**
     * Delete a history item by entry_id.
     * @param {string|number} entryId
     * @returns {Promise<{ok: boolean}>}
     */
    deleteItem: function (entryId) {
      return this._fetch('POST', '/api/delete', { entry_id: entryId });
    },

    /**
     * Toggle pin status of a history item by entry_id.
     * @param {string|number} entryId
     * @returns {Promise<{ok: boolean, pinned: boolean}>}
     */
    togglePin: function (entryId) {
      return this._fetch('POST', '/api/pin', { entry_id: entryId });
    },

    /**
     * Increment paste count for a history item.
     * @param {number} entryId
     * @returns {Promise<{ok: boolean, paste_count: number}>}
     */
    pasteItem: function (entryId) {
      return this._fetch('POST', '/api/paste', { entry_id: entryId });
    },

    /**
     * Paste all rich formats for a history entry to the server's clipboard.
     * Writes TEXT, HTML, RTF, IMAGE, etc. — not just plain text.
     * @param {string|number} entryId
     * @returns {Promise<{ok: boolean, formats: Array<string>, count: number}>}
     */
    pasteRich: function (entryId) {
      return this._fetch('POST', '/api/paste-rich', { entry_id: entryId });
    },

    /**
     * Batch set pin state on multiple history items.
     * @param {Array<number>} entryIds
     * @param {boolean} pinned
     * @returns {Promise<{ok: boolean, count: number}>}
     */
    batchPin: function (entryIds, pinned) {
      return this._fetch('POST', '/api/batch-pin', {
        entry_ids: entryIds,
        pinned: pinned,
      });
    },

    /**
     * Batch delete multiple history items.
     * @param {Array<number>} entryIds
     * @returns {Promise<{ok: boolean, count: number}>}
     */
    batchDelete: function (entryIds) {
      return this._fetch('POST', '/api/batch-delete', {
        entry_ids: entryIds,
      });
    },

    /**
     * Batch add multiple history items to favorites.
     * @param {Array<number>} entryIds
     * @param {string} [group='']
     * @returns {Promise<{ok: boolean, count: number}>}
     */
    batchFavorite: function (entryIds, group) {
      return this._fetch('POST', '/api/batch-favorite', {
        entry_ids: entryIds,
        group: group || '',
      });
    },

    /* ═══════════════════════════════════════════════════════════════
       Device endpoints
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Get connected devices.
     * @returns {Promise<{devices: Array}>}
     */
    getDevices: function () {
      return this._fetch('GET', '/api/devices');
    },

    /* ═══════════════════════════════════════════════════════════════
       Navigation
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Open a URL on the desktop or a remote device.
     * @param {string} url - The URL to open
     * @param {string} [deviceId] - Target device ID (empty = local)
     * @returns {Promise<{ok: boolean}>}
     */
    navigate: function (url, deviceId) {
      return this._fetch('POST', '/api/nav', {
        url: url,
        device_id: deviceId || '',
      });
    },

    /* ═══════════════════════════════════════════════════════════════
       File endpoints
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Get uploaded files.
     * @returns {Promise<{files: Array}>}
     */
    getFiles: function () {
      return this._fetch('GET', '/api/files');
    },

    /**
     * Upload a file to the server.
     * @param {File} file - A browser File object (from <input type="file">)
     * @param {string} [deviceId] - Target device ID (empty = local)
     * @returns {Promise<{ok: boolean, name: string, size: number}>}
     */
    uploadFile: function (file, deviceId) {
      // Build the URL with token
      var sep = '/api/upload'.indexOf('?') !== -1 ? '&' : '?';
      var url = _baseUrl + '/api/upload' + sep + 'token=' + encodeURIComponent(_token);

      var formData = new FormData();
      formData.append('file', file);
      if (deviceId) {
        formData.append('device_id', deviceId);
      }

      var options = { method: 'POST', body: formData };
      // Mirror the generic _fetch timeout: a stalled upload (phone asleep,
      // network drop) must not hang the send dialog forever.
      var controller = null;
      var timeoutId = null;
      if (typeof AbortController !== 'undefined') {
        controller = new AbortController();
        options.signal = controller.signal;
        timeoutId = setTimeout(function () {
          controller.abort();
        }, 15000);
      }

      return fetch(url, options)
        .then(function (r) {
          clearTimeout(timeoutId);
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .catch(function (e) {
          clearTimeout(timeoutId);
          console.error('[ClipsyncAPI] Upload failed:', e);
          throw e;
        });
    },

    /* ═══════════════════════════════════════════════════════════════
       Favorites endpoints
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Get favorites list.
     * @returns {Promise<Object>}
     */
    getFavorites: function () {
      return this._fetch('GET', '/api/favorites');
    },

    /**
     * Add an item to favorites.
     * @param {Object} item - The item to favorite
     * @returns {Promise<Object>}
     */
    addFavorite: function (item) {
      return this._fetch('POST', '/api/favorites', item);
    },

    /**
     * Delete a favorite by ID.
     * @param {string|number} id
     * @returns {Promise<Object>}
     */
    deleteFavorite: function (id) {
      return this._fetch('DELETE', '/api/favorites', { id: id });
    },

    /**
     * Update a favorite item.
     * @param {string|number} id
     * @param {Object} data - Fields to update
     * @returns {Promise<Object>}
     */
    updateFavorite: function (id, data) {
      var payload = Object.assign({}, data, { id: id });
      return this._fetch('PATCH', '/api/favorites', payload);
    },

    /**
     * Export ALL favorites to a Markdown or plain-text file on the host
     * (same Downloads location the history export uses).
     * @param {string} format - "markdown" (default) or "text"
     * @returns {Promise<{ok: boolean, filepath: string, count: number}>}
     */
    exportFavorites: function (format) {
      return this._fetch('POST', '/api/favorites/export', {
        format: format || 'markdown',
      });
    },

    /* ═══════════════════════════════════════════════════════════════
       AI-config sync endpoints (paired devices only)
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Get every paired peer's AI-config file inventory (metadata only).
     * The backend caches inventories per peer; pass refresh=true to ask it
     * to re-request fresh inventories from the peers.
     * @param {boolean} [refresh=false]
     * @returns {Promise<{peers: Object}>} — { pid: {name, entries[], fetched_at} }
     */
    getAiConfigInventory: function (refresh) {
      return this._fetch('GET',
        '/api/aiconfig/inventory' + (refresh ? '?refresh=1' : ''));
    },

    /**
     * This device's AI-config watch list (GET /api/aiconfig/paths).
     * @returns {Promise<{ok: boolean, paths: string[], summary: Object}>}
     */
    getAiConfigPaths: function () {
      return this._fetch('GET', '/api/aiconfig/paths');
    },

    /**
     * Replace this device's watch list. The backend normalizes, persists,
     * recollects and re-broadcasts the inventory to paired peers.
     * @param {string[]} paths - Root directories ('~' supported)
     * @returns {Promise<{ok: boolean, paths: string[], broadcast_to: number}>}
     */
    setAiConfigPaths: function (paths) {
      return this._fetch('POST', '/api/aiconfig/paths', { paths: paths });
    },

    /**
     * Fetch one remote file's text for preview. The backend truncates at
     * 64KB. The answer is parsed tolerantly: the documented shape is JSON
     * `{ok, content, truncated}`; a raw text body is also accepted so an
     * older/experimental host never breaks the preview modal.
     * @param {string} peerId
     * @param {number} rootIndex
     * @param {string} relPath
     * @returns {Promise<{content: string, truncated: boolean}>}
     */
    previewAiConfigFile: function (peerId, rootIndex, relPath) {
      var sep = '/api/aiconfig/preview'.indexOf('?') !== -1 ? '&' : '?';
      var url = _baseUrl + '/api/aiconfig/preview' + sep +
        'token=' + encodeURIComponent(_token);

      var options = {
        method: 'POST',
        headers: {
          'Accept': '*/*',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          peer_id: peerId,
          root_index: rootIndex,
          rel_path: relPath,
        }),
      };

      var controller = null;
      var timeoutId = null;
      if (typeof AbortController !== 'undefined') {
        controller = new AbortController();
        options.signal = controller.signal;
        timeoutId = setTimeout(function () {
          controller.abort();
        }, 15000);
      }

      return fetch(url, options)
        .then(function (response) {
          clearTimeout(timeoutId);
          return response.text().then(function (bodyText) {
            var parsed = null;
            try { parsed = JSON.parse(bodyText); } catch (e) { parsed = null; }
            if (!response.ok) {
              // Structured backend errors carry a human-readable reason.
              throw new Error(
                (parsed && typeof parsed === 'object' && parsed.error) ||
                ('HTTP ' + response.status));
            }
            if (parsed && typeof parsed === 'object') {
              if (typeof parsed.error === 'string' && parsed.error) {
                throw new Error(parsed.error);
              }
              if (typeof parsed.content === 'string') {
                return { content: parsed.content, truncated: !!parsed.truncated };
              }
            }
            // Plain-text (or unexpected-shape) body — show it verbatim and let
            // the length heuristic drive the truncation notice.
            return { content: bodyText, truncated: bodyText.length >= 65536 };
          });
        })
        .catch(function (e) {
          clearTimeout(timeoutId);
          console.warn('[ClipsyncAPI] AI-config preview failed:', e);
          throw e;
        });
    },

    /**
     * Ask the server to pull the selected files from a peer and land them
     * locally. Results arrive per-file later via the WS `aiconfig_file`
     * event — this call only confirms what was requested.
     * @param {string} peerId
     * @param {Array<{root_index: number, rel_path: string}>} items
     * @param {'overwrite'|'copy'|'append'} mode
     * @returns {Promise<{requested: number}>}
     */
    pullAiConfigFiles: function (peerId, items, mode) {
      return this._fetch('POST', '/api/aiconfig/pull', {
        peer_id: peerId,
        items: items,
        mode: mode,
      });
    },

    /* ═══════════════════════════════════════════════════════════════
       Local AI-config manager endpoints (round 18, no pairing needed)
       ═══════════════════════════════════════════════════════════════ */

    /**
     * This device's own AI-config inventory — watch roots + file metadata.
     * Unlike the peer inventory this needs NO paired device: it is a plain
     * file manager over the local watch list.
     * @returns {Promise<{collected_at: number, roots: Array, entries: Array}>}
     *   roots: [{root_index, path, count}]; entries: [{root_index, rel_path,
     *   size, mtime, sha256}].
     */
    getAiConfigLocal: function () {
      return this._fetch('GET', '/api/aiconfig/local');
    },

    /**
     * Read ONE local AI-config file's text for preview (no pairing). Binary
     * files answer {ok:false, error:'binary'} — the caller shows a "cannot
     * preview" notice instead of crashing.
     * @param {number} rootIndex
     * @param {string} relPath
     * @returns {Promise<{ok: boolean, content: string, truncated: boolean}>}
     */
    getAiConfigLocalItem: function (rootIndex, relPath) {
      return this._fetch('GET',
        '/api/aiconfig/local/item?root_index=' + encodeURIComponent(rootIndex) +
        '&rel_path=' + encodeURIComponent(relPath));
    },

    /**
     * Save edited content back to a LOCAL AI-config file. The backend writes
     * a .bak copy beside it before overwriting (never a silent clobber).
     * @param {number} rootIndex
     * @param {string} relPath
     * @param {string} content
     * @returns {Promise<{ok: boolean}>}
     */
    saveAiConfigLocal: function (rootIndex, relPath, content) {
      return this._fetch('POST', '/api/aiconfig/local/save', {
        root_index: rootIndex,
        rel_path: relPath,
        content: content,
      });
    },

    /**
     * Move a LOCAL AI-config file to the OS Recycle Bin — recoverable, not a
     * hard delete. The backend answers the trash path it moved to.
     * @param {number} rootIndex
     * @param {string} relPath
     * @returns {Promise<{ok: boolean, trashed_to: string}>}
     */
    trashAiConfigLocal: function (rootIndex, relPath) {
      return this._fetch('POST', '/api/aiconfig/local/trash', {
        root_index: rootIndex,
        rel_path: relPath,
      });
    },

    /**
     * Ask the server to open a LOCAL AI-config file's containing folder in the
     * OS file manager (or the file itself when the backend chooses).
     * @param {number} rootIndex
     * @param {string} relPath
     * @returns {Promise<{ok: boolean}>}
     */
    openAiConfigLocal: function (rootIndex, relPath) {
      return this._fetch('POST', '/api/aiconfig/open', {
        root_index: rootIndex,
        rel_path: relPath,
      });
    },

    /* ═══════════════════════════════════════════════════════════════
       Data export / import endpoints
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Export clipboard history to a file (JSON or CSV).
     * @param {string} format - "json" or "csv"
     * @returns {Promise<{ok: boolean, filepath: string, count: number}>}
     */
    exportData: function (format) {
      return this._fetch('POST', '/api/export', { format: format || 'json' });
    },

    /**
     * Import clipboard history from a file path.
     * @param {string} filepath - Path to JSON or CSV file on the server
     * @returns {Promise<{ok: boolean, imported: number}>}
     */
    importData: function (filepath) {
      return this._fetch('POST', '/api/import', { filepath: filepath });
    },

    /**
     * Create a full backup zip.
     * @returns {Promise<{ok: boolean, backup_path: string}>}
     */
    createBackup: function () {
      return this._fetch('POST', '/api/backup');
    },

    /**
     * Restore from a backup zip.
     * @param {string} backupPath - Path to backup zip file
     * @returns {Promise<{ok: boolean, summary: object}>}
     */
    restoreBackup: function (backupPath) {
      return this._fetch('POST', '/api/restore', { backup_path: backupPath });
    },

    /**
     * List available backups.
     * @returns {Promise<{ok: boolean, backups: Array}>}
     */
    listBackups: function () {
      return this._fetch('GET', '/api/backups');
    },

    /**
     * Open a well-known data folder on the host in the OS file manager.
     * @param {string} [which] - 'data' (default) or 'backups'
     * @returns {Promise<{ok: boolean, folder: string}>}
     */
    openDataFolder: function (which) {
      return this._fetch('POST', '/api/data/open-folder', { which: which || 'data' });
    },

    /* ═══════════════════════════════════════════════════════════════
       Settings endpoints
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Get application settings.
     * @returns {Promise<Object>}
     */
    getSettings: function () {
      return this._fetch('GET', '/api/settings');
    },

    /**
     * Update application settings.
     * @param {Object} data - Settings key-value pairs to update
     * @returns {Promise<Object>}
     */
    updateSettings: function (data) {
      return this._fetch('POST', '/api/settings', data);
    },

    /* ═══════════════════════════════════════════════════════════════
       Internet pairing endpoints (round 14)
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Generate a fresh internet pairing code. Generating a new code
     * invalidates any previously generated one (the old code stops working).
     * @returns {Promise<{ok: boolean, code: string}>}
     */
    generateInternetPair: function () {
      return this._fetch('POST', '/api/internetpair/generate');
    },

    /**
     * Enter a 12-character internet pairing code generated on another device.
     * Spaces/dashes are tolerated — the backend normalizes them.
     * @param {string} code - e.g. "ABCD-EFGH-IJKL"
     * @returns {Promise<{ok: boolean, peer_id?: string}>} — 400 when the code
     *   is invalid/expired.
     */
    enterInternetPair: function (code) {
      return this._fetch('POST', '/api/internetpair/enter', { code: code });
    },

    /**
     * Current internet pairing state: the code this device generated (if any)
     * and the devices it has paired with over the internet relay.
     * @returns {Promise<{generated_code?: string, peers: Array}>}
     */
    getInternetPairStatus: function () {
      return this._fetch('GET', '/api/internetpair/status');
    },

    /**
     * Rename (alias) an internet-paired peer. The alias takes display
     * priority over the device's own reported name in every UI.
     * @param {string} peerId - The internet peer's device id
     * @param {string} name   - New alias
     * @returns {Promise<{ok: boolean}>}
     */
    renameInternetPair: function (peerId, name) {
      return this._fetch('POST', '/api/internetpair/rename', {
        peer_id: peerId,
        name: name,
      });
    },

    /**
     * Unpair an internet-paired peer on THIS side only. The pairing is not
     * symmetric — the other side must unpair independently.
     * @param {string} peerId - The internet peer's device id
     * @returns {Promise<{ok: boolean}>}
     */
    unpairInternetPair: function (peerId) {
      return this._fetch('POST', '/api/internetpair/unpair', {
        peer_id: peerId,
      });
    },

    /**
     * Per-peer internet delivery status: how many clips are queued for
     * offline retry (pending) and the recent send history (sends, newest
     * first). Each send is {msg_id, ts, status, preview} with status one of
     * sent / delivered / failed / queued. Defensive: an older backend that
     * predates this endpoint answers 404 and the caller treats that as
     * "no delivery data" (the card simply shows no delivery row).
     * @param {string} peerId - The internet peer's device id
     * @returns {Promise<{pending: number, sends: Array}>}
     */
    getInternetDelivery: function (peerId) {
      return this._fetch('GET',
        '/api/internetdelivery?peer_id=' + encodeURIComponent(peerId || ''));
    },

    /**
     * Pause clipboard sync for a number of minutes (auto-resumes).
     * @param {number} minutes - 1..1440
     * @returns {Promise<{ok: boolean, minutes: number, until: number}>}
     */
    pauseSync: function (minutes) {
      return this._fetch('POST', '/api/sync/pause', { minutes: minutes });
    },

    /**
     * End a timed sync pause immediately.
     * @returns {Promise<{ok: boolean, resumed: boolean}>}
     */
    resumeSync: function () {
      return this._fetch('POST', '/api/sync/resume', {});
    },

    /**
     * Respond to a server-pushed dialog.
     * @param {string} dialogId  - The dialog ID to respond to
     * @param {string} action    - "accept" | "reject" | "select" | "cancel" | "close" | "send" | "ok"
     * @param {*}      [value]   - Optional value (peer_id, url text, etc.)
     * @returns {Promise<Object>}
     */
    respondDialog: function (dialogId, action, value) {
      return this._fetch('POST', '/api/dialog-response', {
        dialog_id: dialogId,
        action: action,
        value: value,
      });
    },

    /* ═══════════════════════════════════════════════════════════════
       Transfers endpoints
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Get transfer history.
     * @returns {Promise<Object>}
     */
    getTransfers: function () {
      return this._fetch('GET', '/api/transfer');
    },

    /* ═══════════════════════════════════════════════════════════════
       Server status
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Check server status.
     * @returns {Promise<{ok: boolean, device: string}>}
     */
    getStatus: function () {
      return this._fetch('GET', '/api/status');
    },

    /* ═══════════════════════════════════════════════════════════════
       Overview endpoint
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Get overview dashboard stats.
     * @returns {Promise<{overview: Object}>}
     */
    getOverview: function () {
      return this._fetch('GET', '/api/overview');
    },

    /* ═══════════════════════════════════════════════════════════════
       Speed test endpoints
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Start a speed test.
     * @returns {Promise<{ok: boolean}>}
     */
    startSpeedTest: function () {
      return this._fetch('POST', '/api/speed-test');
    },

    /**
     * Poll speed test result.
     * @returns {Promise<{done: boolean, mbps: number|null, progress: number, status: string}>}
     */
    getSpeedTestResult: function () {
      return this._fetch('GET', '/api/speed-test');
    },

    /* ═══════════════════════════════════════════════════════════════
       Device actions
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Update device note.
     * @param {string} peerId - Peer device ID
     * @param {string} note - Note text
     * @returns {Promise<{ok: boolean}>}
     */
    updateDeviceNote: function (peerId, note) {
      return this._fetch('POST', '/api/device/note', {
        peer_id: peerId,
        note: note,
      });
    },

    /**
     * Pair with a device.
     * @param {string} peerId - Peer device ID
     * @param {string} code - Pairing code
     * @returns {Promise<{ok: boolean}>}
     */
    pairDevice: function (peerId, code) {
      return this._fetch('POST', '/api/device/pair', {
        peer_id: peerId,
        code: code,
      });
    },

    /**
     * Unpair / reject a device.
     * @param {string} peerId - Peer device ID
     * @returns {Promise<{ok: boolean}>}
     */
    unpairDevice: function (peerId) {
      return this._fetch('POST', '/api/device/unpair', {
        peer_id: peerId,
      });
    },

    /**
     * Send pairing response (accept or reject).
     * @param {string} peerId - Peer device ID
     * @param {string} action - 'confirm' or 'reject'
     * @param {string} code - Pairing code (for confirm)
     * @returns {Promise<{ok: boolean}>}
     */
    sendPairingResponse: function (peerId, action, code) {
      if (action === 'reject') {
        return this._fetch('POST', '/api/device/reject', {
          peer_id: peerId,
        });
      }
      return this._fetch('POST', '/api/device/pair', {
        peer_id: peerId,
        code: code || '',
      });
    },

    /**
     * Connect to a peer device.
     * @param {string} peerId - Peer device ID
     * @returns {Promise<{ok: boolean}>}
     */
    connectDevice: function (peerId) {
      return this._fetch('POST', '/api/device/connect', {
        peer_id: peerId,
      });
    },

    /**
     * Disconnect from a peer device.
     * @param {string} peerId - Peer device ID
     * @returns {Promise<{ok: boolean}>}
     */
    disconnectDevice: function (peerId) {
      return this._fetch('POST', '/api/device/disconnect', {
        peer_id: peerId,
      });
    },

    /**
     * Forget / remove a peer device.
     * @param {string} peerId - Peer device ID
     * @returns {Promise<{ok: boolean}>}
     */
    forgetDevice: function (peerId) {
      return this._fetch('POST', '/api/device/forget', {
        peer_id: peerId,
      });
    },

    /* ═══════════════════════════════════════════════════════════════
       Transfer actions
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Open a local file on the host desktop.
     * @param {string} path - Absolute path to the file on the server host
     * @returns {Promise<{ok: boolean}>}
     */
    openFile: function (path) {
      return this._fetch('POST', '/api/file/open', { path: path });
    },

    revealFile: function (path) {
      return this._fetch('POST', '/api/file/reveal', { path: path });
    },

    /**
     * Restart the ClipSync application.
     * @returns {Promise<{ok: boolean}>}
     */
    restartApp: function () {
      return this._fetch('POST', '/api/restart', {});
    },

    /**
     * Cancel a transfer.
     * @param {string} transferId
     * @returns {Promise<{ok: boolean}>}
     */
    cancelTransfer: function (transferId) {
      return this._fetch('POST', '/api/transfer/cancel', { transfer_id: transferId });
    },

    /**
     * Pause a transfer.
     * @param {string} transferId
     * @returns {Promise<{ok: boolean}>}
     */
    pauseTransfer: function (transferId) {
      return this._fetch('POST', '/api/transfer/pause', { transfer_id: transferId });
    },

    /**
     * Resume a transfer.
     * @param {string} transferId
     * @returns {Promise<{ok: boolean}>}
     */
    resumeTransfer: function (transferId) {
      return this._fetch('POST', '/api/transfer/resume', { transfer_id: transferId });
    },

    /**
     * Re-send a FAILED OUTGOING transfer from history to its original peer.
     * The backend looks the row up by its original transfer_id.
     * @param {string} transferId
     * @returns {Promise<{ok: boolean}>}
     */
    retryTransfer: function (transferId) {
      return this._fetch('POST', '/api/transfer/retry', { transfer_id: transferId });
    },

    /**
     * Cancel every ACTIVE transfer at once. The backend resolves the live
     * list itself and reuses the per-transfer cancel path for each row.
     * @returns {Promise<{ok: boolean, cancelled: number}>}
     */
    cancelAllTransfers: function () {
      return this._fetch('POST', '/api/transfer/cancel-all', {});
    },

    /**
     * Clear all history items.
     * @returns {Promise<{ok: boolean}>}
     */
    clearHistory: function () {
      return this._fetch('POST', '/api/history/clear');
    },

    /* ═══════════════════════════════════════════════════════════════
       Nearby Chat endpoints
       ═══════════════════════════════════════════════════════════════ */

    /**
     * List devices that can start a nearby chat session.
     * @returns {Promise<{devices: Array}>}
     */
    chatDevices: function () {
      return this._fetch('GET', '/api/chat/devices');
    },

    /**
     * List chat sessions.
     * @returns {Promise<{sessions: Array}>}
     */
    chatSessions: function () {
      return this._fetch('GET', '/api/chat/sessions');
    },

    /**
     * Get messages for a chat session.
     * @param {string} sessionId
     * @returns {Promise<{messages: Array}>}
     */
    chatMessages: function (sessionId) {
      return this._fetch('GET', '/api/chat/messages?session_id=' + encodeURIComponent(sessionId));
    },

    /**
     * Invite a peer to a chat session.
     * @param {string} peerId
     * @param {string} peerName
     * @returns {Promise<{session_id: string|null, connecting?: boolean}>}
     */
    chatInvite: function (peerId, peerName) {
      return this._fetch('POST', '/api/chat/invite', {
        peer_id: peerId,
        peer_name: peerName || '',
      });
    },

    /**
     * Send a text message in a chat session.
     * @param {string} sessionId
     * @param {string} text
     * @returns {Promise<{ok: boolean}>}
     */
    chatSendText: function (sessionId, text) {
      return this._fetch('POST', '/api/chat/text', {
        session_id: sessionId,
        text: text,
      });
    },

    /**
     * Re-send a FAILED outgoing chat text (the ⟳ button on a failed bubble).
     * @param {string} sessionId
     * @param {string} entryId
     * @returns {Promise<{ok: boolean}>}
     */
    chatResendText: function (sessionId, entryId) {
      return this._fetch('POST', '/api/chat/resend', {
        session_id: sessionId,
        entry_id: entryId,
      });
    },

    /**
     * Send a file in a chat session. `filePath` is the server-side path or
     * basename (returned by uploadFile) — the backend resolves it against the
     * receive directory.
     * @param {string} sessionId
     * @param {string} filePath
     * @returns {Promise<{transfer_id: string}>}
     */
    chatSendFile: function (sessionId, filePath) {
      return this._fetch('POST', '/api/chat/file', {
        session_id: sessionId,
        file_path: filePath,
      });
    },

    /**
     * Accept / decline / cancel a file transfer.
     * @param {string} sessionId
     * @param {string} transferId
     * @param {'accept'|'decline'|'cancel'} action
     * @returns {Promise<{ok: boolean}>}
     */
    chatFileAction: function (sessionId, transferId, action) {
      return this._fetch('POST', '/api/chat/file/' + action, {
        session_id: sessionId,
        transfer_id: transferId,
      });
    },

    /**
     * Session-level action: accept an incoming invite, decline it, close the
     * session, or mark it read.
     * @param {string} sessionId
     * @param {'accept'|'decline'|'close'|'read'} action
     * @returns {Promise<{ok: boolean}>}
     */
    chatSessionAction: function (sessionId, action) {
      return this._fetch('POST', '/api/chat/' + action, {
        session_id: sessionId,
      });
    },

    /**
     * Mute/unmute a chat peer (stops its desktop message notification/sound
     * and its unread badge).  The backend persists the mute set.
     * @param {string} peerId
     * @param {boolean} muted
     * @returns {Promise<{ok: boolean, muted: string[]}>}
     */
    chatMute: function (peerId, muted) {
      return this._fetch('POST', '/api/chat/mute', {
        peer_id: peerId,
        muted: !!muted,
      });
    },

    /**
     * Build the authenticated URL for downloading a received chat file.
     * (The endpoint returns the saved file binary, so it can't go through
     * the JSON `_fetch` wrapper — the caller uses it as a link target.)
     * @param {string} transferId
     * @returns {string}
     */
    chatDownloadUrl: function (transferId) {
      var sep = '/api/chat/download'.indexOf('?') !== -1 ? '&' : '?';
      return _baseUrl + '/api/chat/download' + sep +
        'transfer_id=' + encodeURIComponent(transferId) +
        '&token=' + encodeURIComponent(_token);
    },

    /* ═══════════════════════════════════════════════════════════════
       Window control (for frameless title bar)
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Send a window control command.
     * Only 'close' is implemented by the backend /api/window endpoint.
     * @param {string} action - 'close'
     * @returns {Promise<{ok: boolean}>}
     */
    windowAction: function (action) {
      return this._fetch('POST', '/api/window', { action: action });
    },

    /* ═══════════════════════════════════════════════════════════════
       Internal: generic fetch wrapper
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Generic fetch wrapper that adds token authentication and a timeout.
     *
     * @param {string} method - HTTP method (GET, POST, PUT, DELETE)
     * @param {string} path   - API path (e.g. '/api/history')
     * @param {*}      [body] - Request body (will be JSON.stringify'd)
     * @param {number} [timeoutMs=15000] - Request timeout in milliseconds
     * @returns {Promise<Object>} Parsed JSON response
     */
    _fetch: function (method, path, body, timeoutMs) {
      // Build URL with token
      var sep = path.indexOf('?') !== -1 ? '&' : '?';
      var url = _baseUrl + path + sep + 'token=' + encodeURIComponent(_token);

      var options = {
        method: method,
        headers: {
          'Accept': 'application/json',
        },
      };

      if (body !== undefined && body !== null) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
      }

      // AbortController for timeout — feature-detect so an old webview that
      // predates AbortController still issues the request (just without the
      // abort/timeout guard) instead of crashing on the constructor.
      var controller = null;
      var timeoutId = null;
      if (typeof AbortController !== 'undefined') {
        controller = new AbortController();
        options.signal = controller.signal;
        timeoutId = setTimeout(function () {
          controller.abort();
        }, timeoutMs || 15000);
      }

      return fetch(url, options)
        .then(function (response) {
          clearTimeout(timeoutId);
          // Try to parse JSON even on error statuses
          return response.json().then(function (data) {
            if (!response.ok) {
              var error = new Error(data.error || ('HTTP ' + response.status));
              error.status = response.status;
              error.data = data;
              throw error;
            }
            return data;
          }).catch(function (parseErr) {
            clearTimeout(timeoutId);
            // If JSON parsing failed but response was OK, it's a real error
            if (parseErr instanceof SyntaxError && response.ok) {
              return {}; // Empty response, treat as success
            }
            // The timeout fired mid-body-read: fetch rejected with an
            // AbortError. Let the outer catch report it as a timeout instead
            // of fabricating an "HTTP 200" error from an aborted response.
            if (parseErr && parseErr.name === 'AbortError') {
              throw parseErr;
            }
            if (parseErr.status !== undefined) {
              throw parseErr; // Already our error, re-throw
            }
            // Non-JSON error response
            var err = new Error('HTTP ' + response.status);
            err.status = response.status;
            throw err;
          });
        })
        .catch(function (e) {
          clearTimeout(timeoutId);
          // Network errors (no connection, timeout, etc.)
          if (e.name === 'AbortError') {
            console.warn('[ClipsyncAPI] Request timed out: ' + method + ' ' + path);
          } else if (e instanceof TypeError) {
            console.error('[ClipsyncAPI] Network error:', e);
          }
          throw e;
        });
    },
  };

})();
