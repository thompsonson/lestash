# lestash-takeout

Imports [Google Takeout](https://takeout.google.com) exports automatically, using LeStash's
existing zip importer (Google Keep, Gemini, NotebookLM, ...).

## Setup

1. In Takeout, set **Delivery method → Add to Drive**. Optionally choose
   **Export every 2 months for 1 year**. (Exports sent as an email download link can't be fetched
   by a script.)
2. LeStash needs its Google sign-in with `drive.readonly` (already used by the Drive import).

Each `lestash sources sync takeout` (and the sync timer) lists `takeout-*.zip` files in Drive,
downloads each new one to a temporary file, imports it and deletes the copy. Nothing in Drive is
changed. Imported zips are remembered in `~/.config/lestash/takeout_state.json`.

```bash
lestash takeout list            # zips in Drive and whether they're imported
lestash sources sync takeout    # import new ones
lestash takeout forget <id>     # import that zip again next sync
```

Re-importing is safe: items are updated in place, not duplicated.
