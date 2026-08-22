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
          // Above the toast (z-toast=500) so a "favorited" toast can't cover
          // the menu when it opens near the bottom of the screen.
          zIndex: String(600),
          minWidth: '200px',
        };
      },

      targetItem: function () {
        var cm = this.store.contextMenu || {};
        return cm.mode === 'history-item' ? cm.target : null;
      },

      targetDevice: function () {
        var cm = this.store.contextMenu || {};
        return cm.mode === 'device' ? cm.target : null;
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
        var idx = store.history.findIndex(function (h) {
          return h.entry_id === eid;
        });

        var self = this;
        ClipsyncAPI.togglePin(eid).then(function (res) {
          if (res && res.ok !== false) {
            if (idx !== -1) {
              store.history[idx].pinned = !!res.pinned;
            }
            store.showToast(res.pinned ? self.t('history.pinned_toast') : self.t('history.unpinned_toast'), 1200);
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
        var idx = store.history.findIndex(function (h) {
          return h.entry_id === eid;
        });

        var self = this;
        this.store.confirm(this.t('history.delete_title'), this.t('history.delete_confirm'))
          .then(function () {
            ClipsyncAPI.deleteItem(eid).then(function (res) {
              if (res && res.ok !== false) {
                if (idx !== -1) {
                  store.history.splice(idx, 1);
                }
                store.selectedIds.delete(eid);
                store.selectedIds = new Set(store.selectedIds);
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
          id: item.entry_id || 'N/A',
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
        var self = this;
        this.closeMenu();
        var action = device.connected
          ? ClipsyncAPI.disconnectDevice(peerId)
          : ClipsyncAPI.connectDevice(peerId);
        action.then(function (res) {
          if (res && res.ok) {
            device.connected = !device.connected;
            device.encrypted = device.connected;
            self.store.showToast(
              name + (device.connected ? ' ' + self.t('device.connected') : ' ' + self.t('ui.disconnect')),
              2000
            );
          } else {
            self.store.showToast((res && res.error) || self.t('dialog.failed'), 2000);
          }
        }).catch(function () {
          self.store.showToast(self.t('dialog.failed'), 2000);
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
        if (e.key === 'Escape') {
          var cm = this.store.contextMenu;
          if (cm && cm.visible) {
            this.closeMenu();
          }
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
        if (val) this._measureMenu();
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
        ' :style="menuStyle"' +
        ' @click.stop' +
        ' @contextmenu.prevent' +
      '>' +
        '<!-- History item mode -->' +
        '<template v-if="store.contextMenu.mode === \'history-item\'">' +
          '<div class="context-menu__item" @click="pasteToDevice">' +
            '<span class="context-menu__item-icon">📤</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.paste_device\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item" @click="copyItem">' +
            '<span class="context-menu__item-icon">📋</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.copy\') }}</span>' +
            '<span class="context-menu__shortcut text-subtle">Ctrl+C</span>' +
          '</div>' +
          '<div class="context-menu__item" @click="togglePin">' +
            '<span class="context-menu__item-icon">📌</span>' +
            '<span class="context-menu__item-label">{{ isPinned ? t(\'context.unpin\') : t(\'context.pin\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item" @click="addFavorite">' +
            '<span class="context-menu__item-icon">⭐</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.favorite\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item" @click="translateItem">' +
            '<span class="context-menu__item-icon">🌐</span>' +
            '<span class="context-menu__item-label">{{ t(\'ui.translate\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item context-menu__item--danger" @click="deleteItem">' +
            '<span class="context-menu__item-icon">🗑</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.delete\') }}</span>' +
            '<span class="context-menu__shortcut text-subtle">Del</span>' +
          '</div>' +
          '<div class="context-menu__divider divider"></div>' +
          '<div class="context-menu__item" @click="viewDetails">' +
            '<span class="context-menu__item-icon">ℹ</span>' +
            '<span class="context-menu__item-label">{{ t(\'ui.view_details\') }}</span>' +
          '</div>' +
        '</template>' +

        '<!-- Device mode -->' +
        '<template v-if="store.contextMenu.mode === \'device\'">' +
          '<div v-if="!isLocal" class="context-menu__item" @click="toggleConnect">' +
            '<span class="context-menu__item-icon">🔗</span>' +
            '<span class="context-menu__item-label">{{ isConnected ? t(\'ui.disconnect\') : t(\'ui.connect\') }}</span>' +
          '</div>' +
          '<div class="context-menu__item" @click="renameDevice">' +
            '<span class="context-menu__item-icon">✏</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.rename\') }}</span>' +
          '</div>' +
          '<div v-if="!isLocal" class="context-menu__item context-menu__item--danger" @click="forgetDevice">' +
            '<span class="context-menu__item-icon">🗑</span>' +
            '<span class="context-menu__item-label">{{ t(\'context.forget_device\') }}</span>' +
          '</div>' +
        '</template>' +
      '</div>',
  };

})();
