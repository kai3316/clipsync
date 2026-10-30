/**
 * The appearance modes this shell paints with, and the one line that applies one.
 *
 * The list mirrors the `:root[data-theme="…"]` blocks in `src/styles.css` plus
 * `system`, which is a value in its own right rather than an absence: the dark
 * tokens are keyed off `prefers-color-scheme` for it, so the `system` choice is
 * written to the document like every other one and the stylesheet decides.
 *
 * Shared because the shell is two documents — the main window and the
 * quick-paste overlay — and a theme choice that stopped at one of them would be
 * a window painted in colours the reader did not pick.  That is also why the
 * fallback lives here: the main window needs it for a choice that arrived over
 * the phone's panel, and the overlay needs it for a host older than the field.
 */

export const APPEARANCE_MODES = [
  "system", "light", "dark", "sepia", "midnight", "contrast",
] as const;

export type AppearanceMode = (typeof APPEARANCE_MODES)[number];

const KNOWN: ReadonlySet<string> = new Set(APPEARANCE_MODES);

/**
 * Apply a stored or typed appearance mode and answer what was actually taken.
 *
 * Anything unrecognized reads as `system`, the same rule the sidecar's config
 * validation uses — a value from a newer build must not leave the window with
 * no theme at all.
 */
export function applyAppearanceTheme(mode: unknown): AppearanceMode {
  const value = String(mode || "system");
  const theme = KNOWN.has(value) ? (value as AppearanceMode) : "system";
  if (typeof document !== "undefined") document.documentElement.dataset.theme = theme;
  return theme;
}
