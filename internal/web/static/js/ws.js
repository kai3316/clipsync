/* ═══════════════════════════════════════════════════════════════════
   ClipSync WebSocket Client
   Handles real-time communication with the ClipSync server.

   Usage:
     ClipsyncWS.connect('ws://192.168.1.100:9580/ws', 'my-token');
     ClipsyncWS.on('history_updated', function(data) { ... });
     ClipsyncWS.on('devices_updated', function(data) { ... });
     ClipsyncWS.disconnect();

   Auto-reconnects with exponential backoff on connection loss.
   ═══════════════════════════════════════════════════════════════════ */

var ClipsyncWS = (function () {
  'use strict';

  var ws = null;
  var reconnectTimer = null;
  var reconnectDelay = 1000;        // Start at 1 second
  var maxReconnectDelay = 30000;    // Max 30 seconds
  var listeners = {};
  var _connected = false;
  var _url = '';
  var _token = '';
  var _intentionalClose = false;

  /* ═══════════════════════════════════════════════════════════════
     Public API
     ═══════════════════════════════════════════════════════════════ */

  return {
    /**
     * Whether the WebSocket is currently open.
     */
    get connected() {
      return _connected;
    },

    /**
     * Connect to the ClipSync WebSocket server.
     * @param {string} url  - Full WebSocket URL, e.g. ws://host:port/ws
     * @param {string} token - Auth token (appended as query param)
     */
    connect: function (url, token) {
      _url = url;
      _token = token;
      _intentionalClose = false;
      this._doConnect();
    },

    /**
     * Disconnect and stop auto-reconnecting.
     */
    disconnect: function () {
      _intentionalClose = true;
      this._clearReconnectTimer();
      if (ws) {
        try { ws.close(1000, 'Client disconnect'); } catch (e) { /* ignore */ }
        ws = null;
      }
      _connected = false;
    },

    /**
     * Register an event listener.
     * @param {string}   event    - Event name (matches server message type)
     * @param {Function} callback - Called with (data) when event fires
     */
    on: function (event, callback) {
      if (!listeners[event]) {
        listeners[event] = [];
      }
      listeners[event].push(callback);
    },

    /**
     * Remove an event listener.
     * @param {string}   event    - Event name
     * @param {Function} callback - The callback to remove
     */
    off: function (event, callback) {
      if (!listeners[event]) return;
      var idx = listeners[event].indexOf(callback);
      if (idx !== -1) {
        listeners[event].splice(idx, 1);
      }
    },

    /* ═══════════════════════════════════════════════════════════════
       Internal methods
       ═══════════════════════════════════════════════════════════════ */

    /**
     * Establish the WebSocket connection.
     */
    _doConnect: function () {
      // Build URL with token
      var fullUrl = _url;
      if (_token) {
        var sep = fullUrl.indexOf('?') !== -1 ? '&' : '?';
        fullUrl = fullUrl + sep + 'token=' + encodeURIComponent(_token);
      }

      try {
        ws = new WebSocket(fullUrl);
      } catch (e) {
        console.error('[ClipsyncWS] Failed to create WebSocket:', e);
        this._scheduleReconnect();
        return;
      }

      var self = this;

      ws.onopen = function () {
        console.log('[ClipsyncWS] Connected');
        _connected = true;
        reconnectDelay = 1000;  // Reset backoff
        self._dispatch('connected', {});
        // Play connection sound
        if (window.ClipsyncSound) {
          ClipsyncSound.playConnect();
        }
      };

      ws.onmessage = function (event) {
        try {
          var msg = JSON.parse(event.data);
          self._handleMessage(msg);
        } catch (e) {
          console.error('[ClipsyncWS] Failed to parse message:', e, event.data);
        }
      };

      ws.onclose = function (event) {
        _connected = false;
        ws = null;

        if (!_intentionalClose) {
          console.warn('[ClipsyncWS] Connection closed (code: ' + event.code +
            '). Reconnecting in ' + (reconnectDelay / 1000) + 's...');
          self._dispatch('disconnected', { code: event.code, reason: event.reason });
          self._scheduleReconnect();
          // Play disconnection sound
          if (window.ClipsyncSound) {
            ClipsyncSound.playDisconnect();
          }
        } else {
          console.log('[ClipsyncWS] Disconnected (intentional)');
          _intentionalClose = false;
        }
      };

      ws.onerror = function (err) {
        console.error('[ClipsyncWS] WebSocket error:', err);
        _connected = false;
        self._dispatch('error', { error: err });
      };
    },

    /**
     * Schedule reconnection with exponential backoff.
     */
    _scheduleReconnect: function () {
      this._clearReconnectTimer();
      var self = this;
      reconnectTimer = setTimeout(function () {
        console.log('[ClipsyncWS] Reconnecting...');
        self._doConnect();
        // Exponential backoff: double the delay, cap at max
        reconnectDelay = Math.min(reconnectDelay * 2, maxReconnectDelay);
      }, reconnectDelay);
    },

    /**
     * Clear the reconnection timer.
     */
    _clearReconnectTimer: function () {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    },

    /**
     * Dispatch an event to all registered listeners.
     * @param {string} event - Event name
     * @param {*}      data  - Event payload
     */
    _dispatch: function (event, data) {
      var handlers = listeners[event];
      if (!handlers) return;
      for (var i = 0; i < handlers.length; i++) {
        try {
          handlers[i](data);
        } catch (e) {
          console.error('[ClipsyncWS] Listener error for "' + event + '":', e);
        }
      }
    },

    /**
     * Handle an incoming JSON message from the server.
     * Parses the type field and updates the store accordingly.
     *
     * Expected server message format:
     *   { type: "devices_updated", data: { devices: [...] } }
     *   { type: "history_updated", data: { items: [...] } }
     *   { type: "transfer_progress", data: { ... } }
     *   { type: "clipboard_changed", data: {} }
     *   { type: "transfer_complete", data: { ... } }
     *
     * @param {Object} msg - Parsed JSON message
     */
    _handleMessage: function (msg) {
      var type = msg.type;
      var data = msg.data || {};

      if (!type) {
        console.warn('[ClipsyncWS] Message without type:', msg);
        return;
      }

      // Dispatch the raw event so components can react
      this._dispatch(type, data);

      // Auto-update the reactive store if it exists
      var store = window.__CLIPSYNC_STORE__;
      if (!store) return;

      switch (type) {
        case 'open_settings':
          // Tray/dashboard "Settings" requested the settings panel.
          store.settingsPanelVisible = true;
          break;

        case 'devices_updated':
          if (data.devices && Array.isArray(data.devices)) {
            store.devices.splice(0, store.devices.length);
            for (var i = 0; i < data.devices.length; i++) {
              store.devices.push(data.devices[i]);
            }
          }
          break;

        case 'history_updated':
          if (data.items && Array.isArray(data.items)) {
            store.history.splice(0, store.history.length);
            for (var j = 0; j < data.items.length; j++) {
              store.history.push(data.items[j]);
            }
          }
          break;

        case 'transfer_progress':
          // Update or add to activeTransfers. The WS broadcasts progress as a
          // 0..1 fraction (matching FileTransferManager), but the transfer UI
          // and /api/transfer both use 0..100 — so scale it here.
          if (data && data.id !== undefined) {
            var scaled = Object.assign({}, data);
            if (typeof scaled.progress === 'number') {
              scaled.progress = Math.round(scaled.progress * 1000) / 10;
            }
            var existing = store.activeTransfers.findIndex(function (t) {
              return t.id === data.id;
            });
            if (existing !== -1) {
              // Update in place
              Object.assign(store.activeTransfers[existing], scaled);
            } else {
              // Fill the fields the transfer UI expects so the entry does not
              // render with undefined filename/size until the next refetch.
              store.activeTransfers.push(Object.assign({
                filename: '',
                size: 0,
                direction: 'outgoing',
                speed: 0,
                eta: 0,
              }, scaled));
            }
          }
          break;

        case 'transfer_complete':
          // Move from active to history
          if (data && data.id !== undefined) {
            var idx = store.activeTransfers.findIndex(function (t) {
              return t.id === data.id;
            });
            if (idx !== -1) {
              // Mark completed so the history row shows the correct state —
              // the active entry's status was still "transferring"/"paused".
              store.activeTransfers[idx].status = 'completed';
              store.transferHistory.unshift(store.activeTransfers[idx]);
              store.activeTransfers.splice(idx, 1);
            }
          }
          break;

        case 'show_dialog':
          // Server-pushed dialog modal
          if (data && data.dialog_id) {
            store.showDialog(data);
          }
          break;

        case 'close_dialog':
          if (store.activeDialog && (!data || !data.dialog_id || data.dialog_id === store.activeDialog.dialog_id)) {
            store.closeDialog();
          }
          break;

        case 'update_dialog':
          // Update progress / text on an active dialog
          if (store.activeDialog && data && data.dialog_id === store.activeDialog.dialog_id) {
            Object.assign(store.activeDialog, data);
          }
          break;

        case 'toast':
          // Server-pushed toast notification
          if (data && data.message) {
            store.showToast(data.message, data.duration || 3000);
          }
          break;

        default:
          // Unknown message type — dispatched but not auto-handled
          break;
      }
    },
  };

})();
