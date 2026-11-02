/* Pick the download that fits the reader's machine.
 *
 * The links themselves are stable, versionless names under the Cloudflare
 * bucket's /download/ prefix, so this file only chooses which one to put in
 * front.  With JavaScript unavailable the primary card still downloads a
 * working Windows build and every other platform is a visible link.
 */
(() => {
  const zh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
  const text = zh
    ? {
        detected: (name) => `检测到 ${name}，已为你选中`,
        choose: "选择最适合你的版本",
        download: (name) => `下载 ${name} 版`,
      }
    : {
        detected: (name) => `${name} detected — selected for you`,
        choose: "Choose the build that fits",
        download: (name) => `Download for ${name}`,
      };

  const primary = document.querySelector("[data-download-primary]");
  const titleEl = document.querySelector("[data-download-title]");
  const fileEl = document.querySelector("[data-download-file]");
  const detectEl = document.querySelector("[data-download-detect]");
  const rows = Array.from(document.querySelectorAll("[data-download]"));
  if (!primary || !rows.length) return;

  const platform = /Windows/i.test(navigator.userAgent)
    ? "windows"
    : /Macintosh|Mac OS X/i.test(navigator.userAgent)
      ? "macos"
      : /Linux|X11/i.test(navigator.userAgent) && !/Android/i.test(navigator.userAgent)
        ? "linux"
        : "other";

  async function architecture() {
    if (platform === "macos") {
      // Intel Macs still exist, but every browser that exposes the hint can
      // answer this; the ones that cannot are most likely on Apple silicon,
      // and the Intel row is one click away when that guess is wrong.
      try {
        const values = await navigator.userAgentData?.getHighEntropyValues?.(["architecture"]);
        if (values?.architecture === "x86") return "x64";
        if (values?.architecture === "arm") return "arm64";
      } catch {
        /* fall through to the default */
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

    const row = rows.find((item) => item.dataset.download === key);
    if (!row) {
      if (detectEl) detectEl.textContent = text.choose;
      return;
    }

    const name = row.querySelector("b")?.textContent?.trim() || "";
    const detail = row.querySelector("small")?.textContent?.trim() || "";
    primary.href = row.getAttribute("href") || primary.href;
    if (titleEl) titleEl.textContent = name;
    if (fileEl) fileEl.textContent = detail;
    primary.textContent = text.download(name);
    if (detectEl) detectEl.textContent = text.detected(name);
    for (const item of rows) item.classList.toggle("is-preferred", item === row);
  });
})();
