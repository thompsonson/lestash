# lestash-audible

Audible source plugin for Le Stash. Imports audiobook bookmarks, notes, and highlights using the reverse-engineered Audible API via [mkb79/audible](https://github.com/mkb79/Audible).

## Setup

### Authenticate

```bash
lestash audible auth --locale us
```

Supported marketplaces: us, uk, de, fr, au, ca, jp, it, in, es.

## Commands

### `lestash audible doctor`

Check authentication and library access.

### `lestash audible library`

List books in your Audible library with title, author, and runtime.

### `lestash audible fetch`

Fetch all books with bookmarks/notes and import them.

```bash
# Fetch all
lestash audible fetch

# Fetch a specific book
lestash audible fetch --asin B08G9PRS1K
```

## How It Works

- Each **book** with annotations becomes a parent item
- Each **bookmark/note** becomes a child item under its book
- Books without annotations are skipped
- Uses the `FionaCDEServiceEngine/sidecar` endpoint for bookmarks
- Credentials are stored locally after first authentication

## Data Model

| Item | `source_id` | Content |
|------|-------------|---------|
| Book | `audible:book:{ASIN}` | Title, author, narrator, series, runtime |
| Bookmark | `audible:bookmark:{ASIN}:{position_ms}` | Note text or position timestamp |
