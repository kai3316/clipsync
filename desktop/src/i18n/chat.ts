import { currentLocale, t, type Locale } from "./index";
import type { ChatEntry } from "../api/types";

/**
 * The sentence for a system notice in a conversation.
 *
 * The sidecar does not send these as text.  `ChatEntry.text` is empty and
 * `text_key` carries a dotted key (`internal/sync/nearby_chat.py`, "i18n key
 * for system entries (never literals)"), because the sentence has to come out
 * in the reader's language and the same entry is rendered by three fronts.  The
 * Companion resolves it against the shared locale files
 * (`internal/web/static/locales/*.json`) and the legacy panel against
 * `internal/i18n`; this is the shell's copy of the same table.
 *
 * It lives in the i18n module rather than in `en.ts` because the catalog is
 * keyed by the Chinese source string, and a dotted key with no `t` call behind
 * it would read there as a stale entry — which is what the "has no catalog
 * entry that no longer appears in the shell" case in `tests/i18n.test.ts`
 * exists to catch.  All three copies are held to the
 * shared JSON by `tests/sidecar/test_chat_system_keys.py`, so a notice added to
 * the sidecar fails the Python suite until it is translated here too.
 *
 * Interpolation goes through `t()` rather than a local `{name}` replace so a
 * placeholder with nothing behind it stays visible, the same rule the rest of
 * the shell follows.
 */
const NOTICES: Record<Locale, Record<string, string>> = {
  "zh-CN": {
    "chat.system.peer_offline": "{name} 已离线——消息将在其恢复在线后送达。",
    "chat.system.session_closed_by_peer": "{name} 已关闭会话。",
    "chat.system.file_declined": "{name} 拒绝了文件。",
    "chat.system.file_cancelled": "文件传输已取消。",
  },
  "en": {
    "chat.system.peer_offline": "{name} went offline — messages will be delivered when they return.",
    "chat.system.session_closed_by_peer": "{name} closed the session.",
    "chat.system.file_declined": "{name} declined the file.",
    "chat.system.file_cancelled": "The file transfer was cancelled.",
  },
};

/**
 * The sentence to show for a system entry.
 *
 * `peerName` fills `{name}` for the notices the sidecar sends without a
 * `fmt.name`: an offline or closed session is announced with the peer's name
 * nowhere in the payload, and the conversation on screen is the thing that
 * knows it.  The Companion falls back the same way.  When even that is empty
 * the placeholder is dropped rather than printed.
 *
 * An unrecognised key reads as the generic 系统消息 rather than as the raw
 * dotted key — the failure this module was written to remove.
 */
export function systemText(entry: ChatEntry, peerName = ""): string {
  const template = (NOTICES[currentLocale.value] || NOTICES["zh-CN"])[entry.text_key];
  if (!template) return entry.text || t("系统消息");
  const name = String(entry.fmt?.name ?? "") || peerName;
  return t(name ? template : template.replace("{name} ", ""), { ...entry.fmt, name });
}
