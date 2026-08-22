/* ═══════════════════════════════════════════════════════════════════
   ClipSync Nearby Chat Panel Component
   Two-column layout: left = chat-able devices + session list; right =
   the open conversation (text bubbles, system entries, file cards, an
   invite banner, and a text/file composer).

   Wire contract (see README / backend api/chat.py):
     WS: chat_sessions, chat_message, chat_progress, chat_file_done
     GET  /api/chat/devices|sessions|messages|download
     POST /api/chat/invite|text|file|file/{accept,decline,cancel}|
             {accept,decline,close,read}
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  window.__CLIPSYNC_COMPONENTS__ = window.__CLIPSYNC_COMPONENTS__ || {};

  window.__CLIPSYNC_COMPONENTS__['chat-panel'] = {
    inject: ['store'],

    data: function () {
      return {
        chatDevices: [],       // devices from /api/chat/devices
        devicesLoading: false,
        sessionsLoading: false,
        messagesLoading: false,
        composing: '',         // composer input text
        sendingText: false,
        sendingFile: false,
        inviteBusy: '',        // peer_id of an in-flight invite
        fileBusy: '',          // transfer_id of an in-flight file action
      };
    },

    computed: {
      deviceList: function () {
        return this.chatDevices || [];
      },

      activeSession: function () {
        var sid = this.store.activeChatSession;
        if (!sid) return null;
        var self = this;
        var found = this.store.chatSessions.find(function (s) {
          return s.session_id === sid;
        });
        return found || null;
      },

      hasOpenSession: function () {
        return !!(this.store.activeChatSession && this.activeSession);
      },

      // Incoming invitation awaiting accept/decline — show the invite banner.
      isInvitedSession: function () {
        var s = this.activeSession;
        return !!(s && s.status === 'invited');
      },

      canSend: function () {
        return !!this.store.activeChatSession &&
          !!((this.composing || '').trim()) &&
          !this.sendingText;
      },

      // Most-recently-active first.
      sortedSessions: function () {
        var list = this.store.chatSessions.slice();
        list.sort(function (a, b) {
          return (b.last_activity_ts || 0) - (a.last_activity_ts || 0);
        });
        return list;
      },
    },

    watch: {
      // Auto-scroll whenever new entries land in the open conversation.
      'store.chatMessages': function () {
        this._scrollToBottom();
      },

      // Safety net: if a chat_sessions push shows the session we're viewing
      // with unread > 0 (e.g. an incoming message that ws.js couldn't echo
      // because the pane wasn't mounted yet), refetch the conversation and
      // mark it read.
      'store.chatSessions': function (sessions) {
        var self = this;
        if (!sessions || !this.store.activeChatSession) return;
        var active = sessions.find(function (s) {
          return s.session_id === self.store.activeChatSession;
        });
        if (active && (active.unread || 0) > 0) {
          this.loadMessages();
          this.markRead();
        }
      },
    },

    created: function () {
      this.loadDevices();
      this.loadSessions();
      // A session may already be open (e.g. restored by the app entry).
      if (this.store.activeChatSession) {
        this.loadMessages();
        this.markRead();
      }
    },

    template:
      '<div class="chat-panel">' +
        '<!-- Left column: devices + sessions -->' +
        '<div class="chat-panel__sidebar">' +
          '<div class="section-header">💻 {{ t(\'chat.devices_header\') }}</div>' +
          '<div v-if="devicesLoading" class="chat-skeleton">' +
            '<div class="skeleton-card animate-shimmer"></div>' +
          '</div>' +
          '<div v-else-if="deviceList.length === 0" class="chat-side-empty">' +
            '{{ t(\'chat.empty_no_devices\') }}' +
          '</div>' +
          '<div v-else class="chat-device-list">' +
            '<div v-for="d in deviceList" :key="d.peer_id" class="chat-device-row">' +
              '<div class="chat-device-row__info">' +
                '<span class="chat-device-row__name">{{ d.name || d.peer_id }}</span>' +
                '<span class="badge" :class="d.paired ? \'chat-badge--paired\' : \'chat-badge--unpaired\'">' +
                  '{{ d.paired ? t(\'chat.paired_tag\') : t(\'chat.unpaired_tag\') }}' +
                '</span>' +
              '</div>' +
              '<button class="chat-mini-btn chat-mini-btn--accent" :disabled="inviteBusy === d.peer_id" @click="startChat(d)">' +
                '{{ inviteBusy === d.peer_id ? \'...\' : t(\'chat.start\') }}' +
              '</button>' +
            '</div>' +
          '</div>' +

          '<div class="section-header">💬 {{ t(\'chat.sessions_header\') }}</div>' +
          '<div v-if="sessionsLoading" class="chat-skeleton">' +
            '<div class="skeleton-card animate-shimmer"></div>' +
          '</div>' +
          '<div v-else-if="store.chatSessions.length === 0" class="chat-side-empty">' +
            '{{ t(\'chat.empty_no_session\') }}' +
          '</div>' +
          '<div v-else class="chat-session-list">' +
            '<div v-for="s in sortedSessions" :key="s.session_id" class="chat-session-row"' +
              ' :class="{ \'chat-session-row--active\': store.activeChatSession === s.session_id }"' +
              ' @click="openSession(s)">' +
              '<span class="chat-session-row__dot"' +
                ' :class="s.online ? \'chat-session-row__dot--on\' : \'chat-session-row__dot--off\'"></span>' +
              '<div class="chat-session-row__body">' +
                '<div class="chat-session-row__top">' +
                  '<span class="chat-session-row__name">{{ s.peer_name || s.peer_id }}</span>' +
                  '<span class="chat-session-row__time">{{ formatTime(s.last_activity_ts) }}</span>' +
                '</div>' +
                '<div class="chat-session-row__bottom">' +
                  '<span class="chat-session-row__preview">{{ s.last_preview || \'\' }}</span>' +
                  '<span class="chat-session-row__status" :class="sessionStatusClass(s)">{{ sessionStatusLabel(s) }}</span>' +
                  '<span v-if="(s.unread || 0) > 0" class="chat-session-row__badge">{{ s.unread }}</span>' +
                '</div>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<!-- Right column: conversation -->' +
        '<div class="chat-panel__main">' +
          '<div v-if="!hasOpenSession" class="panel-empty">' +
            '<span class="panel-empty-icon">💬</span>' +
            '<p class="panel-empty-title">{{ t(\'chat.empty_no_session\') }}</p>' +
            '<p class="panel-empty-desc">{{ t(\'chat.invite_prompt\') }}</p>' +
          '</div>' +

          '<template v-else>' +
            '<!-- Incoming invite banner -->' +
            '<div v-if="isInvitedSession" class="chat-invite-banner">' +
              '<div class="chat-invite-banner__title">{{ t(\'chat.invite_banner_title\') }}</div>' +
              '<div class="chat-invite-banner__peer">{{ activeSession.peer_name }}</div>' +
              '<div class="chat-invite-banner__fp">{{ t(\'chat.invite_fingerprint\') }}: {{ activeSession.fingerprint_short }}</div>' +
              '<div class="chat-invite-banner__actions">' +
                '<button class="chat-action-btn chat-action-btn--accept" :disabled="!!inviteBusy" @click="respondInvite(\'accept\')">{{ t(\'chat.accept\') }}</button>' +
                '<button class="chat-action-btn chat-action-btn--decline" :disabled="!!inviteBusy" @click="respondInvite(\'decline\')">{{ t(\'chat.decline\') }}</button>' +
              '</div>' +
            '</div>' +

            '<div class="chat-conv-header">' +
              '<span class="chat-conv-header__name">{{ activeSession.peer_name }}</span>' +
              '<span class="chat-conv-header__status">{{ sessionStatusLabel(activeSession) }}</span>' +
              '<button class="chat-mini-btn chat-mini-btn--danger" @click="closeSession">{{ t(\'chat.close\') }}</button>' +
            '</div>' +

            '<div ref="msgList" class="chat-conversation">' +
              '<div v-if="messagesLoading && store.chatMessages.length === 0" class="chat-conv-loading">{{ t(\'ui.loading\') }}</div>' +
              '<div v-else-if="store.chatMessages.length === 0" class="chat-conv-empty">{{ t(\'chat.invite_greeting\') }}</div>' +
              '<template v-else>' +
                '<div v-for="m in store.chatMessages" :key="m.entry_id" class="chat-msg-row">' +
                  '<div v-if="m.kind === \'system\'" class="chat-system">{{ t(m.text_key || m.text || \'\', m.fmt) }}</div>' +

                  '<div v-else-if="m.kind === \'text\'" class="chat-bubble"' +
                    ' :class="m.outgoing ? \'chat-bubble--out\' : \'chat-bubble--in\'">' +
                    '<div class="chat-bubble__text">{{ m.text }}</div>' +
                    '<div class="chat-bubble__meta">{{ formatTime(m.ts) }}</div>' +
                  '</div>' +

                  '<div v-else class="chat-file"' +
                    ' :class="m.outgoing ? \'chat-file--out\' : \'chat-file--in\'">' +
                    '<div class="chat-file__icon">📄</div>' +
                    '<div class="chat-file__body">' +
                      '<div class="chat-file__name">{{ m.file_name }}</div>' +
                      '<div class="chat-file__meta">' +
                        '<span>{{ formatSize(m.file_size) }}</span>' +
                        '<span class="chat-file__status">{{ fileStatusLabel(m) }}</span>' +
                      '</div>' +
                      '<div v-if="m.status === \'sending\' && typeof m.fraction === \'number\'" class="chat-file__progress">' +
                        '<div class="chat-file__progress-fill" :style="{ width: Math.round(m.fraction * 100) + \'%\' }"></div>' +
                      '</div>' +
                    '</div>' +
                    '<div class="chat-file__actions">' +
                      '<button v-if="!m.outgoing && m.status === \'await_accept\'"' +
                        ' class="chat-action-btn chat-action-btn--accept" :disabled="fileBusy === m.transfer_id" @click="fileAction(m, \'accept\')">{{ t(\'chat.accept\') }}</button>' +
                      '<button v-if="!m.outgoing && m.status === \'await_accept\'"' +
                        ' class="chat-action-btn chat-action-btn--decline" :disabled="fileBusy === m.transfer_id" @click="fileAction(m, \'decline\')">{{ t(\'chat.decline\') }}</button>' +
                      '<button v-if="m.status === \'sending\'"' +
                        ' class="chat-action-btn chat-action-btn--danger" :disabled="fileBusy === m.transfer_id" @click="fileAction(m, \'cancel\')">{{ t(\'chat.cancel\') }}</button>' +
                      '<button v-if="m.status === \'done\' && m.saved_path"' +
                        ' class="chat-action-btn chat-action-btn--accept" @click="downloadFile(m)">{{ t(\'chat.download\') }}</button>' +
                    '</div>' +
                  '</div>' +
                '</div>' +
              '</template>' +
            '</div>' +

            '<div class="chat-composer">' +
              '<button class="chat-composer__attach" :title="t(\'chat.attach\')" :disabled="sendingFile" @click="pickFile">' +
                '{{ sendingFile ? \'...\' : \'📎\' }}' +
              '</button>' +
              '<input class="chat-composer__input" type="text" v-model="composing"' +
                ' :placeholder="t(\'chat.input_placeholder\')" :disabled="sendingText"' +
                ' maxlength="4000" @keyup.enter="sendText">' +
              '<button class="chat-composer__send" :disabled="!canSend" @click="sendText">{{ t(\'chat.send\') }}</button>' +
              '<input type="file" ref="fileInput" style="display:none" @change="onFilePicked">' +
            '</div>' +
          '</template>' +
        '</div>' +
      '</div>',

    methods: {
      /* ── Data loading ─────────────────────────────────────────── */

      loadDevices: function () {
        var self = this;
        this.devicesLoading = true;
        ClipsyncAPI.chatDevices()
          .then(function (res) {
            self.chatDevices = (res && res.devices) ? res.devices : [];
          })
          .catch(function (e) {
            console.error('[ClipSync] Failed to load chat devices:', e);
          })
          .finally(function () {
            self.devicesLoading = false;
          });
      },

      loadSessions: function () {
        var self = this;
        this.sessionsLoading = true;
        ClipsyncAPI.chatSessions()
          .then(function (res) {
            if (res && res.sessions) {
              self.store.replaceChatSessions(res.sessions);
            }
          })
          .catch(function (e) {
            console.error('[ClipSync] Failed to load chat sessions:', e);
          })
          .finally(function () {
            self.sessionsLoading = false;
          });
      },

      loadMessages: function () {
        var self = this;
        var sid = this.store.activeChatSession;
        if (!sid) return Promise.resolve();
        this.messagesLoading = true;
        return ClipsyncAPI.chatMessages(sid)
          .then(function (res) {
            if (res && res.messages) {
              self.store.replaceChatMessages(res.messages);
            }
            self._scrollToBottom();
          })
          .catch(function (e) {
            console.error('[ClipSync] Failed to load chat messages:', e);
          })
          .finally(function () {
            self.messagesLoading = false;
          });
      },

      /* ── Session selection / actions ─────────────────────────── */

      openSession: function (session) {
        if (!session || this.store.activeChatSession === session.session_id) return;
        this.store.activeChatSession = session.session_id;
        this.loadMessages();
        this.markRead();
      },

      markRead: function () {
        var self = this;
        var sid = this.store.activeChatSession;
        if (!sid) return Promise.resolve();
        // Zero the local unread immediately so the badge and row settle.
        var idx = this.store.chatSessions.findIndex(function (s) {
          return s.session_id === sid;
        });
        if (idx !== -1) {
          this.store.chatSessions[idx].unread = 0;
          this.store.recalcChatUnread();
        }
        return ClipsyncAPI.chatSessionAction(sid, 'read').catch(function (e) {
          console.error('[ClipSync] Failed to mark chat read:', e);
        });
      },

      startChat: function (device) {
        var self = this;
        if (!device || !device.peer_id) return;
        this.inviteBusy = device.peer_id;
        ClipsyncAPI.chatInvite(device.peer_id, device.name || device.peer_id)
          .then(function (res) {
            if (res && res.session_id) {
              self.store.activeChatSession = res.session_id;
              // Optimistically ensure the session exists in the list so the
              // conversation pane renders immediately; loadSessions() then
              // reconciles the authoritative row.
              var existing = self.store.chatSessions.find(function (s) {
                return s.session_id === res.session_id;
              });
              if (!existing) {
                self.store.chatSessions.unshift({
                  session_id: res.session_id,
                  peer_id: device.peer_id,
                  peer_name: device.name || device.peer_id,
                  fingerprint_short: device.fingerprint_short || '',
                  status: 'inviting',
                  created_ts: Date.now() / 1000,
                  last_activity_ts: Date.now() / 1000,
                  unread: 0,
                  online: true,
                  last_preview: '',
                });
              }
              self.loadMessages();
              self.loadSessions();
            } else if (res && res.connecting) {
              // The peer is connecting — the session will arrive via a
              // chat_sessions push. Surface a short "connecting" notice.
              self.store.showToast(self.t('chat.connecting'), 2000);
            }
          })
          .catch(function (e) {
            console.error('[ClipSync] Failed to start chat:', e);
            self.store.showToast(self.t('chat.err_connect_timeout'), 2500);
          })
          .finally(function () {
            self.inviteBusy = '';
          });
      },

      respondInvite: function (action) {
        var self = this;
        var sid = this.store.activeChatSession;
        if (!sid) return;
        this.inviteBusy = sid;
        ClipsyncAPI.chatSessionAction(sid, action)
          .then(function () {
            if (action === 'decline') {
              // The invite is settled — leave the conversation; the backend
              // may drop the session or keep it in a declined state.
              self.store.activeChatSession = '';
              self.store.chatMessages.splice(0, self.store.chatMessages.length);
            } else {
              self.loadMessages();
              self.markRead();
            }
            self.loadSessions();
          })
          .catch(function (e) {
            console.error('[ClipSync] Failed to respond to invite:', e);
          })
          .finally(function () {
            self.inviteBusy = '';
          });
      },

      closeSession: function () {
        var self = this;
        var sid = this.store.activeChatSession;
        if (!sid) return;
        ClipsyncAPI.chatSessionAction(sid, 'close')
          .then(function () {
            self.store.activeChatSession = '';
            self.store.chatMessages.splice(0, self.store.chatMessages.length);
            self.loadSessions();
          })
          .catch(function (e) {
            console.error('[ClipSync] Failed to close session:', e);
          });
      },

      /* ── Composer / text ─────────────────────────────────────── */

      sendText: function () {
        var self = this;
        var text = (this.composing || '').trim();
        if (!text || !this.store.activeChatSession) return;
        this.sendingText = true;
        ClipsyncAPI.chatSendText(this.store.activeChatSession, text)
          .then(function () {
            self.composing = '';
            // The backend echoes the entry via chat_message; refetch to be safe.
            self.loadMessages();
          })
          .catch(function (e) {
            console.error('[ClipSync] Failed to send chat text:', e);
            self.store.showToast(self.t('chat.err_send_failed'), 2500);
          })
          .finally(function () {
            self.sendingText = false;
          });
      },

      /* ── File send / actions ─────────────────────────────────── */

      pickFile: function () {
        this.$refs.fileInput.click();
      },

      onFilePicked: function (e) {
        var files = e.target.files;
        if (!files || files.length === 0) return;
        var self = this;
        var sid = this.store.activeChatSession;
        if (!sid) {
          this.store.showToast(this.t('chat.empty_no_session'), 2000);
          e.target.value = '';
          return;
        }
        var file = files[0];
        this.sendingFile = true;
        // Reuse the existing /api/upload flow (the transfer panel's file
        // picker) — it saves to the server's receive dir and returns the
        // basename, which the chat/file endpoint resolves to a path.
        ClipsyncAPI.uploadFile(file)
          .then(function (res) {
            var filePath = (res && (res.path || res.filepath || res.name)) || '';
            if (!filePath) {
              throw new Error('No server path returned from upload');
            }
            return ClipsyncAPI.chatSendFile(sid, filePath);
          })
          .then(function () {
            e.target.value = '';
            self.loadMessages();
          })
          .catch(function (err) {
            console.error('[ClipSync] Failed to send chat file:', err);
            self.store.showToast(self.t('chat.err_send_failed'), 2500);
          })
          .finally(function () {
            self.sendingFile = false;
          });
      },

      fileAction: function (entry, action) {
        var self = this;
        var sid = this.store.activeChatSession;
        if (!sid || !entry || !entry.transfer_id) return;
        this.fileBusy = entry.transfer_id;
        ClipsyncAPI.chatFileAction(sid, entry.transfer_id, action)
          .then(function () {
            // Refetch so the card reflects the authoritative status change.
            self.loadMessages();
          })
          .catch(function (e) {
            console.error('[ClipSync] Failed to ' + action + ' chat file:', e);
          })
          .finally(function () {
            self.fileBusy = '';
          });
      },

      downloadFile: function (entry) {
        if (!entry || !entry.transfer_id) return;
        var a = document.createElement('a');
        a.href = ClipsyncAPI.chatDownloadUrl(entry.transfer_id);
        // Hint the browser to save under the real filename.
        a.download = entry.file_name || '';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      },

      /* ── Label helpers ───────────────────────────────────────── */

      sessionStatusLabel: function (s) {
        var st = s && s.status;
        var map = {
          invited: 'chat.status.invited',
          inviting: 'chat.status.inviting',
          active: 'chat.status.connected',
          connected: 'chat.status.connected',
          pending: 'chat.status.pending',
          declined: 'chat.status.declined',
          declined_remote: 'chat.status.declined',
          closed: 'chat.status.offline',
          offline: 'chat.status.offline',
        };
        return this.t(map[st] || 'chat.status.pending');
      },

      sessionStatusClass: function (s) {
        var st = s && s.status;
        if (st === 'active' || st === 'connected') return 'chat-session-row__status--ok';
        if (st === 'invited' || st === 'inviting' || st === 'pending') return 'chat-session-row__status--warn';
        if (st === 'declined' || st === 'declined_remote') return 'chat-session-row__status--err';
        return '';
      },

      fileStatusLabel: function (entry) {
        var st = (entry && entry.status) || 'pending';
        if (st === 'done') {
          return entry.outgoing ? this.t('chat.file.sent') : this.t('chat.file.received');
        }
        if (st === 'sending' && !entry.outgoing) {
          return this.t('chat.file.receiving');
        }
        var map = {
          pending: 'chat.file.status.pending',
          await_accept: 'chat.file.status.await_accept',
          sending: 'chat.file.status.sending',
          failed: 'chat.file.status.failed',
          declined: 'chat.file.status.declined',
          cancelled: 'chat.file.status.cancelled',
        };
        return this.t(map[st] || 'chat.file.status.pending');
      },

      formatSize: function (bytes) {
        if (!bytes && bytes !== 0) return '';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
      },

      formatTime: function (ts) {
        if (!ts) return '';
        var d = new Date(ts * 1000);
        var h = String(d.getHours()).padStart(2, '0');
        var m = String(d.getMinutes()).padStart(2, '0');
        return h + ':' + m;
      },

      _scrollToBottom: function () {
        var self = this;
        this.$nextTick(function () {
          var el = self.$refs.msgList;
          if (el) el.scrollTop = el.scrollHeight;
        });
      },
    },
  };

})();
