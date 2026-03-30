# Research: Search Mechanisms + Higher-Order Grouping

*Researched 2026-03-30*

## Current State

**Search**: SQLite FTS5 — keyword-based, indexes `title`, `content`, `author`. Works but no semantic understanding ("AI agents" won't find "autonomous systems").

**Grouping**: Three levels exist:
1. **source_type** — LinkedIn, Bluesky, Claude, NotebookLM, etc.
2. **parent_id** — notebook→notes, conversation→messages
3. **tags** — manual, many-to-many

**Gap**: No way to link *related items across sources*. A Claude conversation about "Hebbian learning", a NotebookLM notebook on "Dense Hebbian Learning", and a LinkedIn post about the same topic are three isolated islands.

## Search Options

### 1. FTS5 (current) — keyword search
- Already works, fast, zero dependencies
- Weakness: no semantic understanding, no typo tolerance, no concept matching
- Enhancement possible: FTS5 supports `bm25()` ranking, prefix queries, boolean operators — could improve the query UI without changing infrastructure

### 2. sqlite-vec — vector similarity in SQLite
- Native SQLite extension, vectors stored in same DB file
- Requires: embedding model to generate vectors at ingest time
- Query: `SELECT * FROM items WHERE vec_distance(embedding, ?) < 0.5 ORDER BY vec_distance`
- **20K items × 384-dim embedding ≈ 30MB** — trivial
- System has 20GB free RAM, SQLite 3.50.4 (supports extensions)
- **Best fit**: local-first, no external services, single DB file

### 3. chromadb — embedded vector DB
- Separate DB alongside SQLite, handles embeddings internally
- Simpler API but adds sync complexity (two sources of truth)
- Good for prototyping but architecturally messier

### 4. API-based embeddings (OpenAI/LiteLLM)
- Highest quality but requires network + API costs
- Could use existing LiteLLM proxy if deployed
- Batch-embed on import, query-embed on search

**Recommendation: sqlite-vec + local embeddings** for storage, with the option to use API embeddings for generation. This keeps the local-first philosophy and avoids sync issues.

### Hybrid search
The best approach is **hybrid**: FTS5 for exact keyword matches + vector similarity for semantic matches. Combine scores with reciprocal rank fusion (RRF) or simple weighted merge. This is what most modern search systems do.

## Higher-Order Grouping: Collections

The missing layer is a **collection** — a user-defined group that links items across sources, with a purpose/topic.

### Current hierarchy
```
source_type (automatic)
  └── parent_id (structural — notebook→notes, conversation→messages)
       └── tags (manual, flat labels)
```

### Proposed hierarchy
```
collections (user-defined, cross-source, with purpose)
  └── source_type (automatic)
       └── parent_id (structural)
            └── tags (labels)
```

### What is a collection?

A collection is like a "research folder" or "project" that groups related items regardless of source:

- **"Hebbian Learning Research"** — contains a NotebookLM notebook, 3 Claude conversations, 2 arXiv papers, 1 LinkedIn post
- **"Manta Project"** — contains Claude project docs, NotebookLM notes, voice memos, shared links
- **"Masters Thesis"** — cross-references across everything

### Schema design

```sql
CREATE TABLE collections (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT  -- JSON: color, icon, etc.
);

CREATE TABLE collection_items (
    collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    note TEXT,  -- optional note about why this item is in this collection
    PRIMARY KEY (collection_id, item_id)
);

CREATE INDEX idx_collection_items_item ON collection_items(item_id);
```

**Key properties:**
- An item can be in multiple collections (many-to-many)
- Adding an item to a collection optionally includes a note (context for why)
- Collections have their own metadata (description, icon/color)
- When an item with children is added, the children are implicitly accessible via parent_id

### How collections differ from tags

| Feature | Tags | Collections |
|---------|------|-------------|
| Purpose | Label/categorize | Group with intent |
| Has description | No | Yes |
| Has notes per item | No | Yes (why this item belongs) |
| Ordered | No | Could be (via position column) |
| Cross-source | Yes | Yes |
| UI | Filter chips | Dedicated view with context |

Tags are quick labels. Collections are curated groups with narrative.

### Vector search + collections synergy

With vector embeddings, collections could be **auto-suggested**: "These 5 items are semantically similar to items in your 'Hebbian Learning' collection — add them?"

## Implementation Roadmap

### Phase 1: Enhanced FTS5 — DONE (2026-03-30)
- Auto-prefix matching: `learn` matches `learning`, `learned`, etc.
- Preserves FTS5 syntax: `"quoted phrases"`, `AND`/`OR`/`NOT`, `prefix*`
- Query sanitization: strips dangling operators, fixes unbalanced quotes, returns 400 (not 500) for invalid queries
- Search results show highlighted snippets via `snippet()` function
- Result count + search tips in UI
- `include_children` param (defaults true for search — finds individual messages)
- No schema changes needed

### Phase 2: Collections — DONE (2026-03-30)
- Migration v6: `collections` + `collection_items` tables
- API: full CRUD at `/api/collections`, add/remove items
- UI: Collections tab with list view, collection detail view (master-detail navigation)
- "Add to Collection" action in item detail view with dropdown selector
- ~420 lines of code (routes, models, UI)

### Phase 3: Vector search (medium-large)
- Install sqlite-vec extension
- Add embedding generation (sentence-transformers or API)
- Migration v7: add embedding column or vec table
- Hybrid search endpoint (FTS5 + vector, merged ranking)
- Background job to embed existing items
- ~400 lines of code + model download

### Phase 4: Smart features (future)
- Auto-suggest collections based on vector similarity
- "Related items" sidebar in detail view
- Cluster visualization
- Semantic deduplication

## Summary

| Feature | Effort | Value | Status |
|---------|--------|-------|--------|
| Better FTS5 UI | Small | Medium | **Done** |
| Collections | Medium | High | **Done** |
| sqlite-vec search | Medium-Large | High | Planned |
| Auto-suggestions | Large | Medium | Future |
