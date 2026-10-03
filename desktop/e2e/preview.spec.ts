/**
 * Every page, with a host behind it, photographed.
 *
 * The layout spec next door covers the one state a browser can reach on its own
 * — no native host, everything disabled.  This one covers the states a reader
 * actually spends time in: `host.ts` stands in for the sidecar, so each page
 * renders its rows, chips and counters for real.
 *
 * It asserts the two things a page can get wrong without anyone noticing in a
 * unit test — a console error, and a body that scrolls sideways — and writes a
 * screenshot of each page for a human to look at.
 */

import { expect, test, type Page } from "@playwright/test";
import { LANGUAGE, fixtures } from "./host";
import { installPreviewHost } from "./install-host";

/** The sidebar's own labels, in both languages, because the sweep has to find
 *  the tab by the name the window is currently showing.  `T` picks by the
 *  language the preview host reports, so a run with `CLIPSYNC_E2E_LANG=en`
 *  photographs the same eight pages in English and the three tests that assert
 *  Chinese behaviour below are simply not the ones being run. */
const T = (zh: string, en: string) => (LANGUAGE === "en" ? en : zh);

/** One page of the sweep, and whatever has to happen before it is worth
 *  photographing.
 *
 *  Most pages are their own picture the moment they open.  The AI config page is
 *  not: it opens on its tool catalog, and the two halves worth showing — this
 *  machine's own files, and a peer's — are behind a sub-tab and, for the local
 *  one, behind a button, because that walk reads every configured directory on
 *  disk and is deliberately not done on the way in.  A folder list also opens
 *  collapsed, so the skills a reader came for need a click to unfold. */
type PageEntry = {
  id: string;
  label: string;
  /** A sub-tab of the page, by its own label. */
  sub?: string;
  /** A control on the page, by its own label, pressed after `sub`. */
  action?: string;
  /** Folders to unfold, by name.
   *
   *  Only where the whole point of the shot is what is *inside* a folder — and
   *  only as many as the list's own 340px cap leaves room for, because that cap
   *  holds whatever the window is: unfolding one row more than fits does not
   *  show it, it just puts a half-row at the fold. */
  folders?: string[];
};

const PAGES: PageEntry[] = [
  { id: "overview", label: T("概览", "Overview") },
  { id: "history", label: T("剪贴板历史", "Clipboard History") },
  { id: "devices", label: T("设备", "Devices") },
  { id: "favorites", label: T("收藏库", "Favorites") },
  { id: "transfers", label: T("文件传输", "File Transfer") },
  { id: "chat", label: T("附近聊天", "Nearby Chat") },
  {
    id: "ai",
    label: T("AI 配置", "AI config"),
    sub: T("本机配置", "This machine"),
    action: T("读取本机配置", "Read local config"),
  },
  { id: "settings", label: T("设置", "Settings") },
];

async function open(page: Page, width: number, height = 900) {
  const failures: string[] = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    // The page carries no favicon link — it never needs one, because the real
    // window is a native window whose icon comes from `tauri.conf.json`.  A
    // browser asks for `/favicon.ico` anyway and logs the miss as an error, and
    // the message text does not name the URL, so the location does.
    if (message.location().url.includes("favicon.ico")) return;
    failures.push(message.text());
  });
  page.on("pageerror", (error) => failures.push(error.message));
  await page.addInitScript(installPreviewHost, fixtures);
  await page.setViewportSize({ width, height });
  await page.goto("/");
  // The window opens on the overview, and it is only a page with a host behind
  // it: the heading is the fixture having arrived, not merely the app having
  // mounted.
  await expect(page.getByRole("heading", { name: T("概览", "Overview"), exact: true })).toBeVisible();
  await expect(page.getByText(T("概览暂不可用", "Overview unavailable"))).toHaveCount(0);
  return failures;
}

/** Visit every page, checking nothing overflows and writing a screenshot. */
async function sweep(page: Page, prefix: string) {
  const overflowed: string[] = [];
  for (const entry of PAGES) {
    await page.getByRole("button", { name: entry.label, exact: true }).click();
    if (entry.sub) await page.getByRole("button", { name: entry.sub, exact: true }).click();
    if (entry.action) await page.getByRole("button", { name: entry.action, exact: true }).click();
    for (const folder of entry.folders || []) {
      // Every folder of that name, because two tools can each have one — and
      // only the fold chevrons, whose accessible name is this label.  The name
      // beside each chevron carries the same words as a `title`, not as an
      // `aria-label`, so it is named by its own text and is not matched here.
      const chevrons = await page
        .getByRole("button", { name: T(`展开或折叠 ${folder}`, `Expand or collapse ${folder}`), exact: true })
        .all();
      // Loud when there is nothing to click: a folder that was renamed would
      // otherwise leave the screenshot showing a collapsed tree, which is not a
      // failure any assertion here would notice.
      expect(chevrons.length, `no folder named ${folder}`).toBeGreaterThan(0);
      for (const chevron of chevrons) await chevron.click();
    }
    await page.waitForTimeout(150);
    const past = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (past > 0) overflowed.push(`${entry.id} +${past}px`);
    await page.screenshot({
      path: `test-results/${LANGUAGE}/${prefix}-${entry.id}.png`,
      fullPage: true,
    });
  }
  return overflowed;
}

test("every page renders at desktop width", async ({ page }) => {
  const failures = await open(page, 1280);
  expect(await sweep(page, "page")).toEqual([]);
  expect(failures).toEqual([]);
});

test("the narrow window stacks instead of scrolling sideways", async ({ page }) => {
  const failures = await open(page, 390, 844);
  expect(await sweep(page, "narrow")).toEqual([]);
  expect(failures).toEqual([]);
});

/** A conversation with a backlog, which is the only state the chat pane's own
 *  height is exercised in.
 *
 * The page is a two-column grid whose right half is a four-band stack — head,
 * an optional invite band, messages, composer — and every level of that has to
 * hand its height down rather than take it from its content.  The message list
 * is the scroll container, so the moment the height came from the content
 * instead, three things broke together and none of them showed up in a
 * screenshot of an empty conversation: the list grew to its full scroll height
 * and never scrolled, the pane ran off the bottom of the window, and the
 * composer, sitting in the row that had been left over, went below the fold
 * with it.  All three are asserted here, plus the one thing a working scroll
 * container still gets wrong on its own — a conversation has to open on its
 * newest message, not its oldest.
 */
test("a conversation with a backlog keeps its composer in the window", async ({ page }) => {
  const failures = await open(page, 1280);
  await page.getByRole("button", { name: "附近聊天", exact: true }).click();
  await expect(page.locator(".chat-composer")).toBeVisible();
  const desk = await page.evaluate(() => {
    const list = document.querySelector(".chat-messages") as HTMLElement;
    return {
      composerBottom: Math.round(document.querySelector(".chat-composer")!.getBoundingClientRect().bottom),
      viewport: window.innerHeight,
      client: list.clientHeight,
      scroll: list.scrollHeight,
      fromBottom: Math.round(list.scrollHeight - list.scrollTop - list.clientHeight),
    };
  });
  expect(desk.composerBottom).toBeLessThanOrEqual(desk.viewport);
  expect(desk.scroll).toBeGreaterThan(desk.client);
  expect(desk.fromBottom).toBeLessThanOrEqual(2);
  await expect(page.locator(".chat-message").last()).toBeInViewport();
  // The other half of that: the list follows the conversation only while it is
  // at its end.  The page polls every 1.5s and replaces the array on every tick
  // whether or not anything arrived, so a reader who has scrolled back to the
  // top of the conversation is dragged to the bottom twice a second unless the
  // list knows the difference.
  await page.locator(".chat-messages").evaluate((list) => { list.scrollTop = 0; });
  await page.waitForTimeout(2000);
  expect(await page.locator(".chat-messages").evaluate((list) => Math.round(list.scrollTop))).toBe(0);
  expect(failures).toEqual([]);

  // Stacked, the conversation is the row underneath the session list and is the
  // one that has to take what is left — so the composer has to stay reachable
  // there too.
  await page.setViewportSize({ width: 390, height: 844 });
  const narrow = await page.evaluate(() => ({
    composerBottom: Math.round(document.querySelector(".chat-composer")!.getBoundingClientRect().bottom),
    viewport: window.innerHeight,
    past: document.documentElement.scrollWidth - window.innerWidth,
  }));
  expect(narrow.composerBottom).toBeLessThanOrEqual(narrow.viewport);
  expect(narrow.past).toBe(0);
  await page.screenshot({ path: "test-results/narrow-chat-backlog.png" });
});

/** The same pane in the one state that shows every band at once.
 *
 * The invite band is optional, and it is the band whose absence moved every row
 * below it up one — the failure the test above catches from the other side.  So
 * this is that pane with the band present: it sits above the messages, the
 * conversation is still empty because the relay holds it until the fingerprint
 * is answered, and the composer is not offered until then either.
 */
test("an invitation shows its band above an empty conversation", async ({ page }) => {
  const failures = await open(page, 1280);
  await page.getByRole("button", { name: "附近聊天", exact: true }).click();
  await page.locator(".chat-session", { hasText: "办公室的那台旧笔记本" }).click();
  const band = page.locator(".chat-invite");
  await expect(band).toBeVisible();
  await expect(band).toContainText("邀请你聊天");
  await expect(band.getByRole("button", { name: "接受" })).toBeVisible();
  await expect(band.getByRole("button", { name: "拒绝" })).toBeVisible();
  await expect(page.locator(".chat-composer")).toHaveCount(0);
  await expect(page.locator(".chat-messages")).toContainText("还没有消息");
  const where = await page.evaluate(() => {
    const box = (selector: string) => document.querySelector(selector)!.getBoundingClientRect();
    const head = box(".chat-conversation > .chat-heading");
    const invite = box(".chat-invite");
    const list = box(".chat-messages");
    return {
      // Head, band, messages: the order the rows are declared in.
      stacked: head.bottom <= invite.top && invite.bottom <= list.top,
      paneBottom: Math.round(box(".chat-conversation").bottom),
      viewport: window.innerHeight,
      past: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
  expect(where.stacked).toBe(true);
  expect(where.paneBottom).toBeLessThanOrEqual(where.viewport);
  expect(where.past).toBe(0);
  expect(failures).toEqual([]);
  await page.screenshot({ path: "test-results/page-chat-invite.png" });
});

/** The removed devices, which every screenshot above leaves below the fold.
 *
 * This is the one row that carries two *labelled* buttons rather than a strip
 * of icons — the widest thing a device row ever holds — so it is the row a
 * narrow window would break first, and the only place the page draws its
 * destructive control.  Both facts are asserted: the row fits, and that
 * control's hairline is its own colour rather than the row's, which is one
 * CSS specificity away from silently reverting to grey.
 */
/** The retry count, which is the one thing that tells a peer being reconnected
 *  apart from a peer that is gone.
 *
 * Both rows are paired and neither is online; only one of them is being worked
 * on, and the difference the reader sees is 正在同步 against 已配对 — the same
 * two words the rest of the list uses.
 *
 * The *count* is in the chip's title rather than in its text, which is where it
 * moved when the state was collapsed from two chips into one: 本地·在线 and
 * 重连中 2/5 were never things a reader scanned the list for, and the tooltip is
 * where they went.  This test asserted the text until it was rewritten here,
 * which means it had been failing since that change rather than proving
 * anything about it — so the assertion is on the attribute the count actually
 * lives in.
 */
test("a device being retried says how far along it is", async ({ page }) => {
  const failures = await open(page, 1280);
  await page.getByRole("button", { name: T("设备", "Devices"), exact: true }).click();
  const retrying = page.locator(".device-row", { hasText: "家里的台式机" }).first();
  const chip = retrying.locator(".channel").first();
  await expect(chip).toHaveAttribute("title", T("重连中 3/10", "Reconnecting 3/10"));
  await expect(chip).toHaveText(T("正在同步", "Syncing"));

  const settled = page.locator(".device-row", { hasText: "Mac mini" }).first();
  // The peer that is not being worked on reads as paired, not as syncing, and
  // carries no count at all: the two rows differ in the two ways they should.
  await expect(settled.locator(".channel").first()).toHaveText(T("已配对", "Paired"));
  await expect(settled.locator(".channel").first()).toHaveAttribute(
    "title",
    // Built, not translated as one string: the chip is `${t("本地")}·${state}`,
    // so the English form has no spaces around the separator.
    T("本地·离线", "Local·offline"),
  );
  expect(failures).toEqual([]);
  await page.screenshot({ path: "test-results/page-devices-retrying.png" });
});

/** The unread count in the sidebar, which is the only sign of a waiting
 *  message that reaches a reader who is not already on the chat page. */
test("the sidebar counts unread chat from every page", async ({ page }) => {
  const failures = await open(page, 1280);
  const count = page.locator("nav .nav-count");
  // The fixture's sessions carry 2, 0 and 1 unread.
  await expect(count).toHaveText("3");
  await page.getByRole("button", { name: "剪贴板历史", exact: true }).click();
  await expect(count).toHaveText("3");
  expect(failures).toEqual([]);
});

/** The two lines the transfers page reads out rather than lists: the speed
 *  test's verdict on its own number, and the history card's summary of itself.
 *
 * Both come from the legacy panel — it graded a measurement on fixed thresholds
 * and counted the history under the card's header — and both are colour-graded
 * chips, which is the part a screenshot shows and a unit test does not: the
 * grading has to survive the cascade on a page that has both a neutral chip and
 * a red one.
 */
test("the transfers page grades its speed and counts its history", async ({ page }) => {
  const failures = await open(page, 1280);
  await page.getByRole("button", { name: "文件传输", exact: true }).click();
  const quality = page.locator(".speed-quality");
  await expect(quality).toHaveText("快速");
  await expect(page.locator(".speed-test")).toContainText("42.5 MB/s");
  // 52 MB + 2 KB + 150 MB, the fixture's three rows; two of them completed.
  await expect(page.locator(".transfer-history-stats")).toHaveText("已完成 3 个 · 2 成功 · 1 失败 · 200.0 MB");
  const tones = await page.evaluate(() => ({
    fast: getComputedStyle(document.querySelector(".speed-quality--fast")!).color,
    page: getComputedStyle(document.querySelector(".transfer-history-stats")!).color,
  }));
  // Graded rather than muted: the whole point of the chip is that the word is
  // not the same grey as the sentence it sits in.
  expect(tones.fast).not.toBe(tones.page);
  expect(failures).toEqual([]);
  await page.screenshot({ path: "test-results/page-transfers-summary.png" });
});

/** The age on an activity row, which has to read across rather than down.
 *
 * A feed row's last two cells are its mark and its age, and the mark is only on
 * the rows that have one — a tick for the clip last copied, a pin for a pinned
 * one.  A `v-if` with no `v-else` leaves no box behind, so on every row with
 * neither, the age was the next child in line and the grid handed it the mark's
 * 14px column: one character wide, standing on end.  Nothing about that is
 * visible to a unit test — the row's markup is correct, the stylesheet is
 * correct, and it is the grid's auto-placement that puts them together wrong —
 * so it is measured here, on every row, rather than trusted to stay put.
 */
test("every activity row's age reads across, not down", async ({ page }) => {
  const failures = await open(page, 1280);
  const ages = await page.evaluate(() =>
    [...document.querySelectorAll(".overview-feed-age")].map((age) => {
      const box = age.getBoundingClientRect();
      return {
        text: (age.textContent || "").trim(),
        width: Math.round(box.width),
        height: Math.round(box.height),
      };
    }),
  );
  // The fixture's feed holds six rows, and they do not all carry a mark.
  expect(ages.length).toBeGreaterThan(1);
  for (const age of ages) {
    // One line. A stacked age — the failure — is two, and a 13px line is ~20.
    expect(age.height, age.text).toBeLessThan(30);
    // The width of the whole age, not of one character of it.
    expect(age.width, age.text).toBeGreaterThan(20);
  }
  expect(failures).toEqual([]);
});

/** The dark theme, which nothing above photographs.
 *
 * Every other shot in this file is taken under whatever the runner's colour
 * scheme is — light, on CI — so half of this stylesheet has never been looked at.
 * That matters most for the kind glyphs, which are four colours chosen to stay
 * legible against the page in *both* themes and which are the only place in the
 * window where a colour carries meaning rather than decoration.
 *
 * Asserted rather than merely photographed, because a token that was added to the
 * light block and forgotten in the dark one does not look broken — it looks like
 * a slightly dimmer glyph, which is exactly the kind of thing a screenshot review
 * waves through.  So the check is that the row colours actually changed between
 * the two themes, which is false the moment a dark block misses a token.
 */
test("the dark theme paints the kind glyphs and changes their colour", async ({ page }) => {
  const failures = await open(page, 1280);
  await page.getByRole("button", { name: T("剪贴板历史", "Clipboard History"), exact: true }).click();
  await expect(page.locator(".history-kind").first()).toBeVisible();

  const kindColours = () =>
    page.evaluate(() =>
      [...document.querySelectorAll(".history-kind")].map(
        (node) => getComputedStyle(node).color,
      ),
    );

  const light = await kindColours();
  await page.emulateMedia({ colorScheme: "dark" });
  await page.waitForTimeout(100);
  const dark = await kindColours();

  expect(light.length).toBeGreaterThan(0);
  // A glyph painted in the inheriting text colour would mean the token never
  // resolved, in either theme.
  const pageColour = await page.evaluate(
    () => getComputedStyle(document.body).color,
  );
  for (const colour of light) expect(colour).not.toBe(pageColour);
  // And at least one glyph has to differ between the themes, or the dark tokens
  // are missing and the light ones are being used on a dark page.
  expect(dark).not.toEqual(light);

  await page.screenshot({ path: `test-results/${LANGUAGE}/dark-history.png` });
  expect(failures).toEqual([]);
});

test("the removed-device row fits a narrow window and keeps its own hairline", async ({ page }) => {
  const failures = await open(page, 390, 844);
  await page.getByRole("button", { name: "设备", exact: true }).click();
  await page.getByRole("heading", { name: "已移除的设备" }).scrollIntoViewIfNeeded();
  const purge = page.getByRole("button", { name: "彻底删除", exact: true });
  await expect(purge).toBeVisible();
  const both = await page.evaluate(() => {
    const buttons = [...document.querySelectorAll(".archived-devices .row-actions button")];
    const border = (element: Element) => getComputedStyle(element).borderTopColor;
    const row = document.querySelector(".archived-devices .row-actions")!.getBoundingClientRect();
    return {
      restore: border(buttons[0]),
      purge: border(buttons[1]),
      right: Math.round(row.right),
      past: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
  expect(both.purge).not.toBe(both.restore);
  // Inside the content column, which ends at the window's own edge.
  expect(both.right).toBeLessThanOrEqual(390);
  expect(both.past).toBe(0);
  expect(failures).toEqual([]);
  await page.screenshot({ path: "test-results/narrow-devices-removed.png" });
});


/** The reading width, which nothing asserted and which only shows up wide.
 *
 * Every screenshot above is taken at 1280, where the content column is already
 * narrower than the ceiling and the ceiling therefore does nothing.  The bug it
 * fixes only exists at 1800 and up: with no ceiling, a device row put its name and
 * state in the left quarter and its buttons against the far right edge with about
 * 900px of nothing between, so the row stopped reading as a row.
 *
 * Asserted at 1800 rather than 1280 for exactly that reason, and asserted as a
 * *relationship* rather than as a pixel count: the row must be no wider than the
 * ceiling, it must be centred in the content column, and the toolbar above it must
 * still reach the window's edge.  That last one is the mistake the first attempt
 * made -- capping the full-bleed bands left a bare strip of page colour beside
 * them -- so it is the assertion most worth having.
 */
test("the row pages stop growing at a reading width", async ({ page }) => {
  const failures = await open(page, 1800, 1000);
  for (const label of [T("设备", "Devices"), T("文件传输", "File Transfer"), T("收藏库", "Favorites")]) {
    await page.getByRole("button", { name: label, exact: true }).click();
    await page.waitForTimeout(120);
    const geometry = await page.evaluate(() => {
      const ceiling = parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue("--content-width"),
      );
      const content = document.querySelector(".content")!.getBoundingClientRect();
      // The capped block, whichever of the row pages this is.  Named rather than
      // guessed at with a bare `!`: a selector that matches nothing should fail
      // with the page it was looking at, not with "cannot read properties of null".
      const capped = document.querySelector(
        ".device-list, .transfers-view .card, .favorites-view, .history-list",
      );
      if (!capped) throw new Error("no capped block on this page: " + location.hash);
      const row = capped.getBoundingClientRect();
      // The sticky band, when the page has one.  Settings does not, so this is
      // read as "the widest band present" rather than assumed to exist.
      const band = document.querySelector(".toolbar, header");
      const toolbar = band!.getBoundingClientRect();
      return {
        ceiling,
        rowWidth: Math.round(row.width),
        contentWidth: Math.round(content.width),
        // Distance from each edge of the content column to the row.
        leftGap: Math.round(row.left - content.left),
        rightGap: Math.round(content.right - row.right),
        toolbarWidth: Math.round(toolbar.width),
        window: window.innerWidth,
      };
    });
    // The ceiling holds, and it is what the row is sized to when it applies.
    expect(geometry.ceiling, label).toBeGreaterThan(0);
    expect(geometry.rowWidth, label).toBeLessThanOrEqual(geometry.ceiling + 1);
    // Wide enough that the ceiling is actually doing something.
    expect(geometry.contentWidth, label).toBeGreaterThan(geometry.ceiling);
    // Centred, not pinned left with the slack on one side.
    expect(Math.abs(geometry.leftGap - geometry.rightGap), label).toBeLessThanOrEqual(2);
    // And the bands still reach the window's edge, which is what the first
    // attempt at this got wrong.
    expect(geometry.toolbarWidth, label).toBeGreaterThan(geometry.ceiling);
  }
  expect(failures).toEqual([]);
});


/** The type scale, which is only real if the names resolve.
 *
 * Every font-size in the stylesheet now goes through a token.  A token that was
 * never defined is not a parse error: the declaration is dropped and the element
 * inherits, which leaves text at a plausible size and a screenshot that looks fine.
 * So each rung is compared against the value the token claims, read from the same
 * custom property the stylesheet uses.
 */
test("the type scale resolves to the sizes it names", async ({ page }) => {
  const failures = await open(page, 1280);
  await page.getByRole("button", { name: T("概览", "Overview"), exact: true }).click();
  await page.waitForTimeout(120);

  const resolved = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    const token = (name: string) => root.getPropertyValue(name).trim();
    const computed = (selector: string) => {
      const node = document.querySelector(selector);
      return node ? getComputedStyle(node).fontSize : null;
    };
    return {
      // token name -> the value it declares
      tokens: {
        "--text-title": token("--text-title"),
        "--text-brand": token("--text-brand"),
        "--text-stat": token("--text-stat"),
        "--text-section": token("--text-section"),
        "--text-heading": token("--text-heading"),
        "--text-quiet": token("--text-quiet"),
        "--text-meta": token("--text-meta"),
        // The root's own size, which everything else inherits from.
        "--text-base": token("--text-base"),
      },
      // A rung of the scale, and an element that should be on it.
      checks: [
        ["--text-title", computed("h1")],
        ["--text-brand", computed(".brand")],
        ["--text-stat", computed(".overview-stat strong")],
        ["--text-heading", computed(".card > h2, .settings-section > h2, .card-head")],
        ["--text-quiet", computed(".note")],
        ["--text-meta", computed(".bottom-status")],
        ["--text-base", computed("body")],
      ] as [string, string | null][],
    };
  });

  // Every token is defined at all -- the failure this test exists for.
  for (const [name, value] of Object.entries(resolved.tokens)) {
    expect(value, `${name} is not defined`).not.toBe("");
  }
  // And the elements that ask for a rung are actually on it.
  for (const [name, size] of resolved.checks) {
    const expected = resolved.tokens[name as keyof typeof resolved.tokens];
    expect(size, `${name}: expected ${expected}`).toBe(
      // A token declared in `px` reads back as px; both sides normalised.
      expected.replace(/px$/, "px"),
    );
  }
  expect(failures).toEqual([]);
});


/** The overview is a dashboard, so the thing it is for has to be on the screen.
 *
 * It was five full-width bands -- 1027px of content in the 761px column a 1280x900
 * window leaves -- and the activity card sat entirely below the fold at the default
 * size.  The bands are now two columns, which is 924px, and the feed's own top is
 * inside the viewport.
 *
 * Asserted as three relationships rather than as pixel counts, so a padding change
 * does not fail it while a return to a single column does:
 *   * at desktop width the body is two columns;
 *   * the feed begins above the fold, which is the point of the change;
 *   * the two columns are within a card's height of each other, which is what stops
 *     one of them ending 500px above the other with bare page beneath it -- the
 *     first attempt at this left exactly that.
 */
test("the overview keeps its activity above the fold", async ({ page }) => {
  const failures = await open(page, 1280, 900);
  const layout = await page.evaluate(() => {
    const content = document.querySelector(".content") as HTMLElement;
    const pair = document.querySelector(".overview-pair") as HTMLElement | null;
    const cols = [...document.querySelectorAll(".overview-col")] as HTMLElement[];
    const feed = document.querySelector(".overview-feed") as HTMLElement | null;
    return {
      columns: pair ? getComputedStyle(pair).gridTemplateColumns.split(" ").length : 0,
      heights: cols.map((c) => Math.round(c.getBoundingClientRect().height)),
      fold: content.getBoundingClientRect().top + content.clientHeight,
      feedTop: feed ? Math.round(feed.getBoundingClientRect().top) : null,
    };
  });
  expect(layout.columns, "the overview body is not two columns").toBe(2);
  expect(layout.heights.length).toBe(2);
  expect(layout.feedTop, "no feed").not.toBeNull();
  // The whole point: the rows are on the screen without scrolling.
  expect(layout.feedTop!, "the feed starts below the fold").toBeLessThan(layout.fold);
  // And neither column is left holding a screenful of nothing.
  expect(Math.abs(layout.heights[0] - layout.heights[1])).toBeLessThan(340);
  expect(failures).toEqual([]);
});


/** The conversation has to be readable in the narrow window, not merely reachable.
 *
 * The existing narrow checks are about the composer and the last message being in
 * view, and a message list 95px tall satisfies all of them -- so a rail taking 46vh
 * of a 634px column left about two lines of conversation and nothing noticed.  This
 * asserts the list is a usable share of the pane rather than a sliver.
 */
test("the narrow chat window keeps a readable conversation", async ({ page }) => {
  const failures = await open(page, 390, 844);
  await page.getByRole("button", { name: T("附近聊天", "Nearby Chat"), exact: true }).click();
  await expect(page.locator(".chat-messages")).toBeVisible();
  const share = await page.evaluate(() => {
    const list = document.querySelector(".chat-messages") as HTMLElement;
    const pane = document.querySelector(".chat-conversation") as HTMLElement;
    const rail = document.querySelector(".chat-sessions") as HTMLElement;
    return {
      list: Math.round(list.clientHeight),
      pane: Math.round(pane.clientHeight),
      rail: Math.round(rail.clientHeight),
      // A row is about 34px, so this is how many fit.
      rows: Math.round(list.clientHeight / 34),
    };
  });
  // At least four messages' worth, against the two it was.
  expect(share.rows, `only ${share.rows} rows in ${share.list}px`).toBeGreaterThanOrEqual(4);
  // And the list is a real share of the pane it sits in, not the leftover.
  expect(share.list / share.pane).toBeGreaterThan(0.4);
  expect(failures).toEqual([]);
});
