/* ═══════════════════════════════════════════════════════════════════
   ClipSync Overview Panel Component
   Dashboard panel with connection status, quick controls, stats grid,
   and recent activity. Shows at-a-glance system health.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['overview-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        editingName: false,
        nameInput: '',
      };
    },

    computed: {
      o: function () { return this.store.overview; },
      uptimeDisplay: function () {
        return this.store.formatUptime(this.o.uptimeSeconds);
      },
      syncLabel: function () {
        return this.o.syncEnabled ? this.t('sync.active') : this.t('sync.paused');
      },
      syncDotClass: function () {
        return this.o.syncEnabled ? 'status-dot--online' : 'status-dot--offline';
      },
      networkLabel: function () {
        var nt = this.o.networkType;
        if (nt === 'wifi') return 'Wi-Fi';
        if (nt === 'ethernet') return this.t('network.ethernet');
        return this.t('network.lan') || 'LAN';
      },
    },

    methods: {
      toggleSync: function () {
        var self = this;
        var desired = !this.o.syncEnabled;
        ClipsyncAPI.updateSettings({ sync_enabled: desired })
          .then(function () {
            self.store.overview.syncEnabled = desired;
          })
          .catch(function () {});
      },

      toggleDiscovery: function () {
        var self = this;
        var desired = !this.o.discovering;
        ClipsyncAPI._fetch('POST', '/api/discovery/toggle', { enabled: desired })
          .then(function () {
            self.store.overview.discovering = desired;
          })
          .catch(function () {});
      },

      toggleVisibility: function () {
        var self = this;
        var desired = !this.o.visible;
        ClipsyncAPI._fetch('POST', '/api/visibility/toggle', { enabled: desired })
          .then(function () {
            self.store.overview.visible = desired;
          })
          .catch(function () {});
      },

      toggleWebCompanion: function () {
        var self = this;
        var desired = !this.o.webEnabled;
        ClipsyncAPI.updateSettings({ web_enabled: desired })
          .then(function () {
            self.store.overview.webEnabled = desired;
          })
          .catch(function () {});
      },

      startEditName: function () {
        this.nameInput = this.store.deviceName;
        this.editingName = true;
        var self = this;
        this.$nextTick(function () {
          var el = self.$refs.nameInput;
          if (el) { el.focus(); el.select(); }
        });
      },

      saveName: function () {
        var name = this.nameInput.trim();
        if (name && name !== this.store.deviceName) {
          ClipsyncAPI.updateSettings({ device_name: name })
            .then(function () {
              window.__CLIPSYNC_STORE__.deviceName = name;
            })
            .catch(function () {});
        }
        this.editingName = false;
      },

      cancelEditName: function () {
        this.editingName = false;
      },

      copyUrl: function () {
        var proto = window.location.protocol === 'https:' ? 'https' : 'http';
        var url = proto + '://' + this.o.localIp + ':' + this.o.port +
          '?token=' + this.store.token;
        var self = this;
        var done = function () {
          self.store.showToast(self.t('toast.url_copied'), 1500);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(url).then(done).catch(function () {
            self._fallbackCopy(url, done);
          });
        } else {
          this._fallbackCopy(url, done);
        }
      },

      _fallbackCopy: function (text, done) {
        var textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        try { document.execCommand('copy'); } catch (e) { /* ignore */ }
        document.body.removeChild(textarea);
        if (done) done();
      },
    },

    template:
      '<div class="overview-panel">' +
        '<!-- Status Bar: connection dot, sync, network, devices, uptime -->' +
        '<div class="overview-status-bar glass">' +
          '<div class="overview-status-bar__left">' +
            '<span class="status-dot" :class="syncDotClass"></span>' +
            '<span class="overview-status-bar__sync">{{ syncLabel }}</span>' +
            '<span class="overview-status-bar__sep"></span>' +
            '<span class="overview-status-bar__label">{{ networkLabel }}</span>' +
            '<span class="overview-status-bar__sep"></span>' +
            '<span class="overview-status-bar__label">{{ o.connectedCount }} {{ t(\'overview.connected\') }}</span>' +
          '</div>' +
          '<div class="overview-status-bar__right">' +
            '<span class="text-subtle">{{ o.localIp }}:{{ o.port }}</span>' +
            '<span class="overview-status-bar__sep"></span>' +
            '<span class="text-subtle">{{ t(\'overview.uptime\') }} {{ uptimeDisplay }}</span>' +
          '</div>' +
        '</div>' +

        '<!-- Quick Controls + This Device -->' +
        '<div class="overview-top-row">' +

          '<!-- Quick Controls -->' +
          '<div class="overview-card glass">' +
            '<h3 class="overview-card__title">{{ t(\'overview.quick_controls\') }}</h3>' +
            '<div class="overview-toggles">' +
              '<div class="overview-toggle-row">' +
                '<span>{{ t(\'overview.sync\') }}</span>' +
                '<button class="settings-toggle" :class="{ \'settings-toggle--on\': o.syncEnabled }" @click="toggleSync">' +
                  '<span class="settings-toggle__knob"></span>' +
                '</button>' +
              '</div>' +
              '<div class="overview-toggle-row">' +
                '<span>{{ t(\'overview.discovery\') }}</span>' +
                '<button class="settings-toggle" :class="{ \'settings-toggle--on\': o.discovering }" @click="toggleDiscovery">' +
                  '<span class="settings-toggle__knob"></span>' +
                '</button>' +
              '</div>' +
              '<div class="overview-toggle-row">' +
                '<span>{{ t(\'overview.visibility\') }}</span>' +
                '<button class="settings-toggle" :class="{ \'settings-toggle--on\': o.visible }" @click="toggleVisibility">' +
                  '<span class="settings-toggle__knob"></span>' +
                '</button>' +
              '</div>' +
              '<div class="overview-toggle-row">' +
                '<span>{{ t(\'overview.web_companion\') }}</span>' +
                '<button class="settings-toggle" :class="{ \'settings-toggle--on\': o.webEnabled }" @click="toggleWebCompanion">' +
                  '<span class="settings-toggle__knob"></span>' +
                '</button>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<!-- This Device Card -->' +
          '<div class="overview-card glass overview-device-card">' +
            '<h3 class="overview-card__title">{{ t(\'overview.this_device\') }}</h3>' +
            '<div class="overview-device-info">' +
              '<span v-if="!editingName" class="overview-device-name">' +
                'ClipSync ▸ {{ store.deviceName }}' +
                '<button class="btn-ghost overview-edit-btn" @click="startEditName" :title="t(\'overview.edit_name\')">✎</button>' +
              '</span>' +
              '<span v-else class="overview-device-name">' +
                '<input ref="nameInput" v-model="nameInput" class="overview-name-input" @keydown.enter="saveName" @keydown.escape="cancelEditName" @blur="saveName">' +
              '</span>' +
              '<div class="overview-device-meta">' +
                '<span class="text-subtle">{{ t(\'overview.id\') }}:</span>' +
                '<span class="text-mono selectable">{{ store.deviceId ? store.deviceId.substring(0,12) + \'...\' : \'\' }}</span>' +
              '</div>' +
              '<div class="overview-device-meta">' +
                '<span class="text-subtle">{{ t(\'overview.platform\') }}:</span>' +
                '<span>{{ o.platform }}</span>' +
              '</div>' +
            '</div>' +
            '<div v-if="o.webEnabled && o.localIp" class="overview-web-qr">' +
              '<code class="text-mono text-subtle">{{ o.localIp }}:{{ o.port }}</code>' +
              '<button class="btn-ghost overview-copy-btn" @click="copyUrl">{{ t(\'overview.copy_url\') }}</button>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<!-- Stats Grid -->' +
        '<div class="overview-stats-grid">' +
          '<div class="overview-stat-card glass" style="--stat-accent: var(--clipsync-success)">' +
            '<span class="overview-stat-value">{{ o.connectedCount }}</span>' +
            '<span class="overview-stat-label">{{ t(\'overview.connected\') }}</span>' +
          '</div>' +
          '<div class="overview-stat-card glass" style="--stat-accent: var(--clipsync-warning)">' +
            '<span class="overview-stat-value">{{ o.pairedCount }}</span>' +
            '<span class="overview-stat-label">{{ t(\'overview.paired\') }}</span>' +
          '</div>' +
          '<div class="overview-stat-card glass" style="--stat-accent: var(--clipsync-accent)">' +
            '<span class="overview-stat-value">{{ o.historyCount }}</span>' +
            '<span class="overview-stat-label">{{ t(\'overview.history\') }}</span>' +
          '</div>' +
          '<div class="overview-stat-card glass" style="--stat-accent: var(--clipsync-purple)">' +
            '<span class="overview-stat-value">{{ o.activeTransferCount || \'--\' }}</span>' +
            '<span class="overview-stat-label">{{ t(\'overview.transfers\') }}</span>' +
          '</div>' +
        '</div>' +

        '<!-- Recent Activity -->' +
        '<div v-if="o.recentActivity" class="overview-activity glass">' +
          '<span class="text-subtle">{{ t(\'overview.recent_activity\') }}:</span>' +
          '<span>{{ o.recentActivity }}</span>' +
        '</div>' +
      '</div>',
  };

})();
