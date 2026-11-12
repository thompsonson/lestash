# Ad hoc test scripts

Read-only checks of a running LeStash server. None of the scripts writes to the server.

## `api-check.sh`: the API

```bash
scripts/adhoc/api-check.sh [BASE_URL] [QUERIES_TOML]
```

- Calls the core GET endpoints, then each query in `docs/search-baseline/queries.toml` in `keyword`, `semantic` and `hybrid` modes.
- Also checks the endpoints of the UX work (#268):
  - item kinds, and the `kind`, `exclude_kind`, `year`, `sort`, `top_level` and `person` filters;
  - the Timeline, the Library shelves and facets, People (switches, `me`, a channel);
  - that the app's files are served `Cache-Control: no-cache`.
- Every response must be HTTP 200, valid JSON, and the expected shape for its endpoint.
- Prints each URL, the top 3 hits, and the rank of each query's expected items.
- Exits 1 if any check fails.
- Needs `curl`, `jq` and `python3`.

## `screenshot-search.mjs`: the web UI

```bash
cd scripts/adhoc && npm install      # once: puppeteer-core only, no browser download
node scripts/adhoc/screenshot-search.mjs [--modes hybrid,keyword] [--out DIR] [query ...]
```

- Screenshots the feed, then the Search tab for each query in each mode, as PNGs in `screenshots-<time>/`.
- With no queries given, it uses the queries in `docs/search-baseline/queries.toml`.
- Fails a query when the page logs an error or the search never finishes.
- Drives an existing Chrome: `--browser PATH`, `$CHROME`, the newest Chrome in `~/.cache/puppeteer`, or a system Chrome or Chromium.

## `walkthrough-ui.mjs`: the places and item pages

```bash
node scripts/adhoc/walkthrough-ui.mjs [--url URL] [--out DIR] [--browser PATH]
```

- Walks the app on a desktop (1280 x 900) and a phone (390 x 844) screen:
  - the Timeline: today, a busy day, and a folded line opened;
  - the Library: Books, and Videos for 2024;
  - People, Channels, a channel's page, and you;
  - item pages for a notebook, an audio overview, a post with replies, a book and a chat;
  - Search, and More.
- Finds its examples through the API, so it works on any LeStash.
- For each step it prints the address, the place shown, the active tab, where the first entry starts, and how many touch targets are under 44 px. It also prints what that page should show: rows, folds, counts, groups, seek buttons.
- Saves a PNG per step in `walkthrough-<time>/`.
- Fails a step when the wrong place shows or the page logs an error.

All three default to `https://pop-mini.monkey-ladon.ts.net:8444`; pass `--url` (screenshots) or the first argument (API) for another server.
