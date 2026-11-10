# File store

W2 (#217): one store for every file LeStash keeps.

## Layout

- Files live at `<root>/blobs/ab/abcdef…`, each stored once and named by its SHA-256.
- The root is `~/.local/share/lestash` for the default database; sysbak backs it up with `~/.local`.
- Any other database gets a `store` folder beside it, so a test environment never writes into the real store.
- `general.store_path` in `config.toml` overrides the root.
- A file is copied to a temporary name, checked, and renamed, so an interrupted copy leaves no half-written blob.

## Schema (migration 22)

- `blobs`: `sha256`, `size`, `mime_type`, `first_seen`; one row per stored file.
- `item_media.sha256` points a media row at its blob.
- `item_media.version` and `superseded_at` hold versions; a row with `superseded_at` set is an older one, kept.

## Writing and reading

- `lestash media adopt` copies each media file not yet in the store into it and sets its row's `sha256`.
- `lestash media prune` then removes the media folder's copy, once the stored file is there with the same hash. Each sync runs both.
- Each `lestash sources sync` and each upload through `/api/items/{id}/media` adopts new media the same way.
- Media written elsewhere (PDF enrichment, imports) wait for the next sync or `lestash media adopt`; until then they are served from the media folder.
- `/api/media/{id}` serves the blob from the store, else the media folder's file, else redirects to the remote URL.

## As built (#217, step 1)

- Files are copied with no write open, then recorded in one short transaction; a file that cannot be read is skipped and tried next time, so it never fails a sync.
- On a copy of the live database: 755 media files adopted as 617 blobs (155 MB, against 175 MB in the media folder); a second run adds none.

## As built (#217, step 2): NotebookLM artifacts

- `lestash media notebooklm <zip>…` imports a Takeout's NotebookLM generated artifacts with their files. Give every part: a notebook's files are spread over them.
- Each artifact (slides, audio overview, video overview, infographic, app) becomes a child item of its notebook, with `type: artifact`. Reports are skipped: they are notes already.
- A file belongs to the description whose name is its own, else the longest one either name begins with, among descriptions of a type the file fits (a deck folder, `.mp4`, `.wav`, `.png`, an extensionless app page). Names are cut at 51 bytes, even through " metadata"; two artifacts of one title are "x metadata" and "x metadata(1)".
- Slide images keep their numbers: `_-_Slide_7` is 7; in a deck named `_-_Slide`, `_-_Slide(1)`, … the bare name is 1 and `(n)` is n + 1. Each deck's image count matches its PDF's pages.
- Each file goes into the store once, as a media row with `sha256`; the same export again adds no blobs and no rows.
- The detail view shows slides in order, plays audio and video, and links the PDF and PPTX. An app's HTML page is served only as a download, so its scripts never run in LeStash's origin.
- On a copy of the live database, from the March export: 73 artifacts (30 decks with 447 slides, 15 audio, 9 video, 15 infographics, 4 apps), 548 files, 540 new blobs (2.6 GB).
- Not yet: the Takeout source importing artifacts as it syncs; it downloads one part at a time.

## As built (#217, step 3): Kobo files and media versions

- The Kobo store keeps its snapshots in `<root>/kobo/snapshots`; their files are blobs in this store, not in a folder of their own.
- A Kobo `store_dir` of its own keeps its files there, as before; the import copies each file it attaches into this store.
- Files an older version kept in `kobo/blobs` move here on the next snapshot or Kobo sync: a rename, under the snapshot lock. A file this store already holds is checked by hash before the Kobo copy goes; a damaged copy here is replaced by a good one.
- The Kobo's temp files here are named `.tmp-kobo-…`, so its clean-up never removes a file another writer is storing.
- A sideloaded or Drive book's file (PDF or EPUB) is a media row on its item, pointing at its blob. Kobo store books are encrypted, and get none.
- A markup's JPG and SVG point at their blobs; the media folder gets no new copies.
- Media versions are in use: the import gives one media row per version of a file the snapshots have seen. A row is matched on its file and when it was first seen (`created_at`), within one item, type, position and origin, so an upload at the same place is never taken. Versions are numbered as they arrive and never renumbered. A replaced version stays replaced, so a lost snapshot never makes two rows current. "Earlier versions" are listed by when each was replaced. An older version has `superseded_at`, the time of the snapshot that replaced it, and is kept.
- The detail view shows current media, and lists older versions under "Earlier versions"; listings use current media only.
- `blobs` lists the Kobo files media point at; snapshot-only files (the Kobo's database, logs) are in the store but not in the table.
- On a copy of the live database: 581 Kobo files moved (18 already here), 38 book files attached (37 PDFs, 1 EPUB); a second import adds nothing. The snapshots so far hold one version of each book file.

## As built (#217, step 4): media-folder copies removed

- Each sync, after adopting, removes a media-folder file whose rows all point at one blob, that blob is in the store, and both hash the same; `lestash media prune --delete` does the same by hand, and without `--delete` only counts.
- A file stays when no row names it yet, a row naming it is not adopted, its rows name different blobs, or its stored copy is missing or damaged.
- Each removal checks the rows again under the write lock, so a row a writer has just recorded keeps its file.
- A writer that found the file just before it was removed records a row naming a missing file; the next adopt points it at the stored file another row names.
- A database restored from a backup taken before a prune finds each file in the store by its name, which is the start of its hash, at its next sync; until then those media are not served.
- Nothing is removed when another `*.db` shares the database's folder, since that database shares the media folder too.
- Writers still save into the media folder; their files stay there until the next sync stores them.
- A download is still named after the file it was saved as, not its hash.
- PDF enrichment and OCR read a source PDF from the store, else the media folder.
- A source PDF replaced by another file keeps its stored file on the `source_pdf_version` row; the current row waits for the next adopt.
