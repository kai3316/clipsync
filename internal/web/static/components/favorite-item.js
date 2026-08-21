/* ═══════════════════════════════════════════════════════════════════
   ClipSync Favorite Item Component
   Renders a single favorite card — reuses history-item visual layout
   but with favorites-specific data mappings (title, content, group)
   and actions (copy, edit title, change group, remove).
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['favorite-item'] = {
    inject: ['store'],

    props: {
      item: {
        type: Object,
        required: true,
      },
      index: {
        type: Number,
        default: 0,
      },
    },

    data: function () {
      return {
        editingTitle: false,
        editTitleValue: '',
        showGroupDropdown: false,
        isDragging: false,
      };
    },

    template: `<div
      class="history-item card favorite-item"
      draggable="true"
      :data-id="item.id"
      :data-index="index"
      :class="{
        'favorite-item--dragging': isDragging,
      }"
      @click="onClick"
      @dragstart="onDragStart"
      @dragend="onDragEnd"
    >
      <div class="history-item__icon">
        <span>{{ typeIcon }}</span>
      </div>
      <div class="history-item__body">
        <div
          v-if="!editingTitle"
          class="history-item__text favorite-item__title"
          @dblclick.stop="startEditTitle"
          :title="t('favorites.dblclick_hint')"
        >{{ item.title || t('favorites.untitled') }}</div>
        <input
          v-else
          class="favorite-item__title-edit"
          v-model="editTitleValue"
          @keydown.enter="saveTitle"
          @keydown.escape="cancelEdit"
          @blur="saveTitle"
          ref="titleInput"
          @click.stop
        />
        <div v-if="item.content" class="favorite-item__preview">{{ contentPreview }}</div>
        <div class="history-item__meta">
          <span class="history-item__time">{{ relativeTime }}</span>
          <span v-if="item.group" class="history-item__source badge favorite-item__group-badge">{{ item.group }}</span>
        </div>
      </div>
      <div class="history-item__actions" @click.stop>
        <button
          class="history-item__action-btn"
          @click="copyItem"
          :title="t('favorites.copy_tooltip')"
        >&#128203;</button>
        <button
          class="history-item__action-btn"
          @click="startEditTitle"
          :title="t('favorites.edit_title_tooltip')"
        >&#9999;&#65039;</button>
        <div class="favorite-item__group-dropdown" :class="{ 'favorite-item__group-dropdown--open': showGroupDropdown }">
          <button
            class="history-item__action-btn"
            @click="toggleGroupDropdown"
            :title="t('favorites.move_group_tooltip')"
          >&#128193;</button>
          <div v-if="showGroupDropdown" class="favorite-item__group-menu glass-neo">
            <button
              v-for="(count, group) in store.groupedFavorites()"
              :key="group"
              class="favorite-item__group-menu-item"
              @click="changeGroup(group)"
            >{{ group === 'Ungrouped' ? t('favorites.ungrouped') : group }}</button>
            <button
              class="favorite-item__group-menu-item favorite-item__group-menu-item--new"
              @click="promptNewGroup"
            >{{ t('favorites.new_group_item') }}</button>
          </div>
        </div>
        <button
          class="history-item__action-btn history-item__action-btn--danger"
          @click="removeFavorite"
          :title="t('favorites.remove_tooltip')"
        >&#128465;</button>
      </div>
    </div>`,

    computed: {
      typeIcon: function () {
        var content = (this.item.content || '').trim();
        var title = (this.item.title || '').trim();
        var combined = title + ' ' + content;
        if (/^https?:\/\//i.test(content) || /^https?:\/\//i.test(title)) return '🌐'; // 🌐
        if (/^(git\s|npm\s|pip\s|yarn\s|docker\s|kubectl\s|ssh\s|export\s|cd\s|ls\s|cat\s|echo\s)/im.test(combined)) return '💻'; // 💻
        return '📋'; // 📋
      },

      contentPreview: function () {
        var content = (this.item.content || '').trim();
        if (!content) return '';
        // Show first 100 chars, replace newlines with spaces
        var preview = content.replace(/\s+/g, ' ').trim();
        if (preview.length > 120) {
          preview = preview.substring(0, 120) + '...';
        }
        return preview;
      },

      relativeTime: function () {
        var ts = this.item.created || 0;
        if (!ts) return '';

        var tsMs = ts > 1e12 ? ts : ts * 1000;
        var now = Date.now();
        var diff = Math.floor((now - tsMs) / 1000);

        if (diff < 0) return this.t('time.just_now');
        if (diff < 10) return this.t('time.just_now');
        if (diff < 60) return this.t('time.seconds_ago', { count: diff });
        if (diff < 3600) return this.t('time.minutes_ago', { count: Math.floor(diff / 60) });
        if (diff < 86400) return this.t('time.hours_ago', { count: Math.floor(diff / 3600) });
        if (diff < 604800) return this.t('time.days_ago', { count: Math.floor(diff / 86400) });

        var d = new Date(tsMs);
        return d.toLocaleDateString();
      },
    },

    methods: {
      onClick: function (e) {
        // Ignore clicks on the title itself — double-click handles editing
        if (e.target.closest('.favorite-item__title') || e.target.closest('.favorite-item__title-edit')) return;
        this.copyItem();
      },

      copyItem: function () {
        var text = (this.item.title || '') + '\n' + (this.item.content || '');
        text = text.trim();
        if (!text) {
          this.store.showToast(this.t('history.nothing_to_copy'), 1500);
          return;
        }

        var self = this;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            self.store.showToast(self.t('history.copied'), 1500);
          }).catch(function () {
            self.fallbackCopy(text);
          });
        } else {
          this.fallbackCopy(text);
        }
      },

      fallbackCopy: function (text) {
        var textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        try {
          document.execCommand('copy');
          this.store.showToast(this.t('history.copied'), 1500);
        } catch (e) {
          this.store.showToast(this.t('history.copy_failed'), 2000);
        }
        document.body.removeChild(textarea);
      },

      startEditTitle: function () {
        this.editTitleValue = this.item.title || '';
        this.editingTitle = true;
        var self = this;
        this.$nextTick(function () {
          var input = self.$refs.titleInput;
          if (input) {
            input.focus();
            input.select();
          }
        });
      },

      saveTitle: function () {
        if (!this.editingTitle) return;
        this.editingTitle = false;
        var newTitle = this.editTitleValue.trim();
        if (newTitle === (this.item.title || '').trim()) return;

        var self = this;
        ClipsyncAPI.updateFavorite(this.item.id, { title: newTitle }).then(function (res) {
          if (res && res.ok !== false && res.favorite) {
            var idx = self.store.favorites.findIndex(function (f) { return f.id === self.item.id; });
            if (idx !== -1) {
              self.store.favorites.splice(idx, 1, res.favorite);
            }
            self.store.showToast(self.t('favorites.title_updated'), 1500);
          } else {
            self.store.showToast(self.t('favorites.title_update_failed'), 2000);
          }
        }).catch(function (e) {
          console.error('[ClipSync] Update favorite title failed:', e);
          self.store.showToast(self.t('favorites.title_update_failed'), 2000);
        });
      },

      cancelEdit: function () {
        this.editingTitle = false;
        this.editTitleValue = '';
      },

      removeFavorite: function () {
        var self = this;
        ClipsyncAPI.deleteFavorite(this.item.id).then(function (res) {
          if (res && res.ok !== false) {
            var idx = self.store.favorites.findIndex(function (f) {
              return f.id === self.item.id;
            });
            if (idx !== -1) {
              self.store.favorites.splice(idx, 1);
            }
            self.store.showToast(self.t('favorites.removed'), 1500);
          }
        }).catch(function (e) {
          console.error('[ClipSync] Remove favorite failed:', e);
          self.store.showToast(self.t('favorites.remove_failed'), 2000);
        });
      },

      toggleGroupDropdown: function () {
        this.showGroupDropdown = !this.showGroupDropdown;
      },

      changeGroup: function (group) {
        this.showGroupDropdown = false;
        var self = this;
        // "Ungrouped" is the UI label for an empty group — store it as an
        // empty string so the item is not assigned a literal "Ungrouped"
        // group that would then be filtered inconsistently.
        if (group === 'Ungrouped') group = '';
        ClipsyncAPI.updateFavorite(this.item.id, { group: group }).then(function (res) {
          if (res && res.ok !== false && res.favorite) {
            var idx = self.store.favorites.findIndex(function (f) { return f.id === self.item.id; });
            if (idx !== -1) {
              self.store.favorites.splice(idx, 1, res.favorite);
            }
            self.store.showToast(self.t('favorites.moved_to', { group: (group === 'Ungrouped' ? self.t('favorites.ungrouped') : group) }), 1500);
          }
        }).catch(function (e) {
          console.error('[ClipSync] Change group failed:', e);
          self.store.showToast(self.t('favorites.move_failed'), 2000);
        });
      },

      promptNewGroup: function () {
        var self = this;
        this.store.prompt(this.t('favorites.new_group_title'), this.t('favorites.new_group_prompt'))
          .then(function (name) {
            if (name && name.trim()) {
              self.changeGroup(name.trim());
            }
            self.showGroupDropdown = false;
          })
          .catch(function () { self.showGroupDropdown = false; });
      },

      onDragStart: function (e) {
        this.isDragging = true;
        // Store the dragged item index in the drag data
        e.dataTransfer.setData('text/plain', JSON.stringify({
          id: this.item.id,
          index: this.index,
        }));
        e.dataTransfer.effectAllowed = 'move';
        // Emit event so the parent panel can track the drag
        this.$el.classList.add('favorite-item--dragging');
      },

      onDragEnd: function () {
        this.isDragging = false;
        this.$el.classList.remove('favorite-item--dragging');
        // Clear all drag-over indicators in the list
        var siblings = this.$el.parentElement.querySelectorAll('.favorite-item');
        for (var i = 0; i < siblings.length; i++) {
          siblings[i].classList.remove('favorite-item--drag-over');
        }
      },

      closeGroupDropdown: function (e) {
        if (this.showGroupDropdown && !this.$el.contains(e.target)) {
          this.showGroupDropdown = false;
        }
      },
    },

    mounted: function () {
      var self = this;
      this._closeGroupDropdown = function (e) {
        self.closeGroupDropdown(e);
      };
      document.addEventListener('click', this._closeGroupDropdown);
    },

    beforeUnmount: function () {
      if (this._closeGroupDropdown) {
        document.removeEventListener('click', this._closeGroupDropdown);
      }
    },
  };

})();
