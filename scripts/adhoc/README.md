# Ad hoc test scripts

Read-only checks of a running LeStash server. Neither script writes to the server.

## `api-check.sh`: the API

```bash
scripts/adhoc/api-check.sh [BASE_URL] [QUERIES_TOML]
```

- Calls the core GET endpoints, then each query in `docs/search-baseline/queries.toml` in `keyword`, `semantic` and `hybrid` modes.
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

Both default to `https://pop-mini.monkey-ladon.ts.net:8444`; pass `--url` (screenshots) or the first argument (API) for another server.
