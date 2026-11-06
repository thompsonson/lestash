# lestash-takeout

Imports [Google Takeout](https://takeout.google.com) exports automatically, using LeStash's
zip importer (`lestash.importers`): Google Keep, Gemini, NotebookLM and YouTube watch/search
history. One export can hold several products; all of them are imported.

## Setup

1. In Takeout, set **Delivery method → Add to Drive**. Optionally choose
   **Export every 2 months for 1 year**. (Exports sent as an email download link can't be fetched
   by a script.)
2. For YouTube, choose **JSON** as the history format (the default is HTML, which isn't read).
   The history parser expects an English-language Takeout (file names and "Watched …" titles).
3. LeStash needs its Google sign-in with `drive.readonly` (already used by the Drive import).

Each `lestash sources sync takeout` (and the sync timer) lists your own `takeout-*.zip` files in
Drive, downloads each new one to a temporary file, imports it and deletes the copy. Nothing in
Drive is changed.

```bash
lestash takeout list            # zips in Drive and whether they're imported
lestash sources sync takeout    # import new ones
lestash takeout forget <id>     # import that zip again next sync (resets failures)
```

## What is remembered

Each zip's outcome is stored in the database (`sync_checkpoints`, source `takeout`, key = Drive
file id) and committed in the same transaction as the zip's items. A sync that is killed part-way
never marks a zip done without its items; the zip is simply imported again.

- A zip that can't be read, or that Drive says is gone (404, 403), is recorded as failed, with
  its attempt count; the others still import, and the sync is logged `failed`
  (`lestash sources status`). It is retried on the next 3 syncs at most, then left alone:
  `lestash takeout list` shows the error, and `lestash takeout forget <id>` (or a newer
  importer version) makes it try again.
- Any other download error (expired sign-in, network, a Drive outage or rate limit, a full
  disk) stops the sync at that zip, logged `failed`. Nothing is recorded, so no zip uses up
  its retries; the next sync tries again.
- A zip larger than the free space on the download disk (`$TMPDIR`, else `/tmp`), less 512 MB
  of headroom, is skipped before downloading, without counting an attempt. Smaller zips still
  import, and the sync is logged `failed` naming it, until there is room.
- A zip with nothing recognisable (e.g. only photos) is recorded with the importer version and
  retried when the importer learns a new format.

## Re-imports and multi-part exports

Re-importing is safe: items are updated in place, not duplicated.

- **YouTube history:** watch, view and search times are merged with the stored ones, so an
  export that no longer has old watches (YouTube auto-delete) doesn't erase them. A watched video
  you also liked nests under its `liked:` item. YouTube Music listens (songs and podcasts) are
  skipped for now.
- **NotebookLM:** Takeout can split a notebook over several zips. Each part adds what it holds
  (sources, notes, chats, generated reports); only the part with the notebook's metadata sets its
  title, emoji and date.
