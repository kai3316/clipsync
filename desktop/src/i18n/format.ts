import { currentLocale, t } from "./index";

/**
 * Human-readable values for the shell's surfaces.
 *
 * The window's equivalent of the panel's `ClipsyncFormat`: the legacy module
 * existed because the same four formatters had been copied into five components
 * and had drifted apart, so they live in one place here too.  The legacy
 * `Intl` fallback for a webview without it is deliberately not ported — the
 * shell only ever runs in the platform's current WebView2/WKWebView, where
 * `Intl` is always present, so the fallback would be unreachable code.
 *
 * Transfer ETAs are not here: they need the time units of the user's language,
 * so the sidecar formats them (`format_eta`).
 */

const KB = 1024;
const MB = 1024 * 1024;
const GB = 1024 * 1024 * 1024;

/**
 * A byte count as a short human-readable size.
 *
 * Returns '' for null/undefined/negative/non-finite input, so a template can
 * bind it straight in and show nothing rather than "NaN B".
 */
export function size(bytes: unknown): string {
  const value = Number(bytes);
  if (!isFinite(value) || value < 0) return "";
  if (value < KB) return `${value} B`;
  if (value < MB) return `${(value / KB).toFixed(1)} KB`;
  if (value < GB) return `${(value / MB).toFixed(1)} MB`;
  return `${(value / GB).toFixed(2)} GB`;
}

/**
 * A transfer rate. Unlike `size`, zero is not interesting — a stalled transfer
 * should show nothing rather than "0 B/s".
 */
export function speed(bytesPerSec: unknown): string {
  const value = Number(bytesPerSec);
  if (!isFinite(value) || value <= 0) return "";
  if (value < KB) return `${Math.round(value)} B/s`;
  if (value < MB) return `${(value / KB).toFixed(1)} KB/s`;
  return `${(value / MB).toFixed(1)} MB/s`;
}

/** Two dates falling on the same calendar day, in local time. */
function sameDay(left: Date, right: Date): boolean {
  return left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate();
}

/**
 * A timestamp as it reads inside a conversation or a row list.
 *
 * Three cases, matching the legacy panels' `formatTime`: today shows the clock
 * alone, yesterday is named, and anything older carries an `MM-DD` prefix so a
 * message from last week cannot be read as one that just arrived.  A bare
 * clock time is the one thing a chat list must not show for an old message.
 */
export function shortTime(seconds: number): string {
  const date = new Date(seconds * 1000);
  if (!seconds || isNaN(date.getTime())) return "";
  const clock = new Intl.DateTimeFormat(currentLocale.value, {
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
  const now = new Date();
  if (sameDay(date, now)) return clock;
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (sameDay(date, yesterday)) return t("昨天 {time}", { time: clock });
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${month}-${day} ${clock}`;
}

/**
 * The date and time a timestamp shows as, for the active locale.
 *
 * The year appears only when it is not the current one: recent rows stay
 * compact, older ones stay unambiguous.
 */
export function dateTime(seconds: number): string {
  const date = new Date(seconds * 1000);
  if (!seconds || isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(currentLocale.value, {
    ...(date.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" as const }),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

/**
 * The bracketed previews `history_db` writes for a clip that has no text of
 * its own, and what each says to a reader.
 *
 * The labels are thunks rather than strings because the locale can change
 * after this module loads: a `t("图片")` evaluated at import time would freeze
 * the window into whichever language it started in.
 *
 * "[HTML]" is deliberately absent — it is the format's own name in both
 * languages, so an entry for it would only be a second way to spell it.
 */
const PLACEHOLDER_PREVIEWS = new Map<string, () => string>([
  ["[Image]", () => t("图片")],
  ["[Vector Image]", () => t("矢量图")],
  ["[Rich Text]", () => t("富文本")],
]);

/**
 * A clip's preview, with the sidecar's placeholder labels in the reader's
 * language.
 *
 * An image, a vector image and a rich-text clip have no text to preview, so
 * `history_db` writes a bracketed English word into the preview column and
 * every surface printed it verbatim — a Chinese window read "[Image]" on the
 * line above the row's own 图片 chip.  It is a label rather than the user's
 * words, so it follows the interface language here.  The panel's counterpart
 * is `ClipsyncAPI.previewText`.
 *
 * Only the rendering changes.  The stored form is what the merge, the search
 * index and the sidecar's own type filters read, and it is left alone.
 */
export function previewText(preview: unknown): string {
  const stored = String(preview == null ? "" : preview);
  const label = PLACEHOLDER_PREVIEWS.get(stored);
  return label ? label() : stored;
}

/**
 * Whether a preview is one of those labels rather than something the user
 * copied — a clip whose text there is nothing more of to show.
 */
export function isPlaceholderPreview(preview: unknown): boolean {
  return PLACEHOLDER_PREVIEWS.has(String(preview == null ? "" : preview));
}
