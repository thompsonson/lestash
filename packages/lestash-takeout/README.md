# lestash-takeout

Imports [Google Takeout](https://takeout.google.com) exports automatically, using LeStash's
zip importer (`lestash.importers`): Google Keep, Gemini (conversations and Gemini Apps
activity), NotebookLM and YouTube history (watch/search history, or YouTube's My Activity).
One export can hold several products;
all of them are imported.

## Setup

1. In Takeout, set **Delivery method → Add to Drive**. Optionally choose
   **Export every 2 months for 1 year**. (Exports sent as an email download link can't be fetched
   by a script.)
2. For YouTube, choose **JSON** as the history format (the default is HTML, which isn't read).
   The history parser expects an English-language Takeout (file names and "Watched …" titles).
   For Gemini chats, include **My Activity → Gemini Apps**, also as **JSON**; the same applies.
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
  retried when the importer learns a new format. A zip already imported is not: to read a format
  added since (such as Gemini Apps activity), run `lestash takeout forget <id>` on it.

## Re-imports and multi-part exports

Re-importing is safe: items are updated in place, not duplicated.

- **YouTube history:** watch, view and search times are merged with the stored ones, so an
  export that no longer has old watches (YouTube auto-delete) doesn't erase them. A watched video
  you also liked nests under its `liked:` item. YouTube Music listens (songs and podcasts) are
  skipped for now.
- **YouTube My Activity** (`My Activity/YouTube/My Activity.json`) holds the watches and
  searches too, kept longer than the history files. Its likes, dislikes and shares of a video
  become activities of that video's `history:` item, made for a video you never watched.
  Subscriptions, saved playlists, poll votes and dismissed suggestions are skipped.
- **Gemini Apps activity:** each prompt names its chat, so prompts become chats with prompt and
  reply turns. A chat already saved from the Gemini page or by the extension, matched by its
  exact chat id or URL, keeps its item and turns; Takeout adds only the prompts it lacks. Page
  turns have no times of their own, so the added turns sort after or before them, not between.
  Chats that LeStash holds without a chat id are left alone, so such a chat can appear twice.
  Attachments are listed by name.
  Canvas documents become their own items, not tied to a chat, as Takeout doesn't say which.
  A reply with lines reduced to a lone full stop (cause unconfirmed) is kept as it is and
  flagged `takeout_gaps`.
- **NotebookLM:** Takeout can split a notebook over several zips. Each part adds what it holds
  (sources, notes, chats, generated reports); only the part with the notebook's metadata sets its
  title, emoji and date. Generated artifacts (slides, audio and video overviews, infographics,
  apps) become children of their notebook, with their files in the file store. Their files can
  sit in another part than their description, so what the export's parts held so far is kept
  (checkpoint `notebooklm:<export>`), and each part completes the artifacts it can.
