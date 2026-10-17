/**
 * The page-side half of the preview host: what gets injected before the app
 * loads.  See `host.ts` for why this exists.
 *
 * Playwright serializes this function by source and runs it in the page, so
 * everything it touches has to be *inside* it — a module-level constant or
 * helper is simply not in scope over there, and the script fails with a
 * ReferenceError that surfaces as a page that renders nothing.
 */

export function installPreviewHost(fixtures: Record<string, unknown>): void {
  type Row = {
    id: string;
    timestamp: number;
    preview: string;
    content_type: string;
    pinned: boolean;
    source_name: string;
    /** The id `source_name` stands for; "" for a clip captured here. */
    source_device: string;
    transport: string;
    source_app: string;
    source_title: string;
    paste_count: number;
  };

  // The run's language, and the three machines this sample is about — handed in
  // by `host.ts` because nothing declared in *this* file's module scope is in
  // scope here.  See its own `S` for why the sample is not written once in
  // Chinese and shipped into an English window.
  const sample = fixtures.sample as { lang: string; local: string; mac: string; mini: string };
  const S = (zh: string, en: string) => (sample.lang === "en" ? en : zh);

  const APPS = ["Code.exe", "chrome.exe", "WINWORD.EXE", "WindowsTerminal.exe", "explorer.exe"];
  const TITLES = [
    "rpc.py — copyboard",
    S("Tauri 迁移对照表 - Google Chrome", "Tauri migration checklist - Google Chrome"),
    S("季度总结.docx - Word", "Quarterly summary.docx - Word"),
    "sukai@workstation: ~/copyboard",
    S("下载", "Downloads"),
  ];
  // The first is this machine, by the name the sidecar would label it with: a
  // clip captured here carries no `source_device`, and `source_name` falls back
  // to the local device's own name (`use_cases/history.py`) — never the literal
  // 本机, which no row has ever shown.  The other two are machines, because a
  // peer is one: the browser companion is a token and an HTTP page, so a phone
  // appears in no device list at all.
  const SOURCES = [sample.local, sample.mac, sample.mini];
  /** The device ids behind those names, in the same order.  A row's label is
   * what a reader sees; this is what a request has to name, and a row captured
   * here has none.  The two ids match `list_devices`, so a remote-file row's
   * 下载 button is enabled against the same device the page is showing. */
  const SOURCE_IDS = ["", "3a91f0c4-77b2-4d5e-8f01-6b2c9d4e7a30", "b2d4e6f8-1a3c-5e70-9b81-2d4f6a8c0e13"];
  /** The kind chips the history page draws, and the numbers a chip's own click
   * yields — `KINDS` in `internal/application/use_cases/history.py`. */
  const KINDS = ["all", "text", "image", "file", "link"];
  /** Clips, each with the type the sidecar would have stored it as.  Paired
   * rather than walked by one index across two lists: cycled separately, the
   * third clip is a URL carrying an 图片 chip, and every screenshot of the
   * history page is a small lie.  The type names are the stored ones, uppercase
   * — the window uppercases before it labels anything, so a lowercase fixture
   * would pass whether or not that mapping worked.  `HTML` and `RTF` are the two
   * kinds with no label of their own; the window shows the format's name, which
   * is a name in either language. */
  const CLIPS: Array<{ text: string; type: string }> = [
    { text: "SELECT * FROM orders WHERE created_at > '2026-09-01' AND status = 'paid' ORDER BY created_at DESC", type: "TEXT" },
    { text: S("会议纪要 · 2026-09-14\n\n1. 迁移到 Tauri 的工作收尾\n2. 下周开始灰度\n3. 老版本继续维护到月底", "Meeting notes · 2026-09-14\n\n1. Wrapping up the move to Tauri\n2. Staged rollout starts next week\n3. The old build is maintained until month end"), type: "RTF" },
    { text: "https://github.com/kai3316/copyboard/pull/214", type: "URL" },
    { text: "docker compose -f docker-compose.prod.yml up -d --build", type: "TEXT" },
    { text: "D:\\copyboard\\desktop\\src-tauri\\src\\window.rs", type: "FILE" },
    { text: S("嗯，那就先这样，明天再看", "Fine, let's leave it there and look again tomorrow"), type: "TEXT" },
    { text: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH8kQ2vY0m3nR7pLxW9dT4bE6cJ1aZsV5oN8fG2hK0i", type: "TEXT" },
    { text: S("截图 2026-09-14 091233.png", "screenshot 2026-09-14 091233.png"), type: "IMAGE_PNG" },
    { text: "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff", type: "TEXT" },
    { text: S("本周待办：\n- [x] 桥接层补测\n- [ ] 概览页收尾\n- [ ] 发版说明", "This week:\n- [x] More tests for the bridge\n- [ ] Finish the overview page\n- [ ] Release notes"), type: "HTML" },
    // A file that lives on another device.  Its preview is the offer's own
    // summary — a name and a size, `file_ref.summary` — because that is all a
    // row can know about a file it does not have, and the row's one action is
    // 下载 rather than 复制.
    { text: S("季度总结（终版）.docx · 2.4 MB", "Quarterly summary (final).docx · 2.4 MB"), type: "FILE_REMOTE" },
  ];

  /** Rows are generated rather than listed so the fixture can be long enough to
   * page and short enough to read.  Anchored to the wall clock rather than to a
   * fixed date, so the relative times the rows show ("3 分钟前") stay plausible
   * however long this file sits here; everything else about a row is fixed.
   *
   *  The two cycles are deliberately coprime with each other and with the clip
   *  list: pinned rows float to the top, so a pin rule that shared a period with
   *  `CLIPS` (11 clips and `i % 11`, say) would put the *same* clip at the top
   *  of the page four times over — which is what the first version of this did,
   *  and it read as a broken list rather than as a pinned one. */
  function makeHistory(): Row[] {
    const now = Math.floor(Date.now() / 1000);
    const rows: Row[] = [];
    for (let i = 0; i < 47; i += 1) {
      const clip = CLIPS[i % CLIPS.length];
      // A file that lives on another device necessarily came from one, so those
      // rows take a peer rather than whatever the generic cycle lands on: the
      // cycle would hand some of them 本机, and a row captured on this machine
      // is a path, not an offer — nothing the sidecar ever writes.
      const source = clip.type === "FILE_REMOTE" ? 1 + (i % 2) : i % 3;
      rows.push({
        id: `h-${i + 1}`,
        timestamp: now - i * 137 - (i % 7) * 900,
        preview: clip.text.length > 160 ? `${clip.text.slice(0, 157)}…` : clip.text,
        content_type: clip.type,
        pinned: i % 13 === 5,
        source_name: SOURCES[source],
        source_device: SOURCE_IDS[source],
        transport: source === 0 ? "" : source === 1 ? "lan" : "relay",
        source_app: i % 4 === 0 ? "" : APPS[i % APPS.length],
        source_title: i % 4 === 0 ? "" : TITLES[i % TITLES.length],
        paste_count: i % 5 === 0 ? 0 : (i % 4) + 1,
      });
    }
    return rows;
  }

  /** Whether a row belongs to one of the chips, copied from the sidecar's own
   * `matches_kind`.  A chip has to mean here what it means there: 文本 reaches an
   * RTF clip, and 链接 reaches a URL that arrived as plain text rather than only
   * a row typed as one.  Equality on the type alone would put a different set of
   * rows behind each chip than the application does. */
  function matchesKind(row: Row, kind: string) {
    const type = row.content_type.toUpperCase();
    if (kind === "text") return type === "TEXT" || type === "HTML" || type === "RTF";
    if (kind === "image") return type === "IMAGE" || type === "IMAGE_PNG" || type === "IMAGE_EMF";
    // A file on another device is a file row here too: the 文件 chip is how a
    // reader looks for "the file I copied", and which machine it is on is the
    // row's own business — the sidecar's `matches_kind` groups them the same way.
    if (kind === "file") return type === "FILE" || type === "FILE_REMOTE";
    if (kind === "link") return type === "URL" || type === "LINK" || /^https?:\/\//i.test(row.preview);
    return true;
  }

  const rows = makeHistory();
  const calls: Array<{ cmd: string; args: Record<string, unknown> }> = [];
  const callbacks: Record<number, (message: unknown) => void> = {};
  const listeners: Array<{ event: string; id: number }> = [];
  let callbackSeq = 0;

  const scope = window as unknown as Record<string, unknown>;
  scope.isTauri = true;
  scope.__previewCalls = calls;
  /** Push a sidecar event into the app, as the native host would. */
  scope.__previewEmit = (event: string, payload: unknown) => {
    for (const entry of listeners) {
      if (entry.event !== event) continue;
      const callback = callbacks[entry.id];
      if (callback) callback({ event, id: entry.id, payload });
    }
  };

  function historyPage(args: Record<string, unknown>) {
    const query = String(args.query || "").trim().toLowerCase();
    const kind = String(args.kind || "all");
    const offset = Number(args.offset || 0);
    const limit = Number(args.limit || 30);
    // The sidecar searches everything a row shows — the words, the type, the
    // device, the application and that window's title — and counts the chips
    // under the search, so a chip's number is what clicking it yields.
    const found = query
      ? rows.filter((row) =>
          [row.preview, row.content_type, row.source_name, row.source_app, row.source_title]
            .join(" ")
            .toLowerCase()
            .includes(query))
      : rows;
    const counts: Record<string, number> = {};
    for (const name of KINDS) counts[name] = found.filter((row) => matchesKind(row, name)).length;
    const matched = found.filter((row) => matchesKind(row, kind));
    // Two stable passes, as the sidecar does it: the order that was asked for,
    // then pinned rows floated to the top of it.
    matched.sort((a, b) => (args.sort === "oldest" ? a.timestamp - b.timestamp : b.timestamp - a.timestamp));
    matched.sort((a, b) => Number(b.pinned) - Number(a.pinned));
    return {
      total: matched.length,
      offset,
      kind,
      sort: String(args.sort || "newest"),
      items: matched.slice(offset, offset + limit),
      counts,
      has_history: rows.length > 0,
    };
  }

  /** What the AI config page's local tree is built from: one entry per file the
   * tools keep, plus an `is_dir` entry for every folder so a skills directory
   * shows up as a folder to open rather than as a run of its own files.  The
   * paths are relative to their root, forward-slashed, and the root ids are the
   * ones `ai_profiles.TOOLS` names — the same pair `collect_roots` emits, so the
   * tree groups them the way the real walk does.
   *
   *  One tool, and just the items that fit: the page's list is a capped scroll
   *  region (`styles.css` — `max-height: 340px`), so a longer sample does not
   *  show more, it only puts a half-row at the fold.  Six top-level names is what
   *  the cap holds, and six is what a reader sees here — Claude Code's memory
   *  file, its settings, two skills, a command and an agent.  Codex and Gemini
   *  are still in the tool catalog next door; this machine simply has no
   *  directories for them, which is why the walk finds nothing under them. */
  const AI_FILES: Array<{ tool: string; root: string; rel_path: string; size: number; is_dir?: boolean }> = [
    { tool: "claude_code", root: "memory", rel_path: "CLAUDE.md", size: 4820 },
    { tool: "claude_code", root: "settings", rel_path: "settings.json", size: 2310 },
    { tool: "claude_code", root: "skills", rel_path: "code-review", size: 0, is_dir: true },
    { tool: "claude_code", root: "skills", rel_path: "code-review/SKILL.md", size: 1893 },
    { tool: "claude_code", root: "skills", rel_path: "code-review/checklist.md", size: 744 },
    { tool: "claude_code", root: "skills", rel_path: "release-notes", size: 0, is_dir: true },
    { tool: "claude_code", root: "skills", rel_path: "release-notes/SKILL.md", size: 2201 },
    { tool: "claude_code", root: "commands", rel_path: "commit.md", size: 512 },
    { tool: "claude_code", root: "agents", rel_path: "reviewer.md", size: 980 },
  ];

  /** The `/api/aiconfig/local` answer, in the shape `local_listing` returns. */
  function aiLocalListing() {
    const now = Math.floor(Date.now() / 1000);
    const entries = AI_FILES.map((file, index) => ({
      ...file,
      mtime: now - index * 5400,
      sha256: `a${index.toString(16).padStart(3, "0")}${"9f2c7b1d4e6a8c0f".repeat(3)}`,
    }));
    return {
      collected_at: now,
      tools: (fixtures.ai_profiles as { tools: Array<Record<string, unknown>> }).tools.filter(
        (tool) => tool.key === "claude_code" || tool.key === "codex" || tool.key === "gemini",
      ),
      custom_paths: [],
      roots: entries
        .filter((entry) => !entry.is_dir)
        .map((entry, index) => ({
          root_index: index, tool: entry.tool, root: entry.root, kind: "file",
          path: entry.rel_path, count: 1,
        })),
      entries,
    };
  }

  function aiInventory(args: Record<string, unknown>) {
    const peerId = String(args.peerId || "");
    const base = fixtures.ai_inventory as { peers?: Record<string, unknown> };
    // A read with no peer asked for leaves the peer map alone rather than
    // emptying it — the page keeps what is on screen for the same reason.
    return { ...(fixtures.ai_inventory as Record<string, unknown>), peers: base.peers || {},
      refreshed: args.refresh && peerId ? [peerId] : [] };
  }

  function favoritesPage(args: Record<string, unknown>) {
    const base = fixtures.list_favorites as { items: Array<Record<string, unknown>> };
    const query = String(args.query || "").trim().toLowerCase();
    const group = String(args.group || "");
    const matching = base.items.filter(
      (item) =>
        (!group || item.group === group) &&
        (!query ||
          String(item.title).toLowerCase().includes(query) ||
          String(item.preview).toLowerCase().includes(query)),
    );
    return { ...(fixtures.list_favorites as Record<string, unknown>), items: matching, total: matching.length };
  }

  function overview() {
    const base = fixtures.get_overview as Record<string, unknown>;
    return {
      ...base,
      history_count: rows.length + 1237,
      history_pinned: rows.filter((row) => row.pinned).length,
      // Through the same matcher the 图片 chip uses: a stored type is uppercase
      // (`IMAGE_PNG`), so comparing against a lowercase literal counted nothing
      // and the tile read 0 next to four pinned rows.
      history_images: rows.filter((row) => matchesKind(row, "image")).length,
      // The newest few history rows — the sidecar builds the feed with the history
      // page's own row helper, so a feed row is a history row and arrives carrying
      // its id, which is what the window copies, pins and opens a menu on.  This
      // projected four keys of its own (`text`/`type`/`time`/`pinned`), a shape the
      // window no longer knows: a feed row built from it had no clip to act on and
      // showed neither its text nor a real age.  The preview is cut to what the
      // feed's single line shows, as the sidecar cuts it.
      recent_items: rows.slice(0, 6).map((row) => ({ ...row, preview: row.preview.slice(0, 80) })),
    };
  }

  /** The chat's two reads, with their clock re-anchored: the sessions list says
   * how long ago each one was last spoken in, and a fixture pinned to a fixed
   * date reads as a year old the moment that date is behind us.
   *
   * An invitation still waiting on an answer has no conversation in it — the
   * relay holds both sides until the fingerprint is confirmed — so that one
   * session reads empty rather than borrowing the backlog.
   */
  const INVITED = "cs-3";
  function chatSessions() {
    const now = Math.floor(Date.now() / 1000);
    const base = fixtures.list_chat_sessions as { sessions: Array<Record<string, unknown>> };
    return {
      ...(fixtures.list_chat_sessions as Record<string, unknown>),
      sessions: base.sessions.map((session, index) => ({
        ...session,
        created_ts: now - 7200,
        last_activity_ts: now - (index === 0 ? 240 : 5400),
      })),
    };
  }

  function chatMessages(args: Record<string, unknown>) {
    const now = Math.floor(Date.now() / 1000);
    const base = fixtures.list_chat_messages as { messages: Array<Record<string, unknown>> };
    const count = base.messages.length;
    return {
      ...(fixtures.list_chat_messages as Record<string, unknown>),
      messages: args.sessionId === INVITED ? [] : base.messages.map((message, index) => ({
        ...message,
        ts: now - (count - index) * 90,
      })),
    };
  }

  const dynamic: Record<string, (args: Record<string, unknown>) => unknown> = {
    list_history: historyPage,
    list_favorites: favoritesPage,
    get_overview: overview,
    list_chat_sessions: chatSessions,
    list_chat_messages: chatMessages,
    ai_inventory: aiInventory,
  };
  // A read of this machine's own config walks this machine's disk, so there is
  // nothing to route by argument: whatever was asked for, the answer is what the
  // walk would have found.  The listing is a snapshot taken when this harness
  // seeded it, which is why it is built once rather than per call.
  const aiLocal = aiLocalListing();
  dynamic.ai_local = () => aiLocal;

  function resolve(cmd: string, args: Record<string, unknown>): unknown {
    if (cmd.startsWith("plugin:")) {
      if (cmd === "plugin:event|listen") {
        listeners.push({ event: String(args.event), id: Number(args.handler) });
        return listeners.length;
      }
      return null;
    }
    if (dynamic[cmd]) return dynamic[cmd](args);
    if (cmd in fixtures) return fixtures[cmd];
    // Loud rather than silent: a page that looks empty because a command went
    // unmocked is a page whose review was wasted.
    throw { code: "PREVIEW_UNMOCKED", message: `no fixture for ${cmd}`, retryable: false };
  }

  scope.__TAURI_INTERNALS__ = {
    transformCallback(callback: (message: unknown) => void) {
      callbackSeq += 1;
      callbacks[callbackSeq] = callback;
      return callbackSeq;
    },
    async invoke(cmd: string, args: Record<string, unknown> = {}) {
      calls.push({ cmd, args });
      return resolve(cmd, args);
    },
    convertFileSrc: (path: string) => path,
  };
}
