"""Item API endpoints."""

import json
import logging

from fastapi import APIRouter, HTTPException, Query
from lestash.core.database import (
    add_tag,
    get_item_media,
    get_tags,
    list_tags,
    mark_recent_history,
    max_history_id,
    remove_tag,
)
from lestash.core.enrichment import get_author_actor, get_item_subtype, get_preview
from lestash.models.item import Item

from lestash_server.deps import get_db
from lestash_server.models import (
    _UNSET,
    HistoryListResponse,
    HistoryVersion,
    HistoryVersionDetail,
    ItemCreateRequest,
    ItemListResponse,
    ItemPatchRequest,
    ItemResponse,
    MediaResponse,
    TagAddRequest,
    TagListResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/items", tags=["items"])


def _media_responses(conn, item_id: int) -> list[MediaResponse]:
    """Build MediaResponse list for an item."""
    return [
        MediaResponse(
            id=m["id"],
            media_type=m["media_type"],
            url=m["url"],
            serve_url=f"/api/media/{m['id']}",
            alt_text=m["alt_text"],
            position=m["position"],
            available=bool(m["local_path"] or (m["url"] and m["url"].startswith("http"))),
        )
        for m in get_item_media(conn, item_id)
    ]


def _enrich_item(conn, item: Item) -> ItemResponse:
    """Convert an Item to an enriched API response."""
    author_display, actor_display = get_author_actor(conn, item)
    child_count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE parent_id = ?", (item.id,)
    ).fetchone()[0]
    return ItemResponse(
        id=item.id,
        source_type=item.source_type,
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        content=item.content,
        author=item.author,
        created_at=item.created_at,
        fetched_at=item.fetched_at,
        is_own_content=item.is_own_content,
        metadata=item.metadata,
        parent_id=item.parent_id,
        subtype=get_item_subtype(item),
        author_display=author_display,
        actor_display=actor_display,
        preview=get_preview(conn, item, max_length=120),
        tags=get_tags(conn, item.id),
        child_count=child_count,
        media=_media_responses(conn, item.id),
    )


def _matches_exclude(subtype: str, excludes: set[str]) -> bool:
    """Check if a subtype matches any exclude term."""
    return any(ex in subtype for ex in excludes)


@router.get("", response_model=ItemListResponse)
def list_items(
    source: str | None = Query(None, description="Filter by source type"),
    own: bool | None = Query(None, description="Filter own content"),
    exclude_subtype: str | None = Query(
        None, description="Comma-separated subtypes to exclude (e.g., reaction,invitation,message)"
    ),
    since: str | None = Query(None, description="Only items fetched since this ISO datetime"),
    tag: str | None = Query(None, description="Filter by tag name"),
    parent_id: int | None = Query(None, description="Filter to children of this parent item"),
    include_children: bool = Query(False, description="Include items that have a parent"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List items with optional filters."""
    with get_db() as conn:
        count_query = "SELECT COUNT(*) FROM items WHERE 1=1"
        query = "SELECT * FROM items WHERE 1=1"
        params: list = []

        if parent_id is not None:
            query += " AND parent_id = ?"
            count_query += " AND parent_id = ?"
            params.append(parent_id)
        elif not include_children:
            query += " AND parent_id IS NULL"
            count_query += " AND parent_id IS NULL"

        if tag:
            query = (
                "SELECT items.* FROM items "
                "JOIN item_tags ON items.id = item_tags.item_id "
                "JOIN tags ON item_tags.tag_id = tags.id "
                "WHERE tags.name = ?"
            )
            count_query = (
                "SELECT COUNT(*) FROM items "
                "JOIN item_tags ON items.id = item_tags.item_id "
                "JOIN tags ON item_tags.tag_id = tags.id "
                "WHERE tags.name = ?"
            )
            params.append(tag.strip().lower())

        if source:
            query += " AND source_type = ?"
            count_query += " AND source_type = ?"
            params.append(source)

        if own is not None:
            query += " AND is_own_content = ?"
            count_query += " AND is_own_content = ?"
            params.append(own)

        if since:
            query += " AND datetime(fetched_at) >= datetime(?)"
            count_query += " AND datetime(fetched_at) >= datetime(?)"
            params.append(since)

        total = conn.execute(count_query, params).fetchone()[0]

        # Sort by fetched_at when filtering recent, otherwise by created_at
        sort_col = "fetched_at" if since else "created_at"
        query += f" ORDER BY datetime({sort_col}) DESC"

        excludes = set()
        if exclude_subtype:
            excludes = {s.strip() for s in exclude_subtype.split(",") if s.strip()}

        if excludes:
            # Fetch extra rows to account for filtered-out items
            fetch_limit = limit * 4
            query += " LIMIT ? OFFSET ?"
            params.extend([fetch_limit, offset])

            rows = conn.execute(query, params).fetchall()
            all_enriched = [_enrich_item(conn, Item.from_row(row)) for row in rows]
            items = [i for i in all_enriched if not _matches_exclude(i.subtype, excludes)]

            # Adjust total to reflect filtering (approximate)
            if all_enriched:
                filter_ratio = len(items) / len(all_enriched)
                total = int(total * filter_ratio)

            items = items[:limit]
        else:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(query, params).fetchall()
            items = [_enrich_item(conn, Item.from_row(row)) for row in rows]

    return ItemListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/search", response_model=ItemListResponse)
def search_items(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    include_children: bool = Query(True, description="Include child items in results"),
    mode: str = Query("hybrid", description="Search mode: keyword, semantic, hybrid"),
):
    """Search items using keyword (FTS5), semantic (vector), or hybrid mode.

    Supports FTS5 query syntax: AND, OR, NOT, "phrase search", prefix*.
    """
    fts_query = _sanitize_fts_query(q)

    with get_db() as conn:
        fts_results: dict[int, dict] = {}
        vec_results: dict[int, float] = {}

        # FTS5 keyword search
        if mode in ("keyword", "hybrid"):
            query = """
                SELECT items.*,
                       snippet(items_fts, 1, '<<', '>>', '...', 32) as search_snippet
                FROM items
                JOIN items_fts ON items.id = items_fts.rowid
                WHERE items_fts MATCH ?
            """
            params: list = [fts_query]
            if not include_children:
                query += " AND items.parent_id IS NULL"
            query += " ORDER BY rank LIMIT ?"
            params.append(limit * 2)

            try:
                rows = conn.execute(query, params).fetchall()
                for rank, row in enumerate(rows):
                    snippet = row["search_snippet"]
                    fts_results[row["id"]] = {
                        "row": row,
                        "rank": rank,
                        "snippet": snippet,
                    }
            except Exception:
                if mode == "keyword":
                    raise HTTPException(
                        status_code=400, detail=f"Invalid search query: {q}"
                    ) from None

        # Vector semantic search
        if mode in ("semantic", "hybrid"):
            try:
                from lestash.core.embeddings import (
                    embed_text,
                    ensure_vec_table,
                    load_vec_extension,
                    search_similar,
                )

                load_vec_extension(conn)
                ensure_vec_table(conn)
                query_emb = embed_text(q)
                similar = search_similar(conn, query_emb, limit=limit * 2)
                for rank, (item_id, _distance) in enumerate(similar):
                    vec_results[item_id] = rank
            except Exception:
                logger.warning("Vector search failed, falling back to keyword", exc_info=True)
                if mode == "semantic" and not fts_results:
                    raise HTTPException(
                        status_code=500, detail="Vector search unavailable"
                    ) from None

        # Merge results via RRF (Reciprocal Rank Fusion)
        k = 60  # RRF constant
        scores: dict[int, float] = {}
        all_ids = set(fts_results.keys()) | set(vec_results.keys())

        for item_id in all_ids:
            score = 0.0
            if item_id in fts_results:
                score += 1.0 / (k + fts_results[item_id]["rank"])
            if item_id in vec_results:
                score += 1.0 / (k + vec_results[item_id])
            scores[item_id] = score

        ranked_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:limit]

        # Build response
        items = []
        for item_id in ranked_ids:
            if item_id in fts_results:
                row = fts_results[item_id]["row"]
                item = Item.from_row(row)
                enriched = _enrich_item(conn, item)
                snippet = fts_results[item_id]["snippet"]
                if snippet:
                    enriched.preview = snippet
            else:
                row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
                if not row:
                    continue
                item = Item.from_row(row)
                enriched = _enrich_item(conn, item)
            items.append(enriched)

    return ItemListResponse(items=items, total=len(items), limit=limit, offset=0)


def _sanitize_fts_query(q: str) -> str:
    """Sanitize user input for FTS5 MATCH.

    - Preserves quoted phrases ("exact match")
    - Preserves explicit operators (AND, OR, NOT) when valid
    - Adds implicit prefix matching (word -> word*) for better UX
    - Strips dangling operators that would cause FTS5 syntax errors
    """
    q = q.strip()
    if not q:
        return q

    # Strip unbalanced quotes
    if q.count('"') % 2 != 0:
        q = q.replace('"', "")

    # Split into tokens, process each
    words = q.split()
    operators = {"AND", "OR", "NOT"}

    # Remove leading/trailing operators
    while words and words[0] in operators:
        words.pop(0)
    while words and words[-1] in operators:
        words.pop()

    if not words:
        return q.replace('"', "").strip() + "*" if q.strip() else ""

    # Add prefix matching to non-operator, non-quoted terms
    result = []
    for w in words:
        if w in operators or w.endswith("*") or w.startswith('"'):
            result.append(w)
        else:
            result.append(f"{w}*")

    return " ".join(result)


@router.post("", response_model=ItemResponse, status_code=201)
def create_item(body: ItemCreateRequest):
    """Create a single item."""
    metadata_json = json.dumps(body.metadata) if body.metadata else None
    source_id = body.source_id or body.url

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO items (
                source_type, source_id, url, title, content,
                author, created_at, is_own_content, metadata, parent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_type, source_id) DO UPDATE SET
                content = excluded.content,
                title = excluded.title,
                author = excluded.author,
                metadata = excluded.metadata,
                parent_id = excluded.parent_id
            """,
            (
                body.source_type,
                source_id,
                body.url,
                body.title,
                body.content,
                body.author,
                body.created_at,
                body.is_own_content,
                metadata_json,
                body.parent_id,
            ),
        )
        conn.commit()

        item_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            # Fetch by source_type + source_id if lastrowid didn't work (upsert case)
            row = conn.execute(
                "SELECT * FROM items WHERE source_type = ? AND source_id = ?",
                (body.source_type, source_id),
            ).fetchone()
        return _enrich_item(conn, Item.from_row(row))


@router.get("/tags", response_model=TagListResponse)
def get_all_tags():
    """List all tags with item counts."""
    from lestash_server.models import TagInfo

    with get_db() as conn:
        return TagListResponse(
            tags=[TagInfo(**t) for t in list_tags(conn)],
        )


@router.patch("/{item_id}", response_model=ItemResponse)
def update_item(item_id: int, body: ItemPatchRequest):
    """Partially update an item."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")

        updates: list[str] = []
        params: list = []

        if body.content is not None:
            updates.append("content = ?")
            params.append(body.content)

        if body.title is not _UNSET:
            updates.append("title = ?")
            params.append(body.title)

        if body.parent_id is not _UNSET:
            new_parent = body.parent_id
            if new_parent is not None:
                if new_parent == item_id:
                    raise HTTPException(status_code=400, detail="Item cannot be its own parent")
                # Check parent exists
                if not conn.execute("SELECT 1 FROM items WHERE id = ?", (new_parent,)).fetchone():
                    raise HTTPException(
                        status_code=400, detail=f"Parent item {new_parent} not found"
                    )
                # Check for circular reference
                ancestor = new_parent
                while ancestor is not None:
                    parent_row = conn.execute(
                        "SELECT parent_id FROM items WHERE id = ?", (ancestor,)
                    ).fetchone()
                    if parent_row is None:
                        break
                    ancestor = parent_row[0]
                    if ancestor == item_id:
                        raise HTTPException(
                            status_code=400,
                            detail="Circular parent reference detected",
                        )
            updates.append("parent_id = ?")
            params.append(new_parent)

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        text_changed = body.content is not None or body.title is not _UNSET
        pre_max = max_history_id(conn)

        params.append(item_id)
        conn.execute(
            f"UPDATE items SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        mark_recent_history(conn, pre_max, "user-edit")
        conn.commit()

        if text_changed:
            from lestash.core.embeddings import re_embed_item

            re_embed_item(conn, item_id)

        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _enrich_item(conn, Item.from_row(row))


def _content_preview(content: str | None, max_length: int = 200) -> str | None:
    if content is None:
        return None
    text = content.strip().replace("\n", " ")
    return text[:max_length] + ("…" if len(text) > max_length else "")


def _parse_metadata_old(metadata_old: str | None) -> dict | None:
    if not metadata_old:
        return None
    try:
        parsed = json.loads(metadata_old)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


@router.get("/{item_id}/history", response_model=HistoryListResponse)
def list_item_history(item_id: int):
    """List all history versions for an item, newest first."""
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")

        rows = conn.execute(
            """SELECT id, changed_at, change_reason, change_type,
                      title_old, content_old, parent_id_old
               FROM item_history WHERE item_id = ? ORDER BY id DESC""",
            (item_id,),
        ).fetchall()

        versions = [
            HistoryVersion(
                id=r["id"],
                changed_at=r["changed_at"],
                change_reason=r["change_reason"],
                change_type=r["change_type"],
                title_old=r["title_old"],
                content_preview=_content_preview(r["content_old"]),
                parent_id_old=r["parent_id_old"],
            )
            for r in rows
        ]
        return HistoryListResponse(versions=versions)


@router.get("/{item_id}/history/{version_id}", response_model=HistoryVersionDetail)
def get_item_history_version(item_id: int, version_id: int):
    """Get the full snapshot for one history version."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM item_history WHERE id = ? AND item_id = ?",
            (version_id, item_id),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Version {version_id} not found for item {item_id}",
            )
        return HistoryVersionDetail(
            id=row["id"],
            item_id=row["item_id"],
            changed_at=row["changed_at"],
            change_reason=row["change_reason"],
            change_type=row["change_type"],
            title_old=row["title_old"],
            content_old=row["content_old"],
            author_old=row["author_old"],
            url_old=row["url_old"],
            metadata_old=_parse_metadata_old(row["metadata_old"]),
            is_own_content_old=bool(row["is_own_content_old"])
            if row["is_own_content_old"] is not None
            else None,
            parent_id_old=row["parent_id_old"],
        )


@router.post("/{item_id}/history/{version_id}/restore", response_model=ItemResponse)
def restore_item_history_version(item_id: int, version_id: int):
    """Restore an item to a previous version.

    Writes the snapshot fields back to items; the resulting UPDATE captures
    the pre-restore state as a new history row tagged 'restore'. NULL values
    in the snapshot are written through verbatim — note that history rows
    captured before Migration 9 always have parent_id_old=NULL, so restoring
    those will clear the current parent_id.
    """
    with get_db() as conn:
        snapshot = conn.execute(
            "SELECT * FROM item_history WHERE id = ? AND item_id = ?",
            (version_id, item_id),
        ).fetchone()
        if not snapshot:
            raise HTTPException(
                status_code=404,
                detail=f"Version {version_id} not found for item {item_id}",
            )

        # content has NOT NULL on items, so refuse if snapshot's content_old is NULL
        # (would be a pathological pre-trigger row).
        if snapshot["content_old"] is None:
            raise HTTPException(
                status_code=400,
                detail="Snapshot is missing content; cannot restore",
            )

        pre_max = max_history_id(conn)
        conn.execute(
            """UPDATE items SET
                   title = ?,
                   content = ?,
                   author = ?,
                   url = ?,
                   metadata = ?,
                   is_own_content = ?,
                   parent_id = ?
               WHERE id = ?""",
            (
                snapshot["title_old"],
                snapshot["content_old"],
                snapshot["author_old"],
                snapshot["url_old"],
                snapshot["metadata_old"],
                snapshot["is_own_content_old"],
                snapshot["parent_id_old"],
                item_id,
            ),
        )
        mark_recent_history(conn, pre_max, "restore")
        conn.commit()

        from lestash.core.embeddings import re_embed_item

        re_embed_item(conn, item_id)

        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _enrich_item(conn, Item.from_row(row))


@router.get("/{item_id}", response_model=ItemResponse)
def get_item(item_id: int):
    """Get a single item by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        return _enrich_item(conn, Item.from_row(row))


@router.post("/{item_id}/tags", response_model=ItemResponse, status_code=201)
def add_item_tag(item_id: int, body: TagAddRequest):
    """Add a tag to an item."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        add_tag(conn, item_id, body.name)
        return _enrich_item(conn, Item.from_row(row))


@router.delete("/{item_id}/tags/{tag_name}", response_model=ItemResponse)
def remove_item_tag(item_id: int, tag_name: str):
    """Remove a tag from an item."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        remove_tag(conn, item_id, tag_name)
        return _enrich_item(conn, Item.from_row(row))
