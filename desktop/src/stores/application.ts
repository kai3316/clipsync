import { reactive } from "vue";
import { bridge } from "../api/bridge";
import { t } from "../i18n";
import type { AppStatus, BridgeError, Device, DiagnosticAction, HistoryItem, RecoveryResult, SidecarEvent, UpdateCheckResult, UpdateState } from "../api/types";
import { createFavoritesStore } from "./favorites";
import { createDeliveryStore } from "./delivery";

export function createApplicationStore() {
  const state = reactive({
    status: null as AppStatus | null,
    history: [] as HistoryItem[],
    selectedIds: [] as string[],
    devices: [] as Device[],
    query: "",
    // The history toolbar's filter, kept in the store beside the query because
    // it belongs to the same request: every paging move refetches, and the
    // filter has to travel with it or page two would silently be unfiltered.
    kind: "all",
    sort: "newest",
    counts: {} as Record<string, number>,
    hasHistory: false,
    offset: 0,
    total: 0,
    limit: 30,
    loading: false,
    pending: false,
    refreshing: false,
    copiedId: null as string | null,
    error: null as BridgeError | null,
    notices: [] as Array<{ id: number; title: string; message: string }>,
    aiInventoryEvent: null as { revision: number; event: SidecarEvent } | null,
    // One pulled AI-config file landing (or failing to).  The pull itself
    // answers with how many requests the peer accepted; the files arrive
    // afterwards, one event each, so this is the only way a page can say how a
    // pull is going rather than that it was sent.
    aiFileEvent: null as { revision: number; data: Record<string, any> } | null,
    // A pairing code finally answering.  The handshake is two frames across two
    // machines, so the half that arrives here is the *other* machine's, minutes
    // or seconds after the code was submitted and long after the call that
    // submitted it returned.  Nothing else tells the pairing card to re-read:
    // its own pull happened before the partner had said anything.
    netpairEvent: null as { revision: number; data: Record<string, any> } | null,
    // A dialog the phone's panel asked this window to open. The request travels
    // as an event because the host has no window of its own; `revision` makes
    // two identical requests distinguishable.
    hostRequest: null as { kind: "qr" | "send_url"; revision: number } | null,
    // A device presenting a certificate that no longer matches its pin. The
    // connection was refused, so this is the only place the user learns why —
    // and the answer either re-pins the certificate or unpairs the device.
    // `can_trust` is false when the alert carried no certificate to pin.
    certAlert: null as { device_id: string; name: string; can_trust: boolean } | null,
    // Update lifecycle, mirrored from `update.state` events and hydrated from
    // `update.status`; `updateCheck` holds the last manual lookup (null = never
    // checked), which is what drives the new-version line.
    update: {
      phase: "idle",
      fraction: 0,
      downloaded: 0,
      total: 0,
      error: "",
      version: "",
      path: "",
    } as UpdateState,
    updateCheck: null as UpdateCheckResult | null,
  });
  let disposed = false;
  let started = false;
  let unlisten: (() => void) | undefined;
  let requestSequence = 0;
  let statusSequence = 0;
  let noticeSequence = 0;
  const noticeTimers = new Map<number, ReturnType<typeof setTimeout>>();
  /** A peer's name for a notice, falling back the way legacy did.
   *
   * Two of these events are published from a path that has no name handy — a
   * refused handshake carries whatever the peer announced, and "nowhere to dial"
   * carries whatever the runtime could look up — so an unnamed peer is named
   * from the snapshot, and only then by its short id.
   */
  function peerLabel(data: Record<string, unknown>): string {
    const name = String(data.name || "");
    if (name) return name;
    const id = String(data.device_id || "");
    return state.devices.find((known) => known.id === id)?.name || id.slice(0, 12);
  }
  /** The text of a notice; peer-supplied fields are inserted, never parsed. */
  function noticeMessage(name: string, data: Record<string, unknown>): string {
    const device = String(data.name || "");
    if (name === "pairing.request") {
      return t("{name} 请求配对 — 代码：{code}", { name: device, code: String(data.code || "") });
    }
    if (name === "chat.connect_timeout") {
      return t("无法连接到 {name}，设备可能已离线。", { name: device });
    }
    // The two ways a Connect click comes to nothing. The runtime publishes the
    // reason next to the bare `{accepted: false}` answer, so the click never
    // looks like a silent no-op; the wording is legacy's, advice included.
    if (name === "device.connection_rejected") {
      return t("{name} 拒绝了连接 — 该设备可能已将你移除", { name: peerLabel(data) });
    }
    if (name === "device.connection_unreachable") {
      return t(
        "找不到 {name} — 请确认该设备已开启 ClipSync 且在同一网络",
        { name: peerLabel(data) },
      );
    }
    if (name === "pairing.failed") return t("配对失败。验证码可能已过期。请重新连接。");
    // The answer to "did it connect?", arriving whenever the other machine
    // answers — which is the one moment the internet-pairing card has something
    // good to report, and the only thing that distinguishes a pairing that
    // completed from one that is still waiting.  The naming is the legacy
    // panel's toast, for the same event.
    if (name === "netpair.peer.changed") {
      return t("已与 {name} 完成互联网配对", { name: peerLabel(data) });
    }
    if (name === "device.connected") return t("{name} 已连接", { name: device });
    if (name === "device.disconnected") return t("{name} 已断开", { name: device });
    // The sidecar published this from the day the filter was wired and nothing
    // ever read it, so a clip that left the device with its sensitive values
    // replaced left silently. Legacy said so; the wording is legacy's.
    if (name === "sync.redacted") return t("敏感内容未同步");
    // The same wording the prompt used, so the notice left behind when nobody
    // answers still says what happened and what to do about it.
    if (name === "device.security_alert") {
      return t("设备“{name}”的证书已变更（可能已重装或重置）。", { name: device });
    }
    return String(data.message || data.text || data.filename || data.name || data.url || name);
  }
  function pushNotice(name: string, data: Record<string, unknown>) {
    const id = ++noticeSequence;
    state.notices.push({ id, title: name, message: noticeMessage(name, data) });
    if (state.notices.length > 5) dismissNotice(state.notices[0].id);
    noticeTimers.set(id, setTimeout(() => dismissNotice(id), 6000));
  }
  function dismissNotice(id: number) {
    clearTimeout(noticeTimers.get(id));
    noticeTimers.delete(id);
    state.notices = state.notices.filter((notice) => notice.id !== id);
  }
  /** How long the certificate prompt waits for an answer before it gives up.
   *
   * Legacy's web confirm dialog waited the same two minutes, then answered
   * "nobody decided": nothing changed on either side, and an info toast said
   * why. The prompt is still raised again by the peer's next connection.
   */
  const CERT_PROMPT_TIMEOUT = 120_000;
  let certPromptTimer: ReturnType<typeof setTimeout> | undefined;
  function clearCertPrompt() {
    clearTimeout(certPromptTimer);
    certPromptTimer = undefined;
    state.certAlert = null;
  }
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  const favorites = createFavoritesStore(() => state.status?.health === "ready" &&
    !!state.status.capabilities?.includes("favorites.list"));
  // Relay delivery receipts ride the same event stream as everything else, so
  // the mirror lives beside the store rather than in the window: a receipt has
  // to outlive the settings section it is rendered in, and the chat tab reads
  // the same map.
  const delivery = createDeliveryStore(() => state.status?.health === "ready");

  /** Error codes that mean the sidecar itself is down, not the request. */
  const SIDECAR_DOWN_CODES = ["SIDECAR_UNAVAILABLE", "SIDECAR_START_FAILED", "STARTUP_TIMEOUT"];

  function setError(error: unknown) {
    const value = typeof error === "object" && error !== null && "code" in error
      ? error as BridgeError
      : { code: "CONNECTION_FAILED", message: t("连接失败，请重试"), retryable: true };
    // A dead sidecar is recoverable now that the host can relaunch it, whatever
    // the error's own flag says — the host reports these as non-retryable.
    state.error = SIDECAR_DOWN_CODES.includes(value.code) ? { ...value, retryable: true } : value;
  }

  async function refreshHistory() {
    if (disposed) return;
    const request = ++requestSequence;
    state.loading = true;
    try {
      const page = await bridge.history(state.query, state.offset, state.limit, state.kind, state.sort);
      if (disposed || request !== requestSequence) return;
      state.history = page.items;
      const visibleIds = new Set(page.items.map((item) => item.id));
      state.selectedIds = state.selectedIds.filter((id) => visibleIds.has(id));
      state.total = page.total;
      state.counts = page.counts || {};
      state.hasHistory = !!page.has_history;
    } catch (error) {
      if (!disposed && request === requestSequence) setError(error);
    } finally {
      if (!disposed && request === requestSequence) state.loading = false;
    }
  }

  async function refresh() {
    if (disposed) return;
    const request = ++statusSequence;
    ++requestSequence;
    state.loading = false;
    state.refreshing = true;
    try {
      const status = await bridge.status();
      if (disposed || request !== statusSequence) return;
      state.status = status;
      if (status.health === "ready" && status.capabilities?.includes("favorites.list")) {
        void favorites.refresh();
      } else {
        favorites.reset();
        // Receipts came from that process; a new one has an empty ledger, and a
        // bubble stamped from the old one would keep claiming 已送达.
        delivery.reset();
      }
      if (status.health === "ready") {
        const devices = await bridge.devices();
        if (disposed || request !== statusSequence) return;
        state.devices = devices.items;
        await refreshHistory();
      } else {
        state.devices = [];
        state.history = [];
        state.selectedIds = [];
        state.total = 0;
      }
    } catch (error) {
      if (!disposed && request === statusSequence) setError(error);
    } finally {
      if (!disposed && request === statusSequence) state.refreshing = false;
    }
  }

  async function start() {
    if (started || disposed) return;
    started = true;
    try {
      const off = await bridge.subscribe(
      (event) => {
          if (disposed) return;
          const data = event.data || {};
          // Update events describe a background job, not clipboard data: apply
          // them in place and stop — a global refresh would only add latency
          // and would reset the progress the panel is rendering.
          if (event.name === "update.state") {
            const next = data.state as UpdateState | undefined;
            if (next && typeof next.phase === "string") Object.assign(state.update, next);
            return;
          }
          if (event.name === "update.available") {
            state.updateCheck = {
              available: true,
              latest: String(data.latest || ""),
              current: String(data.current || ""),
              url: String(data.url || ""),
            };
            return;
          }
          // UI requests from the phone's panel: the host owns no window, so it
          // asks this one. Nothing in the data changed — skip the refresh.
          if (event.name === "app.window_close_requested") return;
          if (event.name === "app.qr_requested" || event.name === "app.send_url_requested") {
            state.hostRequest = {
              kind: event.name === "app.qr_requested" ? "qr" : "send_url",
              revision: (state.hostRequest?.revision || 0) + 1,
            };
            return;
          }
          if (event.name === "device.security_alert") {
            // A security decision, not a toast: the last alert wins, and it
            // stands until the user answers it or the wait runs out.
            state.certAlert = {
              device_id: String(data.device_id || ""),
              name: String(data.name || ""),
              can_trust: data.can_trust === true,
            };
            clearTimeout(certPromptTimer);
            const asked = { ...data };
            certPromptTimer = setTimeout(() => {
              if (disposed || !state.certAlert) return;
              clearCertPrompt();
              pushNotice("device.security_alert", asked);
            }, CERT_PROMPT_TIMEOUT);
          }
          if ((event.name === "aiconfig.file" && data.type === "aiconfig_inventory") || event.type === "resync") {
            state.aiInventoryEvent = { revision: (state.aiInventoryEvent?.revision || 0) + 1, event };
          }
          // A file the peer sent because this window asked for it.  Kept apart
          // from the inventory above: the inventory says what the peer has, and
          // this says what has actually arrived.
          if (event.name === "aiconfig.file" && data.type === "aiconfig_file") {
            state.aiFileEvent = { revision: (state.aiFileEvent?.revision || 0) + 1, data };
          }
          // A delivery receipt describes one send, not the clipboard: fold it
          // in place and stop.  Left to the fall-through below it would repaint
          // the whole history list on every transition — a screenshot of a
          // large transfer's acks, for nothing.
          if (event.name === "relay.delivery.changed") {
            delivery.apply(data);
            return;
          }
          if (event.name === "netpair.peer.changed") {
            // Kept, and then left to fall through: the card has to re-read the
            // peers (the event carries one row, not the list), and a confirmed
            // pairing is worth saying out loud.  `unpaired` is the same event
            // with the other status and is reported by whoever asked for it, so
            // it only re-reads.
            state.netpairEvent = {
              revision: (state.netpairEvent?.revision || 0) + 1,
              data,
            };
            if (data.status === "paired") pushNotice(event.name, data);
          }
          if (event.name && ["runtime.error", "pairing.request", "transfer.request", "chat.message", "chat.connect_timeout", "url.received", "device.connected", "device.disconnected", "sync.redacted", "device.connection_rejected", "device.connection_unreachable"].includes(event.name)) {
            pushNotice(event.name, data);
          }
          if (event.name === "favorites.changed" || event.name === "data.changed" || event.type === "resync") favorites.invalidate();
          ++statusSequence;
          ++requestSequence;
          state.loading = false;
          state.refreshing = true;
          // Events invalidate queries; snapshots remain authoritative. Subscribe
          // before the first query so an in-flight mutation cannot be missed.
          clearTimeout(refreshTimer);
          refreshTimer = setTimeout(() => { if (!disposed) void refresh(); }, 60);
        },
        (update) => {
          if (disposed) return;
          if (update.state === "failed" || update.state === "restarting") {
            // Both mean the sidecar is not answering, so drop everything that
            // came from it rather than leaving half a snapshot on screen. The
            // host relaunches on its own after `restarting`; `failed` means it
            // gave up, and the band's retry button asks for one more attempt.
            const restarting = update.state === "restarting";
            favorites.reset();
            delivery.reset();
            clearTimeout(refreshTimer);
            ++statusSequence;
            ++requestSequence;
            state.loading = false;
            state.refreshing = false;
            state.devices = [];
            state.history = [];
            state.selectedIds = [];
            state.total = 0;
            if (state.status) state.status.health = "stopped";
            setError(restarting
              ? { code: "SIDECAR_RESTARTING",
                message: t("后台进程已退出，正在重新启动（第 {attempt} 次）…", { attempt: update.attempt || 1 }),
                retryable: false }
              : { code: update.error || "SIDECAR_UNAVAILABLE",
                message: t("后台进程不可用，请重试"), retryable: true });
          } else if (update.state === "ready" && state.error) {
            // The relaunched sidecar is up: clear the failure and pull the
            // snapshot the dropped UI is missing.
            state.error = null;
            ++statusSequence;
            ++requestSequence;
            void refresh();
          }
        },
      );
      if (disposed) { off(); return; }
      unlisten = off;
      await refresh();
    } catch (error) {
      if (!disposed) setError(error);
    }
  }

  /**
   * The retry behind the error band. A dead sidecar needs a relaunch first —
   * refreshing against a process that is gone would only fail again — while any
   * other retryable error is a transport hiccup that a refresh settles.
   */
  async function reconnect() {
    const down = !!state.error && SIDECAR_DOWN_CODES.includes(state.error.code);
    state.error = null;
    if (down && !disposed) {
      state.pending = true;
      try {
        await bridge.restartSidecar();
      } catch (error) {
        if (!disposed) setError(error);
        return;
      } finally {
        if (!disposed) state.pending = false;
      }
    }
    await refresh();
  }

  /**
   * Move damaged configuration, identity or history aside so the app can start.
   *
   * Offered when the sidecar reports its data directory as invalid: it refuses
   * to start on exactly that damage, so no RPC call — not even the factory
   * reset — can be used to get out of it. The host runs the repair as a
   * one-shot process and relaunches the sidecar, so success only needs the
   * snapshot re-read; the files moved are named in a notice, because they are
   * renamed rather than deleted and a user may want them back.
   */
  async function recoverData() {
    if (disposed || state.pending) return false;
    state.pending = true;
    state.error = null;
    try {
      const result = await bridge.recoverDataDir() as RecoveryResult;
      if (disposed) return false;
      const files = (result.items || []).flatMap((item) => item.files || []);
      if (files.length) {
        const id = ++noticeSequence;
        state.notices.push({
          id,
          title: t("数据修复"),
          message: t("已将损坏的文件移到一旁：{files}", { files: files.join(", ") }),
        });
        if (state.notices.length > 5) dismissNotice(state.notices[0].id);
        // Long enough to copy a file name out of, unlike a passing event.
        noticeTimers.set(id, setTimeout(() => dismissNotice(id), 60000));
      }
      await refresh();
      return true;
    } catch (error) {
      if (!disposed) setError(error);
      return false;
    } finally {
      if (!disposed) state.pending = false;
    }
  }

  /** Run a route and report its outcome.
   *
   * `eventReported` names result fields whose `false` is not a failure to report
   * here, because the runtime publishes the reason as an event of its own — the
   * connect route does exactly that for "there was nowhere to dial", and legacy
   * left the same click to its own toasts for the same reason.
   */
  async function action(run: () => Promise<unknown>, eventReported: string[] = []) {
    if (disposed || state.pending) return false;
    state.pending = true;
    state.error = null;
    try {
      const result = await run();
      if (disposed) return false;
      const refused = (key: string) =>
        key in (result as Record<string, unknown>) &&
        (result as Record<string, unknown>)[key] === false &&
        !eventReported.includes(key);
      if (result && typeof result === "object" && (refused("accepted") || refused("copied"))) {
        throw { code: "ACTION_REJECTED", message: t("操作未完成，请刷新后重试"), retryable: true };
      }
      await refresh();
      return true;
    } catch (error) {
      if (!disposed) setError(error);
      return false;
    } finally {
      if (!disposed) state.pending = false;
    }
  }

  function historyBusy() {
    return disposed || state.pending || state.loading || state.refreshing || state.status?.health !== "ready";
  }

  function selectedBatch(ids: string[]) {
    const unique = [...new Set(ids)];
    if (!unique.length || unique.length > 100 || unique.some((id) =>
      !id.trim() || !state.selectedIds.includes(id) || !state.history.some((item) => item.id === id))) {
      setError({ code: "INVALID_SELECTION", message: t("所选记录已变化，请重新选择"), retryable: false });
      return null;
    }
    return unique;
  }

  function discoveryAvailable() {
    return state.status?.health === "ready" &&
      !!state.status.capabilities?.includes("discovery.status");
  }

  async function discoveryToggle(kind: "enabled" | "visible", enabled: boolean) {
    // Both switches return the resulting discovery state so the UI reflects
    // what the runtime reports rather than what was requested.
    if (disposed || state.pending || !discoveryAvailable()) return null;
    state.pending = true;
    state.error = null;
    try {
      return kind === "enabled"
        ? await bridge.setDiscoveryEnabled(enabled)
        : await bridge.setDiscoveryVisible(enabled);
    } catch (error) {
      if (!disposed) setError(error);
      return null;
    } finally {
      if (!disposed) state.pending = false;
    }
  }

  return {
    state, favorites, delivery, start, refreshHistory,
    dismissNotice,
    refresh() { state.error = null; return refresh(); },
    // The error band's retry: relaunches a dead sidecar, then refreshes.
    reconnect,
    // The error band's repair, offered only for a data directory the sidecar
    // refuses to start on.
    recoverData,
    select(id: string, selected: boolean) {
      if (historyBusy() || !id.trim() || !state.history.some((item) => item.id === id)) return;
      if (!selected) state.selectedIds = state.selectedIds.filter((value) => value !== id);
      else if (!state.selectedIds.includes(id) && state.selectedIds.length < 100) state.selectedIds.push(id);
    },
    selectAllVisible(selected: boolean) {
      if (historyBusy()) return;
      state.selectedIds = selected
        ? [...new Set(state.history.map((item) => item.id).filter((id) => id.trim()))].slice(0, 100)
        : [];
    },
    batchPin(pinned: boolean) {
      if (historyBusy()) return Promise.resolve(false);
      const ids = selectedBatch(state.selectedIds);
      return ids ? action(() => bridge.batchPinHistory(ids, pinned)) : Promise.resolve(false);
    },
    batchDelete(entryIds: string[]) {
      if (historyBusy()) return Promise.resolve(false);
      const ids = selectedBatch(entryIds);
      return ids ? action(() => bridge.batchDeleteHistory(ids)) : Promise.resolve(false);
    },
    async batchFavorite(entryIds: string[], group = "") {
      // Returns how many entries were copied into the favorites store (0 on
      // failure) so the caller can report the count, like the legacy toast.
      if (historyBusy()) return 0;
      const ids = selectedBatch(entryIds);
      if (!ids) return 0;
      state.pending = true;
      state.error = null;
      try {
        const result = await bridge.batchFavoriteHistory(ids, group);
        if (disposed) return 0;
        await refresh();
        return result.added;
      } catch (error) {
        if (!disposed) setError(error);
        return 0;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async clearHistory() {
      if (historyBusy()) return 0;
      state.pending = true;
      state.error = null;
      try {
        const result = await bridge.clearHistory();
        if (disposed) return 0;
        state.selectedIds = [];
        state.offset = 0;
        await refresh();
        return result.cleared;
      } catch (error) {
        if (!disposed) setError(error);
        return 0;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    search(query: string) {
      if (disposed || state.pending) return;
      if (query !== state.query || state.offset !== 0) state.selectedIds = [];
      state.query = query;
      state.offset = 0;
      void refreshHistory();
    },
    /** Narrow the list to one of the panel's kind chips. */
    setKind(kind: string) {
      if (disposed || state.pending || kind === state.kind) return;
      // Back to the first page either way: a narrowing filter applied to the
      // thirty-first row of the old list would open on an empty page, and the
      // selection cannot survive rows that the filter just removed.
      state.selectedIds = [];
      state.kind = kind;
      state.offset = 0;
      void refreshHistory();
    },
    /** Flip between newest- and oldest-first. */
    toggleSort() {
      if (disposed || state.pending) return;
      state.sort = state.sort === "newest" ? "oldest" : "newest";
      state.selectedIds = [];
      state.offset = 0;
      void refreshHistory();
    },
    page(direction: number) {
      if (historyBusy()) return;
      const offset = Math.max(0, state.offset + direction * state.limit);
      if (offset !== state.offset) state.selectedIds = [];
      state.offset = offset;
      void refreshHistory();
    },
    unlock: (password: string) => action(() => bridge.unlock(password)),
    pin: (item: HistoryItem) => action(() => bridge.pinHistory(item.id, !item.pinned)),
    delete: (item: HistoryItem) => action(() => bridge.deleteHistory(item.id)),
    async copy(item: HistoryItem) {
      state.copiedId = null;
      if (await action(() => bridge.copyHistory(item.id))) state.copiedId = item.id;
    },
    startPairing: (device: Device) => action(() => bridge.startPairing(device.id)),
    /** Confirm the code, and say so when it did not take.
     *
     * The runtime answers `{paired, status}` rather than the `accepted` the
     * other actions carry, so a confirm that did not pair is not an error and
     * `action` would report it as a plain success.  It means the request was
     * already over — the code expired before it was answered, which is exactly
     * what the legacy dialog `notify.pairing_failed` was there to say.  Without
     * it the card simply vanishes and the click looks like it did nothing.
     *
     * A confirm that *did* pair says nothing: the row turns paired and the
     * runtime publishes `device.connected`, so a second message for the same
     * moment would only be noise.
     */
    async confirmPairing(device: Device) {
      if (disposed || state.pending) return false;
      state.pending = true;
      state.error = null;
      try {
        const result = await bridge.confirmPairing(device.id, device.pairing_code || "");
        if (disposed) return false;
        await refresh();
        if (result.paired === false) pushNotice("pairing.failed", {});
        return true;
      } catch (error) {
        if (!disposed) setError(error);
        return false;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    rejectPairing: (device: Device) => action(() => bridge.rejectPairing(device.id)),
    unpair: (device: Device) => action(() => bridge.unpairDevice(device.id)),
    /** Answer a certificate-change prompt by pinning what the device presented.
     *
     * The prompt closes only when the runtime accepted the answer: a device that
     * has not connected with its new certificate yet cannot be trusted, and
     * leaving the prompt up (with the error in the band) is what lets the user
     * try again once it has.
     */
    async retrust(deviceId: string) {
      const ok = await action(() => bridge.retrustDevice(deviceId));
      if (ok && state.certAlert?.device_id === deviceId) clearCertPrompt();
      return ok;
    },
    /** Answer it the other way: the changed device stays unpaired. Unpairing is
     * what that answer means, so it runs the existing path — and the runtime
     * voids the prompt with it. Takes an id, because a device that only ever
     * refused its handshake may not be in the snapshot at all. */
    async keepUnpaired(deviceId: string) {
      const ok = await action(() => bridge.unpairDevice(deviceId));
      if (ok && state.certAlert?.device_id === deviceId) clearCertPrompt();
      return ok;
    },
    /** Ask the runtime to dial a device.
     *
     * The route answers whether a dial was *started*, not whether it connected,
     * so `accepted: false` is not reported as a failed action: it means there was
     * nowhere to dial, and the runtime says so itself in
     * `device.connection_unreachable` — a generic "refresh and try again" beside
     * that notice would be advice that cannot help. The outcome of a dial that did
     * start arrives as the card's own state (or a refusal from the peer), never as
     * an optimistic "connected" from here.
     */
    connect: (device: Device) => action(() => bridge.connectDevice(device.id), ["accepted"]),
    disconnect: (device: Device) => action(() => bridge.disconnectDevice(device.id)),
    /** Open a clip that is a web link in the browser.
     *
     * The window names the row and the sidecar reads it: the URL never crosses
     * the IPC from here, and the app only opens what the row already holds. Not
     * run through `action` because nothing about the list changes — an open is
     * not a mutation, so there is no snapshot to re-read afterwards.
     */
    async openLink(item: HistoryItem) {
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.openHistoryLink(item.id);
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    forget: (device: Device) => action(() => bridge.forgetDevice(device.id)),
    restore: (device: Device) => action(() => bridge.restoreDevice(device.id)),
    purge: (device: Device) => action(() => bridge.purgeDevice(device.id)),
    async probe(device: Device) {
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.testDevice(device.id);
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async certificates() {
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return (await bridge.deviceCerts()).devices;
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    setSyncEnabled: (enabled: boolean) => {
      if (state.status?.health !== "ready" ||
        !state.status.capabilities?.includes("sync.set_enabled")) return;
      return action(() => bridge.setSyncEnabled(enabled));
    },
    async sendUrl(device: Device, url: string) {
      // Returns whether the frame actually left this machine. The runtime
      // rejects non-http(s) URLs, so the only local check is the length bound
      // the closed RPC enforces.
      if (disposed || state.pending) return false;
      if (!url.trim() || url.length > 2048) {
        setError({ code: "INVALID_URL", message: t("请输入有效的网址"), retryable: false });
        return false;
      }
      state.pending = true;
      state.error = null;
      try {
        await bridge.sendUrl(device.id, url);
        return !disposed;
      } catch (error) {
        if (!disposed) setError(error);
        return false;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async pushText(text: string) {
      // Writes the local clipboard AND broadcasts: `sent` is false when sync
      // is off, which is still a success for the clipboard half.
      if (disposed || state.pending) return null;
      if (!text.trim() || text.length > 100000) {
        setError({ code: "INVALID_TEXT", message: t("请输入要推送的文本"), retryable: false });
        return null;
      }
      state.pending = true;
      state.error = null;
      try {
        return await bridge.pushText(text);
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async discovery() {
      if (disposed || state.pending || !discoveryAvailable()) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.discoveryStatus();
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async diagnostics() {
      // Live snapshot built by the same use case the legacy panel renders, so
      // the two surfaces cannot disagree about the state of this machine.
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.diagnosticsReport();
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async repairDiagnostics(kind: DiagnosticAction) {
      // The caller re-runs diagnostics afterwards: the repair only opens the
      // OS settings or applies the rule, it does not prove the check passes.
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.diagnosticsRequest(kind);
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async loadUpdateStatus() {
      // Hydration after a reload: the sidecar keeps the live phase, so a
      // download that started before this window opened still renders.
      if (disposed || !state.status?.capabilities?.includes("update.status")) return null;
      try {
        const result = await bridge.updateStatus();
        if (!disposed && result?.state) Object.assign(state.update, result.state);
        return result;
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      }
    },
    async checkUpdate() {
      // Bounded server-side (~8s); a failure is reported as "unknown", never
      // as "up to date", so the caller can tell the two apart.
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        const result = await bridge.updateCheck();
        if (disposed) return null;
        state.updateCheck = result.latest || result.available ? result : null;
        return result;
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async downloadUpdate() {
      // Returns immediately; progress arrives as `update.state` events.
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.updateDownload();
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async openUpdateFolder() {
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.updateOpenFolder();
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    setDiscoveryEnabled: (enabled: boolean) => discoveryToggle("enabled", enabled),
    setDiscoveryVisible: (enabled: boolean) => discoveryToggle("visible", enabled),
    dispose() {
      for (const id of noticeTimers.keys()) dismissNotice(id);
      favorites.dispose();
      delivery.dispose();
      disposed = true;
      ++requestSequence;
      ++statusSequence;
      clearTimeout(refreshTimer);
      clearTimeout(certPromptTimer);
      unlisten?.();
    },
  };
}
