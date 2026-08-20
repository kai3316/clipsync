/* ═══════════════════════════════════════════════════════════════════
   ClipSync Favorites Panel Component
   Full-featured favorites management with group sidebar, search,
   add modal (from history / manual), inline title editing, and
   group management (rename / delete via context menu).
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['favorites-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        // Search
        searchInput: '',

        // Add modal
        showAddModal: false,
        addTab: 'history',        // 'history' | 'manual'
        addGroup: '',             // selected group in add modal
        showAddGroupInput: false, // show "create new group" input in modal
        addNewGroupName: '',

        // Manual add form
        manualTitle: '',
        manualContent: '',

        // History items for "From History" tab
        historyForAdd: [],

        // Sidebar group management
        showNewGroupInput: false,
        newGroupInputValue: '',

        // Group context menu
        contextMenu: {
          show: false,
          x: 0,
          y: 0,
          group: null,
        },

        // Inline group rename
        renamingGroup: null,
        renameValue: '',

        // Mobile sidebar toggle
        mobileSidebarOpen: false,

        // Loading states
        addingFavorite: false,
      };
    },

    template: `<div class="favorites-panel">
      <!-- Header -->
      <div class="favorites-panel__header">
        <div class="favorites-panel__header-left">
          <span class="favorites-panel__header-icon">&#11088;</span>
          <span class="favorites-panel__header-title">{{ t('favorites.title') }}</span>
          <span v-if="store.favorites.length" class="favorites-panel__header-count neon-badge">{{ store.favorites.length }}</span>
        </div>
        <button class="favorites-panel__add-btn btn-primary" @click="openAddModal">
          <span>+</span> {{ t('favorites.add') }}
        </button>
      </div>

      <!-- Mobile group dropdown -->
      <div class="favorites-panel__mobile-groups show-mobile">
        <select
          class="favorites-panel__mobile-select"
          :value="store.activeGroup"
          @change="store.activeGroup = $event.target.value"
        >
          <option value="">{{ t('favorites.all_groups') }} ({{ store.favorites.length }})</option>
          <option
            v-for="(count, group) in store.groupedFavorites()"
            :key="group"
            :value="group"
          >{{ group }} ({{ count }})</option>
        </select>
      </div>

      <!-- Main layout: sidebar + content -->
      <div class="favorites-panel__body">
        <!-- Sidebar — groups -->
        <div class="favorites-panel__sidebar glass-neo hide-mobile">
          <div class="favorites-panel__sidebar-title">{{ t('favorites.groups_title') }}</div>
          <div class="favorites-panel__groups">
            <!-- All -->
            <button
              class="favorites-panel__group-btn"
              :class="{ 'favorites-panel__group-btn--active': store.activeGroup === '' }"
              @click="selectGroup('')"
            >
              <span class="favorites-panel__group-btn-dot">&#9679;</span>
              <span class="favorites-panel__group-btn-name">{{ t('favorites.all_groups') }}</span>
              <span class="favorites-panel__group-btn-count">{{ store.favorites.length }}</span>
            </button>
            <!-- Groups -->
            <button
              v-for="(count, group) in sortedGroups"
              :key="group"
              class="favorites-panel__group-btn"
              :class="{ 'favorites-panel__group-btn--active': store.activeGroup === group }"
              @click="selectGroup(group)"
              @contextmenu.prevent="openContextMenu($event, group)"
            >
              <span v-if="renamingGroup === group" class="favorites-panel__group-rename" @click.stop>
                <input
                  class="favorites-panel__group-rename-input"
                  v-model="renameValue"
                  @keydown.enter="saveRenameGroup"
                  @keydown.escape="cancelRenameGroup"
                  @blur="saveRenameGroup"
                  ref="renameInput"
                />
              </span>
              <template v-else>
                <span class="favorites-panel__group-btn-dot">&#9675;</span>
                <span class="favorites-panel__group-btn-name">{{ group }}</span>
                <span class="favorites-panel__group-btn-count">{{ count }}</span>
              </template>
            </button>
          </div>
          <!-- New group -->
          <div v-if="!showNewGroupInput" class="favorites-panel__new-group">
            <button class="favorites-panel__new-group-btn" @click="startNewGroup">{{ t('favorites.new_group') }}</button>
          </div>
          <div v-else class="favorites-panel__new-group">
            <input
              class="favorites-panel__new-group-input"
              v-model="newGroupInputValue"
              :placeholder="t('favorites.group_placeholder')"
              @keydown.enter="createNewGroup"
              @keydown.escape="cancelNewGroup"
              @blur="cancelNewGroup"
              ref="newGroupInput"
            />
          </div>
        </div>

        <!-- Content area -->
        <div class="favorites-panel__content">
          <!-- Search bar -->
          <div v-if="store.favorites.length > 0" class="favorites-panel__search">
            <span class="favorites-panel__search-icon">&#128269;</span>
            <input
              class="favorites-panel__search-input"
              v-model="searchInput"
              :placeholder="t('favorites.search_placeholder')"
              type="text"
            />
            <button
              v-if="searchInput"
              class="favorites-panel__search-clear"
              @click="clearSearch"
            >&#10005;</button>
          </div>

          <!-- Loading state -->
          <div v-if="store.loading" class="favorites-panel__loading">
            <div v-for="n in 3" :key="n" class="skeleton skeleton-card"></div>
          </div>

          <!-- Empty: no favorites at all -->
          <div v-else-if="store.favorites.length === 0" class="empty-state">
            <span class="empty-state__icon animate-float">&#11088;</span>
            <h3 class="empty-state__title">{{ t('favorites.no_favorites') }}</h3>
            <p class="empty-state__desc">{{ t('favorites.empty_desc') }}</p>
            <button class="btn-primary" @click="openAddModal">{{ t('favorites.add_first') }}</button>
          </div>

          <!-- Empty: active group has no items -->
          <div v-else-if="filteredItems.length === 0 && store.activeGroup" class="empty-state">
            <span class="empty-state__icon">&#128193;</span>
            <h3 class="empty-state__title">{{ t('favorites.empty_group_title') }}</h3>
            <p class="empty-state__desc">{{ t('favorites.empty_group_desc', { group: store.activeGroup }) }}</p>
          </div>

          <!-- Empty: search no results -->
          <div v-else-if="filteredItems.length === 0 && store.favoriteSearch" class="empty-state">
            <span class="empty-state__icon">&#128269;</span>
            <h3 class="empty-state__title">{{ t('favorites.no_matches') }}</h3>
            <p class="empty-state__desc">{{ t('favorites.no_matches_desc', { query: store.favoriteSearch }) }}</p>
            <button class="btn-ghost" @click="clearSearch">{{ t('favorites.clear_search') }}</button>
          </div>

          <!-- Items list -->
          <div
            v-else
            class="favorites-panel__list"
            @dragover.prevent="onDragOver"
            @drop.prevent="onDrop"
          >
            <favorite-item
              v-for="(item, index) in filteredItems"
              :key="item.id"
              :item="item"
              :index="index"
              class="stagger-item"
            ></favorite-item>
          </div>
        </div>
      </div>

      <!-- Group context menu -->
      <div
        v-if="contextMenu.show"
        class="favorites-panel__context-menu glass-neo"
        :style="{ left: contextMenu.x + 'px', top: contextMenu.y + 'px' }"
        @click.stop
      >
        <button class="favorites-panel__context-menu-item" @click="startRenameGroup(contextMenu.group)">
          &#9998; {{ t('context.rename') }}
        </button>
        <button class="favorites-panel__context-menu-item favorites-panel__context-menu-item--danger" @click="deleteGroup(contextMenu.group)">
          &#128465; {{ t('ui.delete') }}
        </button>
      </div>

      <!-- Add to Favorites modal -->
      <div v-if="showAddModal" class="favorites-add-modal" @click.self="closeAddModal">
        <div class="favorites-add-modal__card glass-neo animate-holo-reveal">
          <div class="favorites-add-modal__header">
            <span class="favorites-add-modal__title">{{ t('context.favorite') }}</span>
            <button class="favorites-add-modal__close" @click="closeAddModal">&times;</button>
          </div>

          <!-- Tab switcher -->
          <div class="favorites-add-modal__tabs">
            <button
              class="favorites-add-modal__tab-btn"
              :class="{ 'favorites-add-modal__tab-btn--active': addTab === 'history' }"
              @click="addTab = 'history'"
            >{{ t('favorites.from_history') }}</button>
            <button
              class="favorites-add-modal__tab-btn"
              :class="{ 'favorites-add-modal__tab-btn--active': addTab === 'manual' }"
              @click="switchTab('manual')"
            >{{ t('favorites.manual') }}</button>
          </div>

          <!-- Tab: From History -->
          <div v-if="addTab === 'history'" class="favorites-add-modal__body">
            <div v-if="historyForAdd.length === 0" class="favorites-add-modal__empty">
              <span class="empty-state__icon">&#128466;</span>
              <p class="empty-state__desc">{{ t('favorites.no_history_desc') }}</p>
            </div>
            <div v-else class="favorites-add-modal__history-list">
              <div
                v-for="hitem in historyForAdd"
                :key="hitem.entry_id"
                class="favorites-add-modal__history-item"
                @click="addFromHistory(hitem)"
              >
                <span class="favorites-add-modal__history-icon">{{ historyIcon(hitem) }}</span>
                <span class="favorites-add-modal__history-text text-ellipsis">{{ hitem.text_preview || t('history.empty_preview') }}</span>
              </div>
            </div>
          </div>

          <!-- Tab: Manual -->
          <div v-if="addTab === 'manual'" class="favorites-add-modal__body">
            <div class="favorites-add-modal__field">
              <label class="favorites-add-modal__label">{{ t('favorites.field_title') }}</label>
              <input
                class="favorites-add-modal__input"
                v-model="manualTitle"
                :placeholder="t('favorites.title_placeholder')"
                type="text"
              />
            </div>
            <div class="favorites-add-modal__field">
              <label class="favorites-add-modal__label">{{ t('favorites.field_content') }}</label>
              <textarea
                class="favorites-add-modal__input favorites-add-modal__textarea"
                v-model="manualContent"
                :placeholder="t('favorites.content_placeholder')"
                rows="4"
              ></textarea>
            </div>
          </div>

          <!-- Group selector (shared) -->
          <div class="favorites-add-modal__group-section">
            <label class="favorites-add-modal__label">{{ t('favorites.field_group') }}</label>
            <div class="favorites-add-modal__group-row">
              <select
                v-if="!showAddGroupInput"
                class="favorites-add-modal__select"
                v-model="addGroup"
              >
                <option value="">{{ t('favorites.no_group') }}</option>
                <option
                  v-for="(count, group) in store.groupedFavorites()"
                  :key="group"
                  :value="group"
                >{{ group }}</option>
                <option value="__new__">{{ t('favorites.create_group') }}</option>
              </select>
              <input
                v-else
                class="favorites-add-modal__input"
                v-model="addNewGroupName"
                :placeholder="t('favorites.group_placeholder')"
                @keydown.enter="confirmAdd"
                ref="addNewGroupInput"
              />
              <button
                v-if="showAddGroupInput"
                class="btn-ghost"
                @click="showAddGroupInput = false; addNewGroupName = ''"
              >{{ t('ui.cancel') }}</button>
            </div>
          </div>

          <!-- Submit -->
          <div class="favorites-add-modal__footer">
            <button class="btn-ghost" @click="closeAddModal">{{ t('ui.cancel') }}</button>
            <button
              v-if="addTab === 'manual'"
              class="btn-primary"
              :disabled="addingFavorite"
              @click="confirmAdd"
            >{{ addingFavorite ? t('favorites.adding') : t('context.favorite') }}</button>
          </div>
        </div>
      </div>
    </div>`,

    computed: {
      filteredItems: function () {
        return this.store.filteredFavorites();
      },

      sortedGroups: function () {
        var groups = this.store.groupedFavorites();
        var names = Object.keys(groups);
        names.sort(function (a, b) {
          // "Ungrouped" always last
          if (a === 'Ungrouped') return 1;
          if (b === 'Ungrouped') return -1;
          return a.toLowerCase().localeCompare(b.toLowerCase());
        });
        var result = {};
        for (var i = 0; i < names.length; i++) {
          result[names[i]] = groups[names[i]];
        }
        return result;
      },
    },

    watch: {
      searchInput: function (val) {
        var self = this;
        if (this._searchDebounce) clearTimeout(this._searchDebounce);
        this._searchDebounce = setTimeout(function () {
          self.store.favoriteSearch = val;
        }, 200);
      },

      showAddModal: function (val) {
        if (val) {
          this.loadHistoryForAdd();
          this.store.favoriteSearch = ''; // reset search when opening modal
        }
      },

      addGroup: function (val) {
        if (val === '__new__') {
          this.showAddGroupInput = true;
          this.addGroup = '';
          var self = this;
          this.$nextTick(function () {
            var input = self.$refs.addNewGroupInput;
            if (input) input.focus();
          });
        }
      },
    },

    methods: {
      /* ── Drag-to-reorder ─────────────────────────────────────── */

      onDragOver: function (e) {
        // Highlight the item being dragged over
        e.dataTransfer.dropEffect = 'move';
        var target = e.target.closest('.favorite-item');
        if (!target) return;

        // Clear all highlights first
        var items = this.$el.querySelectorAll('.favorite-item');
        for (var i = 0; i < items.length; i++) {
          items[i].classList.remove('favorite-item--drag-over');
        }
        target.classList.add('favorite-item--drag-over');
      },

      onDrop: function (e) {
        // Clear all drag-over indicators
        var allItems = this.$el.querySelectorAll('.favorite-item');
        for (var j = 0; j < allItems.length; j++) {
          allItems[j].classList.remove('favorite-item--drag-over');
        }

        try {
          var dragData = JSON.parse(e.dataTransfer.getData('text/plain'));
        } catch (err) {
          return;
        }

        var fromId = dragData.id;
        var target = e.target.closest('.favorite-item');
        if (!target) return;

        // Read target item id from data-id attribute
        var targetId = target.getAttribute('data-id');
        if (!targetId || targetId === fromId) return;

        // Find indices in the store's favorites array
        var store = this.store;
        var fromIdx = -1;
        var toIdx = -1;
        for (var i = 0; i < store.favorites.length; i++) {
          if (store.favorites[i].id === fromId) fromIdx = i;
          if (store.favorites[i].id === targetId) toIdx = i;
        }
        if (fromIdx === -1 || toIdx === -1) return;

        // Reorder locally. When moving an earlier item down, the removal shifts
        // the target one index left, so adjust before re-inserting.
        var moved = store.favorites.splice(fromIdx, 1)[0];
        if (fromIdx < toIdx) toIdx--;
        store.favorites.splice(toIdx, 0, moved);

        // Persist the new order by updating positions via API
        this._saveOrder();
      },

      _saveOrder: function () {
        // Save the new order by updating each favorite's position
        var store = this.store;
        var self = this;
        var promises = [];
        for (var i = 0; i < store.favorites.length; i++) {
          var fav = store.favorites[i];
          // Only update if position has changed
          if (fav.position !== i) {
            fav.position = i;
            (function (favItem, idx) {
              promises.push(
                ClipsyncAPI.updateFavorite(favItem.id, { position: idx }).catch(function () {
                  // Silently ignore individual failures
                })
              );
            })(fav, i);
          }
        }

        if (promises.length > 0) {
          Promise.all(promises).then(function () {
            self.store.showToast(self.t('favorites.order_saved'), 1500);
          }).catch(function () {
            // Silently ignore
          });
        }
      },

      /* ── Search ──────────────────────────────────────────────── */

      clearSearch: function () {
        this.searchInput = '';
        this.store.favoriteSearch = '';
      },

      /* ── Group selection ─────────────────────────────────────── */

      selectGroup: function (group) {
        this.store.activeGroup = group;
      },

      /* ── Add modal ───────────────────────────────────────────── */

      openAddModal: function () {
        this.addTab = 'history';
        this.addGroup = '';
        this.showAddGroupInput = false;
        this.addNewGroupName = '';
        this.manualTitle = '';
        this.manualContent = '';
        this.showAddModal = true;
      },

      closeAddModal: function () {
        this.showAddModal = false;
      },

      closeContextMenu: function () {
        this.contextMenu.show = false;
        this.contextMenu.group = null;
      },

      switchTab: function (tab) {
        this.addTab = tab;
      },

      loadHistoryForAdd: function () {
        // Use store.history (already loaded) - take recent 20
        var items = this.store.history.slice();
        items.sort(function (a, b) { return (b.timestamp || 0) - (a.timestamp || 0); });
        this.historyForAdd = items.slice(0, 20);
      },

      historyIcon: function (item) {
        var ct = (item.content_type || '').toUpperCase();
        if (ct === 'IMAGE' || ct === 'IMAGE_EMF') return '🖼';
        if (ct === 'FILE') return '📄';
        if (ct === 'HTML') return '🌐';
        if (ct === 'RTF') return '📋';
        return '📝';
      },

      addFromHistory: function (hitem) {
        var self = this;
        var group = this.getEffectiveGroup();

        // Prefer the full stored text over the truncated preview. The history
        // list only ships text_preview; the full TEXT bytes ride along as
        // base64 in hitem.types.
        var fullText = hitem.text_preview || '';
        if (hitem.types && hitem.types.TEXT) {
          try {
            var bytes = Uint8Array.from(atob(hitem.types.TEXT), function (c) { return c.charCodeAt(0); });
            var decoded = new TextDecoder('utf-8').decode(bytes);
            if (decoded) fullText = decoded;
          } catch (e) { /* keep the preview fallback */ }
        }

        var payload = {
          title: fullText.substring(0, 80),
          content: fullText,
          group: group,
        };

        this.addingFavorite = true;
        ClipsyncAPI.addFavorite(payload).then(function (res) {
          if (res && res.ok !== false && res.favorite) {
            self.store.favorites.push(res.favorite);
            self.store.showToast(self.t('favorites.added'), 1500);
            self.closeAddModal();
          }
        }).catch(function (e) {
          console.error('[ClipSync] Add favorite failed:', e);
          self.store.showToast(self.t('favorites.add_failed'), 2000);
        }).finally(function () {
          self.addingFavorite = false;
        });
      },

      confirmAdd: function () {
        if (this.addTab === 'history') {
          // History tab: items are added individually via addFromHistory
          return;
        }

        // Manual tab
        var title = this.manualTitle.trim();
        var content = this.manualContent.trim();
        if (!title && !content) {
          this.store.showToast(this.t('favorites.enter_content'), 2000);
          return;
        }

        var group = this.getEffectiveGroup();

        var self = this;
        this.addingFavorite = true;
        ClipsyncAPI.addFavorite({
          title: title || this.t('favorites.default_title'),
          content: content,
          group: group,
        }).then(function (res) {
          if (res && res.ok !== false && res.favorite) {
            self.store.favorites.push(res.favorite);
            self.store.showToast(self.t('favorites.added'), 1500);
            self.closeAddModal();
          }
        }).catch(function (e) {
          console.error('[ClipSync] Add favorite failed:', e);
          self.store.showToast(self.t('favorites.add_failed'), 2000);
        }).finally(function () {
          self.addingFavorite = false;
        });
      },

      getEffectiveGroup: function () {
        if (this.showAddGroupInput && this.addNewGroupName.trim()) {
          return this.addNewGroupName.trim();
        }
        // "Ungrouped" is a display label for an empty stored group, not a real
        // group name — map it back so we never create a literal "Ungrouped" group.
        var group = this.addGroup || '';
        return group === 'Ungrouped' ? '' : group;
      },

      /* ── Sidebar group management ────────────────────────────── */

      startNewGroup: function () {
        this.showNewGroupInput = true;
        this.newGroupInputValue = '';
        var self = this;
        this.$nextTick(function () {
          var input = self.$refs.newGroupInput;
          if (input) input.focus();
        });
      },

      createNewGroup: function () {
        var name = this.newGroupInputValue.trim();
        if (!name) {
          this.cancelNewGroup();
          return;
        }
        // Select the new group (it exists as a filter; items get assigned via add or move)
        this.store.activeGroup = name;
        this.showNewGroupInput = false;
        this.newGroupInputValue = '';
        this.store.showToast(this.t('favorites.group_created', { name: name }), 2000);
      },

      cancelNewGroup: function () {
        this.showNewGroupInput = false;
        this.newGroupInputValue = '';
      },

      /* ── Group context menu ──────────────────────────────────── */

      openContextMenu: function (e, group) {
        this.contextMenu.show = true;
        this.contextMenu.x = e.clientX;
        this.contextMenu.y = e.clientY;
        this.contextMenu.group = group;
      },

      startRenameGroup: function (group) {
        this.contextMenu.show = false;
        this.renamingGroup = group;
        this.renameValue = group;
        var self = this;
        this.$nextTick(function () {
          var input = self.$refs.renameInput;
          // The input is rendered inside a v-for, so Vue collects refs into an
          // array even though only one group is renaming at a time.
          if (Array.isArray(input)) input = input[0];
          if (input) {
            input.focus();
            input.select();
          }
        });
      },

      saveRenameGroup: function () {
        var oldName = this.renamingGroup;
        var newName = this.renameValue.trim();
        this.renamingGroup = null;

        if (!oldName || !newName || oldName === newName) return;

        // Rename all favorites in this group
        var store = this.store;
        var toUpdate = [];
        for (var i = 0; i < store.favorites.length; i++) {
          if ((store.favorites[i].group || 'Ungrouped') === oldName) {
            toUpdate.push(store.favorites[i]);
          }
        }

        if (toUpdate.length === 0) return;

        var self = this;
        var completed = 0;
        var total = toUpdate.length;

        toUpdate.forEach(function (fav) {
          ClipsyncAPI.updateFavorite(fav.id, { group: newName }).then(function (res) {
            if (res && res.ok !== false && res.favorite) {
              fav.group = res.favorite.group;
            }
            completed++;
            if (completed === total) {
              if (store.activeGroup === oldName) {
                store.activeGroup = newName;
              }
              self.store.showToast(self.t('favorites.group_renamed', { name: newName }), 2000);
            }
          }).catch(function (e) {
            completed++;
            console.error('[ClipSync] Rename group item failed:', e);
          });
        });
      },

      cancelRenameGroup: function () {
        this.renamingGroup = null;
        this.renameValue = '';
      },

      deleteGroup: function (group) {
        this.contextMenu.show = false;
        if (!group) return;

        var groupName = group === 'Ungrouped' ? '' : group;
        var store = this.store;

        var toUpdate = [];
        for (var i = 0; i < store.favorites.length; i++) {
          if ((store.favorites[i].group || 'Ungrouped') === group) {
            toUpdate.push(store.favorites[i]);
          }
        }

        if (toUpdate.length === 0) return;

        var self = this;
        this.store.confirm(
          this.t('favorites.delete_group_title'),
          this.t('favorites.delete_group_confirm', { group: group, count: toUpdate.length })
        ).then(function () {
          var completed = 0;
          var total = toUpdate.length;

          toUpdate.forEach(function (fav) {
          ClipsyncAPI.updateFavorite(fav.id, { group: '' }).then(function (res) {
            if (res && res.ok !== false && res.favorite) {
              fav.group = res.favorite.group;
            }
            completed++;
            if (completed === total) {
              if (store.activeGroup === group) {
                store.activeGroup = '';
              }
              self.store.showToast(self.t('favorites.group_deleted', { name: group }), 2000);
            }
          }).catch(function (e) {
            completed++;
            console.error('[ClipSync] Delete group item failed:', e);
          });
        });
      }).catch(function () { /* cancelled */ });
    },

    mounted: function () {
      // Close context menu on outside click
      var self = this;
      this._onDocClick = function () {
        self.closeContextMenu();
      };
      document.addEventListener('click', this._onDocClick);

      // Close context menu on Escape
      this._onKeyDown = function (e) {
        if (e.key === 'Escape') {
          self.closeContextMenu();
        }
      };
      document.addEventListener('keydown', this._onKeyDown);

      // Sync search input from store
      this.searchInput = this.store.favoriteSearch || '';
    },

    beforeUnmount: function () {
      if (this._onDocClick) {
        document.removeEventListener('click', this._onDocClick);
      }
      if (this._onKeyDown) {
        document.removeEventListener('keydown', this._onKeyDown);
      }
      if (this._searchDebounce) {
        clearTimeout(this._searchDebounce);
      }
    },
  },
};

})();
