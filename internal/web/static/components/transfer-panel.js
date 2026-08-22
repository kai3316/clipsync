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
        phoneQrSending: false,
        peerOffline: false,     // true when the selected target disconnected during a send
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

    // Watch onlineDevices to default to first online device and to drop a
    // stale targetDevice as soon as the selected peer disconnects.
    created: function () {
      var self = this;
      this._unwatch = this.$watch('onlineDevices', function (devs) {
        var ids = devs.map(function (d) { return d.device_id; });
        if (self.targetDevice && ids.indexOf(self.targetDevice) === -1) {
          // The selected peer went offline — clear it so the send buttons
          // disable rather than aim at a stale target. If a send was in
          // flight, surface a small "peer offline" hint.
          if (self.sending) {
            self.peerOffline = true;
          }
          self.targetDevice = '';
        }
        // Only auto-select the first online device when there is genuinely no
        // selection yet — never immediately after clearing a stale target
        // (that would re-aim the send buttons at a peer the user never chose
        // and defeat the "peer offline" hint above).
        if (!self.targetDevice && !self.peerOffline && devs.length > 0) {
          self.targetDevice = devs[0].device_id;
        }
      }, { immediate: true });
    },

    beforeUnmount: function () {
      if (this._unwatch) this._unwatch();
    },

    template: `<div class="transfer-panel">
      <!-- Speed Test -->
      <div class="transfer-card transfer-card--panel glass" style="margin-bottom:12px">
        <div class="transfer-card__header">
          <span class="transfer-card__title">⚡ {{ t('transfer.speed_test') }}</span>
          <span v-if="speedTest.running" class="transfer-speed-status">{{ t('transfer.running') }}</span>
          <button v-if="!speedTest.running" class="transfer-run-btn" @click="store.startSpeedTest()">
            ▶ {{ t('transfer.run') }}
          </button>
        </div>
        <div class="transfer-speed-body"
             v-if="speedTest.running || speedTest.progress > 0 || speedTest.resultMbps !== null">
          <div class="transfer-speed-result" v-if="speedTest.resultMbps !== null">
            <span class="transfer-speed-value">{{ speedTest.resultMbps.toFixed(1) }}<span class="transfer-speed-unit">MB/s</span></span>
            <span class="badge" :class="speedQualityClass">{{ speedQualityLabel }}</span>
          </div>
          <div class="transfer-speed-progress" v-if="speedTest.running || speedTest.progress > 0">
            <div class="transfer-speed-progress__fill" :style="{ width: (speedTest.progress * 100) + '%' }"></div>
          </div>
        </div>
        <div v-if="speedTest.error" class="transfer-speed-error">{{ speedTest.error }}</div>
      </div>

      <!-- Send Files Card -->
      <div class="transfer-card transfer-card--panel glass" style="margin-bottom:12px">
        <div class="transfer-card__header">
          <span class="transfer-card__title">📤 {{ t('transfer.send_files') }}</span>
        </div>

        <!-- Device selector -->
        <div class="transfer-device-select" v-if="onlineDevices.length > 0">
          <span class="transfer-device-select__label">{{ t('transfer.send_to') }}</span>
          <select class="settings-select" v-model="targetDevice">
            <option v-for="d in onlineDevices" :key="d.device_id" :value="d.device_id">
              {{ d.device_name || d.name || d.device_id }}
            </option>
          </select>
        </div>
        <div v-else class="transfer-empty-hint">{{ t('transfer.no_online_devices') }}</div>
        <div v-if="peerOffline" class="transfer-empty-hint" style="margin-top:6px">{{ t('transfer.peer_offline') }}</div>

        <div class="transfer-send-actions">
          <button class="transfer-send-btn transfer-send-btn--file"
            @click="pickFile" :disabled="sending || !targetDevice">
            <span class="transfer-send-btn__icon">📄</span>
            <span class="transfer-send-btn__text">
              <span class="transfer-send-btn__label">{{ t('transfer.send_file') }}</span>
              <span class="transfer-send-btn__sub">{{ t('transfer.send_file_hint') }}</span>
            </span>
          </button>
          <button class="transfer-send-btn transfer-send-btn--folder"
            @click="pickFolder" :disabled="sending || !targetDevice">
            <span class="transfer-send-btn__icon">📁</span>
            <span class="transfer-send-btn__text">
              <span class="transfer-send-btn__label">{{ t('transfer.send_folder') }}</span>
              <span class="transfer-send-btn__sub">{{ t('transfer.send_folder_hint') }}</span>
            </span>
          </button>
        </div>
        <div v-if="sending" class="transfer-uploading">{{ t('transfer.uploading') }}</div>

        <!-- Send to Phone (QR) — phones connect via Remote access, not P2P -->
        <div class="transfer-phone-row" style="margin-top:10px">
          <button
            class="transfer-send-btn"
            @click="showPhoneQr"
            :disabled="phoneQrSending"
            style="width:100%"
          >
            <span class="transfer-send-btn__icon">📱</span>
            <span class="transfer-send-btn__text">
              <span class="transfer-send-btn__label">{{ t('transfer.phone_title') }}</span>
              <span class="transfer-send-btn__sub">{{ t('transfer.phone_action') }}</span>
            </span>
          </button>
        </div>

        <!-- Hidden file inputs -->
        <input type="file" ref="fileInput" style="display:none" @change="onFilePicked">
        <input type="file" ref="folderInput" style="display:none" webkitdirectory @change="onFilePicked">
      </div>

      <!-- Active transfers -->
      <div v-if="hasActiveTransfers" class="transfer-panel__section">
        <div class="section-header">📡 {{ t('transfer.active_transfers') }}</div>
        <div
          v-for="tr in store.activeTransfers"
          :key="tr.id"
          class="transfer-history-item card"
          :class="'transfer-history-item--' + (tr.direction === 'up' ? 'up' : 'down')"
        >
          <div
            class="transfer-history-item__icon"
            :class="tr.direction === 'up' ? 'transfer-history-item__icon--up' : 'transfer-history-item__icon--down'"
          >{{ tr.direction === 'up' ? '↗' : '↘' }}</div>
          <div class="transfer-history-item__body">
            <div class="transfer-history-item__name">{{ tr.filename || t('transfer.unknown_file') }}</div>
            <div class="transfer-history-item__meta">
              <span class="transfer-history-item__status transfer-history-item__status--active">
                {{ tr.status === 'paused' ? t('transfer.state.paused') : (tr.progress || 0) + '%' }}
              </span>
              <span v-if="tr.size">{{ formatSize(tr.size) }}</span>
              <span v-if="tr.speed">{{ formatSpeed(tr.speed) }}</span>
              <span v-if="tr.eta">{{ tr.eta }}</span>
            </div>
            <div class="transfer-history-item__progress">
              <div
                class="transfer-history-item__progress-fill"
                :class="{ 'transfer-history-item__progress-fill--paused': tr.status === 'paused' }"
                :style="{ width: (tr.progress || 0) + '%' }"
              ></div>
            </div>
          </div>
          <div class="transfer-history-item__actions">
            <button
              v-if="tr.status !== 'paused'"
              class="transfer-history-item__btn"
              :title="t('transfer.pause')"
              @click="pauseTransfer(tr.id)"
            >&#9208;</button>
            <button
              v-if="tr.status === 'paused'"
              class="transfer-history-item__btn"
              :title="t('transfer.resume')"
              @click="resumeTransfer(tr.id)"
            >&#9654;</button>
            <button
              class="transfer-history-item__btn transfer-history-item__btn--danger"
              :title="t('transfer.cancel')"
              @click="cancelTransfer(tr.id)"
            >&#10005;</button>
          </div>
        </div>
      </div>

      <!-- Transfer history -->
      <div v-if="hasTransferHistory" class="transfer-panel__section">
        <div class="section-header">📚 {{ t('transfer.history_title') }}</div>
        <div class="transfer-history">
          <div
            v-for="tr in store.transferHistory"
            :key="tr.id"
            class="transfer-history-item card"
            :class="'transfer-history-item--' + (tr.direction === 'up' ? 'up' : 'down')"
          >
            <div
              class="transfer-history-item__icon"
              :class="tr.direction === 'up' ? 'transfer-history-item__icon--up' : 'transfer-history-item__icon--down'"
            >{{ tr.direction === 'up' ? '↗' : '↘' }}</div>
            <div class="transfer-history-item__body">
              <div class="transfer-history-item__name">{{ tr.filename || t('transfer.unknown_file') }}</div>
              <div class="transfer-history-item__meta">
                <span
                  class="transfer-history-item__status"
                  :class="transferStatusClass(tr)"
                >{{ transferStatusLabel(tr) }}</span>
                <span v-if="tr.size" class="transfer-history-item__size">{{ formatSize(tr.size) }}</span>
                <span v-if="tr.timestamp" class="transfer-history-item__time">{{ formatTimestamp(tr.timestamp) }}</span>
              </div>
            </div>
            <div class="transfer-history-item__actions">
              <button
                v-if="tr.path"
                class="transfer-history-item__btn"
                :title="t('transfer.open')"
                @click="openFile(tr.path)"
              ><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></button>
              <button
                v-if="tr.path"
                class="transfer-history-item__btn"
                :title="t('transfer.open_folder')"
                @click="revealFile(tr.path)"
              ><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></button>
            </div>
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
      showPhoneQr: function () {
        var self = this;
        self.phoneQrSending = true;
        // Server-pushed QR dialog — the desktop app displays it so the user
        // can scan it with their phone.
        ClipsyncAPI._fetch('POST', '/api/show_qr', {})
          .then(function () { self.phoneQrSending = false; })
          .catch(function () {
            self.phoneQrSending = false;
            self.store.showToast(self.t('transfer.phone_qr_failed'), 2000);
          });
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
              var msg = self.t('transfer.sent_partial', { uploaded: uploaded, total: total, failed: errors.length });
              var reason = errors[0].reason;
              if (reason) {
                msg += ' — ' + reason;
              }
              self.store.showToast(msg, 3000);
            }
            return;
          }
          var file = files[idx];
          ClipsyncAPI.uploadFile(file, deviceId).then(function (res) {
            if (res && res.ok === false) {
              // HTTP 200 with {ok:false, error} — the backend surfaces the
              // real reason (peer offline, file too large, ...).
              errors.push({ name: file.name, reason: (res.error || '') });
            } else {
              uploaded++;
            }
            uploadNext(idx + 1);
          }).catch(function (e) {
            var reason = (e && (e.message || e.error)) || '';
            errors.push({ name: file.name, reason: reason });
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
      formatTimestamp: function (ts) {
        if (!ts) return '';
        var d = new Date(ts * 1000);
        var month = String(d.getMonth() + 1).padStart(2, '0');
        var day = String(d.getDate()).padStart(2, '0');
        var hours = String(d.getHours()).padStart(2, '0');
        var mins = String(d.getMinutes()).padStart(2, '0');
        return month + '/' + day + ' ' + hours + ':' + mins;
      },
      isCancelledTransfer: function (tr) {
        return !!(tr && (tr.status === 'cancelled' || tr.cancelled));
      },
      transferStatusLabel: function (tr) {
        if (tr.status === 'completed') return this.t('transfer.status_completed');
        if (this.isCancelledTransfer(tr)) return this.t('transfer.status_cancelled');
        return this.t('transfer.status_failed');
      },
      transferStatusClass: function (tr) {
        if (tr.status === 'completed') return 'transfer-history-item__status--ok';
        if (this.isCancelledTransfer(tr)) return 'transfer-history-item__status--active';
        return 'transfer-history-item__status--err';
      },
      // Reconcile the active/history transfer lists with the server after a
      // control action (pause/resume/cancel) so the UI reflects the backend.
      _refreshTransfers: function () {
        var self = this;
        return ClipsyncAPI.getTransfers().then(function (res) {
          if (res && res.active) {
            self.store.activeTransfers.splice(0, self.store.activeTransfers.length);
            for (var i = 0; i < res.active.length; i++) {
              self.store.activeTransfers.push(res.active[i]);
            }
          }
          if (res && res.history) {
            self.store.transferHistory.splice(0, self.store.transferHistory.length);
            for (var j = 0; j < res.history.length; j++) {
              self.store.transferHistory.push(res.history[j]);
            }
          }
          return res;
        });
      },
      cancelTransfer: function (id) {
        var self = this;
        ClipsyncAPI.cancelTransfer(id).then(function () {
          // Optimistically drop the row, then reconcile with the server.
          var idx = self.store.activeTransfers.findIndex(function (t) { return t.id === id; });
          if (idx !== -1) {
            self.store.activeTransfers.splice(idx, 1);
          }
          self.store.showToast(self.t('transfer.cancelled'), 1500);
          self._refreshTransfers().catch(function () {});
        }).catch(function () {
          self.store.showToast(self.t('transfer.cancel_failed'), 2000);
        });
      },
      pauseTransfer: function (id) {
        var self = this;
        ClipsyncAPI.pauseTransfer(id).then(function () {
          var idx = self.store.activeTransfers.findIndex(function (t) { return t.id === id; });
          if (idx !== -1) {
            self.store.activeTransfers[idx].status = 'paused';
          }
          self.store.showToast(self.t('transfer.paused'), 1500);
          self._refreshTransfers().catch(function () {});
        }).catch(function () {
          self.store.showToast(self.t('transfer.pause_failed'), 2000);
        });
      },
      resumeTransfer: function (id) {
        var self = this;
        ClipsyncAPI.resumeTransfer(id).then(function () {
          var idx = self.store.activeTransfers.findIndex(function (t) { return t.id === id; });
          if (idx !== -1) {
            self.store.activeTransfers[idx].status = 'sending';
          }
          self.store.showToast(self.t('transfer.resumed'), 1500);
          self._refreshTransfers().catch(function () {});
        }).catch(function () {
          self.store.showToast(self.t('transfer.resume_failed'), 2000);
        });
      },
      openFile: function (path) {
        var self = this;
        // /api/nav only accepts http/https URLs, so local file paths must go
        // through the dedicated file-open endpoint on the host.
        ClipsyncAPI.openFile(path)
          .then(function (res) {
            if (!res || res.ok !== true) {
              self.store.showToast(self.t('ui.open_failed_title'), 2000);
            }
          })
          .catch(function () {
            self.store.showToast(self.t('ui.open_failed_title'), 2000);
          });
      },

      revealFile: function (path) {
        var self = this;
        ClipsyncAPI.revealFile(path)
          .then(function (res) {
            if (!res || res.ok !== true) {
              self.store.showToast(self.t('ui.open_failed_title'), 2000);
            }
          })
          .catch(function () {
            self.store.showToast(self.t('ui.open_failed_title'), 2000);
          });
      },
    },
  };

})();
