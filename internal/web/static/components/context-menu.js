/* ═══════════════════════════════════════════════════════════════════
   ClipSync Context Menu Component
   Right-click context menu for history items and device cards.
   Neo-futuristic glass design with staggered item animations.
   Auto-closes on click-outside, Escape, resize, and scroll.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  // Decode the base64 TEXT bytes from a history item's `types` map, falling
  // back to the (possibly truncated) preview. History list responses no longer
  // carry `types`, so callers fetch the full item via getHistoryItem first.
  function decodeTypesText(types, fallback) {
    if (types && types.TEXT) {
      try {
        var bytes = Uint8Array.from(atob(types.TEXT), function (c) { return c.charCodeAt(0); });
        var decoded = new TextDecoder('utf-8').decode(bytes);
        if (decoded) return decoded;
      } catch (e) { /* keep fallback */ }
    }
    return fallback || '';
  }

  window.__CLIPSYNC_COMPONENTS__['context-menu'] = {
    inject: ['store'],

    data: function () {
      return {
        // Measured height of the rendered menu (0 = not yet measured).
        _menuHeight: 0,
        // Index of the currently focused menuitem (roving tabindex), -1 = none.
        _focusedIndex: -1,
      };
    },

    computed: {
      menuStyle: function () {
        var cm = this.store.contextMenu || {};
        var x = cm.x || 0;
        var y = cm.y || 0;
        var w = 220;
        var mode = cm.mode || 'history-item';
        // Use the measured height once the menu is rendered so the bottom-edge
        // clamp uses the real footprint instead of a hardcoded estimate that
        // leaves the last items unreachable near the bottom of the viewport.
        var h = this._menuHeight || (mode === 'history-item' ? 300 : 170);
        if (x + w > window.innerWidth - 8) x = window.innerWidth - w - 8;
        if (y + h > window.innerHeight - 8) y = window.innerHeight - h - 8;
        if (x < 8) x = 8;
        if (y < 8) y = 8;
        return {
          position: 'fixed',
          left: x + 'px',
          top: y + 'px',
          // Below the modal overlay layer (z-400) so the menu never floats
          // above a confirm/prompt dialog.
          zIndex: String(390),
          minWidth: '200px',
        };
      },

      targetItem: function () {
        var cm = this.store.contextMenu || {};
        return cm.mode === 'history-item' ? cm.target : null;
      },

      // A URL worth offering "open in browser" for: a link-type history item,
      // or any text entry that is itself an http(s) URL.
      linkUrl: function () {
        var item = this.targetItem;
        if (!item) return '';
        var t = (item.text_preview || '').trim();
        if (item.content_type === 'link' || /^https?:\/\//i.test(t)) return t;
        return '';
      },

      targetDevice: function () {
        var cm = this.store.contextMenu || {};
        return cm.mode === 'device' ? cm.target : null;
      },

      targetSession: function () {
        var cm = this.store.contextMenu || {};
        return cm.mode === 'chat-session' ? cm.target : null;
      },

      targetChatMsg: function () {
        var cm = this.store.contextMenu || {};
        return cm.mode === 'chat-message' ? cm.target : null;
      },

      isSessionMuted: function () {
        var s = this.targetSession;
        return !!(s && this.store.isChatMuted(s.peer_id));
      },

      isPinned: function () {
        var t = this.targetItem;
        return t && t.pinned;
      },

      isConnected: function () {
        var t = this.targetDevice;
        return t && t.connected;
      },

      isLocal: function () {
        var t = this.targetDevice;
        return t && t.device_id === this.store.deviceId;
      },

      isPaired: function () {
        var t = this.targetDevice;
        return t && t.paired;
      },

      isMac: function () {
        return /Mac/i.test(navigator.platform);
      },
    },

    methods: {
      closeMenu: function () {
        var cm = this.store.contextMenu;
        if (cm) {
          cm.visible = false;
        }
      },

      _measureMenu: function () {
        var self = this;
        this.$nextTick(function () {
          if (self.$el && self.$el.offsetHeight) {
            self._menuHeight = self.$el.offsetHeight;
          }
        });
      },

      // ── Keyboard navigation (roving tabindex over the menuitems) ──

      _getMenuItems: function () {
        if (!this.$el) return [];
        var nodes = this.$el.querySelectorAll('.context-menu__item');
        var items = [];
        for (var i = 0; i < nodes.length; i++) items.push(nodes[i]);
        return items;
      },

      _openMenu: function () {
        var self = this;
        this.$nextTick(function () {
          var cm = self.store.contextMenu;
          if (!cm || !cm.visible) return;
          self._focusedIndex = -1;
          self._focusFirstItem();
        });
      },

      _focusFirstItem: function () {
        var items = this._getMenuItems();
        if (items.length === 0) return;
        var idx = 0;
        for (var j = 0; j < items.length; j++) {
          if (items[j].getAttribute('aria-disabled') !== 'true') { idx = j; break; }
        }
        this._focusedIndex = idx;
        items[idx].focus();
      },

      _moveFocus: function (dir) {
        var items = this._getMenuItems();
        if (items.length === 0) return;
        var idx = this._focusedIndex;
        // If nothing is focused yet, wrap from the opposite end.
        if (idx < 0 || idx >= items.length) idx = dir > 0 ? -1 : items.length;
        var next = idx;
        var steps = 0;
        do {
          next = (next + dir + items.length) % items.length;
          steps++;
        } while (items[next].getAttribute('aria-disabled') === 'true' && steps <= items.length);
        this._focusedIndex = next;
        items[next].focus();
      },

      _activateFocused: function () {
        var items = this._getMenuItems();
        if (this._focusedIndex < 0 || this._focusedIndex >= items.length) return;
        var el = items[this._focusedIndex];
        if (el.getAttribute('aria-disabled') === 'true') return;
        if (el.click) el.click();
      },

      _restoreOpenerFocus: function () {
        // A modal on top (client confirm/prompt or a server dialog) owns focus
        // now — let it keep it rather than yanking it back to the opener.
        if (this.store.clientDialog || this.store.activeDialog) return;
        var cm = this.store.contextMenu;
        var opener = cm && cm.opener;
        if (opener && opener.focus && document.contains(opener)) {
          opener.focus();
        }
      },

      // ── History item actions ──────────────────────────────────────

      _copyFull: function (item, successMsg) {
        var self = this;
        var eid = item.entry_id;
        if (eid !== undefined && eid !== null) {
          return ClipsyncAPI.pasteRich(eid).then(function (res) {
            if (res && res.ok !== false) {
              self.store.showToast(successMsg || self.t('history.copied'), 1500);
            } else {
              self.store.showToast(self.t('history.copy_failed'), 2000);
            }
          }).catch(function () {
            self.store.showToast(self.t('history.copy_failed'), 2000);
          });
        }
        // No stable id — copy the (possibly truncated) preview text.
        return this._copyText(item.text_preview || '', successMsg);
      },

      _copyText: function (text, successMsg) {
        var self = this;
        if (!text) {
          self.store.showToast(self.t('history.nothing_to_copy'), 1500);
          return Promise.resolve();
        }
        var show = function () {
          self.store.showToast(successMsg || self.t('history.copied'), 1500);
        };
        var legacy = function () {
          var textarea = document.createElement('textarea');
          textarea.value = text;
          textarea.style.position = 'fixed';
          textarea.style.opacity = '0';
          document.body.appendChild(textarea);
          textarea.select();
          var ok = false;
          try {
            document.execCommand('copy');
            ok = true;
          } catch (e) {
            ok = false;
          }
          document.body.removeChild(textarea);
          return ok;
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          return navigator.clipboard.writeText(text).then(show).catch(function () {
            if (legacy()) show();
            else self.store.showToast(self.t('history.copy_failed'), 2000);
          });
        }
        if (legacy()) show();
        else self.store.showToast(self.t('history.copy_failed'), 2000);
        return Promise.resolve();
      },

      openLink: function () {
        var url = this.linkUrl;
        if (!url) return;
        this.closeMenu();
        var self = this;
        ClipsyncAPI.navigate(url).then(function (res) {
          if (!(res && res.ok)) {
            self.store.showToast(self.t('context.open_link_failed'), 2000);
          }
        }).catch(function () {
          self.store.showToast(self.t('context.open_link_failed'), 2000);
        });
      },

      pasteToDevice: function () {
        var item = this.targetItem;
        if (!item) return;
        this.closeMenu();
        this._copyFull(item, this.t('context.paste_device'));
      },

      copyItem: function () {
        var item = this.targetItem;
        if (!item) return;
        var self = this;
        var preview = item.text_preview || '';
        // Copy the FULL text to the LOCAL (browser) clipboard — distinct from
        // "Paste to this device", which pushes to the desktop clipboard via
        // pasteRich. Fetch the full item so long clips aren't truncated.
        var doCopy = function (text) {
          self.closeMenu();
          self._copyText(text, self.t('history.copied'));
        };
        // IMAGE / IMAGE_EMF entries carry no TEXT payload, so there is nothing
        // meaningful to put on the local browser clipboard — fall back to the
        // push-to-desktop path (pasteRich) so image content still works.
        var isImage = function (ct) {
          var t = (ct || '').toUpperCase();
          return t === 'IMAGE' || t === 'IMAGE_EMF';
        };
        var hasText = function (types) {
          return !!(types && types.TEXT);
        };
        var pushImage = function () {
          self.closeMenu();
          self._copyFull(item, self.t('history.copied'));
        };
        if (hasText(item.types)) {
          doCopy(decodeTypesText(item.types, preview));
        } else if (item.entry_id) {
          ClipsyncAPI.getHistoryItem(item.entry_id).then(function (res) {
            var detail = (res && res.item) || {};
            if (hasText(detail.types)) {
              doCopy(decodeTypesText(detail.types, preview));
            } else if (isImage(detail.content_type) || isImage(item.content_type)) {
              pushImage();
            } else {
              doCopy(preview);
            }
          }).catch(function () {
            if (isImage(item.content_type)) pushImage();
            else doCopy(preview);
          });
        } else {
          doCopy(preview);
        }
      },

      togglePin: function () {
        var item = this.targetItem;
        if (!item) return;
        var store = this.store;
        var eid = item.entry_id;
        if (eid === undefined || eid === null) { this.closeMenu(); return; }

        var self = this;
        ClipsyncAPI.togglePin(eid).then(function (res) {
          if (res && res.ok !== false) {
            // Shared helper: re-finds by id (pinned rows reorder to the top)
            // and bumps the reconcile guard unconditionally.
            store.setPinned(eid, res.pinned);
            store.showToast(res.pinned ? self.t('history.pinned_toast') : self.t('history.unpinned_toast'), 1200);
          } else {
            // The backend explicitly refused (or returned an empty payload) —
            // surface it instead of silently swallowing the failure.
            store.showToast(self.t('history.pin_failed'), 2000);
          }
          self.closeMenu();
        }).catch(function (e) {
          console.error('[ClipSync] Toggle pin failed:', e);
          store.showToast(self.t('history.pin_failed'), 2000);
          self.closeMenu();
        });
      },

      addFavorite: function () {
        var item = this.targetItem;
        if (!item) return;

        var preview = item.text_preview || '';
        var self = this;

        // List responses no longer carry `types`, so fetch the full item when
        // we need the complete text rather than the truncated preview.
        var doAdd = function (fullText) {
          var favData = {
            title: fullText.substring(0, 80),
            content: fullText,
            group: '',
          };
          ClipsyncAPI.addFavorite(favData).then(function (res) {
            // Push the created favorite into the store so the favorites panel
            // reflects it immediately (same as favorites-panel does).
            if (res && res.ok !== false && res.favorite) {
              self.store.favorites.push(res.favorite);
            }
            self.store.showToast(self.t('favorites.added'), 1500);
          }).catch(function (e) {
            console.error('[ClipSync] Add favorite failed:', e);
            self.store.showToast(self.t('favorites.add_failed'), 2000);
          });
          self.closeMenu();
        };

        if (item.types && item.types.TEXT) {
          doAdd(decodeTypesText(item.types, preview));
        } else if (item.entry_id) {
          ClipsyncAPI.getHistoryItem(item.entry_id).then(function (res) {
            var full = preview;
            if (res && res.item && res.item.types) {
              full = decodeTypesText(res.item.types, preview);
            }
            doAdd(full);
          }).catch(function () { doAdd(preview); });
        } else {
          doAdd(preview);
        }
      },

      translateItem: function () {
        var item = this.targetItem;
        if (!item) return;
        var preview = item.text_preview || '';
        var self = this;

        // The list preview is truncated to ~200 chars; translate the FULL text
        // by fetching the detail entry, falling back to the preview on error.
        var open = function (fullText) {
          var text = fullText || preview;
          if (!text) {
            self.store.showToast(self.t('context.nothing_to_translate'), 1500);
            self.closeMenu();
            return;
          }
          self.store.openTranslateModal(text);
          self.closeMenu();
        };

        if (item.types && item.types.TEXT) {
          open(decodeTypesText(item.types, preview));
        } else if (item.entry_id) {
          ClipsyncAPI.getHistoryItem(item.entry_id).then(function (res) {
            var full = preview;
            if (res && res.item && res.item.types) {
              full = decodeTypesText(res.item.types, preview);
            }
            open(full);
          }).catch(function () { open(preview); });
        } else {
          open(preview);
        }
      },

      deleteItem: function () {
        var item = this.targetItem;
        if (!item) return;
        var store = this.store;
        var eid = item.entry_id;
        if (eid === undefined || eid === null) { this.closeMenu(); return; }

        var self = this;
        // Close the menu before showing the confirm so the menu never sits
        // behind the modal (and the confirm's buttons can't re-trigger it).
        this.closeMenu();
        this.store.confirm(this.t('history.delete_title'), this.t('history.delete_confirm'))
          .then(function () {
            ClipsyncAPI.deleteItem(eid).then(function (res) {
              if (res && res.ok !== false) {
                // Route through the store helper so the reconcile guard
                // (historyMutationTick) is bumped like every other delete
                // path — a bare splice here let an in-flight calibration
                // resurrect the deleted row.  The helper also prunes eid from
                // selectedIds and returns the number of rows actually removed.
                var removedCount = store.removeHistoryItems([eid]);
                // Mirror the other delete paths: shrink the "load more" cursor
                // by the rows actually removed (a concurrent broadcast may
                // have already removed this row — then the cursor was already
                // shrunk and removedCount is 0).
                store.historyOffset = Math.max(0, store.historyOffset - removedCount);
                store.showToast(self.t('history.deleted_toast'), 1200);
              }
              self.closeMenu();
            }).catch(function (e) {
              console.error('[ClipSync] Delete failed:', e);
              store.showToast(self.t('history.delete_failed'), 2000);
              self.closeMenu();
            });
          })
          .catch(function () { self.closeMenu(); });
      },

      viewDetails: function () {
        var item = this.targetItem;
        if (!item) return;
        var detail = this.t('context.detail', {
          type: item.content_type || 'unknown',
          source: item.source_name || 'unknown',
          // entry_id 0 is a valid id — only a genuinely missing id shows "N/A".
          id: (item.entry_id === undefined || item.entry_id === null) ? 'N/A' : item.entry_id,
        });
        this.store.showToast(detail, 3000);
        this.closeMenu();
      },

      // ── Device actions ────────────────────────────────────────────

      toggleConnect: function () {
        var device = this.targetDevice;
        if (!device) return;
        var peerId = device.device_id;
        var name = device.device_name || device.device_id;
        var wasConnected = !!device.connected;
        var self = this;
        this.closeMenu();
        var action = wasConnected
          ? ClipsyncAPI.disconnectDevice(peerId)
          : ClipsyncAPI.connectDevice(peerId);
        action.then(function (res) {
          if (res && res.ok) {
            // No optimistic flip: {ok:true} only means the connect/disconnect
            // attempt was *initiated* — the real state converges via the
            // devices_updated broadcast ≤3s later, so a failed handshake never
            // hangs the menu in a false live state.
            self.store.showToast(
              name + (wasConnected ? ' ' + self.t('ui.disconnect') : ' ' + self.t('device.connected')),
              2000
            );
          } else {
            self.store.showToast((res && res.error) || self.t('dialog.failed'), 2000);
          }
        }).catch(function () {
          self.store.showToast(self.t('dialog.failed'), 2000);
        });
      },

      chatWithDevice: function () {
        var device = this.targetDevice;
        if (!device) return;
        var peerId = device.device_id;
        var name = device.device_name || device.note || device.device_id || '';
        var self = this;
        this.closeMenu();
        ClipsyncAPI.chatInvite(peerId, name).then(function (res) {
          if (res && res.session_id) {
            self.store.activeChatSession = res.session_id;
          }
          self.store.activeTab = 'chat';
        }).catch(function () {
          self.store.showToast(self.t('chat.err_connect_timeout'), 2500);
        });
      },

      renameDevice: function () {
        var device = this.targetDevice;
        if (!device) return;
        // The backend only supports a per-device alias ("note"); use it as the
        // user-editable label so the rename actually persists.
        var currentName = device.note || device.device_name || device.device_id || '';
        var self = this;
        this.closeMenu();
        this.store.prompt(this.t('context.rename_title'), this.t('context.rename_prompt'), currentName)
          .then(function (newName) {
            if (!newName || !newName.trim()) return;
            var alias = newName.trim();
            ClipsyncAPI.updateDeviceNote(device.device_id, alias).then(function (res) {
              if (res && res.ok) {
                device.note = alias;
                self.store.showToast(self.t('context.device_renamed', { name: alias }), 1500);
              } else {
                self.store.showToast(self.t('context.rename_failed'), 2000);
              }
            }).catch(function () {
              self.store.showToast(self.t('context.rename_failed'), 2000);
            });
          })
          .catch(function () {});
      },

      forgetDevice: function () {
        var device = this.targetDevice;
        if (!device) return;
        var deviceName = device.device_name || device.note || device.device_id || 'Unknown';
        var self = this;
        this.store.confirm(this.t('devices.forget_title'), this.t('devices.forget_message', { name: deviceName }))
          .then(function () {
            ClipsyncAPI.forgetDevice(device.device_id).then(function () {
              var store = self.store;
              var idx = store.devices.findIndex(function (d) {
                return d.device_id === device.device_id;
              });
              if (idx !== -1) {
                store.devices.splice(idx, 1);
              }
              store.showToast(self.t('context.device_forgotten'), 1500);
              self.closeMenu();
            }).catch(function () {
              self.store.showToast(self.t('context.forget_failed'), 2000);
              self.closeMenu();
            });
          })
          .catch(function () { self.closeMenu(); });
      },

      // Copy the device's id to the local (browser) clipboard.
      copyDeviceId: function () {
        var device = this.targetDevice;
        if (!device || !device.device_id) return;
        this.closeMenu();
        this._copyText(String(device.device_id), this.t('history.copied'));
      },

      // ── Chat session actions ────────────────────────────────────

      toggleSessionMute: function () {
        var s = this.targetSession;
        if (!s || !s.peer_id) return;
        var muted = this.store.toggleChatMute(s.peer_id);
        this.store.showToast(
          muted ? this.t('chat.muted_peer') : this.t('chat.unmuted_peer'),
          1500
        );
        this.closeMenu();
      },

      markSessionRead: function () {
        var s = this.targetSession;
        if (!s) return;
        var idx = this.store.chatSessions.findIndex(function (x) {
          return x.session_id === s.session_id;
        });
        if (idx !== -1) {
          this.store.chatSessions[idx].unread = 0;
          this.store.recalcChatUnread();
        }
        if (window.ClipsyncAPI && window.ClipsyncAPI.chatSessionAction) {
          window.ClipsyncAPI.chatSessionAction(s.session_id, 'read').catch(function () {});
        }
        this.closeMenu();
      },

      // Close (delete) a conversation: ask first, then close + drop it from
      // the local session list so it no longer clutters the sidebar.
      closeSession: function () {
        var s = this.targetSession;
        if (!s) return;
        var sid = s.session_id;
        var self = this;
        this.closeMenu();
        this.store.confirm(
          this.t('chat.close_session'),
          this.t('chat.close_confirm', { name: s.peer_name || '' })
        )
          .then(function () {
            if (window.ClipsyncAPI && window.ClipsyncAPI.chatSessionAction) {
              window.ClipsyncAPI.chatSessionAction(sid, 'close').then(function (res) {
                if (res && res.ok === false) {
                  self.store.showToast(self.t('chat.err_send_failed'), 2500);
                  return;
                }
                self._dropSession(sid);
              }).catch(function (e) {
                console.error('[ClipSync] Failed to close chat session:', e);
              });
            } else {
              self._dropSession(sid);
            }
          })
          .catch(function () {});
      },

      _dropSession: function (sid) {
        if (this.store.activeChatSession === sid) {
          this.store.activeChatSession = '';
          this.store.chatMessages.splice(0, this.store.chatMessages.length);
        }
        var idx = this.store.chatSessions.findIndex(function (x) {
          return x.session_id === sid;
        });
        if (idx !== -1) {
          this.store.chatSessions.splice(idx, 1);
        }
        this.store.recalcChatUnread();
      },

      // Copy a chat message to the local (browser) clipboard.  Text bubbles
      // carry `.text`; file cards carry only a file name, so fall back to that.
      copyChatMsg: function () {
        var m = this.targetChatMsg;
        if (!m) return;
        this.closeMenu();
        var text = (m && m.text) ? m.text : ((m && m.file_name) || '');
        this._copyText(text, this.t('history.copied'));
      },

      // ── Event handlers ────────────────────────────────────────────

      onDocumentClick: function (e) {
        var cm = this.store.contextMenu;
        if (!cm || !cm.visible) return;
        // A touch long-press opens the menu and the browser then synthesizes a
        // click on the source element — ignore it so the menu isn't closed the
        // instant it appears.
        if (cm.touchOpened) return;
        // Close if clicking outside the context menu
        var menuEl = this.$el;
        if (menuEl && !menuEl.contains(e.target)) {
          this.closeMenu();
        }
      },

      onKeyDown: function (e) {
        var cm = this.store.contextMenu;
        if (!cm || !cm.visible) return;

        // A modal (confirm/prompt/alert or server dialog) is on top: the
        // shortcut keys must not fire through it. Delete/Backspace would
        // otherwise re-trigger a pending delete-confirm (looping the dialog)
        // and Ctrl+C would silently clobber the clipboard.
        if (this.store.clientDialog || this.store.activeDialog) return;

        if (e.key === 'Escape') {
          this.closeMenu();
          return;
        }

        // The shortcut keys the menu advertises are real: within a history-item
        // context, Ctrl/Cmd+C copies the item and Delete removes it. Only fire
        // when the menu targets a history item and no editable field is focused.
        if (cm.mode !== 'history-item') return;
        var t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
            !!(t.isContentEditable) || !!(t.closest && t.closest('[contenteditable]')))) {
          return;
        }
        if ((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C')) {
          e.preventDefault();
          this.copyItem();
        } else if (e.key === 'Delete' || e.key === 'Backspace') {
          e.preventDefault();
          this.deleteItem();
        }
      },

      // Keydown handled on the menu container (keyboard navigation between the
      // menuitems). Shortcuts (Ctrl+C / Delete) are intentionally NOT handled
      // here so they bubble up to the document-level onKeyDown.
      onMenuKeydown: function (e) {
        var cm = this.store.contextMenu;
        if (!cm || !cm.visible) return;
        switch (e.key) {
          case 'ArrowDown':
            e.preventDefault();
            this._moveFocus(1);
            break;
          case 'ArrowUp':
            e.preventDefault();
            this._moveFocus(-1);
            break;
          case 'Home':
            e.preventDefault();
            this._focusedIndex = -1;
            this._moveFocus(1);
            break;
          case 'End':
            e.preventDefault();
            this._focusedIndex = this._getMenuItems().length;
            this._moveFocus(-1);
            break;
          case 'Enter':
          case ' ':
            e.preventDefault();
            this._activateFocused();
            break;
          case 'Escape':
            e.preventDefault();
            e.stopPropagation();
            this.closeMenu();
            break;
          default:
            break;
        }
      },

      onResizeOrScroll: function () {
        this.closeMenu();
      },
    },

    watch: {
      // Measure the rendered menu once it appears so menuStyle clamps against
      // the real height (mirrors preview-popover's approach).
      'store.contextMenu.visible': function (val) {
        if (val) {
          this._measureMenu();
          this._openMenu();
        } else {
          this._restoreOpenerFocus();
        }
      },
      'store.contextMenu.mode': function () {
        var cm = this.store.contextMenu;
        if (cm && cm.visible) this._measureMenu();
      },
    },

    mounted: function () {
      var self = this;
      this._onDocClick = this.onDocumentClick.bind(this);
      this._onKeyDown = this.onKeyDown.bind(this);
      this._onResize = this.onResizeOrScroll.bind(this);
      this._onScroll = this.onResizeOrScroll.bind(this);

      document.addEventListener('click', this._onDocClick, true);
      document.addEventListener('keydown', this._onKeyDown);
      window.addEventListener('resize', this._onResize);
      window.addEventListener('scroll', this._onScroll, true);
    },

    beforeUnmount: function () {
      document.removeEventListener('click', this._onDocClick, true);
      document.removeEventListener('keydown', this._onKeyDown);
      window.removeEventListener('resize', this._onResize);
      window.removeEventListener('scroll', this._onScroll, true);
    },

    template:
      '<div' +
        ' v-if="store.contextMenu.visible"' +
        ' class="context-menu glass-neo"' +
        ' role="menu"' +
        ' :style="menuStyle"' +
        ' @click.stop' +
        ' @contextmenu.prevent' +
        ' @keydown="onMenuKeydown"' +
      '>' +
        '<!-- History item mode -->' +
        '<template v-if="store.contextMenu.mode === \'history-item\'">' +
          '<div class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetItem" @click="pasteToDevice">' +
            '<span class="context-menu__item-icon">📤</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.paste_device\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetItem" @click="copyItem">' +
            '<span class="context-menu__item-icon">📋</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.copy\') }}</span>' +
            '<span class="context-menu__shortcut text-subtle">{{ isMac ? \'⌘C\' : \'Ctrl+C\' }}</span>' +
          '</div>' +
          '<div class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetItem || targetItem.entry_id === undefined || targetItem.entry_id === null" @click="togglePin">' +
            '<span class="context-menu__item-icon">📌</span>' +
            '<span class="context-menu__item-label">{{ isPinned ? t(\'context.unpin\') : t(\'context.pin\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetItem" @click="addFavorite">' +
            '<span class="context-menu__item-icon">⭐</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.favorite\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetItem" @click="translateItem">' +
            '<span class="context-menu__item-icon">🌐</span>' +
            '<span class="context-menu__item-label">{{ t(\'ui.translate\') }}</span>' +
          '</div>' +
          '<div v-if="linkUrl" class="context-menu__item" role="menuitem" tabindex="-1" @click="openLink">' +
            '<span class="context-menu__item-icon">🔗</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.open_link\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item context-menu__item--danger" role="menuitem" tabindex="-1" :aria-disabled="!targetItem || targetItem.entry_id === undefined || targetItem.entry_id === null" @click="deleteItem">' +
            '<span class="context-menu__item-icon">🗑</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.delete\') }}</span>' +
            '<span class="context-menu__shortcut text-subtle">{{ isMac ? \'⌘D\' : \'Del\' }}</span>' +
          '</div>' +
          '<div class="context-menu__divider divider"></div>' +
          '<div class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetItem" @click="viewDetails">' +
            '<span class="context-menu__item-icon">ℹ</span>' +
            '<span class="context-menu__item-label">{{ t(\'ui.view_details\') }}</span>' +
          '</div>' +
        '</template>' +

        '<!-- Device mode -->' +
        '<template v-if="store.contextMenu.mode === \'device\'">' +
          '<div v-if="!isLocal" class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetDevice" @click="toggleConnect">' +
            '<span class="context-menu__item-icon">🔗</span>' +
            '<span class="context-menu__item-label">{{ isConnected ? t(\'ui.disconnect\') : t(\'ui.connect\') }}</span>' +
          '</div>' +
          '<div v-if="!isLocal" class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetDevice" @click="chatWithDevice">' +
            '<span class="context-menu__item-icon">💬</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.open_chat\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetDevice" @click="renameDevice">' +
            '<span class="context-menu__item-icon">✏</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.rename\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetDevice" @click="copyDeviceId">' +
            '<span class="context-menu__item-icon">📋</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.copy_device_id\') }}</span>' +
          '</div>' +
          '<div v-if="!isLocal" class="context-menu__item context-menu__item--danger" role="menuitem" tabindex="-1" :aria-disabled="!targetDevice" @click="forgetDevice">' +
            '<span class="context-menu__item-icon">🗑</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.forget_device\') }}</span>' +
          '</div>' +
        '</template>' +

        '<!-- Chat session mode -->' +
        '<template v-if="store.contextMenu.mode === \'chat-session\'">' +
          '<div class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetSession" @click="toggleSessionMute">' +
            '<span class="context-menu__item-icon">{{ isSessionMuted ? \'🔔\' : \'🔕\' }}</span>' +
            '<span class="context-menu__item-label">{{ isSessionMuted ? t(\'chat.unmute\') : t(\'chat.mute\') }}</span>' +
          '</div>' +
          '<div v-if="((targetSession || {}).unread || 0) > 0" class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetSession" @click="markSessionRead">' +
            '<span class="context-menu__item-icon">✓</span>' +
            '<span class="context-menu__item-label">{{ t(\'chat.mark_read\') }}</span>' +
          '</div>' +
          '<div class="context-menu__divider divider"></div>' +
          '<div class="context-menu__item context-menu__item--danger" role="menuitem" tabindex="-1" :aria-disabled="!targetSession" @click="closeSession">' +
            '<span class="context-menu__item-icon">🗑</span>' +
            '<span class="context-menu__item-label">{{ t(\'chat.close_session\') }}</span>' +
          '</div>' +
        '</template>' +

        '<!-- Chat message mode -->' +
        '<template v-if="store.contextMenu.mode === \'chat-message\'">' +
          '<div class="context-menu__item" role="menuitem" tabindex="-1" :aria-disabled="!targetChatMsg" @click="copyChatMsg">' +
            '<span class="context-menu__item-icon">📋</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.copy\') }}</span>' +
          '</div>' +
        '</template>' +
      '</div>',
  };

})();
