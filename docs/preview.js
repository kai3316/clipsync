/* The interactive window above "common pages".
 *
 * The screenshot carries the sidebar, so one transparent button covers that
 * column.  A click is mapped by its vertical position to the nearest of the
 * ten rows; the numbered controls above the picture are the same switch for
 * touch and keyboard.  With scripting off the first screenshot remains.
 */
(() => {
  const zh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
  const descriptions = zh
    ? {
        overview: "同步状态、本机信息、手机扫码、快速设置和快捷操作。",
        history: "搜索、类型筛选、逐条复制 / 收藏 / 翻译 / 删除，也支持多选批量操作。",
        devices: "配对、手动连接、发现诊断、发送/接收开关，也可以在这里设置跨网手机伴侣。",
        favorites: "值得保留的内容单独存一份：分组、编辑、拖拽排序、一键复制回剪贴板。",
        transfers: "发送文件或文件夹；断了可以续传，失败可以重试，还能测两台电脑之间的速度。",
        chat: "和同一局域网里的设备聊天，不需要先配对；消息和文件直接送达。",
        companion: "手机不在同一网络时用远程访问；同一网络扫码就能打开网页版。",
        internet: "用配对码建立跨网连接；设备、中继状态和待投递消息都在这页。",
        ai: "内置 Claude Code、Codex、Cursor、Gemini CLI、Windsurf、Continue 六套配置，可先比较再同步。",
        settings: "通用、快捷键、粘贴与录制、文件、同步、网络、翻译、安全、数据恢复都在左侧分区。",
      }
    : {
        overview: "Sync state, this machine, phone scanning, quick settings and quick actions.",
        history: "Search, filter, copy, favorite, translate or delete a row; batch actions too.",
        devices: "Pairing, manual connection, discovery diagnostics and per-device send/receive switches.",
        favorites: "Keep what matters: groups, editing, drag ordering and copy back to the clipboard.",
        transfers: "Send files or folders; resume a broken transfer, retry a failed one, or measure speed.",
        chat: "Talk to devices on the same LAN without pairing first; messages and files arrive directly.",
        companion: "Reach the phone on another network; scan on the same network for the browser version.",
        internet: "Pair with a code across networks; devices, relay state and pending deliveries live here.",
        ai: "Six built-in tools — Claude Code, Codex, Cursor, Gemini CLI, Windsurf and Continue.",
        settings: "General, shortcuts, recording, files, sync, network, translation, security and recovery.",
      };

  const rows = [
    ["overview", 12.6], ["history", 18.2], ["devices", 23.9], ["favorites", 32.1],
    ["transfers", 43.5], ["chat", 37.8], ["companion", 52.0], ["internet", 57.7],
    ["ai", 63.4], ["settings", 69.0],
  ];

  const stage = document.querySelector(".app-preview");
  if (!stage) return;
  const picture = stage.querySelector(".preview-stage");
  const shots = Array.from(stage.querySelectorAll(".preview-shot"));
  const tabs = Array.from(stage.querySelectorAll(".preview-tabs [data-page-target]"));
  const sidebar = stage.querySelector(".preview-sidebar");
  const prev = stage.querySelector("[data-preview-prev]");
  const next = stage.querySelector("[data-preview-next]");
  const title = stage.querySelector("[data-preview-title]");
  const number = stage.querySelector("[data-preview-number]");
  const heading = stage.querySelector("[data-preview-heading]");
  const description = stage.querySelector("[data-preview-desc]");
  if (!shots.length || !tabs.length) return;
  let current = shots[0].dataset.page;

  function show(id) {
    const shot = shots.find((item) => item.dataset.page === id);
    if (!shot) return;
    current = id;
    for (const item of shots) item.classList.toggle("is-on", item === shot);
    for (const item of tabs) item.setAttribute("aria-selected", item.dataset.pageTarget === id ? "true" : "false");
    const tab = tabs.find((item) => item.dataset.pageTarget === id);
    const name = tab?.getAttribute("aria-label") || tab?.textContent?.replace(/^\d+\s*/, "") || "";
    if (title) title.textContent = name;
    if (heading) heading.textContent = name;
    if (number && tab) number.textContent = tab.querySelector("span")?.textContent || "";
    if (description) description.textContent = descriptions[id] || "";
    if (tab?.parentElement) {
      const list = tab.parentElement;
      const left = tab.offsetLeft - list.clientWidth / 2 + tab.clientWidth / 2;
      list.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
    }
  }

  if (sidebar && picture) {
    sidebar.addEventListener("click", (event) => {
      const box = picture.getBoundingClientRect();
      const y = ((event.clientY - box.top) / box.height) * 100;
      let closest = rows[0];
      for (const row of rows) {
        if (Math.abs(row[1] - y) < Math.abs(closest[1] - y)) closest = row;
      }
      show(closest[0]);
    });
  }

  for (const tab of tabs) {
    tab.addEventListener("click", (event) => {
      event.preventDefault();
      show(tab.dataset.pageTarget);
    });
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      event.preventDefault();
      const index = tabs.indexOf(tab);
      const next = event.key === "ArrowRight"
        ? (index + 1) % tabs.length
        : (index - 1 + tabs.length) % tabs.length;
      tabs[next].focus();
      show(tabs[next].dataset.pageTarget);
    });
  }

  function step(delta) {
    const index = tabs.findIndex((tab) => tab.dataset.pageTarget === current);
    const target = tabs[(index + delta + tabs.length) % tabs.length];
    show(target.dataset.pageTarget);
  }

  prev?.addEventListener("click", () => step(-1));
  next?.addEventListener("click", () => step(1));

  show(shots[0].dataset.page);
})();
