# Plan — YouTube transcripts as sub-items

## Problem

Sharing a YouTube link into LeStash produces two orphaned top-level items instead of a
video with its transcript nested underneath:

1. The capture flow (`app/src/index.html:3151`) creates a generic `share` item
   (`source_id = 'share-' + Date.now()`, url = the YouTube link), then calls
   `POST /api/youtube/fetch-transcript {url}` **without passing the share item as parent**.
2. The transcript route (`packages/lestash-server/src/lestash_server/routes/youtube.py:79`)
   resolves its parent only via `WHERE source_id = 'liked:{video_id}'`. A `share` item never
   matches → transcript is orphaned.

Deeper issue: the same video can exist as `liked:{vid}`, `history:{vid}`, or a timestamped
`share` blob — three identities for one video. The orphan transcript is the first symptom of
a missing canonical-identity model.

DB confirms 6 orphan transcripts; the weekend two (`8vHKCrNGPhY`, `DxL2HoqLbyA`) have `share`
items as their "videos" (ids 26663 / 26630).

---

## Layer 1 — Correctness (parent resolution + backfill)

Source-type-agnostic linking. Makes transcripts nest under whatever item represents the video
today (including `share`).

### 1.1 Shared parent-resolution helper
**File:** `packages/lestash-youtube/src/lestash_youtube/source.py`

Add:
```python
def resolve_transcript_parent(conn, video_id: str) -> int | None:
    """Find the best existing item to parent a transcript to.

    Preference order:
      1. A real youtube video item (liked:/history:/shared:), excluding transcripts.
      2. Any item whose URL contains the video_id (e.g. a generic `share` capture).
    """
```
- Query 1: `source_type='youtube' AND source_id LIKE '%:'||? AND source_id NOT LIKE 'transcript:%'`
  (param = video_id), ordered to prefer non-transcript youtube items.
- Query 2 (fallback): `url LIKE '%'||?||'%'` (param = the 11-char video_id; collision risk
  negligible) excluding the transcript's own row and other `transcript:` items.
- Return the first match id, else `None`.

### 1.2 Use the helper at both creation sites
- **Server:** `routes/youtube.py:79-84` — replace the hard-coded `liked:` SELECT with
  `resolve_transcript_parent(conn, video_id)`.
- **CLI:** `source.py:688-697` (`fetch-transcript` command) — same replacement.
- Both already `metadata.pop("_parent_source_id", None)` — keep that.

### 1.3 Post-sync backfill hook
**File:** `packages/lestash-youtube/src/lestash_youtube/source.py`

Add `resolve_youtube_transcript_parents(conn) -> int`, mirroring
`resolve_linkedin_parents` (`packages/lestash-linkedin/src/lestash_linkedin/source.py:71`):
```sql
UPDATE items SET parent_id = (<resolve query by metadata.$.video_id>)
WHERE source_type='youtube' AND source_id LIKE 'transcript:%'
  AND parent_id IS NULL
  AND EXISTS (<same resolve query>)
```
Wire into the CLI sync post-sync block (`packages/lestash/src/lestash/cli/sources.py:207`,
next to the LinkedIn branch):
```python
if name == "youtube":
    from lestash_youtube.source import resolve_youtube_transcript_parents
    resolved = resolve_youtube_transcript_parents(conn)
    if resolved:
        console.print(f"  [dim]Resolved {resolved} transcript parent(s)[/dim]")
```
Re-parents orphans whenever the video later appears.

### 1.4 One-time backfill of existing orphans
- Run `resolve_youtube_transcript_parents` once (links the 3 with `liked:` parents +,
  via the URL fallback, the weekend 2 to their `share` items).
- `8xp3Bs6nZ-Y` has no video item at all → stays orphan until its video is imported (expected).

### 1.5 (Optional) thread parent through the capture flow
`app/src/index.html:3151` — capture the id returned by `POST /api/items` and pass it to
`fetch-transcript`. Superseded by Layer 2; skip if doing Layer 2.

### 1.6 Tests
- `packages/lestash-youtube/tests/` — unit-test `resolve_transcript_parent` for
  liked / history / share-url / none cases, and `resolve_youtube_transcript_parents` backfill.

---

## Layer 2 — Canonical YouTube item ("share becomes a youtube item")

A shared YouTube URL mints a first-class `youtube` item (enriched, dedup-safe) instead of a
generic `share` blob. Transcript nesting + dedupe then fall out for free.

### 2.1 Single-video metadata helper
**File:** `packages/lestash-youtube/src/lestash_youtube/client.py`

Add `get_video_details(youtube, video_id) -> dict | None`:
- `videos().list(part="snippet,contentDetails,statistics", id=video_id)`.
- Shape into the dict `video_to_item` consumes (id, title, description, channel_title,
  channel_id, duration, thumbnails, published_at, view/like/comment counts).
- Replaces the inline minimal `snippet` fetch in `routes/youtube.py:67` and `source.py:676`.

### 2.2 `shared` subtype
**File:** `source.py` `video_to_item`
- Accept `source_subtype="shared"`; `created_at` already falls back to `published_at`.
- `is_own_content=False`. Carry an optional user `note` into `metadata["notes"]`.

### 2.3 Dedup-safe import endpoint
**File:** `routes/youtube.py`

`POST /api/youtube/import-video {url, note?}`:
1. Extract video_id (reuse `_extract_video_id`).
2. If a `youtube` item for that video_id already exists (any subtype) → return its id
   (optionally merge `note`/shared provenance into metadata). **No duplicate row.**
3. Else `get_video_details` → `video_to_item(subtype="shared")` → `upsert_item`.
4. Return `{item_id}`.

Dedup via "exists by video_id" check avoids any schema migration of existing
`liked:`/`history:` source_ids. Full canonical `youtube:{video_id}` keying is a later
follow-up if desired.

### 2.4 Capture flow change
**File:** `app/src/index.html:3151` `saveCaptureItem`
- When `_isYouTubeUrl(url)`:
  - Call `/api/youtube/import-video {url, note}` instead of creating a generic `share` item.
  - Then call `/api/youtube/fetch-transcript {url}` — transcript nests via Layer 1 resolution.
- Non-YouTube URLs keep the existing generic `share` path unchanged.

### 2.5 (Optional) backfill existing share-of-youtube items
One-time, behind an explicit admin command (touches/deletes rows → not automatic):
- Find `source_type='share'` items whose URL is a YouTube video.
- Mint the youtube item, re-parent the transcript, convert/delete the share row.
- Apply to the weekend 2 to fully migrate them to the new model.

### 2.6 (Future, not built) share-router
Generalize 2.3/2.4: each source plugin exposes `url_matches(url)` + `ingest_url(url)`;
capture dispatches a recognized URL to its owning plugin (YouTube, Bluesky, arXiv, LinkedIn);
generic `share` becomes the fallback for unrecognized URLs.

### 2.7 Tests
- `get_video_details` shaping (mock API response).
- `import-video` dedup: second call for same video_id returns existing id, no new row.
- Capture integration: YouTube URL → youtube item + nested transcript.

---

## Ordering & risks
- Land Layer 1 first (immediate fix + backfill); Layer 2 builds on its resolution helper.
- Guard against self-parenting: exclude `transcript:%` source_ids in both queries.
- URL-`LIKE` fallback: match on the 11-char video_id (collision negligible); excludes
  transcript rows to avoid mis-linking.
- After Layer 2, the URL fallback in 1.1 becomes rarely-needed but stays as a safety net.
- Standard gate before commit: `uv run ruff check/format`, `uv run mypy packages/`,
  `uv run just test-all`.
