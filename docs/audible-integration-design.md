# Design: Audible Integration (lestash-audible)

## Context

Add an Audible source plugin to import audiobook bookmarks, notes, and highlights into LeStash. This lets you capture and search annotations made while listening — currently these are locked inside the Audible app with no export.

## How Audible API Access Works

There is **no official Audible API**. Access is via the reverse-engineered [`mkb79/audible`](https://github.com/mkb79/Audible) Python library (390+ stars, actively maintained, latest release v0.10.0).

### Authentication
- Amazon account credentials (email + password) — no OAuth
- Device registration (virtual device created on first auth)
- CAPTCHA may be required (library handles via Pillow prompt or custom callback)
- 2FA supported via OTP callback
- Tokens stored locally after first auth (no re-login needed)

### Key Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /1.0/library` | Fetch user's audiobook library (titles, authors, ASINs, covers) |
| `GET /1.0/catalog/products/{ASIN}` | Detailed book metadata |
| `GET https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/sidecar?type=AUDI&key={ASIN}` | **Bookmarks, clips, and notes** for a specific book |

### Bookmark Data Format

The sidecar endpoint returns JSON with:
- **Clips/bookmarks**: position (milliseconds), chapter info, creation timestamp
- **Notes**: text content attached to a position, creation timestamp
- **Last position**: where you stopped listening

## What Gets Imported

Each **book** becomes a parent item. Each **bookmark/note** becomes a child item under that book.

| Item | `source_type` | `source_id` | `parent_id` | Content |
|------|---------------|-------------|-------------|---------|
| Book | `audible` | `audible:book:{ASIN}` | NULL | Book title + author |
| Bookmark | `audible` | `audible:bookmark:{ASIN}:{position}` | book's item ID | Note text or "Bookmark at {chapter} {timestamp}" |

### ItemCreate Fields

**Book (parent):**
```python
ItemCreate(
    source_type="audible",
    source_id=f"audible:book:{asin}",
    title=book_title,
    content=f"{book_title} by {author}",
    author=author,
    url=f"https://www.audible.com/pd/{asin}",
    metadata={"asin": asin, "runtime_minutes": runtime, "cover_url": cover},
)
```

**Bookmark/Note (child):**
```python
ItemCreate(
    source_type="audible",
    source_id=f"audible:bookmark:{asin}:{position_ms}",
    title=f"Bookmark in {book_title}",
    content=note_text or f"Bookmark at {chapter_name} ({formatted_time})",
    author=author,
    created_at=bookmark_created_at,
    parent_id=book_item_id,  # resolved after book is inserted
    metadata={"asin": asin, "position_ms": position, "chapter": chapter_name},
)
```

## Plugin Structure

```
packages/lestash-audible/
├── pyproject.toml
├── src/lestash_audible/
│   ├── __init__.py          # Exports AudibleSource
│   ├── source.py            # SourcePlugin impl + CLI commands
│   └── client.py            # Wrapper around mkb79/audible library
└── tests/
    └── test_extractors.py
```

### Dependencies

```toml
[project]
dependencies = ["lestash", "audible>=0.10.0"]

[project.entry-points."lestash.sources"]
audible = "lestash_audible:AudibleSource"
```

## CLI Commands

```bash
# Authenticate with Audible (one-time)
lestash audible auth

# Check connection and library size
lestash audible doctor

# Fetch all books + their bookmarks/notes
lestash audible fetch

# Fetch bookmarks for a specific book
lestash audible fetch --asin B08G9PRS1K

# List books in your library
lestash audible library
```

### Auth Flow

```bash
$ lestash audible auth
Email: user@example.com
Password: ********
[CAPTCHA image displayed if required]
CAPTCHA answer: xxxxx
2FA code (if enabled): 123456
✓ Authenticated and device registered
✓ Credentials saved to ~/.config/lestash/audible_auth.json
```

The `audible` library handles device registration and token storage. Credentials persist — no re-login needed unless tokens expire.

## Sync Behaviour

`sync()` flow:
1. Load stored auth from `~/.config/lestash/audible_auth.json`
2. Fetch library (`GET /1.0/library`)
3. For each book: fetch sidecar bookmarks/notes
4. Yield book as parent `ItemCreate`
5. Yield each bookmark/note as child `ItemCreate` (with `_parent_source_id` marker for two-pass resolution, same pattern as NotebookLM import)

Books without bookmarks/notes are skipped (no value in importing empty entries).

## Key Risks

- **Unofficial API**: Amazon could change endpoints or block access. The `mkb79/audible` library has tracked changes for 4+ years.
- **CAPTCHA**: May require manual intervention on first auth. Subsequent calls use stored tokens.
- **Rate limits**: Unknown/undocumented. Fetch conservatively with delays between requests.
- **Regional differences**: Audible has separate marketplaces (US, UK, DE, etc.). The `audible` library supports marketplace selection.

## References

- [mkb79/audible](https://github.com/mkb79/Audible) — Python API client
- [mkb79/audible-cli](https://github.com/mkb79/audible-cli) — CLI built on top
- [GGyll/audible-bookmark-extractor](https://github.com/GGyll/audible-bookmark-extractor) — Bookmark extraction + transcription
- [Sidecar endpoint discussion](https://github.com/mkb79/audible-cli/discussions/73)
- [audible.readthedocs.io](https://audible.readthedocs.io/) — Library docs
