import { computed, ref } from "vue";
import { EN } from "./en";
import { TW } from "./zh-TW";

/**
 * The desktop shell's i18n layer.
 *
 * Keys are the Chinese source strings themselves (gettext-style): the shell is
 * written in Chinese, `zh-CN` renders the source verbatim, and `en` looks the
 * source up in the catalog.  That keeps the default output byte-identical to
 * the pre-i18n UI — no existing test or screenshot changes — and makes a
 * missing translation degrade to readable Chinese instead of a raw key.
 *
 * The locale is a module-level reactive ref, so a template that calls `t()`
 * re-renders the moment `setLocale` runs.  Nothing here reads storage: the
 * language is owned by the sidecar (`settings.language`, gated by
 * `language_chosen` for the first-run picker) and pushed in at startup.
 */

/** Locales the shell can render.  Mirrors `internal/config/config.py`. */
export const LOCALES = ["zh-CN", "zh-TW", "en"] as const;
export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "zh-CN";

/** Shown in the language picker, each in its own language. */
export const LOCALE_NAMES: Record<Locale, string> = {
  "zh-CN": "简体中文",
  "zh-TW": "繁體中文",
  en: "English",
};

const locale = ref<Locale>(DEFAULT_LOCALE);

/** The active locale, for templates that need to branch on it. */
export const currentLocale = computed(() => locale.value);

/** Coerce anything the sidecar may hand us to a supported locale. */
export function normalizeLocale(value: unknown): Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value)
    ? (value as Locale)
    : DEFAULT_LOCALE;
}

/** Switch the shell's language; returns what was actually applied. */
export function setLocale(value: unknown): Locale {
  locale.value = normalizeLocale(value);
  // The document's own language is what a screen reader reads the page in: a
  // shell switched to English that keeps `lang="zh-CN"` is still pronounced
  // with Chinese phonetics, and the two differ enough to be unusable.  The
  // phone page already keeps its `<html lang>` in step with the language
  // picker; this is the same line, for the same reason.
  if (typeof document !== "undefined") document.documentElement.lang = locale.value;
  return locale.value;
}

function interpolate(template: string, params: Record<string, unknown>): string {
  return template.replace(/\{(\w+)\}/g, (placeholder, name: string) =>
    name in params ? String(params[name]) : placeholder);
}

/**
 * The English plural form a count selects, or `null` when there is no count to
 * choose from — no params at all, or a `{count}`/`{n}` that is not a number.
 *
 * English's cardinal rule is the simple one: exactly 1 is singular and
 * everything else is not, including 0 ("0 records") and fractions.  The
 * catalog carries the singular as `"key|one"` and the plural as `"key|other"`,
 * so a language with more forms would need its own rule here and nothing else.
 */
function pluralForm(params?: Record<string, unknown>): "one" | "other" | null {
  if (!params) return null;
  const raw = "count" in params ? params.count : params.n;
  if (typeof raw !== "number" || !Number.isFinite(raw)) return null;
  return Math.abs(raw) === 1 ? "one" : "other";
}

/**
 * Translate a Chinese source string.
 *
 * `t("已复制 {count} 项", { count: 3 })` — unknown placeholders are left
 * alone so a missing param is visible rather than silently blank.
 *
 * A count also picks a plural form in English: `"已删除 {count} 条|one"` is
 * "Deleted {count} item", `"|other"` is "Deleted {count} items".  Only the
 * strings that need it carry the suffixed entries — everything else keeps one
 * form, which is what Chinese has and what most of the catalog is.  This is
 * deliberately not ICU: swapping the `t()` signature for a message-format
 * parser would rewrite 1400 entries to fix a handful of "(s)" and turn a
 * one-line lookup into a dependency.  See U95.
 */
export function t(source: string, params?: Record<string, unknown>): string {
  if (locale.value === "zh-TW") {
    return interpolate(TW[source] ?? source, params ?? {});
  }
  if (locale.value === "en") {
    const form = pluralForm(params);
    if (form) {
      const plural = EN[`${source}|${form}`];
      if (plural !== undefined) return interpolate(plural, params!);
    }
    return interpolate(EN[source] ?? source, params ?? {});
  }
  return params ? interpolate(source, params) : source;
}
