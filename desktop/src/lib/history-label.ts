import { t } from "../i18n";
import { previewText } from "../i18n/format";
import type { HistoryItem } from "../api/types";

/**
 * What a clipboard row is called, in the two places that name one.
 *
 * Both of these lived in `App.vue` until the quick search needed them too: a
 * result row in that palette is a history row's name and preview at a smaller
 * size, and the rule for what a row with no text of its own says is one rule
 * rather than two.  It is a module of its own rather than an export from the
 * window because a component importing the window is a cycle.
 */

/**
 * A row's kind in words, the way the panel's row named it.
 *
 * The wire name is not a label: a Chinese user read 图片 where this window said
 * "IMAGE". Legacy named the same kinds and left the rest as the type they were
 * (`history-item.js::typeLabel`), which is what the fallback does — HTML and RTF
 * are the format's own name in either language.
 */
export function typeLabel(item: HistoryItem): string {
  const kind = String(item.content_type || "").toUpperCase();
  if (kind === "TEXT") return t("文本");
  if (kind === "URL" || kind === "LINK") return t("链接");
  if (kind === "FILE") return t("文件");
  // A file on another device is a file; the row's own actions already say which
  // machine it is on, so the label does not have to.
  if (kind === "FILE_REMOTE") return t("文件");
  if (kind === "IMAGE" || kind === "IMAGE_PNG" || kind === "IMAGE_EMF") return t("图片");
  return kind || t("内容");
}

/**
 * A row's preview as it is shown.
 *
 * Falls back to the kind in brackets for a row the sidecar sent no preview at
 * all for, which is what the legacy panel shows — the label is translated by
 * `previewText`, the bracketed kind is not, because it is a format name.
 */
export function previewLabel(item: HistoryItem): string {
  return previewText(item.preview) || `[${item.content_type || t("内容")}]`;
}
