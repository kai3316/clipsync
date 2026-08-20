/* ═══════════════════════════════════════════════════════════════════
   ClipSync History Panel Component
   Main clipboard history view with filter chips, a pinned items
   section, the full history list, empty/loading states, and a
   multi-select action bar for bulk operations.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['history-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        deleting: false,
        clearingAll: false,
        loadingMore: false,
        historyPageSize: 20,
      };
    },

    computed: {
      filters: function () {
        var self = this;
        return [
          { id: 'all', label: self.t('history.type_all'), icon: '' },
          { id: 'text', label: self.t('history.type_text'), icon: '📝' },
          { id: 'image', label: self.t('history.type_image'), icon: '🖼' },
          { id: 'file', label: self.t('history.type_file'), icon: '📄' },
          { id: 'link', label: self.t('history.type_link'), icon: '🔗' },
        ];
      },

      // Split filtered history into pinned and unpinned
      sections: function () {
        var items = this.store.filteredHistory();
        var pinned = [];
        var unpinned = [];
        for (var i = 0; i < items.length; i++) {
          if (items[i].pinned) {
            pinned.push(items[i]);
          } else {
            unpinned.push(items[i]);
          }
        }
        return { pinned: pinned, unpinned: unpinned };
      },

      hasContent: function () {
        return this.sections.pinned.length > 0 || this.sections.unpinned.length > 0;
      },

      selectedCount: function () {
        return this.store.selectedIds.size;
      },

      allPinned: function () {
        var store = this.store;
        var selectedIds = store.selectedIds;
        if (selectedIds.size === 0) return false;
        var allPinned = true;
        selectedIds.forEach(function (id) {
          var item = store.history.find(function (h) { return h.entry_id === id; });
          if (item && !item.pinned) {
            allPinned = false;
          }
        });
        return allPinned;
      },
    },

    template: `<div class="history-panel" @click.self="onPanelClick">
      <!-- Header with Clear All -->
      <div class="history-panel__header" v-if="hasContent">
        <span class="text-muted" style="font-size:12px">{{ sections.pinned.length + sections.unpinned.length }} {{ t('history.items') || 'items' }}</span>
        <button class="btn-ghost" style="color:var(--clipsync-danger)" @click="clearAll" :disabled="clearingAll">
          {{ clearingAll ? '...' : t('history.clear_all') || 'Clear All' }}
        </button>
      </div>

      <!-- Filter bar -->
      <div class="history-panel__filters">
        <button
          v-for="filt in filters"
          :key="filt.id"
          class="history-panel__filter-chip"
          :class="{
            'history-panel__filter-chip--active': store.historyFilter === filt.id,
            'animate-glow-pulse': store.historyFilter === filt.id
          }"
          @click="store.historyFilter = filt.id"
        >
          <span v-if="filt.icon" class="history-panel__filter-chip-icon">{{ filt.icon }}</span>
          {{ filt.label }}
        </button>
      </div>

      <!-- Search results count -->
      <div v-if="store.historySearch" class="history-panel__search-count">
        {{ t('history.search_count', { count: sections.pinned.length + sections.unpinned.length }) }}
      </div>

      <!-- Loading state -->
      <div v-if="store.loading" class="history-panel__loading">
        <div class="skeleton-card animate-shimmer" v-for="n in 2" :key="'s'+n" style="height:64px;margin-bottom:8px;"></div>
      </div>

      <!-- Content -->
      <template v-else-if="hasContent">
        <!-- Pinned section -->
        <template v-if="sections.pinned.length > 0">
          <div class="section-header">&#128204; {{ t('web.pinned') }}</div>
          <history-item
            v-for="(item, index) in sections.pinned"
            :key="item.entry_id || 'p'+index"
            :item="item"
            :index="index"
            class="stagger-item"
          ></history-item>
        </template>

        <!-- All items section -->
        <div v-if="sections.unpinned.length > 0" class="section-header">{{ t('web.history_title') }}</div>
        <history-item
          v-for="(item, index) in sections.unpinned"
          :key="item.entry_id || 'u'+index"
          :item="item"
          :index="index"
          class="stagger-item"
        ></history-item>

        <!-- Load more -->
        <div v-if="store.historyHasMore" class="history-panel__load-more">
          <button class="btn-ghost" @click="loadMore" :disabled="loadingMore">
            {{ loadingMore ? t('ui.loading') || 'Loading...' : t('history.load_more') || 'Load more' }}
          </button>
        </div>
      </template>

      <!-- Empty states (only when no content) -->
      <template v-else>
        <!-- Search empty state -->
        <div v-if="store.historySearch" class="history-panel__empty-search">
          <span class="history-panel__empty-search-icon">🔍</span>
          <p class="history-panel__empty-search-title">{{ t('empty.no_results', { query: store.historySearch }) }}</p>
          <button class="btn-ghost" @click="clearSearch">{{ t('ui.cancel') }}</button>
        </div>

        <!-- General empty state -->
        <div v-else class="panel-empty">
          <span class="panel-empty-icon">&#128203;</span>
          <p class="panel-empty-title">{{ t('web.no_history') }}</p>
          <p class="panel-empty-desc">{{ t('web.no_history_desc') }}</p>
        </div>
      </template>

      <!-- Multi-select action bar -->
      <transition name="slide-up">
        <div v-if="selectedCount > 0" class="history-panel__action-bar">
          <span class="history-panel__action-bar-glow"></span>
          <span class="history-panel__action-bar-count">
            <span class="history-panel__action-bar-count-num">{{ selectedCount }}</span> {{ t('history.selected') }}
          </span>
          <div class="history-panel__action-bar-btns">
            <button class="btn-action-bar" :title="t('web.push_button')" @click="mergeCopySelected">
              <span class="btn-action-bar__icon">&#128203;</span>
              <span class="btn-action-bar__label">{{ t('web.push_button') }}</span>
            </button>
            <button class="btn-action-bar" :title="allPinned ? t('web.unpin') : t('web.pin')" @click="batchPinSelected">
              <span class="btn-action-bar__icon">&#128204;</span>
              <span class="btn-action-bar__label">{{ allPinned ? t('web.unpin') : t('web.pin') }}</span>
            </button>
            <button class="btn-action-bar" :title="t('context.favorite')" @click="batchFavoriteSelected">
              <span class="btn-action-bar__icon">&#11088;</span>
              <span class="btn-action-bar__label">{{ t('context.favorite') }}</span>
            </button>
            <button class="btn-action-bar btn-action-bar--danger" @click="deleteSelected" :disabled="deleting">
              <span class="btn-action-bar__icon">&#128465;</span>
              <span class="btn-action-bar__label">{{ deleting ? t('web.deleted') : t('web.delete') }}</span>
            </button>
            <button class="btn-action-bar btn-action-bar--ghost" @click="clearSelection">
              <span class="btn-action-bar__icon">&#10005;</span>
              <span class="btn-action-bar__label">{{ t('web.cancel') }}</span>
            </button>
          </div>
        </div>
      </transition>
    </div>`,

    methods: {
      onPanelClick: function () {
        // Click on background (not on a history-item) clears selection
        this.store.clearSelection();
      },

      clearSelection: function () {
        this.store.clearSelection();
      },

      loadMore: function () {
        var self = this;
        this.loadingMore = true;
        var offset = this.store.historyOffset;
        ClipsyncAPI.getHistory({ limit: this.historyPageSize, offset: offset })
          .then(function (res) {
            var items = (res && res.items) ? res.items : [];
            // Dedup by entry_id — a loadMore that races a concurrent
            // loadHistory reset must not append duplicate entries.
            var seen = new Set();
            for (var k = 0; k < self.store.history.length; k++) {
              var existingId = self.store.history[k].entry_id;
              if (existingId !== undefined) seen.add(existingId);
            }
            for (var i = 0; i < items.length; i++) {
              var id = items[i].entry_id;
              if (id === undefined || !seen.has(id)) {
                self.store.history.push(items[i]);
                if (id !== undefined) seen.add(id);
              }
            }
            var newOffset = offset + items.length;
            self.store.historyOffset = newOffset;
            self.store.historyHasMore = (res && res.total != null) ? (newOffset < res.total) : false;
            self.loadingMore = false;
          })
          .catch(function () {
            self.loadingMore = false;
          });
      },

      clearAll: function () {
        var self = this;
        this.store.confirm(self.t('history.clear_title'), self.t('history.clear_confirm'))
          .then(function () {
            self.clearingAll = true;
            ClipsyncAPI.clearHistory().then(function (res) {
              if (res && res.ok) {
                self.store.history.splice(0, self.store.history.length);
                self.store.showToast(self.t('history.cleared'), 2000);
              }
              self.clearingAll = false;
            }).catch(function () {
              self.clearingAll = false;
              self.store.showToast(self.t('history.clear_failed'), 2000);
            });
          })
          .catch(function () {});
      },

      clearSearch: function () {
        this.store.historySearch = '';
      },

      mergeCopySelected: function () {
        var store = this.store;
        var selectedIds = Array.from(store.selectedIds);
        if (selectedIds.length === 0) return;

        // Collect text from selected items in display order
        var selectedTexts = [];
        for (var i = 0; i < store.history.length; i++) {
          var h = store.history[i];
          if (selectedIds.indexOf(h.entry_id) !== -1) {
            var text = h.text_preview || '';
            if (text) {
              selectedTexts.push(text);
            }
          }
        }

        if (selectedTexts.length === 0) {
          store.showToast(this.t('history.nothing_to_merge'), 1500);
          return;
        }

        var merged = selectedTexts.join('\n---\n');
        var self = this;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(merged).then(function () {
            store.showToast(self.t('history.merged', { count: selectedTexts.length }), 2000);
          }).catch(function () {
            self._fallbackCopy(merged, selectedTexts.length);
          });
        } else {
          this._fallbackCopy(merged, selectedTexts.length);
        }
      },

      _fallbackCopy: function (text, count) {
        var textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        try {
          document.execCommand('copy');
          this.store.showToast(this.t('history.merged', { count: count }), 2000);
        } catch (e) {
          this.store.showToast(this.t('history.copy_failed'), 2000);
        }
        document.body.removeChild(textarea);
      },

      batchPinSelected: function () {
        var store = this.store;
        var selectedIds = Array.from(store.selectedIds);
        if (selectedIds.length === 0) return;

        var newPinned = !this.allPinned;
        var self = this;

        ClipsyncAPI.batchPin(selectedIds, newPinned).then(function (res) {
          if (res && res.ok !== false) {
            // Update local state
            for (var i = 0; i < store.history.length; i++) {
              if (selectedIds.indexOf(store.history[i].entry_id) !== -1) {
                store.history[i].pinned = newPinned;
              }
            }
            store.showToast(
              self.t(newPinned ? 'history.batch_pinned' : 'history.batch_unpinned', { count: res.count }),
              2000
            );
          }
        }).catch(function (e) {
          console.error('[ClipSync] Batch pin failed:', e);
          store.showToast(self.t('history.pin_failed'), 2000);
        });
      },

      batchFavoriteSelected: function () {
        var store = this.store;
        var selectedIds = Array.from(store.selectedIds);
        if (selectedIds.length === 0) return;

        var self = this;
        ClipsyncAPI.batchFavorite(selectedIds, '').then(function (res) {
          if (res && res.ok !== false) {
            store.showToast(
              self.t('history.batch_favorited', { count: res.count }),
              2000
            );
          }
        }).catch(function (e) {
          console.error('[ClipSync] Batch favorite failed:', e);
          store.showToast(self.t('favorites.add_failed'), 2000);
        });
      },

      deleteSelected: function () {
        var self = this;
        var store = this.store;
        var selectedIds = Array.from(store.selectedIds);

        if (selectedIds.length === 0) return;

        this.store.confirm(
          this.t('history.delete_title'),
          this.t('history.batch_delete_confirm', { count: selectedIds.length })
        ).then(function () {
          self.deleting = true;

          ClipsyncAPI.batchDelete(selectedIds).then(function (res) {
            if (res && res.ok !== false) {
              // Remove deleted entries from local store
              var idSet = new Set(selectedIds);
              var newHistory = [];
              for (var i = 0; i < store.history.length; i++) {
                if (!idSet.has(store.history[i].entry_id)) {
                  newHistory.push(store.history[i]);
                }
              }
              store.history = newHistory;

              store.clearSelection();
              store.showToast(self.t('history.deleted_count', { count: (res.count || selectedIds.length) }), 2000);
            }
            self.deleting = false;
          }).catch(function (e) {
            console.error('[ClipSync] Batch delete failed:', e);
            store.showToast(self.t('history.delete_failed'), 2000);
            self.deleting = false;
          });
        }).catch(function () {});
      },
    },
  };

})();
