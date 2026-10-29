import { bridge } from "../api/bridge";
import { t } from "../i18n";
import { announce } from "./status";

/**
 * Put plain text on this machine's clipboard, and say so.
 *
 * One helper rather than a call per surface, because the menu copies four
 * kinds of thing that are not clips — a chat message, a device id, a file name
 * and a file path — and each of them wants the same two things: the write and
 * a report the reader can act on.  The legacy panel had the same helper
 * (`_copyText`) and reached for it from every one of those menu entries, down
 * to the same single confirmation line, which is why nothing here says *what*
 * was copied: the click named it, and the toast is only saying it worked.
 *
 * Different from a history row's own copy button, which copies a *history
 * entry* by id and is additionally what re-broadcasts a clip: this writes text
 * that is already in hand and is not a clip at all, so neither the history nor
 * any paired device hears about it.
 *
 * Returns whether it worked, for a caller that has something better to say
 * than the generic line.
 */
export async function copyText(text: string): Promise<boolean> {
  const value = String(text ?? "");
  if (!value.trim()) return false;
  try {
    const result = await bridge.copyText(value);
    if (result?.copied === false) throw new Error(t("复制失败"));
    announce(t("已复制！"));
    return true;
  } catch (error: any) {
    announce(error?.message || t("复制失败"));
    return false;
  }
}
