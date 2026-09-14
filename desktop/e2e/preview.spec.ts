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
 * Both rows are offline and paired; only one of them is being worked on.  The
 * chip is asserted on its own — the legacy panel's own words — and against the
 * settled row beside it, because a counter that read the same as 离线 would be
 * no answer at all.
 */
test("a device being retried says how far along it is", async ({ page }) => {
  const failures = await open(page, 1280);
  await page.getByRole("button", { name: "设备", exact: true }).click();
  const retrying = page.locator(".device-row", { hasText: "家里的台式机" }).first();
  await expect(retrying.locator(".channel", { hasText: "重连中" })).toContainText("重连中 3/10");
  const both = await page.evaluate(() => {
    const chip = (name: string) => {
      const row = [...document.querySelectorAll(".device-row")]
        .find(element => element.textContent?.includes(name));
      // Named rather than asserted-on: the `!` this used to carry turned a
      // renamed device into "cannot read properties of undefined", which says
      // nothing about which name went missing.
      if (!row) throw new Error(`no device row for ${name}`);
      const channel = row.querySelector(".channel--connecting, .channel--offline");
      if (!channel) throw new Error(`${name} carries no channel`);
      return getComputedStyle(channel).color;
    };
    // The retried one, against a peer that is also offline and paired: only the
    // first is being worked on, and 重连中 3/10 is the whole difference.
    return { retry: chip("家里的台式机"), settled: chip("Mac mini") };
  });
  expect(both.retry).not.toBe(both.settled);
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
