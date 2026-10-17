import { computed, ref } from "vue";
import { EN } from "./en";

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
export const LOCALES = ["zh-CN", "en"] as const;
export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "zh-CN";

/** Shown in the language picker, each in its own language. */
export const LOCALE_NAMES: Record<Locale, string> = {
  "zh-CN": "简体中文",
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
  return locale.value;
}

function interpolate(template: string, params: Record<string, unknown>): string {
  return template.replace(/\{(\w+)\}/g, (placeholder, name: string) =>
    name in params ? String(params[name]) : placeholder);
}

/**
 * Translate a Chinese source string.
 *
 * `t("已复制 {count} 项", { count: 3 })` — unknown placeholders are left
 * alone so a missing param is visible rather than silently blank.
 */
export function t(source: string, params?: Record<string, unknown>): string {
  const template = locale.value === "en" ? (EN[source] ?? source) : source;
  return params ? interpolate(template, params) : template;
}
