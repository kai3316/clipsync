import { computed } from "vue";
import { afterEach, describe, expect, it } from "vitest";
import { DEFAULT_LOCALE, LOCALES, LOCALE_NAMES, currentLocale, normalizeLocale, setLocale, t } from "../src/i18n";
import { EN } from "../src/i18n/en";

afterEach(() => setLocale(DEFAULT_LOCALE));

describe("i18n core", () => {
  it("renders the Chinese source verbatim by default", () => {
    expect(DEFAULT_LOCALE).toBe("zh-CN");
    expect(currentLocale.value).toBe("zh-CN");
    expect(t("设置")).toBe("设置");
    expect(t("暂无历史记录")).toBe("暂无历史记录");
  });

  it("translates through the catalog once the locale is en", () => {
    expect(setLocale("en")).toBe("en");
    expect(t("设置")).toBe("Settings");
    expect(t("剪贴板历史")).toBe("Clipboard History");
  });

  it("falls back to the source string for an untranslated key", () => {
    setLocale("en");
    expect(t("这句话还没有英文翻译")).toBe("这句话还没有英文翻译");
  });

  it("interpolates {name} params and leaves unknown ones visible", () => {
    expect(t("{count} 条记录", { count: 3 })).toBe("3 条记录");
    expect(t("打开 {path}")).toBe("打开 {path}");
    setLocale("en");
    expect(t("{count} 条记录", { count: 3 })).toBe("3 records");
    expect(t("打开 {path}", { path: "/tmp/a.txt" })).toBe("Open /tmp/a.txt");
    // A placeholder with no matching param stays visible rather than vanishing.
    expect(t("打开 {path}", { other: 1 })).toBe("Open {path}");
  });

  it("picks the English singular for a count of one", () => {
    // Chinese says 1 条记录 and 3 条记录; English does not, and the catalog
    // used to carry the difference as "(s)" — "transfer(s)", "file(s)" — which
    // is not English.  The count picks the form now.  See U95.
    setLocale("en");
    expect(t("{count} 条记录", { count: 1 })).toBe("1 record");
    expect(t("{count} 条记录", { count: 2 })).toBe("2 records");
    expect(t("已取消 {count} 个传输", { count: 1 })).toBe("Cancelled 1 transfer");
    expect(t("已取消 {count} 个传输", { count: 0 })).toBe("Cancelled 0 transfers");
    expect(t("已拖入 {count} 个文件", { count: 1 })).toBe("1 file dropped");
    // The Chinese side is the same sentence at every count: it has no form to
    // choose between, so the source string is returned untouched.
    setLocale("zh-CN");
    expect(t("{count} 条记录", { count: 1 })).toBe("1 条记录");
  });

  it("keeps one form for a count that is not a number, and for keys without one", () => {
    setLocale("en");
    // Unknown param, string count, no count at all: no form to choose, so the
    // entry without a suffix is what renders rather than a crash or a blank.
    expect(t("{count} 条记录", { count: "3" })).toBe("3 records");
    expect(t("{count} 条记录")).toBe("{count} records");
    // A string whose plural was never written keeps its single form.
    expect(t("已加入收藏夹 {count} 条", { count: 1 })).toBe("Added 1 to favorites");
  });

  it("coerces unknown locales back to the default", () => {
    expect(normalizeLocale("en")).toBe("en");
    expect(normalizeLocale("fr")).toBe("zh-CN");
    expect(normalizeLocale("")).toBe("zh-CN");
    expect(normalizeLocale(undefined)).toBe("zh-CN");
    expect(normalizeLocale(42)).toBe("zh-CN");
    setLocale("fr");
    expect(currentLocale.value).toBe("zh-CN");
  });

  it("is reactive: a template recomputes when the locale changes", () => {
    const label = computed(() => t("设置"));
    expect(label.value).toBe("设置");
    setLocale("en");
    expect(label.value).toBe("Settings");
    setLocale("zh-CN");
    expect(label.value).toBe("设置");
  });

  it("names every locale in its own language", () => {
    expect(Object.keys(LOCALE_NAMES).sort()).toEqual([...LOCALES].sort());
    expect(LOCALE_NAMES["zh-CN"]).toBe("简体中文");
    expect(LOCALE_NAMES.en).toBe("English");
  });
});

/** Every `t("…")` / `t('…')` argument in the shell, minus the two files that
 *  are the i18n layer rather than users of it. */
function translatedKeys(): Map<string, string> {
  const found = new Map<string, string>();
  for (const { path, source } of shellFiles()) {
    for (const match of source.matchAll(/\bt\(\s*(["'])((?:\\.|(?!\1)[^\\])*)\1/g)) {
      found.set(match[2], path);
    }
  }
  return found;
}

/** Drops the source key of every `t("…")` so the scan below ignores it. */
function withoutKeys(source: string): string {
  return source.replace(/\bt\(\s*(["'])((?:\\.|(?!\1)[^\\])*)\1/g, "t(");
}

/** Chinese that never reached `t()`: a quoted literal or a raw text node.
 *
 *  Read over the prose only.  The i18n module is where multilingual tables
 *  live on purpose — `en.ts` maps Chinese to English, `chat.ts` holds the
 *  system-notice sentences in both languages — so a Chinese literal there is
 *  the mechanism, not a string that escaped it.  Whether those tables are
 *  complete is the business of the sidecar suite, which holds every one of them
 *  to the shared locale files. */
function untranslatedCjk(): string[] {
  const found: string[] = [];
  for (const { path, source: raw } of shellFiles().filter(({ path }) => !path.includes("i18n/"))) {
    const source = withoutKeys(raw);
    for (const match of source.matchAll(/"([^"\n]*[一-鿿][^"\n]*)"/g)) {
      found.push(`${path}: ${match[1]}`);
    }
    for (const match of source.matchAll(/'([^'\n]*[一-鿿][^'\n]*)'/g)) {
      found.push(`${path}: ${match[1]}`);
    }
    for (const match of source.matchAll(/>([^<>"{}\n'=]*[一-鿿][^<>"{}\n'=]*)</g)) {
      found.push(`${path}: ${match[1].trim()}`);
    }
  }
  return found;
}

// Vite reads the sources for us: `import.meta.glob` is typed by vite/client,
// so this test needs no node builtins (and no @types/node).
const SHELL_SOURCES = import.meta.glob("../src/**/*.{vue,ts}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** The i18n layer itself, rather than a file that renders through it. */
function isI18nLayer(path: string): boolean {
  return path.endsWith("/i18n/en.ts") || path.endsWith("/i18n/index.ts");
}

function shellFiles(): Array<{ path: string; source: string }> {
  return Object.entries(SHELL_SOURCES)
    .filter(([path]) => !isI18nLayer(path))
    .map(([path, source]) => ({ path: path.replace("../src/", ""), source }));
}

describe("catalog coverage", () => {
  it("has an English entry for every string the shell translates", () => {
    const missing = [...translatedKeys()]
      .filter(([source]) => !(source in EN))
      .map(([source, where]) => `${where}: ${source}`);
    expect(missing).toEqual([]);
  });

  it("leaves no Chinese string outside a t() call", () => {
    expect(untranslatedCjk()).toEqual([]);
  });

  it("has no catalog entry that no longer appears in the shell", () => {
    // A stale key means a source string was edited and its translation was
    // silently orphaned — the exact drift the source-string-key scheme risks.
    // A plural variant is not a key of its own: `"…|one"` and `"…|other"` are
    // forms of the string before the bar, so they are checked as that string.
    const used = new Set(translatedKeys().keys());
    const stale = Object.keys(EN)
      .filter((key) => !used.has(key.replace(/\|(one|other)$/, "")))
      .map((key) => key);
    expect(stale).toEqual([]);
  });

  it("gives every plural variant the two forms English needs", () => {
    // `t()` picks `"…|one"` or `"…|other"` and falls back to the bare entry, so
    // a half-written pair would read as the plural at any count — the bug this
    // was meant to fix, only quieter.  Every suffixed entry must have a twin.
    const suffixed = Object.keys(EN).filter((key) => /\|(one|other)$/.test(key));
    expect(suffixed.length).toBeGreaterThan(0);
    const half = suffixed.filter((key) => {
      const twin = key.endsWith("|one") ? `${key.slice(0, -4)}|other` : `${key.slice(0, -6)}|one`;
      return !(twin in EN);
    });
    expect(half).toEqual([]);
    // And the bare entry has to exist as well: it is the fallback when there is
    // no count to choose from.
    const bare = suffixed
      .map((key) => key.replace(/\|(one|other)$/, ""))
      .filter((key) => !(key in EN));
    expect(bare).toEqual([]);
  });
});
