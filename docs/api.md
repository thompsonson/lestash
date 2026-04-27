# Le Stash API Reference

Base URL: `/api`
Interactive docs: `/api/docs` (Swagger UI)

## Authentication

No API key required. Access is controlled via network (Tailscale) and CORS:
- Tailscale domains (`*.ts.net`)
- Tauri app origins
- Chrome extension origins (`chrome-extension://`)
- Local dev servers (`localhost:1420`, `localhost:5173`)

---

## Health

### `GET /api/health`

Server health check.

```json
{ "status": "ok", "version": "1.41.0", "items": 21536 }
```

---

## Items

### `GET /api/items`

List items with filters and pagination.

**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `source` | string | — | Filter by source type |
| `own` | bool | — | Filter own content |
| `exclude_subtype` | string | — | Comma-separated subtypes to exclude (e.g. `reaction,invitation`) |
| `since` | datetime | — | Only items fetched since this time |
| `tag` | string | — | Filter by tag |
| `parent_id` | int | — | Show children of this item |
| `include_children` | bool | false | Include items that have a parent |
| `limit` | int | 20 | Items per page (1–100) |
| `offset` | int | 0 | Pagination offset |

Items with a `parent_id` are excluded by default. Set `include_children=true` or filter by `parent_id` to see them.

**Response:** `{ items: ItemResponse[], total: int, limit: int, offset: int }`

---

### `GET /api/items/search`

Search items using keyword (FTS5), semantic (vector), or hybrid mode.

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | **required** | Search query (supports FTS5 syntax: AND, OR, NOT, "phrase", prefix*) |
| `limit` | int | 20 | Max results (1–100) |
| `include_children` | bool | true | Include child items |
| `mode` | string | hybrid | `keyword`, `semantic`, or `hybrid` |

Hybrid mode combines keyword + semantic results via Reciprocal Rank Fusion (k=60).

---

### `POST /api/items`

Create or update an item. Upserts on `(source_type, source_id)`.

**Request body:**

| Field | Type | Required | Default |
|---|---|---|---|
| `source_type` | string | yes | — |
| `source_id` | string | no | falls back to `url` |
| `url` | string | no | — |
| `title` | string | no | — |
| `content` | string | yes | — |
| `author` | string | no | — |
| `created_at` | datetime | no | — |
| `is_own_content` | bool | no | true |
| `metadata` | object | no | — |
| `parent_id` | int | no | — |

On conflict, updates: `content`, `title`, `author`, `metadata`, `parent_id`.

**Response:** `ItemResponse` (status 201)

---

### `GET /api/items/{item_id}`

Get a single item by ID. Returns 404 if not found.

---

### `PATCH /api/items/{item_id}`

Partially update an item. Only provided fields are changed.

**Request body:**

| Field | Type | Description |
|---|---|---|
| `title` | string \| null | Set or clear the title |
| `content` | string | Update content |
| `parent_id` | int \| null | Reparent the item or detach from parent |

Validates that `parent_id` does not create a circular reference.

**Response:** `ItemResponse`

**Errors:** 400 (circular ref, self-parent, missing parent, no fields), 404 (item not found)

---

### `GET /api/items/tags`

List all tags with usage counts.

```json
{ "tags": [{ "name": "research", "count": 42 }] }
```

---

### `POST /api/items/{item_id}/tags`

Add a tag to an item.

**Request:** `{ "name": "tag-name" }`
**Response:** Updated `ItemResponse` (status 201)

---

### `DELETE /api/items/{item_id}/tags/{tag_name}`

Remove a tag from an item.

---

## Collections

### `GET /api/collections`

List all collections.

```json
[{ "id": 1, "name": "Thesis", "description": "...", "item_count": 15, "created_at": "..." }]
```

---

### `POST /api/collections`

Create a collection.

**Request:** `{ "name": "Collection Name", "description": "optional" }`
**Response:** `CollectionResponse` (status 201)

---

### `GET /api/collections/{collection_id}`

Get a collection with all its items.

**Response:** `{ id, name, description, items: ItemResponse[], created_at }`

---

### `PUT /api/collections/{collection_id}`

Update a collection's name and description.

**Request:** `{ "name": "New Name", "description": "optional" }`

---

### `DELETE /api/collections/{collection_id}`

Delete a collection. Items are NOT deleted. (status 204)

---

### `POST /api/collections/{collection_id}/items`

Add an item to a collection. Duplicates are silently ignored.

**Request:** `{ "item_id": 123, "note": "optional annotation" }`
**Response:** Updated `CollectionResponse` (status 201)

---

### `DELETE /api/collections/{collection_id}/items/{item_id}`

Remove an item from a collection. (status 204)

---

## Sources

### `GET /api/sources`

List installed source plugins with sync status.

```json
[{ "name": "linkedin", "description": "...", "enabled": true, "last_sync": "..." }]
```

---

### `GET /api/sources/status`

Recent sync history (last 20 entries).

```json
[{
  "source_type": "linkedin",
  "started_at": "...", "completed_at": "...",
  "status": "completed",
  "items_added": 12, "items_updated": 3,
  "error_message": null
}]
```

---

### `POST /api/sources/{source_name}/sync`

Trigger a sync for a source plugin. Returns immediately; sync runs in background.

```json
{ "status": "started", "source": "linkedin" }
```

Returns 404 if source not found.

---

## Stats

### `GET /api/stats`

Knowledge base statistics.

```json
{
  "total_items": 21536,
  "sources": { "linkedin": 5000, "bluesky": 3000 },
  "own_content": 150,
  "date_range": { "earliest": "...", "latest": "..." },
  "last_syncs": { "linkedin": "..." }
}
```

---

## Embeddings

### `GET /api/embeddings/status`

Embedding coverage stats.

```json
{
  "model": "all-MiniLM-L6-v2",
  "dimensions": 384,
  "embedded": 18000, "total_parents": 20000,
  "coverage": "90%",
  "rebuilding": false
}
```

---

### `POST /api/embeddings/rebuild`

Trigger embedding rebuild in background. Prevents concurrent rebuilds.

```json
{ "status": "started", "message": "Rebuild started in background" }
```

---

### `GET /api/embeddings/similar/{item_id}`

Find semantically similar items. Query param: `limit` (default 10).

---

## Media

### `GET /api/media/{media_id}`

Serve a media file. Returns the file directly (FileResponse) or redirects to URL.

---

### `POST /api/items/{item_id}/media`

Upload a media attachment (multipart/form-data, max 50MB).

**Form field:** `file`
**Response:** `MediaResponse` (status 201)

---

### `DELETE /api/media/{media_id}`

Delete a media attachment. (status 204)

---

## Voice

### `POST /api/voice/transcribe`

Upload audio, transcribe via Whisper, save as voice note item.

**Form fields:**
- `file` — audio file (.m4a, .mp3, .wav, .ogg, .flac, .webm; max 50MB)
- `model` — Whisper model (default: `base.en`)
- `title` — optional title override

**Response:**
```json
{
  "text": "transcribed text",
  "language": "en",
  "duration_seconds": 45.2,
  "model": "base.en",
  "item_id": 12345,
  "title": "Voice note 2026-04-04 12:30"
}
```

---

### `POST /api/voice/refine`

Refine a transcript using an LLM.

**Request:** `{ "text": "raw transcript", "prompt": "optional custom prompt", "model": "optional model" }`
**Response:** `{ "refined_text": "...", "model_used": "...", "prompt_used": "..." }`

Requires LLM proxy at `LESTASH_LLM_URL` (default: `http://localhost:4000`).

---

### `POST /api/voice/upload`

Upload raw audio to cache (no transcription). Max 50MB.

---

## Imports

### `POST /api/import`

Import from an uploaded file (multipart/form-data, max 50MB).

**Supported formats:**
- `.json` — JSON array of items
- `.zip` — auto-detects: Google Keep Takeout, Gemini Takeout, NotebookLM, Mistral Le Chat, generic JSON

**Response:**
```json
{
  "status": "completed",
  "source_type": "gemini",
  "items_added": 25, "items_updated": 0,
  "errors": []
}
```

Parent-child relationships are resolved automatically (two-pass insertion).

---

### `POST /api/import/drive`

Import files from Google Drive by file ID.

**Request:** `{ "file_ids": ["1abc...", "https://drive.google.com/..."] }`
**Response:** `ImportResponse[]` (one per file)
**Requires:** Google OAuth configured

---

### `POST /api/import/drive/sync`

Import Google Drive files/folders with Docling markdown conversion.

**Request:** `{ "urls": ["https://drive.google.com/...", "https://docs.google.com/..."] }`
**Response:** `{ "status": "completed", "items_added": 10, "items_skipped": 2, "errors": [] }`

Accepts Drive URLs, Docs URLs, folder URLs, or bare file IDs. Processes folders recursively.

---

## Google OAuth

### `GET /api/google/auth-status`

Check if Google OAuth is configured and authenticated.

```json
{ "authenticated": true, "scopes": ["...youtube.readonly", "...drive.readonly"] }
```

---

### `GET /api/google/auth-url`

Generate OAuth consent URL. Query param: `scopes` (comma-separated, defaults to YouTube + Drive).

Requires `~/.config/lestash/client_secrets.json`.

---

### `POST /api/google/auth-complete`

Complete OAuth flow. **Request:** `{ "code": "...", "state": "..." }`

---

### `GET /api/google/android-config`

Public OAuth config for the Android app. Used by the Tauri plugin to call
`AuthorizationClient.requestOfflineAccess()`.

**Response:** `{ "web_client_id": "..." }`

Requires a `web` section in `~/.config/lestash/google_client_secrets.json`.
Both client types can live side-by-side in one file (the format Google
Cloud Console emits when a project has both):

```json
{
  "installed": { "client_id": "...desktop...", "client_secret": "...", ... },
  "web":       { "client_id": "...web...",     "client_secret": "...", ... }
}
```

The Android app additionally needs an **Android OAuth client** registered
in Google Cloud Console with package `dev.lestash.app` and the SHA-1
fingerprint of the signing keystore. That client has no secret and is
identified implicitly by the package + signature, so it doesn't appear in
this file.

---

### `POST /api/google/android-auth-complete`

Exchange a server auth code from Android Identity Services for credentials.
The Android app obtains the code via `AuthorizationClient.authorize()` (with
the Web client id from `/android-config`) and posts it here. The server
exchanges it using the Web client secret.

**Request:** `{ "code": "...", "granted_scopes": ["..."] }`
**Response:** `{ "status": "ok", "scopes": ["..."] }`

---

## Audible OAuth

### `GET /api/audible/auth-status`

Check Audible auth. Requires `lestash-audible` plugin.

---

### `GET /api/audible/auth-url`

Generate Audible OAuth URL. Query param: `locale` (default: `uk`).

---

### `POST /api/audible/auth-complete`

Complete Audible auth. **Request:** `{ "redirect_url": "...", "state_token": "..." }`

---

## LinkedIn (optional plugin)

Requires `lestash-linkedin` with posting credentials configured.

### `GET /api/linkedin/auth-status`

### `GET /api/linkedin/auth-url`

Query param: `redirect_uri` (required).

### `GET /api/linkedin/auth-callback`

OAuth callback endpoint.

### `POST /api/linkedin/post`

Create a text/article post.

**Request:**
```json
{
  "text": "Post content (max 3000 chars)",
  "visibility": "PUBLIC",
  "article_url": "optional",
  "article_title": "optional",
  "article_description": "optional"
}
```

### `POST /api/linkedin/post-with-image`

Create a post with image (multipart/form-data).

**Form fields:** `image`, `text`, `visibility` (default: `PUBLIC`)

---

## YouTube

### `POST /api/youtube/fetch-transcript`

Fetch and store a YouTube video transcript.

**Request:** `{ "url": "https://youtube.com/watch?v=..." }`
**Response:** `{ "item_id": 123, "title": "Video Title", "word_count": 5000 }`

Accepts YouTube URLs, youtu.be links, or bare video IDs. Requires `lestash-youtube` plugin.

---

## Shared Response Models

### ItemResponse

```json
{
  "id": 1,
  "source_type": "linkedin",
  "source_id": "urn:li:activity:123",
  "url": "https://...",
  "title": "Post title",
  "content": "Full content...",
  "author": "urn:li:person:456",
  "created_at": "2025-02-01T00:00:00",
  "fetched_at": "2025-02-01T12:00:00",
  "is_own_content": true,
  "metadata": {},
  "parent_id": null,
  "subtype": "article",
  "author_display": "John Doe",
  "actor_display": "John Doe",
  "preview": "First 120 chars of content...",
  "tags": ["research", "ai"],
  "child_count": 3,
  "media": [{
    "id": 1,
    "media_type": "image",
    "url": "https://...",
    "serve_url": "/api/media/1",
    "alt_text": null,
    "position": 0,
    "available": true
  }]
}
```

---

## Error Format

All errors return:

```json
{ "detail": "Error description" }
```

Common status codes: 400 (bad request), 404 (not found), 413 (too large), 422 (validation), 500 (server error), 501 (plugin not installed).

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LESTASH_LLM_URL` | `http://localhost:4000` | LLM proxy URL for voice refinement |
| `LESTASH_LLM_MODEL` | `claude-sonnet-4-20250514` | Default LLM model |
| `LESTASH_STATIC_DIR` | — | Directory for serving frontend SPA |
