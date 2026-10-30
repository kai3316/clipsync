import { bridge } from "../api/bridge";
import type { SidecarEvent, Transfer, TransfersPage } from "../api/types";
import { t } from "../i18n";

/**
 * The on-demand paste of a remote-file row (1.3.1), as both windows run it.
 *
 * A file that lives on another device is not downloaded when its offer arrives:
 * the row remembers the peer, and the bytes move only when the reader actually
 * pastes it.  That makes one paste a small orchestration — start the fetch, watch
 * its transfers, hear the outcome — and it happens in two places: the quick-paste
 * overlay draws the whole progress panel, and the main window draws a bar for
 * the same fetch (and a headless version for the paste shortcuts).  The pieces
 * they must agree on live here: what a remote-file row is, how the transfer list
 * collapses into one progress reading, what the sidecar's failure reasons are
 * called in the reader's language, and the follow loop itself.
 *
 * The poll is the curve and the events are the steps: the transfer list carries
 * the filename, the size, the rate and the estimate, while
 * `clip_files.paste_ready` / `clip_files.paste_failed` are the only truthful
 * "done" — a row that vanished from the list is not a row that was pasted.
 */

/** How often the progress panel re-reads the transfer list.  Fast enough that a
 *  small file's bar still moves visibly, slow enough that it is four reads a
 *  second over a local pipe rather than a busy loop. */
export const PASTE_POLL_MS = 400;

/** Whether a history row is a file that lives on another device.
 *
 * The same rule the history page's own actions use: the offer id is the proof,
 * the kind is the row's best label — a copy that carries the file's name as
 * text *and* the offer is labelled TEXT by a ranking that preferred the text,
 * and it can still be fetched. */
export function isRemoteFileRow(item: { content_type?: string; offer_entry?: string }): boolean {
  if (item.offer_entry) return true;
  return (item.content_type || "").toUpperCase() === "FILE_REMOTE";
}

/** One paste's progress, collapsed out of however many transfer rows it has. */
export interface PasteFetchProgress {
  entryId: string;
  /** How many of the paste's files have a transfer row right now. */
  files: number;
  /** The first row's name, or "" when only the event fraction is known. */
  filename: string;
  /** 0–100; size-weighted when the rows carry sizes. */
  progress: number;
  /** Bytes per second, summed over the rows. */
  speed: number;
  /** Already formatted by the sidecar (`format_eta`), or "". */
  eta: string;
  /** Total bytes across the rows, best effort (0 when unknown). */
  size: number;
}

/** Collapse the transfer list into the one reading a progress panel shows.
 *
 * `history` is read as well as `active`: the last file of a paste can move out of
 * the active list a fraction of a second before `paste_ready` arrives, and a
 * panel that blinked to 0 % in that gap would look like a restart. */
export function pasteProgressFrom(page: TransfersPage | null | undefined, entryId: string): PasteFetchProgress | null {
  if (!page || !entryId) return null;
  const rows: Transfer[] = [...(page.active || []), ...(page.history || [])]
    .filter((row) => row.paste_entry === entryId);
  if (!rows.length) return null;
  const weight = (row: Transfer) => Math.max(1, Number(row.size) || 0);
  const totalWeight = rows.reduce((sum, row) => sum + weight(row), 0);
  const done = rows.reduce((sum, row) => sum + (Number(row.progress) || 0) * weight(row), 0);
  return {
    entryId,
    files: rows.length,
    filename: rows[0].filename || "",
    progress: totalWeight ? Math.min(100, Math.max(0, done / totalWeight)) : 0,
    speed: rows.reduce((sum, row) => sum + (Number(row.speed) || 0), 0),
    eta: rows.find((row) => row.eta)?.eta || "",
    size: rows.reduce((sum, row) => sum + (Number(row.size) || 0), 0),
  };
}

/** The entry id a `clip_files.paste_ready` event is about, or "". */
export function pasteReadyEntry(event: SidecarEvent | null | undefined): string {
  if (event?.type !== "event" || event.name !== "clip_files.paste_ready") return "";
  return String((event.data as Record<string, unknown> | undefined)?.entry_id || "");
}

/** The paths a `clip_files.paste_ready` event carried. */
export function pasteReadyPaths(event: SidecarEvent | null | undefined): string[] {
  const raw = (event?.data as Record<string, unknown> | undefined)?.paths;
  return Array.isArray(raw) ? raw.map(String) : [];
}

/** The `clip_files.paste_failed` event's entry id and reason, or null. */
export function pasteFailedEvent(event: SidecarEvent | null | undefined): { entryId: string; reason: string } | null {
  if (event?.type !== "event" || event.name !== "clip_files.paste_failed") return null;
  const data = event.data as Record<string, unknown> | undefined;
  const entryId = String(data?.entry_id || "");
  if (!entryId) return null;
  return { entryId, reason: String(data?.reason || "") };
}

/** A finished paste's failure reason in the reader's words.
 *
 * The reasons are the sidecar's (`lan.py`): `cancelled` when the reader or the
 * stall timer stopped it, `timeout` when the peer went quiet, the two size
 * refusals, and the two halves that can fail after the bytes arrived.  Anything
 * else is a transfer status passed through, shown with the code so a bug report
 * can name it. */
export function pasteFailureText(reason: string): string {
  switch (String(reason || "")) {
    case "cancelled": return t("已取消取回");
    case "timeout": return t("等待传输超时，请重试");
    case "too_large": return t("文件超过当前大小上限，可在设置里调大");
    case "relay_too_large": return t("经互联网中继取回的文件超过上限");
    case "not_available": return t("这个条目已无法从另一台设备取回");
    case "clipboard_write_failed": return t("文件已取回，但写入剪贴板失败");
    case "history_update_failed": return t("文件已取回并写入剪贴板，但历史记录未能更新，可以直接粘贴");
    case "": return t("取回失败，请重试");
    default: return t("取回失败：{reason}", { reason: String(reason) });
  }
}

/** A `prepare_paste_entry` refusal in the reader's words.
 *
 * `PASTE_CONFIRM_REQUIRED` is not a failure: it is the sidecar asking a
 * question, and the caller answers it with its own dialog and a second call
 * carrying `confirmed: true` (see `isPasteConfirmRequired`). */
export function pastePrepareErrorText(code: string, message?: string): string {
  switch (String(code || "")) {
    case "PASTE_TOO_LARGE": return t("文件超过当前大小上限，可在设置里调大");
    case "PASTE_RELAY_TOO_LARGE": return t("经互联网中继取回的文件超过上限");
    case "PASTE_NOT_AVAILABLE": return t("这个条目已无法从另一台设备取回");
    case "PASTE_CONFIRM_REQUIRED": return t("文件较大，粘贴前需从另一台设备传输，继续？");
    case "NOT_CONNECTED": return t("对方设备不在线，无法取回文件");
    case "NOT_PAIRED": return t("与那台设备的配对已失效，无法取回文件");
    case "NATIVE_HOST_REQUIRED": return message || t("请在 ClipSync 桌面窗口中打开");
    default: return message || t("无法开始取回文件");
  }
}

/** The error code of a rejected command, as the host reports one. */
export function errorCode(error: unknown): string {
  return String((error as { code?: unknown } | null)?.code || "");
}

/** The error message of a rejected command, as the host reported it. */
export function errorMessage(error: unknown): string {
  return String((error as { message?: unknown } | null)?.message || "");
}

/** Whether the refusal is the sidecar's "ask first" question. */
export function isPasteConfirmRequired(error: unknown): boolean {
  return errorCode(error) === "PASTE_CONFIRM_REQUIRED";
}

/** What a running fetch reports back.  `onReady` and `onFailed` are terminal:
 *  the run has stopped itself by the time either is called. */
export interface PasteFetchHandlers {
  onProgress: (progress: PasteFetchProgress | null) => void;
  onReady: (paths: string[]) => void;
  onFailed: (reason: string) => void;
}

/** A started follow; `stop` just reads nothing more, `cancel` also tells the
 *  runtime to abandon the fetch. */
export interface PasteFetchRun {
  stop: () => void;
  cancel: () => Promise<void>;
}

/**
 * Watch a fetch that has already been prepared.
 *
 * The transfer list is polled for the curve, the sidecar's events decide the
 * end: a permission refusal on the list (or a window that cannot read it) leaves
 * the event stream, which is forwarded to the overlay on its own channel, as the
 * only source — so a paste is never stuck waiting on a read it cannot make.
 */
export function followPasteFetch(entryId: string, handlers: PasteFetchHandlers): PasteFetchRun {
  let stopped = false;
  let unlisten: (() => void) | undefined;
  let timer: ReturnType<typeof setInterval> | undefined;
  /** The fraction from `transfer.progress`, when the list cannot be read.  It is
   * a jump rather than a curve — the event fires per chunk — but it is better
   * than a bar that never moves. */
  let eventFraction: number | null = null;

  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (timer) clearInterval(timer);
    timer = undefined;
    unlisten?.();
    unlisten = undefined;
  };

  const poll = async () => {
    if (stopped) return;
    try {
      const page = await bridge.transfers();
      if (stopped) return;
      const progress = pasteProgressFrom(page, entryId);
      if (progress) { eventFraction = null; handlers.onProgress(progress); return; }
    } catch { /* the events are still coming; a list read is the curve, not the verdict */ }
    if (stopped || eventFraction === null) return;
    handlers.onProgress({
      entryId, files: 0, filename: "", progress: eventFraction * 100,
      speed: 0, eta: "", size: 0,
    });
  };

  void poll();
  timer = setInterval(() => { void poll(); }, PASTE_POLL_MS);

  const subscribe = () => {
    try {
      const pending = bridge.onSidecarEvent((event) => {
        if (stopped) return;
        if (pasteReadyEntry(event) === entryId) {
          const paths = pasteReadyPaths(event);
          stop();
          handlers.onReady(paths);
          return;
        }
        const failed = pasteFailedEvent(event);
        if (failed && failed.entryId === entryId) {
          stop();
          handlers.onFailed(failed.reason);
          return;
        }
        if (event?.type === "event" && event.name === "transfer.progress") {
          const data = event.data as Record<string, unknown> | undefined;
          if (String(data?.paste_entry || "") !== entryId) return;
          const fraction = Number(data?.progress);
          if (Number.isFinite(fraction)) eventFraction = Math.min(1, Math.max(0, fraction));
        }
      });
      void pending
        .then((off) => { if (stopped) off(); else unlisten = off; })
        .catch(() => { /* a window without the event channel keeps the poll */ });
    } catch { /* same: no listener, the poll still runs */ }
  };
  subscribe();

  return {
    stop,
    async cancel() {
      stop();
      try { await bridge.cancelPasteEntry(entryId); } catch { /* already gone */ }
    },
  };
}
