#!/usr/bin/env node
// Ad hoc screenshots of the LeStash web UI: the feed, then the Search tab for each query.
// Read-only: it only opens pages and types into the search box.
//
//   cd scripts/adhoc && npm install            # once: puppeteer-core, no browser download
//   node scripts/adhoc/screenshot-search.mjs [options] [query ...]
//
// Options:
//   --url URL          server (default https://pop-mini.monkey-ladon.ts.net:8444)
//   --queries FILE     queries TOML (default docs/search-baseline/queries.toml), used when no
//                      query is given on the command line
//   --modes LIST       comma-separated search modes (default hybrid; also keyword, semantic)
//   --out DIR          output folder (default ./screenshots-<timestamp>)
//   --width N --height N   viewport (default 1280 x 900)
//   --browser PATH     Chrome or Chromium to drive; default $CHROME, then the newest
//                      Chrome in ~/.cache/puppeteer, then a system Chrome or Chromium
//
// Exit 1 if a page fails to load, the search never settles, or the page logs an error.

import { existsSync, mkdirSync, readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const { values: opts, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    url: { type: "string", default: "https://pop-mini.monkey-ladon.ts.net:8444" },
    queries: { type: "string", default: join(repoRoot, "docs/search-baseline/queries.toml") },
    modes: { type: "string", default: "hybrid" },
    out: { type: "string" },
    width: { type: "string", default: "1280" },
    height: { type: "string", default: "900" },
    browser: { type: "string" },
  },
});

/** Queries from the command line, else every `q = "..."` line of the queries TOML. */
function loadQueries() {
  if (positionals.length) return positionals;
  if (!existsSync(opts.queries)) throw new Error(`no queries given and no file at ${opts.queries}`);
  // Basic strings only, which is all queries.toml uses; enough for an ad hoc tool
  return [...readFileSync(opts.queries, "utf8").matchAll(/^q\s*=\s*"((?:[^"\\]|\\.)*)"/gm)].map(
    (m) => JSON.parse(`"${m[1]}"`),
  );
}

/** A Chrome to drive: --browser, $CHROME, the newest Puppeteer Chrome, or a system one. */
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

const slug = (s) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "").slice(0, 60);
const stamp = new Date().toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-");
const outDir = resolve(opts.out ?? `screenshots-${stamp}`);
const modes = opts.modes.split(",").map((m) => m.trim()).filter(Boolean);
const queries = loadQueries();
mkdirSync(outDir, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: findBrowser(),
  headless: true,
  defaultViewport: { width: Number(opts.width), height: Number(opts.height) },
});
let failed = false;
const report = (ok, what, detail = "") => {
  if (!ok) failed = true;
  console.log(`${ok ? "ok  " : "FAIL"}  ${what}${detail ? `  (${detail})` : ""}`);
};

try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));

  // The feed, as the app opens
  const res = await page.goto(opts.url, { waitUntil: "networkidle0", timeout: 30000 });
  const feedFile = join(outDir, "00-feed.png");
  await page.screenshot({ path: feedFile, fullPage: false });
  report(res?.ok() && errors.length === 0, `feed  ${opts.url}  -> ${feedFile}`, errors.join("; "));

  // Each query on the Search tab, in each mode. The page only reads its #hash on load,
  // so click the tab button; a reload between queries keeps each one independent.
  let n = 0;
  for (const q of queries) {
    for (const mode of modes) {
      n += 1;
      errors.length = 0;
      const file = join(outDir, `${String(n).padStart(2, "0")}-${mode}-${slug(q)}.png`);
      try {
        await page.goto(opts.url, { waitUntil: "networkidle0", timeout: 30000 });
        await page.click('button[data-tab="search"]');
        await page.waitForSelector("#search-input", { visible: true, timeout: 10000 });
        await page.select("#search-mode", mode);
        await page.type("#search-input", q);
        // The status line reads "Searching (mode)..." until results or an error arrive
        let settled = true;
        try {
          await page.waitForFunction(
            () => {
              const s = document.getElementById("search-status")?.textContent ?? "";
              return s !== "" && !s.startsWith("Searching") && !s.startsWith("Tip:");
            },
            { timeout: 30000 },
          );
        } catch {
          settled = false;
        }
        const status = await page.$eval("#search-status", (el) => el.textContent.trim());
        await page.screenshot({ path: file, fullPage: false });
        report(
          settled && !/error|failed/i.test(status) && errors.length === 0,
          `${mode.padEnd(8)} "${q}"  ${status}  -> ${file}`,
          [settled ? "" : "search did not settle", ...errors].filter(Boolean).join("; "),
        );
      } catch (e) {
        await page.screenshot({ path: file, fullPage: false }).catch(() => {});
        report(false, `${mode.padEnd(8)} "${q}"  -> ${file}`, e.message.split("\n")[0]);
      }
    }
  }
} finally {
  await browser.close();
}

console.log(`\n${failed ? "Some pages failed" : "All pages ok"}; screenshots in ${outDir}`);
process.exit(failed ? 1 : 0);
