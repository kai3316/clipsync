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
      // LAN rows a conversation can actually be STARTED with: connected, or
      // visible on this LAN right now (the invite connects first), or
      // reachable over the relay.  A paired-but-offline device is dropped —
      // /api/chat/devices keeps returning it (the desktop view wants it), but
      // offering "start chat" on it could only ever fail with a timeout.
      lanTargets: function () {
        var rows = this.chatDevices || [];
        var out = [];
        for (var i = 0; i < rows.length; i++) {
          var d = rows[i];
          if (!d) continue;
          // A host that predates the flags sends none of them — keep such a
          // row rather than rendering an empty picker (fail open).
          if (d.connected === undefined && d.discovered === undefined
              && d.relay_reachable === undefined) {
            out.push(d);
            continue;
          }
          if (d.connected || d.discovered || d.relay_reachable) out.push(d);
        }
        return out;
      },

      // LAN chat targets, each augmented with `internet` when the SAME peer
      // is also internet-paired — a dual-online device appears once (under
      // LAN) with a 🌐 badge instead of twice.  The name prefers the internet
      // alias (alias||name) so the target label matches the Devices page.
      deviceList: function () {
        var lan = this.lanTargets;
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

      // Internet-ONLY chat targets: internet-paired peers that are NOT also a
      // LAN chat target (deduped against deviceList, i.e. against the FILTERED
      // LAN list — so an internet-paired peer that went LAN-offline moves into
      // this group instead of vanishing from both).  Rendered under the
      // "Internet devices" group.  A peer whose `paired` flag is explicitly
      // false is a stale/in-progress row, not a chat target.
      internetDeviceList: function () {
        var lan = this.lanTargets;
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

      // The open session's peer is an internet-ONLY (netpair) peer — file
      // bytes ride the public relay in smaller chunks, so sending works but
      // is capped at RELAY_FILE_CAP (5 MB).  The attach button stays enabled;
      // this flag only selects the hint title and the client-side size gate.
      activePeerIsInternetOnly: function () {
        var s = this.activeSession;
        if (!s || s.peer_id === undefined || s.peer_id === null) return false;
        var net = this.internetDeviceList;
        for (var i = 0; i < net.length; i++) {
          if (String(net[i].peer_id) === String(s.peer_id)) return true;
        }
        return false;
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
      // Auto-scroll when new entries land in the open conversation, but ONLY
      // when the user is still near the bottom — an up-scrolled reader keeps
      // their place instead of being yanked to the newest message.
      'store.chatMessages': function () {
        var el = this.$refs.msgList;
        if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 80) {
          this._scrollToBottom();
        }
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

    beforeUnmount: function () {
      // The peer-typing deadline is a 4.5s timer that writes this.
      // peerTypingLocal when it fires.  Switching tabs unmounts the panel
      // while it is still armed, so it fired against a dead component
      // instance — a Vue warning in the console, and on a fast tab-flip the
      // stale timer from the previous mount could clear the indicator the
      // new mount had just set.  Report typing=false on the way out too, so
      // the peer doesn't see us "typing…" forever after we leave the tab.
      if (this._peerTypingTimer) {
        clearTimeout(this._peerTypingTimer);
        this._peerTypingTimer = null;
      }
      if (this._typingLastState) {
        // Bypass the 2s throttle: this is our last chance to say it, and a
        // suppressed "stopped typing" leaves the peer staring at a typing
        // indicator that only expires on the server's own deadline.
        this._typingLastSent = 0;
        try {
          this.sendTypingState(false);
        } catch (e) {
          /* best effort — we are going away regardless */
        }
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
                ' :aria-label="isMuted(s) ? t(\'chat.unmute\') : t(\'chat.mute\')" :aria-pressed="isMuted(s)"' +
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
                  '<div v-if="m.kind === \'system\'" class="chat-system selectable">{{ systemText(m) }}</div>' +

                  '<div v-else-if="m.kind === \'text\'" class="chat-bubble"' +
                    ' :class="[m.outgoing ? \'chat-bubble--out\' : \'chat-bubble--in\',' +
                    ' (m.outgoing && m.status === \'failed\') ? \'chat-bubble--failed\' : \'\']"' +
                    ' @contextmenu.prevent="openMsgMenu(m, $event)">' +
                    '<div class="chat-bubble__text selectable">{{ m.text }}</div>' +
                    '<div class="chat-bubble__meta">' +
                      '<template v-if="m.outgoing && m.status === \'failed\'">' +
                        '<span class="chat-bubble__failed">{{ t(\'chat.text_failed\') }}</span>' +
                        '<button class="chat-bubble__retry" :title="t(\'chat.resend\')"' +
                          ' :disabled="resendBusy === m.entry_id" @click="resendText(m)">⟳</button>' +
                      '</template>' +
                      '<span v-if="chatDeliveryStatus(m)" class="chat-bubble__delivery" :class="chatDeliveryClass(m)" :title="t(\'delivery.offline_retry_hint\')">' +
                        '{{ chatDeliveryIcon(m) }} {{ t(chatDeliveryKey(m)) }}' +
                      '</span>' +
                      '{{ formatTime(m.ts) }}' +
                    '</div>' +
                  '</div>' +

                  '<div v-else class="chat-file"' +
                    ' :class="m.outgoing ? \'chat-file--out\' : \'chat-file--in\'"' +
                    ' @contextmenu.prevent="openMsgMenu(m, $event)">' +
                    '<div class="chat-file__icon">📄</div>' +
                    '<div class="chat-file__body">' +
                      '<div class="chat-file__name selectable">{{ m.file_name }}</div>' +
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
                      '<button v-if="m.status === \'done\' && m.saved_path"' +
                        ' class="chat-action-btn" :title="t(\'transfer.open\')" @click="openFile(m.saved_path)">{{ t(\'transfer.open\') }}</button>' +
                      '<button v-if="m.status === \'done\' && m.saved_path"' +
                        ' class="chat-action-btn" :title="t(\'transfer.open_folder\')" @click="revealFile(m.saved_path)">{{ t(\'transfer.open_folder\') }}</button>' +
                    '</div>' +
                  '</div>' +
                '</div>' +
              '</template>' +
            '</div>' +

            '<div v-if="showPeerTyping" class="chat-typing" style="padding:2px 12px 6px;font-size:12px;font-style:italic;display:flex;align-items:center;gap:4px;color:var(--clipsync-fg-muted)">{{ t(\'chat.typing\') }}' +
              '<span class="d">●</span><span class="d">●</span><span class="d">●</span>' +
            '</div>' +

            '<div class="chat-composer">' +
              '<button class="chat-composer__attach" :title="activePeerIsInternetOnly ? t(\'chat.attach_internet\') : t(\'chat.attach\')" :disabled="sendingFile" @click="pickFile">' +
                '{{ sendingFile ? \'...\' : \'📎\' }}' +
              '</button>' +
              '<input ref="composerInput" class="chat-composer__input" type="text" v-model="composing"' +
                ' :placeholder="t(\'chat.input_placeholder\')" :disabled="sendingText"' +
                ' maxlength="4000" @input="onComposerInput" @keyup.enter="onComposerEnter">' +
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
        // On the mobile stacked layout the conversation sits below the whole
        // device+session list — scroll the panel down so it comes into view.
        var self = this;
        this.$nextTick(function () {
          if (window.innerWidth <= 768) {
            var panel = self.$el;
            if (panel && panel.scrollTo) {
              panel.scrollTo({ top: panel.scrollHeight, behavior: 'smooth' });
            } else if (panel) {
              panel.scrollTop = panel.scrollHeight;
            }
          }
        });
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

      // Right-click a chat message (text bubble or file card): open the shared
      // context menu in chat-message mode (copy text / file name).
      openMsgMenu: function (m, e) {
        if (!m) return;
        this.store.contextMenu = {
          visible: true,
          x: e.clientX,
          y: e.clientY,
          mode: 'chat-message',
          target: m,
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
            // Give the (now re-enabled) composer its focus back.
            self.$nextTick(function () {
              var inp = self.$refs.composerInput;
              if (inp) inp.focus();
            });
          });
      },

      /* ── Typing indicator ────────────────────────────────────── */

      // Composer input event → throttled typing report.  An emptied box
      // reports "stopped" immediately (state changes bypass the throttle).
      onComposerInput: function () {
        this.sendTypingState(!!(this.composing || '').trim());
      },

      // Enter-to-send, but never during IME composition: keyup fires once the
      // composition ends, and the isComposing/229 guard drops any lingering
      // intermediate keyups so a Chinese user's half-typed phrase isn't sent.
      onComposerEnter: function (e) {
        if (e && (e.isComposing || e.keyCode === 229)) return;
        this.sendText();
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
        // Internet-only peers ship bytes through the public relay, which caps
        // the file at 5 MB.  Reject client-side so the user hears the reason
        // immediately instead of waiting for an upload + server round-trip.
        if (this.activePeerIsInternetOnly && file.size > 5 * 1024 * 1024) {
          this.store.showToast(this.t('chat.err_internet_file_cap'), 3000);
          e.target.value = '';
          return;
        }
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
              // The server only answers this specific code when the peer is
              // internet-only and the file exceeds the relay cap (a race the
              // client-side gate above can miss when the peer was offline).
              if (res.error === 'internet_file_cap') {
                self.store.showToast(self.t('chat.err_internet_file_cap'), 3000);
              } else {
                self.store.showToast(self.t('chat.err_send_failed'), 2500);
              }
              return;
            }
            self.loadMessages();
          })
          .catch(function (err) {
            console.error('[ClipSync] Failed to send chat file:', err);
            self.store.showToast(self.t('chat.err_send_failed'), 2500);
            // Reset the picker so the SAME file can be re-selected after an
            // error — otherwise it's only cleared on the success path and the
            // next pick of the identical file would be a no-op.
            e.target.value = '';
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
        // Mirror ClipsyncAPI.uploadFile's 15s stall guard: a hung upload must
        // not leave sendingFile=true forever (the attach button stays
        // disabled) with no way to retry the same file.
        var options = { method: 'POST', body: formData };
        var controller = null;
        var timeoutId = null;
        if (typeof AbortController !== 'undefined') {
          controller = new AbortController();
          options.signal = controller.signal;
          timeoutId = setTimeout(function () {
            controller.abort();
          }, 15000);
        }
        return fetch(url, options)
          .then(function (r) {
            clearTimeout(timeoutId);
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
          })
          .catch(function (e) {
            clearTimeout(timeoutId);
            throw e;
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

      openFile: function (path) {
        var self = this;
        if (!path) return;
        ClipsyncAPI.openFile(path).catch(function () {
          self.store.showToast(self.t('ui.open_failed_title'), 2000);
        });
      },

      revealFile: function (path) {
        var self = this;
        if (!path) return;
        ClipsyncAPI.revealFile(path).catch(function () {
          self.store.showToast(self.t('ui.open_failed_title'), 2000);
        });
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
        return ClipsyncFormat.size(bytes);
      },

      formatTime: function (ts) {
        if (!ts) return '';
        var d = new Date(ts * 1000);
        var hh = String(d.getHours()).padStart(2, '0');
        var mm = String(d.getMinutes()).padStart(2, '0');
        var hhmm = hh + ':' + mm;
        var now = new Date();
        var sameDay = function (a, b) {
          return a.getFullYear() === b.getFullYear() &&
            a.getMonth() === b.getMonth() &&
            a.getDate() === b.getDate();
        };
        // Today → just the clock time; yesterday → locale-aware label; any
        // older timestamp gets a short MM-DD prefix so it isn't mistaken for
        // today.
        if (sameDay(d, now)) return hhmm;
        var yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
        if (sameDay(d, yesterday)) {
          return this.t('history.yesterday', { time: hhmm });
        }
        var mo = String(d.getMonth() + 1).padStart(2, '0');
        var dd = String(d.getDate()).padStart(2, '0');
        return mo + '-' + dd + ' ' + hhmm;
      },

      /* ── Internet relay delivery stamp (round 17) ──────────────────
         A tiny ✓已送达 / ✗未送达 / …发送中 pill on an OUTGOING relay-chat
         text bubble, bottom-right. The bubble carries `msg_id` on newer
         hosts; when it matches an `internet_delivery` WS event the store's
         msg_id → status map stamps it. Defensive: entries without msg_id
         (older hosts) or with an id the store never saw get no stamp, and a
         chat-level "failed" bubble keeps its failed label instead (a message
         that never left is not "sending"). */

      chatDeliveryStatus: function (m) {
        if (!m || !m.outgoing || m.kind !== 'text') return null;
        if (m.status === 'failed') return null;
        if (m.msg_id === undefined || m.msg_id === null || m.msg_id === '') return null;
        var st = this.store.internetDeliveryMsgs[String(m.msg_id)];
        return (['sent', 'delivered', 'failed', 'queued'].indexOf(st) !== -1) ? st : null;
      },

      chatDeliveryClass: function (m) {
        switch (this.chatDeliveryStatus(m)) {
          case 'delivered': return 'chat-bubble__delivery--ok';
          case 'failed': return 'chat-bubble__delivery--err';
          case 'queued': return 'chat-bubble__delivery--queued';
          default: return 'chat-bubble__delivery--sending';
        }
      },

      chatDeliveryIcon: function (m) {
        switch (this.chatDeliveryStatus(m)) {
          case 'delivered': return '✓';
          case 'failed': return '✗';
          default: return '…';
        }
      },

      chatDeliveryKey: function (m) {
        var st = this.chatDeliveryStatus(m) || 'sent';
        return 'delivery.' + (st === 'failed' ? 'not_delivered'
          : st === 'sent' ? 'sending' : st);
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
