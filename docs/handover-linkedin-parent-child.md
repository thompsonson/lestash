# Handover: LinkedIn Parent-Child Backfill

> **Status: Implemented** (2026-03-30) — see commits on `feat/vector-search` branch.

## Objective

Populate `parent_id` on LinkedIn reactions and comments so they become children of their parent post, when that post exists in the database.

Currently, LinkedIn likes/comments store a reference to their parent post in metadata fields, but `parent_id` is NULL. This means they show up as top-level items in the feed rather than being grouped under the post they relate to.

## Current State

### Schema

The `items` table has a `parent_id INTEGER` column (migration v5, already applied). It's used by NotebookLM (notebooks → notes/chats), Claude (conversations → messages), and Gemini conversations. LinkedIn items don't use it yet.

### How LinkedIn items reference parents

LinkedIn reactions and comments store their parent reference as URN strings in `metadata`:

- **Reactions**: `metadata.reacted_to` = URN string (e.g. `urn:li:activity:7420083185738424320`)
- **Comments**: `metadata.commented_on` = URN string (e.g. `urn:li:activity:7420083185738424320`)

These are the `activity` URNs of the post being reacted to or commented on.

### How LinkedIn posts are identified

LinkedIn posts are stored with `source_type = "linkedin"`. The activity URN appears in different places depending on how the item was ingested:

- **Changelog API posts**: `metadata.post_id` contains the activity URN
- **Snapshot API posts**: the URN may be in `source_id` or `metadata.raw`
- **URL field**: some posts have `url` set to a LinkedIn feed URL containing the activity ID

The key challenge is **matching the URN in `reacted_to`/`commented_on` to the correct post item's `id`**. There is no single consistent column that stores the activity URN across all ingestion paths.

### Relevant code

- **Database schema**: `packages/lestash/src/lestash/core/database.py` — items table, parent_id column, migrations
- **LinkedIn extractors**: `packages/lestash-linkedin/src/lestash_linkedin/extractors/changelog.py` — where `commented_on`, `reacted_to`, `post_id` metadata fields are set
- **LinkedIn source**: `packages/lestash-linkedin/src/lestash_linkedin/source.py` — sync/import logic, ItemCreate creation
- **Enrichment**: `packages/lestash/src/lestash/core/enrichment.py` — `get_author_actor()`, `get_preview()` already resolve these for display
- **Post cache**: `post_cache` table stores cached content for posts referenced by reactions/comments (separate from items table)
- **Item model**: `packages/lestash/src/lestash/models/item.py` — `ItemCreate` and `Item` both have `parent_id: int | None`

### Item counts

```sql
-- LinkedIn items by resource_name (from metadata)
SELECT json_extract(metadata, '$.resource_name') as type, COUNT(*)
FROM items WHERE source_type = 'linkedin'
GROUP BY type;
```

Rough distribution: ~733 LinkedIn items total, mix of posts (ugcPosts), comments (socialActions/comments), reactions (socialActions/likes), invitations, messages, etc.

## What Needs to Happen

### 1. Build a lookup of activity URN → item ID

Query all LinkedIn items that are posts (not reactions/comments) and extract the activity URN from whichever field it's stored in. Build a dict mapping `urn → items.id`.

Check these fields in order:
- `metadata.post_id`
- `source_id` (if it looks like a URN)
- `metadata.raw` (may contain the URN)
- `url` (may contain the activity ID as a path segment)

### 2. Update reactions and comments

For each LinkedIn item where `metadata.reacted_to` or `metadata.commented_on` is set:
1. Look up the URN in the mapping from step 1
2. If found, `UPDATE items SET parent_id = ? WHERE id = ?`
3. If not found, leave `parent_id` as NULL (the parent post isn't in the DB)

### 3. Update sync/import to set parent_id on new items

When new LinkedIn reactions/comments are synced via the changelog API, resolve `parent_id` at insert time if the parent post already exists. This is in `source.py` where `ItemCreate` objects are yielded.

### 4. Consider the feed

The default item listing already hides children (`AND parent_id IS NULL`). So once reactions/comments get a `parent_id`, they'll disappear from the main feed and only appear when drilling into their parent post's detail view. This is the desired behavior.

## Implementation

Implemented as:
1. **`lestash linkedin backfill-parents`** CLI command — one-time backfill for existing items (with `--dry-run`)
2. **Post-sync resolution** — `resolve_linkedin_parents()` runs automatically after `lestash sources sync` and `lestash linkedin fetch --changelog`
3. **Core `upsert_item()` fix** — now persists `parent_id` from `ItemCreate` (was silently ignored)

### Resolution SQL

Matches `metadata.reacted_to` / `metadata.commented_on` URNs against parent posts via `metadata.post_id` or `source_id`. An `EXISTS` guard ensures only items with a resolvable parent are updated.

### Key Files

| File | Change |
|------|--------|
| `packages/lestash/src/lestash/core/database.py` | `upsert_item()` includes `parent_id` in INSERT/UPDATE |
| `packages/lestash/src/lestash/cli/sources.py` | Inline sync SQL includes `parent_id` + post-sync resolution |
| `packages/lestash-linkedin/src/lestash_linkedin/source.py` | `resolve_linkedin_parents()`, `backfill-parents` command, fetch SQL fix |
| `packages/lestash-linkedin/tests/test_parent_resolution.py` | 9 tests covering resolution, upsert, edge cases |

### Testing

```bash
uv run just check                               # All 427+ tests pass
uv run lestash linkedin backfill-parents --dry-run  # Preview resolvable counts
uv run lestash linkedin backfill-parents            # Run backfill
```
