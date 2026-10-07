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

async function open(
  page: Page,
  width: number,
  height = 900,
  overrides: Record<string, unknown> = {},
) {
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
  // Whole commands, because that is the granularity the host speaks: a test that needs "no
  // password set" replaces `get_settings` rather than reaching inside it.
  await page.addInitScript(installPreviewHost, { ...fixtures, ...overrides });
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


/** The row pages stop stretching, which is what a maximised window showed.
 *
 * Nothing in this file rendered above 1280, and 1920 is where the fault is: the
 * content column is 1708px there, so a device row's name sits in the left quarter and
 * its buttons against the far right with ~900px of nothing between, and a note field
 * draws a 1550px box around a four-word note.  A page that never gets that wide cannot
 * show it, which is why every screenshot this was reviewed against looked fine.
 */
test("the row pages stop stretching at a maximised width", async ({ page }) => {
  const failures = await open(page, 1920, 1040);
  const rows: string[] = [];
  for (const [id, label] of [
    ["devices", T("设备", "Devices")],
    ["settings", T("设置", "Settings")],
    ["favorites", T("收藏库", "Favorites")],
    ["history", T("剪贴板历史", "Clipboard History")],
    ["transfers", T("文件传输", "File Transfer")],
  ] as [string, string][]) {
    await page.getByRole("button", { name: label, exact: true }).click();
    await page.waitForTimeout(120);
    const m = await page.evaluate(() => {
      const content = document.querySelector(".content") as HTMLElement;
      const header = document.querySelector("header") as HTMLElement;
      const root = getComputedStyle(document.documentElement);
      // The widest element that is a page container rather than a full-bleed band.
      const bands = ["toolbar", "error-band", "bottom-status", "notice-stack"];
      let widest = 0;
      for (const child of [...content.children]) {
        const cls = (child.className || "").toString();
        if (bands.some((b) => cls.includes(b))) continue;
        if (!(child as HTMLElement).offsetParent) continue;
        widest = Math.max(widest, Math.round(child.getBoundingClientRect().width));
      }
      return {
        contentW: Math.round(content.getBoundingClientRect().width),
        widest,
        ceiling: parseFloat(root.getPropertyValue("--content-width")),
        contentLeft: Math.round(content.getBoundingClientRect().left),
        headerLeft: Math.round(header.getBoundingClientRect().left),
      };
    });
    // The window is wider than the ceiling, so the ceiling is what is being tested.
    expect(m.contentW, id).toBeGreaterThan(m.ceiling);
    if (m.widest > m.ceiling + 1) {
      rows.push(`${id}: ${m.widest}px > ceiling ${m.ceiling}`);
    }
    // Left-aligned to the header, not centred: a centred cap leaves a blank band
    // under the header where the two edges part company.
    expect(Math.abs(m.contentLeft - m.headerLeft), `${id} alignment`).toBeLessThanOrEqual(2);
  }
  expect(rows).toEqual([]);
  expect(failures).toEqual([]);
});


/** The settings card is the size of the form in it, not the size of the window.
 *
 * It was 1060px wide holding 530px of form: a row measured 1014px for its 168px label,
 * 22px gap and 340px field, and the save bar was the plainest case -- a 111px button
 * alone in a 1060px bar, 87% of it empty.  This is the "row with one small component"
 * shape, and it only shows on a window wider than the 1280 the suite rendered at.
 */
test("the settings card holds its form rather than the window", async ({ page }) => {
  const failures = await open(page, 1920, 1040);
  await page.getByRole("button", { name: T("设置", "Settings"), exact: true }).click();
  await page.waitForTimeout(150);
  const m = await page.evaluate(() => {
    const box = (sel: string) => {
      const n = document.querySelector(sel) as HTMLElement | null;
      if (!n) return null;
      const r = n.getBoundingClientRect();
      return { left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width) };
    };
    const save = document.querySelector(".settings-save") as HTMLElement | null;
    const button = save?.querySelector("button") as HTMLElement | null;
    const row = document.querySelector(".setting") as HTMLElement | null;
    const input = row?.querySelector(".setting-control > input") as HTMLElement | null;
    const content = document.querySelector(".content") as HTMLElement;
    return {
      contentWidth: Math.round(content.getBoundingClientRect().width),
      card: box(".settings-section"),
      save: box(".settings-save"),
      buttonWidth: button ? Math.round(button.getBoundingClientRect().width) : 0,
      rowWidth: row ? Math.round(row.getBoundingClientRect().width) : 0,
      inputRight: input ? Math.round(input.getBoundingClientRect().right) : 0,
      rowLeft: row ? Math.round(row.getBoundingClientRect().left) : 0,
    };
  });
  // The window is wider than the card, so the card is a choice and not a constraint.
  expect(m.contentWidth).toBeGreaterThan(1400);
  expect(m.card).not.toBeNull();
  // The card is not more than about half again the content of one of its rows.
  const used = m.inputRight - m.rowLeft;
  expect(used / m.rowWidth, `a row uses ${Math.round((used / m.rowWidth) * 100)}% of itself`)
    .toBeGreaterThan(0.6);
  // And the save bar is not a wide box around one button.
  expect(m.buttonWidth / m.save!.width, "the save bar is mostly empty")
    .toBeGreaterThan(0.1);
  expect(m.save!.width).toBeLessThanOrEqual(m.card!.width + 1);
  expect(failures).toEqual([]);
});


/** A history row's selection box sits on its clip's first line, not in the row's middle.
 *
 * `.history-row` is `align-items: center` -- right for the timestamp and the buttons,
 * which are about the row -- and that put the checkbox at the row's centre while the
 * text started at the top.  On a one-line clip the two are 5px apart and it looks fine;
 * on the three-line HTML clip the box was **60px below the text it selects**.
 */
test("a history row's checkbox sits on its clip", async ({ page }) => {
  const failures = await open(page, 1280, 900);
  await page.getByRole("button", { name: T("剪贴板历史", "Clipboard History"), exact: true }).click();
  await expect(page.locator(".history-row").first()).toBeVisible();
  const drift = await page.evaluate(() => {
    const out: number[] = [];
    for (const row of [...document.querySelectorAll(".history-row")]) {
      const text = row.querySelector(".history-content p") as HTMLElement | null;
      const box = row.querySelector('input[type="checkbox"]') as HTMLElement | null;
      if (!text || !box) continue;
      const t = text.getBoundingClientRect();
      const b = box.getBoundingClientRect();
      // How far the box's centre is from the first line's centre.
      const firstLine = t.top + Math.min(22, t.height) / 2;
      out.push(Math.round(Math.abs(b.top + b.height / 2 - firstLine)));
    }
    return out;
  });
  expect(drift.length).toBeGreaterThan(3);
  // Two lines of text tall, the fault; a few pixels is a baseline.
  expect(Math.max(...drift), `a checkbox is ${Math.max(...drift)}px off its line`).toBeLessThan(12);
  expect(failures).toEqual([]);
});


/** The device row's sync switch rides the state chip's line, not the row's bottom.
 *
 * It was the last block in the row's stack, so on a paired device it sat below the note
 * field -- four lines from the device it applies to -- while the chip saying whether that
 * device was being synced sat at the top.  The two are one statement about one device.
 *
 * Asserted as "on the same line as the chip, and against the same right edge as the row's
 * buttons", which is what makes it read as part of that line rather than as another row.
 */
test("the device sync switch sits with the state it belongs to", async ({ page }) => {
  const failures = await open(page, 1280, 900);
  await page.getByRole("button", { name: T("设备", "Devices"), exact: true }).click();
  await expect(page.locator(".device-row").first()).toBeVisible();
  const where = await page.evaluate(() => {
    const row = document.querySelector(".device-row") as HTMLElement;
    const box = (sel: string) => {
      const n = row.querySelector(sel) as HTMLElement | null;
      return n ? n.getBoundingClientRect() : null;
    };
    const chips = box(".device-channels");
    const sync = box(".device-sync");
    const actions = box(".row-actions");
    const note = box(".device-note");
    if (!chips || !sync) return null;
    return {
      sameLine: Math.abs(sync.top - chips.top) < 30,
      // The switch's right edge against the buttons', which is the row's own right edge.
      rightAligned: actions ? Math.abs(sync.right - actions.right) < 20 : true,
      // And it is no longer below the note field.
      aboveNote: note ? sync.bottom <= note.top + 2 : true,
    };
  });
  expect(where).not.toBeNull();
  expect(where!.sameLine, "the switch is not on the chip's line").toBe(true);
  expect(where!.rightAligned, "the switch is not against the row's right edge").toBe(true);
  expect(where!.aboveNote, "the switch is still below the note field").toBe(true);
  expect(failures).toEqual([]);
});


/** A device row's "test connection" report costs one short line, and collides with nothing.
 *
 * Measured before: **50px** added to a 153px row for 18px of text -- the grid's 14px row
 * gap, the row's 16px padding above and below, and the line.  The report keeps its row (the
 * alternative rendered through the sync switch) and the padding that made it expensive is
 * trimmed, which brings it to 43px.
 *
 * The collision check is the part worth keeping.  It compares the report's band against the
 * chips, the switch and the note field -- the things that share its columns -- rather than
 * against one neighbour, because the earlier version of this measurement passed a layout
 * whose text ran through the switch.
 */
test("the connection-test report is one bounded line that collides with nothing", async ({ page }) => {
  const problems: string[] = [];
  for (const [w, h] of [[1707, 960], [1280, 900]] as [number, number][]) {
    const failures = await open(page, w, h);
    await page.getByRole("button", { name: T("设备", "Devices"), exact: true }).click();
    await page.waitForTimeout(150);
    const before = await page.evaluate(
      () => Math.round((document.querySelector(".device-row") as HTMLElement).getBoundingClientRect().height),
    );
    await page.locator(`[aria-label="${T("测试连接", "Test connection")}"]`).first().click();
    await page.waitForTimeout(250);

    const seen = await page.evaluate(() => {
      const row = document.querySelector(".device-row") as HTMLElement;
      const band = (sel: string) => {
        const n = row.querySelector(sel) as HTMLElement | null;
        if (!n) return null;
        const r = n.getBoundingClientRect();
        return { t: r.top, b: r.bottom, h: r.height };
      };
      const probe = band(".device-probe");
      const collisions: string[] = [];
      if (probe) {
        for (const [name, sel] of [
          ["chips", ".device-channels"],
          ["switch", ".device-sync"],
          ["note field", ".device-note"],
        ] as [string, string][]) {
          const other = band(sel);
          // They share the row's width, so overlapping bands is the collision.
          if (other && probe.t < other.b && other.t < probe.b) collisions.push(name);
        }
      }
      return {
        height: Math.round(row.getBoundingClientRect().height),
        probeHeight: probe ? Math.round(probe.h) : 0,
        text: (row.querySelector(".device-probe")?.textContent || "").trim(),
        collisions,
      };
    });

    expect(seen.text, `${w}px: the report is not shown`).not.toBe("");
    expect(seen.probeHeight, `${w}px: the report is not laid out`).toBeGreaterThan(8);
    if (seen.collisions.length) problems.push(`${w}px: overlaps ${seen.collisions.join(", ")}`);
    const delta = seen.height - before;
    if (delta > 46) problems.push(`${w}px: the row grew ${delta}px for a one-line report`);
    expect(failures).toEqual([]);
  }
  expect(problems).toEqual([]);
});


/** The favourites sidebar is as tall as its groups, not as tall as the workspace.
 *
 * Measured before: a **640px** bordered column holding **223px** of group names, so **417px**
 * of a box that draws an edge was empty.  Empty space inside a box with a border reads as a
 * thing that failed to load; the same space with no box around it reads as the page being
 * short, which is what it is.
 *
 * Asserted as "the box is close to its content", not as a number, because the number depends
 * on how many groups there are.
 */
test("the favourites sidebar is as tall as the groups in it", async ({ page }) => {
  const failures = await open(page, 1707, 960);
  await page.getByRole("button", { name: T("收藏库", "Favorites"), exact: true }).click();
  await expect(page.locator(".favorites-groups")).toBeVisible();

  const seen = await page.evaluate(() => {
    const sidebar = document.querySelector(".favorites-groups") as HTMLElement;
    const list = document.querySelector(".favorites-list") as HTMLElement;
    const box = sidebar.getBoundingClientRect();
    let filled = 0;
    for (const kid of [...sidebar.children]) {
      const r = (kid as HTMLElement).getBoundingClientRect();
      if (r.height > 0) filled = Math.max(filled, r.bottom - box.top);
    }
    return {
      sidebarHeight: Math.round(box.height),
      filled: Math.round(filled),
      listHeight: Math.round(list.getBoundingClientRect().height),
    };
  });

  // The sidebar's own padding is 14px a side, so a short group list still has real slack.
  const empty = seen.sidebarHeight - seen.filled;
  expect(empty, `the sidebar box is ${empty}px taller than its groups`).toBeLessThan(40);
  // And the list beside it is what decides the workspace height.
  expect(seen.listHeight).toBeGreaterThan(seen.sidebarHeight);
  expect(failures).toEqual([]);
});


/** The favourites card fills its window, and the sidebar inside it does not.
 *
 * The page was asked to fill the height rather than ending wherever its content did.  Measured
 * at 1920x1040 before: a 901px column with the card ending at 867, so **173px** below it.
 *
 * Three levels had to pass the height down and each was broken differently -- `display: block`
 * on the view, `align-content: start` on the page column, and then `align-content: stretch`
 * handing the free space to the toolbar's row as well -- so this asserts the outcome at the
 * card, and separately that the sidebar did **not** come along: stretched, it draws a border
 * down 417px of nothing, which is what it was doing before any of this.
 */
test("the favourites card fills its window and its sidebar does not", async ({ page }) => {
  const problems: string[] = [];
  for (const [w, h] of [[1920, 1040], [1707, 960], [1280, 900]] as [number, number][]) {
    const failures = await open(page, w, h);
    await page.getByRole("button", { name: T("收藏库", "Favorites"), exact: true }).click();
    await expect(page.locator(".favorites-workspace")).toBeVisible();

    const seen = await page.evaluate(() => {
      const view = document.querySelector(".favorites-view") as HTMLElement;
      const card = document.querySelector(".favorites-workspace") as HTMLElement;
      const sidebar = document.querySelector(".favorites-groups") as HTMLElement;
      const col = document.querySelector(".favorites-view > .page-col") as HTMLElement;
      const vr = view.getBoundingClientRect();
      const cr = card.getBoundingClientRect();
      let sidebarContent = 0;
      for (const kid of [...sidebar.children]) {
        const r = (kid as HTMLElement).getBoundingClientRect();
        if (r.height > 0) sidebarContent = Math.max(sidebarContent, r.bottom - sidebar.getBoundingClientRect().top);
      }
      return {
        cardBottom: Math.round(cr.bottom),
        viewBottom: Math.round(vr.bottom),
        // The column's own 32px bottom margin is the only slack expected.
        colMarginBottom: getComputedStyle(col).marginBottom,
        sidebarHeight: Math.round(sidebar.getBoundingClientRect().height),
        sidebarContent: Math.round(sidebarContent),
        // A toolbar that grows is the symptom the third attempt had.
        toolbarHeight: Math.round((col.firstElementChild as HTMLElement).getBoundingClientRect().height),
      };
    });

    const slack = seen.viewBottom - seen.cardBottom;
    if (slack > 40) problems.push(`${w}px: the card ends ${slack}px above the column's end`);
    if (seen.toolbarHeight > 60) {
      problems.push(`${w}px: the toolbar is ${seen.toolbarHeight}px tall -- it took the slack`);
    }
    if (seen.sidebarHeight - seen.sidebarContent > 40) {
      problems.push(
        `${w}px: the sidebar is ${seen.sidebarHeight - seen.sidebarContent}px taller than its groups`,
      );
    }
    expect(failures).toEqual([]);
  }
  expect(problems).toEqual([]);
});


/** The security card says what the encryption protects -- including when it protects nothing.
 *
 * Asked by the user: "does setting a password really encrypt everything?"  Measured, it does not,
 * and the interesting part is not the gaps but the shape: decryption is automatic, so the key has
 * to come from files on the machine -- and with no password it is derived from the device
 * fingerprint, which is stored in plaintext beside the database.  Anything that can read the
 * database can read the key material.
 *
 * The code said so in the log.  A log line does not reach the person deciding, and the sentence
 * beside the setting describes *traffic* encryption, so "encryption is on" reasonably read as
 * "my files are encrypted".  Both states are asserted: a note that appears in only one of them is
 * a note about the other.
 *
 * Three things had to be right before anything rendered, and each was wrong first: `password_set`
 * lives inside `get_settings.settings` rather than on the envelope; the section is behind the
 * **安全** card, which is itself under the **数据** group, and only the open group is rendered;
 * and the group and the card share a label, so `exact` is what keeps them apart.
 */
test("the security card says what is and is not encrypted", async ({ page }) => {
  const settingsWith = (passwordSet: boolean) => {
    const envelope = fixtures.get_settings as { settings: Record<string, unknown> };
    return {
      get_settings: { ...envelope, settings: { ...envelope.settings, password_set: passwordSet } },
    };
  };

  const openSecurity = async (passwordSet: boolean) => {
    await open(page, 1280, 900, settingsWith(passwordSet));
    await page.getByRole("button", { name: T("设置", "Settings"), exact: true }).click();
    await page.waitForTimeout(250);
    await page.getByRole("button", { name: T("数据", "Data"), exact: true }).click();
    await page.waitForTimeout(150);
    await page.getByRole("button", { name: T("安全", "Security"), exact: true }).click();
    await page.waitForTimeout(200);
    return page.locator("#settings-security");
  };

  const encryptedNote = T(
    "本机文件已加密：剪贴板历史与设备私钥用该密码存放。",
    "The files on this machine are encrypted",
  );
  const warningNote = T("注意：未设置密码时", "Note: with no password set");

  // No password: the warning, and not the reassurance.
  let card = await openSecurity(false);
  await expect(card.getByText(warningNote)).toBeVisible();
  await expect(card.getByText(encryptedNote)).toHaveCount(0);

  // A password: the reassurance, and not the warning.
  card = await openSecurity(true);
  await expect(card.getByText(encryptedNote)).toBeVisible();
  await expect(card.getByText(warningNote)).toHaveCount(0);
});


/** A dialog's buttons are not flush against the field above them.
 *
 * Reported for the push-text dialog: "这个窗口按钮和文本直接靠的太近了".  The cause was general --
 * `.modal p` carries a 24px bottom margin and `.modal label` carries none, while `.modal-actions`
 * had no top margin -- so every dialog whose actions follow a *field* had a 0px gap, and the two
 * that do are this one and 发送网址.
 *
 * Measured, both before and after: `gap=0` then `gap=20`.  A minimum is asserted rather than the
 * exact figure, because the claim is "not touching" and pinning 20 would fail on the spacing being
 * retuned, which is not the fault being guarded against.
 */
test("a dialog's buttons are not flush against the field above them", async ({ page }) => {
  await open(page, 1280, 900);
  await page.getByRole("button", { name: T("设备", "Devices"), exact: true }).click();
  await page.waitForTimeout(300);

  const gapFor = async (dialogSelector: string) =>
    page.evaluate((selector) => {
      const dialog = document.querySelector(selector);
      const actions = dialog?.querySelector(".modal-actions") ?? null;
      const above = actions?.previousElementSibling ?? null;
      if (!actions || !above) return null;
      return {
        above: above.tagName.toLowerCase(),
        gap: Math.round(
          actions.getBoundingClientRect().top - above.getBoundingClientRect().bottom,
        ),
      };
    }, dialogSelector);

  // 推送文本: the one reported, and the wider of the two.
  await page.getByRole("button", { name: T("推送文本", "Push text") }).click();
  await page.waitForTimeout(250);
  const pushed = await gapFor("dialog[aria-labelledby='push-text-title']");
  expect(pushed, "the push-text dialog did not open, or has no actions row").not.toBeNull();
  expect(pushed!.above).toBe("label");
  expect(pushed!.gap, `the buttons sit ${pushed!.gap}px under the field`).toBeGreaterThanOrEqual(16);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(200);

  // 发送网址, which shares the cause.
  const urlButton = page
    .locator(`.device-row button[aria-label='${T("发送网址", "Send URL")}']`)
    .first();
  expect(await urlButton.count(), "no device row offers 发送网址").toBeGreaterThan(0);
  await urlButton.click();
  await page.waitForTimeout(250);
  const sent = await gapFor("dialog[aria-labelledby='send-url-title']");
  expect(sent, "the send-url dialog did not open, or has no actions row").not.toBeNull();
  expect(sent!.above).toBe("label");
  expect(sent!.gap, `the buttons sit ${sent!.gap}px under the field`).toBeGreaterThanOrEqual(16);
});
