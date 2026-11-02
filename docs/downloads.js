/* Mark the build that fits the reader's machine.
 *
 * The links are stable, versionless names under Cloudflare's /download/
 * prefix; this file only adds the recommendation.  Without JavaScript every
 * link is still a working download.
 */
(() => {
  const zh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
  const text = zh
    ? {
        detected: (name, arch) => `检测到 ${name}（${arch}），推荐版本已标出。`,
        choose: "选择适合你的版本，全部由 Cloudflare 提供下载。",
        badge: "推荐",
      }
    : {
        detected: (name, arch) => `${name} (${arch}) detected — the recommended build is marked.`,
        choose: "Pick the build that fits your machine. All downloads are hosted by Cloudflare.",
        badge: "Recommended",
      };

  const links = Array.from(document.querySelectorAll("[data-download]"));
  const cards = Array.from(document.querySelectorAll("[data-platform]"));
  const note = document.querySelector("[data-download-detect]");
  if (!links.length || !cards.length) return;

  const platform = /Windows/i.test(navigator.userAgent)
    ? "windows"
    : /Macintosh|Mac OS X/i.test(navigator.userAgent)
      ? "macos"
      : /Linux|X11/i.test(navigator.userAgent) && !/Android/i.test(navigator.userAgent)
        ? "linux"
        : "other";

  async function architecture() {
    if (platform === "macos") {
      try {
        const values = await navigator.userAgentData?.getHighEntropyValues?.(["architecture"]);
        if (values?.architecture === "x86") return "x64";
        if (values?.architecture === "arm") return "arm64";
      } catch {
        /* fall through to the common case */
      }
      return "arm64";
    }
    if (platform === "linux") {
      try {
        const values = await navigator.userAgentData?.getHighEntropyValues?.(["architecture"]);
        if (values?.architecture === "arm") return "arm64";
      } catch {
        /* fall through to x64 */
      }
    }
    return "x64";
  }

  architecture().then((arch) => {
    const key =
      platform === "windows"
        ? "windows-x64"
        : platform === "macos"
          ? `macos-${arch}`
          : platform === "linux"
            ? `linux-${arch}-appimage`
            : "";

    const link = links.find((item) => item.dataset.download === key);
    if (!link) {
      if (note) note.textContent = text.choose;
      return;
    }

    const card = link.closest("[data-platform]");
    if (!card) return;
    for (const item of links) item.classList.toggle("is-preferred", item === link);
    for (const item of cards) item.classList.toggle("is-preferred", item === card);
    const badge = card.querySelector("[data-download-badge]");
    if (badge) badge.textContent = text.badge;
    if (note) {
      const name = card.querySelector("h3")?.textContent?.trim() || "";
      note.textContent = text.detected(name, arch === "arm64" ? "ARM64" : "x64");
    }
  });
})();
