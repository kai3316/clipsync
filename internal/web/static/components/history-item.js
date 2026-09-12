/* ═══════════════════════════════════════════════════════════════════
   ClipSync History Item Component
   Renders a single clipboard history entry card with type icon,
   text preview (2-line clamp), relative timestamp, source device
   badge, the link the clip arrived on, pin/copy/delete action
   buttons, paste count badge, and multi-select support
   (Ctrl+Click / Shift+Click).
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['history-item'] = {
    inject: ['store'],

    props: {
      item: {
        type: Object,
        required: true,
      },
      // Position in the FLAT visible list (pinned section ++ unpinned
      // section) — the same order store.filteredHistory() returns, so the
      // keyboard cursor (store.kbdIndex) can highlight this row.
      flatIndex: {
        type: Number,
        default: -1,
      },
    },

    template: `<div
      class="history-item card"
      role="button"
      tabindex="0"
      :aria-label="copyButtonTitle"
      :data-kbd="flatIndex"
      :class="{
        'history-item--pinned': item.pinned,
        'history-item--selected': isSelected,
        'history-item--kbd': flatIndex >= 0 && flatIndex === store.kbdIndex
      }"
      @click="onClick"
      @keydown="onKeyDown"
      @mouseenter="onMouseEnter"
      @mouseleave="onMouseLeave"
      @contextmenu.prevent="onContextMenu"
      @touchstart="onTouchStart"
      @touchmove="onTouchMove"
      @touchend="onTouchEnd"
      @touchcancel="onTouchCancel"
    >
      <div class="history-item__icon">
        <span>{{ typeIcon }}</span>
      </div>
      <div class="history-item__body">
        <div class="history-item__text selectable">{{ item.text_preview || t('history.empty_preview') }}</div>
        <div v-if="store.isFilteredText(item.text_preview)" class="history-item__filtered-note">
          {{ t('filter.receiver_note') }}
        </div>
        <div class="history-item__meta">
          <span class="history-item__time">{{ relativeTime }}</span>
          <span class="history-item__type">{{ typeLabel }}</span>
          <span v-if="item.source_app" class="history-item__source badge badge--source-app">{{ item.source_app }}</span>
          <span v-if="item.source_title" class="history-item__source-title">{{ item.source_title }}</span>
          <span v-if="item.source_name" class="history-item__source badge">{{ item.source_name }}</span>
          <span v-if="item.transport" class="history-item__route badge">{{ routeLabel }}</span>
          <span v-if="item.paste_count > 0" class="history-item__paste-count badge badge--success">
            {{ item.paste_count }} {{ item.paste_count === 1 ? t('history.paste_singular') : t('history.paste_plural') }}
          </span>
        </div>
      </div>
      <div class="history-item__actions" @click.stop>
        <button
          class="history-item__action-btn"
          :class="{ 'history-item__action-btn--active': item.pinned }"
          @click="togglePin"
          :title="item.pinned ? t('history.unpin_tooltip') : t('history.pin_tooltip')"
          :aria-label="item.pinned ? t('history.unpin_tooltip') : t('history.pin_tooltip')"
        >&#128204;</button>
        <button
          class="history-item__action-btn"
          @click="copyItem"
          :title="copyButtonTitle"
          :aria-label="copyButtonTitle"
        >&#128203;</button>
        <button
          class="history-item__action-btn history-item__action-btn--danger"
          @click="deleteItem"
          :title="t('history.delete_tooltip')"
          :aria-label="t('history.delete_tooltip')"
        >&#128465;</button>
      </div>
    </div>`,

    computed: {
      typeIcon: function () {
        return ClipsyncAPI.typeIcon(this.item.content_type);
      },

      typeLabel: function () {
        var ct = (this.item.content_type || '').toUpperCase();
        if (ct === 'IMAGE' || ct === 'IMAGE_EMF') return this.t('preview.type_image');
        if (ct === 'FILE') return this.t('preview.type_file');
        if (ct === 'HTML') return this.t('preview.type_html');
        if (ct === 'RTF') return this.t('preview.type_rtf');
        return this.t('preview.type_text');
      },

      relativeTime: function () {
        var ts = this.item.timestamp || 0;
        if (!ts) return '';

        // Normalise to milliseconds (server may send seconds or ms)
        var tsMs = ts > 1e12 ? ts : ts * 1000;
        var now = Date.now();
        var diff = Math.floor((now - tsMs) / 1000);

        if (diff < 0) return this.t('history.just_now');
        if (diff < 10) return this.t('history.just_now');
        if (diff < 60) return this.t('history.seconds_ago', { count: diff });
        if (diff < 3600) return this.t('history.minutes_ago', { count: Math.floor(diff / 60) });
        if (diff < 86400) return this.t('history.hours_ago', { count: Math.floor(diff / 3600) });
        if (diff < 604800) return this.t('history.days_ago', { count: Math.floor(diff / 86400) });

        var d = new Date(tsMs);
        return d.toLocaleDateString();
      },

      isSelected: function () {
        var id = this.item.entry_id;
        if (id !== undefined) {
          return this.store.isSelected(id);
        }
        return false;
      },

      // Which link carried the clip, beside the name of the device it came
      // from.  The name alone cannot say it: a peer reachable both ways sends
      // over whichever is up, and a row pushed from this panel is named "Web"
      // without saying where that browser was.  Empty (a clip captured here, or
      // a row written before the route was recorded) draws no chip at all.
      routeLabel: function () {
        var route = this.item.transport;
        if (route === 'relay') return this.t('history.route_internet');
        if (route === 'web') return this.t('history.route_web');
        return this.t('history.route_local');
      },

      // Coarse pointer (touch / phone) — the copy button actually pushes the
      // item to the DESKTOP clipboard, so its label must be honest on phones.
      isCoarse: function () {
        if (this._coarseChecked === undefined) {
          this._coarseChecked = true;
          this._coarseValue = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
        }
        return this._coarseValue;
      },

      copyButtonTitle: function () {
        return this.isCoarse ? this.t('history.copy_to_desktop_tooltip') : this.t('history.copy_tooltip');
      },
    },

    methods: {
      onKeyDown: function (e) {
        // Only the card itself triggers the primary action — inner action
        // buttons handle their own Enter/Space. Check the target so a focused
        // action button's keypress doesn't also paste/copy.
        if (e.target !== this.$el) return;
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          this.onClick(e);
        } else if (e.key === 'Delete' || e.key === 'Backspace') {
          // The app-level handler deletes the keyboard-CURSOR row, but it is
          // gated off when the focused element is a [role="button"] card —
          // so a card that actually has focus must own Delete itself,
          // mirroring the same single-item delete (confirm applies inside).
          e.preventDefault();
          this.deleteItem();
        }
      },

      onClick: function (e) {
        var store = this.store;

        // Never treat a click that landed on an inner action button (pin/copy/
        // delete) as a card primary-action — those buttons own their clicks.
        if (e.target && e.target.closest && e.target.closest('button, .history-item__actions')) {
          return;
        }

        // A touch long-press fires, then the browser synthesizes a click —
        // swallow it so the freshly-opened context menu isn't immediately
        // acted on (and the item isn't pasted).
        if (this._longPressFired) {
          this._longPressFired = false;
          return;
        }

        if (e.ctrlKey || e.metaKey) {
          var id = this.item.entry_id;
          if (id !== undefined) {
            store.toggleSelect(id);
          }
        } else if (e.shiftKey && store.selectedIds.size > 0) {
          var lastId = Array.from(store.selectedIds).pop();
          store.rangeSelect(lastId, this.item.entry_id);
        } else {
          this.copyItem();
        }
      },

      copyItem: function () {
        var eid = this.item.entry_id;
        if (eid !== undefined && eid !== null) {
          // Shared store helper owns the paste/count/toast contract (the
          // app-level keyboard path uses it too; the right-click context
          // menu keeps its own inline pasteRich on the clicked row).
          this.store.pasteHistoryItem(eid, { coarse: this.isCoarse });
          return;
        }
        // No stable id — fall back to copying the (possibly truncated) preview.
        this.fallbackCopy(this.item.text_preview || '');
      },

      fallbackCopy: function (text) {
        if (!text) {
          this.store.showToast(this.t('history.nothing_to_copy'), 1500);
          return;
        }
        var self = this;
        var done = function () {
          self.store.showToast(self.t('history.copied'), 1500);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done).catch(function () {
            self._legacyCopy(text, done);
          });
        } else {
          this._legacyCopy(text, done);
        }
      },

      _legacyCopy: function (text, done) {
        var textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        try {
          document.execCommand('copy');
          if (done) done();
        } catch (e) {
          this.store.showToast(this.t('history.copy_failed'), 2000);
        }
        document.body.removeChild(textarea);
      },

      togglePin: function () {
        var store = this.store;
        var eid = this.item.entry_id;
        if (eid === undefined || eid === null) return;

        var self = this;
        ClipsyncAPI.togglePin(eid).then(function (res) {
          if (res && res.ok !== false) {
            // The shared helper re-finds by id (the row may have reordered
            // pinned-first while the request was in flight) and bumps the
            // reconcile guard unconditionally — the server committed a change,
            // so an in-flight calibration's pre-change snapshot must not write
            // back, even if the toggled row left the loaded window.
            store.setPinned(eid, res.pinned);
            store.showToast(
              res.pinned ? self.t('history.pinned_toast') : self.t('history.unpinned_toast'),
              1200
            );
          }
        }).catch(function (e) {
          console.error('[ClipSync] Toggle pin failed:', e);
          store.showToast(self.t('history.pin_failed'), 2000);
        });
      },

      deleteItem: function () {
        var store = this.store;
        var eid = this.item.entry_id;
        if (eid === undefined || eid === null) return;

        var self = this;
        // Confirm before deleting — the inline trash and the Delete/Backspace
        // key are permanent, and every other delete path (context menu, batch,
        // clear-all, mobile) confirms first.
        store.confirm(this.t('history.delete_title'), this.t('history.delete_confirm'))
          .then(function () {
            ClipsyncAPI.deleteItem(eid).then(function (res) {
              if (res && res.ok !== false) {
                // Removal via the shared helper — it bumps the mutation tick so an
                // in-flight calibration (store.js) abandons its write-back instead
                // of resurrecting this row, and prunes it from the selection
                // (mirrors the ws.js history_item_deleted handler).
                var removed = store.removeHistoryItems([eid]);
                // Shrink the pagination cursor with the array (matching the batch
                // delete) so "Load more" doesn't skip the item that just shifted
                // into the deleted slot.
                if (removed > 0) {
                  store.historyOffset = Math.max(0, store.historyOffset - 1);
                }
                store.showToast(self.t('history.deleted_toast'), 1200);
              }
            }).catch(function (e) {
              console.error('[ClipSync] Delete failed:', e);
              store.showToast(self.t('history.delete_failed'), 2000);
            });
          })
          .catch(function () {});  // cancelled — not an error
      },

      onMouseEnter: function (e) {
        this.store.previewItem = this.item;
        this.store.previewPosition = { x: e.clientX, y: e.clientY };
      },

      onMouseLeave: function () {
        if (this.store.previewItem === this.item) {
          this.store.previewItem = null;
        }
      },

      // Capture any live text selection that overlaps THIS item, so a
      // right-click on a partial selection can copy it.  Returns '' when
      // nothing is selected or the selection belongs to another row.
      _selectionOverlappingItem: function () {
        var sel = window.getSelection ? window.getSelection() : null;
        if (!sel || sel.isCollapsed || sel.rangeCount === 0) return '';
        var text = sel.toString();
        if (!text) return '';
        var el = this.$el;
        if (!el) return '';
        // Anchor/focus may be text nodes — walk up to their element.
        var a = sel.anchorNode;
        var f = sel.focusNode;
        var aEl = (a && a.nodeType === 1) ? a : (a && a.parentNode);
        var fEl = (f && f.nodeType === 1) ? f : (f && f.parentNode);
        if ((aEl && el.contains(aEl)) || (fEl && el.contains(fEl))) return text;
        return '';
      },

      // Shared menu-open path for right-click and touch long-press.  When a
      // text selection overlaps this item, the selected text is copied right
      // away (so a partial selection can never be uncopyable) and exposed as
      // contextMenu.selectionText so the menu's Copy action can prefer it.
      _openContextMenu: function (x, y, opener) {
        var selText = this._selectionOverlappingItem();
        var menu = {
          visible: true,
          x: x,
          y: y,
          mode: 'history-item',
          target: this.item,
          opener: opener
        };
        if (selText) {
          menu.selectionText = selText;
          this.fallbackCopy(selText);
        }
        this.store.contextMenu = menu;
      },

      onContextMenu: function (e) {
        this._openContextMenu(e.clientX, e.clientY, e.currentTarget || e.target);
      },

      // iOS Safari never fires contextmenu on divs, and long-press is
      // suppressed by -webkit-touch-callout — so a ~500ms hold opens the same
      // context menu at the touch position. Normal taps still paste via onClick.
      onTouchStart: function (e) {
        // Don't start a long-press from the action buttons (pin/copy/delete).
        if (e.target && e.target.closest && e.target.closest('.history-item__actions')) return;
        var self = this;
        this._cancelLongPress();
        var t = e.touches && e.touches[0];
        if (!t) return;
        this._longPressStart = { x: t.clientX, y: t.clientY };
        this._longPressFired = false;
        this._longPressTimer = setTimeout(function () {
          self._longPressTimer = null;
          self._longPressFired = true;
          self.openContextMenuAt(self._longPressStart.x, self._longPressStart.y);
        }, 500);
      },

      onTouchMove: function (e) {
        // Cancel the long-press as soon as the finger moves (scrolling etc.).
        if (!this._longPressTimer) return;
        var t = e.touches && e.touches[0];
        if (!t || !this._longPressStart) return;
        var dx = Math.abs(t.clientX - this._longPressStart.x);
        var dy = Math.abs(t.clientY - this._longPressStart.y);
        if (dx > 10 || dy > 10) {
          this._cancelLongPress();
        }
      },

      onTouchEnd: function () {
        // End of a normal tap — no menu. If a long-press already fired, the
        // synthesized click is swallowed by onClick via _longPressFired.
        this._cancelLongPress();
      },

      onTouchCancel: function () {
        this._cancelLongPress();
      },

      _cancelLongPress: function () {
        if (this._longPressTimer) {
          clearTimeout(this._longPressTimer);
          this._longPressTimer = null;
        }
      },

      openContextMenuAt: function (x, y) {
        this._openContextMenu(x, y, this.$el);
        // The browser synthesizes a click right after a long-press; the
        // context-menu component listens on document in the capture phase and
        // would close the menu instantly. Mark it as touch-opened so that
        // component ignores that one click.
        this.store.contextMenu.touchOpened = true;
        var self = this;
        setTimeout(function () {
          if (self.store.contextMenu) self.store.contextMenu.touchOpened = false;
        }, 600);
      },
    },
  };

})();
