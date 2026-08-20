/* ═══════════════════════════════════════════════════════════════════
   ClipSync Transfer Panel Component
   File transfer with device selector, send file/folder buttons,
   speed test, active transfers with controls, and transfer history.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['transfer-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        targetDevice: '',       // selected target device id (empty = none selected)
        sending: false,
      };
    },

    computed: {
      onlineDevices: function () {
        return this.store.onlineDevices();
      },
      hasActiveTransfers: function () {
        return this.store.activeTransfers.length > 0;
      },
      hasTransferHistory: function () {
        return this.store.transferHistory.length > 0;
      },
      hasAnyContent: function () {
        return this.hasActiveTransfers || this.hasTransferHistory;
      },
      speedTest: function () { return this.store.speedTest; },
      speedQualityLabel: function () {
        var q = this.speedTest.quality;
        if (q === 'fast') return this.t('transfer.speed.fast_label');
        if (q === 'good') return this.t('transfer.speed.good_label');
        if (q === 'slow') return this.t('transfer.speed.slow_label');
        return '';
      },
      speedQualityClass: function () {
        var q = this.speedTest.quality;
        if (q === 'fast') return 'badge--success';
        if (q === 'good') return 'badge--warning';
        if (q === 'slow') return 'badge--danger';
        return '';
      },
    },

    // Watch targetDevice to default to first online device
    created: function () {
      var self = this;
      this._unwatch = this.$watch('onlineDevices', function (devs) {
        if (!self.targetDevice && devs.length > 0) {
          self.targetDevice = devs[0].device_id;
        }
      }, { immediate: true });
    },

    beforeUnmount: function () {
      if (this._unwatch) this._unwatch();
    },

    template: `<div class="transfer-panel">
      <!-- Send Files Card -->
      <div class="transfer-card card" style="margin-bottom:12px">
        <div class="transfer-card__header">
          <span style="font-weight:600">📤 {{ t('transfer.send_files') }}</span>
        </div>

        <!-- Device selector -->
        <div v-if="onlineDevices.length > 0" style="margin-bottom:10px">
          <label style="font-size:12px;color:var(--clipsync-fg-muted);display:block;margin-bottom:4px">{{ t('transfer.send_to') }}</label>
          <select class="settings-select" v-model="targetDevice" style="font-size:13px">
            <option v-for="d in onlineDevices" :key="d.device_id" :value="d.device_id">
              {{ d.device_name || d.name || d.device_id }}
            </option>
          </select>
        </div>
        <div v-else style="margin-bottom:10px;font-size:12px;color:var(--clipsync-fg-muted)">
          {{ t('transfer.no_online_devices') }}
        </div>

        <div style="display:flex;gap:8px">
          <button class="btn-primary" style="flex:1;font-size:13px"
            @click="pickFile" :disabled="sending || !targetDevice">
            📄 {{ t('transfer.send_file') }}
          </button>
          <button class="btn-secondary" style="flex:1;font-size:13px"
            @click="pickFolder" :disabled="sending || !targetDevice">
            📁 {{ t('transfer.send_folder') }}
          </button>
        </div>
        <div v-if="sending" style="font-size:12px;color:var(--clipsync-fg-muted);margin-top:6px">
          {{ t('transfer.uploading') }}
        </div>

        <!-- Hidden file inputs -->
        <input type="file" ref="fileInput" style="display:none" @change="onFilePicked">
        <input type="file" ref="folderInput" style="display:none" webkitdirectory @change="onFilePicked">
      </div>

      <!-- Speed Test -->
      <div class="transfer-card card" style="margin-bottom:12px">
        <div class="transfer-card__header">
          <span style="font-weight:600">⚡ {{ t('transfer.speed_test') }}</span>
          <button v-if="!speedTest.running"
            class="btn-primary" style="padding:4px 14px;font-size:12px"
            @click="store.startSpeedTest()">
            {{ t('transfer.run') }}
          </button>
          <span v-if="speedTest.status" class="text-muted" style="font-size:12px">{{ speedTest.status }}</span>
        </div>
        <div v-if="speedTest.running || speedTest.progress > 0" class="transfer-card__progress-track">
          <div class="transfer-card__progress-fill"
            :style="{ width: (speedTest.progress * 100) + '%' }"></div>
        </div>
        <div v-if="speedTest.resultMbps !== null" style="display:flex;align-items:center;gap:8px;margin-top:4px">
          <span style="font-size:20px;font-weight:700;color:var(--clipsync-success)">{{ speedTest.resultMbps.toFixed(1) }} Mbps</span>
          <span class="badge" :class="speedQualityClass">{{ speedQualityLabel }}</span>
        </div>
        <div v-if="speedTest.error" class="text-muted" style="font-size:12px;color:var(--clipsync-danger)">{{ speedTest.error }}</div>
      </div>

      <!-- Active transfers -->
      <div v-if="hasActiveTransfers" class="transfer-panel__section">
        <div class="section-header">📡 {{ t('transfer.active_transfers') }}</div>
        <div v-for="tr in store.activeTransfers" :key="tr.id" class="transfer-card card">
          <div class="transfer-card__header">
            <span v-if="tr.direction === 'up'">📤</span>
            <span v-else>📥</span>
            <span class="text-ellipsis" style="font-weight:600;flex:1">{{ tr.filename || t('transfer.unknown_file') }}</span>
            <span v-if="tr.size" style="font-size:12px;color:var(--clipsync-fg-muted)">{{ formatSize(tr.size) }}</span>
          </div>
          <div class="transfer-card__progress-track">
            <div class="transfer-card__progress-fill"
              :style="{ width: (tr.progress || 0) + '%' }"></div>
          </div>
          <div style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--clipsync-fg-muted)">
            <span>{{ tr.progress || 0 }}%</span>
            <span v-if="tr.speed">{{ formatSpeed(tr.speed) }}</span>
            <span v-if="tr.eta">{{ tr.eta }}</span>
            <span style="flex:1"></span>
            <button v-if="tr.status !== 'paused'" class="btn-ghost" style="padding:2px 8px;font-size:11px"
              @click="pauseTransfer(tr.id)">⏸</button>
            <button v-if="tr.status === 'paused'" class="btn-ghost" style="padding:2px 8px;font-size:11px"
              @click="resumeTransfer(tr.id)">▶</button>
            <button class="btn-ghost" style="padding:2px 8px;font-size:11px;color:var(--clipsync-danger)"
              @click="cancelTransfer(tr.id)">✕</button>
          </div>
        </div>
      </div>

      <!-- Transfer history -->
      <div v-if="hasTransferHistory" class="transfer-panel__section">
        <div class="section-header">📚 {{ t('transfer.history_title') }}</div>
        <div v-for="tr in store.transferHistory" :key="tr.id" class="transfer-card transfer-card--done card">
          <div style="display:flex;align-items:center;gap:8px">
            <span v-if="tr.status === 'completed'">✅</span>
            <span v-else>❌</span>
            <span class="text-ellipsis" style="flex:1">{{ tr.filename || t('transfer.unknown_file') }}</span>
            <span v-if="tr.size" style="font-size:12px;color:var(--clipsync-fg-muted)">{{ formatSize(tr.size) }}</span>
            <button v-if="tr.path" class="btn-ghost" style="padding:2px 8px;font-size:11px"
              @click="openFile(tr.path)">{{ t('transfer.open') }}</button>
          </div>
        </div>
      </div>

      <!-- Empty state (only when no transfers and no speed test) -->
      <div v-if="!hasAnyContent && (!speedTest.running && speedTest.resultMbps === null && !speedTest.error)" class="panel-empty">
        <span class="panel-empty-icon">📤</span>
        <p class="panel-empty-title">{{ t('transfer.no_transfers') }}</p>
        <p class="panel-empty-desc">
          {{ t('transfer.empty_desc') }}
        </p>
      </div>
    </div>`,

    methods: {
      pickFile: function () {
        this.$refs.fileInput.click();
      },
      pickFolder: function () {
        this.$refs.folderInput.click();
      },
      onFilePicked: function (e) {
        var files = e.target.files;
        if (!files || files.length === 0) return;
        var self = this;
        var deviceId = this.targetDevice;
        if (!deviceId) {
          this.store.showToast(this.t('transfer.select_target'), 2000);
          return;
        }
        self.sending = true;
        var uploaded = 0;
        var total = files.length;
        var errors = [];

        function uploadNext(idx) {
          if (idx >= total) {
            self.sending = false;
            e.target.value = '';
            if (errors.length === 0) {
              self.store.showToast(self.t('transfer.sent_files', { count: uploaded }), 2000);
            } else {
              self.store.showToast(self.t('transfer.sent_partial', { uploaded: uploaded, total: total, failed: errors.length }), 2000);
            }
            return;
          }
          var file = files[idx];
          ClipsyncAPI.uploadFile(file, deviceId).then(function () {
            uploaded++;
            uploadNext(idx + 1);
          }).catch(function () {
            errors.push(file.name);
            uploadNext(idx + 1);
          });
        }
        uploadNext(0);
      },
      formatSpeed: function (bytesPerSec) {
        if (!bytesPerSec || bytesPerSec === 0) return '';
        if (bytesPerSec < 1024) return Math.round(bytesPerSec) + ' B/s';
        if (bytesPerSec < 1024 * 1024) return (bytesPerSec / 1024).toFixed(1) + ' KB/s';
        return (bytesPerSec / (1024 * 1024)).toFixed(1) + ' MB/s';
      },
      formatSize: function (bytes) {
        if (!bytes || bytes === 0) return '';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
      },
      cancelTransfer: function (id) {
        var self = this;
        ClipsyncAPI.cancelTransfer(id).then(function () {
          self.store.showToast(self.t('transfer.cancelled'), 1500);
        }).catch(function () {});
      },
      pauseTransfer: function (id) {
        var self = this;
        ClipsyncAPI.pauseTransfer(id).then(function () {
          self.store.showToast(self.t('transfer.paused'), 1500);
        }).catch(function () {});
      },
      resumeTransfer: function (id) {
        var self = this;
        ClipsyncAPI.resumeTransfer(id).then(function () {
          self.store.showToast(self.t('transfer.resumed'), 1500);
        }).catch(function () {});
      },
      openFile: function (path) {
        ClipsyncAPI.navigate(path, '');
      },
    },
  };

})();
