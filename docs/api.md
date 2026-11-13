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

Server health check. `version` is the installed `lestash-server` release.

```json
{ "status": "ok", "version": "1.70.0", "items": 45555 }
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
| `exclude_subtype` | string | — | Comma-separated terms; drops items whose subtype contains any (e.g. `reaction,invitation`). Applied before paging, so pages are full and `total` is exact |
| `kind` | string | — | Comma-separated kinds to keep (e.g. `book,paper`); see `core/kinds.py` |
| `exclude_kind` | string | — | Comma-separated kinds to leave out (e.g. `reaction`) |
| `person` | int | — | Items this person made, voiced, published or received (children included) |
| `role` | string | — | With `person`: only this role (`author`, `creator`, `narrator`, `publisher`, `recipient`) |
| `year` | `YYYY` | — | Made in this year (`created_at`) |
| `sort` | string | `recent` | `recent`, `oldest` or `title` |
| `top_level` | bool | false | Only items not part of another, even with `tag` or `person` (which otherwise include parts) |
| `since` | datetime | — | Only items fetched since this time |
| `tag` | string | — | Filter by tag |
| `parent_id` | int | — | Show children of this item |
| `include_children` | bool | false | Include items that have a parent |
| `limit` | int | 20 | Items per page (1–100) |
| `offset` | int | 0 | Pagination offset |

Items with a `parent_id` are excluded by default. Set `include_children=true` or filter by `parent_id` to see them.

Stubs (`held: false`) are always excluded, unless filtering by `parent_id`. A stub is a thing LeStash knows by its address but does not hold, such as a LinkedIn post you commented on. Search and `/api/stats` exclude stubs too.

**Response:** `{ items: ItemResponse[], total: int, limit: int, offset: int }`

---

### `GET /api/items/search`

Search items using keyword (FTS5), semantic (vector), or hybrid mode.

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | **required** | Search query; see the syntax below |
| `limit` | int | 20 | Max results (1–100) |
| `group` | bool | true | One result per item: a part's match (a chat's round, a post's reply, a book's highlight) counts for its item, at its best part's rank |
| `include_children` | bool | true | With `group=false`: list parts as results of their own (true) or drop them (false) |
| `mode` | string | hybrid | `keyword`, `semantic`, or `hybrid` |
| `kind` | string, repeatable | all | Only results of these kinds (`chat`, `paper`, `book`...; `core/kinds.py`). Grouped, a part is read as its item's kind |

The response adds `facets`: `[{kind, label, count}]`, the kinds among everything the search found (the candidates it fuses, about four times `limit`), before any `kind` filter, most first. A `kind` filter keeps those same results of that kind, best first, so a chip's count is what it returns, up to `limit`. The app shows them as chips.

Hybrid mode combines keyword + semantic results via Reciprocal Rank Fusion (k=60).

A result found by a part carries `match_id` (the part) and `match_label` (its kind, such as "Turn"). Its `preview` is the part's matched words (keyword) or its opening (semantic). The app opens such a result at the part (#221).

**Keyword syntax** (`lestash.core.search`, shared with `lestash items search`):

- `"exact phrase"`, `word*` prefix, and `AND`, `OR`, `NOT` in upper case.
- A `-` in front of a quoted phrase excludes it, as in web search: `notes -"draft copy"` is `notes NOT "draft copy"`. In front of a word it is part of the word, so flags and numbers (`--dry-run`, `-40`) search as written; use `NOT` to exclude a word.
- Every word also matches as a prefix: `learn` finds `learning`.
- All other punctuation is plain text: `Dual-State`, `v2.0` and `don't` match the words as written.
  - Characters the index drops, such as `+`, are dropped from the query too: `C++` searches `C*`.
- FTS5 column filters (`title:x`), `NEAR`, `^` and parentheses are not supported; they are searched as words. Without grouping, FTS5 binds `AND` tighter than `OR`.
- `NOT x` on its own, or `a OR NOT b`, can't be expressed; the excluded term is dropped, never searched.
- A word with no letter or digit (`-`, `++`) is dropped, with an `AND` or `NOT` just before it; an `OR` stays.
- A query with nothing searchable (`++`) returns no keyword results.

**Errors:** `422` without `q`. `500` ("Keyword search failed" or "Search unavailable") when the search can't run:

- `keyword`: the search index fails.
- `semantic`: the vector search fails.
- `hybrid`: one half failing is logged and the other half's results are returned; `500` if both fail, or if the keyword half fails and semantic finds nothing.

Unknown paths under `/api` return a JSON `404` for every method, even when the server also serves the web app.

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

Every item, here and in lists, carries its byline: `by_id`, `by_name` and `by_self`. It is the item's linked author (or creator), else you for your own posts, replies, notes, chats, messages, emails, documents, notebooks and projects. You are named by your short name ("Matt"); anyone else in full. Reactions, videos, searches, highlights and generated items have no byline: a reaction's author is the post's, a video's is its channel.

---

### `GET /api/items/{item_id}/activities`

The item's activities (watches, searches, prompts), newest first. `404` if the item doesn't exist.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 100 | Most activities to return (1–1000) |

```json
{
  "activities": [
    {"id": 7, "verb": "searched", "occurred_at": "2026-01-17T23:20:58.043000Z",
     "source": "takeout:youtube", "context_id": null, "details": {"query": "markov blanket"},
     "actor_id": null, "actor": null}
  ],
  "total": 1
}
```

`actor_id` and `actor` name the person who did it, such as someone who liked your LinkedIn post; null is you.

---

### `GET /api/items/{item_id}/links`

The item's confirmed links to and from other items, found by the linker after each sync. `404` if the item doesn't exist.

- `references`: the `from` item's content holds the `to` item's web address.
- `syndication`: the `to` item is a cross-posted copy of the `from` item, a Micro.blog post.
- `in_reply_to`: the `from` item is a comment on the `to` item, often a stub for a post LeStash doesn't hold.
- `direction` is `outgoing` when this item is the link's `from` end.

```json
{
  "links": [
    {"direction": "incoming", "kind": "references",
     "evidence": "https://matt.thompson.gr/2026/01/18/definitions-reference.html",
     "origin": "linker:url",
     "item": {"id": 2081, "source_type": "claude", "title": "Agents", "url": null,
              "preview": "Agents and the definitions they rely on..."}}
  ]
}
```

`item.preview` is the start of the item's text (120 characters, whitespace folded), so an item without a title, such as a comment, can be told apart.

---

### `GET /api/items/{item_id}/agents`

Who made, voiced, published or received the item, ordered by role and position. `404` if the item doesn't exist.

Roles: `author`, `creator` (a channel's video), `narrator`, `publisher`, `recipient`. After them come the people the item mentions (tags), with role `mentioned`. A person's `kind` is `person`, `organization`, `group` or `software`; `is_self` marks you.

```json
{
  "agents": [
    {"role": "creator", "position": null,
     "person": {"id": 12, "name": "BBC News", "kind": "organization", "is_self": false},
     "accounts": [{"platform": "youtube", "identifier": "UC16niRr50-MSBwiO3YDb3RA",
                   "url": "https://www.youtube.com/channel/UC16niRr50-MSBwiO3YDb3RA"}]}
  ]
}
```

---

### `DELETE /api/items/{item_id}/links`

Remove a link. It is marked `rejected`, so it stops showing and the linker never adds it again. `404` if there is no such link.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `other_id` | int | — | The item at the link's other end |
| `kind` | string | — | The link's kind, such as `references` |
| `direction` | string | `outgoing` | `outgoing` or `incoming`, as `GET` lists it |

Returns `{"status": "rejected"}`.

---

### `PATCH /api/items/{item_id}`

Partially update an item. Only provided fields are changed.

**Request body:**

| Field | Type | Description |
|---|---|---|
| `title` | string \| null | Set or clear the title |
| `content` | string | Update content |
| `parent_id` | int \| null | Reparent the item or detach from parent |

Returns 409 for a stub (`held: false`): a stub is merged into the thing itself once imported, so an edit would be lost.

Validates that `parent_id` does not create a circular reference.

A synced item that names its parent in `metadata._parent_source_id` is linked to that parent
again by its source's next sync. So detaching or reparenting such an item lasts only until
then. Other items keep the parent you set across `lestash sources sync` and server syncs.

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

## Timeline

### `GET /api/timeline`

What you did and what you made between two times, newest first (#262).

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `start` | ISO time | required | From this time; a day's start in your zone, e.g. `2026-03-10T00:00:00+01:00` |
| `end` | ISO time | required | Up to this time, not included |
| `verb` | string | — | Comma-separated verbs to keep (`watched`, `liked`, ...); `made` for items you made |
| `kind` | string | — | Comma-separated item kinds to keep |

Each entry has `at` (UTC), `verb` (an activity's verb, or `made`), `item` (id, title, kind, kind_label, kind_detail, source_type, parent, channel), `context` (e.g. the notebook a prompt used), `details`, and for `prompted` entries `prompt`: the start of what you asked (up to 280 characters), from your turn in the chat at that time. `made` entries are posts, replies, notes, notebooks, chats, generated items, projects and email threads, on the day they were made. `previous` and `next` are the nearest times outside the span with entries. Up to 5,000 entries; `truncated` says when there were more.

## Library

### `GET /api/library`

The shelves, each with its kinds and how many top-level items it holds (#264): Books (`book`), Papers (`paper`), Videos (`video`), Notebooks (`notebook`, `project`), Posts (`post`), Chats (`chat`), Notes (`note`). A shelf's items are `GET /api/items?kind=<its kinds>`.

### `GET /api/library/{shelf}/facets`

What a shelf holds, counted by `people` (in any role, top 30), `topics` (tags, top 30) and `years`. 404 for an unknown shelf. The counts match `GET /api/items?kind=<shelf kinds>&top_level=true` with the same filter.

## People

### `GET /api/people`

People, channels or AIs, those with the most items first (#263).

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `switch` | string | `people` | `people` (person, group), `channels` (organization) or `ai` (software) |
| `q` | string | — | Names containing this |
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | |

### `GET /api/people/{person_id}`

One person: `accounts`, `made` (their items by role and kind), `mentioned_in`, and `your_activity` (what you did to their items, by verb). Each verb has `count` (times, rewatches included), `items` (different items), `first_at` and `last_at`. `GET /api/people/me` is you.

Every person has `short_name`: what a byline calls them. It is the name's first word until set.

### `PATCH /api/people/{person_id}`

Set what bylines call a person: `{ "short_name": "Matt" }`, up to 40 characters. An empty `short_name` goes back to the first word of the name. Returns the person; `404` if there is none.

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

## Profiles

LinkedIn gives no names for members. A profile is the name and profile URL of a member's account and person, in the people tables.

### `GET /api/profiles`

The LinkedIn members with a known name or profile URL.

```json
[{"urn": "urn:li:person:abc", "profile_url": "https://linkedin.com/in/ann", "display_name": "Ann Lee", "source": "web"}]
```

`source` says who named them: `web` or `manual` when you did, else `mention`, a name LinkedIn tags write.

### `PUT /api/profiles/{urn}`

Name a member, and optionally set their profile URL. Body: `{"display_name": "Ann Lee", "profile_url": "https://..."}`.
The person takes the name, and the agents pass keeps it. A member not yet seen gets an account.

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

- `embedded` counts top-level items with a vector of today's model; `parts_embedded` the children's (chat rounds, comments, highlights...).
- `total_parents` counts held top-level items that should have a vector: not empty, and not a video with only its title.
- `out_of_date` counts those with no vector of today's model and recipe; a refresh also finds those whose text has changed.
- `building` is true after a change of model, while the new index is built beside the old one. Search then uses `query_model`, the model of the old index.

```json
{
  "model": "google/embeddinggemma-300m",
  "recipe": "title+content500",
  "dimensions": 768,
  "query_model": "all-MiniLM-L6-v2",
  "building": true,
  "embedded": 18000, "parts_embedded": 13000, "out_of_date": 2000, "total_parents": 20000,
  "coverage": "90%",
  "rebuilding": false
}
```

---

### `POST /api/embeddings/rebuild`

Embed, in the background, every item that is new or changed, and drop vectors of items gone (the same as `lestash embeddings refresh`, with no time limit). Prevents concurrent rebuilds.

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
The transcript is linked as a child of the video item if one exists (a `liked:`/
`history:`/`shared:` YouTube item, or any item whose URL references the video).

### `POST /api/youtube/import-video`

Import a YouTube video as a canonical `youtube` item (subtype `shared`).

**Request:** `{ "url": "https://youtube.com/watch?v=...", "note": "optional note" }`
**Response:** `{ "item_id": 123, "title": "Video Title", "created": true }`

Dedup-safe: if a YouTube video item for this id already exists (any subtype), it is
returned (`created: false`) instead of creating a duplicate. Requires `lestash-youtube`
plugin and YouTube auth to fetch metadata for a new video.

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
  "held": true,
  "subtype": "article",
  "author_display": "John Doe",
  "actor_display": "John Doe",
  "preview": "First 120 chars of content...",
  "tags": ["research", "ai"],
  "child_count": 3,
  "reaction_count": 1,
  "kind": "post",
  "kind_label": "Post",
  "kind_detail": null,
  "parent_title": null,
  "parent_kind": null,
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

- `kind` is what the item is, generated by the database (`items.kind`, migration 24): `post`, `comment`, `reaction`, `message`, `video`, `search`, `transcript`, `paper`, `book`, `highlight`, `chat`, `turn`, `notebook`, `artifact`, `project`, `document`, `note`, `email`, `image`, `stub`, `other`.
- `kind_detail` is a finer label where one exists, such as `"Audio overview"` for a generated artifact.
- `reaction_count` counts the children that are reactions; `child_count` includes them.
- `parent_title` and `parent_kind` describe the item this one is part of.

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
