# Vector Search Design

*Designed 2026-03-30 — Implemented 2026-03-30*

## Overview

Add semantic vector search to LeStash using sqlite-vec for storage and sentence-transformers for embedding generation. Combined with the existing FTS5 keyword search via hybrid ranking (reciprocal rank fusion).

## Current State

- **20,079 items** (2,597 parents, 17,482 children)
- **Content**: avg 1,632 chars, max 80,952 chars
- **DB size**: 66.3 MB
- **Search**: FTS5 keyword search only — no semantic understanding
- **System**: SQLite 3.50.4, 29 GB RAM, 80 GB disk free

## Embedding Storage

**sqlite-vec virtual table** (separate from `items` table):

```sql
CREATE VIRTUAL TABLE vec_items USING vec0(
    item_id INTEGER PRIMARY KEY,
    embedding float[384]
);
```

Why a virtual table and not a BLOB column on `items`:
- sqlite-vec provides built-in KNN distance functions (`vec_distance_L2`, `vec_distance_cosine`)
- The virtual table is optimized for vector similarity queries
- Doesn't bloat the main items table (which is heavily queried)
- Can be rebuilt independently without touching items

The `item_id` maps 1:1 to `items.id`. Not every item needs an embedding.

## What to Embed

**Problem**: content ranges from 10 chars to 80K chars. Embedding models have token limits (~512 tokens ≈ ~2000 chars for most models).

**Strategy**: embed a **summary string** per item:
```
{title} {preview or first 500 chars of content}
```

This keeps input under token limits and focuses on the semantic essence. For parent items (conversations, notebooks), the title + summary is more meaningful than the full content.

**Which items to embed**:
- **Parents only** (2,597 items) — covers all conversations, notebooks, posts
- **Skip children** — individual messages/notes are searchable via FTS5 and findable via parent_id
- This makes backfill fast: ~2,600 embeddings, not 20K

### Content length distribution

| Threshold | Items |
|-----------|-------|
| > 100 chars | 15,462 |
| > 500 chars | 10,983 |
| > 1,000 chars | 9,153 |
| > 5,000 chars | 1,227 |
| > 10,000 chars | 253 |
| > 50,000 chars | 7 |

## Embedding Model

**`all-MiniLM-L6-v2`** from sentence-transformers:
- 384 dimensions
- ~80 MB model download
- Fast: ~100 items/sec on CPU
- Good quality for semantic similarity
- Well within system resources (29 GB RAM)

## How Embeddings Are Created

### On import/insert
After `_insert_items_with_parents()` completes, embed new parent items. The import route has a central `_upsert_item` function — add embedding there.

### On update
If title or content changes, re-embed. Triggered in the API `create_item`/`update_item` endpoints.

### Backfill command
`lestash embeddings rebuild` — batch-embed all items missing vectors. Also available as `POST /api/embeddings/rebuild` for the server.

### Backfill status
`lestash embeddings status` — shows how many items have embeddings vs total.

## Sync Concerns

Unlike FTS5 (which uses SQLite triggers), sqlite-vec virtual tables **cannot be synced via triggers**. Sync must be application-level.

| Operation | Sync method |
|-----------|-------------|
| **Insert** | Embed after inserting parent items in API/import routes |
| **Update** | Re-embed in API update endpoint when title/content changes |
| **Delete** | Delete vec_items row in API delete path |
| **Bulk import** | Backfill command after import completes |
| **Model change** | Full rebuild (drop + recreate vec_items) |

Items that fail to embed (encoding issues, empty content) are silently skipped — they remain searchable via FTS5.

## Maintenance

| Task | Command / Method | When |
|------|-----------------|------|
| Backfill missing embeddings | `lestash embeddings rebuild` | After bulk imports, model changes |
| Check coverage | `lestash embeddings status` | Anytime — shows embedded/total count |
| Model upgrade | Drop vec_items, change model config, rebuild | Rare — when switching models |
| Orphan cleanup | `DELETE FROM vec_items WHERE item_id NOT IN (SELECT id FROM items)` | Periodic or on rebuild |
| Storage monitoring | DB size check | Rare — vectors add <10% to DB size |
| Dimension mismatch | Store model name in config, validate on load | On rebuild |

## Storage Math

- 2,597 parents × 384 dims × 4 bytes = **3.8 MB** for vectors
- Even with all 20K items: 20K × 384 × 4 = **30 MB**
- Current DB is 66 MB — vectors add <10% overhead

## Hybrid Search Architecture

```
User query: "autonomous agent systems"
         ↓
    ┌────┴────┐
    │ FTS5    │ keyword: "autonomous*" "agent*" "systems*"
    │ MATCH   │ → ranked by bm25
    └────┬────┘
         │
    ┌────┴────┐
    │ Vec KNN │ embed query → cosine similarity
    │ search  │ → top K nearest
    └────┬────┘
         │
    ┌────┴────────┐
    │ RRF merge   │ reciprocal rank fusion
    │ (1/(k+rank))│ combines both result sets
    └────┬────────┘
         │
    Final ranked results
```

**Reciprocal Rank Fusion (RRF)**: for each result, score = sum of `1 / (k + rank)` across both search methods (k=60 is standard). Items appearing in both FTS5 and vector results get boosted. Items found by only one method still appear but ranked lower.

This finds both exact keyword matches AND semantically similar items that use different words.

## API Endpoints

| Method | Endpoint | Description | Status |
|--------|----------|-------------|--------|
| GET | `/api/items/search?q=...&mode=hybrid` | Hybrid FTS5 + vector search | **Done** |
| POST | `/api/embeddings/rebuild` | Trigger background backfill | **Done** |
| GET | `/api/embeddings/status` | Coverage stats | **Done** |
| GET | `/api/embeddings/similar/{id}` | Find similar items | **Done** |

## CLI Commands

| Command | Description | Status |
|---------|-------------|--------|
| `lestash embeddings status` | Show model, dimensions, coverage | **Done** |
| `lestash embeddings rebuild` | Batch embed with progress bar | **Done** |

## UI Features

- Search mode dropdown: Hybrid / Keyword / Semantic
- "Similar items" section in item detail view (5 semantically related items)
- Similar items rendered as clickable cards with full drill-down navigation

## Dependencies

```toml
sqlite-vec = ">=0.1.6"
sentence-transformers = ">=3.0.0"
```

## Implementation Files

| File | Change |
|------|--------|
| `packages/lestash/pyproject.toml` | Added sqlite-vec, sentence-transformers deps |
| `packages/lestash/src/lestash/core/embeddings.py` | Model loading, embed, vec table, search, rebuild |
| `packages/lestash/src/lestash/cli/embeddings.py` | CLI: rebuild, status commands |
| `packages/lestash/src/lestash/cli/main.py` | Registered embeddings subcommand |
| `packages/lestash-server/src/lestash_server/routes/items.py` | Hybrid search with RRF merge |
| `packages/lestash-server/src/lestash_server/routes/embeddings.py` | Status, rebuild, similar endpoints |
| `packages/lestash-server/src/lestash_server/app.py` | Registered embeddings router |
| `app/src/index.html` | Search mode toggle, similar items in detail view |
