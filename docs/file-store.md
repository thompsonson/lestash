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
