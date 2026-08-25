/* ═══════════════════════════════════════════════════════════════════
   ClipSync Nearby Chat Panel Component
   Two-column layout: left = chat-able devices + session list; right =
   the open conversation (text bubbles, system entries, file cards, an
   invite banner, and a text/file composer).

   Wire contract (see README / backend api/chat.py):
     WS: chat_sessions, chat_message, chat_progress, chat_file_done
         (session snapshots carry peer_typing — the peer's typing indicator)
     GET  /api/chat/devices|sessions|messages|download
     POST /api/chat/invite|text|typing|resend|file|file/{accept,decline,cancel}|
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
        resendBusy: '',        // entry_id of an in-flight text resend
        peerTypingLocal: false, // locally-armed display of the peer's typing flag
      };
    },

    computed: {
      // LAN chat targets, each augmented with `internet` when the SAME peer
      // is also internet-paired — a dual-online device appears once (under
      // LAN) with a 🌐 badge instead of twice.  The name prefers the internet
      // alias (alias||name) so the target label matches the Devices page.
      deviceList: function () {
        var lan = this.chatDevices || [];
        var net = this.store.internetPairPeers || [];
        var out = [];
        for (var i = 0; i < lan.length; i++) {
          var d = lan[i];
          var netPeer = null;
          for (var j = 0; j < net.length; j++) {
            if (String(net[j].peer_id) === String(d.peer_id)) {
              netPeer = net[j];
              break;
            }
          }
          out.push(Object.assign({}, d, netPeer ? {
            internet: true,
            internetOnline: !!netPeer.online,
            name: netPeer.alias || d.name,
          } : {}));
        }
        return out;
      },

      // Internet-ONLY chat targets: internet-paired peers that are NOT also
      // listed by /api/chat/devices (deduped against deviceList).  Rendered
      // under the "Internet devices" group.  A peer whose `paired` flag is
      // explicitly false is a stale/in-progress row, not a chat target.
      internetDeviceList: function () {
        var lan = this.chatDevices || [];
        var lanIds = {};
        for (var i = 0; i < lan.length; i++) lanIds[String(lan[i].peer_id)] = true;
        var out = [];
        var net = this.store.internetPairPeers || [];
        for (var j = 0; j < net.length; j++) {
          var p = net[j];
          if (!p || p.peer_id === undefined || p.peer_id === null) continue;
          if (p.paired === false) continue;
          if (lanIds[String(p.peer_id)]) continue;
          out.push({
            peer_id: p.peer_id,
            name: p.alias || p.name || p.peer_id,
            paired: true,
            online: !!p.online,
            internet: true,
            isInternetOnly: true,
          });
        }
        return out;
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

      // "typing…" indicator: the server flag lazily expires after ~4s, but a
      // silent stop produces NO further push — this local deadline (armed by
      // the watcher) hides the row even when no new snapshot arrives.
      showPeerTyping: function () {
        var s = this.activeSession;
        return !!s && s.status === 'active' && this.peerTypingLocal;
      },

      // Most-recently-active first.  Closed conversations are hidden — a
      // closed session is a finished one (right-click "close/delete"), and the
      // backend may still echo it as "closed" on the next snapshot.
      sortedSessions: function () {
        var list = this.store.chatSessions.filter(function (s) {
          return s && s.status !== 'closed' && s.status !== 'declined' &&
            s.status !== 'declined_remote';
        });
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
      // mark it read.  Also tracks the peer's typing flag from snapshots.
      'store.chatSessions': function (sessions) {
        var self = this;
        if (!sessions || !this.store.activeChatSession) {
          this._armPeerTyping(null);
          return;
        }
        var active = sessions.find(function (s) {
          return s.session_id === self.store.activeChatSession;
        });
        if (active && (active.unread || 0) > 0) {
          this.loadMessages();
          this.markRead();
        }
        this._armPeerTyping(active || null);
      },
    },

    created: function () {
      // Non-reactive typing bookkeeping (kept off data() so Vue doesn't
      // deep-observe them): last state/timestamp WE reported, and the local
      // display deadline timer for the peer's indicator.
      this._typingLastState = null;
      this._typingLastSent = 0;
      this._peerTypingTimer = null;
      this._injectTypingStyle();
      this.loadDevices();
      this.loadSessions();
      // Internet-paired peers live in the store (kept live by WS + the
      // Devices page), but this panel may mount before Devices is ever
      // opened — fetch the pairing status once so the "Internet devices"
      // group is populated.  Fully defensive: older backend → empty list.
      if (this.store.fetchInternetPairStatus) {
        this.store.fetchInternetPairStatus();
      }
      // A session may already be open (e.g. restored by the app entry).
      if (this.store.activeChatSession) {
        this.loadMessages();
        this.markRead();
        this._armPeerTyping(this.activeSession);
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
          '<template v-else>' +
            '<div v-if="deviceList.length === 0 && internetDeviceList.length === 0" class="chat-side-empty">' +
              '{{ t(\'chat.empty_no_devices\') }}' +
            '</div>' +
            '<div v-else class="chat-device-list">' +
              '<!-- LAN targets (a dual-online device appears here with a 🌐 badge, never twice) -->' +
              '<div v-for="d in deviceList" :key="d.peer_id" class="chat-device-row">' +
                '<div class="chat-device-row__info">' +
                  '<div class="chat-device-row__top">' +
                    '<span class="chat-device-row__name">{{ d.name || d.peer_id }}</span>' +
                    '<span v-if="d.internet" class="badge chat-badge--internet" :title="t(\'devices.netpair_also_internet\')">🌐 {{ d.internetOnline ? t(\'devices.netpair_online\') : t(\'devices.netpair_offline\') }}</span>' +
                  '</div>' +
                  '<span class="badge" :class="d.paired ? \'chat-badge--paired\' : \'chat-badge--unpaired\'">' +
                    '{{ d.paired ? t(\'chat.paired_tag\') : t(\'chat.unpaired_tag\') }}' +
                  '</span>' +
                '</div>' +
                '<button class="chat-mini-btn chat-mini-btn--accent" :disabled="inviteBusy === d.peer_id" @click="startChat(d)">' +
                  '{{ inviteBusy === d.peer_id ? \'...\' : t(\'chat.start\') }}' +
                '</button>' +
              '</div>' +
              '<!-- Internet-only targets -->' +
              '<template v-if="internetDeviceList.length > 0">' +
                '<div class="chat-subheader">🌐 {{ t(\'chat.internet_devices_header\') }}</div>' +
                '<div v-for="p in internetDeviceList" :key="p.peer_id" class="chat-device-row">' +
                  '<div class="chat-device-row__info">' +
                    '<div class="chat-device-row__top">' +
                      '<span class="chat-device-row__dot" :class="p.online ? \'chat-session-row__dot--on\' : \'chat-session-row__dot--off\'"></span>' +
                      '<span class="chat-device-row__name">{{ p.name || p.peer_id }}</span>' +
                    '</div>' +
                    '<span v-if="!p.online" class="chat-device-row__hint">{{ t(\'chat.internet_offline_hint\') }}</span>' +
                  '</div>' +
                  '<button class="chat-mini-btn chat-mini-btn--accent" :disabled="inviteBusy === p.peer_id" @click="startChat(p)">' +
                    '{{ inviteBusy === p.peer_id ? \'...\' : t(\'chat.start\') }}' +
                  '</button>' +
                '</div>' +
              '</template>' +
            '</div>' +
          '</template>' +

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
              ' @click="openSession(s)" @contextmenu.prevent="openSessionMenu(s, $event)">' +
              '<span class="chat-session-row__dot"' +
                ' :class="sessionOnline(s) ? \'chat-session-row__dot--on\' : \'chat-session-row__dot--off\'"></span>' +
              '<div class="chat-session-row__body">' +
                '<div class="chat-session-row__top">' +
                  '<span class="chat-session-row__name">{{ chatPeerName(s.peer_id, s.peer_name) }}</span>' +
                  '<span v-if="internetPeerFor(s.peer_id)" class="chat-session-row__net" :title="t(\'devices.netpair_also_internet\')">🌐</span>' +
                  '<span class="chat-session-row__time">{{ formatTime(s.last_activity_ts) }}</span>' +
                '</div>' +
                '<div class="chat-session-row__bottom">' +
                  '<span class="chat-session-row__preview">{{ s.last_preview || \'\' }}</span>' +
                  '<span class="chat-session-row__status" :class="sessionStatusClass(s)">{{ sessionStatusLabel(s) }}</span>' +
                  '<span v-if="(s.unread || 0) > 0" class="chat-session-row__badge">{{ s.unread }}</span>' +
                '</div>' +
              '</div>' +
              '<button class="chat-session-row__mute" :class="{ \'chat-session-row__mute--on\': isMuted(s) }"' +
                ' :title="isMuted(s) ? t(\'chat.unmute\') : t(\'chat.mute\')"' +
                ' @click.stop="toggleMute(s)">{{ isMuted(s) ? \'🔕\' : \'🔔\' }}</button>' +
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
              '<div class="chat-invite-banner__peer">{{ chatPeerName(activeSession.peer_id, activeSession.peer_name) }}</div>' +
              '<div class="chat-invite-banner__fp">{{ t(\'chat.invite_fingerprint\') }}: {{ activeSession.fingerprint_short }}</div>' +
              '<div class="chat-invite-banner__actions">' +
                '<button class="chat-action-btn chat-action-btn--accept" :disabled="!!inviteBusy" @click="respondInvite(\'accept\')">{{ t(\'chat.accept\') }}</button>' +
                '<button class="chat-action-btn chat-action-btn--decline" :disabled="!!inviteBusy" @click="respondInvite(\'decline\')">{{ t(\'chat.decline\') }}</button>' +
              '</div>' +
            '</div>' +

            '<div class="chat-conv-header">' +
              '<span class="chat-conv-header__name">{{ chatPeerName(activeSession.peer_id, activeSession.peer_name) }}</span>' +
              '<span class="chat-conv-header__status">{{ sessionStatusLabel(activeSession) }}</span>' +
              '<button class="chat-mini-btn chat-mini-btn--danger" @click="closeSession">{{ t(\'chat.close\') }}</button>' +
            '</div>' +

            '<div ref="msgList" class="chat-conversation">' +
              '<div v-if="messagesLoading && store.chatMessages.length === 0" class="chat-conv-loading">{{ t(\'ui.loading\') }}</div>' +
              '<div v-else-if="store.chatMessages.length === 0" class="chat-conv-empty">{{ t(\'chat.invite_greeting\') }}</div>' +
              '<template v-else>' +
                '<div v-for="m in store.chatMessages" :key="m.entry_id" class="chat-msg-row">' +
                  '<div v-if="m.kind === \'system\'" class="chat-system">{{ systemText(m) }}</div>' +

                  '<div v-else-if="m.kind === \'text\'" class="chat-bubble"' +
                    ' :class="[m.outgoing ? \'chat-bubble--out\' : \'chat-bubble--in\',' +
                    ' (m.outgoing && m.status === \'failed\') ? \'chat-bubble--failed\' : \'\']">' +
                    '<div class="chat-bubble__text">{{ m.text }}</div>' +
                    '<div class="chat-bubble__meta">' +
                      '<template v-if="m.outgoing && m.status === \'failed\'">' +
                        '<span class="chat-bubble__failed">{{ t(\'chat.text_failed\') }}</span>' +
                        '<button class="chat-bubble__retry" :title="t(\'chat.resend\')"' +
                          ' :disabled="resendBusy === m.entry_id" @click="resendText(m)">⟳</button>' +
                      '</template>' +
                      '{{ formatTime(m.ts) }}' +
                    '</div>' +
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

            '<div v-if="showPeerTyping" class="chat-typing">{{ t(\'chat.typing\') }}' +
              '<span class="d">●</span><span class="d">●</span><span class="d">●</span>' +
            '</div>' +

            '<div class="chat-composer">' +
              '<button class="chat-composer__attach" :title="t(\'chat.attach\')" :disabled="sendingFile" @click="pickFile">' +
                '{{ sendingFile ? \'...\' : \'📎\' }}' +
              '</button>' +
              '<input class="chat-composer__input" type="text" v-model="composing"' +
                ' :placeholder="t(\'chat.input_placeholder\')" :disabled="sendingText"' +
                ' maxlength="4000" @input="onComposerInput" @keyup.enter="sendText">' +
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
            // The response carries the authoritative per-peer mute set (the
            // backend also suppresses the desktop notification for it), so
            // the bell state follows the backend, not just this tab's cache.
            if (res && Array.isArray(res.muted)) {
              self.store.replaceChatMuted(res.muted);
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
            // The user may have switched sessions while the request was in
            // flight — never clobber the now-open conversation with a stale
            // snapshot for the previously-active one.
            if (sid !== self.store.activeChatSession) return;
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
        // Reset the typing bookkeeping so a report for the PREVIOUS chat is
        // never confused with this one.
        this._typingLastState = null;
        this._typingLastSent = 0;
        this.store.activeChatSession = session.session_id;
        this.loadMessages();
        this.markRead();
        this._armPeerTyping(session);
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

      // Whether a session's peer is muted (no unread badge / notifications).
      isMuted: function (session) {
        return !!(session && this.store.isChatMuted(session.peer_id));
      },

      // Toggle mute for a session's peer from the row bell, with a toast.
      toggleMute: function (session) {
        if (!session || !session.peer_id) return;
        var muted = this.store.toggleChatMute(session.peer_id);
        this.store.showToast(
          muted ? this.t('chat.muted_peer') : this.t('chat.unmuted_peer'),
          1500
        );
      },

      // Right-click a session row: open the shared context menu in
      // chat-session mode (mute / mark-read / close-delete).
      openSessionMenu: function (session, e) {
        if (!session) return;
        this.store.contextMenu = {
          visible: true,
          x: e.clientX,
          y: e.clientY,
          mode: 'chat-session',
          target: session,
          opener: e.currentTarget || e.target,
        };
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
              // chat_sessions push. Surface a short "connecting" notice
              // (internet-specific for peers started from the 🌐 group).
              self.store.showToast(
                device.internet
                  ? self.t('chat.internet_connecting')
                  : self.t('chat.connecting'),
                2000);
            } else if (res && res.ok === false) {
              // Refused invite (rate limit / slots full / peer unreachable) —
              // NOT a connecting wait; name the failure so the user can retry
              // meaningfully instead of staring at a phantom "Connecting…".
              self.store.showToast(self.t('chat.err_invite_failed'), 2500);
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
          .then(function (res) {
            if (!res || res.ok === false) {
              self.store.showToast(self.t('chat.err_send_failed'), 2500);
              return;
            }
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
            self.store.showToast(self.t('chat.err_send_failed'), 2500);
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
          .then(function (res) {
            if (!res || res.ok === false) {
              self.store.showToast(self.t('chat.err_send_failed'), 2500);
              return;
            }
            self.store.activeChatSession = '';
            self.store.chatMessages.splice(0, self.store.chatMessages.length);
            self.loadSessions();
          })
          .catch(function (e) {
            // Keep the conversation open and say why — a silent failure would
            // leave the user staring at a session that never goes away.
            console.error('[ClipSync] Failed to close session:', e);
            self.store.showToast(self.t('chat.err_send_failed'), 2500);
          });
      },

      /* ── Composer / text ─────────────────────────────────────── */

      sendText: function () {
        var self = this;
        // Double-submit guard: two rapid Enters must not POST twice before
        // the first response clears the draft.
        if (this.sendingText) return;
        var text = (this.composing || '').trim();
        if (!text || !this.store.activeChatSession) return;
        this.sendingText = true;
        ClipsyncAPI.chatSendText(this.store.activeChatSession, text)
          .then(function (res) {
            if (res && res.ok === false) {
              // Session inactive / offline / flood control — the text was not
              // sent; keep the draft and say why instead of clearing it.
              self.store.showToast(self.t('chat.err_send_failed'), 2500);
              return;
            }
            self.composing = '';
            // The message went out — the receiver clears our indicator when
            // it lands, so just restart OUR bookkeeping from "not typing".
            self._typingLastState = false;
            self._typingLastSent = Date.now();
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

      /* ── Typing indicator ────────────────────────────────────── */

      // Composer input event → throttled typing report.  An emptied box
      // reports "stopped" immediately (state changes bypass the throttle).
      onComposerInput: function () {
        this.sendTypingState(!!(this.composing || '').trim());
      },

      sendTypingState: function (typing) {
        var sid = this.store.activeChatSession;
        if (!sid) return;
        var now = Date.now();
        if (typing === this._typingLastState &&
            (now - this._typingLastSent) < 2000) return;
        this._typingLastState = typing;
        this._typingLastSent = now;
        // Fire-and-forget: {ok:false} also covers the server-side throttle
        // window, so it is never a user-visible error.  Direct fetch because
        // js/api.js has no typed wrapper for this endpoint yet.
        var base = ((this.store && this.store.serverUrl) || '').replace(/\/+$/, '');
        fetch(base + '/api/chat/typing?token=' +
            encodeURIComponent((this.store && this.store.token) || ''), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sid, typing: !!typing }),
        }).catch(function (e) {
          console.debug('[ClipSync] typing report failed:', e);
        });
      },

      // Arm/disarm the local display of the peer's flag from a snapshot.
      // The server deadline (~4s) plus a small grace covers push latency.
      _armPeerTyping: function (session) {
        var self = this;
        if (this._peerTypingTimer) {
          clearTimeout(this._peerTypingTimer);
          this._peerTypingTimer = null;
        }
        if (session && session.status === 'active' && session.peer_typing) {
          this.peerTypingLocal = true;
          this._peerTypingTimer = setTimeout(function () {
            self.peerTypingLocal = false;
            self._peerTypingTimer = null;
          }, 4500);
        } else {
          this.peerTypingLocal = false;
        }
      },

      // One-time stylesheet for the indicator row (index.html's CSS is not
      // editable from this component).
      _injectTypingStyle: function () {
        if (document.getElementById('cs-chat-typing-style')) return;
        var el = document.createElement('style');
        el.id = 'cs-chat-typing-style';
        el.textContent =
          '.chat-typing{padding:2px 12px 6px;font-size:12px;color:#8a8f98;' +
          'font-style:italic;display:flex;align-items:center;gap:4px}' +
          '.chat-typing .d{display:inline-block;font-size:9px;line-height:1;' +
          'animation:cs-typing-b 1.2s infinite}' +
          '.chat-typing .d:nth-child(2){animation-delay:.2s}' +
          '.chat-typing .d:nth-child(3){animation-delay:.4s}' +
          '@keyframes cs-typing-b{0%,60%,100%{opacity:.25;transform:translateY(0)}' +
          '30%{opacity:1;transform:translateY(-2px)}}';
        document.head.appendChild(el);
      },

      /* ── Failed-text resend ──────────────────────────────────── */

      // ⟳ on a failed outgoing bubble: re-transmit that exact entry.  The
      // backend refuses non-failed entries, so a double-click or a stale
      // bubble simply comes back {ok:false} without side effects.
      resendText: function (entry) {
        var self = this;
        var sid = this.store.activeChatSession;
        if (!sid || !entry || !entry.entry_id) return;
        if (this.resendBusy === entry.entry_id) return;
        this.resendBusy = entry.entry_id;
        ClipsyncAPI.chatResendText(sid, entry.entry_id)
          .then(function (res) {
            if (!res || res.ok === false) {
              self.store.showToast(self.t('chat.err_resend_failed'), 2500);
            }
            // Refetch either way: success flips the bubble to delivered,
            // failure keeps it failed (the chat_message push is deduped by
            // entry_id in ws.js, so the refetch is what repaints the state).
            self.loadMessages();
          })
          .catch(function (e) {
            console.error('[ClipSync] Failed to resend chat text:', e);
            self.store.showToast(self.t('chat.err_resend_failed'), 2500);
          })
          .finally(function () {
            self.resendBusy = '';
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
        // purpose=chat lands the file in a temp dir on the server (never the
        // received-files dir) and skips the receive notification/sound/Files
        // record; the response carries the temp path to feed chatSendFile.
        this._uploadForChat(file)
          .then(function (res) {
            var filePath = (res && (res.path || res.filepath || res.name)) || '';
            if (!filePath) {
              throw new Error('No server path returned from upload');
            }
            return ClipsyncAPI.chatSendFile(sid, filePath);
          })
          .then(function (res) {
            e.target.value = '';
            if (res && res.ok === false) {
              self.store.showToast(self.t('chat.err_send_failed'), 2500);
              return;
            }
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

      // Upload a file strictly for chat sending. Mirrors ClipsyncAPI.uploadFile
      // but tags the request purpose=chat so the server can tell a chat temp
      // upload (no receive notification / sound / Files record) apart from a
      // normal phone upload. The server answers with the absolute temp path.
      _uploadForChat: function (file) {
        var base = (this.store && this.store.serverUrl) || '';
        if (base) base = base.replace(/\/+$/, '');
        var sep = '/api/upload'.indexOf('?') !== -1 ? '&' : '?';
        var url = base + '/api/upload' + sep +
          'token=' + encodeURIComponent((this.store && this.store.token) || '') +
          '&purpose=chat';
        var formData = new FormData();
        formData.append('file', file);
        return fetch(url, { method: 'POST', body: formData })
          .then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
          });
      },

      fileAction: function (entry, action) {
        var self = this;
        var sid = this.store.activeChatSession;
        if (!sid || !entry || !entry.transfer_id) return;
        this.fileBusy = entry.transfer_id;
        ClipsyncAPI.chatFileAction(sid, entry.transfer_id, action)
          .then(function (res) {
            if (res && res.ok === false) {
              // The backend reports {error:"expired"} when the offer was swept
              // by the stale-receive reaper while its Accept button was still
              // shown — say so instead of a misleading "send failed".  The
              // card already flipped to "declined" via the WS broadcast.
              if (res.error === 'expired') {
                self.store.showToast(self.t('pairing.state.expired'), 2500);
              } else {
                self.store.showToast(self.t('chat.err_send_failed'), 2500);
              }
              return;
            }
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

      // The internet-pair peer whose id matches *peerId* (null when the peer
      // is not internet-paired).  Mirrors device-panel's netpairPeerFor so
      // both surfaces agree on the merged identity of a dual-online device.
      internetPeerFor: function (peerId) {
        var peers = this.store.internetPairPeers || [];
        for (var i = 0; i < peers.length; i++) {
          if (String(peers[i].peer_id) === String(peerId)) return peers[i];
        }
        return null;
      },

      // Session/device display name: an internet alias wins, then the
      // reported name, then the peer id.  Keeps every chat surface (target
      // row, session row, conversation header, invite banner) on the same
      // "alias || name || id" label the Devices page uses.
      chatPeerName: function (peerId, fallback) {
        var net = this.internetPeerFor(peerId);
        if (net) return net.alias || net.name || fallback || peerId;
        return fallback || peerId;
      },

      // A session's effective online flag.  An internet-paired session reads
      // its live relay state from internetPairPeers (kept live by WS) so the
      // dot is consistent with the Devices page; the peer is reachable when
      // EITHER the LAN channel or the relay channel is up.
      sessionOnline: function (s) {
        var lanOn = !!(s && s.online);
        var netOn = false;
        if (s && s.peer_id) {
          var net = this.internetPeerFor(s.peer_id);
          if (net) netOn = !!net.online;
        }
        return lanOn || netOn;
      },

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
          closed: 'chat.status.closed',
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
          rejected: 'chat.file.status.declined',
          cancelled: 'chat.file.status.cancelled',
          cancelled_by_peer: 'chat.file.status.cancelled',
          peer_offline: 'chat.file.status.failed',
          error_timeout: 'chat.file.status.failed',
          error_size_mismatch: 'chat.file.status.failed',
          error_security: 'chat.file.status.failed',
          error_disk: 'chat.file.status.failed',
        };
        return this.t(map[st] || 'chat.file.status.pending');
      },

      systemText: function (entry) {
        // The service emits peer_offline / session_closed entries with an
        // EMPTY fmt, but their templates use {name} — substitute the active
        // conversation's peer name so users don't see a literal "{name}".
        var fmt = (entry && entry.fmt) || {};
        if (!fmt.name) {
          var sess = this.activeSession;
          if (sess && sess.peer_name) {
            fmt = Object.assign({}, fmt,
              { name: this.chatPeerName(sess.peer_id, sess.peer_name) });
          }
        }
        return this.t((entry && entry.text_key) || (entry && entry.text) || '', fmt);
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
