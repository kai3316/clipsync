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

  // Debounced overview refresh. history_updated / history_item_deleted /
  // history_clear can arrive in bursts (a multi-clip paste, a batch delete),
  // and every one would otherwise fire a redundant GET /api/overview. Merge
  // them into a single refresh 500ms after the last event.
  var _overviewTimer = null;

  function _scheduleOverviewRefresh() {
    if (_overviewTimer) {
      clearTimeout(_overviewTimer);
    }
    _overviewTimer = setTimeout(function () {
      _overviewTimer = null;
      var store = window.__CLIPSYNC_STORE__;
      if (store) store.fetchOverview();
    }, 500);
  }

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
        // Play connection sound — honour the sound preference. Gated on
        // ClipsyncSound.known: before the localStorage/server preference is
        // actually known (store.soundEnabled defaults to true until the
        // settings load), a sound-disabled user would hear the startup chime
        // on every launch. playConnect() itself respects the loaded value.
        if (window.ClipsyncSound && ClipsyncSound.known) {
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
          // Play disconnection sound — honour the server-side sound setting.
          var store = window.__CLIPSYNC_STORE__;
          if (window.ClipsyncSound && (!store || store.soundEnabled)) {
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
        case 'close_window':
          // The app is quitting — close this dashboard window so no orphaned
          // browser window/process is left behind.
          try {
            window.close();
          } catch (e) { /* window may not be script-openable */ }
          break;

        case 'onboarding_required':
          // A backup restore flipped the config back to "fresh install"
          // (language_chosen=False).  __CLIPSYNC_FRESH__ was baked into this
          // page at serve time, so without this event a running dashboard
          // would never re-surface the wizard until a manual refresh.  Same
          // desktop gating as app.js mounted(): the wizard renames THIS
          // computer, which makes no sense on a phone/tablet client.
          var obLocal = /^(localhost|127\.0\.0\.1)$/.test(window.location.hostname || '');
          var obDesktop = obLocal ||
            (window.matchMedia && window.matchMedia('(pointer: fine)').matches);
          if (store.deviceId && obDesktop) {
            store.onboardingDone = false;
            try { localStorage.removeItem('clipsync_onboarded'); } catch (e) { /* ignore */ }
            store.showOnboarding = true;
          }
          break;

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
          // The broadcast carries the authoritative pending_pairings list too,
          // so keep the pairing-request section in sync with the backend.
          if (data.pending_pairings && Array.isArray(data.pending_pairings)) {
            store.syncPairingRequests(data.pending_pairings);
          }
          // And the removed-devices archive, so restore/purge/forget reflect
          // instantly on the device page.
          if (data.removed && Array.isArray(data.removed)) {
            store.syncRemovedDevices(data.removed);
          }
          break;

        case 'history_updated':
          if (data.items && Array.isArray(data.items)) {
            var incoming = data.items;
            var histLimit = (store.settingsCache && store.settingsCache.web_history_limit) || 30;
            // The broadcast is the newest page-1 snapshot. If the user has
            // loaded beyond the first page (via "Load more"), merge the
            // broadcast into the existing list instead of clobbering it, so
            // their loaded pages don't shrink on every background clipboard
            // change.
            var hasLoadedMore = store.history.length > histLimit;
            // Bump the mutation tick when this broadcast actually merges NEW
            // data (a newly-added entry, a same-id content/pin/timestamp
            // change, or an in-place update in the paged merge), so an
            // in-flight calibration (store.js) abandons its write-back instead
            // of overwriting the concurrent new item.  A pure display refresh
            // (same entries) does NOT bump.  The bump happens BEFORE the
            // ghost-triggered calibration below so that calibration records a
            // startTick that already includes this broadcast's merge.
            if (!hasLoadedMore) {
              // Nothing loaded past the first page — replace wholesale via the
              // shared helper, which detects change on every user-visible
              // field, filters malformed null rows, and bumps the reconcile
              // guard only for real changes (an identical re-broadcast is a
              // display refresh and does NOT bump).
              store.replaceHistory(incoming);
              // Cursor tracks the VISIBLE list length via the shared helper
              // (the invariant every cursor-shrink path depends on).  If a
              // malformed null row is ever filtered, the slot is re-requested
              // once and deduped by the next Load More; harmless.
              store.setHistoryCursor(data.total != null ? data.total : null);
            } else {
              // Upsert/prepend the page-1 snapshot via the shared helper — it
              // updates matching rows in place, prepends genuinely-new rows at
              // the top (dedupe — no duplicates), and bumps the mutation tick
              // only when the merge actually changed the list.
              store.mergeHistoryFresh(incoming);
              // The prepended items now occupy the top of the loaded list.
              // Align the cursor via the shared helper (list length, not a
              // "+fresh.length" delta: dedupe may have discarded incoming
              // duplicates, so the delta would overshoot the real count).
              store.setHistoryCursor(data.total != null ? data.total : null);
              // Refresh "has more" from the broadcast total when present so
              // Load-more stays accurate after new items arrive.  A missed
              // history_item_deleted broadcast leaves ghost rows in the loaded
              // list; history is ordered pinned-DESC, timestamp-DESC, so a
              // deleted row can sit at the TOP (pinned) or in the MIDDLE — a
              // tail-trim would evict LIVE oldest entries and keep the ghost.
              // When ghosts are present (length > total), run a full
              // calibration: fetch the authoritative list and replace wholesale
              // (mirrors the page-1 refresh in app.js).
              if (data.total != null) {
                if (store.history.length > data.total) {
                  // Ghost rows from a missed delete — full calibration via the
                  // shared store.calibrateHistory() (throttled + race guarded —
                  // see store.js; mirrors the page-1 refresh in app.js).
                  store.calibrateHistory(data.total);
                } else {
                  store.setHistoryCursor(data.total);
                }
              }
            }
          }
          // A clipboard change also means the overview's recent-activity feed
          // and stats changed — refresh it so the feed stays live without
          // waiting for the 5s poll.
          _scheduleOverviewRefresh();
          break;

        case 'history_item_deleted':
          // Broadcast after a delete / batch-delete so every client removes
          // the entries instead of only the one that issued the request
          // (the history_updated upsert merge can't express deletions).
          // Bump the mutation tick so an in-flight calibration (store.js)
          // abandons its write-back instead of resurrecting these rows —
          // bumped even when the ids aren't in the local list, because the
          // in-flight calibration's snapshot may still carry them.
          store.historyMutationTick += 1;
          if (data && Array.isArray(data.entry_ids)) {
            // Removal via the shared helper (also prunes selectedIds).
            var removedCount = store.removeHistoryItems(data.entry_ids);
            // Removed items no longer occupy the loaded list, so the
            // pagination cursor must shrink by the same count (mirrors the
            // local delete path's historyOffset--).
            if (removedCount > 0) {
              store.historyOffset = Math.max(0, store.historyOffset - removedCount);
            }
            if (data.total != null) {
              store.historyHasMore = store.history.length < data.total;
            }
          }
          _scheduleOverviewRefresh();
          break;

        case 'history_clear':
          // History was wiped on another client — reset the whole list and
          // the pagination cursor so "Load more" can't skip shifted items.
          // Bump the mutation tick so an in-flight calibration abandons its
          // write-back instead of re-populating the wiped list (via the shared
          // clearHistory helper).
          store.clearHistory();
          store.historyOffset = 0;
          store.historyHasMore = false;
          store.selectedIds = new Set();
          _scheduleOverviewRefresh();
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
              // The template keys on direction 'up'/'down', but WS payloads
              // may carry the manager's 'outgoing'/'incoming' — map them so a
              // send shows the up arrow instead of rendering as a download.
              var dir = (scaled.direction === 'up' || scaled.direction === 'outgoing') ? 'up'
                : 'down';  // 'incoming' and unknown values default to a download.
              var name = scaled.filename || scaled.file_name || '';
              store.activeTransfers.push(Object.assign({
                filename: name,
                size: 0,
                direction: dir,
                speed: 0,
                eta: 0,
              }, scaled, {
                direction: dir,   // never let the raw payload override the mapping
                filename: name,   // keep the display-name fallback stable
              }));
            }
          }
          break;

        case 'transfer_complete':
          // Move from active to history. The broadcast carries `success` and
          // (for cancels) `cancelled`, so stamp a truthful status instead of
          // assuming every completion is a success — otherwise cancelled
          // transfers briefly read as "Completed" until the next refetch.
          if (data && data.id !== undefined) {
            var idx = store.activeTransfers.findIndex(function (t) {
              return t.id === data.id;
            });
            if (idx !== -1) {
              var fin = 'completed';
              if (data.cancelled) {
                fin = 'cancelled';
              } else if (data.success === false) {
                fin = 'failed';
              }
              store.activeTransfers[idx].status = fin;
              store.transferHistory.unshift(store.activeTransfers[idx]);
              store.activeTransfers.splice(idx, 1);
            }
          }
          break;

        case 'chat_sessions':
          // Authoritative session-list snapshot (invite created/accepted/
          // declined, session closed, ...). Replace the list and recompute the
          // total unread for the sidebar badge.
          if (data && data.sessions && Array.isArray(data.sessions)) {
            var sessList = data.sessions.slice();
            // The backend may still report unread for the session the user is
            // actively viewing (mark-read is fire-and-forget). Letting the
            // authoritative list clobber it back to non-zero would flash the
            // badge and trigger a redundant full refetch — force the active
            // session's unread to 0 and keep every other session's count.
            if (store.activeChatSession) {
              for (var ci = 0; ci < sessList.length; ci++) {
                if (sessList[ci] && sessList[ci].session_id === store.activeChatSession) {
                  sessList[ci] = Object.assign({}, sessList[ci], { unread: 0 });
                  break;
                }
              }
            }
            store.replaceChatSessions(sessList);
          }
          break;

        case 'chat_message':
          // A new entry in a conversation. If it belongs to the session that
          // is currently open, push the entry in immediately (deduped, capped)
          // so the bubble appears without a full refetch; otherwise just bump
          // that session's unread counter.
          if (data && data.session_id) {
            var csIdx = store.chatSessions.findIndex(function (s) {
              return s.session_id === data.session_id;
            });
            if (csIdx !== -1) {
              var cs = store.chatSessions[csIdx];
              if (data.session_id === store.activeChatSession) {
                cs.unread = 0;
                // Keep the backend's unread counter accurate while the user is
                // actively viewing the conversation (fire-and-forget).
                if (window.ClipsyncAPI && window.ClipsyncAPI.chatSessionAction) {
                  window.ClipsyncAPI.chatSessionAction(data.session_id, 'read').catch(function () {});
                }
              } else if (data.entry && !data.entry.outgoing &&
                         !store.isChatMuted(cs.peer_id)) {
                // Only incoming messages bump unread — the backend never
                // counts your own echoed outgoing entry (send_text/send_file
                // fire _on_message too), so the badge must not either.  A
                // muted peer never bumps the badge, so a silent device stays
                // silent (recalcChatUnread also excludes muted peers).
                cs.unread = (cs.unread || 0) + 1;
              }
              if (data.entry) {
                if (data.entry.kind === 'text') {
                  cs.last_preview = data.entry.text;
                } else if (data.entry.kind === 'file') {
                  cs.last_preview = (data.entry.outgoing ? '↑ ' : '↓ ') + (data.entry.file_name || '');
                }
                if (data.entry.ts) cs.last_activity_ts = data.entry.ts;
              }
            }
            if (data.session_id === store.activeChatSession && data.entry) {
              var cmEntry = data.entry;
              var cmDup = store.chatMessages.findIndex(function (m) {
                return m.entry_id !== undefined && m.entry_id === cmEntry.entry_id;
              });
              if (cmDup === -1) {
                store.chatMessages.push(cmEntry);
                if (store.chatMessages.length > 200) {
                  store.chatMessages.splice(0, store.chatMessages.length - 200);
                }
              } else {
                // Known entry re-pushed with changed state (e.g. a resent
                // text flipping failed → done): merge instead of dropping,
                // otherwise the bubble keeps its stale status forever.
                var cmExisting = store.chatMessages[cmDup];
                Object.keys(cmEntry).forEach(function (k) {
                  cmExisting[k] = cmEntry[k];
                });
              }
            }
            store.recalcChatUnread();
          }
          break;

        case 'chat_progress':
          // File-transfer progress (0..1 fraction). Patch the matching file
          // card in the open conversation so the bar moves live.
          if (data && data.session_id && data.transfer_id && typeof data.fraction === 'number') {
            if (data.session_id === store.activeChatSession) {
              var cpIdx = store.chatMessages.findIndex(function (m) {
                return m.transfer_id === data.transfer_id;
              });
              if (cpIdx !== -1) {
                store.chatMessages[cpIdx].fraction = data.fraction;
                if (store.chatMessages[cpIdx].status !== 'sending') {
                  store.chatMessages[cpIdx].status = 'sending';
                }
              }
            }
          }
          break;

        case 'chat_file_done':
          // Terminal state for a file transfer: success + saved_path + status.
          if (data && data.session_id && data.transfer_id) {
            if (data.session_id === store.activeChatSession) {
              var fdIdx = store.chatMessages.findIndex(function (m) {
                return m.transfer_id === data.transfer_id;
              });
              if (fdIdx !== -1) {
                var fd = store.chatMessages[fdIdx];
                fd.success = data.success;
                if (data.saved_path) fd.saved_path = data.saved_path;
                // The service reports success as "success" and failures as
                // "rejected"/"cancelled_by_peer"/"error_size_mismatch"/
                // "peer_offline"/…; the file card only understands the small
                // vocabulary below, so map instead of copying verbatim.
                if (data.success) {
                  fd.status = 'done';
                  fd.fraction = 1;
                } else if (data.status === 'cancelled' || data.status === 'cancelled_by_peer') {
                  fd.status = 'cancelled';
                } else if (data.status === 'declined' || data.status === 'rejected') {
                  fd.status = 'declined';
                } else {
                  fd.status = 'failed';
                }
              }
            }
          }
          break;

        case 'relay_state':
          // Internet-sync relay status transition (off/connecting/online/
          // error).  Only known states are accepted so a malformed or
          // future-format payload can't poison the settings display.
          if (data && ['off', 'connecting', 'online', 'error'].indexOf(data.state) !== -1) {
            store.relayState = data.state;
            // Current broker travels with the state event (empty when
            // offline); it is diagnostic-only, so only non-empty values are
            // accepted to keep the display from clobbering to '' on a
            // transition that simply didn't carry a broker.
            if (typeof data.broker === 'string' && data.broker) {
              store.currentRelayBroker = data.broker;
            }
            // Keep the cached copy in sync so reopening the settings panel
            // shows the latest state even before the next settings fetch.
            if (store.settingsCache) {
              store.settingsCache = Object.assign({}, store.settingsCache, {
                internet_sync_state: data.state,
              });
            }
          }
          break;

        case 'netpair_peer':
          // A device paired / unpaired with this one over the internet relay.
          // Only the known status vocabulary is accepted so a malformed
          // payload can't poison the list.
          if (data && data.peer_id &&
              (data.status === 'paired' || data.status === 'unpaired')) {
            store.applyNetpairPeer(data);
          }
          break;

        case 'internet_delivery':
          // Delivery-status transition for one internet-sent message
          // ({peer_id, msg_id, status}). Only the known status vocabulary
          // reaches the store, which stamps the chat-bubble map
          // (msg_id → status), updates the peer card's one-line status +
          // "待补发 N" badge, and refreshes the peer's last-sync time for
          // real contact events — no full-page refresh.
          if (data && data.peer_id &&
              ['sent', 'delivered', 'failed', 'queued'].indexOf(data.status) !== -1) {
            store.applyInternetDelivery(data);
          }
          break;

        case 'show_dialog':
          // Server-pushed dialog modal
          if (data && data.dialog_id) {
            store.showDialog(data);
          }
          break;

        case 'close_dialog':
          // Pass the id through so the store can also drop a force-closed
          // dialog that was still queued (not yet on screen).
          if (data && data.dialog_id) {
            store.closeDialog(data.dialog_id);
          } else {
            store.closeDialog();
          }
          break;

        case 'update_dialog':
          // Update progress / text on the active dialog, and patch any queued
          // dialog with the same id in place so it shows current state once
          // promoted to the front.
          if (data && data.dialog_id) {
            if (store.activeDialog && data.dialog_id === store.activeDialog.dialog_id) {
              Object.assign(store.activeDialog, data);
            } else {
              for (var qDi = 0; qDi < store.dialogQueue.length; qDi++) {
                if (store.dialogQueue[qDi] && store.dialogQueue[qDi].dialog_id === data.dialog_id) {
                  Object.assign(store.dialogQueue[qDi], data);
                  break;
                }
              }
            }
          }
          break;

        case 'aiconfig_file':
          // Per-file result of an AI-config pull (one event per file). Only
          // the known status vocabulary is accepted so a malformed payload
          // can't poison the badge list; store.applyAiConfigFileResult does
          // the toast + rolling-list bookkeeping.
          if (data && data.rel_path &&
              ['saved', 'copied', 'appended', 'error'].indexOf(data.status) !== -1) {
            store.applyAiConfigFileResult(data);
          }
          break;

        case 'connect_rejected':
          // A peer refused our connection attempt (it sent its rejection
          // marker after its identity frame — its user removed/forgot us).
          // Without this the "Connect" click looks like a silent no-op: the
          // card just stays Discovered.  Localize here so the payload stays
          // language-neutral.
          if (data && data.peer_id) {
            store.showToast(store.t('device.connect_rejected', { name: data.name || '' }), 3500, 'error');
          }
          break;

        case 'toast':
          // Server-pushed toast notification
          if (data && data.message) {
            store.showToast(data.message, data.duration || 3000);
          }
          break;

        case 'update_state':
          // Update download progress / ready / failed lifecycle.  The raw
          // `_dispatch` above already fired listeners; mirror the payload into
          // the store so any component can read store.updateState.  Only
          // accepted when it looks like a state object.
          if (data && typeof data === 'object' && data.phase) {
            store.updateState = Object.assign({}, store.updateState, data);
          }
          break;

        default:
          // Unknown message type — dispatched but not auto-handled
          break;
      }
    },
  };

})();
