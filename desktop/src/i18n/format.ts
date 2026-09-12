import { currentLocale } from "./index";

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
