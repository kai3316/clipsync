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

      return fetch(url, {
        method: 'POST',
        body: formData,
      })
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .catch(function (e) {
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
     * Clear all history items.
     * @returns {Promise<{ok: boolean}>}
     */
    clearHistory: function () {
      return this._fetch('POST', '/api/history/clear');
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

      // AbortController for timeout
      var controller = new AbortController();
      options.signal = controller.signal;
      var timeoutId = setTimeout(function () {
        controller.abort();
      }, timeoutMs || 15000);

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
