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

- `lestash media adopt` copies each media file not yet in the store into it and sets its row's `sha256`. The file in the media folder stays.
- Each `lestash sources sync` and each upload through `/api/items/{id}/media` adopts new media the same way.
- Media written elsewhere (PDF enrichment, imports) wait for the next sync or `lestash media adopt`; until then they are served from the media folder.
- `/api/media/{id}` serves the blob from the store, else the media folder's file, else redirects to the remote URL.

## As built (#217, step 1)

- Files are copied with no write open, then recorded in one short transaction; a file that cannot be read is skipped and tried next time, so it never fails a sync.
- On a copy of the live database: 755 media files adopted as 617 blobs (155 MB, against 175 MB in the media folder); a second run adds none.
- Not yet: the Kobo store's blobs moving into this store, media versions in use, NotebookLM artifacts, and removing the media folder's copies.

## As built (#217, step 2): NotebookLM artifacts

- `lestash media notebooklm <zip>…` imports a Takeout's NotebookLM generated artifacts with their files. Give every part: a notebook's files are spread over them.
- Each artifact (slides, audio overview, video overview, infographic, app) becomes a child item of its notebook, with `type: artifact`. Reports are skipped: they are notes already.
- A file belongs to the description whose name is its own, else the longest one either name begins with, among descriptions of a type the file fits (a deck folder, `.mp4`, `.wav`, `.png`, an extensionless app page). Names are cut at 51 bytes, even through " metadata"; two artifacts of one title are "x metadata" and "x metadata(1)".
- Slide images keep their numbers: `_-_Slide_7` is 7; in a deck named `_-_Slide`, `_-_Slide(1)`, … the bare name is 1 and `(n)` is n + 1. Each deck's image count matches its PDF's pages.
- Each file goes into the store once, as a media row with `sha256`; the same export again adds no blobs and no rows.
- The detail view shows slides in order, plays audio and video, and links the PDF and PPTX. An app's HTML page is served only as a download, so its scripts never run in LeStash's origin.
- On a copy of the live database, from the March export: 73 artifacts (30 decks with 447 slides, 15 audio, 9 video, 15 infographics, 4 apps), 548 files, 540 new blobs (2.6 GB).
- Not yet: the Takeout source importing artifacts as it syncs; it downloads one part at a time.
