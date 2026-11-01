import { t } from "../i18n";
import type { HistoryItem } from "../api/types";
import type { createApplicationStore } from "../stores/application";

/**
 * The row's actions that two callers share: the history list's own buttons, and
 * the row menu the overview's feed raises as well.
 *
 * They lived in `App.vue` while the history page was the window's own markup.
 * When the page became a component, the menu stayed behind — the overview opens
 * the same menu, so it cannot belong to the page — and the two would then have
 * needed one rule each about whether a row is a remote file, what its download
 * offers and how a web link opens.  A component importing the window is a
 * cycle (see `history-label.ts`), so the rule is a module of its own and both
 * callers hand it the store they already have.
 *
 * Every function reads the live `state` rather than a snapshot: `remoteFileBusy`
 * is a pending list and `remoteFileDevice` looks the device up as it stands.
 */
export function createHistoryActions(store: ReturnType<typeof createApplicationStore>) {
  const state = store.state;

  /** Whether a history row is a web link, and so gets the legacy 在浏览器打开.
   *
   * Legacy decided from the row's text (`linkUrl`); the sidecar decides again
   * from the *whole* clip before opening anything, so a preview that merely looks
   * like one still opens nothing.
   */
  function isWebLink(item: HistoryItem) {
    return item.content_type === "URL" || /^https?:\/\//i.test(item.preview || "");
  }
  async function openHistoryLink(item: HistoryItem) {
    const result = await store.openLink(item);
    if (result?.opened) store.toast("ui.history", t("已在浏览器打开：{url}", { url: result.url }));
  }

  /** Whether a row is a file that lives on another device.
   *
   * Such a row has no bytes here — only the name, the size and the id of the
   * entry that published it — so it is the one row whose action is 下载 rather
   * than 复制.  Reading it off `content_type` and not the preview keeps that a
   * property of the row rather than of how its name happens to look.
   *
   * `offer_entry` is asked as well because the two come from different halves of
   * the sidecar: the kind is the row's best format, the id is the offer stored
   * inside it.  A copy that carries the file's name as text *and* the offer —
   * which is exactly what the Finder puts on the pasteboard — was labelled TEXT
   * by a ranking that preferred the text, and the row could be downloaded the
   * whole time.  Where the id is there, the offer is there, whatever the row
   * calls itself.
   */
  function isRemoteFile(item: HistoryItem) {
    if (item.offer_entry) return true;
    return (item.content_type || "").toUpperCase() === "FILE_REMOTE";
  }
  /** Whether a clip carries a formatted form beside its text.
   *
   * `content_type` is the row's single best label, and it is HTML or RTF exactly
   * when the entry holds markup next to the words — the two cases where pasting
   * "as copied" gives a reader something other than what they read on screen. */
  function isRichText(item: HistoryItem) {
    const kind = (item.content_type || "").toUpperCase();
    return kind === "HTML" || kind === "RTF";
  }
  /** The device a remote-file row came from, when this window can see it online.
   *
   * Not a gate on the button — never was meant to be: the sidecar is the one that
   * knows whether the peer is reachable, and it answers the click either way (a
   * request that cannot be delivered comes back as `NOT_CONNECTED`, which the
   * store words).  What it is for is the tooltip: it says the device looks offline
   * before the click instead of only after it.
   *
   * It used to gate the button as well, and that made the window stricter than the
   * sidecar on a fact it cannot know: "online" for an internet-paired device comes
   * from a 90-second in-process window over the last frame heard from it
   * (`internet_pairing.py::NETPAIR_ONLINE_WINDOW`, `_last_seen`), which is empty
   * after a restart and lapses while the peer is merely idle — so a download that
   * the backend would have accepted was a greyed-out button whose tooltip said the
   * device was offline.  Reported as "clicking download did nothing".  The window
   * keeps the hint and lets the request go.
   */
  function remoteFileDevice(item: HistoryItem) {
    const id = item.source_device || "";
    if (!id) return undefined;
    // Reachable by *either* route.  `connection_state` on a LAN-owned row is computed from the local
    // link alone (`lan.py`: `"online" if pid in connected ... else "offline"`), so a device that is
    // paired on this network, reachable over the relay while it is away, and carrying clipboard
    // traffic right now still reads `offline` here -- and the row's download button told the reader the
    // device was not online, about a device whose clips were arriving.  Reported exactly that way.
    //
    // This is the rule `device-row.ts::localLinkEstablished` already applies for the state word, and
    // `relay_paired && relay_online` is how a LAN-owned row reports the relay join it also holds.
    // A relay-only row arrives separately with `relay: true` and its own `connection_state`.
    return state.devices.find(
      (device) =>
        device.id === id &&
        (device.connection_state === "online" ||
          Boolean(device.relay_paired && device.relay_online)),
    );
  }
  /** Whether the row names a file this window can actually ask for.
   *
   * A file offer names the entry the *publishing* device stored the clip as, and
   * that id — not this row's — is what a request carries.  An offer this build
   * cannot read (a newer version's shape) leaves the row with nothing to ask
   * with, so it offers no download rather than one that would name the wrong
   * entry over there.
   */
  function remoteFileAskable(item: HistoryItem) {
    return !!item.offer_entry;
  }
  function remoteFileBusy(item: HistoryItem) {
    // Keyed by the id that goes on the wire, which is the id a refusal comes back
    // with — see `downloadRemoteFile`.
    return state.remoteFilePending.includes(`${item.source_device || ""}:${item.offer_entry || ""}`);
  }
  /** Whether the button is live.  The only reasons to refuse a click are the
   *  window's own: the row has no offer to name, or the same row is already
   *  waiting on an answer.  A device that merely *looks* offline is not one — the
   *  sidecar decides that, and it answers a request it cannot deliver. */
  function remoteFileActionable(item: HistoryItem) {
    return remoteFileAskable(item) && !remoteFileBusy(item);
  }
  function remoteFileTitle(item: HistoryItem) {
    if (remoteFileBusy(item)) return t("已请求下载，正在等待那台设备");
    if (!remoteFileAskable(item)) return t("这条记录无法下载");
    if (!remoteFileDevice(item)) {
      // The request is still offered: the sidecar answers whether the peer is
      // really unreachable, and the hint only says the row cannot confirm it.
      return t("{name} 可能不在线，仍可尝试下载", { name: item.source_name || t("未知") });
    }
    // The route decides what to promise, and both routes now work.  A relay transfer is real and
    // slower -- it is cut small enough for a public broker and every chunk is acked -- so the reader
    // about to move a few hundred megabytes is told which one they are getting before they click.
    if (remoteFileDevice(item)?.relay) {
      return t("从 {name} 下载这个文件（经互联网，比局域网慢）", { name: item.source_name || t("未知") });
    }
    return t("从 {name} 下载这个文件", { name: item.source_name || t("未知") });
  }
  async function downloadRemoteFile(item: HistoryItem) {
    if (await store.downloadRemoteFile(item)) {
      store.toast("ui.transfers", t("已请求下载，对方开始发送后可在文件传输里查看"));
    }
  }

  return {
    isWebLink, openHistoryLink, isRemoteFile, isRichText,
    remoteFileBusy, remoteFileActionable, remoteFileTitle, downloadRemoteFile,
  };
}
