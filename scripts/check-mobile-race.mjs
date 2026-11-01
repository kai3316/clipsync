#!/usr/bin/env node
/*
 * Regression check for the phone page's two filtered lists.
 *
 * The review (CODE-REVIEW-ROUND3.md, Blocker 1 and Suggestion 2) found the same
 * bug on both: a five second poll is in flight when the reader changes a
 * filter, and the poll's answer — about the filter they just left — lands
 * afterwards.  Applied, that answer merges rows the new filter excludes back
 * into the list and its echoed `kind` drags the chip highlight back with them:
 * a filter that looks applied and quietly is not.
 *
 * The page is driven against a stub DOM: `internal/web/static/mobile.html`'s
 * inline script is evaluated in a `vm` context with just enough document,
 * window and fetch to run, and every request is answered by this file.  The
 * poll interval is captured rather than waited out, so a run takes a second.
 *
 *   node scripts/check-mobile-race.mjs             # history list
 *   node scripts/check-mobile-race.mjs favorites   # favourites list
 *   node scripts/check-mobile-race.mjs link        # the modal's open button
 *   node scripts/check-mobile-race.mjs history path/to/other.html
 *
 * What it is for: the checks below fail if the stale-response guard, the
 * request-generation bump, or the chip rail's build-once behaviour is removed.
 * That was verified by mutating each of them — a check that cannot fail is not
 * a check.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const tab = process.argv[2] || "history";
const source = process.argv[3] ? path.resolve(process.argv[3]) : path.join(root, "internal", "web", "static", "mobile.html");
const html = readFileSync(source, "utf8");
const blocks = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const boot = blocks[0];
const main = blocks.filter((b) => b.includes("var historyLoading")).pop();
if (!main) throw new Error("could not find the page's inline script");

/* ── A DOM just real enough ───────────────────────────────────────── */

function makeEl(tag = "div") {
  const el = {
    tag,
    id: "",
    className: "",
    textContent: "",
    _html: "",
    hidden: false,
    value: "",
    title: "",
    type: "",
    style: {},
    dataset: {},
    children: [],
    listeners: {},
    scrollLeft: 0,
    scrollTop: 0,
    offsetWidth: 0,
    offsetHeight: 0,
    clientWidth: 0,
    clientHeight: 0,
    scrollHeight: 0,
    classList: {
      _set: new Set(),
      add(...names) { names.forEach((n) => this._set.add(n)); },
      remove(...names) { names.forEach((n) => this._set.delete(n)); },
      toggle(name, force) { (force === undefined ? !this._set.has(name) : force) ? this._set.add(name) : this._set.delete(name); },
      contains(name) { return this._set.has(name); },
    },
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
    removeEventListener() {},
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    insertBefore(child) { this.children.push(child); return child; },
    removeChild(child) { this.children = this.children.filter((c) => c !== child); return child; },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    setAttribute(name, value) { this[name] = String(value); },
    getAttribute(name) { return this[name] ?? null; },
    removeAttribute(name) { delete this[name]; },
    // A page that wires up a child control (`row.querySelector('.x')`) must
    // get something to wire it to, or the row builder throws and the list stays
    // empty for a reason that has nothing to do with what is being tested.
    querySelector(sel) { this._q ||= {}; return (this._q[sel] ||= makeEl("div")); },
    querySelectorAll() { return []; },
    closest() { return null; },
    focus() {},
    blur() {},
    click() { (this.listeners.click || []).forEach((fn) => fn({ target: this, stopPropagation() {}, preventDefault() {} })); },
    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; },
    scrollIntoView() {},
  };
  // innerHTML='' has to actually empty the fake, or every count below measures
  // the harness instead of the page: the page clears a list by assigning it.
  Object.defineProperty(el, "innerHTML", {
    get() { return this._html; },
    set(value) { this._html = String(value); this.children = []; },
  });
  return el;
}

const byId = new Map();
function getEl(id) {
  if (!byId.has(id)) {
    const el = makeEl("div");
    el.id = id;
    byId.set(id, el);
  }
  return byId.get(id);
}

const activeTab = makeEl("button");
// `link` is a second pass over the history list, not a third tab: the page
// dispatches its poll by tab name and an unknown name polls nothing at all,
// which would leave `tick()` silently making no request.
activeTab.setAttribute("data-tab", tab === "favorites" ? "favorites" : "history");

const documentStub = {
  documentElement: makeEl("html"),
  body: makeEl("body"),
  visibilityState: "visible",
  getElementById: getEl,
  createElement: (tag) => makeEl(tag),
  createTextNode: (text) => ({ text }),
  // Anything the page asks for by selector gets a stand-in; the one selector
  // that decides which tab polls gets the tab this run is about.
  querySelector: (sel) => (String(sel).includes("--active") ? activeTab : getEl("sel:" + sel)),
  querySelectorAll: () => [],
  addEventListener() {},
  removeEventListener() {},
  execCommand: () => true,
  get activeElement() { return null; },
};

const windowStub = {
  location: { origin: "http://192.168.1.10:5170", search: "?token=abc", href: "http://192.168.1.10:5170/mobile.html?token=abc" },
  navigator: {
    language: "zh-CN",
    userAgent: "node",
    serviceWorker: { register: () => Promise.resolve() },
    clipboard: { writeText: () => Promise.resolve(), readText: () => Promise.resolve("") },
  },
  document: documentStub,
  localStorage: {
    _v: new Map(),
    getItem(k) { return this._v.has(k) ? this._v.get(k) : null; },
    setItem(k, v) { this._v.set(k, String(v)); },
    removeItem(k) { this._v.delete(k); },
  },
  addEventListener() {},
  removeEventListener() {},
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  confirm: () => true,
  alert() {},
  scrollTo() {},
  setTimeout, clearTimeout,
  // The poll interval is captured rather than run: firing it on demand is
  // faster than waiting five seconds, and it is the same callback either way.
  setInterval: (fn) => { const id = nextTimerId++; intervals.set(id, fn); return id; },
  clearInterval: (id) => intervals.delete(id),
  AbortController,
  URLSearchParams,
  Promise, JSON, Date, Math, Number, String, Object, Array, RegExp, Error, TypeError,
  console,
  __I18N_LOCALE__: "zh-CN",
  __I18N_JSON__: {},
};
windowStub.window = windowStub;
windowStub.self = windowStub;

/* ── fetch, under the test's control ─────────────────────────────── */

const intervals = new Map();
let nextTimerId = 1;

const requests = [];
function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  };
}

windowStub.fetch = (url, options = {}) => {
  const entry = { url: String(url), options, settled: false };
  entry.promise = new Promise((resolve, reject) => {
    entry.resolve = (payload, status) => { entry.settled = true; resolve(response(payload, status)); };
    entry.reject = (error) => { entry.settled = true; reject(error); };
  });
  requests.push(entry);
  return entry.promise;
};

// Anchors the page adds to <body> to open a link in a new tab, kept so the
// harness can see one without a browser.
const appended = [];
const realAppend = documentStub.body.appendChild.bind(documentStub.body);
documentStub.body.appendChild = (child) => { appended.push(child); return realAppend(child); };

const context = vm.createContext(windowStub);
vm.runInContext(boot, context);
vm.runInContext(main, context);

/* ── Helpers ─────────────────────────────────────────────────────── */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// One poll tick: whatever `startPolling` registered, run it once.
function tick() { for (const fn of intervals.values()) fn(); }
function lastRequest(match) {
  for (let i = requests.length - 1; i >= 0; i -= 1) {
    if (requests[i].url.includes(match)) return requests[i];
  }
  return null;
}
const rowsOf = (id, cls) => getEl(id).children.filter((c) => c.className === cls);
const chipNamed = (railId, label) =>
  getEl(railId).children.find((c) => c.children.some((x) => x.textContent === label));

const failures = [];
function check(name, condition, detail = "") {
  if (condition) console.log(`  ok   ${name}`);
  else {
    failures.push(name);
    console.log(`  FAIL ${name}${detail ? ` — ${detail}` : ""}`);
  }
}

/* ── Scenarios ───────────────────────────────────────────────────── */

const historyRows = (n, kind = "all") =>
  Array.from({ length: n }, (_, i) => ({
    entry_id: `${kind}-${i}`,
    timestamp: 1000 + i,
    text_preview: `${kind} row ${i}`,
    content_type: kind === "image" ? "IMAGE_PNG" : "TEXT",
    pinned: false,
  }));

function historyPage(items, kind, counts) {
  return {
    items,
    total: items.length,
    kind,
    sort: "newest",
    counts: counts || { all: items.length, text: kind === "text" ? items.length : 0, image: kind === "image" ? items.length : 0, file: 0, link: 0 },
    has_history: true,
  };
}

async function historyScenario() {
  console.log("history: initial load");
  const initial = lastRequest("/api/history");
  check("a request was issued on start", !!initial);
  initial.resolve(historyPage(historyRows(3, "text"), "all"));
  await sleep(30);
  check("three rows rendered", rowsOf("historyList", "h-item").length === 3, `${rowsOf("historyList", "h-item").length}`);

  console.log("history: poll in flight, then a chip is tapped");
  tick();
  await sleep(10);
  const pollReq = lastRequest("/api/history");
  check("a poll went out and is unanswered", !!pollReq && pollReq !== initial && !pollReq.settled, pollReq && pollReq.url);

  const imageChip = chipNamed("histChips", "图片");
  check("the image chip exists", !!imageChip);
  imageChip.click();
  await sleep(10);
  check("the list was emptied at once", rowsOf("historyList", "h-item").length === 0);
  check("the highlight moved before any answer", imageChip.className.includes("chip--active"), imageChip.className);

  console.log("history: the poll's stale answer lands");
  pollReq.resolve(historyPage(historyRows(3, "text"), "all"));
  await sleep(30);
  check("its rows were not merged in", rowsOf("historyList", "h-item").length === 0, `${rowsOf("historyList", "h-item").length}`);
  check("its echoed kind did not move the chip back", imageChip.className.includes("chip--active"), imageChip.className);

  const refetch = lastRequest("/api/history");
  check("a fresh request carried the new filter", !!refetch && refetch.url.includes("kind=image"), refetch && refetch.url);
  refetch.resolve(historyPage(historyRows(1, "image"), "image"));
  await sleep(30);
  check("the filtered answer rendered", rowsOf("historyList", "h-item").length === 1, `${rowsOf("historyList", "h-item").length}`);
  check("the chip is still the image chip", imageChip.className.includes("chip--active"));

  console.log("history: the rail survives a poll that changes the counts");
  const rail = getEl("histChips");
  rail.scrollLeft = 120;
  const before = rail.children.slice();
  tick();
  await sleep(10);
  const nextPoll = lastRequest("/api/history");
  nextPoll.resolve(historyPage(historyRows(1, "image"), "image", { all: 9, text: 4, image: 1, file: 3, link: 1 }));
  await sleep(30);
  check("the chips are the same nodes", rail.children.length === before.length && rail.children.every((el, i) => el === before[i]));
  check("the rail kept its scroll", rail.scrollLeft === 120, String(rail.scrollLeft));
  check("the counts were updated in place", before[0].children.some((c) => c.textContent === "9"), JSON.stringify(before[0].children.map((c) => c.textContent)));

  console.log("history: a row whose entry is already gone");
  const staleRow = rowsOf("historyList", "h-item")[0];
  chipNamed("histChips", "文本").click();
  await sleep(5);
  let threw = null;
  try { staleRow.click(); } catch (error) { threw = error; }
  check("tapping it does not throw", threw === null, threw && threw.message);
}

const favourite = (id, title, group) => ({
  id,
  title,
  content: `${title} body`,
  group,
  position: 0,
  created: 1000,
  updated: 1000,
});

async function favoritesScenario() {
  console.log("favorites: first poll");
  tick();
  await sleep(10);
  const first = lastRequest("/api/favorites");
  check("a favorites request went out", !!first, first && first.url);
  first.resolve({
    favorites: [favourite("a", "会议纪要", "工作"), favourite("b", "菜谱", "私人")],
    total: 2,
    library_total: 2,
    groups: ["私人", "工作"],
    group_counts: { 工作: 1, 私人: 1 },
  });
  await sleep(30);
  check("both rows rendered", rowsOf("favoritesList", "fav-item").length === 2, `${rowsOf("favoritesList", "fav-item").length}`);

  console.log("favorites: a load in flight, then a group chip is tapped");
  tick();
  await sleep(10);
  const inflight = lastRequest("/api/favorites");
  check("a second request is in flight", !!inflight && inflight !== first && !inflight.settled);
  const workChip = chipNamed("favChips", "工作");
  check("the group chip exists", !!workChip);
  workChip.click();
  await sleep(10);
  check("the highlight moved at once", workChip.className.includes("chip--active"), workChip.className);

  console.log("favorites: the stale answer lands");
  inflight.resolve({
    favorites: [favourite("stale", "STALE", "")],
    total: 1,
    library_total: 1,
    groups: ["私人", "工作"],
    group_counts: { 工作: 1, 私人: 1 },
  });
  await sleep(30);
  // A favourite's row is one innerHTML string, so what it says lives in `_html`.
  const shown = rowsOf("favoritesList", "fav-item").map((el) => el._html).join("\n");
  check("its row was not painted", !shown.includes("STALE"), shown.slice(0, 120));
  check("the chip did not move back", workChip.className.includes("chip--active"), workChip.className);

  const refetch = lastRequest("/api/favorites");
  check("a fresh request carried the group", !!refetch && refetch.url.includes("group="), refetch && refetch.url);
  refetch.resolve({ favorites: [favourite("a", "会议纪要", "工作")], total: 1, library_total: 2, groups: ["私人", "工作"], group_counts: { 工作: 1, 私人: 1 } });
  await sleep(30);
  check("the filtered answer rendered", rowsOf("favoritesList", "fav-item").length === 1, `${rowsOf("favoritesList", "fav-item").length}`);
}

async function linkScenario() {
  console.log("link: a URL clip");
  const initial = lastRequest("/api/history");
  initial.resolve({
    items: [{
      entry_id: "link-1",
      timestamp: 1000,
      text_preview: "https://example.com/a?b=1",
      content_type: "URL",
      pinned: false,
    }],
    total: 1,
    kind: "all",
    sort: "newest",
    counts: { all: 1, text: 0, image: 0, file: 0, link: 1 },
    has_history: true,
  });
  await sleep(30);
  const row = rowsOf("historyList", "h-item")[0];
  check("the row rendered", !!row);
  row.click();
  await sleep(10);
  const openBtn = getEl("histOpenBtn");
  check("the open button is offered", openBtn.style.display === "", JSON.stringify(openBtn.style));
  check("it is labelled", openBtn.textContent.length > 0, openBtn.textContent);

  openBtn.click();
  await sleep(10);
  const anchor = appended.find((el) => el.tag === "a");
  check("a link was handed to the browser", !!anchor && anchor.href === "https://example.com/a?b=1", anchor && anchor.href);
  check("it opens in a new tab, without a referrer", !!anchor && anchor.target === "_blank" && /noopener/.test(anchor.rel));
  check("the sheet closed behind it", !getEl("modal").classList.contains("modal-mask--show"));

  console.log("link: a clip that is not one");
  // The next list the page asks for is a poll; answer it with prose and open
  // that row instead.  (Matching "?": the detail fetch the modal makes is
  // /api/history/item and is not a list.)  It has to be a NEW request — the
  // first list is already answered, so resolving it again does nothing and
  // every check below would quietly be reading the old row.
  const before = appended.length;
  tick();
  await sleep(10);
  const listRequest = lastRequest("/api/history?");
  check(
    "the poll asked for the list again",
    !!listRequest && listRequest !== initial && !listRequest.settled,
    listRequest && `${listRequest.url} settled=${listRequest.settled}`,
  );
  listRequest.resolve({
    items: [{
      entry_id: "text-2",
      timestamp: 1001,
      text_preview: "meeting notes, no link here",
      content_type: "TEXT",
      pinned: false,
    }],
    total: 1,
    kind: "all",
    sort: "newest",
    counts: { all: 1, text: 1, image: 0, file: 0, link: 0 },
    has_history: true,
  });
  await sleep(30);
  rowsOf("historyList", "h-item")[0].click();
  await sleep(10);
  check("the open button is not offered", getEl("histOpenBtn").style.display === "none", getEl("histOpenBtn").style.display);
  check("nothing was handed to the browser again", appended.length === before, `${appended.length} vs ${before}`);
}

if (tab === "favorites") await favoritesScenario();
else if (tab === "link") await linkScenario();
else await historyScenario();

console.log(failures.length ? `\n${failures.length} failed: ${failures.join(", ")}` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
