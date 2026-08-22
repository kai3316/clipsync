/* ═══════════════════════════════════════════════════════════════════
   ClipSync Overview Panel Component
   Dashboard panel with connection status, quick controls, a live stats
   grid with animated counters, a device ring, connected-device chips,
   a recent-clipboard activity feed, and quick actions.
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
        // Animated stat counters (key -> current displayed number).
        stats: {},
        // Network health summary ('ok' | 'warn' | 'fail' | '').
        netHealth: '',
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

      connectedChips: function () {
        return (this.o.connectedNames || []).slice(0, 8);
      },

      recentList: function () {
        return this.o.recentItems || [];
      },

      netHealthClass: function () {
        if (this.netHealth === 'ok') return 'net-health-dot--ok';
        if (this.netHealth === 'fail') return 'net-health-dot--fail';
        if (this.netHealth === 'warn') return 'net-health-dot--warn';
        return '';
      },

      netHealthLabel: function () {
        if (this.netHealth === 'ok') return this.t('overview.net_health_ok');
        if (this.netHealth === 'fail') return this.t('overview.net_health_fail');
        if (this.netHealth === 'warn') return this.t('overview.net_health_warn');
        return this.t('overview.net_health_check');
      },

      // ── Device ring: connected / paired / discovered ──────────
      ring: function () {
        var c = this.o.connectedCount || 0;
        var p = Math.max(0, (this.o.pairedCount || 0) - c);
        var d = this.o.discoveredCount || 0;
        return { c: c, p: p, d: d, total: (c + p + d) || 1 };
      },

      ringStyle: function () {
        var r = this.ring;
        var c = r.c / r.total * 100;
        var p = r.p / r.total * 100;
        var d = r.d / r.total * 100;
        return {
          background: 'conic-gradient(' +
            'var(--clipsync-success) 0% ' + c + '%,' +
            'var(--clipsync-accent-2) ' + c + '% ' + (c + p) + '%,' +
            'var(--clipsync-fg-subtle) ' + (c + p) + '% ' + (c + p + d) + '%,' +
            'transparent ' + (c + p + d) + '% 100%)',
        };
      },
    },

    watch: {
      // Animate stat counters whenever the backend pushes a new overview.
      'store.overview': {
        deep: true,
        handler: function (o) {
          if (!o) return;
          var self = this;
          var targets = {
            connected: o.connectedCount || 0,
            paired: o.pairedCount || 0,
            history: o.historyCount || 0,
            today: o.historyToday || 0,
            images: o.historyImages || 0,
            transfers: o.activeTransfers || 0,
            completed: o.transferCompleted || 0,
            pinned: o.historyPinned || 0,
          };
          Object.keys(targets).forEach(function (key) {
            self._animate(key, targets[key]);
          });
        },
      },
    },

    mounted: function () {
      var self = this;
      this.loadNetworkHealth();
      // Refresh network health alongside the overview poll.
      this._netHealthTimer = setInterval(function () {
        self.loadNetworkHealth();
      }, 8000);
    },

    beforeUnmount: function () {
      if (this._netHealthTimer) clearInterval(this._netHealthTimer);
    },

    methods: {
      _animate: function (key, target) {
        var self = this;
        var from = this.stats[key] || 0;
        var to = Number(target) || 0;
        if (from === to) return;
        if (this._animTimers && this._animTimers[key]) {
          cancelAnimationFrame(this._animTimers[key]);
        }
        this._animTimers = this._animTimers || {};
        var start = performance.now();
        var dur = 650;
        function step(now) {
          var t = Math.min(1, (now - start) / dur);
          var eased = 1 - Math.pow(1 - t, 3);
          self.stats[key] = Math.round(from + (to - from) * eased);
          if (t < 1) self._animTimers[key] = requestAnimationFrame(step);
          else self._animTimers[key] = null;
        }
        this._animTimers[key] = requestAnimationFrame(step);
      },

      timeAgo: function (ts) {
        if (!ts) return '';
        var sec = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
        if (sec < 60) return sec + 's';
        if (sec < 3600) return Math.floor(sec / 60) + 'm';
        if (sec < 86400) return Math.floor(sec / 3600) + 'h';
        return Math.floor(sec / 86400) + 'd';
      },

      loadNetworkHealth: function () {
        var self = this;
        if (!window.ClipsyncAPI) return;
        ClipsyncAPI._fetch('GET', '/api/diagnostics')
          .then(function (res) {
            self.netHealth = (res && res.summary) || '';
          })
          .catch(function () { self.netHealth = ''; });
      },

      openDiagnostics: function () {
        // Diagnostics now live on their own page — jump straight there.
        if (this.store) {
          this.store.activeTab = 'diagnostics';
        }
      },

      typeIcon: function (type) {
        return ClipsyncAPI.typeIcon(type);
      },

      toggleSync: function () {
        var self = this;
        var desired = !this.o.syncEnabled;
        ClipsyncAPI.updateSettings({ sync_enabled: desired })
          .then(function () { self.store.overview.syncEnabled = desired; })
          .catch(function () { self.store.showToast(self.t('dialog.failed'), 2000); });
      },

      toggleDiscovery: function () {
        var self = this;
        var desired = !this.o.discovering;
        ClipsyncAPI._fetch('POST', '/api/discovery/toggle', { enabled: desired })
          .then(function () { self.store.overview.discovering = desired; })
          .catch(function () { self.store.showToast(self.t('dialog.failed'), 2000); });
      },

      toggleVisibility: function () {
        var self = this;
        var desired = !this.o.visible;
        ClipsyncAPI._fetch('POST', '/api/visibility/toggle', { enabled: desired })
          .then(function () { self.store.overview.visible = desired; })
          .catch(function () { self.store.showToast(self.t('dialog.failed'), 2000); });
      },

      toggleWebCompanion: function () {
        var self = this;
        var desired = !this.o.webEnabled;
        ClipsyncAPI.updateSettings({ web_enabled: desired })
          .then(function () { self.store.overview.webEnabled = desired; })
          .catch(function () { self.store.showToast(self.t('dialog.failed'), 2000); });
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
        var self = this;
        if (name && name !== this.store.deviceName) {
          ClipsyncAPI.updateSettings({ device_name: name })
            .then(function () {
              window.__CLIPSYNC_STORE__.deviceName = name;
              self.editingName = false;
            })
            .catch(function () {
              // Keep the editor open and restore the old name so the failure
              // isn't silently swallowed.
              self.nameInput = self.store.deviceName;
              self.store.showToast(self.t('dialog.failed'), 2000);
            });
        } else {
          this.editingName = false;
        }
      },

      cancelEditName: function () { this.editingName = false; },

      copyUrl: function () {
        var proto = window.location.protocol === 'https:' ? 'https' : 'http';
        // Deliver the lightweight phone page (mobile.html), matching the QR
        // code, so a phone opening the copied URL gets the mobile UI instead
        // of the heavy desktop dashboard.
        var url = proto + '://' + this.o.localIp + ':' + this.o.port +
          '/mobile.html?token=' + encodeURIComponent(this.store.token);
        var self = this;
        var done = function () { self.store.showToast(self.t('toast.url_copied'), 1500); };
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

      showQr: function () {
        ClipsyncAPI._fetch('POST', '/api/show_qr', {}).catch(function () {});
      },

      sendUrl: function () {
        ClipsyncAPI._fetch('POST', '/api/send_url', {}).catch(function () {});
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
            '<span class="overview-status-bar__label">{{ networkLabel }}<span v-if="o.networkDetail"> · {{ o.networkDetail }}</span></span>' +
            '<span class="overview-status-bar__sep"></span>' +
            '<span class="overview-status-bar__label">{{ stats.connected || 0 }} {{ t(\'overview.connected\') }}</span>' +
          '</div>' +
          '<div class="overview-status-bar__right">' +
            '<button class="net-health-chip" @click="openDiagnostics" :title="t(\'overview.net_health_hint\')">' +
              '<span class="net-health-dot" :class="netHealthClass"></span>' +
              '<span>{{ netHealthLabel }}</span>' +
            '</button>' +
            '<span class="text-subtle">{{ o.localIp }}:{{ o.port }}</span>' +
            '<span class="overview-status-bar__sep"></span>' +
            '<span class="text-subtle">{{ t(\'overview.uptime\') }} {{ uptimeDisplay }}</span>' +
          '</div>' +
        '</div>' +

        '<div class="overview-top-row">' +

          '<!-- This Device Hero -->' +
          '<div class="overview-card glass overview-hero">' +
            '<h3 class="overview-card__title">{{ t(\'overview.this_device\') }}</h3>' +
            '<div class="overview-device-info">' +
              '<span v-if="!editingName" class="overview-device-name">' +
                '<span class="overview-hero-icon">💻</span> {{ store.deviceName }}' +
                '<button class="btn-ghost overview-edit-btn" @click="startEditName" :title="t(\'overview.edit_name\')">&#9999;&#65039;</button>' +
              '</span>' +
              '<span v-else class="overview-device-name">' +
                '<input ref="nameInput" v-model="nameInput" class="overview-name-input" @keydown.enter="saveName" @keydown.escape="cancelEditName" @blur="saveName">' +
              '</span>' +
              '<div class="overview-device-meta">' +
                '<span class="text-subtle">{{ t(\'overview.id\') }}:</span>' +
                '<span class="text-mono selectable" :title="store.deviceId">{{ store.deviceId }}</span>' +
              '</div>' +
              '<div class="overview-device-meta">' +
                '<span class="text-subtle">{{ t(\'overview.platform\') }}:</span>' +
                '<span>{{ o.platform }} · v{{ o.version }}</span>' +
              '</div>' +
            '</div>' +
            '<div class="overview-hero-strip">' +
              '<span class="overview-hero-chip"><span class="status-dot" :class="stats.connected > 0 ? \'status-dot--online\' : \'status-dot--offline\'"></span> {{ stats.connected || 0 }} {{ t(\'overview.connected\') }}</span>' +
              '<span class="overview-hero-chip">{{ t(\'overview.uptime\') }} {{ uptimeDisplay }}</span>' +
            '</div>' +
            '<div v-if="o.webEnabled && o.localIp" class="overview-web-qr">' +
              '<code class="text-mono text-subtle">{{ o.localIp }}:{{ o.port }}</code>' +
              '<button class="btn-ghost overview-copy-btn" @click="copyUrl">{{ t(\'overview.copy_url\') }}</button>' +
            '</div>' +
          '</div>' +

          '<!-- Quick Controls -->' +
          '<div class="overview-card glass">' +
            '<h3 class="overview-card__title">{{ t(\'overview.quick_controls\') }}</h3>' +
            '<div class="overview-toggles">' +
              '<div class="overview-toggle-row">' +
                '<span>{{ t(\'overview.sync\') }}</span>' +
                '<button class="settings-toggle" role="switch" :aria-checked="o.syncEnabled" :aria-label="t(\'overview.sync\')" :class="{ \'settings-toggle--on\': o.syncEnabled }" @click="toggleSync"><span class="settings-toggle__knob"></span></button>' +
              '</div>' +
              '<div class="overview-toggle-row">' +
                '<span>{{ t(\'overview.discovery\') }}</span>' +
                '<button class="settings-toggle" role="switch" :aria-checked="o.discovering" :aria-label="t(\'overview.discovery\')" :class="{ \'settings-toggle--on\': o.discovering }" @click="toggleDiscovery"><span class="settings-toggle__knob"></span></button>' +
              '</div>' +
              '<div class="overview-toggle-row">' +
                '<span>{{ t(\'overview.visibility\') }}</span>' +
                '<button class="settings-toggle" role="switch" :aria-checked="o.visible" :aria-label="t(\'overview.visibility\')" :class="{ \'settings-toggle--on\': o.visible }" @click="toggleVisibility"><span class="settings-toggle__knob"></span></button>' +
              '</div>' +
              '<div class="overview-toggle-row">' +
                '<span>{{ t(\'overview.web_companion\') }}</span>' +
                '<button class="settings-toggle" role="switch" :aria-checked="o.webEnabled" :aria-label="t(\'overview.web_companion\')" :class="{ \'settings-toggle--on\': o.webEnabled }" @click="toggleWebCompanion"><span class="settings-toggle__knob"></span></button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<!-- Live stats grid (animated counters) -->' +
        '<div class="overview-stats-grid">' +
          '<div class="overview-stat-card glass" style="--stat-accent: var(--clipsync-success)">' +
            '<span class="overview-stat-value">{{ stats.connected || 0 }}</span>' +
            '<span class="overview-stat-label">{{ t(\'overview.connected\') }}</span>' +
            '<span class="overview-stat-sub">{{ stats.paired || 0 }} {{ t(\'overview.trusted\') }}</span>' +
          '</div>' +
          '<div class="overview-stat-card glass" style="--stat-accent: var(--clipsync-accent-2)">' +
            '<span class="overview-stat-value">{{ stats.history || 0 }}</span>' +
            '<span class="overview-stat-label">{{ t(\'overview.history\') }}</span>' +
            '<span class="overview-stat-sub">+{{ stats.today || 0 }} {{ t(\'overview.today\') }}</span>' +
          '</div>' +
          '<div class="overview-stat-card glass" style="--stat-accent: var(--clipsync-accent-3)">' +
            '<span class="overview-stat-value">{{ stats.images || 0 }}</span>' +
            '<span class="overview-stat-label">{{ t(\'overview.images\') }}</span>' +
            '<span class="overview-stat-sub">{{ stats.pinned || 0 }} {{ t(\'overview.pinned\') }}</span>' +
          '</div>' +
          '<div class="overview-stat-card glass" style="--stat-accent: var(--clipsync-accent)">' +
            '<span class="overview-stat-value">{{ stats.transfers || 0 }}</span>' +
            '<span class="overview-stat-label">{{ t(\'overview.transfers\') }}</span>' +
            '<span class="overview-stat-sub">{{ stats.completed || 0 }} {{ t(\'overview.completed\') }}</span>' +
          '</div>' +
        '</div>' +

        '<!-- Device ring + connected chips + quick actions -->' +
        '<div class="overview-mid-row">' +
          '<div class="overview-card glass overview-ring-card">' +
            '<h3 class="overview-card__title">{{ t(\'overview.network_map\') }}</h3>' +
            '<div class="overview-ring-wrap">' +
              '<div class="overview-ring" :style="ringStyle">' +
                '<div class="overview-ring__center">' +
                  '<span class="overview-ring__big">{{ ring.c }}</span>' +
                  '<span class="overview-ring__label">{{ t(\'overview.connected\') }}</span>' +
                '</div>' +
              '</div>' +
              '<div class="overview-ring-legend">' +
                '<div class="overview-ring-legend__row"><span class="legend-dot" style="background:var(--clipsync-success)"></span>{{ ring.c }} {{ t(\'overview.connected\') }}</div>' +
                '<div class="overview-ring-legend__row"><span class="legend-dot" style="background:var(--clipsync-accent-2)"></span>{{ ring.p }} {{ t(\'device.paired_offline\') }}</div>' +
                '<div class="overview-ring-legend__row"><span class="legend-dot" style="background:var(--clipsync-fg-subtle)"></span>{{ ring.d }} {{ t(\'device.discovered\') }}</div>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<div class="overview-card glass overview-devices-card">' +
            '<div class="overview-card__head">' +
              '<h3 class="overview-card__title">{{ t(\'overview.connected_devices\') }}</h3>' +
              '<span class="overview-badge" v-if="connectedChips.length > 0">{{ connectedChips.length }}</span>' +
            '</div>' +
            '<div v-if="connectedChips.length > 0" class="overview-chips">' +
              '<span v-for="name in connectedChips" :key="name" class="overview-chip">' +
                '<span class="status-dot status-dot--online"></span><span class="text-ellipsis">{{ name }}</span>' +
              '</span>' +
            '</div>' +
            '<div v-else class="overview-empty-state">' +
              '<span class="overview-empty-state__icon">📡</span>' +
              '<span class="overview-empty-state__text">{{ t(\'overview.no_connected_hint\') }}</span>' +
            '</div>' +
            '<div class="overview-action-row">' +
              '<button class="overview-quick-btn" @click="showQr">📱 {{ t(\'tray.show_web_qr\') }}</button>' +
              '<button class="overview-quick-btn" @click="sendUrl">🔗 {{ t(\'tray.send_url\') }}</button>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<!-- Recent clipboard activity feed -->' +
        '<div class="overview-card glass overview-activity-card">' +
          '<h3 class="overview-card__title">{{ t(\'overview.recent_activity\') }}</h3>' +
          '<div v-if="recentList.length > 0" class="overview-feed">' +
            '<div v-for="(item, i) in recentList" :key="i" class="overview-feed__item" :style="{ animationDelay: (i * 0.06) + \'s\' }">' +
              '<span class="overview-feed__icon">{{ typeIcon(item.type) }}</span>' +
              '<span class="overview-feed__text text-ellipsis">{{ item.text || t(\'history.empty_preview\') }}</span>' +
              '<span class="overview-feed__meta">' +
                '<span v-if="item.pinned" class="overview-feed__pin">📌</span>' +
                '<span class="text-subtle">{{ timeAgo(item.time) }}</span>' +
              '</span>' +
            '</div>' +
          '</div>' +
          '<div v-else class="overview-empty-state">' +
            '<span class="overview-empty-state__icon">📋</span>' +
            '<span class="overview-empty-state__text">{{ t(\'overview.no_activity_hint\') }}</span>' +
          '</div>' +
        '</div>' +
      '</div>',
  };

})();
