#!/usr/bin/env node
// Ad hoc walkthrough of the LeStash web UI after the UX object-model work (#268, U1-U6):
// the Timeline, Library, People, item pages by kind, Search and More, on a desktop and a
// phone screen. Read-only: it only opens pages, clicks links and filters, and types into
// the search box.
//
//   cd scripts/adhoc && npm install            # once: puppeteer-core, no browser download
//   node scripts/adhoc/walkthrough-ui.mjs [options]
//
// Options:
//   --url URL      server (default https://pop-mini.monkey-ladon.ts.net:8444)
//   --out DIR      output folder (default ./walkthrough-<timestamp>)
//   --browser PATH Chrome or Chromium to drive; default $CHROME, then the newest Chrome
//                  in ~/.cache/puppeteer, then a system Chrome or Chromium
//
// Examples (a busy day, a notebook, an audio overview, a post with replies, a book, a
// channel) are found through the API, so the walk works on any LeStash.
// Each step prints ok or FAIL, its address, its screenshot and what it measured.
// Exit 1 if a page fails to load, shows the wrong place, or logs an error.

import { existsSync, mkdirSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import puppeteer from "puppeteer-core";

const { values: opts } = parseArgs({
  options: {
    url: { type: "string", default: "https://pop-mini.monkey-ladon.ts.net:8444" },
    out: { type: "string" },
    browser: { type: "string" },
  },
});
const BASE = opts.url.replace(/\/$/, "");

function findBrowser() {
  if (opts.browser) return opts.browser;
  if (process.env.CHROME) return process.env.CHROME;
  const cache = join(homedir(), ".cache/puppeteer/chrome");
  if (existsSync(cache)) {
    const newest = readdirSync(cache)
      .filter((v) => v.startsWith("linux-"))
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
      .at(-1);
    const bin = newest && join(cache, newest, "chrome-linux64/chrome");
    if (bin && existsSync(bin)) return bin;
  }
  for (const bin of ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]) {
    if (existsSync(bin)) return bin;
  }
  throw new Error("no Chrome found: pass --browser PATH or set CHROME");
}

const api = async (path) => {
  const res = await fetch(BASE + path);
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
};
const first = async (path) => (await api(path)).items?.[0];

// ── Examples, found through the API ──────────────────────────────────────────────────
const ex = {};
const today = new Date();
const dayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate());
const lastActive = (await api(`/api/timeline?start=${encodeURIComponent(dayStart.toISOString())}`
  + `&end=${encodeURIComponent(new Date(+dayStart + 86400000).toISOString())}`)).previous;
// The busiest of the last 30 active days: step back and keep the day with most entries
let busy = { day: null, n: -1 };
let cursor = lastActive;
for (let i = 0; i < 30 && cursor; i++) {
  const d = new Date(cursor);
  const s = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const t = await api(`/api/timeline?start=${encodeURIComponent(s.toISOString())}`
    + `&end=${encodeURIComponent(new Date(+s + 86400000).toISOString())}`);
  const iso = `${s.getFullYear()}-${String(s.getMonth() + 1).padStart(2, "0")}-${String(s.getDate()).padStart(2, "0")}`;
  if (t.entries.length > busy.n) busy = { day: iso, n: t.entries.length };
  cursor = t.previous;
}
ex.busyDay = busy.day;
ex.notebook = (await api("/api/items?kind=notebook&limit=50")).items.sort((a, b) => b.child_count - a.child_count)[0];
ex.audio = (await api("/api/items?kind=artifact&include_children=true&limit=100")).items.find((i) => i.kind_detail === "Audio overview");
ex.post = (await api("/api/items?kind=post&own=true&limit=100")).items
  .sort((a, b) => (b.child_count - b.reaction_count) - (a.child_count - a.reaction_count))[0];
ex.book = (await api("/api/items?kind=book&limit=100")).items.sort((a, b) => b.child_count - a.child_count)[0];
ex.channel = (await api("/api/people?switch=channels&limit=1")).people[0];
ex.chat = await first("/api/items?kind=chat&limit=1");

// ── The walk ─────────────────────────────────────────────────────────────────────────
const stamp = new Date().toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-");
const outDir = resolve(opts.out ?? `walkthrough-${stamp}`);
mkdirSync(outDir, { recursive: true });

const browser = await puppeteer.launch({ executablePath: findBrowser(), headless: true });
let failed = false;
let n = 0;

// What every screen reports: its address, the place shown, where its first entry starts,
// and whether anything a finger taps is under 44 px on a phone
const measure = (page) => page.evaluate(() => {
  const visible = (el) => el && el.offsetParent !== null;
  const shown = [...document.querySelectorAll(".tab-content, #item-view")].filter(visible).map((e) => e.id);
  const firstEntry = [...document.querySelectorAll(
    ".timeline > li, .library-list .item-card, .people-list .item-card, #search-list .item-card, .more-list a, #item-view-content .item-view-title")]
    .find(visible);
  const small = [...document.querySelectorAll("nav#tabs button, .switch a, .fold-filters summary, .item-card, .day-step, .item-view-back")]
    .filter((e) => visible(e) && e.getBoundingClientRect().height < 44).length;
  return {
    url: location.pathname + location.search,
    shown,
    active: document.querySelector("#tabs button.active")?.dataset.tab ?? null,
    firstTop: firstEntry ? Math.round(firstEntry.getBoundingClientRect().top) : null,
    small,
    title: document.querySelector("#item-view-content .item-view-title")?.innerText?.slice(0, 70) ?? null,
  };
});

async function step(page, errors, name, expect, act) {
  n += 1;
  errors.length = 0;
  const file = join(outDir, `${String(n).padStart(2, "0")}-${name}.png`);
  try {
    await act();
    await new Promise((r) => setTimeout(r, 1500));
    const m = await measure(page);
    await page.screenshot({ path: file });
    const problems = [];
    if (expect.shown && !m.shown.includes(expect.shown)) problems.push(`showed ${m.shown.join(",") || "nothing"}`);
    if (expect.url && !expect.url.test(m.url)) problems.push(`address ${m.url}`);
    problems.push(...errors);
    if (problems.length) failed = true;
    console.log(`${problems.length ? "FAIL" : "ok  "}  ${name.padEnd(26)} ${m.url}`);
    console.log(`        ${JSON.stringify({ active: m.active, firstTop: m.firstTop, small: m.small, title: m.title, ...(expect.extra ? await expect.extra() : {}) })}`);
    if (problems.length) console.log(`        problems: ${problems.join("; ")}`);
    console.log(`        -> ${file}`);
  } catch (e) {
    failed = true;
    await page.screenshot({ path: file }).catch(() => {});
    console.log(`FAIL  ${name}  ${e.message.split("\n")[0]}  -> ${file}`);
  }
}

async function walk(label, viewport) {
  const page = await browser.newPage();
  await page.setViewport(viewport);
  await page.setCacheEnabled(false);
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && !/Failed to load resource/.test(m.text()) && errors.push(m.text()));
  const go = (path) => page.goto(BASE + path, { waitUntil: "networkidle0", timeout: 30000 });
  const text = (sel) => page.$eval(sel, (e) => e.innerText.replace(/\s+/g, " ").slice(0, 160)).catch(() => null);
  const count = (sel) => page.$$eval(sel, (els) => els.length);
  console.log(`\n== ${label} (${viewport.width} x ${viewport.height})`);

  await step(page, errors, `${label}-timeline-today`, { shown: "tab-timeline", url: /^\/$/,
    extra: async () => ({ rows: await count(".timeline > li"), empty: await text(".timeline-list .empty-state") }) },
  () => go("/"));
  if (ex.busyDay) {
    await step(page, errors, `${label}-timeline-busy-day`, { shown: "tab-timeline", url: /^\/day\//,
      extra: async () => ({ rows: await count(".timeline > li"), folded: await count(".timeline-group"),
        landmarks: await count(".timeline-entry.landmark"), firstRows: await page.$$eval(".timeline > li", (l) => l.slice(0, 4).map((x) => x.innerText.replace(/\s+/g, " ").slice(0, 70))) }) },
    () => go(`/day/${ex.busyDay}`));
    await step(page, errors, `${label}-timeline-folded-open`, { shown: "tab-timeline",
      extra: async () => ({ opened: await count(".timeline-group details[open]"), subRows: await count(".timeline-sub li") }) },
    () => page.evaluate(() => { const d = document.querySelector(".timeline-group details"); if (d) d.open = true; }));
  }
  await step(page, errors, `${label}-library-books`, { shown: "tab-library", url: /^\/library/,
    extra: async () => ({ count: await text(".library-count"), shelves: await text(".shelves"), filters: await text("#tab-library .fold-filters summary") }) },
  () => go("/library?shelf=books"));
  await step(page, errors, `${label}-library-videos-2024`, { shown: "tab-library", url: /year=2024/,
    extra: async () => ({ count: await text(".library-count"), filters: await text("#tab-library .fold-filters summary") }) },
  () => go("/library?shelf=videos&year=2024"));
  await step(page, errors, `${label}-people`, { shown: "tab-people", url: /^\/people/,
    extra: async () => ({ count: await text(".people-count"), top: await page.$$eval(".people-list .item-card", (c) => c.slice(0, 3).map((x) => x.innerText.replace(/\s+/g, " ").slice(0, 60))) }) },
  () => go("/people"));
  await step(page, errors, `${label}-channels`, { shown: "tab-people", url: /switch=channels/,
    extra: async () => ({ count: await text(".people-count") }) },
  () => go("/people?switch=channels"));
  if (ex.channel) {
    await step(page, errors, `${label}-channel-page`, { shown: "item-view", url: /^\/people\/\d+/,
      extra: async () => ({ meta: await text(".item-view-meta"), withYou: await text(".person-activity"), chips: await count(".chips .chip"), items: await count("#person-items .item-card") }) },
    () => go(`/people/${ex.channel.id}`));
  }
  await step(page, errors, `${label}-you`, { shown: "item-view", url: /^\/people\/me/,
    extra: async () => ({ meta: await text(".item-view-meta"), accounts: await text(".person-accounts") }) },
  () => go("/people/me"));
  for (const [key, extra] of [
    ["notebook", async () => ({ up: await text(".item-up"), groups: await page.$$eval(".parts-group-title", (h) => h.map((x) => x.innerText)), parts: await count("#children-list .item-card") })],
    ["audio", async () => ({ up: await text(".item-up"), settings: await text(".item-settings"), player: await count(".item-player audio, .item-player video"), seek: await count("button.seek"), playerTop: await page.$eval(".item-player", (e) => Math.round(e.getBoundingClientRect().top)).catch(() => null) })],
    ["post", async () => ({ meta: await text(".item-view-meta"), reactions: await text(".reactions-toggle"), replies: await count(".thread-row"), firstReply: await text(".thread-row") })],
    ["book", async () => ({ meta: await text(".item-view-meta"), groups: await page.$$eval(".parts-group-title", (h) => h.map((x) => x.innerText)), people: await text("#people-list") })],
    ["chat", async () => ({ meta: await text(".item-view-meta"), bubbles: await count(".chat-bubble") })],
  ]) {
    if (!ex[key]) { console.log(`skip  ${label}-${key}  (no example found)`); continue; }
    await step(page, errors, `${label}-item-${key}`, { shown: "item-view", url: /^\/items\/\d+/, extra },
      () => go(`/items/${ex[key].id}`));
  }
  await step(page, errors, `${label}-search`, { shown: "tab-search", url: /^\/search\?q=/,
    extra: async () => ({ status: await text("#search-status"), top: await page.$$eval("#search-list .item-card", (c) => c.slice(0, 3).map((x) => x.innerText.replace(/\s+/g, " ").slice(0, 70))) }) },
  async () => { await go("/search?q=AtomicGuard"); await page.waitForFunction(() => /result/.test(document.getElementById("search-status")?.textContent ?? ""), { timeout: 30000 }).catch(() => {}); });
  await step(page, errors, `${label}-more`, { shown: "tab-more", url: /^\/more$/,
    extra: async () => ({ links: await count(".more-list a") }) },
  () => go("/more"));
  await page.close();
}

try {
  console.log(`== Walkthrough of ${BASE} at ${new Date().toString().slice(0, 21)}`);
  console.log(`   examples: busy day ${ex.busyDay} (${busy.n} entries); notebook ${ex.notebook?.id}; audio ${ex.audio?.id}; post ${ex.post?.id}; book ${ex.book?.id}; channel ${ex.channel?.id} ${ex.channel?.name}; chat ${ex.chat?.id}`);
  await walk("desktop", { width: 1280, height: 900 });
  await walk("phone", { width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 1 });
} finally {
  await browser.close();
}
console.log(`\n${failed ? "Some steps failed" : "All steps ok"}; screenshots in ${outDir}`);
process.exit(failed ? 1 : 0);
